import json, datetime, importlib
import licence

NOW = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)

def _write(tmp_path, monkeypatch, checked_at, is_pro=True, key="cs_live_abc123"):
    f = tmp_path / "licence.json"
    f.write_text(json.dumps({"key": key, "entitlement": {"is_pro": is_pro, "plan": "pro" if is_pro else "free"}, "checked_at": checked_at}), encoding="utf-8")
    monkeypatch.setattr(licence, "LICENCE_FILE", str(f))
    return f

def test_pro_within_grace(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, (NOW - datetime.timedelta(days=13)).isoformat())
    assert licence.is_pro(now=NOW) is True

def test_pro_expired_after_grace(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, (NOW - datetime.timedelta(days=15)).isoformat())
    assert licence.is_pro(now=NOW) is False

def test_future_checked_at_does_not_grant_forever(tmp_path, monkeypatch):
    # A clock moved backwards must not yield an enormous positive grace window.
    _write(tmp_path, monkeypatch, (NOW + datetime.timedelta(days=400)).isoformat())
    assert licence.is_pro(now=NOW) is True  # future stamp treated as "just checked", still bounded by grace later

def test_free_entitlement_never_pro(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, NOW.isoformat(), is_pro=False)
    assert licence.is_pro(now=NOW) is False

def test_corrupt_file_fails_safe(tmp_path, monkeypatch):
    f = tmp_path / "licence.json"
    f.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(licence, "LICENCE_FILE", str(f))
    assert licence.is_pro(now=NOW) is False

def test_missing_file_fails_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(licence, "LICENCE_FILE", str(tmp_path / "nope.json"))
    assert licence.is_pro(now=NOW) is False

def test_refresh_downgrades_when_server_says_not_pro(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, (NOW - datetime.timedelta(days=1)).isoformat(), is_pro=True)
    monkeypatch.setattr(licence, "_post_entitlement", lambda base, key: (200, {"is_pro": False, "plan": "free"}))
    assert licence.refresh(now=NOW) == "downgraded"
    assert licence.is_pro(now=NOW) is False  # immediate, does NOT ride grace

def test_refresh_offline_keeps_cache_within_grace(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, (NOW - datetime.timedelta(days=2)).isoformat(), is_pro=True)
    monkeypatch.setattr(licence, "_post_entitlement", lambda base, key: (0, {"error": "unreachable"}))
    assert licence.refresh(now=NOW) == "offline"
    assert licence.is_pro(now=NOW) is True  # grace still applies

def test_activate_offline_refuses(tmp_path, monkeypatch):
    # First-run activation with the server unreachable must NOT grant Pro and must write no cache.
    monkeypatch.setattr(licence, "LICENCE_FILE", str(tmp_path / "licence.json"))
    monkeypatch.setattr(licence, "_post_entitlement", lambda base, key: (0, {"error": "unreachable"}))
    ok, _ = licence.activate("cs_live_abc123def456")
    assert ok is False and (tmp_path / "licence.json").exists() is False

def test_activate_non_pro_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(licence, "LICENCE_FILE", str(tmp_path / "licence.json"))
    monkeypatch.setattr(licence, "_post_entitlement", lambda base, key: (200, {"is_pro": False, "plan": "free"}))
    ok, _ = licence.activate("cs_live_abc123def456")
    assert ok is False and (tmp_path / "licence.json").exists() is False

def test_refresh_transient_error_keeps_grace(tmp_path, monkeypatch):
    # A server that is UP but erroring (5xx / 429), rate-limiting, timing out (408) or 403-ing an automated
    # request (hosting bot-protection / WAF) must be treated as offline, not as "you are not Pro". Otherwise a
    # transient blip - or a Cloudflare challenge - locks a paying user out on every `rubric audit`.
    _write(tmp_path, monkeypatch, (NOW - datetime.timedelta(days=2)).isoformat(), is_pro=True)
    for status in (500, 502, 503, 429, 408, 403):
        monkeypatch.setattr(licence, "_post_entitlement", lambda base, key, s=status: (s, {"error": "server"}))
        assert licence.refresh(now=NOW) == "offline"
        assert licence.is_pro(now=NOW) is True

def test_verify_403_is_not_a_trustworthy_negative(tmp_path, monkeypatch):
    # A 403 from a WAF in front of the licence endpoint is "couldn't check", not "not Pro".
    monkeypatch.setattr(licence, "_post_entitlement", lambda base, key: (403, {"error": "forbidden"}))
    reachable, _ = licence.verify("cs_live_abc123def456")
    assert reachable is False
    # A genuine 401 (invalid/revoked key) IS trustworthy and is acted on.
    monkeypatch.setattr(licence, "_post_entitlement", lambda base, key: (401, {"error": "invalid"}))
    reachable, _ = licence.verify("cs_live_abc123def456")
    assert reachable is True

def test_activate_save_failure_refuses(tmp_path, monkeypatch):
    # If the licence file cannot be written, activate must NOT claim success.
    monkeypatch.setattr(licence, "LICENCE_FILE", str(tmp_path / "licence.json"))
    monkeypatch.setattr(licence, "_post_entitlement", lambda base, key: (200, {"is_pro": True, "plan": "pro"}))
    monkeypatch.setattr(licence, "save", lambda *a, **k: False)
    ok, msg = licence.activate("cs_live_abc123def456")
    assert ok is False and "could not" in msg.lower()

def test_key_prefix_handles_non_string():
    # A hand-edited licence.json can carry a non-string key; key_prefix must not raise (status uses it).
    assert licence.key_prefix(None) == "…"
    assert isinstance(licence.key_prefix(123456789012345), str)

def test_key_prefix_redacts():
    assert licence.key_prefix("cs_live_0123456789abcdef") == "cs_live_0123…"
    assert "9abcdef" not in licence.key_prefix("cs_live_0123456789abcdef")
