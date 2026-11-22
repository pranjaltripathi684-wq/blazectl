# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from blazectl.cli import main
from fakes import capture_dpi


@pytest.fixture
def caps(tmp_path):
    paths = {}
    for name, code in (("dpi_800", 0x0F), ("dpi_1600", 0x2F), ("dpi_3200", 0x5E)):
        p = tmp_path / f"{name}.pcapng"
        capture_dpi(p, code)
        paths[name] = str(p)
    return paths


def test_devices_and_analyze(caps, capsys):
    main(["devices", caps["dpi_800"]])
    out = capsys.readouterr().out
    assert "1:5" in out and "1:3" in out
    main(["analyze", caps["dpi_800"], "--only-out"])
    out = capsys.readouterr().out
    assert "using bus 1 device 5" in out
    assert "SET_REPORT feature id=0x08 if=1" in out
    assert "Command groups" in out


def test_diff_cli(caps, capsys):
    main(["diff", caps["dpi_800"], caps["dpi_1600"]])
    out = capsys.readouterr().out
    assert "changed offsets [4, 16]" in out
    assert "[0f]" in out and "[2f]" in out


def test_checksum_cli(caps, capsys):
    main(["checksum", caps["dpi_800"], caps["dpi_1600"], caps["dpi_3200"], "--report-id", "8"])
    out = capsys.readouterr().out
    assert "byte[16] = (0x55 - sum(bytes[0:16])) & 0xFF" in out


def test_extract_and_dry_replay_profile(caps, tmp_path, capsys, monkeypatch):
    recipe = tmp_path / "r.json"
    main(["extract", caps["dpi_800"], "-o", str(recipe), "--usb-id", "30fa:1701"])
    data = json.loads(recipe.read_text())
    assert data["usb_id"] == "30fa:1701" and len(data["steps"]) == 3

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    main(["profile", "new", "fps"])
    main(["profile", "check", "fps"])
    assert "valid" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="isn't decoded yet"):
        main(["apply", "fps", "--dry-run"])


def test_missing_file_is_friendly(capsys):
    with pytest.raises(SystemExit, match="Not found"):
        main(["analyze", "/nonexistent.pcapng"])
