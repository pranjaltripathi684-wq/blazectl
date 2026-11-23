# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""blazectl - open-source Windows/Linux configurator for the EvoFox Blaze Ultra mouse.

The vendor protocol is undocumented, so this package ships two halves:

* a reverse-engineering toolkit (capture parsing, diffing, checksum guessing,
  safe replay) to decode what the official Windows software sends, and
* a driver framework (profiles, verify-after-write, clear errors) that the
  decoded protocol plugs into.
"""

__version__ = "0.3.1"
__author__ = "Pranjal Tripathi"
__copyright__ = "Copyright (C) 2026 Pranjal Tripathi"
__license__ = "GPL-3.0-or-later"
