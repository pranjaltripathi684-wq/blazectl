# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Save captured packets as a 'recipe' and resend them from Linux.

This is the quickest real win: record the official software setting, say,
static red, and replay exactly those bytes. No decoding required - but only
replay packets the official software actually sent.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from .backend import make_device
from .capture import HidTransfer
from .hidraw import HidrawNode


def make_recipe(name: str, transfers: list[HidTransfer], usb_id: str | None = None) -> dict:
    steps = [t.to_dict() for t in transfers if t.kind in ("SET_REPORT", "GET_REPORT", "INTR_OUT")]
    return {"name": name, "usb_id": usb_id, "format": 1, "steps": steps}


def load_recipe(path: str) -> dict:
    with open(path) as f:
        recipe = json.load(f)
    if recipe.get("format") != 1 or "steps" not in recipe:
        raise ValueError(f"{path} is not a blazectl recipe")
    return recipe


def choose_node(step: dict, nodes: list[HidrawNode]) -> HidrawNode | None:
    """Pick the hidraw node a captured packet was meant for."""
    # On Windows one USB interface can show up as several collections, so narrow
    # by interface first, then pick the collection that actually has the report.
    iface = step.get("interface")
    candidates = [n for n in nodes if iface is not None and n.interface == iface] or list(nodes)
    kind = {"SET_REPORT": step.get("report_type"), "GET_REPORT": "feature", "INTR_OUT": "output"}.get(step["kind"])
    rid = step.get("report_id")
    for n in candidates:
        s = n.summary
        if rid is not None and s.find(kind, rid):
            return n
        if rid is None and any(r.kind == "output" for r in s.reports):
            return n
    vendor = [n for n in candidates if n.is_vendor]
    if vendor:
        return vendor[0]
    return candidates[0] if iface is not None and candidates else None


def wire_buffer(step: dict, node: HidrawNode) -> bytes:
    """hidraw always expects the report-ID byte first (0 when the device uses none)."""
    data = bytes.fromhex(step["data"])
    if step["kind"] == "INTR_OUT":
        return data if node.summary.uses_report_ids else b"\x00" + data
    rid = step.get("report_id") or 0
    if rid == 0:
        return b"\x00" + data
    return data if data[:1] == bytes([rid]) else bytes([rid]) + data


@dataclass
class ReplayLine:
    index: int
    node: str
    action: str
    data: bytes
    note: str = ""


def replay(recipe: dict, nodes: list[HidrawNode], execute: bool = False, delay: float = 0.02) -> list[ReplayLine]:
    lines: list[ReplayLine] = []
    open_devs: dict = {}
    try:
        for i, step in enumerate(recipe["steps"]):
            node = choose_node(step, nodes)
            if node is None:
                raise RuntimeError(f"step {i}: no matching hidraw interface found for {step}")
            buf = wire_buffer(step, node)
            if step["kind"] == "GET_REPORT":
                action = "get_feature"
            elif step["kind"] == "INTR_OUT" or step.get("report_type") == "output":
                action = "write_output"
            elif step.get("report_type") == "feature":
                action = "set_feature"
            else:
                lines.append(ReplayLine(i, node.name, "skip", buf, f"unsupported report type {step.get('report_type')}"))
                continue
            note = ""
            if execute:
                dev = open_devs.get(node.devnode)
                if dev is None:
                    dev = open_devs[node.devnode] = make_device(node).open()
                if action == "set_feature":
                    dev.set_feature(buf)
                elif action == "write_output":
                    dev.write_output(buf)
                else:
                    rid = step.get("report_id") or 0
                    got = dev.get_feature(rid, len(buf))
                    note = "matches capture" if got == buf else f"differs from capture: {got.hex(' ')}"
                time.sleep(delay)
            lines.append(ReplayLine(i, node.name, action, buf, note))
    finally:
        for d in open_devs.values():
            d.close()
    return lines
