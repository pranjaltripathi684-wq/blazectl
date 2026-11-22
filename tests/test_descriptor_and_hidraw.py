# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys

import pytest

from blazectl import descriptor
from blazectl.hidraw import HIDIOCGFEATURE, HIDIOCSFEATURE, enumerate_nodes

MOUSE = bytes.fromhex(
    "05 01 09 02 a1 01 85 01 09 01 a1 00 05 09 19 01 29 05 15 00 25 01 95 05 75 01 81 02"
    " 95 01 75 03 81 01 05 01 09 30 09 31 15 81 25 7f 75 08 95 02 81 06 c0 c0"
)
VENDOR = bytes.fromhex(
    "06 02 ff 09 02 a1 01 85 08 15 00 26 ff 00 75 08 95 10 09 01 b1 02"
    " 85 09 95 10 09 01 81 02 c0"
)


def test_mouse_descriptor():
    s = descriptor.parse(MOUSE)
    assert s.uses_report_ids
    assert s.collections == [(0x01, 0x02)]
    r = s.find("input", 1)
    assert r.size_bytes == 3 and not r.vendor_defined
    assert not s.has_vendor_interface


def test_vendor_descriptor():
    s = descriptor.parse(MOUSE + VENDOR)
    feat = s.find("feature", 8)
    assert feat.size_bytes == 16 and feat.vendor_defined and feat.usage_page == 0xFF02
    assert s.wire_length("feature", 8) == 17
    assert s.find("input", 9).size_bytes == 16
    assert [r.report_id for r in s.vendor_reports] == [8, 9]
    assert s.has_vendor_interface


def test_four_byte_usage_and_long_item_dont_crash():
    desc = bytes.fromhex("0b 01 00 00 ff a1 01 fe 02 00 aa bb 85 02 75 08 95 04 b1 02 c0")
    s = descriptor.parse(desc)
    assert s.collections == [(0xFF00, 0x01)]
    assert s.find("feature", 2).size_bytes == 4


def test_ioctl_numbers_match_kernel_headers():
    # values from <linux/hidraw.h> as compiled by gcc for len=17
    assert HIDIOCSFEATURE(17) == 0xC0114806
    assert HIDIOCGFEATURE(17) == 0xC0114807


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks + sysfs layout")
def test_enumerate_nodes_from_fake_sysfs(tmp_path):
    real = tmp_path / "devices/usb1/1-2/1-2:1.1/0003:30FA:1701.0005"
    real.mkdir(parents=True)
    (real / "uevent").write_text("HID_ID=0003:000030FA:00001701\nHID_NAME=Fake Gaming Mouse\n")
    (real / "report_descriptor").write_bytes(VENDOR)
    node_dir = tmp_path / "class/hidraw/hidraw4"
    node_dir.mkdir(parents=True)
    os.symlink(real, node_dir / "device")

    nodes = enumerate_nodes(sysfs_root=str(tmp_path / "class/hidraw"), dev_root=str(tmp_path / "dev"))
    assert len(nodes) == 1
    n = nodes[0]
    assert (n.vid, n.pid, n.interface, n.usb_id) == (0x30FA, 0x1701, 1, "30fa:1701")
    assert n.hid_name == "Fake Gaming Mouse"
    assert n.summary.has_vendor_interface
    assert not n.accessible  # no /dev node in the fake tree
