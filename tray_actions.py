"""Launcher logic for the Rubric tray. No pystray import here, so this stays importable and
testable without a display. tray.py (the glue) translates menu_spec() into a real pystray menu
and calls open_url/open_report. The local HTTP server (app.start_server) is started lazily, once,
on an OS-picked loopback port with no auto-open; the tray then opens focused pages in the browser."""
import os, glob, time, threading, webbrowser

_BASE = None
_LOCK = threading.Lock()


def reports_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def server_base():
    """Start the local server once and return its base URL (http://127.0.0.1:<port>)."""
    global _BASE
    with _LOCK:
        if _BASE is None:
            import app
            port = app.start_server(0)
            _BASE = "http://127.0.0.1:" + str(port)
        return _BASE


def open_url(path):
    try:
        webbrowser.open(server_base() + path)
    except Exception:
        pass


def open_report(name):
    open_url("/report/" + name)


def recent_reports(n=8):
    try:
        files = glob.glob(os.path.join(reports_dir(), "*.html"))
    except Exception:
        files = []
    files.sort(key=os.path.getmtime, reverse=True)
    out = []
    for f in files[:n]:
        out.append({"name": os.path.basename(f)[:-5],
                    "when": time.strftime("%d %b, %H:%M", time.localtime(os.path.getmtime(f)))})
    return out


def recent_report():
    r = recent_reports(1)
    return r[0]["name"] if r else None


def menu_spec():
    """The tray menu as plain data (translated to pystray in tray.py). Items carry one of:
    url (open a focused browser page), report (open a stored report), submenu, separator, quit."""
    reports = recent_reports(8)
    if reports:
        recent_items = [{"label": r["name"] + "   " + r["when"], "report": r["name"]} for r in reports]
    else:
        recent_items = [{"label": "No reports yet", "enabled": False}]
    return [
        {"label": "Run audit…", "url": "/audit"},
        {"label": "Open recent report", "submenu": recent_items},
        {"label": "Watches", "url": "/watches-page"},
        {"label": "Connect to…", "url": "/connect"},
        {"separator": True},
        {"label": "Licence & settings", "url": "/settings"},
        {"separator": True},
        {"label": "Quit", "quit": True},
    ]
