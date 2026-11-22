# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""blazectl command-line interface."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .analysis import (
    device_table,
    diff_captures,
    group_commands,
    guess_checksums,
    hexdump_row,
    pick_device,
)
from .capture import CaptureError, load_transfers
from .descriptor import page_name
from .backend import enumerate_nodes, make_device
from .protocol import ApplyError, Profile, ProfileStore


def _int(s: str) -> int:
    return int(s, 0)


def _usb_id(s: str) -> tuple[int, int]:
    v, p = s.split(":")
    return int(v, 16), int(p, 16)


def _device(s: str) -> tuple[int, int]:
    b, d = s.split(":")
    return int(b), int(d)


def _nodes_for(args):
    nodes = enumerate_nodes()
    if getattr(args, "node", None):
        nodes = [n for n in nodes if n.name == args.node]
    elif getattr(args, "usb_id", None):
        vid, pid = _usb_id(args.usb_id)
        nodes = [n for n in nodes if n.vid == vid and n.pid == pid]
    if not nodes:
        sys.exit("No matching hidraw device. Is the mouse plugged in? Try `blazectl list`.")
    return nodes


def _load(path: str, args) -> list:
    dev = _device(args.device) if getattr(args, "device", None) else None
    transfers = load_transfers(path, device=None, include_in=getattr(args, "include_in", False))
    if dev is None:
        dev = pick_device(transfers)
        if dev is None:
            sys.exit(f"{path}: no host->device HID traffic found.")
        print(f"# {path}: using bus {dev[0]} device {dev[1]} (most config traffic; override with --device)")
    out = [t for t in transfers if (t.bus, t.device) == dev]
    for i, t in enumerate(out):
        t.index = i
    return out


# ------------------------------------------------------------------ device ---


def cmd_list(args):
    nodes = enumerate_nodes()
    if not nodes:
        print("No hidraw devices found.")
        return
    for n in nodes:
        flag = "VENDOR CONFIG IF" if n.is_vendor else ""
        access = {True: "rw", False: "no-access", None: ""}[n.accessible]
        print(f"{n.name:9} {n.usb_id}  if={n.interface}  {access:9}  {flag:16}  {n.hid_name}")
    print("\nThe mouse's configuration channel is usually the interface marked VENDOR CONFIG IF.")


def cmd_probe(args):
    for n in _nodes_for(args):
        s = n.summary
        print(f"== {n.name}  {n.usb_id}  interface {n.interface}  {n.hid_name}")
        if getattr(n, "usage_page", None) is not None and n.backend == "hidapi":
            print(f"   collection usage page: {page_name(n.usage_page)}  usage 0x{n.usage:02X}")
        if not n.report_descriptor:
            print("   (report descriptor unavailable: locked by Windows or old hidapi; sizes unknown)\n")
            continue
        print(f"   report IDs in use: {s.uses_report_ids}")
        for page, usage in s.collections:
            print(f"   collection: {page_name(page)} usage 0x{usage:02X}")
        for r in s.reports:
            tag = "  <-- vendor" if r.vendor_defined else ""
            print(f"   {r.kind:8} id=0x{r.report_id:02X}  {r.size_bytes:3} bytes  ({page_name(r.usage_page)}){tag}")
        if args.raw:
            print("   descriptor:", n.report_descriptor.hex(" "))
        print()


def _node_with_feature(args, report_id: int):
    """On Windows one mouse is several collections; pick the one that owns this feature report."""
    nodes = _nodes_for(args)
    for n in nodes:
        if n.summary.find("feature", report_id):
            return n
    return nodes[0]


def cmd_get_feature(args):
    node = _node_with_feature(args, args.report_id)
    length = args.length or node.summary.wire_length("feature", args.report_id)
    if not length:
        sys.exit(f"{node.name} has no feature report 0x{args.report_id:02X}; pass --length to force.")
    with make_device(node) as dev:
        data = dev.get_feature(args.report_id, length)
    print(data.hex(" "))


