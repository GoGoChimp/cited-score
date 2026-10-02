"""Tests for rubric_app.py - the frozen exe entry point. It must dispatch on how the exe was launched:
--mcp serves the local MCP over stdio, --watch-run runs the watch sweep, no args launches the tray. The
--mcp / --crawl paths gate on Pro. Dependencies are imported lazily inside the functions, so each test
injects a fake module into sys.modules and asserts the right path ran - nothing real is launched."""
import sys, types
import pytest
import rubric_app


def _fake(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def test_mcp_arg_serves_the_mcp_server(monkeypatch):
    calls = []
    lic = _fake("licence", refresh=lambda *a, **k: None, require_pro=lambda *a, **k: None)
    mcp = _fake("mcp_server")
    mcp.mcp = types.SimpleNamespace(run=lambda *a, **k: calls.append("mcp.run"))
    monkeypatch.setitem(sys.modules, "licence", lic)
    monkeypatch.setitem(sys.modules, "mcp_server", mcp)
    monkeypatch.setattr(sys, "argv", ["Rubric.exe", "--mcp"])
    assert rubric_app.main() == 0
    assert calls == ["mcp.run"]


def test_mcp_arg_gates_on_pro(monkeypatch):
    # require_pro() exits(2) when not Pro; main must let that propagate, never fall through to the tray.
    def _deny(*a, **k):
        raise SystemExit(2)
    lic = _fake("licence", refresh=lambda *a, **k: None, require_pro=_deny)
    monkeypatch.setitem(sys.modules, "licence", lic)
    monkeypatch.setattr(sys, "argv", ["Rubric.exe", "--mcp"])
    with pytest.raises(SystemExit) as e:
        rubric_app.main()
    assert e.value.code == 2


def test_watch_run_arg_runs_the_sweep(monkeypatch):
    calls = []
    cli = _fake("cli", _cmd_watch=lambda rest: calls.append(rest) or 0)
    monkeypatch.setitem(sys.modules, "cli", cli)
    monkeypatch.setattr(sys, "argv", ["Rubric.exe", "--watch-run"])
    assert rubric_app.main() == 0
    assert calls == [["run"]]


def test_no_args_launches_the_tray(monkeypatch):
    calls = []
    lic = _fake("licence", refresh=lambda *a, **k: None, require_pro=lambda *a, **k: None)
    tray = _fake("tray", run=lambda *a, **k: calls.append("tray") or 0)
    monkeypatch.setitem(sys.modules, "licence", lic)
    monkeypatch.setitem(sys.modules, "tray", tray)
    monkeypatch.setattr(sys, "argv", ["Rubric.exe"])
    assert rubric_app.main() == 0
    assert calls == ["tray"]
