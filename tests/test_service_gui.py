# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os

import pytest

from blazectl import service as svcmod
from blazectl.drivers import instant as ins
from sim import make_service
from test_instant import REAL_BUTTONS, FakeInstantMouse


def lighting_packets_sent(fake):
    return [p for p in fake.sent if p[1] in (0x13, 0x14)]


def test_read_state(tmp_path):
    fake = FakeInstantMouse()
    fake.mem[0x08:0x10] = REAL_BUTTONS
    svc, _ = make_service(tmp_path, fake)
    st = svc.read_state()
    assert st.usb_id == "30fa:1440"
    assert st.dpi == [ins.code_to_dpi(c, 0x1440) for c in st.dpi_codes]
    assert st.polling_hz == 1000  # state byte 6 = 0x00
    assert st.buttons[7] == 0x15


def test_mouse_missing(tmp_path):
    svc, _ = make_service(tmp_path, present=False)
    with pytest.raises(svcmod.ServiceError, match="not found"):
        svc.read_state()


def test_lighting_saved_and_reapplied_after_memory_writes(tmp_path):
    fake = FakeInstantMouse()
    fake.mem[0x08:0x10] = REAL_BUTTONS
    svc, _ = make_service(tmp_path, fake)
    s = svcmod.LightingSettings(mode="static", colors=["#00ff00"] * 6, brightness=5)
    svc.set_lighting(s)
    assert json.load(open(svc.settings_path))["colors"][0] == "#00ff00"
    assert svc.load_lighting() == s

    fake.sent.clear()
    assert svc.set_dpi([800, 1600, 3200, 6400, 9600, 12800]) == [800, 1600, 3200, 6400, 9600, 12800]
    relit = lighting_packets_sent(fake)
    assert relit == s.packets()  # lighting restored after the reload
    fake.sent.clear()
    fake.sent.clear()
    assert svc.set_polling(1000) is False  # already 1000 Hz: nothing written...
    assert lighting_packets_sent(fake) == []  # ...and no needless relight
    assert svc.set_polling(250) is True and fake.mem[0x1E] == 0x03
    assert lighting_packets_sent(fake) == s.packets()
    fake.sent.clear()
    svc.set_buttons({7: ins.BUTTON_FUNCTIONS["middle-click"]})
    assert fake.mem[0x0D] & 0x1F == 0x02 and lighting_packets_sent(fake) == s.packets()
    svc.reset_buttons()
    assert bytes(fake.mem[0x08:0x10]) == REAL_BUTTONS


def test_no_relight_when_nothing_changed(tmp_path):
    fake = FakeInstantMouse()
    svc, _ = make_service(tmp_path, fake)
    current = [ins.code_to_dpi(c, 0x1440) for c in ins.decode_dpi(bytes(fake.mem[0x10:0x18]))]
    fake.sent.clear()
    svc.set_dpi(current)
    assert lighting_packets_sent(fake) == []


def test_errors_are_service_errors(tmp_path):
    svc, fake = make_service(tmp_path)
    with pytest.raises(svcmod.ServiceError, match="not a known value"):
        svc.set_dpi([2200] * 6)
    with pytest.raises(svcmod.ServiceError, match="must be one of"):
        svc.set_polling(300)
    with pytest.raises(svcmod.ServiceError, match="only supports"):
        svc.set_buttons({8: ins.BUTTON_FUNCTIONS["back"]})
    with pytest.raises(ins.InstantError):
        svc.set_lighting(svcmod.LightingSettings(mode="disco"))
    assert fake.writes() == []


def test_backup(tmp_path):
    svc, fake = make_service(tmp_path)
    path = svc.backup(str(tmp_path / "b.json"))
    data = json.load(open(path))
    assert data["usb_id"] == "30fa:1440" and data["blocks"]["0x00"].startswith("5a03a5")


def test_button_choices_respect_firmware_limits():
    assert set(svcmod.button_choices(8)) == {"rapid-fire", "dpi-loop"}
    assert "back" in svcmod.button_choices(5)


# ---- GUI smoke test (needs tkinter + a display; skipped otherwise) ---------- #

tk = pytest.importorskip("tkinter")


@pytest.fixture
def app(tmp_path):
    if not os.environ.get("DISPLAY") and os.name != "nt":
        pytest.skip("no display")
    from blazectl.gui import BlazeApp

    fake = FakeInstantMouse()
    fake.mem[0x08:0x10] = REAL_BUTTONS
    svc, _ = make_service(tmp_path, fake)
    try:
        root = tk.Tk()
    except tk.TclError as e:   # broken Tcl/Tk install on the machine (seen on GitHub's Windows + Python 3.13)
        pytest.skip(f"Tk can't start here: {e}")
    a = BlazeApp(root, svc, check_official_app=False)
    yield a, fake
    root.destroy()


def test_gui_loads_state_and_applies(app):
    a, fake = app
    assert a.device_var.get().startswith("Connected: INSTANT USB Gaming Mouse")
    assert a.btn_vars[7].get() == "LED Loop" and a.btn_vars[8].get() == "Rapid Fire"
    assert all(v.get() for v in a.dpi_vars)

    for v, d in zip(a.dpi_vars, ["400", "800", "1600", "3200", "6400", "12800"]):
        v.set(d)
    a.apply_dpi()
    assert "DPI saved and verified" in a.status_var.get()
    assert [ins.code_to_dpi(c, 0x1440) for c in ins.decode_dpi(bytes(fake.mem[0x10:0x18]))] == [400, 800, 1600, 3200, 6400, 12800]

    a.btn_vars[7].set("Middle Click")
    a.apply_buttons()
    assert "Buttons saved" in a.status_var.get() and fake.mem[0x0D] & 0x1F == 0x02

    a.poll_var.set(250)
    a.apply_polling()
    assert fake.mem[0x1E] == 0x03

    a.mode_var.set("Rainbow wave")
    a._sync_light_controls()
    assert str(a.swatches[0].cget("state")) == "disabled"
    a.apply_lighting()
    assert "Lighting set: Rainbow wave" in a.status_var.get()

    # an error shows in the status bar instead of crashing
    a.dpi_vars[0].set("")
    a.apply_dpi()
    assert a.status_var.get().startswith("✗")


def test_gui_same_colour_for_all(app):
    a, _ = app
    a.same_var.set(True)
    a.set_swatch(2, "#123456")
    assert {b.cget("bg") for b in a.swatches} == {"#123456"}
    a.same_var.set(False)
    a.set_swatch(0, "#ff0000")
    assert a.swatches[0].cget("bg") == "#ff0000" and a.swatches[1].cget("bg") == "#123456"

