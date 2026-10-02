"""Create a clickable 'Rubric' launcher: a desktop and Start-menu shortcut to the windowless tray
(rubric-trayw), with the Rubric icon. Uses PowerShell's WScript.Shell COM object, so there is no
extra dependency. The .ico is generated once from the embedded brand PNG into %LOCALAPPDATA%\\Rubric."""
import os, sys, subprocess, shutil, base64, io


def _app_dir():
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Rubric")
    os.makedirs(d, exist_ok=True)
    return d


def icon_path():
    """Write the Rubric .ico (multi-size) from the embedded PNG once; return its path, or '' on failure."""
    p = os.path.join(_app_dir(), "rubric.ico")
    if not os.path.exists(p):
        try:
            import rubric_icon
            from PIL import Image
            im = Image.open(io.BytesIO(base64.b64decode(rubric_icon.RUBRIC_ICON_PNG_B64))).convert("RGBA")
            im.save(p, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        except Exception:
            return ""
    return p


def _tray_exe():
    """The windowless tray launcher. Frozen exe -> the packaged exe itself (no args = tray); otherwise
    from this interpreter's Scripts dir, then PATH."""
    if getattr(sys, "frozen", False):
        return sys.executable
    sdir = os.path.dirname(sys.executable)
    for n in ("rubric-trayw.exe", "rubric-tray.exe"):
        c = os.path.join(sdir, n)
        if os.path.exists(c):
            return c
    return shutil.which("rubric-trayw") or shutil.which("rubric-tray") or "rubric-trayw"


def _desktop():
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _start_menu():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs")


def _lnk_paths():
    return (os.path.join(_start_menu(), "Rubric.lnk"), os.path.join(_desktop(), "Rubric.lnk"))


def _ps_lnk(path, target, icon, workdir):
    q = lambda s: str(s).replace("'", "''")
    ps = ("$W=New-Object -ComObject WScript.Shell;"
          f"$s=$W.CreateShortcut('{q(path)}');"
          f"$s.TargetPath='{q(target)}';"
          f"$s.WorkingDirectory='{q(workdir)}';"
          + (f"$s.IconLocation='{q(icon)}';" if icon else "")
          + "$s.Description='Rubric - AI-search citability auditor';$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   check=True, capture_output=True, timeout=25)


def create(target=None, desktop=True, start_menu=True):
    exe = target or _tray_exe()
    ico = icon_path()
    wd = os.path.dirname(exe) or _app_dir()
    sm_path, dt_path = _lnk_paths()
    made = []
    try:
        if start_menu:
            _ps_lnk(sm_path, exe, ico, wd); made.append(sm_path)
        if desktop:
            _ps_lnk(dt_path, exe, ico, wd); made.append(dt_path)
        return {"ok": True, "created": made, "icon": ico, "target": exe}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "created": made}


def remove():
    removed = []
    for p in _lnk_paths():
        try:
            if os.path.exists(p):
                os.remove(p); removed.append(p)
        except Exception:
            pass
    return {"ok": True, "removed": removed}


def installed():
    return [p for p in _lnk_paths() if os.path.exists(p)]
