# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Network Fix GUI (standalone DNS/DHCP tool).

Build with:
    pyinstaller network_fix.spec --clean --noconfirm
"""

import os

block_cipher = None
_HERE = os.path.abspath(SPECPATH if os.path.isdir(SPECPATH) else os.path.dirname(SPECPATH))

a = Analysis(
    [os.path.join(_HERE, "network_fix_gui.py")],
    pathex=[_HERE],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="NetworkFix",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
