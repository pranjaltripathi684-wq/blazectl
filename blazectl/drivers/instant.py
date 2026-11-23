# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Instant Microelectronics mouse protocol (USB VID 0x30FA).

The EvoFox Blaze Ultra reports as 30fa:1440 "INSTANT USB Gaming Mouse". This
module implements the protocol family used by Instant mouse chips:

* Lighting: OpenRGB's InstantMouseController (GPL-2.0, constants only) and
  julianfdz/open-isy4500-customizer (MIT), verified there on 30fa:1040.
* Config memory (DPI, active level, buttons): open-isy4500-customizer, which
  took it from the official software of another mouse on the same chip.

Everything goes through ONE channel: 8-byte feature report 0x07 on the vendor
collection with usage page 0xFF01 (interface 1). On the Blaze Ultra this is
confirmed by `blazectl probe`: "feature id=0x07 7 bytes (Vendor-defined 0xFF01)".

The memory layout is only *assumed* to match 30fa:1040 until `blazectl dump`
shows the same header. DPI writes refuse to run until that check passes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

VID = 0x30FA
REPORT_ID = 0x07
PACKET_LEN = 8
CONTROL_USAGE_PAGE = 0xFF01
EVENT_USAGE_PAGE = 0xFF00

CMD_MODE = 0x13
CMD_COLOR = 0x14
CMD_MEMORY = 0x18
MEM_IDLE, MEM_WRITE, MEM_READ, MEM_COMMIT, MEM_RELOAD = 0x00, 0x03, 0x05, 0x09, 0x10

DPI_LEVELS = 6
SPEED_MAX = 5
BRIGHTNESS_MAX = 7

HEADER_MAGIC = bytes([0x5A, 0x03, 0xA5])
HEADER_BLOCK = 0x00
BUTTON_BLOCK = 0x08
DPI_BLOCK = 0x10
STATE_BLOCK = 0x18
WRITABLE_BLOCKS = (BUTTON_BLOCK, DPI_BLOCK, STATE_BLOCK)
# Macro storage (drivers/macro.py): pointer words, the per-button entry table, the macro data.
RESTORE_START, RESTORE_END = 0x008, 0x800     # restore_blocks() range: everything but the header
MACRO_BLOCKS = frozenset([0x68, 0x70, 0x78, *range(0xA0, 0xF8, 8), *range(0x400, 0x800, 8)])
# App buttons that can hold a macro, media key or shortcut. 4, 5, 6, 7 confirmed by app dumps
# (5 = the shortcut in m7_ms); 2, 3 follow the same entry spacing. 1 must stay Left Click,
# 8 lives in another block and is firmware-limited.
MACRO_BUTTONS = {2: "predicted", 3: "predicted", 4: "confirmed", 5: "confirmed", 6: "confirmed", 7: "confirmed"}
DUMP_END = 0x40

