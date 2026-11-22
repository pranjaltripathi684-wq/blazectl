# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read USB captures (.pcap / .pcapng) and pull out HID traffic.

Supports both ways you are likely to capture the official software:

* Windows + Wireshark + USBPcap  (link type 249)
* Linux usbmon, e.g. a Windows VM with USB passthrough captured on the host
  (link types 189 and 220)

The result is a list of HidTransfer objects: SET_REPORT / GET_REPORT control
transfers and interrupt OUT (and optionally IN) data.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

LINKTYPE_USB_LINUX = 189
LINKTYPE_USB_LINUX_MMAPPED = 220
LINKTYPE_USBPCAP = 249
SUPPORTED_LINKTYPES = {LINKTYPE_USB_LINUX, LINKTYPE_USB_LINUX_MMAPPED, LINKTYPE_USBPCAP}

TRANSFER_NAMES = {0: "iso", 1: "interrupt", 2: "control", 3: "bulk"}
REPORT_TYPE_NAMES = {1: "input", 2: "output", 3: "feature"}


class CaptureError(Exception):
    pass


# --------------------------------------------------------------------------- #
# File containers
# --------------------------------------------------------------------------- #


def read_frames(path: str) -> Iterator[tuple[int, float, bytes]]:
    """Yield (linktype, timestamp, frame) from a pcap or pcapng file."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 24:
        raise CaptureError(f"{path}: file too small to be a capture")
    magic = data[:4]
    if magic == b"\x0a\x0d\x0d\x0a":
        yield from _read_pcapng(data)
    elif magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"):
        yield from _read_pcap(data, "<", magic == b"\x4d\x3c\xb2\xa1")
    elif magic in (b"\xa1\xb2\xc3\xd4", b"\xa1\xb2\x3c\x4d"):
        yield from _read_pcap(data, ">", magic == b"\xa1\xb2\x3c\x4d")
    else:
        raise CaptureError(f"{path}: not a pcap/pcapng file")


def _read_pcap(data: bytes, e: str, nanos: bool):
    linktype = struct.unpack_from(e + "I", data, 20)[0]
    div = 1e9 if nanos else 1e6
    pos = 24
    while pos + 16 <= len(data):
        sec, frac, incl, _orig = struct.unpack_from(e + "IIII", data, pos)
        pos += 16
        yield linktype, sec + frac / div, data[pos : pos + incl]
        pos += incl


def _tsresol(options: bytes, e: str) -> float:
    pos = 0
    while pos + 4 <= len(options):
        code, length = struct.unpack_from(e + "HH", options, pos)
        if code == 0:
            break
        if code == 9 and length >= 1:
            v = options[pos + 4]
            return float(2 ** (v & 0x7F)) if v & 0x80 else float(10**v)
        pos += 4 + ((length + 3) & ~3)
    return 1e6


def _read_pcapng(data: bytes):
    e = "<"
    ifaces: list[tuple[int, float]] = []
    pos = 0
    while pos + 12 <= len(data):
        if data[pos : pos + 4] == b"\x0a\x0d\x0d\x0a":
            e = "<" if data[pos + 8 : pos + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            ifaces = []
            btype = 0x0A0D0D0A
        else:
            btype = struct.unpack_from(e + "I", data, pos)[0]
        blen = struct.unpack_from(e + "I", data, pos + 4)[0]
        if blen < 12 or pos + blen > len(data):
            break
        body = data[pos + 8 : pos + blen - 4]
        if btype == 1:  # Interface Description Block
            linktype = struct.unpack_from(e + "H", body, 0)[0]
            ifaces.append((linktype, _tsresol(body[8:], e)))
        elif btype == 6 and ifaces:  # Enhanced Packet Block
            iface, th, tl, caplen, _ = struct.unpack_from(e + "IIIII", body, 0)
            if iface < len(ifaces):
                lt, div = ifaces[iface]
                yield lt, ((th << 32) | tl) / div, body[20 : 20 + caplen]
        elif btype == 3 and ifaces:  # Simple Packet Block
            orig = struct.unpack_from(e + "I", body, 0)[0]
            yield ifaces[0][0], 0.0, body[4 : 4 + orig]
        pos += blen


# --------------------------------------------------------------------------- #
# Link-layer decoders
# --------------------------------------------------------------------------- #


@dataclass
class UsbPacket:
    ts: float
    source: str  # "usbpcap" | "usbmon"
    urb_id: int
    bus: int
    device: int
    endpoint: int
    transfer: str
    is_completion: bool
    setup: bytes | None
    data: bytes
    stage: int | None = None  # USBPcap control stage: 0 setup, 1 data, 2 status, 3 complete

    @property
    def direction_in(self) -> bool:
        return bool(self.endpoint & 0x80)


def decode_usbpcap(frame: bytes, ts: float) -> UsbPacket | None:
    if len(frame) < 27:
        return None
    hlen, irp, _status, _func, info, bus, dev, ep, xfer, dlen = struct.unpack_from(
        "<HQIHBHHBBI", frame, 0
    )
    stage = frame[27] if xfer == 2 and hlen >= 28 and len(frame) >= 28 else None
    payload = frame[hlen : hlen + dlen]
    setup = None
    if xfer == 2 and stage == 0 and not (info & 1) and len(payload) >= 8:
        setup, payload = payload[:8], payload[8:]
    return UsbPacket(
        ts=ts,
        source="usbpcap",
        urb_id=irp,
        bus=bus,
        device=dev,
        endpoint=ep,
        transfer=TRANSFER_NAMES.get(xfer, str(xfer)),
        is_completion=bool(info & 1),
        setup=setup,
        data=payload,
        stage=stage,
    )


def decode_usbmon(frame: bytes, ts: float, mmapped: bool) -> UsbPacket | None:
    hdr = 64 if mmapped else 48
    if len(frame) < hdr:
        return None
    (urb, typ, xfer, ep, dev, bus, flag_setup, _flag_data, sec, usec, _status, _length, len_cap) = (
        struct.unpack_from("<QBBBBHbbqiiII", frame, 0)
    )
    is_submit = typ == ord("S")
    setup = frame[40:48] if (xfer == 2 and is_submit and flag_setup == 0) else None
    return UsbPacket(
        ts=sec + usec / 1e6 if sec else ts,
        source="usbmon",
        urb_id=urb,
        bus=bus,
        device=dev,
        endpoint=ep,
        transfer=TRANSFER_NAMES.get(xfer, str(xfer)),
        is_completion=typ == ord("C"),
        setup=setup,
        data=frame[hdr : hdr + len_cap],
    )


def read_usb_packets(path: str) -> list[UsbPacket]:
    packets = []
    seen_linktypes = set()
    for lt, ts, frame in read_frames(path):
        seen_linktypes.add(lt)
        if lt == LINKTYPE_USBPCAP:
            p = decode_usbpcap(frame, ts)
        elif lt in (LINKTYPE_USB_LINUX, LINKTYPE_USB_LINUX_MMAPPED):
            p = decode_usbmon(frame, ts, lt == LINKTYPE_USB_LINUX_MMAPPED)
        else:
            continue
        if p is not None:
            packets.append(p)
    if seen_linktypes and not (seen_linktypes & SUPPORTED_LINKTYPES):
        raise CaptureError(
            f"{path}: no USB traffic found (link types {sorted(seen_linktypes)}). "
            "Capture on a USBPcap or usbmon interface."
        )
    return packets


# --------------------------------------------------------------------------- #
# HID-level view
# --------------------------------------------------------------------------- #


@dataclass
class HidTransfer:
    index: int
    ts: float
    bus: int
    device: int
    kind: str  # SET_REPORT | GET_REPORT | INTR_OUT | INTR_IN
    report_type: str | None
    report_id: int | None
    interface: int | None
    endpoint: int
    data: bytes

    @property
    def host_to_device(self) -> bool:
        return self.kind in ("SET_REPORT", "INTR_OUT")

    def label(self) -> str:
        if self.kind in ("SET_REPORT", "GET_REPORT"):
            return f"{self.kind} {self.report_type} id=0x{self.report_id:02x} if={self.interface}"
        return f"{self.kind} ep=0x{self.endpoint:02x}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "report_type": self.report_type,
            "report_id": self.report_id,
            "interface": self.interface,
            "endpoint": self.endpoint,
            "data": self.data.hex(),
        }


def extract_hid_transfers(packets: list[UsbPacket], include_in: bool = False) -> list[HidTransfer]:
    out: list[HidTransfer] = []
    pending: dict[tuple, dict] = {}

    def emit(p: UsbPacket, info: dict, data: bytes):
        out.append(
            HidTransfer(
                index=len(out),
                ts=p.ts,
                bus=p.bus,
                device=p.device,
                kind=info["kind"],
                report_type=REPORT_TYPE_NAMES.get(info["rtype"], str(info["rtype"])),
                report_id=info["rid"],
                interface=info["iface"],
                endpoint=0,
                data=bytes(data),
            )
        )

    for p in packets:
        key = (p.source, p.bus, p.device, p.urb_id)
        if p.transfer == "control":
            if p.setup is not None and not p.is_completion:
                bm, breq, wval, widx, _wlen = struct.unpack("<BBHHH", p.setup)
                info = {"rtype": wval >> 8, "rid": wval & 0xFF, "iface": widx}
                if bm == 0x21 and breq == 0x09:
                    info["kind"] = "SET_REPORT"
                    if p.data:
                        emit(p, info, p.data)
                    else:  # USBPcap may log the data stage separately
                        pending[key] = info
                elif bm == 0xA1 and breq == 0x01:
                    info["kind"] = "GET_REPORT"
                    pending[key] = info
            elif key in pending:
                info = pending[key]
                if info["kind"] == "SET_REPORT" and not p.is_completion and p.data:
                    emit(p, pending.pop(key), p.data)
                elif info["kind"] == "GET_REPORT" and p.is_completion and p.data:
                    emit(p, pending.pop(key), p.data)
        elif p.transfer == "interrupt" and p.data:
            if not p.direction_in and not p.is_completion:
                kind = "INTR_OUT"
            elif include_in and p.direction_in and p.is_completion:
                kind = "INTR_IN"
            else:
                continue
            out.append(
                HidTransfer(len(out), p.ts, p.bus, p.device, kind, None, None, None, p.endpoint, bytes(p.data))
            )
    return out


def load_transfers(
    path: str, device: tuple[int, int] | None = None, include_in: bool = False
) -> list[HidTransfer]:
    transfers = extract_hid_transfers(read_usb_packets(path), include_in=include_in)
    if device is not None:
        bus, dev = device
        transfers = [t for t in transfers if t.bus == bus and t.device == dev]
        for i, t in enumerate(transfers):
            t.index = i
    return transfers
