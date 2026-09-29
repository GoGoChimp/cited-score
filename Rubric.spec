# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['rubric_app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['pystray._win32', 'app', 'connectors', 'startup', 'shortcut', 'watch_store', 'audit_store', 'skills_data', 'skills_install', 'rubric_icon', 'cited_fonts', 'cited_logo_data', 'favicon_data', 'aiseo_audit', 'licence', 'tray', 'tray_actions'],
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
    name='Rubric',
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
    icon=['rubric.ico'],
)
