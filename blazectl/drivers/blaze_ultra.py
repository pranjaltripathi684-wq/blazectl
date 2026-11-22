# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""EvoFox Blaze Ultra driver - TO BE FILLED IN FROM YOUR CAPTURES.

Nothing here is guessed. Every encode_* method raises until you've decoded the
matching packets with `blazectl analyze/diff/checksum` (README, step 4).

How to fill one in, using DPI as the example:
  1. Capture the official software setting DPI level 1 to 800, then to 1600.
  2. `blazectl diff dpi_800.pcapng dpi_1600.pcapng` shows which bytes moved.
  3. `blazectl checksum dpi_*.pcapng` tells you if a trailing byte is a checksum.
  4. Write encode_dpi_levels() to build those bytes from the Profile, and add a
     test in tests/test_blaze_ultra.py asserting it reproduces the captured packet
     byte-for-byte BEFORE you ever send it to the mouse.
"""

from __future__ import annotations

from ..protocol import Command, MouseDriver, Profile

# Fill these from `lsusb` (the line that says EvoFox / Amkette / your mouse).
VID: int | None = None
PID: int | None = None

# Fill these from `blazectl probe`: the vendor-defined report the software uses.
CONFIG_REPORT_ID: int | None = None
CONFIG_REPORT_LEN: int | None = None  # including the report-ID byte

NOT_DECODED = (
    "{what} isn't decoded yet. Capture the official software changing it (README step 3), "
    "decode with `blazectl diff` / `blazectl checksum`, then implement {fn}() in "
    "blazectl/drivers/blaze_ultra.py. Until then, `blazectl replay` can resend captured packets as-is."
)


def checksum(packet: bytes) -> int:
    """Replace with the formula `blazectl checksum` finds, e.g. (0x55 - sum(packet[0:15])) & 0xFF."""
    raise NotImplementedError(NOT_DECODED.format(what="The checksum", fn="checksum"))


class BlazeUltraDriver(MouseDriver):
    name = "EvoFox Blaze Ultra"
    vid = VID
    pid = PID

    def encode_dpi_levels(self, profile: Profile) -> list[Command]:
        raise NotImplementedError(NOT_DECODED.format(what="DPI", fn="encode_dpi_levels"))

    def encode_polling(self, profile: Profile) -> list[Command]:
        raise NotImplementedError(NOT_DECODED.format(what="Polling rate", fn="encode_polling"))

    def encode_lighting(self, profile: Profile) -> list[Command]:
        # Bug 4 (blinking after picking static): when you decode this, send the
        # mode byte AND the colour in the same write, then read it back.
        raise NotImplementedError(NOT_DECODED.format(what="Lighting", fn="encode_lighting"))

    def encode_buttons(self, profile: Profile) -> list[Command]:
        if not profile.buttons:
            return []
        raise NotImplementedError(NOT_DECODED.format(what="Button mapping", fn="encode_buttons"))

    def encode_commit(self, profile: Profile) -> list[Command]:
        # Many mice need a final "save to flash / activate profile" packet (bug 3).
        raise NotImplementedError(NOT_DECODED.format(what="The save/commit step", fn="encode_commit"))

    def encode_profile(self, profile: Profile) -> list[Command]:
        return (
            self.encode_dpi_levels(profile)
            + self.encode_polling(profile)
            + self.encode_lighting(profile)
            + self.encode_buttons(profile)
            + self.encode_commit(profile)
        )
