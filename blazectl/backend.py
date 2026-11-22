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


def make_device(node):
    """Return an unopened device; use it as a context manager or call .open()."""
    if getattr(node, "backend", "hidraw") == "hidapi":
        from .winhid import HidapiDevice

        return HidapiDevice(node)
    from .hidraw import HidrawDevice

    return HidrawDevice(node)
