# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""BLAZE Web (blazectl/web/webhid.js) must behave exactly like the Python driver.

The same sequence of GUI calls runs through webui.Api (Python) and WebApi (JavaScript, in Node)
against the same simulated mouse. Every USB packet, every response and the final memory must match.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from blazectl import webui
from blazectl.service import ServiceError
from sim import make_service

NODE = shutil.which("node")
HERE = Path(__file__).parent
pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node.js")

NO_STATE = {"/api/state", "/api/profiles", "/api/profiles/save", "/api/macros/library",
            "/api/macros/library/save", "/api/macros/library/delete"}

MAP = {"1": "left-click", "2": "right-click", "3": "middle-click", "4": "forward", "5": "back",
       "6": "dpi-loop", "7": "led-loop", "8": "rapid-fire"}
STEPS = [
    ["/api/state", {}],
    ["/api/dpi", {"dpi": [400, 800, 1600, 2400, 3200, 6400]}],
    ["/api/dpi", {"dpi": [100, 800, 1600, 2400, 3200, 6400]}],                       # error: unknown DPI
    ["/api/polling", {"rate": 500}],
    ["/api/polling", {"rate": 500}],                                                  # already set
    ["/api/active", {"level": 3}],
    ["/api/lighting", {"mode": "breathing", "colors": ["#00ff00", "#112233", "#ffffff", "#000000", "#ff0000", "#0000ff"],
                       "speed": 3, "brightness": 7, "direction": 0}],
    ["/api/buttons", {"buttons": {**MAP, "7": "dpi-up", "8": "dpi-loop"}}],
    ["/api/buttons", {"buttons": {**MAP, "8": "back"}}],                              # error: firmware limit
    ["/api/macros", {"bind": {"6": {"mode": "times", "repeat": 2, "events": [["A", "down", 30], ["A", "up", 300]]},
                              "4": {"type": "media", "key": "volume-up"},
                              "5": {"type": "shortcut", "mods": ["ctrl", "shift"], "key": "Esc"},
                              "7": {"mode": "hold", "repeat": 1, "events": [["A", "down", 30], ["A", "up", 300]]}},
                     "unbind": {}}],
    ["/api/state", {}],
    ["/api/macros", {"bind": {"3": {"mode": "times", "repeat": 1, "events": [["LCtrl", "down", 5], ["C", "down", 5],
                                                                             ["C", "up", 5], ["LCtrl", "up", 5]]}},
                     "unbind": {"6": "dpi-loop"}}],
    ["/api/buttons", {"buttons": {**MAP, "4": "forward"}}],                           # moving off a media key
    ["/api/macros", {"bind": {"1": {"type": "media", "key": "mute"}}, "unbind": {}}],  # error: button 1
    ["/api/macros/library/save", {"name": "spam", "macro": {"mode": "times", "repeat": 1, "events": [["B", "down", 1]]}}],
    ["/api/macros/library", {}],
    ["/api/profiles/save", {"slot": "game1", "active": "game1"}],
    ["/api/buttons/reset", {}],
    ["/api/backup", {}],
]


def run_python(tmp_path):
    svc, fake = make_service(tmp_path)
    start = bytes(fake.mem)
    api = webui.Api(svc)
    results = []
    for path, body in STEPS:
        try:
            r = api.handle(path, json.loads(json.dumps(body)))
            if path not in NO_STATE:
                r["state"] = api.state()
            results.append({"ok": True, "result": json.loads(json.dumps(r))})
        except ServiceError as e:
            results.append({"ok": False, "error": str(e)})
    return start, results, [p.hex() for p in fake.sent], bytes(fake.mem).hex(), fake.reloads


def run_js(mem: bytes):
    out = subprocess.run([NODE, str(HERE / "web" / "run_scenario.js")], input=json.dumps({"mem": mem.hex(), "steps": STEPS}),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_js_driver_matches_python_byte_for_byte(tmp_path):
    start, py_results, py_sent, py_mem, py_reloads = run_python(tmp_path)
    js = run_js(start)
    for i, ((path, _), a, b) in enumerate(zip(STEPS, py_results, js["results"])):
        assert a["ok"] == b["ok"], f"step {i} {path}: python {a} vs js {b}"
        if a["ok"]:
            assert a["result"] == b["result"], f"step {i} {path}"
        else:
            assert a["error"] == b["error"], f"step {i} {path}"
    assert len(py_sent) == len(js["sent"])
    for i, (a, b) in enumerate(zip(py_sent, js["sent"])):
        assert a == b, f"packet {i}: python {a} vs js {b}"
    assert py_mem == js["mem"] and py_reloads == js["reloads"]
    assert [r["ok"] for r in py_results].count(False) == 3          # the 3 refused steps really were refused


def test_built_site_is_self_contained(tmp_path):
    from blazectl.webbuild import build
    out = build(tmp_path / "site")
    html = out.read_text(encoding="utf-8")
    assert "navigator.hid" in html and "BlazeWeb" in html and '"web": true' in html
    assert "<script src=" not in html and "http://" not in html.replace("http://www.w3.org", "")
    assert "Content-Security-Policy" in html and "connect-src 'none'" in html
