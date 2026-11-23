# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Agent: per-app profiles, DPI popup, alert light, the lock, and the CLI around them."""
import json
import os
import threading
import urllib.request

import pytest

from blazectl import agent as ag
from blazectl import devlock
from blazectl.drivers import instant as ins
from blazectl.foreground import AppInfo, parse_xprop_active, parse_xprop_window, rule_matches
from blazectl.overlay import NullOverlay
from blazectl.service import ProfileStore, ServiceError
from sim import make_service


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def lighting_packets(fake):
    return [p for p in fake.sent if p[1] in (0x13, 0x14)]


def config_from_mouse(svc):
    st = svc.read_state()
    from dataclasses import asdict
    names = ins.FUNCTION_NAMES
    return {"dpi": st.dpi, "polling": st.polling_hz,
            "buttons": {str(n): names[c] for n, c in st.buttons.items()},
            "lighting": asdict(svc.load_lighting()), "macros": {}}


# ---------------------------------------------------------------- config / rules

def test_defaults_and_validation():
    cfg = ag.validate_config({})
    assert cfg["auto_profiles"]["enabled"] is False and cfg["dpi_popup"]["enabled"] is False   # opt-in: it polls
    good = ag.validate_config({"auto_profiles": {"enabled": True, "rules": [{"match": "valorant.exe", "profile": "game1"}],
                                                 "default": "office"}})
    assert good["auto_profiles"]["rules"] == [{"match": "valorant.exe", "profile": "game1"}]
    for bad in ({"auto_profiles": {"rules": [{"match": "x", "profile": "nope"}]}},
                {"auto_profiles": {"default": "work"}},
                {"auto_profiles": {"delay": 0}},
                {"dpi_popup": {"position": "left"}},
                {"alerts": {"min_severity": "huge"}},
                {"auto_profiles": {"rules": [{"profile": "game1"}]}}):
        with pytest.raises(ag.ConfigError):
            ag.validate_config(bad)


def test_config_store_roundtrip(tmp_path):
    st = ag.AgentConfigStore(str(tmp_path))
    assert st.load() == ag.validate_config({})
    st.save({"auto_profiles": {"enabled": True, "rules": [{"match": "code", "profile": "office"}]}})
    assert st.load()["auto_profiles"]["rules"][0]["match"] == "code"
    (tmp_path / "agent.json").write_text("{broken")
    assert st.load() == ag.validate_config({})          # a damaged file never stops the agent


@pytest.mark.parametrize("pattern,app,hit", [
    ("valorant.exe", AppInfo("valorant.exe"), True),
    ("VALORANT.EXE", AppInfo("valorant.exe"), True),
    ("valorant", AppInfo("valorant.exe"), True),          # bare name matches the .exe
    ("code", AppInfo("code"), True),                       # Linux process name
    ("cs2*", AppInfo("cs2.exe"), True),
    ("cs2", AppInfo("cs2go.exe"), False),
    ("title:*youtube*", AppInfo("chrome.exe", "Lofi beats - YouTube - Google Chrome"), True),
    ("title:*youtube*", AppInfo("chrome.exe", "GitHub"), False),
    ("", AppInfo("x.exe"), False),
])
def test_rule_matches(pattern, app, hit):
    assert rule_matches(pattern, app) is hit


def test_xprop_parsing():
    assert parse_xprop_active("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007") == "0x3a00007"
    assert parse_xprop_active("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0") is None
    out = ('_NET_WM_PID(CARDINAL) = 4242\nWM_CLASS(STRING) = "code", "Code"\n'
           '_NET_WM_NAME(UTF8_STRING) = "agent.py - blazectl"')
    assert parse_xprop_window(out) == (4242, "code", "agent.py - blazectl")


# ---------------------------------------------------------------- apply_config

