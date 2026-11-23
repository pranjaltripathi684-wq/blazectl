# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""blazectl audit: finds stored key sequences that could run commands. Uses real macro bytes
written by the same code that writes them to the mouse (simulated)."""
import json

import pytest

from blazectl import audit, cli
from blazectl.drivers import instant as ins
from blazectl.drivers.macro import Macro, MacroEvent as E, Media, Shortcut, key_code, parse_script
from sim import make_service

WIN, CTRL, ALT, SHIFT = 0xE3, 0xE0, 0xE2, 0xE1


def tap(k, d=20):
    return [E(k, True, d), E(k, False, d)]


def typed(text):
    out = []
    for ch in text:
        if ch == " ":
            out += tap(0x2C)
        elif ch == "/":
            out += tap(0x38)
        else:
            out += tap(key_code(ch))
    return out


def combo(*keys):
    return [E(k, True, 10) for k in keys] + [E(k, False, 10) for k in reversed(keys)]


def mouse_with(tmp_path, bindings):
    svc, fake = make_service(tmp_path)
    svc.set_macros(bindings, {})
    return bytes(fake.mem), svc


def test_factory_mouse_is_clean(tmp_path):
    svc, fake = make_service(tmp_path)
    r = audit.audit_memory(bytes(fake.mem), "sim")
    assert r.worst == "info" and r.exit_code == 0
    assert "nothing suspicious" in audit.format_report(r)


def test_run_box_command_is_high(tmp_path):
    payload = combo(WIN, key_code("r")) + typed("cmd") + tap(0x28)
    mem, _ = mouse_with(tmp_path, {6: Macro(payload)})
    r = audit.audit_memory(mem, "sim")
    assert r.worst == "high" and r.exit_code == 2
    f = next(f for f in r.findings if f.button == 6)
    assert f.typed == "[Win+R]cmd⏎" and "Run box" in f.detail and "cmd" in f.detail


def test_typed_command_with_enter_is_high_without_launcher(tmp_path):
    mem, _ = mouse_with(tmp_path, {4: Macro(typed("curl x") + tap(0x28))})
    assert audit.audit_memory(mem).worst == "high"


def test_shortcuts_and_media(tmp_path):
    mem, _ = mouse_with(tmp_path, {5: Shortcut(["win"], key_code("r")), 7: Media("browser"), 4: Shortcut(["ctrl"], key_code("c"))})
    r = audit.audit_memory(mem)
    by = {f.button: f for f in r.findings if f.button}
    assert by[5].level == "medium" and by[5].typed == "[Win+R]"     # opens Run, types nothing itself
    assert by[4].level == "info"                                    # Ctrl+C is harmless
    assert by[7].level == "info" and "opens an app" in by[7].title
    assert r.bindings[7] == "media key: Browser"


def test_benign_text_macro_is_low_and_shift_is_rendered(tmp_path):
    seq = [E(SHIFT, True, 5)] + tap(key_code("h")) + [E(SHIFT, False, 5)] + tap(key_code("i")) + tap(0x28)
    mem, _ = mouse_with(tmp_path, {6: Macro(seq)})
    f = next(f for f in audit.audit_memory(mem).findings if f.button == 6)
    assert f.level == "low" and f.typed == "Hi⏎"


def test_game_macro_is_just_info(tmp_path):
    mem, _ = mouse_with(tmp_path, {7: Macro(parse_script("a tap 30, d tap 30, space tap 10"), "hold")})
    r = audit.audit_memory(mem)
    assert r.worst == "info"


def test_left_button_macro_and_foreign_data(tmp_path):
    mem, _ = mouse_with(tmp_path, {6: Macro(typed("hi"))})
    m = bytearray(mem)
    m[ins.BUTTON_BLOCK] = (m[ins.BUTTON_BLOCK] & 0xE0) | 0x0D      # slot 0 = left button -> macro
    m[0x300:0x308] = b"\x13\x37\x13\x37\x00\x00\x00\x00"
    r = audit.audit_memory(bytes(m))
    titles = [f.title for f in r.findings]
    assert "the LEFT button holds a macro/shortcut" in titles
    assert "data in a normally-empty area" in titles and r.worst == "high"


def test_leftover_data_is_reported_not_alarmed(tmp_path):
    svc, fake = make_service(tmp_path)
    svc.set_macros({6: Macro(combo(WIN, key_code("r")) + typed("cmd") + tap(0x28))}, {})
    svc.set_macros({}, {6: ins.BUTTON_FUNCTIONS["dpi-loop"]})      # unbound: bytes stay behind (like the app)
    r = audit.audit_memory(bytes(fake.mem))
    left = [f for f in r.findings if f.title.startswith("leftover key data")]
    assert left and left[0].level == "low" and "cmd" in left[0].detail and r.worst == "low"


def test_render_keeps_unknown_keys_readable():
    t = audit.render([E(0x46, True, 1), E(0x46, False, 1)] + combo(CTRL, ALT, key_code("t")))
    assert t.text == "⟨PrintScreen⟩[Alt+Ctrl+T]" and ("Alt", "Ctrl", "T") in t.combos


def test_cli_audit_on_dump(tmp_path, capsys):
    mem, _ = mouse_with(tmp_path, {6: Macro(combo(WIN, key_code("r")) + typed("cmd") + tap(0x28))})
    dump = tmp_path / "d.json"
    dump.write_text(json.dumps({"blocks": {f"0x{a:03x}": mem[a:a + 8].hex() for a in range(0, 0x1000, 8)}}))
    with pytest.raises(SystemExit) as e:
        cli.main(["audit", "--dump", str(dump)])
    assert e.value.code == 2
    out = capsys.readouterr().out
    assert "HIGH" in out and "[Win+R]cmd⏎" in out
    with pytest.raises(SystemExit):
        cli.main(["audit", "--dump", str(dump), "--json"])
    j = json.loads(capsys.readouterr().out)
    assert j["worst"] == "high" and j["findings"][0]["level"] == "high"
