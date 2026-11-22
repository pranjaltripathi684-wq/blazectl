#!/bin/sh
# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
# One-time Linux setup for blazectl: installs a udev rule so your normal user
# can talk to the mouse. This is the ONLY step that needs root; after it,
# blazectl and its GUI run without sudo.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
RULE_SRC="$HERE/../udev/99-evofox-blaze.rules"
RULE_DST=/etc/udev/rules.d/99-evofox-blaze.rules

echo "Installing $RULE_DST (one line: gives your desktop user access to the 30fa:1440 mouse)."
echo "Contents:"
grep -v '^#' "$RULE_SRC"
sudo install -m 0644 "$RULE_SRC" "$RULE_DST"
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw
echo
echo "Done. Unplug and replug the mouse, then run:  python3 -m blazectl list"
echo "The mouse's line should say 'rw' instead of 'no-access'."
echo "To undo:  sudo rm $RULE_DST && sudo udevadm control --reload-rules"
