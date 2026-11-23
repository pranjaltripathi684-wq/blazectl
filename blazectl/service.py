# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""High-level mouse operations shared by the GUI (and usable from scripts).

Each call opens the device, does its work, and closes it again, so the GUI never
holds the mouse open. After any memory write the mouse reloads and drops its
lighting, so the last lighting the user chose is re-applied automatically.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field

from .backend import enumerate_nodes, make_device
from .drivers import instant as ins
from .drivers.macro import Macro, MacroError, binding_from_json


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "blazectl")


@dataclass
class LightingSettings:
    mode: str = "static"
    colors: list[str] = field(default_factory=lambda: ["#ff0000"] * ins.DPI_LEVELS)
    speed: int = 2
    brightness: int = ins.BRIGHTNESS_MAX
    direction: int = 0

    def packets(self) -> list[bytes]:
        return ins.lighting_packets(self.mode, self.colors, self.speed, self.brightness, self.direction)


@dataclass
class MouseState:
    name: str
    usb_id: str
    dpi: list[int | None]
    dpi_codes: list[int]
    active_level: int
    polling_hz: int | None
    buttons: dict[int, int]
    macros: dict[int, Macro] = field(default_factory=dict)
    macro_error: str | None = None


class ServiceError(Exception):
    pass


# The official app runs as "Gaming Mouse 3.0.exe" (installed in "EvoFox Blaze Gaming Mouse"); its helpers
# may carry "EvoFox" or "Blaze" in their names. Our own programs ("BLAZE Control.exe", which is also what
# the BLAZE Agent runs as) must never count, or the agent would pause itself and the app would warn about itself.
OWN_PROGRAMS = ("blaze control", "blazectl")


def is_official_app(image: str) -> bool:
    """Does this process name (as tasklist shows it, e.g. "Gaming Mouse 3.0.exe") look like the EvoFox app?"""
    name = image.strip().lower()
    if not name or name.startswith(OWN_PROGRAMS):
        return False
    return name.startswith("gaming mouse") or "evofox" in name or "blaze" in name


def _process_names() -> list[str]:
    # CSV keeps full names (the table view cuts them at 25 characters);
    # CREATE_NO_WINDOW: no console flash when running inside the windowed app
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return [row[0] for row in csv.reader(out.splitlines()) if row]


