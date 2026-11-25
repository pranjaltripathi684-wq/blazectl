# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""blazectl desktop UI: a local web app served by Python's standard library.

    python -m blazectl gui

Security (the page can control a USB device, so it's locked down):
* binds to 127.0.0.1 only, on a random port: nothing on the network can reach it;
* every API call needs a random per-session token that only the opened window knows,
  so other websites in your browser can't drive the mouse (CSRF);
* the Host header must be 127.0.0.1/localhost (blocks DNS-rebinding attacks);
* requests are small JSON bodies with a size limit.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from .drivers import instant as ins
from .drivers import macro as mac
from .service import (
    MacroLibrary,
    PROFILE_SLOTS, LightingSettings, MouseService, ProfileStore, ServiceError, button_choices, official_app_running,
)

FUNCTION_LABELS = {
    "left-click": "Left Click", "middle-click": "Middle Click", "right-click": "Right Click",
    "back": "Back", "forward": "Forward", "dpi-loop": "DPI Loop", "dpi-up": "DPI +", "dpi-down": "DPI −",
    "double-click": "Double Click", "show-desktop": "Show Desktop", "rapid-fire": "Rapid Fire", "led-loop": "LED Loop",
    "macro": "Macro", "macro-hold": "Macro (hold)", "media": "Media", "shortcut": "Shortcut",
}
BUTTON_LABELS = {
    1: "Left button", 2: "Right button", 3: "Wheel click", 4: "Side · front",
    5: "Side · rear", 6: "Top · upper", 7: "Top · lower", 8: "Fire button",
}
MODE_LABELS = {
    "static": "Static", "breathing": "Breathing", "breathing-multi": "Breathing RGB", "rainbow": "Rainbow wave",
    "spectrum": "Spectrum", "fill": "Fill & drain", "loop": "Loop", "enraptured": "Enraptured",
    "flicker": "Flicker", "ripple": "Ripple", "startrek": "Star Trek", "off": "Off",
}
MAX_BODY = 128 * 1024


class NotFound(Exception):
    pass


def _special_name(b) -> str | None:
    """Function name the GUI uses for a button holding a media key or shortcut."""
    if isinstance(b, mac.Media):
        return "media"
    if isinstance(b, mac.Shortcut):
        return "shortcut"
    return None


def catalog() -> dict:
    """Static info the page needs to draw its controls."""
    from . import __version__
    return {
        "version": __version__,
        "dpi_values": list(ins.dpi_table(0x1440).values()),
        "poll_rates": sorted(ins.POLL_RATES),
        "modes": [
            {"id": m.name, "label": MODE_LABELS[m.name], "color": m.color, "speed": m.speed,
             "brightness": m.brightness, "direction": m.direction}
            for m in ins.MODES.values()
        ],
        "speed_max": ins.SPEED_MAX,
        "brightness_max": ins.BRIGHTNESS_MAX,
        "functions": FUNCTION_LABELS,
        "factory_buttons": {str(num): ins.FUNCTION_NAMES[code] for num, _, _, _, code in ins.BUTTON_SLOTS},
        "profile_slots": list(PROFILE_SLOTS),
        "buttons": {str(n): {"label": BUTTON_LABELS[n], "choices": list(button_choices(n))} for n in BUTTON_LABELS},
        "macro_buttons": {str(n): status for n, status in ins.MACRO_BUTTONS.items()},
        "keys": list(dict.fromkeys(mac.KEY_NAMES.values())),
        "max_pairs": mac.MAX_PAIRS,
        "media_keys": {k: label for k, (_, label) in mac.MEDIA_KEYS.items()},
        "media_confirmed": sorted(mac.MEDIA_CONFIRMED),
        "modifiers": list(mac.MODIFIERS),
        "macro_bytes": mac.MACRO_LIMIT - mac.MACRO_BASE - 0x28,
    }


class Api:
    def __init__(self, svc: MouseService, profiles: ProfileStore | None = None):
        self.svc = svc
        self.profiles = profiles or ProfileStore(
            os.path.join(os.path.dirname(svc.settings_path), "profiles.json"))
        self.library = MacroLibrary(os.path.join(os.path.dirname(svc.settings_path), "macros.json"))
        self.lock = threading.Lock()  # one USB conversation at a time

    def state(self) -> dict:
        light = asdict(self.svc.load_lighting())
        try:
            st = self.svc.read_state()
        except ServiceError as e:
            return {"connected": False, "error": str(e), "lighting": light, "official_app": official_app_running()}
        names = ins.FUNCTION_NAMES
        return {
            "connected": True,
            "name": st.name,
            "usb_id": st.usb_id,
            "dpi": st.dpi,
            "active_level": st.active_level,
            "polling": st.polling_hz,
            "buttons": {str(n): _special_name(st.macros.get(n)) or names.get(c, f"unknown-0x{c:02x}")
                        for n, c in st.buttons.items()},
            "macros": {str(n): m.to_json() for n, m in st.macros.items()},
            "macro_error": st.macro_error,
            "lighting": light,
            "official_app": official_app_running(),
        }

    def handle(self, path: str, body: dict) -> dict:
        with self.lock:
            if path == "/api/state":
                return self.state()
            if path == "/api/lighting":
                s = LightingSettings(
                    mode=str(body["mode"]), colors=[str(c) for c in body["colors"]][: ins.DPI_LEVELS],
                    speed=int(body["speed"]), brightness=int(body["brightness"]), direction=int(body["direction"]),
                )
                try:
                    self.svc.set_lighting(s)
                except ins.InstantError as e:
                    raise ServiceError(str(e)) from e
                return {"message": f"Lighting set to {MODE_LABELS.get(s.mode, s.mode)}"}
            if path == "/api/dpi":
                vals = self.svc.set_dpi([int(v) for v in body["dpi"]])
                return {"message": "DPI saved and verified: " + " · ".join(str(v) for v in vals)}
            if path == "/api/active":
                lvl = self.svc.set_active_level(int(body["level"]))
                return {"message": f"Level {lvl} is now active"}
            if path == "/api/polling":
                rate = int(body["rate"])
                changed = self.svc.set_polling(rate)
                return {"message": f"Polling rate {'saved and verified' if changed else 'already'}: {rate} Hz"}
            if path == "/api/buttons":
                mapping = {}
                for num, name in body["buttons"].items():
                    known = {**ins.BUTTON_FUNCTIONS, **ins.KEEP_ONLY_FUNCTIONS, "shortcut": mac.MACRO_HOLD}
                    if name not in known:
                        raise ServiceError(f"unknown function {name!r} for button {num}")
                    mapping[int(num)] = known[name]
                self.svc.set_buttons(mapping)
                return {"message": "Buttons saved and verified"}
            if path == "/api/macros":
                known = ins.BUTTON_FUNCTIONS
                bind = {}
                for n, d in dict(body.get("bind") or {}).items():
                    try:
                        bind[int(n)] = mac.binding_from_json(d)
                    except (mac.MacroError, KeyError, TypeError, ValueError) as e:
                        raise ServiceError(f"button {n}: macro not accepted ({e})") from e
                unbind = {}
                for n, name in dict(body.get("unbind") or {}).items():
                    if name not in known:
                        raise ServiceError(f"unknown function {name!r} for button {n}")
                    unbind[int(n)] = known[name]
                self.svc.set_macros(bind, unbind)
                parts = [f"button {n}" for n in sorted(bind)]
                gone = [f"button {n}" for n in sorted(unbind)]
                return {"message": ("Saved to " + ", ".join(parts) if parts else "") +
                        ("; " if parts and gone else "") + ("cleared " + ", ".join(gone) if gone else "") + " · verified"}
            if path == "/api/macros/library":
                return {"library": self.library.load()}
            if path == "/api/macros/library/save":
                try:
                    m = mac.Macro.from_json(body["macro"])
                except mac.MacroError as e:
                    raise ServiceError(f"bad macro: {e}") from e
                return {"library": self.library.save(str(body["name"]), m, body.get("old_name"))}
            if path == "/api/macros/library/delete":
                return {"library": self.library.delete(str(body["name"]))}
            if path == "/api/buttons/reset":
                self.svc.reset_buttons()
                return {"message": "Buttons reset to factory"}
            if path == "/api/profiles":
                return self.profiles.load()
            if path == "/api/profiles/save":
                slot = str(body.get("slot", ""))
                return self.profiles.save(slot, body.get("config"), body.get("active"))
            if path.startswith("/api/agent"):
                return self.agent(path, body)
            if path == "/api/backup":
                import tempfile

                fd, tmp = tempfile.mkstemp(suffix=".json")
                os.close(fd)
                try:
                    self.svc.backup(tmp)
                    with open(tmp) as f:
                        return {"message": "Backup created", "backup": json.load(f)}
                finally:
                    os.remove(tmp)
            raise NotFound(path)


    # -- automation (the background agent) ------------------------------------
    def agent_info(self) -> dict:
        from . import agent as ag
        d = os.path.dirname(self.svc.settings_path)
        running = ag.is_running()
        try:
            autostart = ag.autostart_enabled()
        except OSError:
            autostart = False
        slots = self.profiles.load()["slots"]
        return {"config": ag.AgentConfigStore(d).load(), "running": running, "autostart": autostart,
                "status": ag.read_status(d) if running else None,
                "saved_profiles": [k for k, v in slots.items() if v], "platform": sys.platform}

    def agent(self, path: str, body: dict) -> dict:
        from . import agent as ag
        d = os.path.dirname(self.svc.settings_path)
        if path == "/api/agent/save":
            try:
                ag.AgentConfigStore(d).save(body["config"])
            except ag.ConfigError as e:
                raise ServiceError(str(e)) from e
        elif path == "/api/agent/start":
            if not ag.is_running():
                ag.launch()
                import time
                for _ in range(30):
                    if ag.is_running():
                        break
                    time.sleep(0.1)
                if not ag.is_running():
                    raise ServiceError("the agent didn't start; see agent.log in the settings folder")
        elif path == "/api/agent/stop":
            if not ag.request_stop(d):
                raise ServiceError("the agent didn't stop within 6 s")
        elif path == "/api/agent/autostart":
            try:
                ag.set_autostart(bool(body["on"]))
            except OSError as e:
                raise ServiceError(f"couldn't change start-with-login: {e}") from e
        elif path == "/api/agent/test-alert":
            if not ag.is_running():
                raise ServiceError("start the agent first; it's what blinks the mouse")
            cfg = ag.AgentConfigStore(d).load()["alerts"]
            if not cfg["enabled"]:
                raise ServiceError("turn the alert light on first")
            want = ag.normalize_severity(body.get("severity", "high"))
            sev = max(want, cfg["min_severity"], key=ag.SEVERITIES.index)      # always passes the minimum
            res = ag.send_alert(sev, "Test alert from BLAZE Control", directory=d)
            if res is None:
                raise ServiceError("the agent isn't answering; stop and start it again")
        elif path == "/api/agent/preview":
            if not ag.is_running() or not ag.preview_popup(d):
                raise ServiceError("start the agent first; it's what shows the popup")
        elif path == "/api/agent/test-alert/ack":
            ag.send_alert("", "", directory=d, ack=True)
        elif path != "/api/agent":
            raise NotFound(path)
        return self.agent_info()


def make_handler(api: Api, token: str, page: bytes):
    class Handler(BaseHTTPRequestHandler):
        server_version = "blazectl"

        def log_message(self, *a):  # keep the terminal quiet
            pass

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, status, payload: bytes, ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                             "img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj).encode())

        def do_GET(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            if self.path.split("?")[0] in ("/", "/index.html"):
                return self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad host"})
            if not secrets.compare_digest(self.headers.get("X-Blazectl-Token", ""), token):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "bad token"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "bad Content-Length"})
            if length > MAX_BODY:
                return self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
            except ValueError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": f"bad JSON: {e}"})
            try:
                result = api.handle(self.path, body)
            except NotFound:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except ServiceError as e:
                return self._json(HTTPStatus.CONFLICT, {"error": str(e)})
            except KeyError as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": f"missing field {e}"})
            except (TypeError, ValueError, AttributeError) as e:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": f"bad request: {e}"})
            if self.path not in ("/api/state", "/api/profiles", "/api/profiles/save", "/api/macros/library",
                                 "/api/macros/library/save", "/api/macros/library/delete") \
                    and not self.path.startswith("/api/agent"):
                result["state"] = api.state()
            return self._json(HTTPStatus.OK, result)

    return Handler


