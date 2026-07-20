#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CITED Score - desktop launcher.
Starts the local server on a free port and shows it in a native window (pywebview /
Edge WebView2 on Windows). This is the entry point PyInstaller bundles into CITED-Score.exe.
"""
import os, tempfile, webview
from app import start_server, VERSION

def run():
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
    run()
