"""Tests for tray_actions.py - the launcher logic behind the tray menu. No pystray, no display.
The report directory and webbrowser are patched so nothing real is opened."""
import os, time
import tray_actions


def test_menu_spec_is_four_items(monkeypatch, tmp_path):
    monkeypatch.setattr(tray_actions, "reports_dir", lambda: str(tmp_path))
    spec = tray_actions.menu_spec()
    labels = [i.get("label", "") for i in spec if i.get("label")]
    assert labels == ["Open", "Open recent reports", "Licence settings", "Quit"]
    # 'Open' is the default (left-click) action and opens the connections window
    openit = [i for i in spec if i.get("label") == "Open"][0]
    assert openit.get("default") is True and openit.get("window") == "/connect"


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


def test_open_report_opens_report_window(monkeypatch):
    monkeypatch.setattr(tray_actions, "server_base", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(tray_actions, "_app_browser", lambda: None)   # force the browser fallback path
    seen = {}
    monkeypatch.setattr(tray_actions.webbrowser, "open", lambda url: seen.setdefault("url", url))
    tray_actions.open_report("mysite-com")
    assert seen["url"] == "http://127.0.0.1:9/report/mysite-com"


def test_open_window_uses_app_mode(monkeypatch):
    monkeypatch.setattr(tray_actions, "server_base", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(tray_actions, "_app_browser", lambda: r"C:\fake\msedge.exe")
    seen = {}
    monkeypatch.setattr(tray_actions.subprocess, "Popen", lambda args, **kw: seen.setdefault("args", args))
    tray_actions.open_window("/connect")
    assert seen["args"][0].endswith("msedge.exe")
    assert any(a == "--app=http://127.0.0.1:9/connect" for a in seen["args"])


def test_open_window_falls_back_to_browser(monkeypatch):
    monkeypatch.setattr(tray_actions, "server_base", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(tray_actions, "_app_browser", lambda: None)
    seen = {}
    monkeypatch.setattr(tray_actions.webbrowser, "open", lambda url: seen.setdefault("url", url))
    tray_actions.open_window("/connect")
    assert seen["url"] == "http://127.0.0.1:9/connect"