def cmd_send(args):
    data = bytes.fromhex(args.hex.replace(" ", "").replace(":", ""))
    node = _node_with_feature(args, data[0]) if args.feature else _nodes_for(args)[0]
    channel = "feature" if args.feature else "output"
    print(f"{node.name} {channel}: {data.hex(' ')}")
    if not args.execute:
        print("(dry run - add --execute to actually send. Only send bytes you saw the official software send.)")
        return
    with make_device(node) as dev:
        (dev.set_feature if args.feature else dev.write_output)(data)
    print("sent")


# --------------------------------------------------------------- analysis ---


def cmd_devices(args):
    transfers = load_transfers(args.capture, include_in=True)
    print("bus:dev   host->device   total HID transfers")
    for (bus, dev), out_n, all_n in device_table(transfers):
        print(f"{bus:3}:{dev:<4}  {out_n:12}   {all_n}")


def cmd_analyze(args):
    transfers = _load(args.capture, args)
    print(f"# {len(transfers)} HID transfers\n")
    for t in transfers:
        if args.only_out and not t.host_to_device:
            continue
        print(f"{t.index:4}  {t.ts:12.6f}  {t.label():38}  {t.data.hex(' ')}")
    print("\n# Command groups (same channel + first bytes = probably the same command)")
    for g in group_commands(transfers, prefix_len=args.prefix):
        print(f"  {g.count:4}x  {g.signature}  lengths={sorted(g.lengths)}  variants={len(g.variants)}")


def cmd_diff(args):
    a = _load(args.a, args)
    b = _load(args.b, args)
    entries = diff_captures(a, b)
    if not entries:
        print("No differences in host->device traffic.")
        return
    for e in entries:
        if e.op == "changed":
            offs = {o for o, _, _ in e.bytes_changed}
            print(f"~ {e.a.label()}   changed offsets {sorted(offs)}")
            print(f"  A: {hexdump_row(e.a.data, offs)}")
            print(f"  B: {hexdump_row(e.b.data, offs)}")
        elif e.op == "only_a":
            print(f"- only in A: {e.a.label()}  {e.a.data.hex(' ')}")
        else:
            print(f"+ only in B: {e.b.label()}  {e.b.data.hex(' ')}")
    print("\n[xx] = byte that differs. A lone changing byte at the end of a packet is often a checksum;")
    print("confirm with `blazectl checksum`.")


def cmd_checksum(args):
    payloads: dict[str, list[bytes]] = {}
    for path in args.captures:
        for t in _load(path, args):
            if t.host_to_device and (args.report_id is None or t.report_id == args.report_id):
                payloads.setdefault(t.label(), []).append(t.data)
    found = False
    for channel, datas in payloads.items():
        guesses = guess_checksums(datas)
        if guesses:
            found = True
            print(f"{channel}  ({len(set(datas))} distinct packets)")
            for g in guesses[:5]:
                print(f"   {g.describe()}")
    if not found:
        print("No consistent single-byte checksum found (need >= 3 distinct packets on one channel).")


def cmd_extract(args):
    from .replay import make_recipe

    transfers = [t for t in _load(args.capture, args) if t.host_to_device or (args.with_reads and t.kind == "GET_REPORT")]
    recipe = make_recipe(args.name or args.output, transfers, args.usb_id)
    with open(args.output, "w") as f:
        json.dump(recipe, f, indent=2)
    print(f"Wrote {len(recipe['steps'])} steps to {args.output}")


def cmd_replay(args):
    from .replay import load_recipe, replay

    recipe = load_recipe(args.recipe)
    if not args.usb_id and recipe.get("usb_id"):
        args.usb_id = recipe["usb_id"]
    nodes = _nodes_for(args)
    lines = replay(recipe, nodes, execute=args.execute, delay=args.delay)
    for ln in lines:
        print(f"{ln.index:4} {ln.node:9} {ln.action:12} {ln.data.hex(' ')}  {ln.note}")
    if not args.execute:
        print("\n(dry run - add --execute to send)")


# ------------------------------------------------------- instant mice -----


def _instant_node(args):
    from .drivers.instant import VID, find_control_node

    vid, pid = _usb_id(args.usb_id) if args.usb_id else (VID, None)
    node = find_control_node(enumerate_nodes(), vid, pid)
    if node is None:
        sys.exit(
            "No Instant control interface found (feature report 0x07 on usage page 0xFF01).\n"
            "Is the mouse plugged in? Check with `blazectl probe --usb-id 30fa:1440`."
        )
    return node