# Buttons (EvoFox Blaze Ultra, 30fa:1440), decoded 2026-11-21 by changing buttons in the
# official app and diffing memory. Each slot byte = flags (top 3 bits, preserved) | function
# (low 5 bits). Slot order follows the app's MENU order, not its button numbers.
#   (app number, name, block, byte index, factory code)
BUTTON_SLOTS = (
    (1, "left", BUTTON_BLOCK, 0, 0x01),
    (2, "right", BUTTON_BLOCK, 2, 0x03),
    (3, "middle", BUTTON_BLOCK, 1, 0x02),   # confirmed: app btn3 -> DPI Loop changed slot 1
    (4, "forward", BUTTON_BLOCK, 4, 0x05),
    (5, "back", BUTTON_BLOCK, 3, 0x04),     # confirmed: rear side button = Back in a browser
    (6, "dpi", BUTTON_BLOCK, 7, 0x06),
    (7, "led", BUTTON_BLOCK, 5, 0x15),      # confirmed: app btn7 -> Middle changed slot 5
    (8, "fire", DPI_BLOCK, 0, 0x09),        # inferred: the 9th slot holds 0x09
)
# Function codes. "confirmed" = observed in a write + physical test; others = factory
# defaults sitting on the physical button with that label.
BUTTON_FUNCTIONS = {
    "left-click": 0x01,     # factory on left button
    "middle-click": 0x02,   # confirmed: button 7 set to 0x02 closed a browser tab
    "right-click": 0x03,    # factory on right button
    "back": 0x04,           # confirmed: factory on rear side button, went Back
    "forward": 0x05,        # factory on front side button
    "dpi-loop": 0x06,       # confirmed by app diff
    "rapid-fire": 0x09,     # factory on button 8
    "led-loop": 0x15,       # confirmed by app diff
    # Extend menu, codes written by the official app (extend.json, 2026-11-22):
    "show-desktop": 0x07,   # app: button 5 -> Show Desktop  (64 -> 67)
    "double-click": 0x08,   # app: button 3 -> Double Click  (22 -> 28)
    "dpi-up": 0x0B,         # app: button 7 -> DPI +         (b5 -> ab)
    "dpi-down": 0x0C,       # app: button 6 -> DPI -         (66 -> 6c)
}
# Functions blazectl can show and keep, but not assign yet. 0x0D = macro: the official app
# wrote ad on button 7 (b5 -> ad) when a macro was bound (m_ab.json, 2026-11-22). The macro
# itself lives at 0x400 (see drivers/macro.py); writing it isn't supported yet.
KEEP_ONLY_FUNCTIONS = {"macro": 0x0D, "macro-hold": 0x0E, "media": 0x0F}  # set through set_macros()
FUNCTION_NAMES = {v: k for k, v in {**BUTTON_FUNCTIONS, **KEEP_ONLY_FUNCTIONS}.items()}


def decode_buttons(button_block: bytes, dpi_block: bytes) -> dict[int, int]:
    """App button number -> function code."""
    blocks = {BUTTON_BLOCK: button_block, DPI_BLOCK: dpi_block}
    return {num: blocks[blk][idx] & 0x1F for num, _, blk, idx, _ in BUTTON_SLOTS}


# Button 8 (0x10[0]) is confirmed: the official app writes e6 for DPI Loop and it works.
# But Backward (e4, written by the official app itself) does nothing on this button, so
# the firmware only honours some functions here. Only verified ones are allowed.
BUTTON_ALLOWED = {8: {0x09: "rapid-fire (factory)", 0x06: "dpi-loop (verified)"}}


def encode_buttons(button_block: bytes, dpi_block: bytes, changes: dict[int, int]) -> tuple[bytes, bytes]:
    """Apply {app button: function code}, keeping the top 3 flag bits of every slot."""
    for num, code in changes.items():
        allowed = BUTTON_ALLOWED.get(num)
        if allowed is not None and code not in allowed:
            raise InstantError(
                f"button {num} only supports: {', '.join(allowed.values())}. Other functions "
                "(e.g. Back) do nothing on it, even when set by the official app."
            )
    blocks = {BUTTON_BLOCK: bytearray(button_block), DPI_BLOCK: bytearray(dpi_block)}
    slot = {num: (blk, idx) for num, _, blk, idx, _ in BUTTON_SLOTS}
    for num, code in changes.items():
        if num not in slot:
            raise InstantError(f"no button {num}; buttons are 1-8")
        if code not in FUNCTION_NAMES:
            raise InstantError(f"function 0x{code:02x} is not a known function")
        blk, idx = slot[num]
        if code in KEEP_ONLY_FUNCTIONS.values() and blocks[blk][idx] & 0x1F != code:
            raise InstantError(
                f"button {num}: blazectl can keep an existing {FUNCTION_NAMES[code]} binding but can't "
                "create one yet; bind it in the official app for now."
            )
        blocks[blk][idx] = (blocks[blk][idx] & 0xE0) | code
    new_b, new_d = bytes(blocks[BUTTON_BLOCK]), bytes(blocks[DPI_BLOCK])
    if BUTTON_FUNCTIONS["left-click"] not in decode_buttons(new_b, new_d).values():
        raise InstantError("refusing: at least one button must stay Left Click")
    return new_b, new_d


