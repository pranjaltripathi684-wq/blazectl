# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""A simulated Blaze Ultra wired into MouseService, for tests and UI screenshots."""

from blazectl import service as svcmod
from blazectl.hidraw import HidrawNode
from test_instant import CONTROL_DESC, REAL_BUTTONS, FakeInstantMouse


def make_service(tmp_path, fake=None, present=True):
    if fake is None:
        fake = FakeInstantMouse()
        fake.mem[0x08:0x10] = REAL_BUTTONS
    fake.mem.extend(bytes(max(0, 0x100 - len(fake.mem))))  # settings area like the real mouse
    if len(fake.mem) < 0x1000:                       # macro area + erased flash
        fake.mem[0x6E:0x7B] = bytes.fromhex("00040804100418044071002004")
        fake.mem.extend(bytes(0x460 - len(fake.mem)) + b"\xff" * (0x1000 - 0x460))
    node = HidrawNode("hid6", "hid6", 0, 0x30FA, 0x1440, "INSTANT USB Gaming Mouse", 1, CONTROL_DESC)

    class Ctx:
        def __enter__(self):
            return fake

        def __exit__(self, *a):
            pass

    svc = svcmod.MouseService(
        nodes_fn=lambda: [node] if present else [],
        device_fn=lambda n: Ctx(),
        settings_path=str(tmp_path / "cfg" / "lighting.json"),
        sleep=lambda _: None,
    )
    return svc, fake