def cmd_dump(args):
    from .drivers.instant import DPI_BLOCK, STATE_BLOCK, InstantMouse, code_to_dpi, decode_dpi, DPI_CODES

    node = _instant_node(args)
    print(f"# {node.name} {node.usb_id} {node.hid_name}  (read-only)")
    big = args.end - args.start > 0x100

    def progress(done, total):
        if big and (done % 0x100 == 0 or done == total):
            print(f"\r  reading… {done * 100 // total:3d}%", end="", file=sys.stderr, flush=True)

    with make_device(node) as dev:
        mouse = InstantMouse(dev)
        mem = mouse.dump(args.end, start=args.start, progress=progress)
    if big:
        print("", file=sys.stderr)
    zero_run = None
    for addr, blk in mem.items():
        if big and blk == bytes(8):  # collapse long runs of empty memory
            zero_run = zero_run if zero_run is not None else addr
            continue
        if zero_run is not None:
            print(f"0x{zero_run:02x}-0x{addr - 1:02x}: all 00")
            zero_run = None
        print(f"0x{addr:02x}: {blk.hex(' ')}")
    if zero_run is not None:
        print(f"0x{zero_run:02x}-0x{args.end - 1:02x}: all 00")
    if 0 in mem:
        ok, msg = mouse.check_header(mem[0], pid=node.pid)
        print(("OK   " if ok else "WARN ") + msg)
    if DPI_BLOCK in mem:
        codes = decode_dpi(mem[DPI_BLOCK])
        plausible = all(c in DPI_CODES for c in codes)
        print(f"DPI levels: {[code_to_dpi(c, node.pid) or f'code {c}?' for c in codes]}  (codes {codes})"
              f"  ({'plausible' if plausible else 'NOT plausible: layout may differ'})")
    if STATE_BLOCK in mem:
        st = mem[STATE_BLOCK]
        print(f"active DPI level {(st[0] & 7) + 1}, enabled levels mask 0x{st[1] & 0x3F:02x}")
    if args.save:
        with open(args.save, "w") as f:
            json.dump({"usb_id": node.usb_id, "name": node.hid_name,
                       "blocks": {f"0x{a:02x}": b.hex() for a, b in mem.items()}}, f, indent=2)
        print(f"saved backup to {args.save}")


def _load_dump(path):
    with open(path) as f:
        data = json.load(f)
    return {int(k, 16): bytes.fromhex(v) for k, v in data["blocks"].items()}


def cmd_memdiff(args):
    """Compare two memory dumps byte by byte."""
    a, b = _load_dump(args.a), _load_dump(args.b)
    common = sorted(set(a) & set(b))
    changed = [addr for addr in common if a[addr] != b[addr]]
    if not changed:
        print(f"No differences in the {len(common) * 8} bytes both dumps cover.")
        return
    total = 0
    for addr in changed:
        x, y = a[addr], b[addr]
        offs = {i for i in range(8) if x[i] != y[i]}
        total += len(offs)
        print(f"0x{addr:03x}:  " + " ".join(f"[{v:02x}]" if i in offs else f" {v:02x} " for i, v in enumerate(x)))
        print(f"      → " + " ".join(f"[{v:02x}]" if i in offs else f" {v:02x} " for i, v in enumerate(y)))
    print(f"\n{total} byte(s) changed in {len(changed)} block(s); [xx] marks a changed byte.")


def _print_macro(m, indent="    "):
    total = sum(ev.delay for ev in m.events)
    how = "repeats while held" if m.mode == "hold" else f"plays {m.repeat}x"
    print(f"{indent}{len(m.events)} events, {total} ms, {how}")
    for i, ev in enumerate(m.events, 1):
        print(f"{indent}  {i:2}. {ev.name:10} {'down' if ev.down else 'up  '}  then wait {ev.delay} ms")


def _show_binding(num, b):
    from .drivers.macro import Macro
    if isinstance(b, Macro):
        print(f"button {num}:")
        _print_macro(b)
    else:
        print(f"button {num}: {_describe(b)}")


