# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""The desktop app wrapper (BLAZE Control): selftest, version, single instance."""
import sys

import pytest

from blazectl import app


@pytest.fixture(autouse=True)
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))


def test_selftest_serves_page_and_api(tmp_path, capsys):
    out = tmp_path / "report.txt"
    assert app.main(["--selftest", str(out)]) == 0
    text = out.read_text()
    assert "page ok, api ok" in text and "BLAZE Control" in text
    assert "page ok" in capsys.readouterr().out


def test_version_shows_copyright(capsys):
    assert app.main(["--version"]) == 0
    out = capsys.readouterr().out
    assert "BLAZE Control" in out and "Pranjal Tripathi" in out and "GPL-3.0" in out


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows uses a named mutex, tested in CI's app build")
def test_only_one_copy_may_run():
    first = app.SingleInstance()
    second = app.SingleInstance()
    assert first.ok and not second.ok