def test_apply_config_writes_only_differences(tmp_path):
    svc, fake = make_service(tmp_path)
    base = config_from_mouse(svc)
    assert svc.apply_config(base) == []                  # identical: nothing stored
    assert fake.writes() == [] and fake.reloads == 0
    assert lighting_packets(fake)                        # lighting (RAM) is always sent

    game = json.loads(json.dumps(base))
    game["dpi"] = [400, 800, 1600, 2400, 3200, 6400]
    game["buttons"]["7"] = "dpi-up"
    game["polling"] = 500
    game["macros"]["4"] = {"type": "media", "key": "volume-up"}
    game["buttons"]["4"] = "media"
    game["lighting"]["colors"] = ["#00ff00"] * 6
    changed = svc.apply_config(game)
    assert set(changed) == {"macros", "buttons", "dpi", "polling"}
    assert fake.reloads == 1                             # one reload for the whole profile
    st = svc.read_state()
    assert st.dpi == game["dpi"] and st.polling_hz == 500
    assert ins.FUNCTION_NAMES[st.buttons[7]] == "dpi-up"
    assert st.macros[4].to_json() == {"type": "media", "key": "volume-up"}
    assert svc.load_lighting().colors[0] == "#00ff00"   # becomes the user's lighting

    n = len(fake.writes())
    assert svc.apply_config(game) == [] and len(fake.writes()) == n   # switching back and forth is cheap
    assert set(svc.apply_config(base)) == {"macros", "buttons", "dpi", "polling"}
    assert 4 not in svc.read_state().macros


def test_apply_config_rejects_bad_profiles(tmp_path):
    svc, _ = make_service(tmp_path)
    cfg = config_from_mouse(svc)
    cfg["dpi"] = [100] * 6
    with pytest.raises(ServiceError):
        svc.apply_config(cfg)
    cfg = config_from_mouse(svc)
    cfg["buttons"]["4"] = "macro"                        # macro function without macro data
    with pytest.raises(ServiceError):
        svc.apply_config(cfg)


# ---------------------------------------------------------------- profile switcher

class FakeSvc:
    def __init__(self, fail=False):
        self.applied = []
        self.fail = fail

    def apply_config(self, cfg):
        if self.fail:
            raise ServiceError("mouse unplugged")
        self.applied.append(cfg["dpi"][0])
        return ["dpi"]


def make_switcher(tmp_path, rules, default=None, gui_open=False, svc=None):
    profiles = ProfileStore(str(tmp_path / "profiles.json"))
    real, _ = make_service(tmp_path)
    base = config_from_mouse(real)
    for slot, dpi0 in (("office", 800), ("game1", 400)):
        cfg = json.loads(json.dumps(base))
        cfg["dpi"][0] = dpi0
        profiles.save(slot, cfg)
    cfg = {"auto_profiles": {"enabled": True, "rules": rules, "default": default, "delay": 1.0}}
    cfg = ag.validate_config(cfg)
    app = {"v": AppInfo("explorer.exe")}
    clock, popups = Clock(), []
    sw = ag.ProfileSwitcher(svc or FakeSvc(), profiles, lambda: cfg, popups.append, lambda: app["v"],
                            lambda: gui_open, clock)
    return sw, app, clock, popups, profiles


def run_for(sw, clock, seconds, step=0.5):
    for _ in range(int(seconds / step)):
        sw.tick()
        clock.t += step


def test_switcher_debounces_and_applies_once(tmp_path):
    sw, app, clock, popups, profiles = make_switcher(tmp_path, [{"match": "valorant.exe", "profile": "game1"}], "office")
    app["v"] = AppInfo("valorant.exe")
    sw.tick()
    assert sw.svc.applied == []                          # needs to stay in front for `delay`
    run_for(sw, clock, 3)
    assert sw.svc.applied == [400]                       # game1, exactly once
    assert profiles.load()["active"] == "game1" and popups[-1].value == "Game I"
    app["v"] = AppInfo("chrome.exe")
    clock.t += 0.4
    sw.tick()
    app["v"] = AppInfo("valorant.exe")                   # quick alt-tab back: no flapping
    run_for(sw, clock, 3)
    assert sw.svc.applied == [400]
    app["v"] = AppInfo("chrome.exe")
    run_for(sw, clock, 3)
    assert sw.svc.applied == [400, 800]                  # default profile
    assert sw.recent[0] == "chrome.exe" and "valorant.exe" in sw.recent


