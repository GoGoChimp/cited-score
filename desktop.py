#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CITED Score - desktop launcher.
Starts the local server on a free port and shows it in a native window (pywebview /
Edge WebView2 on Windows). This is the entry point PyInstaller bundles into CITED-Score.exe.

Also serves the local MCP server over stdio when launched as `CITED-Score.exe --mcp`
(so hosts like Claude Desktop / Cursor can point their MCP config straight at the exe -
no separate Python needed). NOTE: for the frozen exe this requires the `mcp` SDK to be
bundled in the PyInstaller spec (hiddenimports / --collect-all mcp); until the exe is
rebuilt with it, the --mcp path works from a Python run, which is what the Connect button
uses on a dev machine.
"""
import os, sys, tempfile


def _run_mcp():
    """`--mcp` -> run the CITED Score MCP server over stdio and block. No GUI, no window."""
    try:
        import mcp_server
    except Exception as e:                       # mcp SDK not bundled / import error
        sys.stderr.write(f"CITED Score MCP server unavailable: {e}\n")
        raise SystemExit(1)
    mcp_server.mcp.run()                          # stdio JSON-RPC loop; blocks until the host disconnects


def run():
    import webview
    from app import start_server
    port = start_server(0)                       # 0 = OS-assigned free port (no clashes)
    url = f"http://127.0.0.1:{port}/"
    try:
        with open(os.path.join(tempfile.gettempdir(), "cited-score.url"), "w") as f:
            f.write(url)                          # so a test harness / relaunch can find it
    except Exception:
        pass
    print(url, flush=True)
    webview.create_window("CITED Score", url, width=1180, height=820, min_size=(900, 600))
    webview.start()                              # blocks on the GUI event loop until closed


if __name__ == "__main__":
    if "--mcp" in sys.argv:
        _run_mcp()
    else:
        run()
