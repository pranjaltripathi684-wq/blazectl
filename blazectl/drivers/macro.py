# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Macro storage format for Instant-chipset mice (EvoFox Blaze Ultra, 30fa:1440).

Decoded from a real dump taken after the official app stored this macro on
button 7 (cycle times = 1):

    A down, 110 ms | A up, 2232 ms | Enter down, 90 ms | Enter up, 976 ms
    B down, 80 ms  | B up, 20 ms

Memory at 0x400 afterwards:

    01 00 | ee 04 | 49 04 | 7f 00 x17 | da 28 | 57 28 | 7f 00 x7 | d0 05 | 14 05 | 00 00

* 2-byte header (01 00): byte 0 is probably the repeat count (not confirmed yet).
* Events are 2-byte pairs (T, K): bit 7 of T = key down, the low 7 bits of T =
  the delay in ms after the event (0-127), K = the USB HID keyboard usage.
* Delays over 127 ms continue with filler pairs (7f 00) worth 127 ms each,
  the event itself carries delay % 127.
* (00 00) ends the list.
Layout confirmed with 7 dumps (m_base, m_ab, m_two .. m_six, 2026-11-22):

* Macros are stored back to back from 0x400, each = [repeat, 00] + pairs + (00 00).
  One copy per distinct macro, even when several buttons use it.
* Every button slot s (0-7, the byte index in block 0x08) has a 10-byte entry at
  0xA1 + 10*s:  [pairs | 0x80 if hold-mode, ff, 00, 00, addrL, addrH, 00, 0a, 00, 00]
  All zeros when the button has no macro. Confirmed for slots 4, 5 and 7.
* The 16-bit little-endian words at 0x6e, 0x70, 0x72, 0x74 and 0x79 always equal
  end_of_macros + 0, 8, 16, 24, 32 (0x400 + ... when there are no macros).
* The button slot gets function 0x0D for "specified cycle times" (repeat count in
  the macro's header byte) or 0x0E for "cycle until the key is released" (header 1,
  entry byte 0 gets bit 7). "Cycle until clicked again" never reached the mouse
  in testing, so it isn't supported.
