# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""The popup's show/fade logic, run against a stub tkinter (no display needed)."""
import sys
import types

import pytest

from blazectl import overlay as ov


def fake_tk():
    calls = []

    class TclError(Exception):
        pass

    class W:
        def __init__(self, *a, **k):
            self.opts = dict(k)
            self.alpha = None
            self.state = "normal"

        def __getattr__(self, name):                  # pack, overrideredirect, geometry, ...
            return lambda *a, **k: calls.append((name, a))

        def configure(self, **k):
            self.opts.update(k)

        def attributes(self, key, value=None):
            if key == "-alpha":
                self.alpha = value
            calls.append(("attributes", (key, value)))

        def withdraw(self):
            self.state = "withdrawn"
            calls.append(("withdraw", ()))

        def deiconify(self):
            self.state = "normal"
            calls.append(("deiconify", ()))

        def winfo_screenwidth(self):
            return 1920

        def winfo_screenheight(self):
            return 1080

        def wm_frame(self):
            return "0x1234"

    class Tk(W):
        def __init__(self):
            super().__init__()
            self.jobs = []

        def after(self, ms, fn, *a):
            self.jobs.append((fn, a))
            return len(self.jobs)

        def after_cancel(self, job):
            pass

    mod = types.SimpleNamespace(Tk=Tk, Toplevel=W, Frame=W, Label=W, TclError=TclError)
    return mod, calls


def run_jobs(o, limit=50):
    for _ in range(limit):
        if not o.root.jobs:
            return
        fn, a = o.root.jobs.pop(0)
        fn(*a)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_popup_shows_then_fades(monkeypatch, platform):
    mod, calls = fake_tk()
    monkeypatch.setitem(sys.modules, "tkinter", mod)
    monkeypatch.setattr(ov.sys, "platform", platform)
    o = ov.TkOverlay("bottom")
    if platform == "win32":
        assert o.win.alpha == 0.0 and o.win.state == "normal"     # mapped once, invisible
    else:
        assert o.win.state == "withdrawn"
    o._display(ov.dpi_popup(3, 1600, "#00ff00"))
    assert o.win.alpha == 0.94 and o.val.opts["text"] == "1600" and o.bar.opts["bg"] == "#00ff00"
    assert o.cap.opts["text"] == "DPI · LEVEL 3"
    run_jobs(o)
    if platform == "win32":
        assert o.win.alpha == 0.0 and ("deiconify", ()) not in calls   # never re-shown: can't steal focus
    else:
        assert o.win.state == "withdrawn"
    o._display(ov.alert_popup("critical", "x" * 80))
    assert o.val.opts["text"].endswith("…") and len(o.val.opts["text"]) == 46


def test_make_overlay_falls_back_without_tkinter(monkeypatch):
    monkeypatch.setitem(sys.modules, "tkinter", None)                   # import fails
    monkeypatch.setattr(ov.sys, "platform", "win32")
    assert isinstance(ov.make_overlay(), ov.NullOverlay)
    monkeypatch.setattr(ov.sys, "platform", "linux")
    assert isinstance(ov.make_overlay(), ov.NotifyOverlay)
    assert type(ov.make_overlay(enabled=False)) is ov.NullOverlay
