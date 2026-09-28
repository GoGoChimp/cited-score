import licence, cli

def test_status_reports_locked(capsys, monkeypatch):
    monkeypatch.setattr(licence, "is_pro", lambda *a, **k: False)
    monkeypatch.setattr(licence, "load", lambda: None)
    assert cli.main(["status"]) == 0
    assert "not activated" in capsys.readouterr().out.lower()

def test_status_never_prints_full_key(capsys, monkeypatch):
    monkeypatch.setattr(licence, "load", lambda: {"key": "cs_live_0123456789abcdef", "entitlement": {"is_pro": True}})
    monkeypatch.setattr(licence, "is_pro", lambda *a, **k: True)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "cs_live_0123" in out and "9abcdef" not in out

def test_activate_forwards_key(monkeypatch):
    seen = {}
    monkeypatch.setattr(licence, "activate", lambda k: (seen.setdefault("key", k), (True, "ok"))[1])
    assert cli.main(["activate", "cs_live_xyz"]) == 0
    assert seen["key"] == "cs_live_xyz"

def test_activate_failure_returns_nonzero(monkeypatch):
    monkeypatch.setattr(licence, "activate", lambda k: (False, "nope"))
    assert cli.main(["activate", "cs_live_xyz"]) == 1

def test_audit_gated_by_pro(monkeypatch):
    calls = {"require": 0, "audit": 0}
    def fake_require():
        calls["require"] += 1
        raise SystemExit(2)
    monkeypatch.setattr(licence, "refresh", lambda *a, **k: "no_key")  # hermetic: no real network
    monkeypatch.setattr(licence, "require_pro", fake_require)
    monkeypatch.setattr("aiseo_audit.main", lambda: calls.__setitem__("audit", calls["audit"] + 1))
    try:
        cli.main(["audit", "--url", "https://example.com"])
    except SystemExit:
        pass
    assert calls["require"] == 1 and calls["audit"] == 0  # gate runs, audit never reached

def test_unknown_command_does_not_echo_a_keylike_arg(capsys):
    # A user who runs `rubric cs_live_...` (forgetting `activate`) must not have the key echoed to stdout.
    cli.main(["cs_live_supersecretvalue0000"])
    out = capsys.readouterr().out
    assert "supersecretvalue0000" not in out

import json as _json, os as _os

