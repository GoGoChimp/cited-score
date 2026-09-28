"""Rubric desktop Pro licence. The user's account API key (cs_live_...) is the licence key.
We verify it once online against /api/entitlement, cache the entitlement locally, and honour a
14-day OFFLINE grace window. Fail safe: any ambiguity resolves to Free, never Pro. A key value is
never logged or printed beyond its prefix. Additive: nothing else in the engine imports this."""
import os, json, datetime, urllib.request, urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
LICENCE_FILE = os.path.join(_HERE, "licence.json")
DEFAULT_BASE = "https://cited.gogochimp.com"   # one constant; flips to rubric.gogochimp.com in one edit
GRACE_DAYS = 14

def api_base():
    return (os.environ.get("RUBRIC_API_BASE") or DEFAULT_BASE).rstrip("/")

def key_prefix(key):
    k = (key or "").strip()
    return (k[:12] + "…") if len(k) > 12 else "…"

def _post_entitlement(base, key):
    """POST the key to /api/entitlement. Returns (status, dict). status 0 == server unreachable.
    The key travels only in the Authorization header, never in a URL or a log."""
    req = urllib.request.Request(f"{base}/api/entitlement", data=b"",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return getattr(r, "status", 200), json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {"error": "request failed"}
    except Exception:
        return 0, {"error": "Could not reach the licence server."}

def verify(key, base=None):
    """(reachable, payload). reachable=False means the server could not be contacted (grace territory)."""
    status, data = _post_entitlement(base or api_base(), key)
    if status == 0:
        return False, data
    return True, data

def load():
    try:
        with open(LICENCE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None

def save(key, entitlement):
    try:
        with open(LICENCE_FILE, "w", encoding="utf-8") as f:
            json.dump({"key": key, "entitlement": entitlement,
                       "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, f)
        return True
    except Exception:
        return False

def _parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None

def is_pro(now=None, grace_days=GRACE_DAYS):
    """Pure. True iff the cached entitlement is Pro AND the last successful check is within the grace window.
    A future timestamp is clamped to 'now' (a backwards clock cannot buy infinite Pro)."""
    d = load()
    if not d:
        return False
    ent = d.get("entitlement") or {}
    if not isinstance(ent, dict) or ent.get("is_pro") is not True:
        return False
    ts = _parse_ts(d.get("checked_at") or "")
    if ts is None:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    age = now - min(ts, now)          # future stamp clamped to 0 age
    return age <= datetime.timedelta(days=grace_days)

def refresh(now=None):
    """Best-effort online re-verify. Never raises.
    'refreshed'  server reached, entitlement re-stamped (checked_at updated).
    'downgraded' server reached and says NOT pro -> cache updated to free NOW (does not ride grace).
    'offline'    server unreachable -> cache untouched, grace applies.
    'no_key'     nothing stored yet."""
    d = load()
    if not d or not d.get("key"):
        return "no_key"
    reachable, payload = verify(d["key"])
    if not reachable:
        return "offline"
    ent = {"is_pro": bool(payload.get("is_pro")), "plan": payload.get("plan", "free"),
           "status": payload.get("status"), "current_period_end": payload.get("current_period_end")}
    save(d["key"], ent)
    return "refreshed" if ent["is_pro"] else "downgraded"

def activate(key):
    """(ok, message). Requires ONE successful online verification returning is_pro. Offline => refuse."""
    key = (key or "").strip()
    if not key.startswith("cs_live_"):
        return False, "That does not look like a Rubric API key (expected cs_live_...)."
    reachable, payload = verify(key)
    if not reachable:
        return False, "Could not reach the licence server. Activation needs one online check; try again when connected."
    if not payload.get("is_pro"):
        return False, f"Key {key_prefix(key)} is valid but not on a Pro plan. Upgrade at {api_base()}/pricing."
    save(key, {"is_pro": True, "plan": payload.get("plan", "pro"),
               "status": payload.get("status"), "current_period_end": payload.get("current_period_end")})
    return True, f"Activated. Rubric Pro unlocked for key {key_prefix(key)}."

def require_pro():
    import sys
    if not is_pro():
        sys.stderr.write("Rubric desktop needs a Pro licence. Run: rubric activate <your cs_live_ key>\n"
                         f"Create or copy your key at {api_base()}/mcp; upgrade at {api_base()}/pricing.\n")
        sys.exit(2)
