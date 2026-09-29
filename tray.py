"""The Rubric system-tray app (pystray glue). pystray and Pillow are imported lazily inside run()
so this module imports cleanly with no display and no tray extra installed. The menu is built from
tray_actions.menu_spec() (plain data), which build_menu() translates into a pystray menu. Every
action opens a focused page in the browser or a stored report, so nothing here needs an AI."""
import tray_actions


def _open_url_cb(u):
    return lambda icon, item: tray_actions.open_url(u)


def _open_report_cb(name):
    return lambda icon, item: tray_actions.open_report(name)


def build_menu(ps, spec):
    """Translate a menu_spec() list into pystray menu items. `ps` is the pystray module (injected so
    this is testable with a fake). Returns a list of items suitable for ps.Menu(*items)."""
    items = []
    for s in spec:
        if s.get("separator"):
            items.append(ps.Menu.SEPARATOR)
            continue
        label = s.get("label", "")
        if s.get("submenu"):
            items.append(ps.MenuItem(label, ps.Menu(*build_menu(ps, s["submenu"]))))
        elif s.get("quit"):
            items.append(ps.MenuItem(label, lambda icon, item: icon.stop()))
        elif s.get("enabled") is False:
            items.append(ps.MenuItem(label, None, enabled=False))
        elif "report" in s:
            items.append(ps.MenuItem(label, _open_report_cb(s["report"])))
        elif "url" in s:
            items.append(ps.MenuItem(label, _open_url_cb(s["url"])))
        else:
            items.append(ps.MenuItem(label, None))
    return items


def _icon_image(Image, ImageDraw):
    """A small Rubric-red tile with a white R. Deliberately simple and dependency-light."""
    img = Image.new("RGBA", (64, 64), (219, 6, 50, 255))
    try:
        d = ImageDraw.Draw(img)
        d.rectangle([16, 14, 26, 50], fill=(255, 255, 255, 255))      # R stem
        d.ellipse([20, 14, 46, 34], outline=(255, 255, 255, 255), width=6)  # R bowl
        d.line([30, 32, 46, 50], fill=(255, 255, 255, 255), width=6)  # R leg
    except Exception:
        pass
    return img


def run():
    """Launch the tray. Returns 0 on a clean exit, 1 if the tray extra is not installed."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        print("The Rubric tray needs the 'tray' extra. Install it, then run 'rubric tray':")
        print("  pipx inject rubric-desktop pystray Pillow")
        print("Meanwhile you can use the CLI (rubric audit ..., rubric connect, rubric watch) or the dashboard: rubric ui")
        return 1
    # Warm the local server so the first menu click opens instantly.
    try:
        tray_actions.server_base()
    except Exception:
        pass
    # The recent-report submenu is a snapshot at launch; new crawls appear next launch. Kept simple
    # and static to avoid depending on pystray's dynamic-menu quirks (a crash here would be user-facing).
    menu = pystray.Menu(*build_menu(pystray, tray_actions.menu_spec()))
    icon = pystray.Icon("Rubric", _icon_image(Image, ImageDraw), "Rubric", menu)
    icon.run()
    return 0
