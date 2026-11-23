# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Firmware analysis, READ-ONLY.

    blazectl firmware map                  sample the 64 KB address space: what's where? (~1 s)
    blazectl firmware dump --save fw.json  read all 64 KB (a few minutes)
    blazectl firmware analyze fw.json      look for code, vector tables, strings, mirrors
    blazectl firmware appscan "C:\\...\\EvoFox"   scan the official app's files (static, no mouse needed)

Only the memory READ command (07 18 05 ...) that blazectl uses all the time is sent: no writes, no
unknown commands, nothing that could put the chip into a bootloader. The 16-bit address field limits
reads to 0x0000-0xFFFF. If an address isn't backed by memory, the chip may repeat (mirror) a smaller
memory, return a constant, or stop answering until it's unplugged; replugging fixes that.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field

PAGE = 0x100


def entropy(data: bytes) -> float:
    """Shannon entropy in bits per byte (0 = constant, ~8 = random/compressed)."""
    if not data:
        return 0.0
    n = len(data)
    return -sum(c / n * math.log2(c / n) for c in Counter(data).values())


def classify(block: bytes) -> str:
    if all(b == 0xFF for b in block):
        return "erased"
    if all(b == 0x00 for b in block):
        return "zero"
    return "data"


@dataclass
class Region:
    start: int
    end: int
    kind: str          # erased | zero | data | mirror(0x....) | no-reply


@dataclass
class FirmwareReport:
    regions: list[Region] = field(default_factory=list)
    mirror_size: int | None = None
    notes: list[str] = field(default_factory=list)
    strings: list[tuple[int, str]] = field(default_factory=list)
    entropy_pages: dict[int, float] = field(default_factory=dict)

    def text(self) -> str:
        out = ["Address map (sampled or full):"]
        for r in self.regions:
            out.append(f"  0x{r.start:04x}-0x{r.end - 1:04x}  {r.kind}")
        if self.mirror_size:
            out.append(f"\nMemory repeats every 0x{self.mirror_size:x} bytes: the readable memory is probably "
                       f"{self.mirror_size // 1024 or self.mirror_size} {'KB' if self.mirror_size >= 1024 else 'bytes'} "
                       "(the address wraps around).")
        if self.notes:
            out.append("\nFindings:")
            out += [f"  - {n}" for n in self.notes]
        if self.strings:
            out.append("\nText found (address: string):")
            out += [f"  0x{a:04x}: {s}" for a, s in self.strings[:40]]
        return "\n".join(out)


def merge_regions(samples: dict[int, str], step: int, end: int) -> list[Region]:
    regions: list[Region] = []
    for a in sorted(samples):
        kind = samples[a]
        if regions and regions[-1].kind == kind and regions[-1].end == a:
            regions[-1].end = min(a + step, end)
        else:
            regions.append(Region(a, min(a + step, end), kind))
    return regions


def find_mirror(read, base: bytes, candidates=(0x400, 0x800, 0x1000, 0x2000, 0x4000, 0x8000)) -> int | None:
    """If memory at N repeats the header block at 0 (and the next block too), memory is N bytes, wrapped."""
    first, second = base[:8], base[8:16]
    if first.count(first[0]) == 8:          # a constant header can't prove anything
        return None
    for n in candidates:
        try:
            if read(n) == first and read(n + 8) == second:
                return n
        except OSError:
            return None
    return None


def map_memory(read, end: int = 0x10000, step: int = PAGE, progress=None) -> FirmwareReport:
    """Sample one 8-byte block per `step` bytes. `read(addr) -> bytes` (raises OSError if no reply)."""
    rep = FirmwareReport()
    base = read(0) + read(8)
    samples: dict[int, str] = {}
    first_dead = None
    for a in range(0, end, step):
        try:
            blk = read(a)
        except OSError as e:
            samples[a] = "no-reply"
            first_dead = first_dead if first_dead is not None else a
            rep.notes.append(f"no reply at 0x{a:04x} ({e}); stopped there. Unplug and replug the mouse if it misbehaves.")
            break
        samples[a] = classify(blk)
        if progress:
            progress(a + step, end)
    rep.mirror_size = find_mirror(read, base)
    if rep.mirror_size:
        samples = {a: (k if a < rep.mirror_size else f"mirror of 0x{a % rep.mirror_size:04x}") for a, k in samples.items()}
        samples = {a: ("mirror" if k.startswith("mirror") else k) for a, k in samples.items()}
    rep.regions = merge_regions(samples, step, end if first_dead is None else first_dead + step)
    return rep


_STR = re.compile(rb"[\x20-\x7e]{5,}")


