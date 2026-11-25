# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the issues found in the 25 Nov 2026 code review."""

import json
import os
import shutil
import socket
import subprocess
import threading

import pytest

from blazectl import agent as ag
from blazectl import cli, winhid
from blazectl.foreground import AppInfo
from blazectl.drivers import instant as ins
from blazectl.service import LightingSettings, MouseService, ProfileStore, ServiceError
from blazectl.webui import make_server
from sim import make_service
from test_agent import Clock, config_from_mouse, make_switcher, run_for
from test_windows import FakeHid

EVIL = 'red"><img src=x onerror=alert(1)>'


# ---------------------------------------------------------------- profiles: check before writing

@pytest.mark.parametrize("break_it", [
    lambda c: c.update(dpi=[100] * 6),                                       # DPI the mouse doesn't have
    lambda c: c.update(polling=333),                                         # polling rate it doesn't have
    lambda c: c["macros"].update({"1": {"type": "media", "key": "mute"}}),   # button 1 can't hold a macro
    lambda c: c["buttons"].update({"8": "back"}),                            # the fire button can't go Back
])
def test_bad_profile_writes_nothing(tmp_path, break_it):
    """A profile that can't be applied fully must not leave the mouse half-switched."""
    svc, fake = make_service(tmp_path)
    cfg = config_from_mouse(svc)
    cfg["buttons"]["7"] = "dpi-up"                       # a valid change that would be written first
    cfg["macros"]["4"] = {"type": "media", "key": "volume-up"}
    cfg["buttons"]["4"] = "media"
    break_it(cfg)
    with pytest.raises(ServiceError):
        svc.apply_config(cfg)
    assert fake.writes() == [] and fake.reloads == 0


# ---------------------------------------------------------------- colours can't carry HTML

def test_profiles_reject_colours_that_are_not_hex(tmp_path):
    svc, _ = make_service(tmp_path)
    cfg = config_from_mouse(svc)
    for mode in ("static", "rainbow", "off"):            # rainbow/off don't use colours: still checked
        cfg["lighting"] = {"mode": mode, "colors": [EVIL] * 6, "speed": 2, "brightness": 7, "direction": 0}
        with pytest.raises(ServiceError, match="colour"):
            ProfileStore(str(tmp_path / "p.json")).save("office", cfg)


def test_lighting_rejects_bad_colours_for_every_effect(tmp_path):
    svc, _ = make_service(tmp_path)
    with pytest.raises(ins.InstantError):
        svc.set_lighting(LightingSettings("rainbow", [EVIL]))
    with pytest.raises(ins.InstantError):
        svc.set_lighting(LightingSettings("static", ["ff0000"]))   # no '#'
    svc.set_lighting(LightingSettings("rainbow", ["#00ff00"]))      # a good one still works


def test_damaged_lighting_file_falls_back_to_default(tmp_path):
    svc, _ = make_service(tmp_path)
    os.makedirs(os.path.dirname(svc.settings_path), exist_ok=True)
    for bad in ({"mode": "rainbow", "colors": [EVIL]}, {"mode": "static", "colors": "#ff0000"},
                {"mode": "nope", "colors": ["#ff0000"]}, {"mode": "static", "colors": ["#ff0000"], "speed": 99}):
        with open(svc.settings_path, "w") as f:
            json.dump(bad, f)
        assert svc.load_lighting() == LightingSettings()


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_browser_driver_checks_colours_too():
    js = os.path.join(os.path.dirname(os.path.dirname(__file__)), "blazectl", "web", "webhid.js")
    code = f"""
const B = require({json.dumps(js)});
const ok = s => {{ try {{ B.checkLighting(s); return true; }} catch (e) {{ return false; }} }};
const api = new B.WebApi();
let saved = true; try {{ api.validateProfile({{dpi: [400,800,1600,2400,3200,6400], polling: 1000, buttons: {{}},
  lighting: {{mode: "rainbow", colors: [{json.dumps(EVIL)}], speed: 2, brightness: 7, direction: 0}}}}); }} catch (e) {{ saved = false; }}
process.stdout.write(JSON.stringify([ok({{mode: "rainbow", colors: [{json.dumps(EVIL)}], speed: 2, brightness: 7, direction: 0}}),
  ok({{mode: "static", colors: ["#00ff00"], speed: 2, brightness: 7, direction: 0}}), saved]));
"""
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [False, True, False]


def test_page_cleans_colours_before_they_reach_html():
    page = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "blazectl", "web", "index.html"),
                encoding="utf-8").read()
    assert "draft.lighting = cleanLighting(draft.lighting);" in page      # every draft goes through it
    assert "HEX.test(c)" in page and "bad lighting (effect, #rrggbb colours" in page   # import checks it


# ---------------------------------------------------------------- Windows: don't open every HID device

class TwoVendorHid(FakeHid):
    """The mouse plus a keyboard from another vendor; records which devices get opened."""

    def __init__(self):
        super().__init__()
        self.registry[b"kbd"] = {"desc": b"\x05\x01\x09\x06", "features": {}}
        self.opened = []

    def enumerate(self):
        return super().enumerate() + [{"path": b"kbd", "vendor_id": 0x046D, "product_id": 0xC31C, "usage_page": 1,
                                        "usage": 6, "interface_number": 0}]

    def device(self):
        d = super().device()
        real = d.open_path

        def open_path(path):
            self.opened.append(path)
            return real(path)
        d.open_path = open_path
        return d


def test_mouse_scan_opens_only_the_mouse():
    h = TwoVendorHid()
    assert len(winhid.enumerate_nodes(h)) == 4 and b"kbd" in h.opened          # `blazectl list`: everything
    h = TwoVendorHid()
    nodes = winhid.enumerate_nodes(h, vid=ins.VID)
    assert {n.vid for n in nodes} == {ins.VID} and b"kbd" not in h.opened      # mouse operations


