# -*- mode: python ; coding: utf-8 -*-

import re

with open('relay_server.py', encoding='utf-8') as _f:
    _version = re.search(r'RELAY_VERSION\s*=\s*"([^"]+)"', _f.read()).group(1)

a = Analysis(
    ['relay_server.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('mobile_roll_call.html', '.'),
        ('signup.html', '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f'relay_server_v{_version}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
