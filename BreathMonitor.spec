# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

bleak_datas, bleak_binaries, bleak_hiddenimports = collect_all("bleak")

analysis = Analysis(
    ["main.py"],
    pathex=[],
    binaries=bleak_binaries,
    datas=bleak_datas + [
        ("assets/app_icon.png", "assets"),
        ("THIRD_PARTY_NOTICES.md", "."),
    ],
    hiddenimports=bleak_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "PIL", "tkinter"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="BreathMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/app_icon.ico",
    version="windows_version_info.txt",
)

bundle = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BreathMonitor",
)