def test_switcher_keeps_profile_without_default_and_ignores_own_window(tmp_path):
    sw, app, clock, *_ = make_switcher(tmp_path, [{"match": "valorant", "profile": "game1"}])
    app["v"] = AppInfo("valorant.exe")
    run_for(sw, clock, 3)
    for other in (AppInfo("notepad.exe"), AppInfo("blaze control.exe")):
        app["v"] = other
        run_for(sw, clock, 3)
    assert sw.svc.applied == [400]


def test_switcher_pauses_while_editor_open(tmp_path):
    sw, app, clock, *_ = make_switcher(tmp_path, [{"match": "valorant.exe", "profile": "game1"}], gui_open=True)
    app["v"] = AppInfo("valorant.exe")
    run_for(sw, clock, 5)
    assert sw.svc.applied == [] and "BLAZE Control is open" in sw.paused


def test_switcher_reports_empty_profile_and_errors(tmp_path):
    sw, app, clock, *_ = make_switcher(tmp_path, [{"match": "spotify.exe", "profile": "media"}])
    app["v"] = AppInfo("spotify.exe")
    run_for(sw, clock, 3)
    assert sw.svc.applied == [] and "Media is empty" in sw.error
    sw2, app2, clock2, *_ = make_switcher(tmp_path / "b", [{"match": "x.exe", "profile": "game1"}], svc=FakeSvc(fail=True))
    app2["v"] = AppInfo("x.exe")
    run_for(sw2, clock2, 3)
    assert "unplugged" in sw2.error and sw2.applied is None


def test_switcher_survives_unsupported_desktop(tmp_path):
    from blazectl.foreground import UnsupportedDesktop

    sw, _, clock, *_ = make_switcher(tmp_path, [{"match": "x", "profile": "game1"}])

    def boom():
        raise UnsupportedDesktop("Wayland session")
    sw.fg_fn = boom
    sw.tick()
    assert "Wayland" in sw.error


# ---------------------------------------------------------------- DPI popup

def make_watcher(tmp_path, app_running=False):
    svc, fake = make_service(tmp_path)
    cfg = ag.validate_config({"dpi_popup": {"enabled": True}})
    clock, popups = Clock(), []
    w = ag.DpiWatcher(svc, lambda: cfg, popups.append, clock, app_running_fn=lambda: app_running)
    return w, svc, fake, clock, popups


def set_level(fake, level):
    fake.mem[ins.STATE_BLOCK] = (fake.mem[ins.STATE_BLOCK] & 0xF8) | level


def test_dpi_watcher_pops_on_confirmed_level_change(tmp_path):
    w, svc, fake, clock, popups = make_watcher(tmp_path)
    w.tick()
    assert popups == []                                  # first read is the baseline
    dpi = svc.read_state().dpi
    set_level(fake, 4)                                   # user pressed DPI: level 5
    clock.t += 0.1
    w.tick()
    assert popups == []                                  # not due yet (poll_ms)
    clock.t += 0.35
    w.tick()
    assert popups == [] and w.pending == 4               # seen once: confirm first
    clock.t += 0.1
    w.tick()
    assert len(popups) == 1 and popups[0].value == str(dpi[4]) and popups[0].caption == "DPI · LEVEL 5"
    assert fake.writes() == []                           # the watcher never writes
    clock.t += 0.5
    w.tick()
    assert len(popups) == 1


