import audit_store as S

def _mcp(domain, score):
    return {"domain": domain, "score": score, "pages_crawled": 3, "created_at": "2026-09-28",
            "action_plan": [], "pages": []}

def test_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORE_DIR", str(tmp_path))
    aid = S.save(_mcp("example.com", 72), "https://example.com/")
    assert aid and aid.startswith("example.com-")
    d = S.load(aid)
    assert d["score"] == 72 and d["_url"] == "https://example.com/" and d["_audit_id"] == aid

def test_load_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORE_DIR", str(tmp_path))
    assert S.load("nope-20260101000000") is None

def test_load_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORE_DIR", str(tmp_path))
    assert S.load("../../etc/passwd") is None

def test_list_recent_newest_first_with_delta(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORE_DIR", str(tmp_path))
    import time
    a1 = S.save(_mcp("example.com", 60), "https://example.com/"); time.sleep(1.01)
    a2 = S.save(_mcp("example.com", 68), "https://example.com/")
    rows = S.list_recent()
    assert rows[0]["audit_id"] == a2 and rows[0]["delta_vs_previous"] == 8   # 68 - 60
    assert rows[1]["audit_id"] == a1 and rows[1]["delta_vs_previous"] is None

def test_list_recent_filters_domain(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORE_DIR", str(tmp_path))
    S.save(_mcp("a.com", 50), "https://a.com/"); S.save(_mcp("b.com", 90), "https://b.com/")
    rows = S.list_recent(domain="b.com")
    assert len(rows) == 1 and rows[0]["domain"] == "b.com"

def test_list_recent_skips_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORE_DIR", str(tmp_path))
    S.save(_mcp("good.com", 80), "https://good.com/")
    (tmp_path / "bad-20260101000000.json").write_text("{not json", encoding="utf-8")
    rows = S.list_recent()
    assert len(rows) == 1 and rows[0]["domain"] == "good.com"   # corrupt file skipped, no raise
