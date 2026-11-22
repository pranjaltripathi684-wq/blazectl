# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows backend, tested with a fake `hid` module so it runs on any OS."""

import subprocess
import sys
import os

import pytest

from blazectl import winhid
from blazectl.replay import choose_node
from test_descriptor_and_hidraw import MOUSE, VENDOR


class FakeHidDevice:
    def __init__(self, registry):
        self.registry = registry
        self.path = None
        self.sent = []

    def open_path(self, path):
        info = self.registry[path]
        if info.get("locked"):
            raise OSError("open failed")
        self.path = path

    def close(self):
        self.path = None

    def get_report_descriptor(self):
        return list(self.registry[self.path]["desc"])

    def send_feature_report(self, data):
        self.sent.append(("feature", bytes(data)))
        self.registry[self.path]["features"][data[0]] = bytes(data)
        return len(data)

    def get_feature_report(self, rid, length):
        return list(self.registry[self.path]["features"].get(rid, bytes([rid]) + b"\0" * (length - 1)))[:length]

    def write(self, data):
        if self.registry[self.path].get("write_fails"):
            raise OSError("write error")
        self.sent.append(("output", bytes(data)))
        return len(data)

    def read(self, n, timeout):
        return []


class FakeHid:
    """Mimics one Windows mouse: a locked mouse collection + an open vendor collection, same interface."""

    def __init__(self):
        self.registry = {
            b"\\\\?\\hid#vid_30fa&pid_1701&mi_00#col01": {"locked": True, "desc": MOUSE},
            b"\\\\?\\hid#vid_30fa&pid_1701&mi_01#col02": {"locked": True, "desc": b""},
            b"\\\\?\\hid#vid_30fa&pid_1701&mi_01#col03": {"desc": VENDOR, "features": {}},
        }
        self.last = None

    def enumerate(self):
        out = []
        for path, page, usage, iface in (
            (b"\\\\?\\hid#vid_30fa&pid_1701&mi_00#col01", 0x01, 0x02, 0),
            (b"\\\\?\\hid#vid_30fa&pid_1701&mi_01#col02", 0x01, 0x06, 1),
            (b"\\\\?\\hid#vid_30fa&pid_1701&mi_01#col03", 0xFF02, 0x02, 1),
        ):
            out.append({"path": path, "vendor_id": 0x30FA, "product_id": 0x1701, "usage_page": page,
                        "usage": usage, "interface_number": iface,
                        "manufacturer_string": "EvoFox", "product_string": "Blaze Ultra"})
        return out

    def device(self):
        self.last = FakeHidDevice(self.registry)
        return self.last


def test_enumerate_marks_vendor_collection():
    nodes = winhid.enumerate_nodes(FakeHid())
    assert [n.is_vendor for n in nodes] == [False, False, True]
    assert nodes[2].summary.find("feature", 8).size_bytes == 16  # descriptor read via hidapi
    assert nodes[0].report_descriptor == b""  # locked by Windows -> no descriptor, no crash
    assert nodes[2].hid_name == "EvoFox Blaze Ultra" and nodes[2].usb_id == "30fa:1701"
    assert nodes[2].accessible is None


def test_replay_picks_vendor_collection_not_keyboard():
    nodes = winhid.enumerate_nodes(FakeHid())
    step = {"kind": "SET_REPORT", "report_type": "feature", "report_id": 8, "interface": 1, "data": "08"}
    assert choose_node(step, nodes) is nodes[2]


def test_device_io_and_errors():
    fake = FakeHid()
    nodes = winhid.enumerate_nodes(fake)
    with winhid.HidapiDevice(nodes[2], fake) as dev:
        pkt = bytes([8]) + bytes(range(16))
        assert dev.set_feature(pkt) == 17
        assert dev.get_feature(8, 17) == pkt
        fake.registry[nodes[2].raw_path]["write_fails"] = True
        with pytest.raises(OSError, match="write failed"):
            dev.write_output(b"\x00\x01")
    with pytest.raises(PermissionError, match="VENDOR CONFIG IF"):
        winhid.HidapiDevice(nodes[0], fake).open()


def test_missing_hidapi_gives_install_hint(monkeypatch):
    monkeypatch.setattr(winhid, "hid", None)
    with pytest.raises(RuntimeError, match="pip install hidapi"):
        winhid.enumerate_nodes()


def test_cli_runs_without_fcntl(tmp_path):
    """Simulate Windows: block the Linux-only fcntl module, the analysis commands must still work."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; sys.modules['fcntl'] = None\n"
        f"sys.path[:0] = [{root!r}, {os.path.join(root, 'tests')!r}]\n"
        "from fakes import capture_dpi\n"
        f"capture_dpi({str(tmp_path / 'a.pcapng')!r}, 0x0F); capture_dpi({str(tmp_path / 'b.pcapng')!r}, 0x2F)\n"
        "from blazectl.cli import main\n"
        f"main(['diff', {str(tmp_path / 'a.pcapng')!r}, {str(tmp_path / 'b.pcapng')!r}])\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "changed offsets [4, 16]" in r.stdout
