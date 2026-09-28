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