def test_dpi_watcher_ignores_garbled_reads(tmp_path):
    w, svc, fake, clock, popups = make_watcher(tmp_path)
    base = fake.mem[ins.STATE_BLOCK] & 7
    w.tick()
    set_level(fake, 7)                                   # a level that doesn't exist (this crashed the agent)
    for _ in range(3):
        clock.t += 0.5
        w.tick()
    assert popups == [] and w.glitches == 3
    set_level(fake, (base + 1) % 6)                      # one wrong read, then back: no popup
    clock.t += 0.5
    w.tick()
    set_level(fake, base)
    clock.t += 0.1
    w.tick()
    assert popups == [] and w.pending is None
    mask_byte = ins.STATE_BLOCK + 1
    fake.mem[mask_byte] = fake.mem[mask_byte] & ~(1 << 5) & 0xFF  # level 6 disabled
    set_level(fake, 5)
    for _ in range(3):
        clock.t += 0.5
        w.tick()
    assert popups == []


def test_dpi_watcher_pauses_while_official_app_runs(tmp_path):
    w, svc, fake, clock, popups = make_watcher(tmp_path, app_running=True)
    n = len(fake.sent)
    for _ in range(5):
        clock.t += 0.5
        w.tick()
    assert "EvoFox app" in w.paused and len(fake.sent) == n   # didn't touch the mouse at all


def test_dpi_watcher_handles_unplugged_mouse(tmp_path):
    svc, _ = make_service(tmp_path, present=False)
    clock = Clock()
    w = ag.DpiWatcher(svc, lambda: ag.validate_config({"dpi_popup": {"enabled": True}}), lambda p: None, clock)
    w.tick()
    assert "not found" in w.error and w.next_t == clock.t + 3


# ---------------------------------------------------------------- alerts

@pytest.mark.parametrize("raw,sev", [("critical", "critical"), ("Informational", "info"), ("warning", "medium"),
                                     (9.8, "critical"), (7, "high"), ("5", "medium"), (0, "info"), ("??", "medium")])
def test_normalize_severity(raw, sev):
    assert ag.normalize_severity(raw) == sev


def test_parse_alert_shapes():
    assert ag.parse_alert({"severity": "high", "title": "Brute force"}) == ("high", "Brute force")
    sigma = {"rule": {"title": "Mimikatz use", "level": "critical"}, "host": "pc1"}
    assert ag.parse_alert(sigma) == ("critical", "Mimikatz use")
    assert ag.parse_alert({"level": "low"}) == ("low", "Security alert")
    assert ag.parse_alert({"host": "x"}) is None and ag.parse_alert([1]) is None


def test_file_tail_follows_appends_and_rotation(tmp_path):
    p = tmp_path / "alerts.jsonl"
    p.write_text('{"title": "old"}\n')
    t = ag.FileTail(str(p))
    assert t.poll() == []                                 # starts at the end
    with open(p, "a") as f:
        f.write('{"title": "a"}\n{"title": "b"')
    assert t.poll() == ['{"title": "a"}']
    with open(p, "a") as f:
        f.write('}\n')
    assert t.poll() == ['{"title": "b"}']
    p.write_text('{"title": "c"}\n')                      # rotated (smaller)
    assert t.poll() == ['{"title": "c"}']
    missing = ag.FileTail(str(tmp_path / "later.jsonl"))
    assert missing.poll() == []
    (tmp_path / "later.jsonl").write_text('{"title": "first"}\n')
    assert missing.poll() == ['{"title": "first"}']      # a file created later is read from the start


def test_beacon_blinks_then_restores(tmp_path):
    svc, fake = make_service(tmp_path)
    cfg = ag.validate_config({"alerts": {"enabled": True, "min_severity": "medium", "seconds": 2}})
    clock, popups = Clock(), []
    b = ag.AlertBeacon(svc, lambda: cfg, popups.append, clock)
    assert b.submit("low", "ignored") is False
    assert b.submit("critical", "Ransomware behaviour") is True
    b.tick()
    assert popups[-1].caption == "⚠ ALERT · CRITICAL" and b.active.severity == "critical"
    colors = []
    for _ in range(8):
        clock.t += 0.25
        b.tick()
        last = [p for p in fake.sent if p[1] == 0x14][-1]
        colors.append(last[3])
    assert len(set(colors)) == 2                          # toggles on/off
    clock.t += 3
    b.tick()
    assert b.active is None
    assert fake.writes() == []                            # alerts never touch flash