# Polling rate: block 0x18 byte 6 = report interval in ms minus 1 = (1000 / rate) - 1.
# Found by diffing 1000 Hz vs 125 Hz dumps on 30fa:1440; every value confirmed with an
# online polling-rate tester after writing it with blazectl (2026-11-21):
# 1000 -> 1000 Hz, 250 -> 250 Hz, 500 -> 500 Hz, 125 -> 123 Hz.
POLL_BYTE = 6
POLL_RATES = {1000: 0x00, 500: 0x01, 250: 0x03, 125: 0x07}
POLL_HYPOTHESIS = POLL_RATES  # backwards-compatible name

DPI_CODES = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24)


@dataclass(frozen=True)
class Mode:
    name: str
    value: int
    color: bool = False
    speed: bool = False
    brightness: bool = False
    direction: bool = False


MODES = {
    m.name: m
    for m in (
        Mode("static", 0x0A, color=True, brightness=True),
        Mode("breathing", 0x08, color=True, speed=True),
        Mode("breathing-multi", 0x09, speed=True),
        Mode("rainbow", 0x07, speed=True, direction=True),
        Mode("spectrum", 0x06, speed=True),
        Mode("fill", 0x03, speed=True, brightness=True, direction=True),
        Mode("loop", 0x04, speed=True, brightness=True, direction=True),
        Mode("enraptured", 0xBB, speed=True),
        Mode("flicker", 0xB8, speed=True),
        Mode("ripple", 0xBA, speed=True, direction=True),
        Mode("startrek", 0xB9),
        Mode("off", 0x0A),  # static with every colour black
    )
}


class InstantError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Packet encoding (pure functions, unit-tested against the reference project)
# --------------------------------------------------------------------------- #


def parse_color(color: str) -> tuple[int, int, int]:
    c = color.strip().lstrip("#")
    if len(c) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in c):
        raise InstantError(f"invalid colour {color!r}, use #rrggbb")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def quantize(color: str) -> str:
    """The mouse stores 4 bits per channel; this is the colour you'll actually see."""
    return "#" + "".join(f"{(v >> 4) * 17:02x}" for v in parse_color(color))


def mode_packet(mode: Mode, speed: int = 0, brightness: int = 0, direction: int = 0) -> bytes:
    v = mode.value
    return bytes(
        [
            REPORT_ID,
            CMD_MODE,
            0xFF,
            ((v << 4) & 0xFF) | (speed & 0xF),
            ((direction & 1) << 4) | 0x0B,
            0xF - (brightness & 0xF),
            v & 0xF0,
            0x00,
        ]
    )


def color_packet(level: int, color: str) -> bytes:
    r, g, b = (0xF - (c >> 4) for c in parse_color(color))
    return bytes([REPORT_ID, CMD_COLOR, ((level * 2) << 4) | g, (r << 4) | b, 0, 0, 0, 0])


def lighting_packets(
    mode_name: str,
    colors: list[str] | None = None,
    speed: int = 2,
    brightness: int = BRIGHTNESS_MAX,
    direction: int = 0,
) -> list[bytes]:
    if mode_name not in MODES:
        raise InstantError(f"unknown mode {mode_name!r}; choose from: {', '.join(MODES)}")
    m = MODES[mode_name]
    if not 0 <= speed <= SPEED_MAX:
        raise InstantError(f"speed must be 0-{SPEED_MAX}")
    if not 0 <= brightness <= BRIGHTNESS_MAX:
        raise InstantError(f"brightness must be 0-{BRIGHTNESS_MAX}")
    if direction not in (0, 1):
        raise InstantError("direction must be 0 or 1")
    if m.name == "off":
        return [mode_packet(m)] + [color_packet(i, "#000000") for i in range(DPI_LEVELS)]
    pkts = [
        mode_packet(
            m,
            speed if m.speed else 0,
            brightness if m.brightness else 0,
            direction if m.direction else 0,
        )
    ]
    if m.color:
        colors = colors or ["#ffffff"]
        for c in colors:
            parse_color(c)
        for i in range(DPI_LEVELS):
            pkts.append(color_packet(i, colors[i] if i < len(colors) else colors[0]))
    return pkts


