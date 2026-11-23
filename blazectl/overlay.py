# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""On-screen popups for the agent: "DPI 1600 · level 3", "Profile: Game I", "ALERT · HIGH ...".

TkOverlay draws a small always-on-top box that never takes keyboard focus and lets clicks pass
through (on Windows), then fades out. Without tkinter it falls back to desktop notifications
(Linux `notify-send`) or the log.

Note: games in *exclusive* fullscreen draw over every window, so the popup only shows on top of
games running in borderless/windowed mode.
"""
from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
from dataclasses import dataclass

_log = logging.getLogger("blazectl.overlay")


@dataclass
class Popup:
    kind: str          # "dpi" | "profile" | "alert" | "info"
    caption: str       # small line, e.g. "DPI · LEVEL 3"
    value: str         # big line, e.g. "1600"
    color: str = "#35d6ff"
    seconds: float = 1.2


def dpi_popup(level: int, dpi: int | None, color: str, seconds: float = 1.2) -> Popup:
    return Popup("dpi", f"DPI · LEVEL {level}", f"{dpi}" if dpi else "?", color, seconds)


def profile_popup(label: str, changed: list[str], seconds: float = 1.6) -> Popup:
    what = ", ".join(changed) if changed else "lighting only"
    return Popup("profile", f"PROFILE · {what.upper()}", label, "#35d6ff", seconds)


SEVERITY_COLORS = {"critical": "#ff1a2e", "high": "#ff5a1f", "medium": "#ffd21f", "low": "#35a6ff", "info": "#35d6ff"}


def alert_popup(severity: str, title: str, seconds: float = 4.0) -> Popup:
    t = title if len(title) <= 46 else title[:45] + "…"
    return Popup("alert", f"⚠ ALERT · {severity.upper()}", t, SEVERITY_COLORS.get(severity, "#ff1a2e"), seconds)


class NullOverlay:
    """Collects popups (tests) or just logs them."""

    def __init__(self):
        self.shown: list[Popup] = []

    def show(self, p: Popup) -> None:
        self.shown.append(p)
        _log.info("popup: %s %s", p.caption, p.value)

    def run(self, stop_event, tick=None) -> None:
        while not stop_event.wait(0.2):
            if tick:
                tick()


class NotifyOverlay(NullOverlay):
    """Linux without tkinter: one replacing desktop notification."""

    def show(self, p: Popup) -> None:
        super().show(p)
        if shutil.which("notify-send"):
            try:
                subprocess.run(["notify-send", "-a", "BLAZE", "-t", str(int(p.seconds * 1000)),
                                "-h", "string:x-canonical-private-synchronous:blazectl", p.value, p.caption],
                               timeout=3, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                pass


class TkOverlay:
    """Must be created and run on the main thread (tkinter's rule); show() is thread-safe."""

    W, H = 300, 92

    def __init__(self, position: str = "bottom"):
        import tkinter as tk

        if sys.platform.startswith("win"):
            try:                                            # crisp text on high-DPI screens
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        self.tk = tk
        self.position = position
        self.q: queue.Queue[Popup] = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = None
        self._hide_job = None
        self._fade_job = None
        self.win32 = sys.platform.startswith("win")
        # Windows: create the window now (at agent start, not mid-game), keep it mapped but fully transparent
        # and click-through, and only change its opacity. Showing/hiding a window could take focus from a game.
        self._make_window()
        if self.win32:
            self._set_alpha(0.0)
        else:
            self.win.withdraw()

    def show(self, p: Popup) -> None:
        self.q.put(p)

    # -- main thread only below --------------------------------------------------
    def _make_window(self):
        tk = self.tk
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg="#0d1016", highlightthickness=2, highlightbackground="#35d6ff")
        self.bar = tk.Frame(w, width=6, bg="#35d6ff")
        self.bar.pack(side="left", fill="y")
        box = tk.Frame(w, bg="#0d1016")
        box.pack(side="left", fill="both", expand=True, padx=14, pady=8)
        font = self.family = "Segoe UI" if sys.platform.startswith("win") else "DejaVu Sans"
        self.cap = tk.Label(box, text="", fg="#8b93a5", bg="#0d1016", font=(font, 10, "bold"), anchor="w")
        self.cap.pack(fill="x")
        self.val = tk.Label(box, text="", fg="#ffffff", bg="#0d1016", font=(font, 26, "bold italic"), anchor="w")
        self.val.pack(fill="x")
        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        x = (sw - self.W) // 2
        y = 70 if self.position == "top" else sh - self.H - 130
        w.geometry(f"{self.W}x{self.H}+{x}+{y}")
        w.update_idletasks()
        self.win = w
        if sys.platform.startswith("win"):
            self.hwnd = self._no_activate(w)

    @staticmethod
    def _no_activate(w):
        """Never steal focus from the game, hide from Alt+Tab, let clicks pass through. Returns the HWND."""
        try:
            import ctypes
            hwnd = int(w.wm_frame(), 16)
            GWL_EXSTYLE, NOACTIVATE, TOOLWINDOW, TRANSPARENT, LAYERED = -20, 0x08000000, 0x80, 0x20, 0x80000
            u = ctypes.windll.user32
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, u.GetWindowLongW(hwnd, GWL_EXSTYLE) | NOACTIVATE | TOOLWINDOW
                             | TRANSPARENT | LAYERED)
            return hwnd
        except Exception as e:                              # cosmetic: the popup still works
            _log.debug("no-activate style failed: %s", e)
            return None

    def _set_alpha(self, a: float) -> bool:
        try:
            self.win.attributes("-alpha", a)
            return True
        except self.tk.TclError:
            return False

    def _raise_quietly(self) -> None:
        """Keep the popup above other windows WITHOUT activating it (Windows)."""
        if self.win32 and getattr(self, "hwnd", None):
            try:
                import ctypes
                HWND_TOPMOST, NOSIZE, NOMOVE, NOACTIVATE, SHOWWINDOW = -1, 0x1, 0x2, 0x10, 0x40
                ctypes.windll.user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                                  NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW)
                return
            except Exception:
                pass
        if not self.win32:
            self.win.deiconify()         # X11 override-redirect windows never take focus
            self.win.lift()

    def _display(self, p: Popup) -> None:
        for job in (self._hide_job, self._fade_job):
            if job:
                self.root.after_cancel(job)
        self.win.configure(highlightbackground=p.color)
        self.bar.configure(bg=p.color)
        self.cap.configure(text=p.caption)
        self.val.configure(text=p.value, font=(self.family, 26 if len(p.value) < 14 else 15, "bold italic"))
        self._raise_quietly()
        self._set_alpha(0.94)
        self._hide_job = self.root.after(int(p.seconds * 1000), self._fade, 0.94)

    def _fade(self, alpha: float) -> None:
        alpha -= 0.12
        if alpha <= 0 or not self._set_alpha(alpha):
            self._fade_job = None
            if self.win32:
                self._set_alpha(0.0)     # stays mapped, invisible and click-through
            else:
                self.win.withdraw()
            return
        self._fade_job = self.root.after(30, self._fade, alpha)

    def run(self, stop_event, tick=None) -> None:
        def pump():
            if stop_event.is_set():
                self.root.quit()
                return
            try:
                while True:
                    self._display(self.q.get_nowait())
            except queue.Empty:
                pass
            if tick:
                tick()
            self.root.after(50, pump)

        self.root.after(50, pump)
        self.root.mainloop()
        try:
            self.root.destroy()
        except Exception:
            pass


def make_overlay(enabled: bool = True, position: str = "bottom"):
    if not enabled:
        return NullOverlay()
    try:
        return TkOverlay(position)
    except Exception as e:                                  # no tkinter, or no display
        _log.info("on-screen popups unavailable (%s); using notifications/log instead", e)
        return NotifyOverlay() if sys.platform.startswith("linux") else NullOverlay()
