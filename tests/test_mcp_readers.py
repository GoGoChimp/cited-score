import audit_store as S
import mcp_server as M

def _mcp(domain, score, fixes):
    return {"domain": domain, "score": score, "quotable_threshold": 70, "pages_crawled": 2,
            "created_at": "2026-09-28",
            "pillars": {"known": 80, "findable": 70, "trusted": 55, "weakest": "trusted"},
            "engines": {"chatgpt": 72, "perplexity": 70, "ai_overviews": 78, "gemini": 75, "copilot": 80, "claude": 74},
            "counts": {"errors": 1, "warnings": 1, "passed": 40, "checks_total": 42},
            "pages_by_type": [], "reachability": {}, "website_type": {}, "caveats": [],
            "total_fixes": len(fixes), "projected_score_if_all_applied": score + 5,
            "action_plan": fixes,
            "pages": [{"url": "https://" + domain + "/", "page_type": "home", "score": score,
                       "engines": {"chatgpt": 72}, "checks": [{"check_id": "title", "title": "Title", "status": "good", "detail": ""}]}]}

FIX = {"rank": 1, "check_id": "answerfirst", "title": "Answer-first opener", "pillar": "Findable",
       "severity": "warn", "effort": "Med", "pages_affected": 3, "owner": "content", "owner_secondary": None,
       "gain_overall": 3, "gain_by_engine": {"chatgpt": 2}, "instruction": "Lead with the answer.",
       "source_ref": "Ch5", "template_level": True,
       "affected": [{"url": "https://x.com/a", "page_type": "page", "current_value": "buried", "expected": "40-70 words"}]}

def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STORE_DIR", str(tmp_path))

def test_list_and_get_audit(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    aid = S.save(_mcp("x.com", 66, [FIX]), "https://x.com/")
    assert M.list_audits()["audits"][0]["audit_id"] == aid
    g = M.get_audit(aid)
    assert g["score"] == 66 and g["pillars"]["weakest"] == "trusted"

def test_get_audit_bad_id(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    assert "error" in M.get_audit("does-not-exist")

def test_action_plan_owner_filter(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    aid = S.save(_mcp("x.com", 66, [FIX]), "https://x.com/")
    dev = M.get_action_plan(aid, owner="dev")
    assert dev["returned"] == 0                     # the only fix is owner=content
    content = M.get_action_plan(aid, owner="content")
    assert content["fixes"][0]["check_id"] == "answerfirst"

def test_affected_pages(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    aid = S.save(_mcp("x.com", 66, [FIX]), "https://x.com/")
    ap = M.get_affected_pages(aid, "answerfirst")
    assert ap["total"] == 1 and ap["pages"][0]["url"] == "https://x.com/a"
    assert "error" in M.get_affected_pages(aid, "no-such-check")

def test_get_page(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    aid = S.save(_mcp("x.com", 66, [FIX]), "https://x.com/")
    assert M.get_page(aid, "https://x.com/")["page_type"] == "home"
    assert "error" in M.get_page(aid, "https://x.com/missing")

def test_compare_audits_handles_none_scores(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    m1 = _mcp("x.com", 60, []); m1["pillars"] = {"known": None, "findable": 70, "trusted": 55, "weakest": None}
    m2 = _mcp("x.com", 68, []); m2["score"] = None; m2["engines"] = {"chatgpt": None}
    a = S.save(m1, "https://x.com/"); b = S.save(m2, "https://x.com/")
    c = M.compare_audits(a, b)   # a present-but-None pillar/engine/score must not raise
    assert "error" not in c and isinstance(c["score_delta"], int)

def test_compare_audits(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    older = S.save(_mcp("x.com", 60, [FIX]), "https://x.com/")
    newer = S.save(_mcp("x.com", 68, []), "https://x.com/")   # the fix is gone in the newer -> fixed
    c = M.compare_audits(newer, older)
    assert c["score_delta"] == 8
    assert any(f["check_id"] == "answerfirst" for f in c["fixed"]) and c["regressed"] == []
