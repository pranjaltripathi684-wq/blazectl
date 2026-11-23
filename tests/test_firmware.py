# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only firmware exploration: address map, mirror detection, code heuristics, app scan."""
import json
import os
import random

import pytest

from blazectl import cli
from blazectl import firmware as fw
from blazectl.drivers import instant as ins
from sim import make_service


def wrapped_reader(mem: bytes, size: int):
    """A chip whose address bus wraps every `size` bytes (a very common behaviour)."""
    def read(addr):
        a = addr % size
        return bytes(mem[a:a + 8])
    return read


def test_map_finds_wraparound_and_regions(tmp_path):
    _, fake = make_service(tmp_path)
    mem = bytes(fake.mem[:0x1000])
    rep = fw.map_memory(wrapped_reader(mem, 0x1000))
    assert rep.mirror_size == 0x1000
    kinds = [(r.start, r.kind) for r in rep.regions]
    assert (0x0, "data") in kinds and any(k == "mirror" for _, k in kinds)
    assert all(r.kind == "mirror" for r in rep.regions if r.start >= 0x1000)
    assert "probably 4 KB" in rep.text()


def test_map_stops_when_the_chip_stops_answering():
    mem = bytes([0x5A, 0x03, 0xA5, 0x40, 0x14, 0xFA, 0x30, 0x10]) + bytes(0x2000 - 8)

    def read(addr):
        if addr >= 0x2000:
            raise OSError("timeout")
        return mem[addr:addr + 8]
    rep = fw.map_memory(read)
    assert rep.regions[-1].kind == "no-reply" and any("no reply at 0x2000" in n for n in rep.notes)


def test_analyze_config_memory_says_no_code(tmp_path):
    _, fake = make_service(tmp_path)
    rep = fw.analyze_image(bytes(fake.mem[:0x1000]) * 2)
    notes = " ".join(rep.notes)
    assert "configuration memory" in notes and "repeats every 0x1000" in notes and "no page has code-like entropy" in notes


def test_analyze_recognises_code():
    rnd = random.Random(7)
    arm = (0x20001000).to_bytes(4, "little") + (0x00000101).to_bytes(4, "little") + bytes(rnd.randrange(256) for _ in range(0x2000))
    notes = " ".join(fw.analyze_image(arm).notes)
    assert "ARM Cortex-M vector table" in notes and "code-like entropy" in notes and "no page" not in notes
    c51 = bytes([0x02, 0x01, 0x00]) + bytes(0x200)
    assert any("8051 LJMP" in n for n in fw.analyze_image(c51).notes)
    rep = fw.analyze_image(b"\xff" * 64 + b"EVOFOX BLAZE v1.07\x00" + b"\xff" * 64)
    assert (64, "EVOFOX BLAZE v1.07") in rep.strings


def test_entropy_bounds():
    assert fw.entropy(b"\x00" * 100) == 0 and fw.entropy(bytes(range(256))) == pytest.approx(8.0)


def test_appscan_finds_ids_firmware_and_words(tmp_path):
    app = tmp_path / "EvoFox"
    (app / "Firmware").mkdir(parents=True)
    (app / "Firmware" / "BLAZE_V107.bin").write_bytes(os.urandom(40000))
    (app / "devices.ini").write_text("[mouse]\nhid=VID_30FA&PID_1440\nother=vid_30fa&pid_1040\n")
    (app / "EvoFox.exe").write_bytes(b"MZ" + b"\x00" * 100 + "Firmware upgrade failed".encode("utf-16-le")
                                     + "\\\\?\\HID#VID_30FA&PID_1540&MI_01".encode("utf-16-le") + b"\x00" * 50)
    (app / "logo.png").write_bytes(os.urandom(50000))
    (app / "skin.jpg").write_bytes(b"\xff\xd8 junk isp iap dfu " + os.urandom(100))
    (app / "skin.ini").write_text("DisplayMode=1\nRedisplay=0\n")
    res = fw.scan_app_folder(str(app))
    assert res.files == 6
    assert any("BLAZE_V107.bin" in f for f in res.firmware_candidates)
    assert not any("logo.png" in f for f in res.firmware_candidates)
    assert {"30fa:1440", "30fa:1040", "30fa:1540"} <= set(res.usb_ids)
    assert "firmware" in res.keywords and "upgrade" in res.keywords
    assert "isp" not in res.keywords and "dfu" not in res.keywords      # no JPG noise, no "display"
    assert any("Firmware upgrade failed" in s for s in res.interesting_strings["EvoFox.exe"])


def test_cli_firmware_reads_only(tmp_path, monkeypatch, capsys):
    _, fake = make_service(tmp_path)
    fake.mem.extend(bytes(0x10000 - len(fake.mem)))

    class Ctx:
        def __enter__(self):
            return fake

        def __exit__(self, *a):
            pass
    from blazectl.hidraw import HidrawNode
    from test_instant import CONTROL_DESC
    node = HidrawNode("hid6", "hid6", 0, 0x30FA, 0x1440, "INSTANT USB Gaming Mouse", 1, CONTROL_DESC)
    monkeypatch.setattr(cli, "enumerate_nodes", lambda: [node])
    monkeypatch.setattr(cli, "make_device", lambda n: Ctx())
    monkeypatch.setattr(ins.time, "sleep", lambda s: None)
    cli.main(["firmware", "map"])
    out = capsys.readouterr().out
    assert "Address map" in out
    dump = tmp_path / "fw.json"
    cli.main(["firmware", "dump", "--end", "0x2000", "--save", str(dump)])
    assert len(json.loads(dump.read_text())["blocks"]) == 0x400
    cli.main(["firmware", "analyze", str(dump)])
    assert "configuration memory" in capsys.readouterr().out
    assert fake.writes() == [] and fake.reloads == 0          # nothing but reads
