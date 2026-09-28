"""Local audit store for the desktop MCP fix-loop. Persists each audit as the mcp_audit() JSON
(the decision-ready shape the hosted MCP serves) so the local reader tools can slice it exactly like
the hosted ones. Lives under ~/.rubric/audits/; nothing leaves the machine."""
import os, json, re, datetime

STORE_DIR = os.environ.get("RUBRIC_AUDIT_DIR") or os.path.join(os.path.expanduser("~"), ".rubric", "audits")

def _safe(s):
    return re.sub(r"[^a-z0-9.-]", "-", (s or "").lower())[:60] or "site"

def save(mcp_json, url):
    os.makedirs(STORE_DIR, exist_ok=True)
    dom = mcp_json.get("domain") or _safe(url)
    aid = f"{_safe(dom)}-{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"   # microseconds: unique + sortable even for same-second saves
    rec = dict(mcp_json); rec["_url"] = url; rec["_audit_id"] = aid
    with open(os.path.join(STORE_DIR, aid + ".json"), "w", encoding="utf-8") as f:
        json.dump(rec, f)
    return aid

def load(audit_id):
    if not audit_id or "/" in audit_id or "\\" in audit_id or ".." in audit_id:
        return None
    try:
        with open(os.path.join(STORE_DIR, audit_id + ".json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None

def _all_recs():
    recs = []
    try:
        names = [n for n in os.listdir(STORE_DIR) if n.endswith(".json")]
    except Exception:
        return recs
    for n in sorted(names, key=lambda x: x[:-5].rsplit("-", 1)[-1], reverse=True):   # by timestamp segment = globally newest-first, not domain-alphabetical
        d = load(n[:-5])
        if d:
            recs.append(d)
    return recs

def list_recent(limit=10, domain=None):
    rows = []
    for d in _all_recs():
        dom = d.get("domain")
        if domain and dom != domain:
            continue
        rows.append({"audit_id": d.get("_audit_id"), "domain": dom, "url": d.get("_url"),
                     "score": d.get("score"), "pages_crawled": d.get("pages_crawled"),
                     "created_at": d.get("created_at"), "delta_vs_previous": None})
    # delta vs the previous (older) crawl of the SAME domain; rows are newest-first, so walk oldest-first
    prev_by_dom = {}
    for r in reversed(rows):
        dom = r["domain"]
        if dom in prev_by_dom and r["score"] is not None and prev_by_dom[dom] is not None:
            r["delta_vs_previous"] = r["score"] - prev_by_dom[dom]
        prev_by_dom[dom] = r["score"]
    return rows[:limit]
