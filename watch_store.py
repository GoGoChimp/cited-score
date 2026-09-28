"""Local watch list + alert log for the desktop. Watched sites are re-crawled on a schedule (rubric watch run)
and an alert is recorded when the score moves. Lives under ~/.rubric/; nothing leaves the machine."""
import os, json, datetime

_HOME = os.environ.get("RUBRIC_HOME") or os.path.join(os.path.expanduser("~"), ".rubric")
WATCH_FILE = os.path.join(_HOME, "watches.json")
ALERTS_FILE = os.path.join(_HOME, "alerts.jsonl")

def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return default

def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f: json.dump(data, f, indent=2)

def list_watches():
    d = _read_json(WATCH_FILE, [])
    return d if isinstance(d, list) else []

def get(url):
    return next((w for w in list_watches() if w.get("url") == url), None)

def add(url, cadence="weekly"):
    ws = [w for w in list_watches() if w.get("url") != url]
    w = {"url": url, "cadence": cadence, "last_score": None, "last_checked": None,
         "added": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    ws.append(w); _write_json(WATCH_FILE, ws); return w

def remove(url):
    ws = list_watches(); kept = [w for w in ws if w.get("url") != url]
    if len(kept) == len(ws): return False
    _write_json(WATCH_FILE, kept); return True

def update_score(url, score, checked_at):
    ws = list_watches()
    for w in ws:
        if w.get("url") == url: w["last_score"] = score; w["last_checked"] = checked_at
    _write_json(WATCH_FILE, ws)

def record_alert(alert):
    os.makedirs(os.path.dirname(ALERTS_FILE), exist_ok=True)
    with open(ALERTS_FILE, "a", encoding="utf-8") as f: f.write(json.dumps(alert) + "\n")

def list_alerts(limit=20):
    out = []
    try:
        with open(ALERTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try: out.append(json.loads(line))
                    except Exception: pass
    except Exception:
        return []
    return list(reversed(out))[:limit]