def cmd_macro(args):
    """Show, set or clear macros."""
    from .drivers.macro import Macro, MacroError, parse_script, read_bindings

    if args.dump:
        blocks = _load_dump(args.dump)
        mem = bytearray(0x1000)
        for a, b in blocks.items():
            mem[a:a + 8] = b
        try:
            bound = read_bindings(bytes(mem), {i: mem[0x08 + i] & 0x1F for i in range(8)})
        except MacroError as e:
            sys.exit(f"can't read macros from that dump: {e}")
        from .drivers.instant import BUTTON_SLOTS
        num_of = {idx: num for num, _, blk, idx, _ in BUTTON_SLOTS if blk == 0x08}
        macros = {num_of[s]: m for s, m in bound.items()}
        if not macros:
            print("No buttons use a macro, media key or shortcut.")
        for num, m in sorted(macros.items()):
            _show_binding(num, m)
        return

    from .drivers.instant import BUTTON_SLOTS, InstantError, InstantMouse, MACRO_BUTTONS

    changes = {}
    if args.action in ("set", "clear") and args.button not in MACRO_BUTTONS:
        sys.exit(f"give a button that can hold a macro: {sorted(MACRO_BUTTONS)}")
    if args.action == "set" and not args.script:
        sys.exit('give the macro, e.g. blazectl macro set 4 "a tap 30, b tap 30"')
    if args.action == "set":
        try:
            events = parse_script(args.script)
            changes[args.button] = Macro(events, "hold" if args.hold else "times", args.repeat)
            changes[args.button].validate()
        except MacroError as e:
            sys.exit(f"bad macro: {e}")
    elif args.action == "clear":
        factory = {num: code for num, _, _, _, code in BUTTON_SLOTS}
        changes[args.button] = factory[args.button]
    node = _instant_node(args)
    with make_device(node) as dev:
        mouse = InstantMouse(dev, pid=node.pid)
        try:
            current = mouse.get_macros()
        except InstantError as e:
            sys.exit(str(e))
        if not changes:
            if not current:
                print("No buttons are bound to a macro.")
            for num, m in sorted(current.items()):
                _show_binding(num, m)
            return
        num = args.button
        if num in MACRO_BUTTONS and MACRO_BUTTONS[num] != "confirmed":
            print(f"note: button {num}'s macro slot is predicted from the pattern, not yet seen in an app dump.")
        if args.action == "set":
            print(f"button {num} will play:")
            _print_macro(changes[num])
        else:
            print(f"button {num}: remove its macro, back to its factory function")
        if not args.execute:
            print("(dry run, add --execute to write)")
            return
        try:
            res = mouse.set_macros(changes, pid=node.pid)
        except (InstantError, OSError) as e:
            sys.exit(f"macro not written: {e}")
    print("saved and verified." if res["changed"] else "already set, nothing written")
    if res["changed"]:
        print("note: the mouse reloads, which resets lighting; set it again with `blazectl light ...`.")


def cmd_light(args):
    from .drivers.instant import InstantError, InstantMouse, lighting_packets, quantize

    try:
        pkts = lighting_packets(args.mode, args.color, args.speed, args.brightness, args.direction)
    except InstantError as e:
        sys.exit(str(e))
    if args.color:
        shown = [quantize(c) for c in args.color]
        if shown != [c.lower() if c.startswith("#") else "#" + c.lower() for c in args.color]:
            print(f"note: the mouse has 4 bits per channel, colours will look like {shown}")
    for p in pkts:
        print(p.hex(" "))
    if args.dry_run:
        print("(dry run, nothing sent)")
        return
    node = _instant_node(args)
    with make_device(node) as dev:
        try:
            InstantMouse(dev).set_lighting(mode_name=args.mode, colors=args.color, speed=args.speed,
                                           brightness=args.brightness, direction=args.direction)
        except OSError as e:
            sys.exit(f"Sending failed: {e}")
    print(f"lighting set to {args.mode} on {node.usb_id} {node.hid_name} ({node.name})")


