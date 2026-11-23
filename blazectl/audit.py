# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""blazectl audit: could this mouse type commands on its own?

A mouse that stores keyboard macros in its own flash can carry keystrokes from PC to PC: plug it in
anywhere, press the bound button, and it types, with no software installed. On Windows any program
running as the normal user can write those macros (blazectl itself needs no admin), so a macro is a
place where unwanted keystrokes could hide and survive a reinstall. See docs/security/macro-persistence.md.

The audit is READ-ONLY. It reads the settings and macro memory, then reports:

* every macro, shortcut and media key, with the text it would type ("[Win+R]cmd⏎");
* patterns that open a command prompt / Run box / terminal or type command-like text;
* macro data left in memory that no button uses (normal after edits, but worth seeing);
* bytes in areas that are normally empty.

Exit code: 0 = nothing suspicious, 1 = something to look at (low/medium), 2 = high.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .drivers import instant as ins
from .drivers import macro as mac

LEVELS = ("info", "low", "medium", "high")

# US layout: HID usage -> (normal, with Shift)
_CHARS: dict[int, tuple[str, str]] = {0x04 + i: (c, c.upper()) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
_CHARS.update({0x1E + i: (d, s) for i, (d, s) in enumerate(zip("1234567890", "!@#$%^&*()"))})
_CHARS.update({0x2C: (" ", " "), 0x2D: ("-", "_"), 0x2E: ("=", "+"), 0x2F: ("[", "{"), 0x30: ("]", "}"),
               0x31: ("\\", "|"), 0x33: (";", ":"), 0x34: ("'", '"'), 0x35: ("`", "~"), 0x36: (",", "<"),
               0x37: (".", ">"), 0x38: ("/", "?"), 0x2B: ("\t", "\t")})
_CHARS.update({0x59 + i: (d, d) for i, d in enumerate("1234567890")})
_CHARS.update({0x54: ("/", "/"), 0x55: ("*", "*"), 0x56: ("-", "-"), 0x57: ("+", "+")})
ENTER_KEYS = {0x28, 0x58}
MODS = {0xE0: "Ctrl", 0xE4: "Ctrl", 0xE1: "Shift", 0xE5: "Shift", 0xE2: "Alt", 0xE6: "Alt", 0xE3: "Win", 0xE7: "Win"}

# Key combos that open a place where typed text runs as a command.
LAUNCHERS = {
    ("Win", "R"): "opens the Run box (Windows)",
    ("Win", "X"): "opens the power-user menu (Windows: terminal, PowerShell)",
    ("Alt", "Ctrl", "T"): "opens a terminal (Linux desktops)",
    ("Ctrl", "Shift", "Enter"): "runs the selected search result as administrator (Windows)",
    ("Alt", "F2"): "opens the run-command box (Linux desktops)",
}
# Command-like words, matched case-insensitively as whole words (so "model x" doesn't hit "del").
KEYWORDS = ("cmd", "powershell", "pwsh", "wscript", "cscript", "mshta", "rundll32", "regsvr32", "certutil",
            "bitsadmin", "curl", "wget", "iwr", "iex", "invoke-", "http://", "https://", "ftp://", "\\\\",
            "bash", "sh -c", "/bin/", "nc", "ncat", "/dev/tcp", "base64", "-enc", "-encodedcommand", "chmod",
            "sudo", "rm -rf", "del", "reg add", "schtasks", "net user", "start-process", "downloadstring",
            "frombase64string", "set-mppreference", "vssadmin", "bcdedit", "attrib")


def command_words(text: str) -> list[str]:
    import re
    low = text.lower()
    hits = []
    for k in KEYWORDS:
        if k[0].isalnum() and k[-1].isalnum():
            if re.search(r"(?<![a-z0-9_])" + re.escape(k) + r"(?![a-z0-9_])", low):
                hits.append(k)
        elif k in low:
            hits.append(k)
    return sorted(set(hits))


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""
    button: int | None = None
    typed: str | None = None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    bindings: dict[int, str] = field(default_factory=dict)      # button -> description
    source: str = ""

    @property
    def worst(self) -> str:
        return max((f.level for f in self.findings), key=LEVELS.index, default="info")

    @property
    def exit_code(self) -> int:
        return {"info": 0, "low": 1, "medium": 1, "high": 2}[self.worst]

    def add(self, level, title, detail="", button=None, typed=None) -> None:
        self.findings.append(Finding(level, title, detail, button, typed))

    def to_json(self) -> dict:
        return {"source": self.source, "worst": self.worst, "exit_code": self.exit_code,
                "bindings": {str(k): v for k, v in self.bindings.items()},
                "findings": [asdict(f) for f in sorted(self.findings, key=lambda f: -LEVELS.index(f.level))]}


@dataclass
class Typed:
    text: str            # readable rendering, e.g. "[Win+R]cmd /c ...⏎"
    plain: str           # only the characters, for keyword matching
    combos: list[tuple]  # sorted modifier+key tuples pressed, e.g. ("Win", "R")
    enters: int
    ends_with_enter: bool


def render(events: list[mac.MacroEvent]) -> Typed:
    """What a key sequence types, assuming a US layout."""
    held: set[str] = set()
    out, plain, combos, enters = [], [], [], 0
    last_was_enter = False
    for ev in events:
        if ev.key in MODS:
            name = MODS[ev.key]
            if ev.down:
                held.add(name)
            else:
                held.discard(name)
            continue
        if not ev.down:
            continue
        keyname = mac.KEY_NAMES.get(ev.key, f"0x{ev.key:02x}")
        combo_mods = sorted(held - {"Shift"})
        if combo_mods:
            label = "+".join(combo_mods + (["Shift"] if "Shift" in held else []) + [keyname])
            out.append(f"[{label}]")
            combos.append(tuple(sorted(held)) + (keyname,))
            if ev.key in ENTER_KEYS:
                enters += 1
                last_was_enter = True
            else:
                last_was_enter = False
            continue
        if ev.key in ENTER_KEYS:
            out.append("⏎")
            enters += 1
            last_was_enter = True
            continue
        last_was_enter = False
        ch = _CHARS.get(ev.key)
        if ch:
            c = ch[1] if "Shift" in held else ch[0]
            out.append(c)
            plain.append(c)
        else:
            out.append(f"⟨{keyname}⟩")
    return Typed("".join(out), "".join(plain), combos, enters, last_was_enter)


def _win_tapped_alone(events) -> bool:
    """Win pressed and released with no other key in between: opens the Start menu search."""
    for i, ev in enumerate(events[:-1]):
        nxt = events[i + 1]
        if ev.key in (0xE3, 0xE7) and ev.down and nxt.key == ev.key and not nxt.down:
            return True
    return False


def assess(button: int, binding, report: Report) -> None:
    if isinstance(binding, mac.Media):
        report.bindings[button] = f"media key: {binding.label}"
        if binding.key in ("browser", "mail", "my-computer", "calculator", "media-player"):
            report.add("info", f"button {button} opens an app ({binding.label})", button=button)
        return
    if isinstance(binding, mac.Shortcut):
        report.bindings[button] = f"shortcut: {binding.label}"
        events = binding.events()
    else:
        how = "while held" if binding.mode == "hold" else f"{binding.repeat}x"
        report.bindings[button] = f"macro ({how}), {len(binding.events)} events"
        events = binding.events
    t = render(events)
    launch = [LAUNCHERS[c] for c in (tuple(sorted(c[:-1])) + (c[-1],) for c in t.combos) if c in LAUNCHERS]
    if _win_tapped_alone(events):
        launch.append("opens the Start menu, where typed text can start programs (Windows)")
    words = command_words(t.plain)
    executes = t.enters > 0
    long_text = len(t.plain) >= 40
    level, why = "info", []
    if launch:
        why.append(launch[0])
    if words:
        why.append("types command-like text (" + ", ".join(words[:6]) + ")")
    if executes and (t.plain or launch):
        why.append("then presses Enter")
    if launch and (words or (executes and t.plain)):
        level = "high"
    elif words and executes:
        level = "high"
    elif launch or words:
        level = "medium"
    elif long_text and executes:
        level, why = "medium", why + ["types a long text and presses Enter"]
    elif executes and t.plain:
        level = "low"
    if isinstance(binding, mac.Macro) and binding.repeat >= 50 and t.plain:
        why.append(f"repeats {binding.repeat} times per press")
        level = max(level, "low", key=LEVELS.index)
    title = f"button {button}: " + ("could launch a command" if level == "high" else
                                    "worth checking" if level == "medium" else "types text" if t.plain else
                                    "key sequence")
    report.add(level, title, "; ".join(why) or "plain key sequence", button, t.text)


def _region_has_data(mem: bytes, start: int, end: int) -> list[int]:
    """Addresses (8-byte blocks) in [start, end) that are neither erased (ff) nor empty (00)."""
    out = []
    for a in range(start, min(end, len(mem)), 8):
        blk = mem[a:a + 8]
        if any(b not in (0x00, 0xFF) for b in blk):
            out.append(a)
    return out


def leftover_runs(mem: bytes, start: int, limit: int = mac.MACRO_LIMIT) -> list[tuple[int, Typed]]:
    """Key events stored after the live macros: stale tails of earlier macros (the official app and
    blazectl both leave them, it's normal). Walks raw (T, K) pairs: K = 0 is a delay filler or a
    header, (00 00) ends a run, anything outside the keyboard range breaks it."""
    found, events, run_at = [], [], None
    for a in range(start & ~1, min(limit, len(mem)) - 1, 2):
        t, k = mem[a], mem[a + 1]
        if t == 0 and k == 0:
            if events:
                found.append((run_at, render(events)))
            events, run_at = [], None
        elif k == 0:
            if events:
                events[-1].delay += t & mac.DELAY_STEP
        elif 0x04 <= k <= 0xE7:
            if not events:
                run_at = a
            events.append(mac.MacroEvent(k, bool(t & mac.DOWN_BIT), t & mac.DELAY_STEP))
        else:
            events, run_at = [], None
    if events:
        found.append((run_at, render(events)))
    return found


def audit_memory(mem: bytes, source: str = "") -> Report:
    """Audit a memory image (index = address). Needs at least 0x000-0x800."""
    r = Report(source=source)
    mem = bytes(mem)
    if mem[:3] != ins.HEADER_MAGIC:
        r.add("medium", "memory header is not the expected Instant layout",
              f"header {mem[:8].hex(' ')}; the rest of this report may be wrong")
    codes = {i: mem[ins.BUTTON_BLOCK + i] & 0x1F for i in range(8)}
    num_of = {idx: num for num, _, blk, idx, _ in ins.BUTTON_SLOTS if blk == ins.BUTTON_BLOCK}
    if codes[0] in mac.SPECIAL_CODES:
        r.add("high", "the LEFT button holds a macro/shortcut",
              "every left click would play it; the official app never does this", 1)
    unknown = {num_of.get(s, f"slot {s}"): f"0x{c:02x}" for s, c in codes.items()
               if c not in ins.FUNCTION_NAMES and s in num_of}
    if unknown:
        r.add("medium", "buttons use functions blazectl doesn't know", json.dumps(unknown))
    try:
        bound = mac.read_bindings(mem, codes)
    except mac.MacroError as e:
        r.add("medium", "the macro area doesn't decode cleanly", str(e))
        bound = {}
    for slot, b in sorted(bound.items()):
        assess(num_of.get(slot, slot), b, r)
    # entries for buttons that don't use them (left behind; inactive)
    for slot in range(mac.ENTRY_SLOTS):
        e = mac.parse_entry(mem[mac.entry_addr(slot):mac.entry_addr(slot) + mac.ENTRY_STRIDE])
        if e is not None and slot not in bound and codes.get(slot) not in mac.SPECIAL_CODES:
            r.add("info", f"inactive macro entry for slot {slot}", "a button used to hold a macro; it does nothing now")
    # end of live data from the trailing pointer (all 5 pointers = end + 0, 8, ... 32)
    ptr = mem[mac.POINTERS[0]] | mem[mac.POINTERS[0] + 1] << 8
    end = ptr if mac.MACRO_BASE <= ptr < mac.MACRO_LIMIT else mac.MACRO_BASE
    for a, t in leftover_runs(mem, end):
        words = command_words(t.plain)
        r.add("low" if words else "info", f"leftover key data at 0x{a:03x} (not on any button)",
              "stale copy from an earlier edit; it can't play unless a button points to it"
              + (f"; contains command-like text ({', '.join(words[:6])})" if words else ""), typed=t.text)
    odd = _region_has_data(mem, 0xF8, 0x400)
    if odd:
        r.add("medium", "data in a normally-empty area",
              f"{len(odd)} block(s) between 0x0f8 and 0x3ff, first at 0x{odd[0]:03x}; on a factory mouse this area is empty")
    if len(mem) >= 0x1000:
        odd = _region_has_data(mem, mac.MACRO_LIMIT, 0xFA0)
        if odd:
            r.add("medium", "data past the macro area",
                  f"{len(odd)} block(s) between 0x800 and 0xf9f, first at 0x{odd[0]:03x}; normally erased (ff)")
    if not r.findings or all(f.level == "info" for f in r.findings):
        r.add("info", "nothing suspicious found", "no stored key sequence opens a command line or types commands")
    return r


def image_from_blocks(blocks: dict[int, bytes], size: int = 0x1000) -> bytes:
    mem = bytearray(size)
    for a, b in blocks.items():
        if a + len(b) <= size:
            mem[a:a + len(b)] = b
    return bytes(mem)


def format_report(r: Report) -> str:
    icon = {"high": "!!", "medium": " !", "low": " ·", "info": "  "}
    lines = [f"blazectl audit · {r.source}", ""]
    if r.bindings:
        lines.append("Buttons with stored key sequences / media keys:")
        for b, d in sorted(r.bindings.items()):
            lines.append(f"  button {b}: {d}")
        lines.append("")
    for f in sorted(r.findings, key=lambda f: -LEVELS.index(f.level)):
        lines.append(f"{icon[f.level]} [{f.level.upper():6}] {f.title}")
        if f.detail:
            lines.append(f"           {f.detail}")
        if f.typed:
            lines.append(f"           types: {f.typed}")
    lines += ["", {"high": "RESULT: HIGH. A stored key sequence could run commands. Remove it: "
                           "`blazectl macro clear N --execute` (or pick a normal function in the app).",
                   "medium": "RESULT: worth a look. Check the entries above are yours.",
                   "low": "RESULT: probably fine. Check the entries above are yours.",
                   "info": "RESULT: clean."}[r.worst]]
    return "\n".join(lines)
