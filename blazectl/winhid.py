# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Windows (and macOS) transport using the `hidapi` package.

Install once, no admin needed:   py -m pip install hidapi

On Windows every HID "top-level collection" appears as its own device path.
Windows locks the normal mouse and keyboard collections, but the
vendor-defined collection (usage page 0xFF00+) that the configuration software
uses can be opened by a normal user. That's the one blazectl talks to.
"""

from __future__ import annotations

from dataclasses import dataclass

from .hidraw import HidrawNode

try:
    import hid  # pip package "hidapi"
except ImportError:  # pragma: no cover - depends on the machine
    hid = None

INSTALL_HINT = "Talking to the mouse on Windows needs the hidapi package. Install it with:  py -m pip install hidapi"


@dataclass
class HidapiNode(HidrawNode):
    usage_page: int = 0
    usage: int = 0
    raw_path: bytes = b""

    backend = "hidapi"

    @property
    def accessible(self) -> bool | None:
        return None  # only known after trying to open it

    @property
    def is_vendor(self) -> bool:
        return self.usage_page >= 0xFF00 or super().is_vendor


def _hid(module):
    h = module or hid
    if h is None:
        raise RuntimeError(INSTALL_HINT)
    return h


def enumerate_nodes(hid_module=None) -> list[HidapiNode]:
    h = _hid(hid_module)
    nodes = []
    for i, d in enumerate(h.enumerate()):
        raw = d.get("path") or b""
        raw = raw if isinstance(raw, bytes) else str(raw).encode()
        desc = b""
        try:  # hidapi >= 0.14 can rebuild the report descriptor
            dev = h.device()
            dev.open_path(raw)
            try:
                desc = bytes(dev.get_report_descriptor())
            finally:
                dev.close()
        except Exception:
            pass  # locked (system mouse/keyboard) or old hidapi: fall back to usage page only
        name = " ".join(x for x in (d.get("manufacturer_string"), d.get("product_string")) if x)
        iface = d.get("interface_number")
        nodes.append(
            HidapiNode(
                name=f"hid{i}",
                devnode=raw.decode(errors="replace"),
                bus=0,
                vid=d.get("vendor_id", 0),
                pid=d.get("product_id", 0),
                hid_name=name,
                interface=iface if iface is not None and iface >= 0 else None,
                report_descriptor=desc,
                usage_page=d.get("usage_page", 0) or 0,
                usage=d.get("usage", 0) or 0,
                raw_path=raw,
            )
        )
    return nodes


class HidapiDevice:
    """Same interface as hidraw.HidrawDevice, backed by hidapi."""

    def __init__(self, node: HidapiNode, hid_module=None):
        self.node = node
        self._h = hid_module
        self.dev = None

    def open(self) -> "HidapiDevice":
        h = _hid(self._h)
        d = h.device()
        try:
            d.open_path(self.node.raw_path)
        except (OSError, ValueError) as e:
            raise PermissionError(
                f"Could not open {self.node.name} ({self.node.hid_name}): {e}\n"
                "Windows locks the normal mouse/keyboard interfaces; use the one marked VENDOR CONFIG IF "
                "in `blazectl list`. Also close the EvoFox software, which may be holding the device."
            ) from e
        self.dev = d
        return self

    def close(self) -> None:
        if self.dev is not None:
            self.dev.close()
            self.dev = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _call(self, what, fn, *args):
        if self.dev is None:
            raise RuntimeError("device not open")
        try:
            result = fn(*args)
        except (OSError, ValueError) as e:
            raise OSError(f"{what} failed: {e}") from e
        if isinstance(result, int) and result < 0:
            raise OSError(f"{what} failed (hidapi returned {result})")
        return result

    def set_feature(self, data: bytes) -> int:
        return self._call("SET_FEATURE", lambda: self.dev.send_feature_report(bytes(data)))

    def get_feature(self, report_id: int, length: int) -> bytes:
        return bytes(self._call("GET_FEATURE", lambda: self.dev.get_feature_report(report_id, length)))

    def write_output(self, data: bytes) -> int:
        return self._call("write", lambda: self.dev.write(bytes(data)))

    def read_input(self, timeout: float = 0.5, size: int = 64) -> bytes | None:
        r = self._call("read", lambda: self.dev.read(size, int(timeout * 1000)))
        return bytes(r) if r else None