def cmd_dpi(args):
    from .drivers.instant import InstantError, InstantMouse, code_to_dpi, dpi_to_code

    node = _instant_node(args)
    with make_device(node) as dev:
        mouse = InstantMouse(dev, pid=node.pid)
        info = mouse.get_dpi()
        shown = [d if d is not None else f"code {c}?" for d, c in zip(info["dpi"], info["codes"])]
        print(f"current: {shown}  (codes {info['codes']}), active level {info['active'] + 1}")
        if args.active is not None:
            if not args.execute:
                print(f"would make level {args.active} active (dry run, add --execute)")
                return
            try:
                mouse.set_active_level(args.active - 1, pid=node.pid)
            except (InstantError, OSError) as e:
                sys.exit(f"active level not changed: {e}")
            print(f"level {args.active} is now active. note: the mouse reloads, which resets lighting.")
            return
        if not args.set:
            return
        if len(args.set) != 6:
            sys.exit("give exactly 6 DPI values, one per level")
        try:
            codes = [dpi_to_code(d, node.pid) for d in args.set]
        except InstantError as e:
            sys.exit(str(e))
        print(f"new:     {[code_to_dpi(c, node.pid) for c in codes]}  (codes {codes})")
        if not args.execute:
            print("(dry run, add --execute to write. The mouse header is checked first; "
                  "the write is read back and undone on mismatch.)")
            return
        try:
            result = mouse.set_dpi(codes, pid=node.pid)
        except (InstantError, OSError) as e:
            sys.exit(f"DPI not changed: {e}")
    if result["changed"]:
        print(f"saved and verified: {[code_to_dpi(c, node.pid) for c in result['codes']]} DPI")
        print("note: applying DPI reloads the mouse, which resets lighting; run `blazectl light ...` again.")
    else:
        print("already set, nothing written")


def _parse_assignments(items):
    """'7=middle-click' -> code; '4=media:volume-up' -> Media; '5=shortcut:ctrl+c' -> Shortcut."""
    from .drivers.instant import BUTTON_FUNCTIONS
    from .drivers.macro import MEDIA_KEYS, Media, MacroError, Shortcut

    out = {}
    for item in items:
        if "=" not in item:
            sys.exit(f"use BUTTON=FUNCTION, e.g. 7=middle-click (got {item!r})")
        num, func = item.split("=", 1)
        if not num.isdigit():
            sys.exit(f"button must be a number 1-8 (got {num!r})")
        low = func.lower()
        try:
            if low.startswith("media:"):
                out[int(num)] = Media(low[6:])
                out[int(num)].validate()
                continue
            if low.startswith("shortcut:"):
                out[int(num)] = Shortcut.parse(low[9:])
                continue
        except MacroError as e:
            sys.exit(f"button {num}: {e}")
        if func not in BUTTON_FUNCTIONS:
            sys.exit(f"unknown function {func!r}; choose from: {', '.join(BUTTON_FUNCTIONS)}, "
                     f"media:<{'|'.join(MEDIA_KEYS)}>, shortcut:<ctrl+shift+alt+win+KEY>")
        out[int(num)] = BUTTON_FUNCTIONS[func]
    return out


def _describe(binding_or_code):
    from .drivers.instant import FUNCTION_NAMES
    from .drivers.macro import Macro, Media, Shortcut

    b = binding_or_code
    if isinstance(b, Media):
        return f"media: {b.label}"
    if isinstance(b, Shortcut):
        return f"shortcut: {b.label}"
    if isinstance(b, Macro):
        keys = " ".join(dict.fromkeys(ev.name for ev in b.events))
        return f"macro ({'while held' if b.mode == 'hold' else f'{b.repeat}x'}): {keys}"
    return FUNCTION_NAMES.get(b, f"unknown 0x{b:02x}")


