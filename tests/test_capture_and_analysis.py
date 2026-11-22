# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from blazectl.analysis import byte_diff, diff_captures, group_commands, guess_checksums, pick_device
from blazectl.capture import CaptureError, load_transfers, read_frames
from fakes import (
    LINKTYPE_USBMON_MM,
    LINKTYPE_USBPCAP,
    capture_dpi,
    fake_packet,
    set_report_setup,
    get_report_setup,
    usbmon,
    usbpcap,
    write_pcap,
    write_pcapng,
)


def test_usbpcap_set_and_get_reports(tmp_path):
    path = tmp_path / "dpi.pcapng"
    capture_dpi(path, 0x2F)
    ts = load_transfers(str(path), include_in=True)
    kinds = [(t.kind, t.device) for t in ts]
    assert ("GET_REPORT", 5) in kinds
    assert ("INTR_IN", 3) in kinds
    sets = [t for t in ts if t.kind == "SET_REPORT"]
    assert len(sets) == 3  # inline + split data stage + commit
    assert all(t.report_type == "feature" and t.report_id == 8 and t.interface == 1 for t in sets)
    assert sets[0].data == fake_packet(0x07, bytes([0, 0x0C, 0x2F, 0]))
    assert sets[1].data == fake_packet(0x07, bytes([0, 0x2C, 255, 0, 0]))  # split stage reassembled
    assert pick_device(ts) == (1, 5)


def test_usbmon_capture(tmp_path):
    data = fake_packet(0x07, b"\x00\x0c\x10")
    frames = [
        usbmon(1, "S", 2, 0x00, 7, data, set_report_setup(8, length=17)),
        usbmon(1, "C", 2, 0x00, 7),
        usbmon(2, "S", 2, 0x80, 7, b"", get_report_setup(8)),
        usbmon(2, "C", 2, 0x80, 7, data),
        usbmon(3, "S", 1, 0x02, 7, b"\xaa\xbb"),  # interrupt OUT
        usbmon(4, "C", 1, 0x81, 7, b"\x01\x02"),  # interrupt IN (excluded by default)
    ]
    for writer, name in ((write_pcapng, "a.pcapng"), (write_pcap, "a.pcap")):
        path = tmp_path / name
        writer(path, frames, LINKTYPE_USBMON_MM)
        ts = load_transfers(str(path))
        assert [t.kind for t in ts] == ["SET_REPORT", "GET_REPORT", "INTR_OUT"]
        assert ts[0].data == data and ts[1].data == data and ts[2].data == b"\xaa\xbb"


def test_non_usb_capture_rejected(tmp_path):
    path = tmp_path / "eth.pcap"
    write_pcap(path, [b"\x00" * 60], 1)  # ethernet
    with pytest.raises(CaptureError, match="no USB traffic"):
        load_transfers(str(path))
    bad = tmp_path / "junk.bin"
    bad.write_bytes(b"hello world, not a capture at all")
    with pytest.raises(CaptureError, match="not a pcap"):
        list(read_frames(str(bad)))


def test_diff_isolates_dpi_byte_and_checksum(tmp_path):
    a, b = tmp_path / "dpi_800.pcapng", tmp_path / "dpi_1600.pcapng"
    capture_dpi(a, 0x0F)
    capture_dpi(b, 0x2F)
    entries = diff_captures(load_transfers(str(a), device=(1, 5)), load_transfers(str(b), device=(1, 5)))
    assert len(entries) == 1
    e = entries[0]
    assert e.op == "changed"
    assert [o for o, _, _ in e.bytes_changed] == [4, 16]  # the DPI byte and the checksum


def test_diff_reports_added_packets(tmp_path):
    a = tmp_path / "a.pcapng"
    capture_dpi(a, 0x0F)
    base = load_transfers(str(a), device=(1, 5))
    extra = base + [base[-1]]
    entries = diff_captures(base, extra)
    assert [e.op for e in entries] == ["only_b"]


def test_checksum_guesser_finds_0x55_minus_sum():
    pkts = [fake_packet(0x07, bytes([0, 0x0C, d, 0])) for d in (0x0F, 0x2F, 0x5E, 0xBD)]
    guesses = guess_checksums(pkts)
    best = guesses[0]
    assert (best.algorithm, best.start, best.position, best.constant) == ("K - sum", 0, 16, 0x55)
    assert all(best.compute(p) == p[16] for p in pkts)
    assert "0x55" in best.describe()


def test_checksum_guesser_needs_variation():
    assert guess_checksums([b"\x01\x02\x03"] * 5) == []
    # constant packets padded with zeros must not yield fake checksums
    pkts = [bytes([0x05, v]) + b"\x00" * 6 for v in range(5)]
    assert guess_checksums(pkts) == []


def test_xor_checksum():
    pkts = []
    for v in (1, 7, 42, 99):
        body = bytes([0x04, 0x11, v, v * 2 & 0xFF])
        x = 0
        for c in body:
            x ^= c
        pkts.append(body + bytes([x ^ 0xA5]))
    g = guess_checksums(pkts)[0]
    assert (g.algorithm, g.position, g.constant) == ("xor ^ K", 4, 0xA5)


def test_group_commands(tmp_path):
    a = tmp_path / "a.pcapng"
    capture_dpi(a, 0x0F)
    groups = group_commands(load_transfers(str(a), device=(1, 5)))
    sigs = {g.signature: g.count for g in groups}
    assert sigs["SET_REPORT feature id=0x08 if=1 prefix=0807"] == 2
    assert sigs["SET_REPORT feature id=0x08 if=1 prefix=0809"] == 1


def test_byte_diff_lengths():
    assert byte_diff(b"\x01\x02", b"\x01\x03\x04") == [(1, 2, 3), (2, None, 4)]


def test_truncated_capture_does_not_crash(tmp_path):
    path = tmp_path / "t.pcapng"
    write_pcapng(path, [usbpcap(1, 0, 5, 0, 2, set_report_setup(8) + b"\x08\x07", 0)], LINKTYPE_USBPCAP)
    raw = path.read_bytes()
    path.write_bytes(raw[:-10])
    assert load_transfers(str(path)) == []
