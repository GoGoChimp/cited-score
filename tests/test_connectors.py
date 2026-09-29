"""Tests for connectors.py - the multi-tool MCP connector registry behind the Connect grid.
Covers the json (auto-write), cli (shell), and snippet (manual) tool kinds, the backup-merge-write
discipline, and honest status. Nothing here touches a real Claude/Cursor install; every path is
redirected to a tmp file via connectors.config_path."""
import json, os
import connectors


def _patch_path(monkeypatch, cfg):
    monkeypatch.setattr(connectors, "config_path", lambda tid: str(cfg))


def test_install_json_writes_rubric_block(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    _patch_path(monkeypatch, cfg)
    res = connectors.install("cursor")
    assert res["ok"] is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert connectors.SERVER_KEY in data["mcpServers"]
    assert data["mcpServers"][connectors.SERVER_KEY]["command"]


def test_install_preserves_other_servers_and_backs_up(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    _patch_path(monkeypatch, cfg)
    connectors.install("cursor")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"] and connectors.SERVER_KEY in data["mcpServers"]
    assert os.path.exists(str(cfg) + ".rubric-backup")


def test_install_corrupt_config_recovers(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{not valid json", encoding="utf-8")
    _patch_path(monkeypatch, cfg)
    res = connectors.install("cursor")
    assert res["ok"] is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert connectors.SERVER_KEY in data["mcpServers"]
    assert "backup" in res["message"].lower()


def test_status_connected_after_install(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    _patch_path(monkeypatch, cfg)
    assert connectors.status("cursor") == "not_connected"
    connectors.install("cursor")
    assert connectors.status("cursor") == "connected"


def test_status_not_connected_clean(tmp_path, monkeypatch):
    cfg = tmp_path / "nope.json"
    _patch_path(monkeypatch, cfg)
    assert connectors.status("cursor") == "not_connected"


def test_uninstall_removes_only_rubric(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    _patch_path(monkeypatch, cfg)
    connectors.install("cursor")
    connectors.uninstall("cursor")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert connectors.SERVER_KEY not in data.get("mcpServers", {})
    assert "other" in data["mcpServers"]


def test_snippet_tool_is_manual(tmp_path, monkeypatch):
    res = connectors.install("codex")
    assert res.get("manual") is True
    assert "rubric-mcp" in res["snippet"]
    assert connectors.status("codex") == "manual"


def test_cli_tool_shells_claude(monkeypatch):
    calls = []
    monkeypatch.setattr(connectors.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    monkeypatch.setattr(connectors.subprocess, "run", lambda *a, **k: calls.append(a[0]) or type("R", (), {"returncode": 0})())
    res = connectors.install("claude-code")
    assert res["ok"] is True
    assert calls and calls[0][:3] == ["/usr/bin/claude", "mcp", "add"]
    assert connectors.SERVER_KEY in calls[0]


def test_cli_tool_missing_claude_is_graceful(monkeypatch):
    monkeypatch.setattr(connectors.shutil, "which", lambda name: None)
    res = connectors.install("claude-code")
    assert res["ok"] is False
    assert "claude" in res["message"].lower()


def test_list_tools_shape(monkeypatch):
    tools = connectors.list_tools()
    ids = [t["id"] for t in tools]
    assert "claude-desktop" in ids and "cursor" in ids and len(tools) == 5
    for t in tools:
        assert set(["id", "name", "kind", "status"]).issubset(t.keys())


def test_unknown_tool_is_rejected():
    assert connectors.install("chatgpt")["ok"] is False
    assert connectors.status("chatgpt") == "unknown"