def cmd_buttons(args):
    from .drivers.instant import BUTTON_SLOTS, InstantError, InstantMouse
    from .drivers.macro import Macro, Media, Shortcut

    names = {num: name for num, name, *_ in BUTTON_SLOTS}
    defaults = {num: code for num, _, _, _, code in BUTTON_SLOTS}
    node = _instant_node(args)
    with make_device(node) as dev:
        mouse = InstantMouse(dev, pid=node.pid)
        current = mouse.get_buttons()
        try:
            special = mouse.get_macros()
        except InstantError as e:
            special = {}
            print(f"  (couldn't read macros/media/shortcuts: {e})")
        for num in sorted(current):
            print(f"  button {num} ({names[num]:7}): {_describe(special.get(num, current[num]))}")
        changes = {num: code for num, code in defaults.items()} if args.reset else _parse_assignments(args.assign or [])
        if not changes:
            return
        print("change:", ", ".join(f"button {n} -> {_describe(c)}" for n, c in sorted(changes.items())))
        if not args.execute:
            print("(dry run, add --execute to write)")
            return
        bindings = {n: c for n, c in changes.items() if isinstance(c, (Macro, Media, Shortcut))}
        plain = {n: c for n, c in changes.items() if n not in bindings}
        try:
            changed = False
            if bindings:
                changed |= mouse.set_macros(bindings, pid=node.pid)["changed"]
            if plain:
                changed |= mouse.set_buttons(plain, pid=node.pid)["changed"]
        except (InstantError, OSError) as e:
            sys.exit(f"buttons not changed: {e}")
    if changed:
        print("saved and verified. note: the mouse reloads, which resets lighting; set it again with `blazectl light ...`.")
    else:
        print("already set, nothing written")


def cmd_poll(args):
    from .drivers.instant import POLL_HYPOTHESIS, InstantError, InstantMouse

    rate_of = {v: k for k, v in POLL_HYPOTHESIS.items()}
    node = _instant_node(args)
    with make_device(node) as dev:
        mouse = InstantMouse(dev, pid=node.pid)
        cur = mouse.get_polling_byte()
        rate = f"{rate_of[cur]} Hz" if cur in rate_of else f"unknown value 0x{cur:02x}"
        print(f"polling rate: {rate}")
        if args.rate is None:
            return
        if args.rate not in POLL_HYPOTHESIS:
            sys.exit(f"rate must be one of {sorted(POLL_HYPOTHESIS)}")
        value = POLL_HYPOTHESIS[args.rate]
        print(f"new polling rate: {args.rate} Hz")
        if not args.execute:
            print("(dry run, add --execute to write)")
            return
        try:
            res = mouse.set_polling_byte(value, pid=node.pid)
        except (InstantError, OSError) as e:
            sys.exit(f"polling rate not changed: {e}")
    print("saved and verified" if res["changed"] else "already set, nothing written")
    if res["changed"]:
        print("note: the mouse reloads after this, which resets lighting; set it again with `blazectl light ...`.")


def cmd_gui(args):
    if args.classic:
        try:
            from .gui import main as gui_main
        except ImportError as e:
            sys.exit(f"The classic GUI needs tkinter, which this Python doesn't include ({e}).")
        return gui_main()
    from .webui import main as web_main

    web_main(open_window=not args.no_window)


# ---------------------------------------------------------------- profiles ---


def cmd_profile(args):
    store = ProfileStore()
    if args.action == "list":
        for name in store.list():
            print(name)
    elif args.action == "new":
        p = Profile(name=args.name)
        print(f"Created {store.save(p)} - edit it, then `blazectl apply {args.name} --dry-run`.")
    elif args.action == "show":
        print(store.load(args.name).to_json())
    elif args.action == "check":
        errs = store.load(args.name).validate()
        print("valid" if not errs else "\n".join(errs))


def cmd_apply(args):
    from .drivers import BlazeUltraDriver

    profile = ProfileStore().load(args.name)
    driver = BlazeUltraDriver(None)
    try:
        if args.dry_run:
            for r in driver.apply(profile, dry_run=True):
                print(f"{r.description}: {r.sent.hex(' ')}")
            return
        node = _nodes_for(args)[0]
        with make_device(node) as dev:
            driver.transport = dev
            for r in driver.apply(profile, verify=not args.no_verify):
                status = {True: "verified", False: "MISMATCH", None: "sent"}[r.verified]
                print(f"[{status}] {r.description}")
        print(f"Profile '{profile.name}' applied.")
    except NotImplementedError as e:
        sys.exit(str(e))
    except ApplyError as e:
        sys.exit(f"Apply failed: {e}")


