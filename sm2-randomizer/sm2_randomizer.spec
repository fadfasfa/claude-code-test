# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

project_root = Path(SPEC).resolve().parent
release_root = project_root / "dist" / "sm2-randomizer-win"

if not release_root.exists():
    raise SystemExit("dist/sm2-randomizer-win 不存在，请先运行: python build_release.py package-release --skip-refresh")

added_data = [
    (str(release_root / "static"), "static"),
    (str(release_root / "data"), "data"),
    (str(release_root / "assets"), "assets"),
]

block_cipher = None

a = Analysis(
    [str(project_root / "serve_static.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=added_data,
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
    name='sm2-randomizer',
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
