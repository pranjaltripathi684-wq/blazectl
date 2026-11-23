# -*- mode: python ; coding: utf-8 -*-
# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Build "BLAZE Control" (Windows):
#   python packaging/make_version_info.py
#   pyinstaller --noconfirm --clean packaging/blaze_control.spec
# Output: dist/BLAZE Control/BLAZE Control.exe  (one folder: starts fast, fewer antivirus false alarms)
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

here = os.path.abspath(SPECPATH)
root = os.path.dirname(here)

datas = [(os.path.join(root, "blazectl", "web", "index.html"), os.path.join("blazectl", "web")),
         (os.path.join(root, "LICENSE"), ".")]
datas += collect_data_files("webview")
binaries = collect_dynamic_libs("webview")

a = Analysis(
    [os.path.join(here, "launcher.py")],
    pathex=[root],
    binaries=binaries,
    datas=datas,
    hiddenimports=["hid", "clr", "webview.platforms.edgechromium", "webview.platforms.winforms",
                   "tkinter", "blazectl.agent", "blazectl.overlay", "blazectl.foreground", "blazectl.audit"],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
version_file = os.path.join(root, "build", "version_info.txt")
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BLAZE Control",
    console=False,
    icon=os.path.join(here, "blaze.ico"),
    version=version_file if os.path.exists(version_file) else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="BLAZE Control")
