# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Builders for synthetic USB captures and a fake mouse, used by the tests.

The fake mouse speaks an E-YOOSO-style protocol (17-byte feature report 0x08,
checksum = 0x55 - sum of bytes 0..15) purely so the analysis tools have
something realistic to chew on. It says nothing about the real Blaze Ultra.
"""

import struct

LINKTYPE_USBPCAP = 249
LINKTYPE_USBMON_MM = 220


def fake_packet(cmd: int, payload: bytes) -> bytes:
    body = bytes([0x08, cmd]) + payload.ljust(14, b"\x00")
    return body + bytes([(0x55 - sum(body)) & 0xFF])


def set_report_setup(rid: int, rtype: int = 3, iface: int = 1, length: int = 17) -> bytes:
    return struct.pack("<BBHHH", 0x21, 0x09, (rtype << 8) | rid, iface, length)


def get_report_setup(rid: int, rtype: int = 3, iface: int = 1, length: int = 17) -> bytes:
    return struct.pack("<BBHHH", 0xA1, 0x01, (rtype << 8) | rid, iface, length)


# ---- USBPcap (Windows) ---------------------------------------------------- #


def usbpcap(irp, info, dev, ep, xfer, payload, stage=None, bus=1):
    hlen = 28 if xfer == 2 else 27
    hdr = struct.pack("<HQIHBHHBBI", hlen, irp, 0, 0, info, bus, dev, ep, xfer, len(payload))
    if xfer == 2:
        hdr += bytes([stage])
    return hdr + payload


def usbpcap_set_report(irp, dev, data, rid=8, split=False):
    setup = set_report_setup(rid, length=len(data))
    if split:
        return [usbpcap(irp, 0, dev, 0x00, 2, setup, 0), usbpcap(irp, 0, dev, 0x00, 2, data, 1)]
    return [usbpcap(irp, 0, dev, 0x00, 2, setup + data, 0)]


def usbpcap_get_report(irp, dev, response, rid=8):
    return [
        usbpcap(irp, 0, dev, 0x80, 2, get_report_setup(rid), 0),
        usbpcap(irp, 1, dev, 0x80, 2, response, 3),
    ]


# ---- usbmon (Linux) ------------------------------------------------------- #


def usbmon(urb, typ, xfer, ep, dev, data=b"", setup=None, bus=1):
    flag_setup = 0 if setup is not None else ord("-")
    hdr = struct.pack(
        "<QBBBBHbbqiiII", urb, ord(typ), xfer, ep, dev, bus, flag_setup, 0, 1700000000, 0, 0, len(data), len(data)
    )
    hdr += setup if setup is not None else b"\x00" * 8
    hdr += b"\x00" * 16  # mmapped extension -> 64-byte header
    return hdr + data


# ---- file writers --------------------------------------------------------- #


def _block(btype: int, body: bytes) -> bytes:
    body += b"\x00" * ((4 - len(body) % 4) % 4)
    n = len(body) + 12
    return struct.pack("<II", btype, n) + body + struct.pack("<I", n)


def write_pcapng(path, frames, linktype):
    out = _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    out += _block(1, struct.pack("<HHI", linktype, 0, 65535))
    for i, f in enumerate(frames):
        ts = 1_700_000_000_000_000 + i * 1000
        out += _block(6, struct.pack("<IIIII", 0, ts >> 32, ts & 0xFFFFFFFF, len(f), len(f)) + f)
    with open(path, "wb") as fh:
        fh.write(out)


def write_pcap(path, frames, linktype):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)
    for i, f in enumerate(frames):
        out += struct.pack("<IIII", 1_700_000_000 + i, 0, len(f), len(f)) + f
    with open(path, "wb") as fh:
        fh.write(out)


def capture_dpi(path, dpi_code: int, color=(255, 0, 0), dev=5, noise_dev=3):
    """A capture of 'the official software' setting one DPI level, plus noise from another device."""
    frames = []
    frames += usbpcap_get_report(1, dev, fake_packet(0x03, b"\x01"))  # handshake read
    frames.append(usbpcap(2, 1, noise_dev, 0x81, 1, b"\x00\x05\xfb\x00"))  # keyboard noise (IN)
    frames += usbpcap_set_report(3, dev, fake_packet(0x07, bytes([0x00, 0x0C, dpi_code, 0x00])))
    frames += usbpcap_set_report(4, dev, fake_packet(0x07, bytes([0x00, 0x2C, *color])), split=True)
    frames += usbpcap_set_report(5, dev, fake_packet(0x09, b""))  # commit
    write_pcapng(path, frames, LINKTYPE_USBPCAP)


# ---- fake device ---------------------------------------------------------- #


class FakeMouse:
    """In-memory stand-in for HidrawDevice."""

    def __init__(self, sticky=True, fail_on_write=None):
        self.features: dict[int, bytes] = {}
        self.writes: list[tuple[str, bytes]] = []
        self.sticky = sticky
        self.fail_on_write = fail_on_write

    def set_feature(self, data):
        if self.fail_on_write is not None and len(self.writes) == self.fail_on_write:
            raise OSError(5, "Input/output error")
        self.writes.append(("feature", bytes(data)))
        if self.sticky:
            self.features[data[0]] = bytes(data)
        return len(data)

    def get_feature(self, rid, length):
        return self.features.get(rid, bytes([rid]) + b"\x00" * (length - 1))[:length]

    def write_output(self, data):
        self.writes.append(("output", bytes(data)))
        return len(data)

    def read_input(self, timeout=0.5, size=64):
        return None

    def open(self):
        return self

    def close(self):
        pass
