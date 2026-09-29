"""Tests for tray.build_menu - the pystray translation - using a fake pystray module so no display
or tray extra is needed. Verifies the menu tree, that submenus nest, and that actions are callable."""
import tray, tray_actions


class FakeMenu:
    SEPARATOR = "---"
    def __init__(self, *items):
        self.items = items


class FakeItem:
    def __init__(self, label, cb, enabled=True, **kw):
        self.label = label
        self.cb = cb
        self.enabled = enabled


class FakePS:
    Menu = FakeMenu
    MenuItem = FakeItem


def test_build_menu_has_core_actions(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    items = tray.build_menu(FakePS(), tray_actions.menu_spec())
    labels = [getattr(i, "label", None) for i in items if hasattr(i, "label")]
    joined = " | ".join(l for l in labels if l)
    for need in ("Run audit", "Open recent report", "Watches", "Connect", "Licence & settings", "Quit"):
        assert need in joined
    assert FakeMenu.SEPARATOR in items   # at least one separator present


def test_submenu_nests_as_menu(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    (tmp_path / "site-com.html").write_text("x", encoding="utf-8")
    items = tray.build_menu(FakePS(), tray_actions.menu_spec())
    recent = [i for i in items if getattr(i, "label", "") == "Open recent report"][0]
    assert isinstance(recent.cb, FakeMenu)   # submenu is a nested Menu


def test_url_action_is_callable(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    opened = {}
    monkeypatch.setattr(tray_actions, "open_url", lambda p: opened.setdefault("p", p))
    items = tray.build_menu(FakePS(), tray_actions.menu_spec())
    run_item = [i for i in items if "Run audit" in getattr(i, "label", "")][0]
    assert callable(run_item.cb)
    run_item.cb(None, None)
    assert opened["p"] == "/audit"
