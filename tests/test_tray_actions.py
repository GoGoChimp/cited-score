"""Tests for tray_actions.py - the launcher logic behind the tray menu. No pystray, no display.
The report directory and webbrowser are patched so nothing real is opened."""
import os, time
import tray_actions


def test_menu_spec_has_core_items_in_order(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    labels = [i.get("label", "") for i in tray_actions.menu_spec()]
    joined = " | ".join(labels)
    for need in ("Run audit", "Open recent report", "Watches", "Connect", "Licence & settings", "Quit"):
        assert need in joined, f"missing {need}"
    # order: Run audit before Connect before Settings before Quit
    idx = {n: next(i for i, l in enumerate(labels) if n in l) for n in ("Run audit", "Connect", "Licence & settings", "Quit")}
    assert idx["Run audit"] < idx["Connect"] < idx["Licence & settings"] < idx["Quit"]


def test_recent_report_picks_newest(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    (tmp_path / "older.html").write_text("a", encoding="utf-8")
    (tmp_path / "newer.html").write_text("b", encoding="utf-8")
    now = time.time()
    os.utime(tmp_path / "older.html", (now - 100, now - 100))
    os.utime(tmp_path / "newer.html", (now, now))
    assert tray_actions.recent_report() == "newer"


def test_recent_report_none_when_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    assert tray_actions.recent_report() is None


def test_open_report_hits_report_route(monkeypatch):
    monkeypatch.setattr(tray_actions, "server_base", lambda: "http://127.0.0.1:9")
    seen = {}
    monkeypatch.setattr(tray_actions.webbrowser, "open", lambda url: seen.setdefault("url", url))
    tray_actions.open_report("mysite-com")
    assert seen["url"] == "http://127.0.0.1:9/report/mysite-com"


def test_recent_report_submenu_populates(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    (tmp_path / "site-com.html").write_text("x", encoding="utf-8")
    spec = tray_actions.menu_spec()
    recent = next(i for i in spec if i.get("label") == "Open recent report")
    names = [s.get("report") for s in recent["submenu"]]
    assert "site-com" in names
