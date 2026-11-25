# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-Python Linux hidraw transport (no third-party dependencies).

Listing devices only reads sysfs, which is world-readable, so `blazectl list`
and `blazectl probe` work without root. Opening /dev/hidrawN for writing needs
permission: install the udev rule in udev/ once (see README).
"""

from __future__ import annotations

import errno
import glob
import os
import re
import select
from dataclasses import dataclass

from . import descriptor

try:
    import fcntl  # Linux/Unix only; Windows uses winhid.py instead
except ImportError:  # pragma: no cover
    fcntl = None

SYSFS_HIDRAW = "/sys/class/hidraw"

_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


def HIDIOCSFEATURE(n: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, 0x06, n)


def HIDIOCGFEATURE(n: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, 0x07, n)


def HIDIOCGINPUT(n: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, 0x0A, n)


PERMISSION_HINT = (
    "Permission denied opening {dev}. Raw HID writes need access to the device node.\n"
    "Install the udev rule once (udev/99-evofox-blaze.rules, see README), then replug the mouse."
)


@dataclass
class HidrawNode:
    name: str
    devnode: str
    bus: int
    vid: int
    pid: int
    hid_name: str
    interface: int | None
    report_descriptor: bytes

    backend = "hidraw"

    @property
    def summary(self) -> descriptor.DescriptorSummary:
        return descriptor.parse(self.report_descriptor)

    @property
    def is_vendor(self) -> bool:
        return self.summary.has_vendor_interface

    @property
    def accessible(self) -> bool:
        return os.access(self.devnode, os.R_OK | os.W_OK)

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"


def _read(path: str, mode: str = "r"):
    try:
        with open(path, mode) as f:
            return f.read()
    except OSError:
        return b"" if "b" in mode else ""


def _num(name: str) -> int:
    m = re.search(r"(\d+)$", name)
    return int(m.group(1)) if m else 0


def enumerate_nodes(sysfs_root: str = SYSFS_HIDRAW, dev_root: str = "/dev", vid: int | None = None) -> list[HidrawNode]:
    nodes = []
    for path in sorted(glob.glob(os.path.join(sysfs_root, "hidraw*")), key=_num):
        name = os.path.basename(path)
        devdir = os.path.join(path, "device")
        uevent = _read(os.path.join(devdir, "uevent"))
        fields = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        bus = pid = 0
        if "HID_ID" in fields:
            b, v, p = fields["HID_ID"].split(":")
            bus, dvid, pid = int(b, 16), int(v, 16), int(p, 16)
        else:
            dvid = 0
        if vid is not None and dvid != vid:
            continue
        real = os.path.realpath(devdir)
        iface_matches = re.findall(r"\d+-[\d.]+:\d+\.(\d+)", real)
        nodes.append(
            HidrawNode(
                name=name,
                devnode=os.path.join(dev_root, name),
                bus=bus,
                vid=dvid,
                pid=pid,
                hid_name=fields.get("HID_NAME", ""),
                interface=int(iface_matches[-1]) if iface_matches else None,
                report_descriptor=_read(os.path.join(devdir, "report_descriptor"), "rb"),
            )
        )
    return nodes


def find_nodes(vid: int, pid: int, **kw) -> list[HidrawNode]:
    return [n for n in enumerate_nodes(**kw) if n.vid == vid and n.pid == pid]


class HidrawDevice:
    """Thin wrapper over one /dev/hidrawN node."""

    def __init__(self, node: HidrawNode):
        self.node = node
        self.fd: int | None = None

    def open(self) -> "HidrawDevice":
        try:
            self.fd = os.open(self.node.devnode, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as e:
            raise PermissionError(PERMISSION_HINT.format(dev=self.node.devnode)) from e
        return self

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _need_fd(self) -> int:
        if self.fd is None:
            raise RuntimeError("device not open")
        return self.fd

    def set_feature(self, data: bytes) -> int:
        """data[0] must be the report ID (0 if the device uses none)."""
        buf = bytearray(data)
        n = fcntl.ioctl(self._need_fd(), HIDIOCSFEATURE(len(buf)), buf, True)
        if n < 0:
            raise OSError(errno.EIO, "SET_FEATURE failed")
        return n

    def get_feature(self, report_id: int, length: int) -> bytes:
        """length includes the report-ID byte."""
        buf = bytearray(length)
        buf[0] = report_id
        n = fcntl.ioctl(self._need_fd(), HIDIOCGFEATURE(length), buf, True)
        return bytes(buf[:n])

    def write_output(self, data: bytes) -> int:
        """Output report; data[0] must be the report ID (0 if none)."""
        return os.write(self._need_fd(), bytes(data))

    def read_input(self, timeout: float = 0.5, size: int = 64) -> bytes | None:
        fd = self._need_fd()
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            return None
        try:
            return os.read(fd, size)
        except BlockingIOError:
            return None