def build_page(token: str) -> bytes:
    html = resources.files("blazectl").joinpath("web/index.html").read_text(encoding="utf-8")
    boot = json.dumps({"token": token, "catalog": catalog()})
    return html.replace("/*__BOOT__*/null", boot).encode("utf-8")


def make_server(svc: MouseService | None = None, port: int = 0):
    token = secrets.token_urlsafe(24)
    api = Api(svc or MouseService())
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api, token, build_page(token)))
    return server, token


def _open_window(url: str) -> None:
    """Prefer a chromeless app window (Edge/Chrome --app); fall back to the default browser."""
    candidates = []
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            candidates += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                           os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    else:
        candidates += [shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    for exe in candidates:
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1240,800"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
    webbrowser.open(url)


_presence = None


def claim_gui_presence() -> None:
    """While the editor is open, the agent pauses per-app switching (it would change the mouse under you)."""
    global _presence
    try:
        from .devlock import GUI_LOCK, Presence
        p = Presence(GUI_LOCK)
        if p.claim():
            _presence = p
    except OSError:
        pass


def main(open_window: bool = True) -> None:
    claim_gui_presence()
    server, _ = make_server()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"blazectl UI running at {url}  (local only; press Ctrl+C here to quit)")
    if open_window:
        _open_window(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
