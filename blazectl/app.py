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


def unblock_bundle(base: str | None = None, remove=os.remove) -> int:
    """Remove Windows' "downloaded from the internet" mark from the app's own DLLs.

    Files extracted from a downloaded ZIP carry a Zone.Identifier stream; .NET Framework then
    refuses to load pythonnet's Python.Runtime.dll, so the window can't open ("Failed to resolve
    Python.Runtime.Loader.Initialize"). This is what PowerShell's Unblock-File does, limited to
    the files inside this app's own folder. Returns how many marks were removed.
    """
    if base is None:
        if not (sys.platform.startswith("win") and getattr(sys, "frozen", False)):
            return 0
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    removed = 0
    for root, _, files in os.walk(base):
        for name in files:
            if name.lower().endswith((".dll", ".pyd", ".exe")):
                try:
                    remove(os.path.join(root, name) + ":Zone.Identifier")
                    removed += 1
                except OSError:
                    pass        # no mark (the normal case), or a read-only install folder
    return removed


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
        n = unblock_bundle()
        if n:
            _log.info("removed the downloaded-file mark from %d bundled files", n)
    except Exception:
        _log.exception("unblock failed (continuing)")
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
        text = str(e)
        if "Python.Runtime" in text or "pythonnet" in text or "clr" in text.lower():
            hint = ("Windows blocked one of the app's files because it came from a downloaded ZIP.\n\n"
                    "Fix: use the installer (BLAZE-Control-Setup.exe), or right-click the ZIP > Properties > "
                    "tick 'Unblock' > OK, then extract it again.")
        else:
            hint = ("The window needs the Microsoft Edge WebView2 Runtime, which is built into Windows 11 and "
                    "most Windows 10 PCs. You can get it from microsoft.com/edge/webview2.")
        _message(f"{APP_NAME} couldn't open its window.\n\n{hint}\n\nDetails: {text}")
        return 1
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
