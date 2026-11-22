# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Macro format, checked against a real dump taken after the official app stored a macro."""
import json
import os

import pytest

from blazectl.drivers import instant as ins
from blazectl.drivers.macro import (MacroError, MacroEvent, decode_events, encode_events, entry_addr, key_code,
                                    pair_count, parse_script, read_macro)

DATA = os.path.join(os.path.dirname(__file__), "data")


def load(name):
    blocks = json.load(open(os.path.join(DATA, name)))["blocks"]
    mem = bytearray(0x1000)
    for k, v in blocks.items():
        a = int(k, 16)
        mem[a:a + 8] = bytes.fromhex(v)
    return bytes(mem)


RECORDED = [  # what the user recorded in the official app
    MacroEvent(0x04, True, 110), MacroEvent(0x04, False, 2232),
    MacroEvent(0x28, True, 90), MacroEvent(0x28, False, 976),
    MacroEvent(0x05, True, 80), MacroEvent(0x05, False, 20),
]


def test_real_dump_decodes_to_the_recorded_macro():
    info = read_macro(load("macro_ab.json"))
    assert info["events"] == RECORDED
    assert info["pairs"] == 30 and info["consistent"] and info["repeat"] == 1


def test_encoder_reproduces_the_apps_bytes_exactly():
    mem = load("macro_ab.json")
    enc = encode_events(RECORDED)
    assert mem[0x402:0x402 + len(enc)] == enc
    assert pair_count(RECORDED) == 30


def test_base_dump_has_no_macro():
    assert read_macro(load("macro_base.json"))["events"] == []


def test_macro_codes_are_keep_only_in_plain_button_edits():
    mem = load("macro_ab.json")
    assert ins.decode_buttons(mem[0x08:0x10], mem[0x10:0x18])[7] == 0x0D
    assert ins.FUNCTION_NAMES[0x0D] == "macro" and ins.FUNCTION_NAMES[0x0E] == "macro-hold"
    assert "macro" not in ins.BUTTON_FUNCTIONS
    b, d = ins.encode_buttons(mem[0x08:0x10], mem[0x10:0x18], {7: 0x0D, 4: 0x05})
    assert b[5] == 0xAD
    with pytest.raises(ins.InstantError, match="can't create"):   # needs set_macros (content)
        ins.encode_buttons(mem[0x08:0x10], mem[0x10:0x18], {6: 0x0D})


# ---- writing: replay what the official app did, compare with its own dumps ------- #

from test_instant import FakeInstantMouse, no_sleep  # noqa: E402
from blazectl.drivers.macro import Macro  # noqa: E402

E = MacroEvent
C = [E(0x06, True, 81), E(0x06, False, 20)]
D = [E(0x07, True, 71), E(0x07, False, 20)]
NOISE = range(0x10, 0x20)          # DPI/state bytes the app also rewrote; not macro related


def mouse_from(name):
    fake = FakeInstantMouse(load(name))
    return fake, ins.InstantMouse(fake, sleep=no_sleep, pid=0x1440)


@pytest.mark.parametrize("before,after,changes", [
    ("macro_base.json", "macro_ab.json", {7: Macro(RECORDED)}),
    ("macro_two.json", "macro_three.json", {7: 0x15, 6: Macro(C, "times", 3)}),
    ("macro_three.json", "macro_four.json", {4: Macro(C, "hold"), 6: Macro(C, "hold")}),
    ("macro_five.json", "macro_six.json", {7: Macro(D)}),
])
def test_writer_matches_the_official_app_byte_for_byte(before, after, changes):
    fake, m = mouse_from(before)
    res = m.set_macros(changes, pid=0x1440)
    want = load(after)
    assert [hex(a) for a in range(0x1000) if fake.mem[a] != want[a] and a not in NOISE] == []
    assert res["changed"] and fake.reloads == 1


def test_get_macros_reads_every_dump():
    _, m = mouse_from("macro_six.json")
    got = m.get_macros()
    assert set(got) == {4, 6, 7}
    assert got[4].mode == "hold" and got[4].events == C and got[7].events == D and got[7].repeat == 1
    _, m = mouse_from("macro_three.json")
    assert m.get_macros()[6].repeat == 3


def test_unbinding_through_set_buttons_clears_the_entry_and_compacts():
    fake, m = mouse_from("macro_six.json")
    m.set_buttons({4: 0x05, 6: 0x06})            # both users of the C macro go back to plain functions
    got = m.get_macros()
    assert set(got) == {7} and got[7].events == D
    assert fake.mem[0x400:0x408] == bytes([1, 0]) + encode_events(D)   # D moved down to 0x400
    assert fake.mem[0x6E:0x70] == bytes([0x08, 0x04])                  # pointers follow the new end
    assert not any(fake.mem[0xC9:0xD3]) and not any(fake.mem[0xE7:0xF1])
    assert fake.mem[0x08 + 4] & 0x1F == 0x05 and fake.mem[0x08 + 7] & 0x1F == 0x06