def test_beacon_reads_alert_files_and_ack(tmp_path):
    svc, _ = make_service(tmp_path)
    f = tmp_path / "triagelens-alerts.jsonl"
    f.write_text("")
    cfg = ag.validate_config({"alerts": {"enabled": True, "files": [str(f)], "seconds": 60}})
    clock = Clock()
    b = ag.AlertBeacon(svc, lambda: cfg, lambda p: None, clock)
    b.tick()
    with open(f, "a") as fh:
        fh.write(json.dumps({"rule": {"title": "Failed logon burst", "level": "high"}}) + "\nnot json\n")
    b.tick()
    assert b.active and b.active.title == "Failed logon burst"
    b.ack()
    b.tick()
    clock.t += 0.01
    b.tick()
    assert b.active is None


def test_alert_endpoint_needs_token_and_local_host(tmp_path):
    svc, _ = make_service(tmp_path)
    cfg = ag.validate_config({"alerts": {"enabled": True}})
    b = ag.AlertBeacon(svc, lambda: cfg, lambda p: None, Clock())
    token = ag.load_or_create_token(str(tmp_path))
    assert ag.load_or_create_token(str(tmp_path)) == token
    srv = ag.make_alert_server(b, token)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        def post(path, body, tok=token, host=None):
            h = {"Content-Type": "application/json", "X-Blazectl-Token": tok}
            if host:
                h["Host"] = host
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", json.dumps(body).encode(), h)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        assert post("/alert", {"severity": "high", "title": "x"}, tok="wrong")[0] == 403
        assert post("/alert", {"severity": "high", "title": "x"}, host="evil.example")[0] == 403
        assert post("/alert", {"severity": "high", "title": "x"}) == (202, {"accepted": True})
        assert post("/alert", {"severity": "info", "title": "x"}) == (202, {"accepted": False})
        assert post("/alert", {"nothing": 1})[0] == 400
        (tmp_path / "agent-endpoint.json").write_text(json.dumps({"port": port}))
        assert ag.send_alert("critical", "from the CLI", directory=str(tmp_path)) is True
        assert ag.send_alert("low", "too quiet", directory=str(tmp_path)) is False
        assert ag.send_alert("", "", directory=str(tmp_path), ack=True) is True
        cfg["alerts"]["enabled"] = False
        assert ag.send_alert("critical", "light is off", directory=str(tmp_path)) is False
    finally:
        srv.shutdown()
    assert ag.send_alert("high", "x", directory=str(tmp_path / "none")) is None


# ---------------------------------------------------------------- the agent loop

def test_agent_step_writes_status_and_stops_on_request(tmp_path):
    svc, _ = make_service(tmp_path)
    agent = ag.Agent(svc=svc, directory=str(tmp_path), overlay=NullOverlay(),
                     fg_fn=lambda: AppInfo("code.exe"), gui_open_fn=lambda: False, clock=Clock())
    agent.step()
    st = json.loads((tmp_path / "agent-status.json").read_text())
    assert st["pid"] == os.getpid() and st["alert"] is None
    ag.AgentConfigStore(str(tmp_path)).save({"dpi_popup": {"enabled": False}})
    os.utime(tmp_path / "agent.json", (1, 1))                    # make sure the mtime differs
    agent.step()
    assert agent.cfg["dpi_popup"]["enabled"] is False            # settings hot-reload
    (tmp_path / "agent.stop").write_text("stop")
    agent.step()
    assert agent.stop_event.is_set() and not (tmp_path / "agent.stop").exists()


# ---------------------------------------------------------------- locks

