# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Driver framework: profiles, commands, verify-after-write.

This is where the official software's problems get designed out:

* Every profile is validated before anything is sent (no half-applied configs).
* Each command can be read back and checked, so "saved" means "the mouse
  actually holds it" (bugs 3, 4 and 8 in the README).
* Every failure raises ApplyError naming the exact step and bytes, instead of
  crashing or failing silently (bugs 1 and 8).
* Profiles are plain JSON files, so they can't get lost in a GUI (bug 3).
"""

from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

DPI_MIN, DPI_MAX = 200, 12800  # advertised range for the Blaze Ultra
POLLING_RATES = (125, 250, 500, 1000)
LIGHTING_MODES = ("off", "static", "breathing", "spectrum", "wave")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class ApplyError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Profile model
# --------------------------------------------------------------------------- #


@dataclass
class DpiLevel:
    dpi: int
    color: str = "#ffffff"
    enabled: bool = True


@dataclass
class Lighting:
    mode: str = "static"
    color: str = "#ff0000"
    brightness: int = 100  # percent
    speed: int = 50  # percent


@dataclass
class Profile:
    name: str = "default"
    dpi_levels: list[DpiLevel] = field(
        default_factory=lambda: [DpiLevel(800, "#ff0000"), DpiLevel(1600, "#00ff00"), DpiLevel(3200, "#0000ff")]
    )
    active_dpi: int = 1  # index into dpi_levels
    polling_hz: int = 1000
    lighting: Lighting = field(default_factory=Lighting)
    buttons: dict[str, str] = field(default_factory=dict)  # e.g. {"side_back": "browser_back"}

    def validate(self) -> list[str]:
        errs = []
        if not self.dpi_levels:
            errs.append("at least one DPI level is required")
        for i, lvl in enumerate(self.dpi_levels):
            if not DPI_MIN <= lvl.dpi <= DPI_MAX:
                errs.append(f"dpi_levels[{i}]: {lvl.dpi} outside {DPI_MIN}-{DPI_MAX}")
            if not _HEX.match(lvl.color):
                errs.append(f"dpi_levels[{i}]: color {lvl.color!r} is not #rrggbb")
        if not 0 <= self.active_dpi < len(self.dpi_levels):
            errs.append(f"active_dpi {self.active_dpi} is not a valid level index")
        if self.polling_hz not in POLLING_RATES:
            errs.append(f"polling_hz must be one of {POLLING_RATES}")
        if self.lighting.mode not in LIGHTING_MODES:
            errs.append(f"lighting.mode must be one of {LIGHTING_MODES}")
        if not _HEX.match(self.lighting.color):
            errs.append(f"lighting.color {self.lighting.color!r} is not #rrggbb")
        for name, v in (("brightness", self.lighting.brightness), ("speed", self.lighting.speed)):
            if not 0 <= v <= 100:
                errs.append(f"lighting.{name} must be 0-100")
        return errs

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        return cls(
            name=d.get("name", "default"),
            dpi_levels=[DpiLevel(**x) for x in d.get("dpi_levels", [])],
            active_dpi=d.get("active_dpi", 0),
            polling_hz=d.get("polling_hz", 1000),
            lighting=Lighting(**d.get("lighting", {})),
            buttons=dict(d.get("buttons", {})),
        )

    @classmethod
    def from_json(cls, text: str) -> "Profile":
        return cls.from_dict(json.loads(text))


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


class ProfileStore:
    def __init__(self, root: str | None = None):
        base = os.environ.get("XDG_CONFIG_HOME") or os.environ.get("APPDATA") or os.path.expanduser("~/.config")
        self.root = root or os.path.join(base, "blazectl", "profiles")

    def path(self, name: str) -> str:
        if not re.match(r"^[\w.-]+$", name):
            raise ValueError(f"invalid profile name {name!r}")
        return os.path.join(self.root, f"{name}.json")

    def save(self, profile: Profile) -> str:
        os.makedirs(self.root, exist_ok=True)
        p = self.path(profile.name)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write(profile.to_json())
        os.replace(tmp, p)  # atomic: never leaves a half-written profile
        return p

    def load(self, name: str) -> Profile:
        with open(self.path(name)) as f:
            return Profile.from_json(f.read())

    def list(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(f[:-5] for f in os.listdir(self.root) if f.endswith(".json"))


# --------------------------------------------------------------------------- #
# Commands and drivers
# --------------------------------------------------------------------------- #


class Transport(Protocol):
    def set_feature(self, data: bytes) -> int: ...
    def get_feature(self, report_id: int, length: int) -> bytes: ...
    def write_output(self, data: bytes) -> int: ...
    def read_input(self, timeout: float = 0.5, size: int = 64) -> bytes | None: ...


@dataclass
class Command:
    description: str
    channel: str  # "feature" | "output"
    data: bytes
    readback: tuple[int, int] | None = None  # (report_id, length) to GET_FEATURE afterwards
    verify: Callable[[bytes], bool] | None = None
    delay: float = 0.0


@dataclass
class StepResult:
    description: str
    sent: bytes
    verified: bool | None
    response: bytes | None = None


class MouseDriver(ABC):
    name = "generic"
    vid: int | None = None
    pid: int | None = None

    def __init__(self, transport: Transport | None):
        self.transport = transport

    @abstractmethod
    def encode_profile(self, profile: Profile) -> list[Command]:
        """Translate a Profile into the exact packets the mouse expects."""

    def apply(self, profile: Profile, verify: bool = True, dry_run: bool = False) -> list[StepResult]:
        errs = profile.validate()
        if errs:
            raise ApplyError("profile is invalid, nothing was sent:\n  - " + "\n  - ".join(errs))
        commands = self.encode_profile(profile)
        results: list[StepResult] = []
        for n, cmd in enumerate(commands, 1):
            step = f"step {n}/{len(commands)} ({cmd.description})"
            if dry_run:
                results.append(StepResult(cmd.description, cmd.data, None))
                continue
            if self.transport is None:
                raise ApplyError("no device connected")
            try:
                if cmd.channel == "feature":
                    self.transport.set_feature(cmd.data)
                else:
                    self.transport.write_output(cmd.data)
            except OSError as e:
                raise ApplyError(
                    f"{step} failed to send: {e}. Steps 1-{n - 1} were applied; the mouse may hold a "
                    "partial config. Re-apply the profile or your last known-good one."
                ) from e
            if cmd.delay:
                time.sleep(cmd.delay)
            response, ok = None, None
            if verify and cmd.readback is not None:
                rid, length = cmd.readback
                try:
                    response = self.transport.get_feature(rid, length)
                except OSError as e:
                    raise ApplyError(f"{step} was sent but reading it back failed: {e}") from e
                ok = cmd.verify(response) if cmd.verify else response[: len(cmd.data)] == cmd.data
                if not ok:
                    raise ApplyError(
                        f"{step} did NOT stick: the mouse accepted the packet but reads back differently.\n"
                        f"  sent:      {cmd.data.hex(' ')}\n  read back: {response.hex(' ')}"
                    )
            results.append(StepResult(cmd.description, cmd.data, ok, response))
        return results
