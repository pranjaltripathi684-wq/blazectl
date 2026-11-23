# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cross-process locks.

The mouse's memory protocol is a sequence (write bytes, commit, idle). If two programs talk to it at
the same time (the GUI, the background agent, a CLI command), their packets could interleave and the
mouse would store a mix. So every conversation with the mouse holds one machine-wide lock.

Windows: named mutexes (kernel objects, released automatically if a process dies).
Linux/macOS: flock() on a file in the config folder (also released when the process dies).
"""
from __future__ import annotations

import os
import sys
import threading
import time

DEVICE_LOCK = "device"
GUI_LOCK = "gui"
AGENT_LOCK = "agent"


class LockTimeout(OSError):
    pass


def _lock_dir() -> str:
    from .service import config_dir
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    return d


class _Win32Mutex:
    WAIT_OBJECT_0, WAIT_ABANDONED, WAIT_TIMEOUT = 0x0, 0x80, 0x102

    def __init__(self, name: str):
        import ctypes
        from ctypes import wintypes

        self._k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k.CreateMutexW.restype = wintypes.HANDLE
        self._k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        self._k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self._k.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self._k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.name = f"Local\\blazectl.{name}"
        self.handle = self._k.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), f"CreateMutex {self.name} failed")

    def acquire(self, timeout: float | None) -> bool:
        ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        r = self._k.WaitForSingleObject(self.handle, ms)
        return r in (self.WAIT_OBJECT_0, self.WAIT_ABANDONED)   # abandoned = previous owner crashed

    def release(self) -> None:
        self._k.ReleaseMutex(self.handle)

    def close(self) -> None:
        if self.handle:
            self._k.CloseHandle(self.handle)
            self.handle = None


class _FileLock:
    def __init__(self, name: str, directory: str | None = None):
        self.path = os.path.join(directory or _lock_dir(), f"{name}.lock")
        self.fh = None

    def acquire(self, timeout: float | None) -> bool:
        import fcntl
        if self.fh is None:
            self.fh = open(self.path, "a+")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                time.sleep(0.02)

    def release(self) -> None:
        import fcntl
        if self.fh is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)

    def close(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def _native(name: str, directory: str | None = None):
    if sys.platform.startswith("win"):
        return _Win32Mutex(name)
    return _FileLock(name, directory)


class NamedLock:
    """A machine-wide lock that is also re-entrant inside one process (thread-safe)."""

    def __init__(self, name: str, directory: str | None = None):
        self.name = name
        self._dir = directory
        self._native = None
        self._tlock = threading.RLock()
        self._depth = 0

    def acquire(self, timeout: float | None = 10.0) -> None:
        if not self._tlock.acquire(timeout=-1 if timeout is None else timeout):
            raise LockTimeout(f"timed out waiting for the {self.name} lock")
        if self._depth == 0:
            try:
                if self._native is None:
                    self._native = _native(self.name, self._dir)
                if not self._native.acquire(timeout):
                    raise LockTimeout(
                        "The mouse is busy in another blazectl program (the app, the agent or a command). "
                        "Try again in a moment.")
            except BaseException:
                self._tlock.release()
                raise
        self._depth += 1

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._native is not None:
            self._native.release()
        self._tlock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


_locks: dict[str, NamedLock] = {}
_locks_guard = threading.Lock()


def get_lock(name: str = DEVICE_LOCK) -> NamedLock:
    with _locks_guard:
        if name not in _locks:
            _locks[name] = NamedLock(name)
        return _locks[name]


def device_lock() -> NamedLock:
    return get_lock(DEVICE_LOCK)


class Presence:
    """'I am running' marker (the GUI and the agent use it) that another process can test."""

    def __init__(self, name: str, directory: str | None = None):
        self.name = name
        self._dir = directory
        self._native = None

    def claim(self) -> bool:
        """Take the marker. False if another process already holds it."""
        self._native = _native(self.name, self._dir)
        if not self._native.acquire(0):
            self._native.close()
            self._native = None
            return False
        return True

    def drop(self) -> None:
        if self._native is not None:
            self._native.release()
            self._native.close()
            self._native = None


def is_running(name: str, directory: str | None = None) -> bool:
    """True if another process holds the `name` presence marker right now."""
    probe = _native(name, directory)
    try:
        if probe.acquire(0):
            probe.release()
            return False
        return True
    finally:
        probe.close()
