# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from blazectl import replay as replay_mod
from blazectl.capture import load_transfers
from blazectl.drivers import BlazeUltraDriver
from blazectl.hidraw import HidrawNode
from blazectl.protocol import ApplyError, Command, DpiLevel, MouseDriver, Profile, ProfileStore, hex_to_rgb
from fakes import FakeMouse, capture_dpi, fake_packet
from test_descriptor_and_hidraw import VENDOR


class DemoDriver(MouseDriver):
    """Encodes the fake protocol from fakes.py, to exercise apply()."""

    def encode_profile(self, profile):
        cmds = []
        for i, lvl in enumerate(profile.dpi_levels):
            pkt = fake_packet(0x07, bytes([i, lvl.dpi // 100]))
            cmds.append(Command(f"dpi level {i + 1} = {lvl.dpi}", "feature", pkt, readback=(8, 17)))
        r, g, b = hex_to_rgb(profile.lighting.color)
        cmds.append(Command("lighting", "feature", fake_packet(0x07, bytes([0x2C, r, g, b])), readback=(8, 17)))
        return cmds


def test_profile_roundtrip_and_validation(tmp_path):
    store = ProfileStore(str(tmp_path))
    p = Profile(name="fps")
    store.save(p)
    assert store.list() == ["fps"]
    assert store.load("fps") == p
    assert p.validate() == []
    bad = Profile(dpi_levels=[DpiLevel(50, "red")], polling_hz=333, active_dpi=4)
    errs = bad.validate()
    assert len(errs) == 4
    with pytest.raises(ValueError):
        store.path("../../etc/passwd")


def test_apply_verifies_each_step():
    mouse = FakeMouse()
    results = DemoDriver(mouse).apply(Profile())
    assert all(r.verified for r in results)
    assert len(mouse.writes) == 4


def test_apply_detects_write_that_did_not_stick():
    with pytest.raises(ApplyError, match="did NOT stick"):
        DemoDriver(FakeMouse(sticky=False)).apply(Profile())


def test_invalid_profile_sends_nothing():
    mouse = FakeMouse()
    with pytest.raises(ApplyError, match="nothing was sent"):
        DemoDriver(mouse).apply(Profile(polling_hz=7))
    assert mouse.writes == []


def test_mid_apply_failure_names_the_step():
    with pytest.raises(ApplyError, match=r"step 3/4 .* Steps 1-2 were applied"):
        DemoDriver(FakeMouse(fail_on_write=2)).apply(Profile())


def test_dry_run_touches_nothing():
    results = DemoDriver(None).apply(Profile(), dry_run=True)
    assert len(results) == 4 and all(r.verified is None for r in results)


def test_blaze_driver_explains_what_is_missing():
    with pytest.raises(NotImplementedError, match="DPI isn't decoded yet"):
        BlazeUltraDriver(None).apply(Profile(), dry_run=True)


def _node(name="hidraw5", iface=1):
    return HidrawNode(name, f"/dev/{name}", 3, 0x30FA, 0x1701, "Fake", iface, VENDOR)


def test_recipe_extract_and_replay(tmp_path, monkeypatch):
    cap = tmp_path / "red.pcapng"
    capture_dpi(cap, 0x2F)
    transfers = [t for t in load_transfers(str(cap), device=(1, 5)) if t.host_to_device]
    recipe = replay_mod.make_recipe("red", transfers, "30fa:1701")
    path = tmp_path / "red.json"
    path.write_text(json.dumps(recipe))
    recipe = replay_mod.load_recipe(str(path))

    lines = replay_mod.replay(recipe, [_node("hidraw4", 0), _node("hidraw5", 1)], execute=False)
    assert [ln.action for ln in lines] == ["set_feature"] * 3
    assert all(ln.node == "hidraw5" for ln in lines)  # routed by interface number
    assert lines[0].data == transfers[0].data  # report ID already leads the data

    mouse = FakeMouse()
    monkeypatch.setattr(replay_mod, "make_device", lambda node: mouse)
    replay_mod.replay(recipe, [_node()], execute=True, delay=0)
    assert [d for _, d in mouse.writes] == [t.data for t in transfers]


def test_wire_buffer_adds_report_id_zero():
    n = _node()
    assert replay_mod.wire_buffer({"kind": "SET_REPORT", "report_id": 0, "data": "aabb"}, n) == b"\x00\xaa\xbb"
    assert replay_mod.wire_buffer({"kind": "SET_REPORT", "report_id": 8, "data": "0102"}, n) == b"\x08\x01\x02"
    assert replay_mod.wire_buffer({"kind": "INTR_OUT", "report_id": None, "data": "0102"}, n) == b"\x01\x02"