def test_set_macros_guards():
    fake, m = mouse_from("macro_six.json")
    for bad in ({1: Macro(C)}, {8: Macro(C)}, {7: Macro([])}, {7: Macro(C, "times", 0)}, {7: 0x0D}):
        with pytest.raises(ins.InstantError):
            m.set_macros(bad, pid=0x1440)
    huge = Macro([E(0x04, True, 127 * 60), E(0x04, False, 127 * 60)] , "times")
    with pytest.raises(ins.InstantError):          # 122 pairs each, three distinct copies overflow
        m.set_macros({2: huge, 3: Macro(huge.events, "times", 2), 5: Macro(huge.events, "times", 3),
                      6: Macro(huge.events, "times", 4), 7: Macro(huge.events, "times", 5)}, pid=0x1440)
    assert fake.writes() == []


def test_same_macro_twice_is_a_no_op():
    fake, m = mouse_from("macro_six.json")
    n = len(fake.writes())
    assert m.set_macros({7: Macro(D)}, pid=0x1440)["changed"] is False
    assert len(fake.writes()) == n and fake.reloads == 0


def test_json_round_trip():
    m = Macro(RECORDED, "times", 4)
    assert Macro.from_json(json.loads(json.dumps(m.to_json()))) == m


@pytest.mark.parametrize("delay", [0, 1, 126, 127, 128, 254, 255, 5000])
def test_delay_round_trip(delay):
    ev = [MacroEvent(0x06, True, delay), MacroEvent(0x06, False, 1)]
    assert decode_events(encode_events(ev))[0] == ev


def test_limits_and_parsing():
    with pytest.raises(MacroError):
        encode_events([MacroEvent(0x04, True, 127 * 200)])
    with pytest.raises(MacroError):
        key_code("nope")
    assert key_code("enter") == 0x28 and key_code("F5") == 0x3E and key_code("kpenter") == 0x58
    assert parse_script("A down 110, A up 2232, Enter tap 50") == [
        MacroEvent(4, True, 110), MacroEvent(4, False, 2232), MacroEvent(0x28, True, 50), MacroEvent(0x28, False, 50)]
    with pytest.raises(MacroError):
        decode_events(b"\x7f\x00\x00\x00")       # filler with nothing before it
    with pytest.raises(MacroError):
        decode_events(b"\x84\x04")               # no end marker


# ---- media keys and shortcuts (m7_base -> m7_ms) ------------------------------------------ #

from blazectl.drivers.macro import Media, Shortcut, binding_from_json  # noqa: E402


def test_media_and_shortcut_match_the_official_app_byte_for_byte():
    fake, m = mouse_from("media_base.json")
    m.set_macros({5: Shortcut(["ctrl"], 0x06), 4: Media("volume-up"), 7: Media("play-pause")}, pid=0x1440)
    want = load("media_shortcut.json")
    assert [hex(a) for a in range(0x1000) if fake.mem[a] != want[a] and a not in NOISE] == []


def test_media_and_shortcut_read_back_from_the_apps_dump():
    _, m = mouse_from("media_shortcut.json")
    got = m.get_macros()
    assert got[4] == Media("volume-up") and got[7] == Media("play-pause")
    assert got[5] == Shortcut(["ctrl"], 0x06) and got[5].label == "Ctrl+C"
    assert got[6].mode == "hold" and isinstance(got[6], Macro)     # a recorded hold macro stays a macro


def test_shortcut_parse_and_order():
    sc = Shortcut.parse("Shift+ctrl+Esc")
    assert sc.label == "Ctrl+Shift+Esc"                            # stored in the app's modifier order
    assert sc.blob() == bytes.fromhex("0100 84e0 84e1 8429 0000")
    for bad in ("ctrl+", "ctrl+shift", "hyper+c", ""):
        with pytest.raises(MacroError):
            Shortcut.parse(bad)


def test_binding_json_round_trip():
    for b in (Media("mute"), Shortcut(["alt"], 0x3D), Macro(RECORDED, "times", 2)):
        assert binding_from_json(json.loads(json.dumps(b.to_json()))) == b
    with pytest.raises(MacroError):
        binding_from_json({"type": "media", "key": "explode"})
    with pytest.raises(MacroError):
        binding_from_json({"type": "teleport"})


def test_switching_between_kinds_and_back():
    fake, m = mouse_from("media_shortcut.json")
    m.set_buttons({4: 0x05, 5: 0x04, 7: 0x15})                     # all back to plain functions
    got = m.get_macros()
    assert set(got) == {6}
    assert not any(fake.mem[0xBF:0xD3]) and fake.mem[0x08 + 3] & 0x1F == 0x04
    m.set_macros({3: Media("browser"), 2: Shortcut(["win"], 0x07)}, pid=0x1440)
    got = m.get_macros()
    assert got[3] == Media("browser") and got[2] == Shortcut(["win"], 0x07)
    assert fake.mem[entry_addr(1) + 2:entry_addr(1) + 4] == bytes([0x96, 0x01])   # 16-bit usage 0x196