def memory_packet(op: int, index: int, addr: int, value: int, last: int) -> bytes:
    return bytes([REPORT_ID, CMD_MEMORY, op, index, addr & 0xFF, addr >> 8, value, last])


def decode_dpi(block: bytes) -> list[int]:
    lo = [block[5] >> 4, block[5] & 15, block[6] >> 4, block[6] & 15, block[7] >> 4, block[7] & 15]
    return [lo[i] | (((block[3] >> i) & 1) << 4) for i in range(DPI_LEVELS)]


def encode_dpi(block: bytes, codes: list[int]) -> bytes:
    if len(codes) != DPI_LEVELS:
        raise InstantError(f"need exactly {DPI_LEVELS} DPI values")
    for c in codes:
        if c not in DPI_CODES:
            raise InstantError(f"invalid DPI code {c}")
    b = bytearray(block)
    bit4 = 0
    for i, c in enumerate(codes):
        bit4 |= ((c >> 4) & 1) << i
    b[3] = (block[3] & 0xC0) | bit4
    b[5] = ((codes[0] & 15) << 4) | (codes[1] & 15)
    b[6] = ((codes[2] & 15) << 4) | (codes[3] & 15)
    b[7] = ((codes[4] & 15) << 4) | (codes[5] & 15)
    return bytes(b)


# Code -> DPI per product. The reference mouse (30fa:1040) is code x 200.
#
# EvoFox Blaze Ultra (30fa:1440), calibrated 2026-11-21 (see FINDINGS.md):
#   codes 1-10  -> 200..2000 in steps of 200
#   code 12     -> 2400            (code 11 / 2200 does not exist, same as the reference)
#   codes 13-23 -> 3200..11200 in steps of 800
#   code 24     -> 12800           (the app slider skips 12000)
# Anchors observed in memory dumps: 1,2,3,4,5,8,9,12,17,18,24. The rest come from the
# official app's slider stops (2400, 3200, ... 11200, 12800), which fit the anchors
# exactly and add up to the 23 codes the chip supports.
BLAZE_ULTRA_OBSERVED = {1, 2, 3, 4, 5, 8, 9, 12, 17, 18, 24}


def _blaze_ultra_table() -> dict[int, int]:
    t = {c: c * 200 for c in range(1, 11)}
    t[12] = 2400
    t.update({c: 2400 + (c - 12) * 800 for c in range(13, 24)})
    t[24] = 12800
    return t


DPI_TABLES = {0x1440: _blaze_ultra_table()}


def dpi_table(pid: int | None) -> dict[int, int]:
    if pid in DPI_TABLES:
        return dict(sorted(DPI_TABLES[pid].items()))
    return {c: c * 200 for c in DPI_CODES}


def code_to_dpi(code: int, pid: int | None = None) -> int | None:
    return dpi_table(pid).get(code)


def dpi_to_code(dpi: int, pid: int | None = None) -> int:
    """Exact match only: never silently round to a different DPI."""
    table = dpi_table(pid)
    for code, value in table.items():
        if value == dpi:
            return code
    raise InstantError(
        f"{dpi} DPI is not a known value for this mouse yet. Supported: {sorted(table.values())}"
    )


# --------------------------------------------------------------------------- #
# Device I/O
# --------------------------------------------------------------------------- #


def find_control_node(nodes, vid: int = VID, pid: int | None = None):
    """The collection that carries feature report 0x07 (usage page 0xFF01)."""
    for n in nodes:
        if n.vid != vid or (pid is not None and n.pid != pid):
            continue
        r = n.summary.find("feature", REPORT_ID)
        if (r is not None and r.size_bytes + 1 == PACKET_LEN) or getattr(n, "usage_page", None) == CONTROL_USAGE_PAGE:
            return n
    return None