"""
from __future__ import annotations

from dataclasses import dataclass

MACRO_BASE = 0x400
MACRO_DIR = 0xD0
MACRO_FUNCTION = 0x0D
DELAY_STEP = 0x7F          # max delay one pair can hold
DOWN_BIT = 0x80
FILLER = (DELAY_STEP, 0x00)
MAX_PAIRS = 0x7F           # entry byte 0 holds the count in 7 bits (bit 7 = hold mode)
MACRO_TIMES = 0x0D         # button function: play N times
MACRO_HOLD = 0x0E          # button function: repeat while held
MACRO_CODES = (MACRO_TIMES, MACRO_HOLD)
ENTRY_BASE = 0xA1
ENTRY_STRIDE = 10
ENTRY_SLOTS = 8            # slots 0-7 of block 0x08
POINTERS = (0x6E, 0x70, 0x72, 0x74, 0x79)
MACRO_LIMIT = 0x800        # keep all macros + the 5 trailing records below this (erased flash up to 0xF9F)


def entry_addr(slot: int) -> int:
    return ENTRY_BASE + ENTRY_STRIDE * slot

# USB HID keyboard usage page (0x07) - the keys people actually put in macros.
KEY_NAMES: dict[int, str] = {}
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEY_NAMES[0x04 + i] = ch.upper()
for i, ch in enumerate("1234567890"):
    KEY_NAMES[0x1E + i] = ch
KEY_NAMES.update({
    0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2B: "Tab", 0x2C: "Space",
    0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]", 0x31: "\\", 0x33: ";", 0x34: "'",
    0x35: "`", 0x36: ",", 0x37: ".", 0x38: "/", 0x39: "CapsLock",
    0x46: "PrintScreen", 0x47: "ScrollLock", 0x48: "Pause", 0x49: "Insert",
    0x4A: "Home", 0x4B: "PageUp", 0x4C: "Delete", 0x4D: "End", 0x4E: "PageDown",
    0x4F: "Right", 0x50: "Left", 0x51: "Down", 0x52: "Up",
    0x53: "NumLock", 0x54: "KP/", 0x55: "KP*", 0x56: "KP-", 0x57: "KP+", 0x58: "KPEnter",
    0xE0: "LCtrl", 0xE1: "LShift", 0xE2: "LAlt", 0xE3: "LWin",
    0xE4: "RCtrl", 0xE5: "RShift", 0xE6: "RAlt", 0xE7: "RWin",
})
for i in range(12):
    KEY_NAMES[0x3A + i] = f"F{i + 1}"
for i, ch in enumerate("1234567890"):
    KEY_NAMES[0x59 + i] = f"KP{ch}"
KEY_CODES = {v.lower(): k for k, v in KEY_NAMES.items()}


class MacroError(ValueError):
    pass


@dataclass
class MacroEvent:
    key: int          # HID usage
    down: bool
    delay: int        # ms to wait after this event

    @property
    def name(self) -> str:
        return KEY_NAMES.get(self.key, f"0x{self.key:02x}")

    def __str__(self) -> str:
        return f"{self.name} {'down' if self.down else 'up'}, {self.delay} ms"


def key_code(name: str) -> int:
    """'a' / 'Enter' / 'F5' / '0x04' -> HID usage."""
    s = name.strip()
    if s.lower().startswith("0x"):
        code = int(s, 16)
    elif s.lower() in KEY_CODES:
        code = KEY_CODES[s.lower()]
    else:
        raise MacroError(f"unknown key {name!r}")
    if not 0x04 <= code <= 0xE7:
        raise MacroError(f"key code 0x{code:02x} is out of range")
    return code


def decode_events(data: bytes) -> tuple[list[MacroEvent], int]:
    """Parse pairs until (00 00). Returns (events, number_of_pairs)."""
    events: list[MacroEvent] = []
    pairs = 0
    for i in range(0, len(data) - 1, 2):
        t, k = data[i], data[i + 1]
        if t == 0 and k == 0:
            return events, pairs
        pairs += 1
        delay = t & DELAY_STEP
        if k == 0 and not t & DOWN_BIT:
            if not events:
                raise MacroError("macro starts with a delay filler")
            events[-1].delay += delay
            continue
        events.append(MacroEvent(k, bool(t & DOWN_BIT), delay))
    raise MacroError("no end marker (00 00) found")


def encode_events(events: list[MacroEvent]) -> bytes:
    """Inverse of decode_events, including the (00 00) end marker."""
    out = bytearray()
    for ev in events:
        if not 0x04 <= ev.key <= 0xE7:
            raise MacroError(f"key code 0x{ev.key:02x} is out of range")
        if ev.delay < 0:
            raise MacroError("negative delay")
        fillers, first = divmod(ev.delay, DELAY_STEP)
        out += bytes([(DOWN_BIT if ev.down else 0) | first, ev.key])
        out += bytes(FILLER) * fillers
    if len(out) // 2 > MAX_PAIRS:
        raise MacroError(f"macro needs {len(out) // 2} pairs, limit is {MAX_PAIRS}")
    return bytes(out) + b"\x00\x00"


@dataclass
class Macro:
    events: list
    mode: str = "times"       # "times" or "hold"
    repeat: int = 1           # used in "times" mode, 1-255

    def validate(self) -> None:
        if not self.events:
            raise MacroError("a macro needs at least one event")
        if self.mode not in ("times", "hold"):
            raise MacroError("mode must be 'times' or 'hold'")
        if not 1 <= self.repeat <= 255:
            raise MacroError("repeat must be 1-255")

    def blob(self) -> bytes:
        self.validate()
        return bytes([self.repeat if self.mode == "times" else 1, 0]) + encode_events(self.events)

    @property
    def code(self) -> int:
        return MACRO_HOLD if self.mode == "hold" else MACRO_TIMES

    def to_json(self) -> dict:
        return {"mode": self.mode, "repeat": self.repeat,
                "events": [[KEY_NAMES.get(e.key, f"0x{e.key:02x}"), "down" if e.down else "up", e.delay] for e in self.events]}

    @classmethod
    def from_json(cls, d: dict) -> "Macro":
        evs = [MacroEvent(key_code(str(k)), str(a).lower() == "down", int(ms)) for k, a, ms in d["events"]]
        m = cls(evs, str(d.get("mode", "times")), int(d.get("repeat", 1)))
        m.validate()
        return m


# ---- media keys and shortcuts (m7_base vs m7_ms, 2026-11-22) -------------------------------
# Media: button function 0x0F; the entry carries the USB HID consumer usage in bytes 2-3
# (Volume Up = e9, Play/Pause = cd, seen in the dump) and no macro data.
# Shortcut: function 0x0E like a hold macro, data = [01 00] + one key-down pair per key (4 ms),
# entry byte 2 = b7 and byte 7 = 00 (a recorded hold macro has 00 / 0a there).
MEDIA_CODE = 0x0F
SPECIAL_CODES = (MACRO_TIMES, MACRO_HOLD, MEDIA_CODE)
MEDIA_KEYS = {   # name -> (USB HID consumer usage, label)
    "volume-up": (0x0E9, "Volume Up"), "volume-down": (0x0EA, "Volume Down"), "mute": (0x0E2, "Mute"),
    "play-pause": (0x0CD, "Play/Pause"), "next-track": (0x0B5, "Next Track"), "prev-track": (0x0B6, "Previous Track"),
    "stop": (0x0B7, "Stop"), "browser": (0x196, "Browser"), "media-player": (0x183, "Media Player"),
    "mail": (0x18A, "Mail"), "calculator": (0x192, "Calculator"), "my-computer": (0x194, "My Computer"),
}
# Confirmed: e9/cd from the app's dump; browser (16-bit 0x196) and mute tested on the real mouse.
# The table is the standard USB consumer-page one, so the rest follow the same codes.
MEDIA_CONFIRMED = {"volume-up", "play-pause", "browser", "mute"}
MEDIA_BY_USAGE = {u: n for n, (u, _) in MEDIA_KEYS.items()}
MODIFIERS = {"ctrl": 0xE0, "shift": 0xE1, "alt": 0xE2, "win": 0xE3}   # app order; left-hand keys
SHORTCUT_DELAY = 4


@dataclass
class Media:
    key: str

    def validate(self) -> None:
        if self.key not in MEDIA_KEYS:
            raise MacroError(f"unknown media key {self.key!r}; use one of {', '.join(MEDIA_KEYS)}")

    code = MEDIA_CODE
    usage = property(lambda self: MEDIA_KEYS[self.key][0])
    label = property(lambda self: MEDIA_KEYS[self.key][1])

    def to_json(self) -> dict:
        return {"type": "media", "key": self.key}


@dataclass
class Shortcut:
    mods: list
    key: int

    def validate(self) -> None:
        bad = [m for m in self.mods if m not in MODIFIERS]
        if bad:
            raise MacroError(f"unknown modifier {bad[0]!r}; use ctrl, shift, alt, win")
        if not 0x04 <= self.key <= 0xE7 or self.key in MODIFIERS.values():
            raise MacroError("a shortcut needs one normal key (not only modifiers)")

    code = MACRO_HOLD

    def events(self) -> list:
        mods = [m for m in MODIFIERS if m in self.mods]          # always in app order
        return [MacroEvent(MODIFIERS[m], True, SHORTCUT_DELAY) for m in mods] + [MacroEvent(self.key, True, SHORTCUT_DELAY)]

    def blob(self) -> bytes:
        self.validate()
        return bytes([1, 0]) + encode_events(self.events())

    @property
    def label(self) -> str:
        return "+".join([m.capitalize() for m in MODIFIERS if m in self.mods] + [KEY_NAMES.get(self.key, hex(self.key))])

    def to_json(self) -> dict:
        return {"type": "shortcut", "mods": [m for m in MODIFIERS if m in self.mods], "key": KEY_NAMES.get(self.key, hex(self.key))}

    @classmethod
    def parse(cls, text: str) -> "Shortcut":
        """'ctrl+c', 'ctrl+shift+esc'"""
        parts = [p.strip().lower() for p in text.split("+") if p.strip()]
        if not parts:
            raise MacroError("empty shortcut")
        mods, key = parts[:-1], key_code(parts[-1])
        sc = cls(mods, key)
        sc.validate()
        return sc


def binding_from_json(d: dict):
    kind = d.get("type", "macro")
    try:
        if kind == "media":
            b = Media(str(d["key"]))
        elif kind == "shortcut":
            b = Shortcut([str(m).lower() for m in d.get("mods", [])], key_code(str(d["key"])))
        elif kind == "macro":
            return Macro.from_json(d)
        else:
            raise MacroError(f"unknown binding type {kind!r}")
    except (KeyError, TypeError) as e:
        raise MacroError(f"incomplete {kind}: {e}") from e
    b.validate()
    return b


def parse_entry(entry: bytes) -> dict | None:
    if not any(entry):
        return None
    return {"pairs": entry[0] & 0x7F, "hold": bool(entry[0] & 0x80), "addr": entry[4] | entry[5] << 8,
            "usage": entry[2] | entry[3] << 8, "tag": entry[7]}


def make_entry(binding, addr: int, pairs: int = 0) -> bytes:
    if isinstance(binding, Media):
        u = binding.usage
        return bytes([0, 0xFF, u & 0xFF, u >> 8, addr & 0xFF, addr >> 8, 0, 0, 0, 0])
    if isinstance(binding, Shortcut):
        return bytes([pairs | 0x80, 0xFF, 0xB7, 0, addr & 0xFF, addr >> 8, 0, 0, 0, 0])
    return bytes([pairs | (0x80 if binding.mode == "hold" else 0), 0xFF, 0, 0, addr & 0xFF, addr >> 8, 0, 0x0A, 0, 0])


def read_bindings(mem: bytes, codes: dict[int, int]) -> dict:
    """codes: slot -> function code (low 5 bits). Returns slot -> Macro | Shortcut | Media."""
    out = {}
    for slot, code in codes.items():
        if code not in SPECIAL_CODES or slot >= ENTRY_SLOTS:
            continue
        e = parse_entry(mem[entry_addr(slot):entry_addr(slot) + ENTRY_STRIDE])
        if e is None:
            raise MacroError(f"slot {slot} has function 0x{code:02x} but no entry")
        if code == MEDIA_CODE:
            name = MEDIA_BY_USAGE.get(e["usage"])
            if name is None:
                raise MacroError(f"slot {slot}: media usage 0x{e['usage']:03x} isn't known yet")
            out[slot] = Media(name)
            continue
        events, pairs = decode_events(mem[e["addr"] + 2:e["addr"] + 4 + 2 * MAX_PAIRS])
        if pairs != e["pairs"]:
            raise MacroError(f"slot {slot}: entry says {e['pairs']} pairs, data at 0x{e['addr']:03x} has {pairs}")
        hold = code == MACRO_HOLD
        if hold and e["tag"] == 0 and events and all(ev.down for ev in events):
            mods = [m for m, c in MODIFIERS.items() if any(ev.key == c for ev in events)]
            keys = [ev.key for ev in events if ev.key not in MODIFIERS.values()]
            if len(keys) == 1:
                out[slot] = Shortcut(mods, keys[0])
                continue
        out[slot] = Macro(events, "hold" if hold else "times", 1 if hold else mem[e["addr"]] or 1)
    return out


def layout(mem: bytearray, bindings: dict) -> int:
    """Write data, entries and pointers for {slot: Macro | Shortcut | Media} into a memory image.

    Same order as the official app: slots ascending, each distinct blob stored once at the next free
    address; a media entry records the free address at that point. Returns the end of the data.
    """
    addr_of: dict[bytes, int] = {}
    pos = MACRO_BASE
    entries = {}
    for slot in sorted(bindings):
        b = bindings[slot]
        if isinstance(b, Media):
            b.validate()
            entries[slot] = make_entry(b, pos)
            continue
        blob = b.blob()
        if blob not in addr_of:
            addr_of[blob] = pos
            pos += len(blob)
        entries[slot] = make_entry(b, addr_of[blob], (len(blob) - 4) // 2)
    if pos + 0x28 > MACRO_LIMIT:
        raise MacroError(f"macros need {pos - MACRO_BASE} bytes, limit is {MACRO_LIMIT - MACRO_BASE - 0x28}")
    for blob, a in addr_of.items():
        mem[a:a + len(blob)] = blob
    for slot in range(ENTRY_SLOTS):
        ea = entry_addr(slot)
        mem[ea:ea + ENTRY_STRIDE] = entries.get(slot, bytes(ENTRY_STRIDE))
    for k, pa in enumerate(POINTERS):
        v = pos + 8 * k
        mem[pa:pa + 2] = bytes([v & 0xFF, v >> 8])
    return pos


def pair_count(events: list[MacroEvent]) -> int:
    return (len(encode_events(events)) - 2) // 2


def read_macro(mem: bytes, base: int = MACRO_BASE) -> dict:
    """Decode the macro stored at `base` of a full memory image."""
    header = mem[base:base + 2]
    events, pairs = decode_events(mem[base + 2:base + 2 + 2 * (MAX_PAIRS + 1)])
    stored = mem[MACRO_DIR + 3] if len(mem) > MACRO_DIR + 3 else None
    return {
        "header": header.hex(" "),
        "repeat": header[0],
        "events": events,
        "pairs": pairs,
        "directory_pairs": stored,
        "consistent": stored == pairs,
    }


def parse_script(text: str) -> list[MacroEvent]:
    """'A down 110, A up 2232, Enter tap 50' -> events. 'tap' = down then up."""
    events: list[MacroEvent] = []
    for chunk in text.replace("\n", ",").split(","):
        parts = chunk.split()
        if not parts:
            continue
        if len(parts) not in (2, 3):
            raise MacroError(f"cannot parse {chunk.strip()!r} (want: KEY down|up|tap [ms])")
        key, action = key_code(parts[0]), parts[1].lower()
        delay = int(parts[2]) if len(parts) == 3 else 20
        if action == "tap":
            events += [MacroEvent(key, True, delay), MacroEvent(key, False, delay)]
        elif action in ("down", "up"):
            events.append(MacroEvent(key, action == "down", delay))
        else:
            raise MacroError(f"action must be down, up or tap, not {action!r}")
    return events
