# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal HID report-descriptor parser.

Gaming mice usually expose several HID interfaces: a normal mouse, often a
keyboard (for macros), and one with a *vendor-defined* usage page (0xFF00 and
up). The vendor-defined reports are where the configuration software talks to
the mouse, so finding them is step one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Main item tags
_INPUT, _OUTPUT, _FEATURE, _COLLECTION, _END_COLLECTION = 0x8, 0x9, 0xB, 0xA, 0xC
REPORT_KINDS = {_INPUT: "input", _OUTPUT: "output", _FEATURE: "feature"}

USAGE_PAGE_NAMES = {
    0x01: "Generic Desktop",
    0x07: "Keyboard",
    0x08: "LED",
    0x09: "Button",
    0x0C: "Consumer",
}


def page_name(page: int) -> str:
    if page >= 0xFF00:
        return f"Vendor-defined 0x{page:04X}"
    return USAGE_PAGE_NAMES.get(page, f"0x{page:04X}")


@dataclass
class ReportInfo:
    report_id: int
    kind: str  # "input" | "output" | "feature"
    bits: int = 0
    usage_page: int = 0
    usage: int = 0

    @property
    def size_bytes(self) -> int:
        """Payload size, excluding the report-ID byte."""
        return (self.bits + 7) // 8

    @property
    def vendor_defined(self) -> bool:
        return self.usage_page >= 0xFF00


@dataclass
class DescriptorSummary:
    reports: list[ReportInfo] = field(default_factory=list)
    uses_report_ids: bool = False
    collections: list[tuple[int, int]] = field(default_factory=list)  # (page, usage)

    def find(self, kind: str, report_id: int) -> ReportInfo | None:
        for r in self.reports:
            if r.kind == kind and r.report_id == report_id:
                return r
        return None

    def wire_length(self, kind: str, report_id: int) -> int | None:
        """Buffer length for hidraw calls (payload + report-ID byte)."""
        r = self.find(kind, report_id)
        if r is None:
            return None
        return r.size_bytes + 1  # hidraw always wants a leading report-ID byte

    @property
    def vendor_reports(self) -> list[ReportInfo]:
        return [r for r in self.reports if r.vendor_defined]

    @property
    def has_vendor_interface(self) -> bool:
        return any(p >= 0xFF00 for p, _ in self.collections) or bool(self.vendor_reports)


def parse(desc: bytes) -> DescriptorSummary:
    g = {"page": 0, "size": 0, "count": 0, "id": 0}
    stack: list[dict] = []
    usages: list[int] = []
    depth = 0
    app_page = app_usage = 0
    reports: dict[tuple[str, int], ReportInfo] = {}
    summary = DescriptorSummary()

    i = 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:  # long item: 0xFE, size, tag, data...
            if i + 1 >= len(desc):
                break
            i += 3 + desc[i + 1]
            continue
        size = prefix & 0x3
        size = 4 if size == 3 else size
        typ = (prefix >> 2) & 0x3
        tag = (prefix >> 4) & 0xF
        data = desc[i + 1 : i + 1 + size]
        val = int.from_bytes(data, "little") if data else 0
        i += 1 + size

        if typ == 1:  # global
            if tag == 0:
                g["page"] = val
            elif tag == 7:
                g["size"] = val
            elif tag == 8:
                g["id"] = val
                summary.uses_report_ids = True
            elif tag == 9:
                g["count"] = val
            elif tag == 10:
                stack.append(dict(g))
            elif tag == 11 and stack:
                g = stack.pop()
        elif typ == 2:  # local
            if tag == 0:
                usages.append(val)
        elif typ == 0:  # main
            if tag == _COLLECTION:
                if depth == 0:
                    usage = usages[0] if usages else 0
                    page = g["page"]
                    if usage > 0xFFFF:  # 4-byte usage carries its own page
                        page, usage = usage >> 16, usage & 0xFFFF
                    app_page, app_usage = page, usage
                    summary.collections.append((page, usage))
                depth += 1
            elif tag == _END_COLLECTION:
                depth = max(0, depth - 1)
            elif tag in REPORT_KINDS:
                key = (REPORT_KINDS[tag], g["id"])
                r = reports.get(key)
                if r is None:
                    r = ReportInfo(g["id"], REPORT_KINDS[tag], 0, app_page, app_usage)
                    reports[key] = r
                r.bits += g["size"] * g["count"]
            usages = []

    summary.reports = sorted(reports.values(), key=lambda r: (r.report_id, r.kind))
    return summary