class InstantMouse:
    def __init__(self, dev, sleep=None, pid: int | None = None):
        self.dev = dev
        self.pid = pid
        self.sleep = sleep or (lambda s: time.sleep(s))  # looked up at call time (patchable)
        self.sent: list[bytes] = []

    def _send(self, pkt: bytes) -> None:
        self.sent.append(pkt)
        self.dev.set_feature(pkt)

    # lighting ---------------------------------------------------------------

    def set_lighting(self, **kw) -> list[bytes]:
        pkts = lighting_packets(**kw)
        for p in pkts:
            self._send(p)
            self.sleep(0.005)
        return pkts

    # memory -----------------------------------------------------------------

    def read_block(self, addr: int, n: int = 8) -> bytes:
        out = bytearray()
        for i in range(n):
            self._send(memory_packet(MEM_READ, i, addr, 0, n - 1))
            self.sleep(0.002)
            resp = self.dev.get_feature(REPORT_ID, PACKET_LEN)
            if len(resp) < 2:
                raise InstantError(f"short reply reading 0x{addr:02x}+{i}: {resp.hex(' ')}")
            out.append(resp[1])
        self._send(memory_packet(MEM_IDLE, 0, 0, 0, 0))
        self.sleep(0.002)
        return bytes(out)

    def dump(self, end: int = DUMP_END, start: int = 0, progress=None) -> dict[int, bytes]:
        """Read memory [start, end) in 8-byte blocks. Read-only."""
        if start % 8 or end % 8 or not 0 <= start < end <= 0x10000:
            raise InstantError("start/end must be multiples of 8 within 0x0000-0x10000")
        out = {}
        for addr in range(start, end, 8):
            out[addr] = self.read_block(addr)
            if progress:
                progress(addr + 8 - start, end - start)
        return out

    def check_header(self, header: bytes | None = None, pid: int | None = None) -> tuple[bool, str]:
        header = header if header is not None else self.read_block(HEADER_BLOCK)
        if header[:3] != HEADER_MAGIC:
            return False, (
                f"header is {header.hex(' ')}, expected it to start with 5a 03 a5. The memory layout "
                "differs from the reference mouse, so DPI writes stay locked."
            )
        note = ""
        if pid is not None:
            ids = (pid.to_bytes(2, "little"), pid.to_bytes(2, "big"))
            note = " (PID found in header)" if any(i in header for i in ids) else " (PID not found in header)"
        return True, f"header {header.hex(' ')} matches the Instant layout{note}"

    def write_block(self, addr: int, data: bytes, _restore: bool = False) -> None:
        if _restore:                     # restore_blocks(): any settings block, never the header
            if not (RESTORE_START <= addr < RESTORE_END and addr % 8 == 0 and len(data) == 8):
                raise InstantError(f"refusing to restore block 0x{addr:02x}: only 8-byte blocks 0x008-0x7f8")
        elif addr not in WRITABLE_BLOCKS and addr not in MACRO_BLOCKS:
            raise InstantError(f"refusing to write block 0x{addr:02x}; it isn't a decoded settings block")
        n = len(data)
        for i, b in enumerate(data):
            self._send(memory_packet(MEM_WRITE, i, addr, b, n - 1))
        self.sleep(0.004)
        self._send(memory_packet(MEM_COMMIT, 0, addr, 0, n - 1))
        self.sleep(0.002)
        self._send(bytes([REPORT_ID, CMD_MEMORY, MEM_IDLE, 0, addr & 0xFF, addr >> 8, 0, 0]))
        self.sleep(0.002)

    def restore_blocks(self, wanted: dict[int, bytes]) -> list[int]:
        """Put blocks back to the bytes of a backup taken from THIS mouse (the caller checks the header).

        Used to repair damage, e.g. after two programs talked to the mouse at once. Any settings block
        0x008-0x7f8 can be restored, but only to bytes the mouse itself held before; the header (0x000)
        is never written. Each block is verified; the mouse reloads once at the end."""
        written = []
        for addr in sorted(wanted):
            data = bytes(wanted[addr])
            before = self.read_block(addr)
            if before == data:
                continue
            self._verified_write(addr, before, data, _restore=True)
            written.append(addr)
        if written:
            self.reload()
        return written

    def reload(self) -> None:
        """Apply stored memory now. Note: this resets lighting set in RAM."""
        self._send(memory_packet(MEM_RELOAD, 0, 0, 0, 0))
        self.sleep(0.005)
        self._send(memory_packet(MEM_IDLE, 0, 0, 0, 0))
        self.sleep(0.05)

    def _verified_write(self, addr: int, before: bytes, after: bytes, _restore: bool = False) -> bytes:
        self.write_block(addr, after, _restore)
        check = self.read_block(addr)
        if check != after:
            self.write_block(addr, before, _restore)
            restored = self.read_block(addr)
            state = "restored" if restored == before else f"RESTORE ALSO FAILED (now {restored.hex(' ')})"
            raise InstantError(
                f"mouse did not store block 0x{addr:02x}: wrote {after.hex(' ')}, read back {check.hex(' ')}; "
                f"previous value {state}. If another program is using the mouse at the same time (the EvoFox app, "
                "BLAZE Web in a browser, or BLAZE Control while the web page is open), close it and try again."
            )
        return check

    def get_dpi(self) -> dict:
        dpi = self.read_block(DPI_BLOCK)
        state = self.read_block(STATE_BLOCK)
        codes = decode_dpi(dpi)
        return {
            "codes": codes,
            "dpi": [code_to_dpi(c, self.pid) for c in codes],
            "active": state[0] & 7,
            "enabled_mask": state[1] & 0x3F,
            "dpi_block": dpi,
            "state_block": state,
            "plausible": all(c in DPI_CODES for c in codes),
        }

    def get_polling_byte(self) -> int:
        return self.read_block(STATE_BLOCK)[POLL_BYTE]

    def set_active_level(self, level: int, pid: int | None = None, apply_now: bool = True) -> dict:
        """Make DPI level `level` (0-5) the active one: block 0x18 byte 0, low 3 bits.

        Same encoding the official app and the reference project use; every other bit is kept.
        Verified by read-back and auto-restored on mismatch, then the mouse reloads to apply it.
        """
        if not 0 <= level < DPI_LEVELS:
            raise InstantError(f"level must be 1-{DPI_LEVELS}")
        ok, msg = self.check_header(pid=pid)
        if not ok:
            raise InstantError(msg)
        before = self.read_block(STATE_BLOCK)
        if (before[0] & 7) >= DPI_LEVELS:
            raise InstantError(f"current state block {before.hex(' ')} looks unexpected; refusing to write.")
        if not (before[1] >> level) & 1:
            raise InstantError(f"level {level + 1} is disabled in the mouse (enabled mask 0x{before[1]:02x})")
        after = bytearray(before)
        after[0] = (before[0] & 0xF8) | level
        after = bytes(after)
        if after == before:
            if apply_now:
                self.reload()  # make sure the mouse is really on it (the DPI button may have moved it)
            return {"changed": False, "active": level}
        self._verified_write(STATE_BLOCK, before, after)
        if apply_now:
            self.reload()
        return {"changed": True, "active": level}

    def set_polling_byte(self, value: int, pid: int | None = None, apply_now: bool = True) -> dict:
        """Write block 0x18 byte 6 only, keeping every other byte. Verified, auto-restored."""
        if value not in POLL_HYPOTHESIS.values():
            raise InstantError(f"polling byte must be one of {sorted(POLL_HYPOTHESIS.values())}")
        ok, msg = self.check_header(pid=pid)
        if not ok:
            raise InstantError(msg)
        before = self.read_block(STATE_BLOCK)
        if before[POLL_BYTE] not in POLL_HYPOTHESIS.values():
            raise InstantError(f"current state block {before.hex(' ')} looks unexpected; refusing to write.")
        after = bytearray(before)
        after[POLL_BYTE] = value
        after = bytes(after)
        if after == before:
            return {"changed": False, "value": value}
        check = self._verified_write(STATE_BLOCK, before, after)
        if apply_now:
            self.reload()
        return {"changed": True, "before": before, "after": check, "value": value}

    def read_macro(self) -> dict:
        """Read-only: decode the macro stored at 0x400 (see drivers/macro.py)."""
        from .macro import MACRO_BASE, MACRO_DIR, MAX_PAIRS, read_macro

        mem = bytearray(0x400 + 2 * (MAX_PAIRS + 2))
        mem[MACRO_DIR:MACRO_DIR + 16] = self.read_block(MACRO_DIR) + self.read_block(MACRO_DIR + 8)
        for addr in range(MACRO_BASE, len(mem), 8):
            blk = self.read_block(addr)
            mem[addr:addr + 8] = blk
            if addr > MACRO_BASE and any(blk[i] == blk[i + 1] == 0 for i in range(0, 8, 2)):
                break  # end marker found, no need to read further
        return read_macro(bytes(mem))

    def _macro_image(self) -> tuple[bytearray, dict]:
        """Read everything the macro layout touches. Returns (memory image, slot -> Macro)."""
        from .macro import MACRO_LIMIT, MacroError, parse_entry, entry_addr, ENTRY_SLOTS, ENTRY_STRIDE, read_bindings

        mem = bytearray(MACRO_LIMIT)
        for a in (BUTTON_BLOCK, 0x68, 0x70, 0x78, *range(0xA0, 0xF8, 8)):
            mem[a:a + 8] = self.read_block(a)
        codes = {i: mem[BUTTON_BLOCK + i] & 0x1F for i in range(8)}
        done = set()
        for slot in range(ENTRY_SLOTS):
            e = parse_entry(mem[entry_addr(slot):entry_addr(slot) + ENTRY_STRIDE])
            if e is None or codes[slot] not in (0x0D, 0x0E, 0x0F):
                continue
            end = e["addr"] + 4 + 2 * e["pairs"]
            if not 0x400 <= e["addr"] < end <= MACRO_LIMIT:
                raise InstantError(f"macro entry for slot {slot} points outside the macro area (0x{e['addr']:x})")
            for a in range(e["addr"] & ~7, end, 8):
                if a not in done:
                    mem[a:a + 8] = self.read_block(a)
                    done.add(a)
        try:
            return mem, read_bindings(bytes(mem), codes)
        except MacroError as e:
            raise InstantError(f"can't read the macros on the mouse: {e}") from e

    def get_macros(self) -> dict:
        """App button -> Macro | Shortcut | Media for every button using one."""
        _, bound = self._macro_image()
        num_of = {idx: num for num, _, blk, idx, _ in BUTTON_SLOTS if blk == BUTTON_BLOCK}
        return {num_of[s]: m for s, m in bound.items()}

    def set_macros(self, changes: dict, pid: int | None = None, apply_now: bool = True) -> dict:
        """changes: app button -> Macro | Shortcut | Media (bind) or a function code (unbind).

        Rewrites the macro area the way the official app does: data from 0x400, one entry per
        button, the 5 trailing pointers, then the button codes. Each block is read back.
        """
        from .macro import Macro, Media, Shortcut, MacroError, SPECIAL_CODES, layout

        ok, msg = self.check_header(pid=pid)
        if not ok:
            raise InstantError(msg)
        mem, bound = self._macro_image()
        dpi = self.read_block(DPI_BLOCK)
        current_codes = decode_buttons(bytes(mem[BUTTON_BLOCK:BUTTON_BLOCK + 8]), dpi)
        unknown = {n: c for n, c in current_codes.items() if c not in FUNCTION_NAMES}
        if unknown:
            raise InstantError(f"current mapping has functions blazectl doesn't know yet {unknown}; refusing to write.")
        slot_of = {num: idx for num, _, blk, idx, _ in BUTTON_SLOTS if blk == BUTTON_BLOCK}
        new = dict(bound)
        btn = bytearray(mem[BUTTON_BLOCK:BUTTON_BLOCK + 8])
        for num, v in changes.items():
            if num not in MACRO_BUTTONS:
                raise InstantError(f"button {num} can't hold a macro; use buttons {sorted(MACRO_BUTTONS)}")
            slot = slot_of[num]
            if isinstance(v, (Macro, Media, Shortcut)):
                try:
                    v.validate()
                except MacroError as e:
                    raise InstantError(str(e)) from e
                new[slot] = v
                btn[slot] = (btn[slot] & 0xE0) | v.code
            else:
                if v in SPECIAL_CODES or v not in FUNCTION_NAMES:
                    raise InstantError(f"button {num}: 0x{v:02x} is not a plain function")
                new.pop(slot, None)
                btn[slot] = (btn[slot] & 0xE0) | v
        if BUTTON_FUNCTIONS["left-click"] not in decode_buttons(bytes(btn), dpi).values():
            raise InstantError("refusing: at least one button must stay Left Click")
        try:
            end = layout(bytearray(mem), new)             # dry run: where will the data end?
        except MacroError as e:
            raise InstantError(str(e)) from e
        data_blocks = list(range(0x400, (end + 7) & ~7, 8))
        for a in data_blocks:                             # real "before" for every block we touch
            mem[a:a + 8] = self.read_block(a)
        after = bytearray(mem)
        layout(after, new)                                # bytes past `end` keep their old value
        after[BUTTON_BLOCK:BUTTON_BLOCK + 8] = btn
        changed = False
        for a in [*data_blocks, *range(0xA0, 0xF8, 8), 0x68, 0x70, 0x78, BUTTON_BLOCK]:
            old, new_b = bytes(mem[a:a + 8]), bytes(after[a:a + 8])
            if old != new_b:
                self._verified_write(a, old, new_b)
                changed = True
        if changed and apply_now:
            self.reload()
        return {"changed": changed, "end": end, "macros": self.get_macros()}

    def get_buttons(self) -> dict[int, int]:
        return decode_buttons(self.read_block(BUTTON_BLOCK), self.read_block(DPI_BLOCK))

    def set_buttons(self, changes: dict[int, int], pid: int | None = None, apply_now: bool = True) -> dict:
        ok, msg = self.check_header(pid=pid)
        if not ok:
            raise InstantError(msg)
        btn_before = self.read_block(BUTTON_BLOCK)
        dpi_before = self.read_block(DPI_BLOCK)
        current = decode_buttons(btn_before, dpi_before)
        unknown = {n: c for n, c in current.items() if c not in FUNCTION_NAMES}
        if unknown:
            raise InstantError(
                f"current mapping has functions blazectl doesn't know yet {unknown}; "
                "reset them in the official app first, refusing to write."
            )
        # moving a button off a macro must also clear its macro entry: let set_macros do it
        special = (0x0D, 0x0E, 0x0F)
        unbind = {n: c for n, c in changes.items() if current.get(n) in special and c not in special}
        changed = False
        if unbind:
            changed = self.set_macros(unbind, pid=pid, apply_now=False)["changed"]
            changes = {n: c for n, c in changes.items() if n not in unbind}
            btn_before = self.read_block(BUTTON_BLOCK)
        btn_after, dpi_after = encode_buttons(btn_before, dpi_before, changes)
        if btn_after != btn_before:
            self._verified_write(BUTTON_BLOCK, btn_before, btn_after)
            changed = True
        if dpi_after != dpi_before:
            self._verified_write(DPI_BLOCK, dpi_before, dpi_after)
            changed = True
        if changed and apply_now:
            self.reload()
        return {"changed": changed, "buttons": decode_buttons(self.read_block(BUTTON_BLOCK), self.read_block(DPI_BLOCK))}

    def set_dpi(self, codes: list[int], pid: int | None = None, apply_now: bool = True) -> dict:
        ok, msg = self.check_header(pid=pid)
        if not ok:
            raise InstantError(msg)
        before = self.read_block(DPI_BLOCK)
        current = decode_dpi(before)
        if not all(c in DPI_CODES for c in current):
            raise InstantError(
                f"current DPI block {before.hex(' ')} decodes to {current}, which is not a valid set. "
                "The layout may differ on this mouse; refusing to write."
            )
        after = encode_dpi(before, codes)
        if after == before:
            return {"changed": False, "codes": current}
        check = self._verified_write(DPI_BLOCK, before, after)
        if apply_now:
            self.reload()
        return {"changed": True, "before": before, "after": check, "codes": decode_dpi(check)}
