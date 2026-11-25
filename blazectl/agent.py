# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""BLAZE Agent: the small background part of blazectl.

    blazectl agent                 run it (Ctrl+C or `blazectl agent --stop` to quit)
    BLAZE Control.exe --agent      the same, windowless (what "Start with Windows" uses)

It does three optional jobs, configured in <config dir>/agent.json (the app's Automation page edits it):

* Per-app profiles: when an app comes to the front (valorant.exe, code.exe, title:*YouTube*), apply the
  profile its rule names. Only settings that differ are written, so the mouse's flash is spared.
* DPI popup: shows "DPI 1600 · level 3" on screen when the active DPI level changes.
* Alert light: blinks the mouse in a severity colour when a security tool reports an alert
  (JSON lines appended to a file, or `blazectl alert` / a POST to the agent's local endpoint).
  Alerts only use lighting, which lives in RAM: no flash writes.

All USB traffic goes through MouseService, which holds the machine-wide device lock (devlock.py),
so the agent, the app and CLI commands never talk over each other.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .foreground import UnsupportedDesktop, foreground_app, rule_matches
from .overlay import Popup, alert_popup, dpi_popup, make_overlay, profile_popup
from .service import PROFILE_SLOTS, LightingSettings, MouseService, ProfileStore, ServiceError, config_dir

_log = logging.getLogger("blazectl.agent")

PROFILE_LABELS = {"office": "Office", "game1": "Game I", "game2": "Game II", "media": "Media"}
SEVERITIES = ("info", "low", "medium", "high", "critical")
SEVERITY_COLOR = {"critical": "#ff0000", "high": "#ff4400", "medium": "#ffcc00", "low": "#0088ff", "info": "#00ccff"}
BLINK_INTERVAL = {"critical": 0.25, "high": 0.4, "medium": 0.7, "low": 1.0, "info": 1.2}
IGNORED_APPS = {"blaze control.exe", "blaze control"}          # our own window
DEFAULTS = {
    "auto_profiles": {"enabled": False, "rules": [], "default": None, "delay": 1.5},
    "dpi_popup": {"enabled": False, "position": "bottom", "seconds": 1.2, "poll_ms": 400},   # opt-in: it polls
    "alerts": {"enabled": False, "files": [], "min_severity": "medium", "seconds": 15, "listen": True},
}


class ConfigError(ValueError):
    pass


def _path(name: str, directory: str | None = None) -> str:
    return os.path.join(directory or config_dir(), name)


def _atomic_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- config

def validate_config(cfg: dict) -> dict:
    """Merge with defaults and check every field. Raises ConfigError with a readable message."""
    if not isinstance(cfg, dict):
        raise ConfigError("agent settings must be an object")
    out = json.loads(json.dumps(DEFAULTS))
    try:
        ap = dict(cfg.get("auto_profiles") or {})
        rules = []
        for r in list(ap.get("rules") or [])[:50]:
            match, prof = str(r["match"]).strip(), str(r["profile"])
            if not match or len(match) > 120:
                raise ConfigError("a rule needs an app name (1-120 characters)")
            if prof not in PROFILE_SLOTS:
                raise ConfigError(f"rule {match!r}: unknown profile {prof!r}")
            rules.append({"match": match, "profile": prof})
        default = ap.get("default")
        if default is not None and default not in PROFILE_SLOTS:
            raise ConfigError(f"unknown default profile {default!r}")
        delay = float(ap.get("delay", 1.5))
        if not 0.3 <= delay <= 30:
            raise ConfigError("switch delay must be 0.3-30 seconds")
        out["auto_profiles"] = {"enabled": bool(ap.get("enabled", False)), "rules": rules,
                                "default": default, "delay": delay}

        dp = dict(cfg.get("dpi_popup") or {})
        pos = str(dp.get("position", "bottom"))
        if pos not in ("bottom", "top"):
            raise ConfigError("popup position must be bottom or top")
        secs, poll = float(dp.get("seconds", 1.2)), int(dp.get("poll_ms", 400))
        if not 0.5 <= secs <= 10:
            raise ConfigError("popup time must be 0.5-10 seconds")
        if not 150 <= poll <= 5000:
            raise ConfigError("DPI check interval must be 150-5000 ms")
        out["dpi_popup"] = {"enabled": bool(dp.get("enabled", False)), "position": pos, "seconds": secs, "poll_ms": poll}

        al = dict(cfg.get("alerts") or {})
        files = [str(f) for f in list(al.get("files") or []) if str(f).strip()][:10]
        sev = str(al.get("min_severity", "medium"))
        if sev not in SEVERITIES:
            raise ConfigError(f"minimum severity must be one of {', '.join(SEVERITIES)}")
        asecs = float(al.get("seconds", 15))
        if not 2 <= asecs <= 600:
            raise ConfigError("alert time must be 2-600 seconds")
        out["alerts"] = {"enabled": bool(al.get("enabled", False)), "files": files, "min_severity": sev,
                         "seconds": asecs, "listen": bool(al.get("listen", True))}
    except (KeyError, TypeError, ValueError) as e:
        if isinstance(e, ConfigError):
            raise
        raise ConfigError(f"invalid agent settings: {e}") from e
    return out


class AgentConfigStore:
    def __init__(self, directory: str | None = None):
        self.path = _path("agent.json", directory)

    def mtime(self) -> float:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return validate_config(json.load(f))
        except (OSError, ValueError):
            return validate_config({})

    def save(self, cfg: dict) -> dict:
        cfg = validate_config(cfg)
        _atomic_json(self.path, cfg)
        return cfg


# --------------------------------------------------------------------------- per-app profiles

class ProfileSwitcher:
    def __init__(self, svc: MouseService, profiles: ProfileStore, cfg_fn, notify, fg_fn=foreground_app,
                 gui_open_fn=lambda: False, clock=time.monotonic, on_applied=lambda: None):
        self.svc, self.profiles, self.cfg_fn, self.notify = svc, profiles, cfg_fn, notify
        self.fg_fn, self.gui_open_fn, self.clock, self.on_applied = fg_fn, gui_open_fn, clock, on_applied
        self.candidate = None
        self.since = 0.0
        self.applied = None
        self.retry_at = 0.0
        self.foreground = ""
        self.recent: deque[str] = deque(maxlen=15)
        self.paused = None
        self.error = None

    def target_for(self, app, cfg) -> str | None:
        for r in cfg["rules"]:
            if rule_matches(r["match"], app):
                return r["profile"]
        return cfg["default"]

    def tick(self) -> None:
        cfg = self.cfg_fn()["auto_profiles"]
        if not cfg["enabled"]:
            self.paused, self.candidate = None, None
            return
        if self.gui_open_fn():
            self.paused = "BLAZE Control is open (switching resumes when you close it)"
            self.candidate = None
            self.applied = None          # you may switch profiles by hand there: re-apply the right one after
            return
        self.paused = None
        try:
            app = self.fg_fn()
        except UnsupportedDesktop as e:
            self.error = str(e)
            return
        except Exception as e:                       # never let a desktop quirk kill the agent
            self.error = f"couldn't read the focused app: {e}"
            return
        if app is None or app.exe in IGNORED_APPS:
            return
        self.foreground = app.exe or app.title
        if app.exe:
            if app.exe in self.recent:
                self.recent.remove(app.exe)
            self.recent.appendleft(app.exe)
        target = self.target_for(app, cfg)
        now = self.clock()
        if target is None:
            self.candidate = None
            return
        if target != self.candidate:
            self.candidate, self.since = target, now
            return
        if target == self.applied or now - self.since < cfg["delay"] or now < self.retry_at:
            return
        slot_cfg = self.profiles.load()["slots"].get(target)
        if not slot_cfg:
            self.error = (f"{PROFILE_LABELS[target]} is empty: open BLAZE Control, pick {PROFILE_LABELS[target]} "
                          "and Sync once to save it")
            self.retry_at = now + 30
            return
        try:
            changed = self.svc.apply_config(slot_cfg)
        except ServiceError as e:
            self.error = f"couldn't switch to {PROFILE_LABELS[target]}: {e}"
            self.retry_at = now + 10
            return
        self.applied, self.error = target, None
        try:
            self.profiles.save(target, None, active=target)
        except (OSError, ServiceError):
            pass
        _log.info("profile %s for %s (%s)", target, app.exe, ", ".join(changed) or "lighting only")
        self.notify(profile_popup(PROFILE_LABELS[target], changed))
        self.on_applied()


# --------------------------------------------------------------------------- DPI popup

class DpiWatcher:
    """Pops up the DPI when the active level changes. It polls the mouse (read-only), so it:
    * ignores garbled reads (a level that doesn't exist or is disabled): another program talking to the
      mouse at the same moment can swap replies, and on a real mouse a read occasionally comes back wrong;
    * confirms a change with a second quick read before showing anything;
    * pauses while the official EvoFox app runs, so the two never talk over each other."""

    APP_CHECK_S = 10.0
    CONFIRM_S = 0.08

    def __init__(self, svc: MouseService, cfg_fn, notify, clock=time.monotonic, app_running_fn=None):
        from .service import official_app_running
        self.svc, self.cfg_fn, self.notify, self.clock = svc, cfg_fn, notify, clock
        self.app_running_fn = app_running_fn or official_app_running
        self.level = None
        self.pending = None
        self.values = None
        self.next_t = 0.0
        self.error = None
        self.paused = None
        self.glitches = 0
        self._app_t = 0.0
        self._app_running = False

    def invalidate(self) -> None:
        self.values = None

    @staticmethod
    def _plausible(vals) -> bool:
        return vals is not None and len(vals) == 6 and all(vals)

    def tick(self) -> None:
        cfg = self.cfg_fn()["dpi_popup"]
        if not cfg["enabled"]:
            self.level = self.pending = self.paused = None
            return
        now = self.clock()
        if now < self.next_t:
            return
        if now >= self._app_t:
            self._app_t = now + self.APP_CHECK_S
            try:
                self._app_running = bool(self.app_running_fn())
            except Exception:
                self._app_running = False
        if self._app_running:
            self.paused = "DPI popup paused: the EvoFox app is running"
            self.level = self.pending = None
            self.next_t = now + 2
            return
        self.paused = None
        self.next_t = now + cfg["poll_ms"] / 1000
        try:
            level, mask, vals = self.svc.read_active(with_values=self.values is None)
            if self._plausible(vals):
                self.values = vals
            if level >= len(LEVEL_NAMES) or not (mask >> level) & 1:
                self.glitches += 1                   # garbled read: ignore it
                self.pending = None
                return
            if self.level is None or level == self.level:
                self.level, self.pending, self.error = level, None, None
                return
            if self.pending != level:                # looks changed: confirm before showing
                self.pending = level
                self.next_t = now + self.CONFIRM_S
                return
            self.level, self.pending, self.error = level, None, None
            _, _, vals = self.svc.read_active(with_values=True)        # fresh values for the popup
            if self._plausible(vals):
                self.values = vals
            colors = self.svc.load_lighting().colors
            color = colors[level] if level < len(colors) else "#35d6ff"
            dpi = self.values[level] if self.values else None
            self.notify(dpi_popup(level + 1, dpi, color, cfg["seconds"]))
        except ServiceError as e:                    # unplugged / busy: try again a bit later
            self.level = self.pending = None
            self.error = str(e)
            self.next_t = now + 3


LEVEL_NAMES = ("L1", "L2", "L3", "L4", "L5", "L6")


# --------------------------------------------------------------------------- alert light

_SEV_ALIASES = {"informational": "info", "information": "info", "notice": "low", "warning": "medium", "warn": "medium",
                "moderate": "medium", "error": "high", "severe": "critical", "crit": "critical", "emergency": "critical",
                "alert": "critical", "fatal": "critical"}


def normalize_severity(v) -> str:
    if isinstance(v, bool):
        return "medium"
    if isinstance(v, (int, float)):                  # 0-10 scales (CVSS-like)
        return "critical" if v >= 9 else "high" if v >= 7 else "medium" if v >= 4 else "low" if v >= 1 else "info"
    s = str(v).strip().lower()
    if s.isdigit():
        return normalize_severity(int(s))
    s = _SEV_ALIASES.get(s, s)
    return s if s in SEVERITIES else "medium"


def parse_alert(obj) -> tuple[str, str] | None:
    """A JSON alert (Sigma/TriageLens style or a simple {severity, title}) -> (severity, title)."""
    if not isinstance(obj, dict):
        return None
    rule = obj.get("rule") if isinstance(obj.get("rule"), dict) else {}
    sev = next((src[k] for src in (obj, rule) for k in ("severity", "level", "priority", "risk") if k in src), None)
    title = next((str(src[k]) for src in (obj, rule) for k in ("title", "rule_name", "name", "summary", "message",
                                                               "description") if src.get(k)), None)
    if title is None and isinstance(obj.get("rule"), str):
        title = obj["rule"]
    if sev is None and title is None:
        return None
    return normalize_severity(sev if sev is not None else "medium"), (title or "Security alert")[:200]


class FileTail:
    """Follows a growing file like `tail -f`; starts at the end so old alerts don't fire."""

    def __init__(self, path: str):
        self.path = path
        self.pos = None
        self.partial = b""

    def poll(self) -> list[str]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            self.pos, self.partial = 0, b""            # appears later: read it from the start
            return []
        if self.pos is None:
            self.pos = size
            return []
        if size < self.pos:                          # truncated or rotated
            self.pos, self.partial = 0, b""
        if size == self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read(min(size - self.pos, 1 << 20))
        self.pos += len(data)
        data = self.partial + data
        lines = data.split(b"\n")
        self.partial = lines.pop()
        return [ln.decode("utf-8", "replace").strip() for ln in lines if ln.strip()]


@dataclass
class ActiveAlert:
    severity: str
    title: str
    until: float
    on: bool = False
    next_toggle: float = 0.0


class AlertBeacon:
    def __init__(self, svc: MouseService, cfg_fn, notify, clock=time.monotonic):
        self.svc, self.cfg_fn, self.notify, self.clock = svc, cfg_fn, notify, clock
        self.inbox: queue.Queue[tuple[str, str]] = queue.Queue()
        self.tails: dict[str, FileTail] = {}
        self.active: ActiveAlert | None = None
        self.last: tuple[str, str] | None = None
        self.error = None

    def submit(self, severity, title: str) -> bool:
        cfg = self.cfg_fn()["alerts"]
        sev = normalize_severity(severity)
        if not cfg["enabled"] or SEVERITIES.index(sev) < SEVERITIES.index(cfg["min_severity"]):
            return False
        self.inbox.put((sev, str(title)[:200]))
        return True

    def ack(self) -> None:
        self.inbox.put(("__ack__", ""))

    def _poll_files(self, cfg) -> None:
        wanted = set(cfg["files"])
        for p in list(self.tails):
            if p not in wanted:
                del self.tails[p]
        for p in wanted:
            tail = self.tails.setdefault(p, FileTail(p))
            for line in tail.poll():
                try:
                    parsed = parse_alert(json.loads(line))
                except ValueError:
                    parsed = None
                if parsed:
                    self.submit(*parsed)

    def _show(self, color: str) -> None:
        self.svc.preview_lighting(LightingSettings(mode="static", colors=[color] * 6, brightness=7))

    def tick(self) -> None:
        cfg = self.cfg_fn()["alerts"]
        now = self.clock()
        if cfg["enabled"]:
            self._poll_files(cfg)
        newest = None
        while True:
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            if item[0] == "__ack__":
                newest = None
                if self.active:
                    self.active.until = now
                continue
            if newest is None or SEVERITIES.index(item[0]) >= SEVERITIES.index(newest[0]):
                newest = item
        if newest is not None:
            sev, title = newest
            if self.active is None or SEVERITIES.index(sev) >= SEVERITIES.index(self.active.severity) \
                    or now >= self.active.until:
                self.active = ActiveAlert(sev, title, now + cfg["seconds"])
                self.last = newest
                _log.warning("alert %s: %s", sev, title)
                self.notify(alert_popup(sev, title))
        a = self.active
        if a is None:
            return
        try:
            if now >= a.until:
                self.svc.restore_lighting()       # if this fails, the alert stays and we retry (below)
                self.active = None
                self.error = None
                return
            if now >= a.next_toggle:
                a.on = not a.on
                self._show(SEVERITY_COLOR[a.severity] if a.on else "#000000")
                a.next_toggle = now + BLINK_INTERVAL[a.severity]
            self.error = None
        except ServiceError as e:
            self.error = str(e)
            a.next_toggle = now + 2


def _token_path(directory=None) -> str:
    return _path("agent-token", directory)


def load_or_create_token(directory: str | None = None) -> str:
    p = _token_path(directory)
    try:
        with open(p, encoding="utf-8") as f:
            tok = f.read().strip()
        if len(tok) >= 16:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)     # owner-only on Linux
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(tok)
    return tok


def make_alert_server(beacon: AlertBeacon, token: str, port: int = 0, on_preview=None):
    """POST /alert {"severity","title"}, /ack and /preview on 127.0.0.1, with the agent's token."""

    class H(BaseHTTPRequestHandler):
        server_version = "blaze-agent"

        def log_message(self, *a):
            pass

        def _json(self, status, obj):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            if not secrets.compare_digest(self.headers.get("X-Blazectl-Token", ""), token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad Content-Length"})
            if n > 4096:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
            if self.path == "/alert":
                parsed = parse_alert(body)
                if not parsed:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": "need severity and/or title"})
                return self._json(HTTPStatus.ACCEPTED, {"accepted": beacon.submit(*parsed)})
            if self.path == "/ack":
                beacon.ack()
                return self._json(HTTPStatus.OK, {"ok": True})
            if self.path == "/preview" and on_preview is not None:
                on_preview()
                return self._json(HTTPStatus.OK, {"ok": True})
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def preview_popup(directory: str | None = None) -> bool:
    """Ask the running agent to show a sample DPI popup (the app's PREVIEW button)."""
    return send_alert("", "", directory, path="preview") is not None


def send_alert(severity: str, title: str, directory: str | None = None, ack: bool = False,
               timeout: float = 3, path: str | None = None) -> bool | None:
    """Tell a running agent about an alert.

    Returns None when no agent is listening, False when the agent ignored it (alert light off or below
    the minimum severity), True when it's showing."""
    import urllib.error
    import urllib.request

    try:
        with open(_path("agent-endpoint.json", directory), encoding="utf-8") as f:
            port = int(json.load(f)["port"])
        with open(_token_path(directory), encoding="utf-8") as f:
            tok = f.read().strip()
    except (OSError, ValueError, KeyError):
        return None
    path = path or ("ack" if ack else "alert")
    body = json.dumps({"severity": severity, "title": title} if path == "alert" else {}).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/{path}", data=body,
                                 headers={"Content-Type": "application/json", "X-Blazectl-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reply = json.loads(r.read() or b"{}")
            return bool(reply.get("accepted")) if path == "alert" else True
    except (urllib.error.URLError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- the agent

def gui_is_open() -> bool:
    from .devlock import GUI_LOCK, is_running
    try:
        return is_running(GUI_LOCK)
    except OSError:
        return False


class Agent:
    def __init__(self, svc: MouseService | None = None, directory: str | None = None, overlay=None,
                 fg_fn=foreground_app, gui_open_fn=gui_is_open, clock=time.monotonic):
        self.dir = directory or config_dir()
        self.svc = svc or MouseService(settings_path=_path("lighting.json", self.dir))
        self.store = AgentConfigStore(self.dir)
        self.profiles = ProfileStore(_path("profiles.json", self.dir))
        self.cfg = self.store.load()
        self._cfg_mtime = self.store.mtime()
        self.overlay = overlay
        self.clock = clock
        self.stop_event = threading.Event()
        self.dpi = DpiWatcher(self.svc, lambda: self.cfg, self._notify, clock)
        self.switcher = ProfileSwitcher(self.svc, self.profiles, lambda: self.cfg, self._notify, fg_fn,
                                        gui_open_fn, clock, on_applied=self.dpi.invalidate)
        self.beacon = AlertBeacon(self.svc, lambda: self.cfg, self._notify, clock)
        self.server = None
        self.started = time.time()
        self._next_switch = 0.0
        self._next_status = 0.0

    def _preview(self) -> None:
        level = (self.dpi.level or 0)
        vals = self.dpi.values or [1600] * 6
        colors = self.svc.load_lighting().colors
        self._notify(dpi_popup(level + 1, vals[level], colors[level] if level < len(colors) else "#35d6ff",
                               self.cfg["dpi_popup"]["seconds"] + 1.5))

    def _notify(self, p: Popup) -> None:
        if self.overlay is not None:
            self.overlay.show(p)

    def reload_config(self) -> None:
        m = self.store.mtime()
        if m != self._cfg_mtime:
            self._cfg_mtime = m
            self.cfg = self.store.load()
            _log.info("settings reloaded")

    def status(self) -> dict:
        a = self.beacon.active
        return {
            "pid": os.getpid(), "started": self.started, "updated": time.time(),
            "profile": self.switcher.applied, "foreground": self.switcher.foreground,
            "recent_apps": list(self.switcher.recent),
            "paused": "; ".join(p for p in (self.switcher.paused, self.dpi.paused) if p) or None,
            "errors": [e for e in (self.switcher.error, self.dpi.error if self.cfg["dpi_popup"]["enabled"] else None,
                                   self.beacon.error) if e],
            "alert": {"severity": a.severity, "title": a.title} if a else None,
            "last_alert": list(self.beacon.last) if self.beacon.last else None,
            "listening": self.server is not None,
        }

    def step(self) -> None:
        """One round of work (the worker thread calls this every 50 ms)."""
        now = self.clock()
        self.reload_config()
        self.beacon.tick()
        self.dpi.tick()
        if now >= self._next_switch:
            self._next_switch = now + 0.5
            self.switcher.tick()
        if now >= self._next_status:
            self._next_status = now + 2
            try:
                _atomic_json(_path("agent-status.json", self.dir), self.status())
            except OSError:
                pass
        stop = _path("agent.stop", self.dir)
        if os.path.exists(stop):
            try:
                os.remove(stop)
            except OSError:
                pass
            self.stop_event.set()

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.step()
            except Exception:                        # log and keep going: this runs for days
                _log.exception("agent step failed")
                self.stop_event.wait(1)
            self.stop_event.wait(0.05)

    def _start_listener(self) -> None:
        if not self.cfg["alerts"]["listen"]:
            return
        try:
            self.server = make_alert_server(self.beacon, load_or_create_token(self.dir), on_preview=self._preview)
        except OSError as e:
            _log.warning("alert endpoint not started: %s", e)
            return
        threading.Thread(target=self.server.serve_forever, daemon=True, name="blaze-alerts").start()
        _atomic_json(_path("agent-endpoint.json", self.dir), {"port": self.server.server_address[1], "pid": os.getpid()})

    def run(self) -> int:
        from .devlock import AGENT_LOCK, Presence

        presence = Presence(AGENT_LOCK)
        if not presence.claim():
            print("BLAZE Agent is already running.", file=sys.stderr)
            return 1
        try:
            stale = _path("agent.stop", self.dir)
            if os.path.exists(stale):
                os.remove(stale)
            if self.overlay is None:
                self.overlay = make_overlay(True, self.cfg["dpi_popup"]["position"])
            self._start_listener()
            _log.info("agent started (pid %d)", os.getpid())
            worker = threading.Thread(target=self._worker, daemon=True, name="blaze-agent")
            worker.start()
            try:
                self.overlay.run(self.stop_event)
            except KeyboardInterrupt:
                pass
            self.stop_event.set()
            worker.join(timeout=5)
            if self.beacon.active:
                try:
                    self.svc.restore_lighting()
                except ServiceError:
                    pass
            return 0
        finally:
            if self.server:
                self.server.shutdown()
            for name in ("agent-endpoint.json", "agent-status.json"):
                try:
                    os.remove(_path(name, self.dir))
                except OSError:
                    pass
            presence.drop()
            _log.info("agent stopped")


# --------------------------------------------------------------------------- control

def is_running() -> bool:
    from .devlock import AGENT_LOCK, is_running as _running
    try:
        return _running(AGENT_LOCK)
    except OSError:
        return False


def read_status(directory: str | None = None) -> dict | None:
    try:
        with open(_path("agent-status.json", directory), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def request_stop(directory: str | None = None, wait: float = 6.0) -> bool:
    if not is_running():
        return True
    p = _path("agent.stop", directory)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write("stop")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(0.2)
    return False


def agent_command() -> list[str]:
    """How to start the agent with no console window."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--agent"]
    exe = sys.executable
    if sys.platform.startswith("win"):
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.exists(w) else exe
    return [exe, "-m", "blazectl", "agent"]


def launch() -> None:
    """Start the agent in the background (from the app's Automation page)."""
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "close_fds": True}
    if sys.platform.startswith("win"):
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(agent_command(), **kw)


AUTOSTART_NAME = "BLAZE Agent"


def _desktop_file() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "blaze-agent.desktop")


def autostart_enabled() -> bool:
    if sys.platform.startswith("win"):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, AUTOSTART_NAME)
                return True
        except OSError:
            return False
    return os.path.exists(_desktop_file())


def set_autostart(on: bool) -> None:
    """Start the agent when you log in. Per-user only: no admin/sudo needed."""
    cmd = agent_command()
    if sys.platform.startswith("win"):
        import winreg
        line = subprocess.list2cmdline(cmd)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, line)
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return
    path = _desktop_file()
    if on:
        import shlex
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=BLAZE Agent\n"
                    "Comment=blazectl: per-app mouse profiles, DPI popup, alert light\n"
                    f"Exec={' '.join(shlex.quote(c) for c in cmd)}\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n")
    elif os.path.exists(path):
        os.remove(path)


def setup_logging(console: bool = False) -> None:
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = []
    try:
        os.makedirs(config_dir(), exist_ok=True)
        handlers.append(RotatingFileHandler(_path("agent.log"), maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers or None)


def main(argv: list[str] | None = None, console: bool = False) -> int:
    setup_logging(console)
    return Agent().run()
