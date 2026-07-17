# -*- mode: python ; coding: utf-8 -*-
# Builds the windowed GUI as a onedir distribution (see R2-9 in
# docs/ai/todolist.md). Onedir rather than onefile: faster startup and less
# likely to trip Defender/SmartScreen heuristics than a self-extracting
# single exe. Run from the repo root:
#   pyinstaller k2s_gui.spec
# Only k2s_gui_entry.py is bundled -- NOT the CLI entry point, whose default
# captcha callback (Image.show() + input()) has no stdin in a windowed app
# and would hang. See src/k2s_downloader/core/k2s_client.py.

a = Analysis(
    ['k2s_gui_entry.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('resources/style.qss', 'resources'),
        ('src/assets/icon/icon.ico', 'assets/icon'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='K2SDownloaderm',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='src/assets/icon/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='K2SDownloaderm',
)
