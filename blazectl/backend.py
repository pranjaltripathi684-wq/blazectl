# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Picks the right way to talk to HID devices on this OS.

Linux   -> /dev/hidraw (built in, no extra packages)
Windows -> hidapi      (py -m pip install hidapi)
"""

from __future__ import annotations

import sys


def enumerate_nodes():
    if sys.platform.startswith("linux"):
        from .hidraw import enumerate_nodes as _enum
    else:
        from .winhid import enumerate_nodes as _enum
    return _enum()


class LockedDevice:
    """Holds the machine-wide device lock (devlock.py) while the device is open, so the GUI, the
    background agent and CLI commands never interleave packets. Everything else is passed through."""

    def __init__(self, inner):
        self._inner = inner
        self._held = False

    def open(self) -> "LockedDevice":
        from .devlock import device_lock

        lock = device_lock()
        lock.acquire()
        try:
            self._inner.open()
        except BaseException:
            lock.release()
            raise
        self._held = True
        return self

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            if self._held:
                from .devlock import device_lock

                self._held = False
                device_lock().release()

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def make_device(node):
    """Return an unopened device; use it as a context manager or call .open()."""
    if getattr(node, "backend", "hidraw") == "hidapi":
        from .winhid import HidapiDevice

        return LockedDevice(HidapiDevice(node))
    from .hidraw import HidrawDevice

    return LockedDevice(HidrawDevice(node))
