# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point PyInstaller freezes into "BLAZE Control.exe"."""
import sys

from blazectl.app import main

if __name__ == "__main__":
    sys.exit(main())
