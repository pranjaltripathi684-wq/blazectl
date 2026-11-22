# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tools for figuring out what the bytes mean.

The core trick: capture the official software doing ONE thing at a time
(DPI 800 vs DPI 1600, red vs green), then diff. The bytes that change are the
setting; a byte that changes alongside them is usually a checksum.
"""

from __future__ import annotations

import difflib
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import reduce

from .capture import HidTransfer


def pick_device(transfers: list[HidTransfer]) -> tuple[int, int] | None:
    """The device receiving the most host->device HID traffic is almost always the mouse."""
    c = Counter((t.bus, t.device) for t in transfers if t.host_to_device)
    return c.most_common(1)[0][0] if c else None


def device_table(transfers: list[HidTransfer]) -> list[tuple[tuple[int, int], int, int]]:
    out_c = Counter((t.bus, t.device) for t in transfers if t.host_to_device)
    all_c = Counter((t.bus, t.device) for t in transfers)
    return sorted(((d, out_c[d], all_c[d]) for d in all_c), key=lambda r: -r[1])


@dataclass
class CommandGroup:
    signature: str
    count: int
    lengths: set[int]
    variants: list[bytes]


def group_commands(transfers: list[HidTransfer], prefix_len: int = 2) -> list[CommandGroup]:
    """Cluster host->device packets by channel + first bytes (usually report ID + command ID)."""
    groups: dict[str, list[bytes]] = defaultdict(list)
    for t in transfers:
        if not t.host_to_device:
            continue
        sig = f"{t.label()} prefix={t.data[:prefix_len].hex()}"
        groups[sig].append(t.data)
    result = []
    for sig, datas in groups.items():
        uniq = list(dict.fromkeys(datas))
        result.append(CommandGroup(sig, len(datas), {len(d) for d in datas}, uniq))
    return sorted(result, key=lambda g: -g.count)


def byte_diff(a: bytes, b: bytes) -> list[tuple[int, int | None, int | None]]:
    out = []
    for i in range(max(len(a), len(b))):
        x = a[i] if i < len(a) else None
        y = b[i] if i < len(b) else None
        if x != y:
            out.append((i, x, y))
    return out


@dataclass
class DiffEntry:
    op: str  # "changed" | "only_a" | "only_b"
    a: HidTransfer | None
    b: HidTransfer | None
    bytes_changed: list[tuple[int, int | None, int | None]]


def diff_captures(a: list[HidTransfer], b: list[HidTransfer]) -> list[DiffEntry]:
    """Align two captures' host->device packets and report what differs."""
    a = [t for t in a if t.host_to_device]
    b = [t for t in b if t.host_to_device]
    keys_a = [(t.label(), t.data) for t in a]
    keys_b = [(t.label(), t.data) for t in b]
    sm = difflib.SequenceMatcher(a=keys_a, b=keys_b, autojunk=False)
    entries: list[DiffEntry] = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        pairs = min(i2 - i1, j2 - j1) if op == "replace" else 0
        for k in range(pairs):
            ta, tb = a[i1 + k], b[j1 + k]
            entries.append(DiffEntry("changed", ta, tb, byte_diff(ta.data, tb.data)))
        for ta in a[i1 + pairs : i2]:
            entries.append(DiffEntry("only_a", ta, None, []))
        for tb in b[j1 + pairs : j2]:
            entries.append(DiffEntry("only_b", None, tb, []))
    return entries


@dataclass
class ChecksumGuess:
    algorithm: str  # "K - sum" | "sum + K" | "xor ^ K"
    start: int
    position: int
    constant: int
    support: int  # number of distinct packets that agree

    def describe(self) -> str:
        rng = f"bytes[{self.start}:{self.position}]"
        k = f"0x{self.constant:02X}"
        if self.algorithm == "K - sum":
            expr = f"({k} - sum({rng})) & 0xFF"
        elif self.algorithm == "sum + K":
            expr = f"(sum({rng}) + {k}) & 0xFF"
        else:
            expr = f"xor({rng}) ^ {k}"
        return f"byte[{self.position}] = {expr}   (agrees on {self.support} distinct packets)"

    def compute(self, data: bytes) -> int:
        body = data[self.start : self.position]
        if self.algorithm == "K - sum":
            return (self.constant - sum(body)) & 0xFF
        if self.algorithm == "sum + K":
            return (sum(body) + self.constant) & 0xFF
        return reduce(lambda x, y: x ^ y, body, 0) ^ self.constant


def guess_checksums(payloads: list[bytes], min_support: int = 3, max_start: int = 3) -> list[ChecksumGuess]:
    """Find single-byte checksums consistent across all given packets.

    Feed it packets from ONE channel (same report ID). A guess is only reported
    when the covered bytes actually vary between packets, so constant padding
    can't produce false positives.
    """
    distinct = list(dict.fromkeys(bytes(p) for p in payloads if len(p) >= 2))
    if len(distinct) < min_support:
        return []
    length = min(len(p) for p in distinct)
    guesses = []
    for pos in range(1, length):
        for start in range(0, min(pos, max_start)):
            sums = [sum(p[start:pos]) & 0xFF for p in distinct]
            xors = [reduce(lambda x, y: x ^ y, p[start:pos], 0) for p in distinct]
            if len(set(sums)) > 1:
                ks = {(p[pos] + s) & 0xFF for p, s in zip(distinct, sums)}
                if len(ks) == 1:
                    guesses.append(ChecksumGuess("K - sum", start, pos, ks.pop(), len(distinct)))
                ks = {(p[pos] - s) & 0xFF for p, s in zip(distinct, sums)}
                if len(ks) == 1:
                    guesses.append(ChecksumGuess("sum + K", start, pos, ks.pop(), len(distinct)))
            if len(set(xors)) > 1:
                ks = {p[pos] ^ x for p, x in zip(distinct, xors)}
                if len(ks) == 1:
                    guesses.append(ChecksumGuess("xor ^ K", start, pos, ks.pop(), len(distinct)))
    # Prefer checksums that cover the most bytes (latest position, earliest start).
    guesses.sort(key=lambda g: (-g.position, g.start))
    return guesses


def hexdump_row(data: bytes, highlight: set[int] | None = None) -> str:
    highlight = highlight or set()
    return " ".join(f"[{b:02x}]" if i in highlight else f"{b:02x}" for i, b in enumerate(data))