def analyze_image(mem: bytes, base_addr: int = 0) -> FirmwareReport:
    """Look at a full read for signs of executable code."""
    rep = FirmwareReport()
    samples = {a: classify(mem[a:a + PAGE]) for a in range(0, len(mem), PAGE)}
    rep.regions = merge_regions(samples, PAGE, len(mem))
    for a in range(0, len(mem), PAGE):
        page = mem[a:a + PAGE]
        if classify(page) == "data":
            rep.entropy_pages[a] = round(entropy(page), 2)
    # wrap-around: does the image repeat?
    for n in (0x400, 0x800, 0x1000, 0x2000, 0x4000, 0x8000):
        if len(mem) >= 2 * n and mem[:n] == mem[n:2 * n] and len(set(mem[:n])) > 2:
            rep.mirror_size = n
            rep.notes.append(f"the image repeats every 0x{n:x} bytes: only 0x{n:x} bytes of real memory are readable")
            break
    # ARM Cortex-M vector table: initial SP in RAM (0x2000xxxx), reset handler odd (Thumb) in flash
    if len(mem) >= 8:
        sp = int.from_bytes(mem[0:4], "little")
        rst = int.from_bytes(mem[4:8], "little")
        if 0x20000000 <= sp < 0x20100000 and rst & 1 and rst < 0x10000000:
            rep.notes.append(f"looks like an ARM Cortex-M vector table: SP=0x{sp:08x}, reset=0x{rst:08x}")
    # 8051-family: reset vector at 0x0000 is LJMP addr16 (02 hh ll)
    if len(mem) >= 3 and mem[0] == 0x02 and int.from_bytes(mem[1:3], "big") < len(mem):
        rep.notes.append(f"starts with an 8051 LJMP to 0x{int.from_bytes(mem[1:3], 'big'):04x}: possible 8051 code")
    if mem[:3] == bytes([0x5A, 0x03, 0xA5]):
        rep.notes.append("starts with the settings header 5a 03 a5: this is the configuration memory, not firmware code")
    code_like = [a for a, e in rep.entropy_pages.items() if e >= 5.5]
    if code_like:
        rep.notes.append(f"{len(code_like)} page(s) with code-like entropy (≥5.5 bits/byte), first at 0x{code_like[0]:04x}")
    else:
        rep.notes.append("no page has code-like entropy: no firmware code is visible through this read command")
    rep.strings = [(m.start() + base_addr, m.group().decode()) for m in _STR.finditer(mem)]
    return rep


# --------------------------------------------------------------------------- official app scan

FW_EXT = (".bin", ".hex", ".fw", ".img", ".dfu", ".upd", ".rom")
MEDIA_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".ico", ".wav", ".mp3", ".ttf", ".otf")
KEYWORDS = (b"firmware", b"upgrade", b"update", b"bootloader", b"isp", b"iap", b"flash", b"dfu", b"vid_", b"pid_")
WHOLE_WORD = {b"isp", b"iap", b"dfu", b"flash"}          # short: only as whole words ("display" isn't ISP)


def _has_word(low: bytes, k: bytes) -> bool:
    if k not in WHOLE_WORD:
        return k in low or k.decode().encode("utf-16-le") in low
    pat = rb"(?<![a-z0-9_])" + re.escape(k) + rb"(?![a-z0-9_])"
    wide = low.decode("utf-16-le", errors="ignore").encode("ascii", errors="ignore")
    return bool(re.search(pat, low) or re.search(pat, wide))


@dataclass
class AppScan:
    files: int = 0
    firmware_candidates: list[str] = field(default_factory=list)
    usb_ids: dict[str, set] = field(default_factory=dict)       # "30fa:1440" -> {files}
    keywords: dict[str, set] = field(default_factory=dict)
    interesting_strings: dict[str, list[str]] = field(default_factory=dict)

    def text(self) -> str:
        out = [f"Scanned {self.files} files (read-only)."]
        if self.firmware_candidates:
            out.append("\nPossible firmware images:")
            out += [f"  {f}" for f in self.firmware_candidates]
        else:
            out.append("\nNo firmware image files found (no .bin/.hex/.fw, no large high-entropy blobs).")
        if self.usb_ids:
            out.append("\nInstant (30fa) USB IDs mentioned: these are the mice this app knows:")
            for uid, files in sorted(self.usb_ids.items()):
                out.append(f"  {uid}  in {', '.join(sorted(files))[:120]}")
        if self.keywords:
            out.append("\nUpdate-related words:")
            for k, files in sorted(self.keywords.items()):
                out.append(f"  {k:11} in {', '.join(sorted(files))[:120]}")
        for f, strs in self.interesting_strings.items():
            out.append(f"\nStrings in {f}:")
            out += [f"  {s}" for s in strs[:25]]
        return "\n".join(out)


_VIDPID = re.compile(rb"(?i)vid[_&]?30fa[^0-9a-f]{0,8}pid[_&]?([0-9a-f]{4})")
_INTERESTING = re.compile(rb"(?i)[\x20-\x7e]*(firmware|upgrade|bootloader|30fa|instant|evofox|blaze)[\x20-\x7e]*")


def scan_app_folder(path: str, max_file: int = 64 << 20) -> AppScan:
    res = AppScan()
    for root, _, files in os.walk(path):
        for name in files:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, path)
            try:
                size = os.path.getsize(p)
                if size > max_file:
                    continue
                with open(p, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            res.files += 1
            if name.lower().endswith(MEDIA_EXT):       # pictures/sounds: random bytes, not text
                continue
            low = data.lower()
            if name.lower().endswith(FW_EXT) or (32 << 10 <= size <= 512 << 10 and entropy(data[:65536]) > 7.2
                                                and not name.lower().endswith((".dll", ".exe", ".zip", ".cab"))):
                res.firmware_candidates.append(f"{rel} ({size} bytes, entropy {entropy(data[:65536]):.2f})")
            wide_text = data.decode("utf-16-le", errors="ignore").encode("ascii", errors="ignore")
            for blob in (data, wide_text):                 # ASCII, and UTF-16 (Windows device paths)
                for m in _VIDPID.finditer(blob):
                    res.usb_ids.setdefault(f"30fa:{m.group(1).decode().lower()}", set()).add(rel)
            for k in KEYWORDS:
                if _has_word(low, k):
                    res.keywords.setdefault(k.decode(), set()).add(rel)
            hits = sorted({m.group().decode(errors="replace").strip() for m in _INTERESTING.finditer(data)
                           if 6 <= len(m.group()) <= 160})
            wide = sorted({s for s in re.findall(r"[ -~]{6,160}", data.decode("utf-16-le", errors="ignore"))
                           if re.search(r"(?i)firmware|upgrade|bootloader|30fa|instant|evofox|blaze", s)})
            if hits or wide:
                res.interesting_strings[rel] = (hits + wide)[:60]
    return res
