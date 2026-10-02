# -*- mode: python ; coding: utf-8 -*-

# The local MCP (mcp_server -> `from mcp.server.fastmcp import FastMCP`) must be bundled so that
# `Rubric.exe --mcp` can serve it over stdio. collect_all pulls the mcp SDK's submodules, data and
# binaries into the onefile build; without this the --mcp path raises ModuleNotFoundError at runtime.
from PyInstaller.utils.hooks import collect_all

_mcp_datas, _mcp_binaries, _mcp_hidden = collect_all('mcp')

_HIDDEN = ['pystray._win32', 'app', 'connectors', 'startup', 'shortcut', 'watch_store', 'audit_store',
           'skills_data', 'skills_install', 'rubric_icon', 'cited_fonts', 'cited_logo_data',
           'favicon_data', 'aiseo_audit', 'licence', 'tray', 'tray_actions', 'mcp_server'] + _mcp_hidden

a = Analysis(
    ['rubric_app.py'],
    pathex=[],
    binaries=_mcp_binaries,
    datas=_mcp_datas,
    hiddenimports=_HIDDEN,
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
    upx=False,  # UPX-packed exes draw antivirus false positives and complicate code signing; off for a shipped build.
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
