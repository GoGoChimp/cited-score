"""Start-with-Windows for the Rubric tray. We drop a tiny .cmd shim into the user's Startup
folder that launches `rubric-tray`. This needs no admin rights and no registry write, and the
user can see and delete it themselves. The Startup dir is resolved via _startup_dir() so tests
(and non-Windows machines) can redirect it."""
import os, shutil

SHIM_NAME = "Rubric.cmd"


def _startup_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _shim_path():
    return os.path.join(_startup_dir(), SHIM_NAME)


def _tray_command():
    # Prefer the windowless gui-script (pythonw, no console popup at login); fall back to the console one.
    return shutil.which("rubric-trayw") or shutil.which("rubric-tray") or "rubric-trayw"


def is_enabled():
    return os.path.exists(_shim_path())


def enable():
    """Write the Startup shim. Returns True on success."""
    try:
        d = _startup_dir()
        os.makedirs(d, exist_ok=True)
        with open(_shim_path(), "w", encoding="utf-8") as f:
            f.write('@echo off\r\nstart "" "' + _tray_command() + '"\r\n')
        return True
    except Exception:
        return False


def disable():
    """Remove the Startup shim. Returns True whether or not it was present (idempotent)."""
    try:
        p = _shim_path()
        if os.path.exists(p):
            os.remove(p)
        return True
    except Exception:
        return False
