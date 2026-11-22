# blazectl - open-source configurator for the EvoFox Blaze Ultra mouse
# Copyright (C) 2026 Pranjal Tripathi
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write build/version_info.txt: the details Windows shows under the exe's Properties > Details."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blazectl import __version__  # noqa: E402

nums = tuple(int(x) for x in __version__.split(".")[:3]) + (0,)
text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Pranjal Tripathi'),
      StringStruct('FileDescription', 'BLAZE Control - EvoFox Blaze Ultra configurator'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'BLAZE Control'),
      StringStruct('LegalCopyright', 'Copyright (C) 2026 Pranjal Tripathi. GPL-3.0-or-later.'),
      StringStruct('OriginalFilename', 'BLAZE Control.exe'),
      StringStruct('ProductName', 'BLAZE Control'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(os.path.join(root, "build"), exist_ok=True)
with open(os.path.join(root, "build", "version_info.txt"), "w", encoding="utf-8") as f:
    f.write(text)
print("wrote build/version_info.txt for", __version__)
