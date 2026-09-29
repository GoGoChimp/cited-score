"""Tests for tray.build_menu - the pystray translation - using a fake pystray module so no display
or tray extra is needed. Verifies the four-item menu, the default (left-click) action, and that
actions are callable and open a native window."""
import tray, tray_actions


class FakeMenu:
    SEPARATOR = "---"
    def __init__(self, *items):
        self.items = items


class FakeItem:
    def __init__(self, label, cb, enabled=True, default=False, **kw):
        self.label = label
        self.cb = cb
        self.enabled = enabled
        self.default = default


class FakePS:
    Menu = FakeMenu
    MenuItem = FakeItem


def test_build_menu_has_four_actions(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    items = tray.build_menu(FakePS(), tray_actions.menu_spec())
    labels = [getattr(i, "label", None) for i in items if hasattr(i, "label")]
    joined = " | ".join(l for l in labels if l)
    for need in ("Open", "Open recent reports", "Licence settings", "Quit"):
        assert need in joined
    assert FakeMenu.SEPARATOR in items   # separator before Quit


def test_open_is_default_left_click_action(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    items = tray.build_menu(FakePS(), tray_actions.menu_spec())
    openit = [i for i in items if getattr(i, "label", "") == "Open"][0]
    assert openit.default is True   # left-click opens the connections window


def test_window_action_opens_native_window(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    opened = {}
    monkeypatch.setattr(tray_actions, "open_window", lambda p: opened.setdefault("p", p))
    items = tray.build_menu(FakePS(), tray_actions.menu_spec())
    openit = [i for i in items if getattr(i, "label", "") == "Open"][0]
    assert callable(openit.cb)
    openit.cb(None, None)
    assert opened["p"] == "/connect"
