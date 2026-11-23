# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""The "is the EvoFox app running?" check must find the real app and never our own programs.

Bug in 0.3.0: the check matched any process with "blaze" in its name, so the installed
BLAZE Control.exe (the agent runs as "BLAZE Control.exe --agent") paused its own DPI popup,
while the real EvoFox app ("Gaming Mouse 3.0.exe") wasn't recognised at all.
"""

import subprocess
import sys

import pytest

from blazectl import service
from blazectl.service import is_official_app, official_app_running


@pytest.mark.parametrize("name", [
    "Gaming Mouse 3.0.exe",
    "gaming mouse.exe",
    "EvoFoxHelper.exe",
    "BlazeUltraDriver.exe",
])
def test_official_app_names(name):
    assert is_official_app(name)


@pytest.mark.parametrize("name", [
    "BLAZE Control.exe",          # the app and the agent
    "blaze control.exe",
    "blazectl.exe",
    "python.exe",
    "pythonw.exe",
    "chrome.exe",
    "",
])
def test_not_official_app(name):
    assert not is_official_app(name)


TASKLIST = '''"System Idle Process","0","Services","0","8 K"
"explorer.exe","4312","Console","1","120,404 K"
"BLAZE Control.exe","10252","Console","1","40,112 K"
"BLAZE Control.exe","10388","Console","1","38,020 K"
'''


def _fake_tasklist(text):
    def run(cmd, **kw):
        assert cmd[:3] == ["tasklist", "/FO", "CSV"]
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")
    return run


def test_own_app_running_is_not_the_evofox_app(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(service.subprocess, "run", _fake_tasklist(TASKLIST))
    assert official_app_running() is False


def test_evofox_app_running_is_found(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(service.subprocess, "run",
                        _fake_tasklist(TASKLIST + '"Gaming Mouse 3.0.exe","7720","Console","1","25,300 K"\n'))
    assert official_app_running() is True


def test_tasklist_failure_means_not_running(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(cmd, **kw):
        raise OSError("no tasklist")
    monkeypatch.setattr(service.subprocess, "run", boom)
    assert official_app_running() is False


def test_never_runs_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def boom(cmd, **kw):
        raise AssertionError("must not run tasklist")
    monkeypatch.setattr(service.subprocess, "run", boom)
    assert official_app_running() is False