def official_app_running() -> bool:
    """Best effort: the EvoFox app re-applies its own profile when it's running (Windows only)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        names = _process_names()
    except Exception:
        return False
    return any(is_official_app(n) for n in names)


class MouseService:
    def __init__(self, nodes_fn=enumerate_nodes, device_fn=make_device, settings_path: str | None = None, sleep=None):
        self._nodes_fn = nodes_fn
        self._device_fn = device_fn
        self._sleep = sleep
        self.settings_path = settings_path or os.path.join(config_dir(), "lighting.json")

    # -- plumbing -----------------------------------------------------------

    def _node(self):
        try:
            nodes = self._nodes_fn()
        except RuntimeError as e:   # e.g. hidapi missing on Windows: show the install hint, don't crash
            raise ServiceError(str(e)) from e
        node = ins.find_control_node(nodes)
        if node is None:
            raise ServiceError("Mouse not found. Is it plugged in? Close the EvoFox app if it's running.")
        return node

    def _run(self, fn):
        node = self._node()
        try:
            with self._device_fn(node) as dev:
                kw = {"pid": node.pid}
                if self._sleep is not None:
                    kw["sleep"] = self._sleep
                return fn(ins.InstantMouse(dev, **kw), node)
        except (ins.InstantError, OSError) as e:
            raise ServiceError(str(e)) from e

    # -- reading ------------------------------------------------------------

    def read_state(self) -> MouseState:
        def go(m, node):
            ok, msg = m.check_header(pid=node.pid)
            if not ok:
                raise ServiceError(msg)
            dpi = m.get_dpi()
            poll = {v: k for k, v in ins.POLL_RATES.items()}.get(m.get_polling_byte())
            macros, merr = {}, None
            try:
                macros = m.get_macros()
            except ins.InstantError as e:   # don't let an odd macro area hide everything else
                merr = str(e)
            return MouseState(
                name=node.hid_name, usb_id=node.usb_id, dpi=dpi["dpi"], dpi_codes=dpi["codes"],
                active_level=dpi["active"], polling_hz=poll, buttons=m.get_buttons(),
                macros=macros, macro_error=merr,
            )
        return self._run(go)

    def backup(self, path: str) -> str:
        def go(m, node):
            mem = m.dump(0x800)   # settings + macro area
            with open(path, "w") as f:
                json.dump({"usb_id": node.usb_id, "name": node.hid_name,
                           "blocks": {f"0x{a:02x}": b.hex() for a, b in mem.items()}}, f, indent=2)
            return path
        return self._run(go)

    # -- lighting -----------------------------------------------------------

    def load_lighting(self) -> LightingSettings:
        try:
            with open(self.settings_path) as f:
                return LightingSettings(**json.load(f))
        except (OSError, ValueError, TypeError):
            return LightingSettings()

    def save_lighting(self, s: LightingSettings) -> None:
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        tmp = self.settings_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(s), f, indent=2)
        os.replace(tmp, self.settings_path)

    def set_lighting(self, s: LightingSettings) -> None:
        s.packets()  # validate before touching the device
        self._run(lambda m, node: m.set_lighting(mode_name=s.mode, colors=s.colors, speed=s.speed,
                                                 brightness=s.brightness, direction=s.direction))
        self.save_lighting(s)

    def _relight(self) -> None:
        """Memory writes reload the mouse, which drops lighting: put the user's back."""
        s = self.load_lighting()
        self._run(lambda m, node: m.set_lighting(mode_name=s.mode, colors=s.colors, speed=s.speed,
                                                 brightness=s.brightness, direction=s.direction))

    restore_lighting = _relight

    def preview_lighting(self, s: LightingSettings) -> None:
        """Show lighting without saving it as the user's choice (alerts). RAM only: no flash writes."""
        s.packets()
        self._run(lambda m, node: m.set_lighting(mode_name=s.mode, colors=s.colors, speed=s.speed,
                                                 brightness=s.brightness, direction=s.direction))

    def read_active(self, with_values: bool = False) -> tuple[int, int, list[int | None] | None]:
        """Cheap read for the DPI popup: (active level 0-7 as read, enabled-levels mask, DPI values or None).
        Read-only. Callers must check the level is valid: a garbled read can return anything."""
        def go(m, node):
            st = m.read_block(ins.STATE_BLOCK)
            vals = None
            if with_values:
                vals = [ins.code_to_dpi(c, node.pid) for c in ins.decode_dpi(m.read_block(ins.DPI_BLOCK))]
            return st[0] & 7, st[1] & 0x3F, vals
        return self._run(go)

    # -- memory settings ------------------------------------------------------

    def set_dpi(self, values: list[int]) -> list[int]:
        if len(values) != ins.DPI_LEVELS:
            raise ServiceError(f"need {ins.DPI_LEVELS} DPI values")
        node = self._node()
        try:
            codes = [ins.dpi_to_code(v, node.pid) for v in values]
        except ins.InstantError as e:
            raise ServiceError(str(e)) from e
        res = self._run(lambda m, n: m.set_dpi(codes, pid=n.pid))
        if res["changed"]:
            self._relight()
        return [ins.code_to_dpi(c, node.pid) for c in res["codes"]]

    def set_active_level(self, level: int) -> int:
        """level is 1-6 (as shown to users)."""
        if not 1 <= int(level) <= ins.DPI_LEVELS:
            raise ServiceError(f"level must be 1-{ins.DPI_LEVELS}")
        self._run(lambda m, n: m.set_active_level(int(level) - 1, pid=n.pid))
        self._relight()  # a reload always happened, so lighting needs putting back
        return int(level)

    def set_polling(self, rate: int) -> bool:
        if rate not in ins.POLL_RATES:
            raise ServiceError(f"polling rate must be one of {sorted(ins.POLL_RATES)}")
        res = self._run(lambda m, n: m.set_polling_byte(ins.POLL_RATES[rate], pid=n.pid))
        if res["changed"]:
            self._relight()
        return res["changed"]

    def set_buttons(self, mapping: dict[int, int]) -> dict[int, int]:
        res = self._run(lambda m, n: m.set_buttons(mapping, pid=n.pid))
        if res["changed"]:
            self._relight()
        return res["buttons"]

    def set_macros(self, bind: dict, unbind: dict[int, int]) -> dict:
        """bind: button -> Macro | Shortcut | Media; unbind: button -> plain function code. One verified write."""
        changes: dict = {**unbind, **bind}
        if set(bind) & set(unbind):
            raise ServiceError("a button can't be bound and unbound in the same call")
        res = self._run(lambda m, n: m.set_macros(changes, pid=n.pid))
        if res["changed"]:
            self._relight()
        return res["macros"]

    def reset_buttons(self) -> dict[int, int]:
        return self.set_buttons({num: code for num, _, _, _, code in ins.BUTTON_SLOTS})

    def apply_config(self, config: dict) -> list[str]:
        """Write a whole profile (as ProfileStore stores it) in ONE device session.

        Only settings that differ from the mouse are written, so switching between profiles that
        share DPI/buttons costs no flash writes at all; lighting lives in RAM and is always sent.
        The mouse reloads once at the end (if anything was stored). Returns what changed.
        """
        cfg = ProfileStore.validate(config)
        light = LightingSettings(**cfg["lighting"])
        light.packets()                                   # validate before touching the device

        def go(m, node):
            ok, msg = m.check_header(pid=node.pid)
            if not ok:
                raise ServiceError(msg)
            done: list[str] = []
            try:
                current_special = m.get_macros()
            except ins.InstantError as e:
                raise ServiceError(f"can't read the macros on the mouse, not switching: {e}") from e
            current_codes = m.get_buttons()
            bind, unbind, plain = {}, {}, {}
            for num in sorted(current_codes):
                want_fn = cfg["buttons"].get(str(num))
                want_b = cfg["macros"].get(str(num))
                have_b = current_special.get(num)
                if want_b is not None:
                    b = binding_from_json(want_b)
                    if have_b is None or have_b.to_json() != b.to_json():
                        bind[num] = b
                elif want_fn is None:
                    continue                              # profile doesn't mention this button
                elif want_fn not in ins.BUTTON_FUNCTIONS:
                    raise ServiceError(f"profile: button {num} = {want_fn!r} has no macro/media/shortcut data")
                elif have_b is not None:
                    unbind[num] = ins.BUTTON_FUNCTIONS[want_fn]
                elif current_codes[num] != ins.BUTTON_FUNCTIONS[want_fn]:
                    plain[num] = ins.BUTTON_FUNCTIONS[want_fn]
            if bind or unbind:
                if m.set_macros({**unbind, **bind}, pid=node.pid, apply_now=False)["changed"]:
                    done.append("macros")
            if plain:
                if m.set_buttons(plain, pid=node.pid, apply_now=False)["changed"]:
                    done.append("buttons")
            codes = [ins.dpi_to_code(v, node.pid) for v in cfg["dpi"]]
            if m.set_dpi(codes, pid=node.pid, apply_now=False)["changed"]:
                done.append("dpi")
            if cfg["polling"] not in ins.POLL_RATES:
                raise ServiceError(f"profile: polling rate {cfg['polling']} is not supported")
            if m.set_polling_byte(ins.POLL_RATES[cfg["polling"]], pid=node.pid, apply_now=False)["changed"]:
                done.append("polling")
            if done:
                m.reload()
            m.set_lighting(mode_name=light.mode, colors=light.colors, speed=light.speed,
                           brightness=light.brightness, direction=light.direction)
            return done

        try:
            done = self._run(go)
        except ins.InstantError as e:
            raise ServiceError(str(e)) from e
        self.save_lighting(light)
        return done


