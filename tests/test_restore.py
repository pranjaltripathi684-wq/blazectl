# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""`blazectl restore` repairs the real damage seen on 2026-11-23 (tests/data/damaged_20261123.json)
from the previous day's dump (media_shortcut.json), on a simulated mouse."""
import json
from pathlib import Path

import pytest

from blazectl import audit, cli
from blazectl.drivers import instant as ins
from blazectl.hidraw import HidrawNode
from sim import make_service
from test_instant import CONTROL_DESC

DATA = Path(__file__).parent / "data"
DAMAGED = [0x18, 0x50, 0x68, 0x70, 0x78, 0xA8, 0xD0, 0xD8, 0x110, 0x140]


def image(name):
    blocks = json.loads((DATA / name).read_text())["blocks"]
    mem = bytearray(0x1000)
    for k, v in blocks.items():
        a = int(k, 16)
        if a < 0x1000:
            mem[a:a + 8] = bytes.fromhex(v)
    return mem


@pytest.fixture
def damaged_mouse(tmp_path, monkeypatch):
    svc, fake = make_service(tmp_path)
    fake.mem[:0x1000] = image("damaged_20261123.json")
    node = HidrawNode("hid6", "hid6", 0, 0x30FA, 0x1440, "INSTANT USB Gaming Mouse", 1, CONTROL_DESC)

    class Ctx:
        def __enter__(self):
            return fake

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(cli, "enumerate_nodes", lambda: [node])
    monkeypatch.setattr(cli, "make_device", lambda n: Ctx())
    monkeypatch.setattr(ins.time, "sleep", lambda s: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("blazectl.service.MouseService.restore_lighting", lambda self: None)
    return svc, fake


def test_damage_is_what_the_audit_and_driver_see(damaged_mouse):
    svc, fake = damaged_mouse
    with pytest.raises(ins.InstantError, match="macro entry for slot 5|media usage"):
        ins.InstantMouse(fake, pid=0x1440).get_macros()
    titles = [f.title for f in audit.audit_memory(bytes(fake.mem[:0x1000])).findings]
    assert "the macro area doesn't decode cleanly" in titles and "data in a normally-empty area" in titles


def test_list_suggests_only_damaged_blocks(damaged_mouse, capsys):
    cli.main(["restore", str(DATA / "media_shortcut.json")])
    out = capsys.readouterr().out
    assert "0x010" in out and "on purpose" in out                  # DPI differs, but is never suggested
    suggested = out.split("--blocks ")[1].split()[0]
    assert sorted(int(x, 16) for x in suggested.split(",")) == DAMAGED


def test_dry_run_then_execute_repairs_it(damaged_mouse, capsys, tmp_path):
    svc, fake = damaged_mouse
    good = image("media_shortcut.json")
    blocks = ",".join(hex(a) for a in DAMAGED)
    cli.main(["restore", str(DATA / "media_shortcut.json"), "--blocks", blocks])
    assert "dry run: 10 block(s)" in capsys.readouterr().out and fake.writes() == []
    cli.main(["restore", str(DATA / "media_shortcut.json"), "--blocks", blocks, "--execute"])
    out = capsys.readouterr().out
    assert "restored and verified" in out and list(tmp_path.glob("pre-restore-*.json"))
    for a in DAMAGED:
        assert bytes(fake.mem[a:a + 8]) == bytes(good[a:a + 8])
    assert bytes(fake.mem[0x10:0x18]) != bytes(good[0x10:0x18])     # DPI left alone (not requested)
    macros = ins.InstantMouse(fake, pid=0x1440).get_macros()
    assert macros[7].to_json() == {"type": "media", "key": "play-pause"}
    assert macros[4].to_json() == {"type": "media", "key": "volume-up"}
    assert audit.audit_memory(bytes(fake.mem[:0x1000])).worst != "high"


def test_refuses_header_and_foreign_backups(damaged_mouse, tmp_path):
    other = json.loads((DATA / "media_shortcut.json").read_text())
    with pytest.raises(SystemExit, match="never written"):
        cli.main(["restore", str(DATA / "media_shortcut.json"), "--blocks", "0x0,0x18"])
    other["blocks"]["0x00"] = "5a03a54099999999"
    p = tmp_path / "foreign.json"
    p.write_text(json.dumps(other))
    with pytest.raises(SystemExit, match="isn't this mouse"):
        cli.main(["restore", str(p), "--blocks", "0x18"])