def test_service_uses_the_vendor_scan():
    from blazectl import backend
    assert MouseService()._nodes_fn is backend.mouse_nodes


# ---------------------------------------------------------------- agent

def test_switcher_reapplies_after_you_change_profile_in_the_editor(tmp_path):
    sw, app, clock, *_ = make_switcher(tmp_path, [{"match": "valorant.exe", "profile": "game1"}])
    app["v"] = AppInfo("valorant.exe")
    run_for(sw, clock, 3)
    assert sw.svc.applied == [400]
    gui = {"open": True}
    sw.gui_open_fn = lambda: gui["open"]
    run_for(sw, clock, 3)                  # you open BLAZE Control and pick Office by hand
    gui["open"] = False
    run_for(sw, clock, 3)                  # close it: the game's profile comes back
    assert sw.svc.applied == [400, 400]


class FlakySvc:
    def __init__(self):
        self.restores = 0
        self.fail_next = True

    def preview_lighting(self, s):
        pass

    def restore_lighting(self):
        self.restores += 1
        if self.fail_next:
            self.fail_next = False
            raise ServiceError("mouse busy")


def test_alert_keeps_trying_to_put_lighting_back():
    cfg = ag.validate_config({"alerts": {"enabled": True, "seconds": 2}})
    clock, svc = Clock(), FlakySvc()
    b = ag.AlertBeacon(svc, lambda: cfg, lambda p: None, clock)
    assert b.submit("high", "x")
    b.tick()
    clock.t += 3
    b.tick()                                # restore fails: the alert must not be forgotten
    assert b.active is not None and svc.restores == 1 and "busy" in b.error
    clock.t += 3
    b.tick()                                # retried and succeeded
    assert b.active is None and svc.restores == 2 and b.error is None


def test_agent_log_rotates(tmp_path, monkeypatch):
    import logging
    from logging.handlers import RotatingFileHandler
    root = logging.getLogger()
    before = list(root.handlers)
    root.handlers = []
    try:
        ag.setup_logging()
        assert any(isinstance(h, RotatingFileHandler) and h.maxBytes > 0 for h in root.handlers)
    finally:
        for h in root.handlers:
            h.close()
        root.handlers = before


# ---------------------------------------------------------------- local servers: bad Content-Length

def _raw_post(port, headers):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(("POST /api/state HTTP/1.1\r\nHost: 127.0.0.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
                   + "\r\n").encode())
        return s.recv(200).split(b"\r\n", 1)[0]
    finally:
        s.close()


@pytest.mark.parametrize("length", ["-1", "abc"])
def test_gui_server_rejects_bad_content_length(tmp_path, length):
    svc, _ = make_service(tmp_path)
    server, token = make_server(svc)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status = _raw_post(server.server_address[1], {"X-Blazectl-Token": token, "Content-Length": length})
        assert b" 400 " in status                # answered at once, not left waiting for a body
    finally:
        server.shutdown()


@pytest.mark.parametrize("length", ["-1", "abc"])
def test_alert_endpoint_rejects_bad_content_length(tmp_path, length):
    svc, _ = make_service(tmp_path)
    cfg = ag.validate_config({"alerts": {"enabled": True}})
    b = ag.AlertBeacon(svc, lambda: cfg, lambda p: None, Clock())
    token = ag.load_or_create_token(str(tmp_path))
    srv = ag.make_alert_server(b, token)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert b" 400 " in _raw_post(srv.server_address[1], {"X-Blazectl-Token": token, "Content-Length": length})
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- CLI

def test_cli_reports_errors_without_a_traceback(monkeypatch):
    from blazectl.devlock import LockTimeout

    def busy(args):
        raise LockTimeout("The mouse is busy in another blazectl program")
    monkeypatch.setattr(cli, "cmd_poll", busy)
    with pytest.raises(SystemExit) as e:
        cli.main(["poll"])
    assert "busy" in str(e.value.code)

    def bad(args):
        raise ins.InstantError("mouse did not store block 0x18")
    monkeypatch.setattr(cli, "cmd_poll", bad)
    with pytest.raises(SystemExit) as e:
        cli.main(["poll"])
    assert "did not store" in str(e.value.code)


@pytest.fixture
def cli_mouse(monkeypatch):
    from blazectl.hidraw import HidrawNode
    from test_instant import CONTROL_DESC, FakeInstantMouse, no_sleep

    fake = FakeInstantMouse()
    fake.mem.extend(bytes(0x1000 - len(fake.mem)))
    node = HidrawNode("hid6", "hid6", 0, 0x30FA, 0x1440, "INSTANT USB Gaming Mouse", 1, CONTROL_DESC)

    class Ctx:
        def __enter__(self):
            return fake

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(cli, "enumerate_nodes", lambda: [node])
    monkeypatch.setattr(cli, "make_device", lambda n: Ctx())
    monkeypatch.setattr(ins.time, "sleep", no_sleep)
    return fake


def test_cli_light_is_remembered_and_put_back_after_writes(cli_mouse, monkeypatch, capsys):
    cli.main(["light", "breathing", "--color", "00ff00", "--speed", "3"])
    saved = MouseService().load_lighting()
    assert (saved.mode, saved.colors, saved.speed) == ("breathing", ["#00ff00"], 3)

    calls = []
    monkeypatch.setattr(MouseService, "restore_lighting", lambda self: calls.append(1))
    cli.main(["poll", "--rate", "500", "--execute"])
    out = capsys.readouterr().out
    assert calls == [1] and "lighting put back" in out