PROFILE_SLOTS = ("office", "game1", "game2", "media")


class ProfileStore:
    """Full configurations kept on the PC (the official app's profile switching isn't decoded)."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(config_dir(), "profiles.json")

    def load(self) -> dict:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
        active = data.get("active") if data.get("active") in PROFILE_SLOTS else "office"
        return {"active": active, "slots": {k: slots.get(k) for k in PROFILE_SLOTS}}

    @staticmethod
    def validate(config: dict) -> dict:
        try:
            dpi = [int(v) for v in config["dpi"]]
            if len(dpi) != ins.DPI_LEVELS:
                raise ValueError("dpi must have 6 values")
            poll = int(config["polling"])
            buttons = {str(int(k)): str(v) for k, v in dict(config["buttons"]).items()}
            light = dict(config["lighting"])
            lighting = LightingSettings(
                mode=str(light["mode"]), colors=[str(c) for c in light["colors"]][: ins.DPI_LEVELS],
                speed=int(light["speed"]), brightness=int(light["brightness"]), direction=int(light["direction"]),
            )
            macros = {str(int(k)): binding_from_json(v).to_json() for k, v in dict(config.get("macros") or {}).items()}
        except (KeyError, TypeError, ValueError, MacroError) as e:
            raise ServiceError(f"invalid profile: {e}") from e
        return {"dpi": dpi, "polling": poll, "buttons": buttons, "lighting": asdict(lighting), "macros": macros}

    def save(self, slot: str, config: dict | None, active: str | None = None) -> dict:
        if slot not in PROFILE_SLOTS:
            raise ServiceError(f"unknown profile slot {slot!r}")
        data = self.load()
        if config is not None:
            data["slots"][slot] = self.validate(config)
        if active is not None:
            if active not in PROFILE_SLOTS:
                raise ServiceError(f"unknown profile slot {active!r}")
            data["active"] = active
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)
        return data


class MacroLibrary:
    """Named macros kept on the PC (the mouse stores macros without names)."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(config_dir(), "macros.json")

    def load(self) -> dict[str, dict]:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for name, d in (data.get("macros") or {}).items() if isinstance(data, dict) else []:
            try:
                out[str(name)] = Macro.from_json(d).to_json()
            except (MacroError, KeyError, TypeError, ValueError):
                continue   # skip a damaged entry rather than losing the whole library
        return out

    def _write(self, macros: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"macros": macros}, f, indent=2)
        os.replace(tmp, self.path)

    def save(self, name: str, macro: Macro, old_name: str | None = None) -> dict:
        name = name.strip()
        if not name or len(name) > 40:
            raise ServiceError("macro name must be 1-40 characters")
        lib = self.load()
        if old_name and old_name != name:
            lib.pop(old_name, None)
        lib[name] = macro.to_json()
        self._write(lib)
        return lib

    def delete(self, name: str) -> dict:
        lib = self.load()
        lib.pop(name, None)
        self._write(lib)
        return lib


def button_choices(num: int) -> dict[str, int]:
    """Functions offered for a button in the GUI (button 8 is limited by firmware)."""
    allowed = ins.BUTTON_ALLOWED.get(num)
    return {name: code for name, code in ins.BUTTON_FUNCTIONS.items() if allowed is None or code in allowed}
