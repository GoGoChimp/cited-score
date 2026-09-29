"""Launcher logic for the Rubric tray. No pystray import here, so this stays importable and
testable without a display. tray.py (the glue) translates menu_spec() into a real pystray menu
and calls open_url/open_report. The local HTTP server (app.start_server) is started lazily, once,
on an OS-picked loopback port with no auto-open; the tray then opens focused pages in the browser."""
import os, glob, time, threading, webbrowser, subprocess, shutil

_BASE = None
_LOCK = threading.Lock()


def _app_browser():
    """Path to Edge or Chrome, for opening a page as a chromeless app WINDOW (--app), not a tab."""
    pf = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    pf2 = os.environ.get("ProgramFiles") or r"C:\Program Files"
    la = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    for c in [os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
              os.path.join(pf2, "Microsoft", "Edge", "Application", "msedge.exe"),
              shutil.which("msedge"),
              os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(pf2, "Google", "Chrome", "Application", "chrome.exe"),
              os.path.join(la, "Google", "Chrome", "Application", "chrome.exe"),
              shutil.which("chrome")]:
        if c and os.path.exists(c):
            return c
    return None


def open_window(path):
    """Open a Rubric page as a chromeless app WINDOW (Edge/Chrome --app), not a browser tab.
    Falls back to the default browser only if neither is installed."""
    url = server_base() + path
    exe = _app_browser()
    if exe:
        subprocess.Popen([exe, "--app=" + url, "--window-size=1120,780"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return
    webbrowser.open(url)


def reports_dir():
    # Same persistent per-user reports dir as app.py REPORTS (survives a frozen onefile restart).
    return os.path.join(os.environ.get("RUBRIC_HOME") or os.path.join(os.path.expanduser("~"), ".rubric"), "reports")


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
    # No try/except here: if the local server cannot start (e.g. a broken install), the caller in
    # tray.py surfaces it as a notification instead of a menu click that silently does nothing.
    webbrowser.open(server_base() + path)


def open_report(name):
    open_window("/report/" + name)


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
    """The tray menu as plain data (translated to pystray in tray.py). Kept to four items on Chris's
    direction, matching Ollama: Open (the connections/apps window), Open recent reports, Licence
    settings, Quit. 'Open' is the default action, so a LEFT-click opens the window. Items open a
    native app window (window key), not a browser tab. Auditing happens inside the connected AI tool."""
    return [
        {"label": "Open", "window": "/connect", "default": True},
        {"label": "Open recent reports", "window": "/reports-view"},
        {"label": "Licence settings", "window": "/settings"},
        {"separator": True},
        {"label": "Quit", "quit": True},
    ]
