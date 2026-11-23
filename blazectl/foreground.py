# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which app is in front? Used by the agent to switch profiles per app.

Windows: Win32 API through ctypes (no extra packages, no admin).
Linux:   X11 through the `xprop` tool (Kali/Xfce, GNOME on Xorg, KDE on X11).
         Wayland doesn't let normal programs see the focused window, so per-app
         switching isn't available there.
"""
from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class AppInfo:
    exe: str            # "valorant.exe", "firefox" (lower case, file name only)
    title: str = ""     # window title, for rules like title:*YouTube*
    pid: int = 0


def _windows() -> AppInfo | None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    title = buf.value
    exe = ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)       # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                exe = os.path.basename(path.value)
        finally:
            kernel32.CloseHandle(h)
    # Elevated/protected processes (e.g. some anti-cheat launchers) can't be opened by a normal user:
    # we still get the title, so title rules keep working.
    return AppInfo(exe.lower(), title, pid.value)


def _xprop(*args: str) -> str:
    return subprocess.run(["xprop", *args], capture_output=True, text=True, timeout=2).stdout


def parse_xprop_active(out: str) -> str | None:
    """'_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007' -> '0x3a00007'"""
    if "#" not in out:
        return None
    wid = out.rsplit("#", 1)[1].split(",")[0].strip()
    return wid if wid and wid != "0x0" else None


def parse_xprop_window(out: str) -> tuple[int, str, str]:
    """-> (pid, wm_class instance, title) from `xprop -id W _NET_WM_PID WM_CLASS _NET_WM_NAME`."""
    pid, klass, title = 0, "", ""
    for line in out.splitlines():
        if line.startswith("_NET_WM_PID") and "=" in line:
            try:
                pid = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("WM_CLASS") and "=" in line:
            klass = line.split("=", 1)[1].split(",")[0].strip().strip('"')
        elif line.startswith("_NET_WM_NAME") and "=" in line:
            title = line.split("=", 1)[1].strip().strip('"')
    return pid, klass, title


def _linux() -> AppInfo | None:
    if os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        raise UnsupportedDesktop("Wayland session: the focused app can't be seen, so per-app profiles "
                                 "need an Xorg/X11 session (Kali's default Xfce works).")
    if not shutil.which("xprop"):
        raise UnsupportedDesktop("needs the `xprop` tool (Kali/Debian: package x11-utils)")
    wid = parse_xprop_active(_xprop("-root", "_NET_ACTIVE_WINDOW"))
    if not wid:
        return None
    pid, klass, title = parse_xprop_window(_xprop("-id", wid, "_NET_WM_PID", "WM_CLASS", "_NET_WM_NAME"))
    exe = ""
    if pid:
        try:
            exe = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
        except OSError:
            try:
                with open(f"/proc/{pid}/comm") as f:
                    exe = f.read().strip()
            except OSError:
                pass
    return AppInfo((exe or klass).lower(), title, pid)


class UnsupportedDesktop(RuntimeError):
    pass


def foreground_app() -> AppInfo | None:
    """The app the user is looking at, or None (desktop, lock screen, nothing focused)."""
    if sys.platform.startswith("win"):
        return _windows()
    if sys.platform.startswith("linux"):
        return _linux()
    raise UnsupportedDesktop(f"per-app profiles aren't supported on {sys.platform}")


def rule_matches(pattern: str, app: AppInfo) -> bool:
    """'valorant.exe', 'cs2*', 'title:*YouTube*' (case-insensitive, * and ? wildcards).

    A bare name also matches without '.exe', so 'code' matches code.exe on Windows and code on Linux.
    """
    p = pattern.strip().lower()
    if not p:
        return False
    if p.startswith("title:"):
        return fnmatch.fnmatchcase(app.title.lower(), p[6:])
    exe = app.exe.lower()
    stem = exe[:-4] if exe.endswith(".exe") else exe
    return fnmatch.fnmatchcase(exe, p) or fnmatch.fnmatchcase(stem, p)