def elsewhere(fn):
    """Run fn in another thread, like another process would (Windows mutexes are owned per thread)."""
    box = {}

    def go():
        try:
            box["value"] = fn()
        except Exception as e:                      # noqa: BLE001 - reported to the test
            box["error"] = e
    t = threading.Thread(target=go)
    t.start()
    t.join(10)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def try_lock(name, directory, timeout=0.1):
    def go():
        other = devlock.NamedLock(name, directory)   # separate handle, like another program
        try:
            other.acquire(timeout=timeout)
        except devlock.LockTimeout:
            return False
        other.release()
        return True
    return elsewhere(go)


def test_named_lock_is_reentrant_and_exclusive(tmp_path):
    a = devlock.NamedLock("t", str(tmp_path))
    with a:
        with a:                                     # re-entrant in one process
            assert try_lock("t", str(tmp_path)) is False
    assert try_lock("t", str(tmp_path), timeout=1) is True


def test_presence_marker(tmp_path):
    d = str(tmp_path)
    p = devlock.Presence("gui", d)
    assert elsewhere(lambda: devlock.is_running("gui", d)) is False
    assert p.claim() is True
    assert elsewhere(lambda: devlock.is_running("gui", d)) is True
    assert elsewhere(lambda: devlock.Presence("gui", d).claim()) is False
    p.drop()
    assert elsewhere(lambda: devlock.is_running("gui", d)) is False


def test_locked_device_holds_the_lock_while_open(tmp_path, monkeypatch):
    from blazectl import backend

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(devlock, "_locks", {})
    events = []

    class Inner:
        def open(self):
            events.append("open")

        def close(self):
            events.append("close")

        def set_feature(self, d):
            return 8

    dev = backend.LockedDevice(Inner())
    d = os.path.join(str(tmp_path), "blazectl")
    with dev:
        assert dev.set_feature(b"x") == 8
        assert try_lock(devlock.DEVICE_LOCK, d) is False
    assert try_lock(devlock.DEVICE_LOCK, d, timeout=1) is True
    assert events == ["open", "close"]


# ---------------------------------------------------------------- CLI

def test_cli_auto_rules(tmp_path, monkeypatch, capsys):
    from blazectl import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(ag, "is_running", lambda: False)
    cli.main(["auto", "add", "valorant.exe", "game1"])
    cli.main(["auto", "default", "office"])
    cli.main(["auto", "on"])
    out = capsys.readouterr().out
    assert "valorant.exe" in out and "-> game1" in out and "agent isn't running" in out
    cfg = json.loads((tmp_path / "blazectl" / "agent.json").read_text())
    assert cfg["auto_profiles"] == {"enabled": True, "rules": [{"match": "valorant.exe", "profile": "game1"}],
                                    "default": "office", "delay": 1.5}
    cli.main(["auto", "remove", "valorant.exe"])
    with pytest.raises(SystemExit):
        cli.main(["auto", "remove", "nothing.exe"])
    with pytest.raises(SystemExit):
        cli.main(["auto", "add", "x.exe", "work"])


def test_autostart_linux(tmp_path, monkeypatch):
    if not os.name == "posix":
        pytest.skip("posix only")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(ag.sys, "platform", "linux")
    assert ag.autostart_enabled() is False
    ag.set_autostart(True)
    text = (tmp_path / "autostart" / "blaze-agent.desktop").read_text()
    assert "-m blazectl agent" in text and "Name=BLAZE Agent" in text
    assert ag.autostart_enabled() is True
    ag.set_autostart(False)
    assert ag.autostart_enabled() is False


# ---------------------------------------------------------------- GUI endpoints