def test_mcp_status_not_registered(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_claude_config_path", lambda: str(tmp_path / "cfg.json"))
    assert cli.main(["mcp", "status"]) == 0
    assert "not registered" in capsys.readouterr().out.lower()

def test_mcp_install_then_status(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "cfg.json"
    monkeypatch.setattr(cli, "_claude_config_path", lambda: str(cfg))
    assert cli.main(["mcp", "install"]) == 0
    data = _json.loads(cfg.read_text(encoding="utf-8"))
    assert "rubric" in data["mcpServers"] and data["mcpServers"]["rubric"]["command"]
    capsys.readouterr()
    assert cli.main(["mcp", "status"]) == 0
    assert "registered" in capsys.readouterr().out.lower()

def test_mcp_install_preserves_other_servers(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(_json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    monkeypatch.setattr(cli, "_claude_config_path", lambda: str(cfg))
    cli.main(["mcp", "install"])
    data = _json.loads(cfg.read_text(encoding="utf-8"))
    assert "other" in data["mcpServers"] and "rubric" in data["mcpServers"]  # merged, not clobbered
    assert _os.path.exists(str(cfg) + ".rubric-backup")                       # backed up first

def test_mcp_install_on_corrupt_config(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cli, "_claude_config_path", lambda: str(cfg))
    assert cli.main(["mcp", "install"]) == 0
    data = _json.loads(cfg.read_text(encoding="utf-8"))
    assert "rubric" in data["mcpServers"]  # recovered to a valid config

def test_mcp_uninstall(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    monkeypatch.setattr(cli, "_claude_config_path", lambda: str(cfg))
    cli.main(["mcp", "install"]); cli.main(["mcp", "uninstall"])
    data = _json.loads(cfg.read_text(encoding="utf-8"))
    assert "rubric" not in data.get("mcpServers", {})

def test_mcp_install_warns_on_unparseable_config(tmp_path, monkeypatch, capsys):
    # A hand-edited, non-empty but unparseable config gets replaced (backed up). The user must be TOLD,
    # not silently have their other servers vanish from the live file.
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{not valid json at all, trailing comma,}", encoding="utf-8")
    monkeypatch.setattr(cli, "_claude_config_path", lambda: str(cfg))
    assert cli.main(["mcp", "install"]) == 0
    out = capsys.readouterr().out.lower()
    assert "could not be parsed" in out and "backed up" in out
    assert "rubric" in _json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]

import watch_store as _W

def _wh(tmp_path, monkeypatch):
    monkeypatch.setattr(_W, "WATCH_FILE", str(tmp_path / "w.json"))
    monkeypatch.setattr(_W, "ALERTS_FILE", str(tmp_path / "a.jsonl"))

def test_watch_add_list_remove(tmp_path, monkeypatch, capsys):
    _wh(tmp_path, monkeypatch)
    assert cli.main(["watch", "add", "https://a.com/", "--every", "daily"]) == 0
    cli.main(["watch", "list"]); assert "a.com" in capsys.readouterr().out
    assert cli.main(["watch", "remove", "https://a.com/"]) == 0

def test_watch_run_baseline_then_alert(tmp_path, monkeypatch, capsys):
    _wh(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.licence, "require_pro", lambda: None)
    cli.main(["watch", "add", "https://a.com/"])
    scores = iter([70, 75])   # first run = baseline, second run = +5 -> alert
    monkeypatch.setattr(cli, "_score_of", lambda url: next(scores))
    monkeypatch.setattr(cli, "_notify", lambda *a, **k: None)
    cli.main(["watch", "run"]); capsys.readouterr()
    assert _W.get("https://a.com/")["last_score"] == 70 and _W.list_alerts() == []   # baseline, no alert
    cli.main(["watch", "run"])
    al = _W.list_alerts()
    assert len(al) == 1 and al[0]["new"] == 75 and al[0]["old"] == 70
    assert _W.get("https://a.com/")["last_score"] == 75

def test_watch_run_below_threshold_no_alert(tmp_path, monkeypatch):
    _wh(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.licence, "require_pro", lambda: None)
    cli.main(["watch", "add", "https://a.com/"])
    scores = iter([70, 71])   # +1 < threshold(2)
    monkeypatch.setattr(cli, "_score_of", lambda url: next(scores))
    monkeypatch.setattr(cli, "_notify", lambda *a, **k: None)
    cli.main(["watch", "run"]); cli.main(["watch", "run"])
    assert _W.list_alerts() == [] and _W.get("https://a.com/")["last_score"] == 71

def test_watch_run_skips_failed_crawl(tmp_path, monkeypatch):
    _wh(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.licence, "require_pro", lambda: None)
    cli.main(["watch", "add", "https://a.com/"])
    monkeypatch.setattr(cli, "_score_of", lambda url: None)   # crawl failed
    monkeypatch.setattr(cli, "_notify", lambda *a, **k: None)
    assert cli.main(["watch", "run"]) == 0                    # no crash
    assert _W.list_alerts() == [] and _W.get("https://a.com/")["last_score"] is None

def test_score_of_none_on_failed_or_partial_crawl(monkeypatch):
    import aiseo_audit
    monkeypatch.setattr(aiseo_audit, "run_audit", lambda *a, **k: {"overall": 5, "crawl_failed": True})
    assert cli._score_of("https://x.com/") is None      # a WAF-blocked crawl scores low but is not a real drop
    monkeypatch.setattr(aiseo_audit, "run_audit", lambda *a, **k: {"overall": 3, "partial": True})
    assert cli._score_of("https://x.com/") is None      # a partial crawl is not comparable
    monkeypatch.setattr(aiseo_audit, "run_audit", lambda *a, **k: {"overall": 88})
    assert cli._score_of("https://x.com/") == 88

def test_watch_run_skips_corrupt_watch_entry(tmp_path, monkeypatch):
    _wh(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.licence, "require_pro", lambda: None)
    (tmp_path / "w.json").write_text('[{"cadence":"weekly"}, {"url":"https://a.com/","last_score":null}]', encoding="utf-8")
    monkeypatch.setattr(cli, "_score_of", lambda url: 70)
    monkeypatch.setattr(cli, "_notify", lambda *a, **k: None)
    assert cli.main(["watch", "run"]) == 0              # a watch entry with no url must not crash the whole run

def test_watch_run_toast_failure_is_nonfatal(tmp_path, monkeypatch):
    _wh(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.licence, "require_pro", lambda: None)
    cli.main(["watch", "add", "https://a.com/"])
    scores = iter([70, 80])
    monkeypatch.setattr(cli, "_score_of", lambda url: next(scores))
    def boom(*a, **k): raise RuntimeError("no notifier")
    monkeypatch.setattr(cli, "_notify", boom)
    cli.main(["watch", "run"]); assert cli.main(["watch", "run"]) == 0   # toast raised, run still ok
    assert len(_W.list_alerts()) == 1                                    # alert still recorded