# -------------------------------------------------------------------- main ---


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blazectl", description="Linux configurator + RE toolkit for the EvoFox Blaze Ultra")
    p.add_argument("--version", action=_VersionAction, nargs=0, help="show version and copyright")
    sub = p.add_subparsers(dest="cmd", required=True)

    def target(sp):
        sp.add_argument("--node", help="hidraw node, e.g. hidraw3")
        sp.add_argument("--usb-id", help="VID:PID from lsusb, e.g. 30fa:1701")

    def cap_opts(sp):
        sp.add_argument("--device", help="bus:device to analyse (default: auto)")

    sp = sub.add_parser("list", help="list HID devices (no root needed)")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("probe", help="show a device's HID reports (no root needed)")
    target(sp)
    sp.add_argument("--raw", action="store_true", help="also dump the raw descriptor")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("get-feature", help="read a feature report")
    target(sp)
    sp.add_argument("--report-id", type=_int, required=True)
    sp.add_argument("--length", type=_int, help="buffer length incl. report-ID byte")
    sp.set_defaults(func=cmd_get_feature)

    sp = sub.add_parser("send", help="send raw bytes (dry run unless --execute)")
    target(sp)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--feature", action="store_true")
    g.add_argument("--output", action="store_true")
    sp.add_argument("hex", help="bytes incl. report ID, e.g. '08 07 00 ...'")
    sp.add_argument("--execute", action="store_true")
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("devices", help="list USB devices seen in a capture")
    sp.add_argument("capture")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("analyze", help="print HID traffic from a capture")
    sp.add_argument("capture")
    cap_opts(sp)
    sp.add_argument("--include-in", action="store_true", help="include interrupt IN (noisy: mouse movement)")
    sp.add_argument("--only-out", action="store_true", help="show only host->device packets")
    sp.add_argument("--prefix", type=int, default=2, help="bytes used to group commands")
    sp.set_defaults(func=cmd_analyze)

    sp = sub.add_parser("diff", help="diff two captures that differ by one setting")
    sp.add_argument("a")
    sp.add_argument("b")
    cap_opts(sp)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("checksum", help="guess the checksum formula")
    sp.add_argument("captures", nargs="+")
    cap_opts(sp)
    sp.add_argument("--report-id", type=_int)
    sp.set_defaults(func=cmd_checksum)

    sp = sub.add_parser("extract", help="save a capture's config packets as a replayable recipe")
    sp.add_argument("capture")
    sp.add_argument("-o", "--output", required=True)
    sp.add_argument("--name")
    sp.add_argument("--usb-id", help="store VID:PID in the recipe")
    sp.add_argument("--with-reads", action="store_true", help="also keep GET_REPORT reads")
    cap_opts(sp)
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser("replay", help="resend a recipe to the mouse (dry run unless --execute)")
    sp.add_argument("recipe")
    target(sp)
    sp.add_argument("--execute", action="store_true")
    sp.add_argument("--delay", type=float, default=0.02, help="seconds between packets")
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("dump", help="Instant mice: read config memory (read-only) and check the layout")
    sp.add_argument("--usb-id", help="VID:PID, default: any 30fa device")
    sp.add_argument("--start", type=_int, default=0, help="start address (multiple of 8)")
    sp.add_argument("--end", type=_int, default=0x40, help="dump up to this address, e.g. 0x1000")
    sp.add_argument("--save", help="save the dump as a JSON backup")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("memdiff", help="compare two memory dumps (from dump --save)")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.set_defaults(func=cmd_memdiff)

    sp = sub.add_parser("macro", help="Instant mice: show, set or clear keyboard macros",
                        description="Examples:\n"
                        "  blazectl macro                       show macros on the mouse\n"
                        "  blazectl macro set 4 \"ctrl down 10, c tap 20, ctrl up 10\" --execute\n"
                        "  blazectl macro set 7 \"a tap 30\" --hold --execute    repeat while held\n"
                        "  blazectl macro clear 4 --execute    back to the factory function\n"
                        "Script: KEY down|up|tap [ms after], comma separated. Keys: a-z, 0-9, enter, "
                        "space, tab, esc, f1-f12, lctrl, lshift, lalt, up, down, ...",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    sp.add_argument("action", nargs="?", choices=["show", "set", "clear"], default="show")
    sp.add_argument("button", nargs="?", type=int, help="app button number (2-7)")
    sp.add_argument("script", nargs="?", help='e.g. "a down 50, a up 20" or "enter tap 30"')
    sp.add_argument("--repeat", type=int, default=1, help="play N times (1-255), default 1")
    sp.add_argument("--hold", action="store_true", help="repeat while the button is held")
    sp.add_argument("--usb-id", help="VID:PID, default: any 30fa device")
    sp.add_argument("--dump", help="show macros from a saved dump instead of the mouse")
    sp.add_argument("--execute", action="store_true")
    sp.set_defaults(func=cmd_macro)

    sp = sub.add_parser("light", help="Instant mice: set RGB lighting")
    sp.add_argument("mode", help="static, breathing, breathing-multi, rainbow, spectrum, fill, loop, "
                    "enraptured, flicker, ripple, startrek, off")
    sp.add_argument("--color", nargs="+", help="#rrggbb, one for all levels or up to 6 (one per DPI level)")
    sp.add_argument("--speed", type=int, default=2, help="0-5")
    sp.add_argument("--brightness", type=int, default=7, help="0-7")
    sp.add_argument("--direction", type=int, default=0, help="0 or 1")
    sp.add_argument("--usb-id")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_light)

    sp = sub.add_parser("dpi", help="Instant mice: show DPI levels, or set them with --set")
    sp.add_argument("--set", type=int, nargs="+", metavar="DPI", help="six DPI values, e.g. 800 1200 1600 2400 3200 4800")
    sp.add_argument("--active", type=int, choices=range(1, 7), metavar="N", help="make level N (1-6) the active one")
    sp.add_argument("--execute", action="store_true", help="actually write (otherwise dry run)")
    sp.add_argument("--usb-id")
    sp.set_defaults(func=cmd_dpi)

    sp = sub.add_parser("gui", help="open the graphical configurator")
    sp.add_argument("--classic", action="store_true", help="use the simple tkinter window instead")
    sp.add_argument("--no-window", action="store_true", help="just print the local URL")
    sp.set_defaults(func=cmd_gui)

    sp = sub.add_parser("buttons", help="Instant mice: show or remap the 8 buttons")
    sp.add_argument("assign", nargs="*", help="BUTTON=FUNCTION, e.g. 7=middle-click 8=dpi-loop. Functions: "
                    "left-click, middle-click, right-click, back, forward, dpi-loop, dpi-up, dpi-down, "
                    "double-click, show-desktop, rapid-fire, led-loop")
    sp.add_argument("--reset", action="store_true", help="restore factory mapping")
    sp.add_argument("--execute", action="store_true")
    sp.add_argument("--usb-id")
    sp.set_defaults(func=cmd_buttons)

    sp = sub.add_parser("poll", help="Instant mice: show or set polling rate (125/250/500/1000 Hz)")
    sp.add_argument("--rate", type=int, help="125, 250, 500 or 1000")
    sp.add_argument("--execute", action="store_true")
    sp.add_argument("--usb-id")
    sp.set_defaults(func=cmd_poll)

    sp = sub.add_parser("profile", help="manage JSON profiles")
    sp.add_argument("action", choices=["list", "new", "show", "check"])
    sp.add_argument("name", nargs="?", default="default")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("apply", help="apply a profile with the decoded driver")
    sp.add_argument("name")
    target(sp)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-verify", action="store_true", help="skip read-back verification")
    sp.set_defaults(func=cmd_apply)
    return p


class _VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        print(f"blazectl {__version__}\nCopyright (C) 2026 Pranjal Tripathi\n"
              "License GPL-3.0-or-later: <https://www.gnu.org/licenses/gpl-3.0.html>\n"
              "This is free software: you are free to change and share it. There is NO WARRANTY.")
        parser.exit()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except CaptureError as e:
        sys.exit(str(e))
    except (PermissionError, RuntimeError) as e:
        sys.exit(str(e))
    except FileNotFoundError as e:
        sys.exit(f"Not found: {e.filename}")


if __name__ == "__main__":
    main()