def test_gui_agent_endpoints(tmp_path, monkeypatch):
    from blazectl import webui

    svc, _ = make_service(tmp_path)
    monkeypatch.setattr(ag, "is_running", lambda: False)
    monkeypatch.setattr(ag, "autostart_enabled", lambda: False)
    api = webui.Api(svc)
    info = api.handle("/api/agent", {})
    assert info["running"] is False and info["config"]["dpi_popup"]["enabled"] is False
    cfg = info["config"]
    cfg["auto_profiles"]["rules"] = [{"match": "valorant.exe", "profile": "game1"}]
    info = api.handle("/api/agent/save", {"config": cfg})
    assert info["config"]["auto_profiles"]["rules"][0]["profile"] == "game1"
    cfg["auto_profiles"]["rules"] = [{"match": "x.exe", "profile": "nope"}]
    with pytest.raises(webui.ServiceError):
        api.handle("/api/agent/save", {"config": cfg})
    with pytest.raises(webui.ServiceError):
        api.handle("/api/agent/test-alert", {"severity": "high"})        # agent not running
    with pytest.raises(webui.NotFound):
        api.handle("/api/agent/nope", {})


def test_agent_runs_end_to_end(tmp_path, monkeypatch):
    """The real run loop: presence marker, alert endpoint, CLI client, stop request."""
    import time
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(devlock, "_locks", {})
    d = tmp_path / "blazectl"
    svc, fake = make_service(tmp_path)
    ag.AgentConfigStore(str(d)).save({"alerts": {"enabled": True, "seconds": 30}, "dpi_popup": {"enabled": False}})
    overlay = NullOverlay()
    agent = ag.Agent(svc=svc, directory=str(d), overlay=overlay, fg_fn=lambda: None, gui_open_fn=lambda: False)
    t = threading.Thread(target=agent.run, daemon=True)
    t.start()
    for _ in range(50):
        if (d / "agent-endpoint.json").exists():
            break
        time.sleep(0.05)
    assert ag.is_running() is True
    assert ag.Agent(svc=svc, directory=str(d), overlay=NullOverlay()).run() == 1      # second copy refuses
    assert ag.send_alert("critical", "end to end", directory=str(d)) is True
    for _ in range(40):
        if overlay.shown:
            break
        time.sleep(0.05)
    assert overlay.shown and overlay.shown[0].kind == "alert"
    assert ag.preview_popup(str(d)) is True
    assert overlay.shown[-1].kind == "dpi"
    assert ag.request_stop(str(d), wait=5) is True
    t.join(5)
    assert not t.is_alive() and not (d / "agent-endpoint.json").exists()
    assert fake.writes() == []                       # the alert only touched RAM lighting


def test_cli_events_lists_reports_and_stops_cleanly(monkeypatch, capsys):
    from types import SimpleNamespace
    from blazectl import cli

    nodes = [SimpleNamespace(vid=0x30FA, backend="hidapi", usage_page=0xFF00, usage=1, interface=1),
             SimpleNamespace(vid=0x30FA, backend="hidapi", usage_page=0x01, usage=0x02, interface=0),   # locked mouse
             SimpleNamespace(vid=0x30FA, backend="hidapi", usage_page=0x0C, usage=1, interface=1),
             SimpleNamespace(vid=0x1234, backend="hidapi", usage_page=0xFF00, usage=1, interface=1)]

    class Dev:
        def __init__(self, n):
            self.n, self.q = n, [bytes([0x06, 0x02])] if n.usage_page == 0xFF00 else []

        def open(self):
            return self

        def close(self):
            pass

        def read_input(self, timeout):
            if self.q:
                return self.q.pop()
            if self.n.usage_page == 0x0C:
                raise KeyboardInterrupt                   # a mouse button set to Ctrl+C
            return None

    monkeypatch.setattr(cli, "enumerate_nodes", lambda: nodes)
    monkeypatch.setattr(cli, "make_device", lambda n: Dev(n))
    cli.main(["events", "--seconds", "5"])
    out = capsys.readouterr().out
    assert "vendor event channel, consumer (media keys)" in out and "generic desktop" not in out
    assert f"{'vendor event channel':24} 06 02" in out and "stopped (Ctrl+C" in out and "1 report(s) seen" in out
