# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""BLAZE Control: the desktop app.

Runs the same local GUI as `blazectl gui`, but inside a native window (pywebview, which uses
Edge WebView2 on Windows) instead of a browser. The server still listens on 127.0.0.1 only
with a per-session token, so the security model is unchanged. Without pywebview it falls back
to the browser app window.

    BLAZE Control.exe              open the app
    BLAZE Control.exe --selftest [report.txt]   check the bundle works (used by the build), then exit
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import urllib.request

from . import __version__

APP_NAME = "BLAZE Control"
_log = logging.getLogger("blazectl.app")


def _config_dir() -> str:
    from .service import config_dir
    return config_dir()


def _setup_logging() -> None:
    """A windowed app has no console, so errors go to a log file next to the settings."""
    try:
        os.makedirs(_config_dir(), exist_ok=True)
        logging.basicConfig(filename=os.path.join(_config_dir(), "app.log"), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
    except OSError:
        logging.basicConfig(level=logging.INFO)


def _message(text: str) -> None:
    if sys.platform.startswith("win"):
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x40)   # MB_ICONINFORMATION
    else:
        print(text, file=sys.stderr)


class SingleInstance:
    """Only one copy may talk to the mouse at a time."""

    def __init__(self) -> None:
        self.ok = True
        self._handle = None
        if sys.platform.startswith("win"):
            import ctypes
            self._handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\BLAZEControl.blazectl")
            self.ok = ctypes.windll.kernel32.GetLastError() != 183            # ERROR_ALREADY_EXISTS
        else:
            import fcntl
            os.makedirs(_config_dir(), exist_ok=True)
            self._handle = open(os.path.join(_config_dir(), "app.lock"), "w")
            try:
                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.ok = False


def _start_server():
    from .webui import make_server
    server, token = make_server()
    threading.Thread(target=server.serve_forever, daemon=True, name="blazectl-ui").start()
    return server, token, f"http://127.0.0.1:{server.server_address[1]}/"


def _report(text: str, out: str | None) -> None:
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    if sys.stdout is not None:          # None inside a windowed (no console) exe
        print(text)


def selftest(out: str | None = None) -> int:
    """Start the UI server, fetch the page and the state, check the bundled modules. No window."""
    server, token, url = _start_server()
    try:
        page = urllib.request.urlopen(url, timeout=10).read().decode()
        assert "BLAZE" in page and token in page, "page didn't render"
        req = urllib.request.Request(url + "api/state", data=b"{}", headers={
            "X-Blazectl-Token": token, "Content-Type": "application/json"})
        state = json.loads(urllib.request.urlopen(req, timeout=20).read())
        assert "connected" in state, "state API failed"
        mods = {}
        for name in ("webview", "hid"):
            try:
                __import__(name)
                mods[name] = "ok"
            except Exception as e:   # report, don't hide
                mods[name] = f"missing ({e})"
        report = (f"{APP_NAME} {__version__} selftest: page ok, api ok, mouse connected={state['connected']}, "
                  + ", ".join(f"{k} {v}" for k, v in mods.items()))
        ok = all(v == "ok" for v in mods.values()) or not getattr(sys, "frozen", False)
        _report(report, out)
        return 0 if ok else 1
    except Exception as e:
        _report(f"{APP_NAME} {__version__} selftest FAILED: {e!r}", out)
        return 1
    finally:
        server.shutdown()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        i = argv.index("--selftest")
        return selftest(argv[i + 1] if i + 1 < len(argv) else None)
    if "--version" in argv:
        _report(f"{APP_NAME} {__version__}  (blazectl) Copyright (C) 2026 Pranjal Tripathi, GPL-3.0-or-later", None)
        return 0
    _setup_logging()
    lock = SingleInstance()
    if not lock.ok:
        _message(f"{APP_NAME} is already running.")
        return 0
    try:
        server, _, url = _start_server()
    except Exception as e:
        _log.exception("could not start the UI server")
        _message(f"{APP_NAME} couldn't start: {e}")
        return 1
    _log.info("%s %s started at %s", APP_NAME, __version__, url)
    try:
        import webview
    except ImportError:
        _log.warning("pywebview not installed; opening a browser app window instead")
        from .webui import _open_window
        _open_window(url)
        try:
            threading.Event().wait()            # keep serving until the process is closed
        except KeyboardInterrupt:
            pass
        return 0
    try:
        webview.create_window(f"{APP_NAME}  ·  v{__version__}", url, width=1240, height=800,
                              min_size=(1000, 640), background_color="#0b0d12", text_select=False)
        webview.start()
    except Exception as e:
        _log.exception("window failed")
        _message(f"{APP_NAME} couldn't open its window ({e}).\n\nOn Windows this needs the Microsoft Edge "
                 "WebView2 Runtime, which is built into Windows 11 and most Windows 10 PCs.")
        return 1
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
