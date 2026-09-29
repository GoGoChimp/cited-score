#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rubric - local desktop app / shell.
Run:  python app.py    (opens http://127.0.0.1:5000 in your browser)
Enter any website, click Run, watch the crawl, open the report. No command line needed.
Uses the same engine as aiseo_audit.py (your installed Chrome renders each page).
Zero third-party web deps - Python standard library only, so it packages cleanly to an .exe.
"""
import os, re, sys, json, threading, time, webbrowser, urllib.parse, urllib.request, urllib.error, glob, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import aiseo_audit as A

APP_VERSION = "0.15.7"                # semver; bump on every release + tag the GitHub release to match
GITHUB_REPO = "GoGoChimp/cited-score" # public repo that hosts the releases (update check reads /releases/latest)
VERSION = f"v{APP_VERSION} - August 2026"

_update = {"checked": False, "update": False, "latest": None, "url": None, "dl": None}
def _ver_tuple(s):
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums[:3]) if nums else ()
def check_update():
    """Ask GitHub for the latest release once per run. Fails silently offline / if the
    repo or a release doesn't exist yet, so the app never blocks or errors on this."""
    if _update["checked"]:
        return _update
    _update["checked"] = True
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"User-Agent": "CITED-Score", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            d = json.load(r)
        _update["latest"] = d.get("tag_name") or ""
        _update["url"] = d.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases/latest"
        _update["dl"] = f"https://github.com/{GITHUB_REPO}/releases/latest/download/CITED-Score.exe"
        _update["update"] = _ver_tuple(_update["latest"]) > _ver_tuple(APP_VERSION)
    except Exception:
        pass  # no network / repo or release not published yet / rate-limited -> no banner
    return _update
def app_dir():
    # frozen (.exe): sit next to the executable so reports are user-visible; else script dir
    if getattr(sys, "frozen", False): return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))
HERE = app_dir()
REPORTS = os.path.join(HERE, "reports"); os.makedirs(REPORTS, exist_ok=True)
JOBS = {}
PORT = 5000

def safe(d): return re.sub(r"[^a-z0-9._-]", "-", d.lower())[:80]

def _compare_reports(da, db):
    """Diff two stored report data dicts (a = newer, b = older): score, pillar and engine deltas, and which
    issue check-ids were fixed (in b, gone in a) or regressed (in a, not in b). Mirrors the MCP compare_audits."""
    Pa, Pb = da.get("pillars") or {}, db.get("pillars") or {}
    Ea, Eb = da.get("engines") or {}, db.get("engines") or {}
    Ia = {i.get("id"): i.get("label") for i in (da.get("issues") or [])}
    Ib = {i.get("id"): i.get("label") for i in (db.get("issues") or [])}
    return {
        "newer": {"domain": da.get("domain"), "date": da.get("date"), "overall": da.get("overall")},
        "older": {"domain": db.get("domain"), "date": db.get("date"), "overall": db.get("overall")},
        "score_delta": (da.get("overall") or 0) - (db.get("overall") or 0),
        "pillar_deltas": {k: (Pa.get(k) or 0) - (Pb.get(k) or 0) for k in ("Known", "Findable", "Trusted")},
        "engine_deltas": {k: (Ea.get(k) or 0) - (Eb.get(k) or 0) for k in Ea},
        "fixed": [{"id": k, "label": Ib[k]} for k in Ib if k not in Ia][:20],
        "regressed": [{"id": k, "label": Ia[k]} for k in Ia if k not in Ib][:20],
    }

# --- Activation (Phase A: hard-gate on launch, email capture) ------------------
SB_FUNCTIONS = "https://xhalhtbsddaqmnqruljt.supabase.co/functions/v1"
ACT_FILE = os.path.join(HERE, "activation.json")

def load_activation():
    """Return the cached activation dict (with a token) or None. Presence == activated."""
    try:
        with open(ACT_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if d.get("token") else None
    except Exception:
        return None

def save_activation(d):
    try:
        with open(ACT_FILE, "w", encoding="utf-8") as f: json.dump(d, f)
        return True
    except Exception:
        return False

def is_activated(): return load_activation() is not None

def _pro_ok():
    """A pipx desktop user unlocks with their Pro licence (rubric activate) instead of the free email activation."""
    try:
        import licence; return licence.is_pro()
    except Exception:
        return False

def sb_post(fn, payload, timeout=12):
    """POST to a Rubric Edge Function. Returns (status, dict). No secret ever ships here -
    the functions are public; the secret + signing key live only in Supabase."""
    req = urllib.request.Request(f"{SB_FUNCTIONS}/{fn}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return getattr(r, "status", 200), json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, {"error": "request failed"}
    except Exception:
        return 0, {"error": "Could not reach the activation server. Check your connection."}

# ---- MCP connect (add Rubric to Claude Desktop as an MCP server) -------------------------
def _mcp_command():
    """(command, args) that launch THIS build's MCP server for the current runtime.
    Frozen exe -> the exe itself with --mcp; a Python run -> python + mcp_server.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        return sys.executable, ["--mcp"]
    return sys.executable, [os.path.join(here, "mcp_server.py")]

def _claude_desktop_config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Claude", "claude_desktop_config.json")

def mcp_config_snippet():
    """Raw config a user can paste into any host (Cursor, Claude Code, etc.)."""
    cmd, args = _mcp_command()
    return json.dumps({"mcpServers": {"cited-score": {"command": cmd, "args": args}}}, indent=2)

def mcp_status():
    """Is cited-score registered in Claude Desktop's config? {connected, exists, path}."""
    path = _claude_desktop_config_path()
    try:
        with open(path, encoding="utf-8") as f: cfg = json.load(f)
        return {"connected": "cited-score" in (cfg.get("mcpServers") or {}), "exists": True, "path": path}
    except FileNotFoundError:
        return {"connected": False, "exists": False, "path": path}
    except Exception as e:
        return {"connected": False, "exists": True, "path": path, "error": str(e)[:120]}

def connect_mcp():
    """Merge the cited-score server into Claude Desktop's config (backs up first). {ok, path}."""
    path = _claude_desktop_config_path()
    cmd, args = _mcp_command()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cfg = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f: cfg = json.load(f) or {}
            except Exception: cfg = {}
            try:
                import shutil; shutil.copy(path, path + ".cited-backup")   # never clobber a user file blind
            except Exception: pass
        if not isinstance(cfg, dict): cfg = {}
        cfg.setdefault("mcpServers", {})["cited-score"] = {"command": cmd, "args": args}
        with open(path, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2)
        return {"ok": True, "path": path, "note": "Restart Claude Desktop to load Rubric."}
    except Exception as e:
        return {"ok": False, "path": path, "error": str(e)[:200]}

def disconnect_mcp():
    """Remove the cited-score server from Claude Desktop's config (backs up first; leaves other servers)."""
    path = _claude_desktop_config_path()
    try:
        if not os.path.exists(path):
            return {"ok": True, "note": "Nothing to remove."}
        with open(path, encoding="utf-8") as f: cfg = json.load(f)
        servers = (cfg.get("mcpServers") or {}) if isinstance(cfg, dict) else {}
        if "cited-score" in servers:
            try:
                import shutil; shutil.copy(path, path + ".cited-backup")
            except Exception: pass
            del servers["cited-score"]; cfg["mcpServers"] = servers
            with open(path, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2)
        return {"ok": True, "path": path, "note": "Removed. Restart Claude Desktop to unload it."}
    except Exception as e:
        return {"ok": False, "path": path, "error": str(e)[:200]}

# ---- anonymous usage analytics (DEFAULT ON; disclosed; one-click opt-out) --------------------------
# What leaves the machine: an anonymous install id + version + os, per-tool MCP call counts, and per
# event BUCKETS / CATEGORIES / BOOLEANS only (page-count band, site type, score band, is-this-a-re-run,
# white-label used, feature used). What NEVER leaves: audited URLs, domains, the site's name, page
# content, crawl results, or anything joining usage to a person. Re-runs are detected ON-DEVICE from
# local history - only the true/false leaves. The user can turn it off in one click (opt-out).
_HEREDIR = os.path.dirname(os.path.abspath(__file__))
def _telemetry_consent_path(): return os.path.join(_HEREDIR, "telemetry_consent.json")
def telemetry_consent():
    try:
        with open(_telemetry_consent_path(), encoding="utf-8") as f: return bool(json.load(f).get("consent"))
    except Exception:
        return True                                        # default ON (disclosed) until the user opts out
def set_telemetry_consent(v):
    try:
        with open(_telemetry_consent_path(), "w", encoding="utf-8") as f: json.dump({"consent": bool(v)}, f)
        return True
    except Exception: return False
def _install_id():
    import hashlib
    raw = (os.environ.get("COMPUTERNAME", "") + _HEREDIR).encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()[:16]           # stable, anonymous, no PII

def _usage_events_path(): return os.path.join(_HEREDIR, "usage_events.jsonl")
def _pbucket(n): n = int(n or 0); return "1-10" if n <= 10 else "11-50" if n <= 50 else "51-200" if n <= 200 else "200+"
def _sbucket(s): s = float(s or 0); return "<50" if s < 50 else "50-69" if s < 70 else "70-84" if s < 85 else "85+"
def record_usage(kind, **fields):
    """Append ONE content-free usage event locally (uploaded later only if consented). By construction it
    records only buckets / categories / booleans - never a URL, domain, or crawl content."""
    if not telemetry_consent(): return
    try:
        ev = {"k": kind, "day": time.strftime("%Y-%m-%d")}
        ev.update({k: v for k, v in fields.items() if v is not None})
        with open(_usage_events_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:
        pass

def upload_telemetry():
    """Batch-send the anonymous events + MCP call counts to the `usage` endpoint, then clear the local
    batch. No-op unless opted in. Robust: rename-then-send so a failed upload never loses events.
    Graceful if the `usage` endpoint isn't deployed yet."""
    if not telemetry_consent(): return
    try:
        usage = {}
        up = os.path.join(_HEREDIR, "mcp_usage.json")
        if os.path.exists(up):
            with open(up, encoding="utf-8") as f:
                usage = {k: (v or {}).get("count") for k, v in json.load(f).items()}
        ep = _usage_events_path(); tmp = ep + ".send"; events = []
        if os.path.exists(ep):
            try: os.replace(ep, tmp)                        # atomically claim the batch (avoids upload races)
            except Exception: tmp = None
        if tmp and os.path.exists(tmp):
            with open(tmp, encoding="utf-8") as f:
                events = [json.loads(l) for l in f if l.strip()][:3000]
        if not events and not usage:
            if tmp and os.path.exists(tmp):
                try: os.replace(tmp, ep)
                except Exception: pass
            return
        st, _ = sb_post("usage", {"install": _install_id(), "version": APP_VERSION,
                                  "os": sys.platform, "tools": usage, "events": events})
        if st and 200 <= st < 300:
            if tmp and os.path.exists(tmp):
                try: os.remove(tmp)
                except Exception: pass
        elif tmp and os.path.exists(tmp):                  # send failed -> put the batch back, never lose it
            try:
                with open(tmp, encoding="utf-8") as fr, open(ep, "a", encoding="utf-8") as fa:
                    fa.write(fr.read())
                os.remove(tmp)
            except Exception: pass
    except Exception:
        pass

# ---- silent auto-update (BETA, GATED - built but NOT wired to the banner until tested vs 2 real builds) --
def self_update():
    """Silent self-replace + relaunch for the FROZEN exe: download latest to temp, then a tiny batch waits
    for THIS process to exit, swaps the file, and relaunches. NO-OP unless running as the packaged exe.
    DELIBERATELY not the default update path - a bad self-updater bricks installs, so the banner still just
    opens the download until this is tested against two real signed builds. Test before wiring."""
    if not getattr(sys, "frozen", False):
        return {"ok": False, "note": "Self-update only applies to the packaged exe (this is a Python run)."}
    try:
        exe = sys.executable
        tmp = exe + ".new"
        urllib.request.urlretrieve(f"https://github.com/{GITHUB_REPO}/releases/latest/download/CITED-Score.exe", tmp)
        if os.path.getsize(tmp) < 1_000_000:                     # a real exe is many MB; guard vs an HTML error page
            os.remove(tmp); return {"ok": False, "error": "Downloaded file too small - aborted (not swapped)."}
        pid = os.getpid()
        bat = os.path.join(tempfile.gettempdir(), "cited-update.bat")
        with open(bat, "w") as f:
            f.write("@echo off\r\n:wait\r\n"
                    f'tasklist /fi "PID eq {pid}" | find "{pid}" >nul && (timeout /t 1 >nul & goto wait)\r\n'
                    f'move /y "{tmp}" "{exe}" >nul\r\n'
                    f'start "" "{exe}"\r\n'
                    'del "%~f0"\r\n')
        import subprocess
        subprocess.Popen(["cmd", "/c", bat], creationflags=0x00000008)   # DETACHED_PROCESS; completes once the app exits
        return {"ok": True, "note": "Update downloaded. Close Rubric to finish; it will reopen on the new version."}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

def schedule_crawl(url, cadence="WEEKLY"):
    """Register a Windows scheduled task to re-crawl a URL on a cadence, so the Score-over-time trend +
    AI-bot log activity fill in automatically. Frozen exe only (the task runs `CITED-Score.exe --crawl`).
    Returns {ok}. A Python run is a no-op with guidance (use the CLI + your own scheduler)."""
    if not getattr(sys, "frozen", False):
        return {"ok": False, "note": "Scheduling needs the packaged exe. From Python, point your own scheduler at: python aiseo_audit.py --url <url> --out reports/<name>"}
    try:
        import subprocess, urllib.parse
        dom = urllib.parse.urlparse(url if url.startswith("http") else "https://"+url).netloc.replace("www.", "") or "site"
        name = "Rubric - " + dom
        tr = f'"{sys.executable}" --crawl {url}'
        r = subprocess.run(["schtasks","/create","/tn",name,"/tr",tr,"/sc",cadence,"/d","MON","/st","09:00","/f"],
                           capture_output=True, text=True, creationflags=0x08000000)
        if r.returncode == 0:
            return {"ok": True, "task": name, "note": "Weekly re-crawl scheduled (Mon 09:00). Manage or remove it in Windows Task Scheduler."}
        return {"ok": False, "error": (r.stderr or r.stdout or "schtasks failed").strip()[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

INDEX = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Rubric</title><link rel="icon" href="__FAV__"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400..900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#14140f;--panel:#191914;--panel2:#1e1e18;--line:#2a2a24;--muted:#a8a495;--txt:#f2f0e4;--grn:#db0632;--grn2:#ef1a48;--ok:#3DD68C;--red:#db0632;--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--display:'Archivo',sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 'Archivo',-apple-system,Segoe UI,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:26px 26px 60px}
a{color:var(--grn);text-decoration:none}
.logo{display:inline-flex;align-items:baseline;gap:0}
.logo .wm{display:inline-flex;align-items:baseline;gap:8px}
.logo .lw{font-family:'Archivo',sans-serif;font-weight:800;font-size:25px;letter-spacing:-.056em;color:var(--txt);line-height:1;margin-left:-1px}
.logo .ls{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.24em;color:var(--muted);text-transform:uppercase}
.upd{display:flex;align-items:center;gap:16px;background:rgba(219,6,50,.07);border:1px solid rgba(219,6,50,.32);border-radius:14px;padding:14px 18px;margin-bottom:26px}
.upd .uc{background:var(--grn);color:#0a0a0a;font-weight:800;font-size:11px;letter-spacing:.5px;padding:3px 8px;border-radius:5px}
.upd .ut{font-weight:800}.upd .ud{color:var(--muted);font-size:13px}.upd .sp{flex:1}
.updbtn{background:var(--grn);color:#0a0a0a;font-weight:800;padding:9px 16px;border-radius:9px;white-space:nowrap}
.later{color:#fff;font-weight:600;cursor:pointer;padding:9px 10px}
.cols{display:grid;grid-template-columns:1fr 360px;gap:30px;align-items:start}
@media(max-width:960px){.cols{grid-template-columns:1fr}.side{margin-top:0}}
.h1{font-family:var(--display);font-weight:800;font-size:clamp(40px,5vw,60px);line-height:.95;letter-spacing:-.03em;margin:16px 0 0}
.lede{color:var(--muted);font-size:17px;max-width:520px;margin:16px 0 14px}
.engrow{color:var(--muted);font-size:14px;display:flex;flex-wrap:wrap;gap:22px;margin-bottom:26px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:24px}
.lab{font-family:var(--display);font-weight:800;text-transform:uppercase;letter-spacing:.6px;font-size:12px;color:var(--muted);margin-bottom:10px}
.inp{display:flex;align-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:0 14px}
.inp .pfx{color:var(--muted);font-size:16px}.inp input{flex:1;background:none;border:0;color:var(--txt);font-size:16px;padding:14px 6px;outline:none}
.runbtn{width:100%;background:var(--grn);color:#0a0a0a;border:0;border-radius:11px;padding:16px;font-family:var(--display);font-weight:800;font-size:17px;text-transform:uppercase;letter-spacing:.5px;cursor:pointer;margin-top:14px}
.runbtn:hover:not(:disabled){background:var(--grn2)}.runbtn:disabled{opacity:.5;cursor:default}
.hint{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:14px;color:var(--muted);font-size:13px}
.optbtn{background:none;border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:6px 14px;font-size:13px;cursor:pointer}
.opts{display:none;gap:14px;margin-top:14px}.opts.on{display:flex;align-items:end;flex-wrap:nowrap}.opts>div{flex:1;min-width:0}.opts label{min-height:32px}
.opts label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
.opts input,.opts select{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:10px 12px;font-size:14px}
.side{margin-top:58px}
.side .card{padding:20px;margin-bottom:22px}
.ph{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
.ph .t{font-family:var(--display);font-weight:800;text-transform:uppercase;letter-spacing:.5px;font-size:15px}.ph .m{color:var(--muted);font-size:12px}
.rep{display:flex;align-items:center;gap:14px;padding:14px 0;border-top:1px solid var(--line);color:var(--txt)}.rep:first-of-type{border-top:0}
.rep .sc{font-family:var(--display);font-weight:800;font-size:34px;color:var(--grn);width:52px;flex:0 0 52px}
.rep .nm{font-weight:800}.rep .mt{color:var(--muted);font-size:12px}
.dl{margin-left:auto;font-size:13px;font-weight:700;white-space:nowrap}.dl.up{color:var(--ok)}.dl.dn{color:var(--red)}.dl.z{color:var(--muted)}
.note2{color:var(--muted);font-size:12px;margin-top:12px}
.chk{display:flex;gap:12px;padding:9px 0;font-size:14px}.chk b{font-family:var(--display);font-weight:800;color:var(--grn);width:22px;flex:0 0 22px}
.bar{height:10px;background:#2a2320;border-radius:6px;overflow:hidden;margin:12px 0}.bar i{display:block;height:100%;background:var(--grn);width:0;transition:width .3s}
.log{font:12px/1.5 var(--mono);color:var(--muted);background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px;height:170px;overflow:auto;white-space:pre-wrap}
.tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:14px 0}
.tile{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px;text-align:center}.tile .n{font-family:var(--display);font-weight:800;font-size:24px}.tile .l{font-size:11px;color:var(--muted)}
.open{display:inline-block;margin-top:6px;background:var(--grn);color:#0a0a0a;font-weight:800;padding:11px 18px;border-radius:9px}
.foot{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-top:44px;color:var(--muted);font-size:13px}
.foot .lk{display:flex;gap:20px}
.hide{display:none}.err{color:var(--red)}
.tools{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:34px}
@media(max-width:820px){.tools{grid-template-columns:1fr}}
.tool .note2{margin-top:6px}
.tool textarea{width:100%;min-height:92px;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:10px;padding:10px 12px;font:12px/1.5 var(--mono);resize:vertical;margin-top:10px;outline:none}
.tool select{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:9px 12px;font-size:13px;margin-top:10px;outline:none}
.mini{margin-top:12px;background:var(--grn);color:#0a0a0a;border:0;border-radius:9px;padding:10px 16px;font-family:var(--display);font-weight:800;text-transform:uppercase;letter-spacing:.4px;font-size:13px;cursor:pointer}
.mini:hover:not(:disabled){background:var(--grn2)}.mini:disabled{opacity:.5;cursor:default}
.tout{margin-top:14px;font-size:13px}
.tout table{width:100%;border-collapse:collapse;font-size:12px}
.tout th,.tout td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
.tout th:first-child,.tout td:first-child{text-align:left}
.tout th{color:var(--muted);font-weight:600}
.tout .best{color:var(--grn);font-weight:800}
.rho.up{color:var(--ok)}.rho.dn{color:var(--red)}.rho.z{color:var(--muted)}
.tag{font-size:10px;padding:1px 7px;border-radius:20px;border:1px solid var(--line);color:var(--muted)}
.tag.up{color:var(--ok);border-color:#3DD68C55}.tag.inv{color:#F0B429;border-color:#F0B42955}
</style></head><body><div class="wrap">
<div class="upd hide" id="upd"><span class="uc">Update</span><div><div class="ut" id="updmsg">Update available</div><div class="ud" id="upddesc"></div></div><div class="sp"></div><a class="updbtn" id="updlink" onclick="doUpdate()" style="cursor:pointer">Update now</a><span class="later" onclick="dismissUpd()">Later</span></div>

<div class="cols">
 <div class="main">
   <div class="logo"><svg viewBox='0 0 97 100' width='17' height='18' style='flex:none'><path fill-rule='evenodd' d='M0 0 H61.8 A35 35 0 0 1 74.8 67.5 L96.6 100 H68.6 Z M30.5 23 H55.3 A12.5 12.5 0 0 1 55.3 48 H30.5 Z' fill='#db0632'/></svg><span class="wm"><span class="lw">ubric</span></span></div>
   <h1 class="h1">Score every page the way an AI crawler would</h1>
   <div class="lede">Enter a website. Rubric crawls every page and grades how citable it is for six engines, then tells you which fix moves the number fastest.</div>
   <div class="engrow"><span>ChatGPT</span><span>Perplexity</span><span>AI Overviews</span><span>Gemini</span><span>Copilot</span><span>Claude</span></div>

   <div class="card" id="form">
     <div class="lab">Website URL</div>
     <div class="inp"><span class="pfx">https://</span><input id="url" placeholder="www.example.com" autofocus></div>
     <button class="runbtn" id="run" onclick="run()">Run audit</button>
     <div class="hint"><span>Crawls the whole site with 6 parallel renderers by default. A 32-page site takes about 2 minutes.</span><button class="optbtn" onclick="toggleOpts()">Options</button></div>
     <div class="opts" id="opts">
       <div><label>Max pages (blank = entire site)</label><input id="maxp" type="number" placeholder="all" min="1"></div>
       <div><label>Parallel renderers</label><select id="workers"><option>4</option><option selected>6</option><option>8</option><option>10</option></select></div>
       <div><label>Site type (scoring profile)</label><select id="stype"><option value="">Auto-detect</option><option value="ecommerce">E-commerce</option><option value="blog">Blog / publisher</option><option value="b2b_saas">B2B SaaS</option><option value="general">General</option></select></div>
       <div><label>Client name (white-label report, optional)</label><input id="client" type="text" placeholder="e.g. Acme Corp"></div>
       <div><label>Intro line (optional)</label><input id="intro" type="text" placeholder="Prepared as part of your Q3 review"></div>
       <div><label>Agency name (white-label, optional)</label><input id="agency" type="text" placeholder="e.g. GoGoChimp"></div>
       <div><label>Logo file path (white-label, optional)</label><input id="logo" type="text" placeholder="C:\path\to\logo.png"></div>
       <div style="display:flex;align-items:center;gap:8px;grid-column:1/-1"><input id="dolinks" type="checkbox" checked style="width:auto"><label style="margin:0">Check for broken links (adds ~30-60s at the end)</label></div>
       <div style="grid-column:1/-1;display:flex;align-items:center;gap:10px"><button class="optbtn" type="button" onclick="scheduleCrawl()">Schedule weekly re-crawl</button><span class="note2" id="schednote">Re-crawls the URL above every Monday, building the Score-over-time trend automatically.</span></div>
     </div>
     <div id="chrome" class="note2"></div>
   </div>

   <div class="card hide" id="progress" style="margin-top:22px">
     <div class="lab" id="phase" style="color:var(--txt);font-size:14px">Starting...</div>
     <div class="bar"><i id="fill"></i></div>
     <div id="count" class="note2" style="margin:0 0 10px"></div>
     <div class="log" id="log"></div>
     <div id="done" class="hide">
       <div class="tiles" id="tiles"></div>
       <a class="open" id="openbtn" target="_blank">Open full report</a>
       <button onclick="reset()" class="optbtn" style="margin-left:10px;padding:11px 18px">Run another</button>
     </div>
   </div>
 </div>

 <div class="side">
   <div class="card"><div class="ph"><div class="t">Recent reports</div><span class="m">stored locally</span></div><div id="recent"></div><div class="note2">Re-run the same site to see a before-and-after on every page.</div></div>
   <div class="card"><div class="ph"><div class="t">What it checks</div></div>
     <div class="chk"><b>01</b><span>Answer-first structure and chunk quality</span></div>
     <div class="chk"><b>02</b><span>Server-rendered schema vs JS-injected</span></div>
     <div class="chk"><b>03</b><span>GPTBot and PerplexityBot reachability</span></div>
     <div class="chk"><b>04</b><span>Entity clarity, sameAs and authorship</span></div>
     <div class="chk"><b>05</b><span>Freshness signals and response times</span></div>
   </div>
   <div class="card" id="mcpcard"><div class="ph"><div class="t">Use inside Claude</div><span class="m" id="mcpdot">checking&hellip;</span></div>
     <div class="note2">Add Rubric to Claude Desktop as an MCP tool, then just ask Claude to audit a site or check a draft.</div>
     <button class="mini" id="mcpbtn" onclick="mcpToggle()">Connect to Claude Desktop</button>
     <div class="note2" id="mcpnote" style="margin-top:8px"></div>
     <div class="note2" style="margin-top:6px"><a href="#" onclick="copyMcp();return false" style="color:var(--muted)">Copy config for Cursor / Claude Code</a></div>
     <label class="note2" style="margin-top:8px;display:flex;gap:6px;align-items:flex-start;cursor:pointer;line-height:1.4"><input type="checkbox" id="tel" style="width:auto;margin-top:2px" onchange="setTel()"><span><b>Anonymous usage stats are on</b> - content-free patterns only (page-count and site-type bands, feature use, re-runs), <b>never your URLs, domains or audit data</b>. Uncheck to turn off.</span></label>
   </div>
   <div class="card"><div class="ph"><div class="t">Watched sites</div><span class="m">local</span></div>
     <div class="note2">Re-crawl on a schedule and get an alert when a score moves. Schedule it with <code>rubric watch install</code>; see changes with <code>rubric watch alerts</code>.</div>
     <div class="inp" style="margin-top:8px"><span class="pfx">https://</span><input id="watchurl" placeholder="www.example.com"></div>
     <button class="mini" id="watchbtn" onclick="addWatch()">Watch this site</button>
     <div id="watchlist" style="margin-top:8px"></div>
   </div>
 </div>
</div>

<div class="tools">
 <div class="card tool">
   <div class="ph"><div class="t">Benchmark vs competitors</div><span class="m">crawls each, capped</span></div>
   <div class="note2">Your site plus up to four competitors, one URL per line. Each is crawled (capped for speed) and scored side by side per pillar and engine.</div>
   <textarea id="benurls" placeholder="https://you.com&#10;https://competitor-a.com&#10;https://competitor-b.com"></textarea>
   <button class="mini" id="benbtn" onclick="runBench()">Benchmark</button>
   <div class="tout" id="benout"></div>
 </div>
 <div class="card tool">
   <div class="ph"><div class="t">Calibrate against real citations</div><span class="m">optional</span></div>
   <div class="note2">Paste your Bing Webmaster Tools &rarr; AI Performance export (<code>url,citations</code>) to see which signals actually predict citations on a site you have crawled.</div>
   <select id="calsite"><option value="">— pick a crawled site —</option></select>
   <textarea id="calcsv" placeholder="https://you.com/page,42&#10;https://you.com/other-page,17"></textarea>
   <button class="mini" id="calbtn" onclick="runCal()">Correlate</button>
   <div class="tout" id="calout"></div>
 </div>
 <div class="card tool">
   <div class="ph"><div class="t">Check a draft before you publish</div><span class="m">no crawl</span></div>
   <div class="note2">Paste draft copy (or enter a URL) and get the citability checks it would pass or fail before it goes live.</div>
   <textarea id="draftc" placeholder="Paste your draft copy here, or leave blank and enter a URL below"></textarea>
   <div class="inp" style="margin-top:8px"><span class="pfx">url</span><input id="drafturl" placeholder="or https://you.com/draft-page"></div>
   <button class="mini" id="draftbtn" onclick="runDraft()">Check draft</button>
   <div class="tout" id="draftout"></div>
 </div>
 <div class="card tool">
   <div class="ph"><div class="t">Compare a page vs a competitor</div><span class="m">crawls both</span></div>
   <div class="note2">Your page and a competitor page, scored side by side, with the signals they have that you lack.</div>
   <div class="inp"><span class="pfx">you</span><input id="cmpyou" placeholder="https://you.com/page"></div>
   <div class="inp" style="margin-top:8px"><span class="pfx">rival</span><input id="cmprival" placeholder="https://competitor.com/page"></div>
   <button class="mini" id="cmpbtn" onclick="runCompare()">Compare pages</button>
   <div class="tout" id="cmpout"></div>
 </div>
 <div class="card tool">
   <div class="ph"><div class="t">Clicks at risk (value bridge)</div><span class="m">crawls + matches CSV</span></div>
   <div class="note2">Crawl your site and match a Search Console Pages export (<code>url,clicks</code>) to put a ranged number on the clicks your un-citable pages put at risk.</div>
   <div class="inp"><span class="pfx">https://</span><input id="valurl" placeholder="www.you.com"></div>
   <textarea id="valcsv" placeholder="https://you.com/page,420&#10;https://you.com/other,180" style="margin-top:8px"></textarea>
   <button class="mini" id="valbtn" onclick="runValue()">Estimate</button>
   <div class="tout" id="valout"></div>
 </div>
 <div class="card tool">
   <div class="ph"><div class="t">Compare two crawls</div><span class="m">score over time</span></div>
   <div class="note2">Pick two stored reports of the same site to see what changed: score, pillars, engines, and which checks were fixed or regressed.</div>
   <select id="cra"><option value="">— newer report —</option></select>
   <select id="crb" style="margin-top:8px"><option value="">— older report —</option></select>
   <button class="mini" id="crbtn" onclick="runCompareReports()">Compare crawls</button>
   <div class="tout" id="crout"></div>
 </div>
</div>

<div class="foot"><span id="ver">Rubric · free for life with the book</span><span class="lk"><a href="https://github.com/GoGoChimp/cited-score" target="_blank">Read the docs</a></span></div>
</div>
<script>
const $=id=>document.getElementById(id);
fetch('/chrome').then(r=>r.json()).then(d=>{
  $('ver').textContent='Rubric '+d.version+' · free for life with the book';
  $('chrome').innerHTML = d.chrome ? '' : '<b class="err">Rubric needs Chrome or Edge to read pages.</b> Install one, then reopen.';});
var _mcpConn=false;
function mcpRender(s){var dot=$('mcpdot'),btn=$('mcpbtn');if(!dot)return;
  _mcpConn=!!(s&&s.connected);
  if(_mcpConn){dot.innerHTML='<span style="color:var(--grn)">&#9679;</span> Connected';btn.textContent='Disconnect';}
  else{dot.innerHTML='<span style="color:var(--muted)">&#9675;</span> Not connected';btn.textContent='Connect to Claude Desktop';}}
function loadMcp(){fetch('/mcp-status').then(r=>r.json()).then(mcpRender).catch(()=>{});}
function mcpToggle(){ if(_mcpConn){disconnectMcp();} else {connectMcp();} }
function connectMcp(){var btn=$('mcpbtn');btn.disabled=true;btn.textContent='Connecting...';
  fetch('/connect-mcp',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json()).then(d=>{btn.disabled=false;
    $('mcpnote').innerHTML = d.ok ? '<b style="color:var(--ok)">Added.</b> Restart Claude Desktop to load Rubric.' : '<b class="err">Could not write config.</b> '+((d&&d.error)||'');
    loadMcp();}).catch(()=>{btn.disabled=false;});}
function disconnectMcp(){var btn=$('mcpbtn');btn.disabled=true;btn.textContent='Disconnecting...';
  fetch('/disconnect-mcp',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json()).then(d=>{btn.disabled=false;
    $('mcpnote').innerHTML = d.ok ? '<b style="color:var(--ok)">Removed.</b> Restart Claude Desktop to unload it.' : '<b class="err">Could not update config.</b> '+((d&&d.error)||'');
    loadMcp();}).catch(()=>{btn.disabled=false;});}
function copyMcp(){fetch('/mcp-config').then(r=>r.json()).then(d=>{navigator.clipboard.writeText(d.snippet);$('mcpnote').textContent='Config copied to clipboard.';});}
function loadTel(){fetch('/telemetry-status').then(r=>r.json()).then(d=>{if($('tel'))$('tel').checked=!!d.consent;}).catch(()=>{});}
function setTel(){fetch('/telemetry-consent',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({consent:$('tel').checked})}).catch(()=>{});}
loadMcp();loadTel();
function scheduleCrawl(){var u=$('url').value.trim();if(!u){$('schednote').textContent='Enter a URL above first.';return;}
  $('schednote').textContent='Scheduling...';
  fetch('/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})}).then(r=>r.json()).then(d=>{
    $('schednote').textContent = d.ok ? 'Scheduled - re-crawls weekly (Mon 9am). Manage it in Windows Task Scheduler.' : (d.note||d.error||'Could not schedule.');
  }).catch(()=>{$('schednote').textContent='Could not schedule.';});}
function loadRecent(){fetch('/reports').then(r=>r.json()).then(list=>{
  $('recent').innerHTML = list.length ? list.map(r=>{const d=r.delta;const dl=(d==null)?'<span class="dl z">first run</span>':`<span class="dl ${d>0?'up':d<0?'dn':'z'}">${d>0?'▲':d<0?'▼':'▬'} ${Math.abs(d)}</span>`;const sc=r.score==null?'':`<div class="sc">${r.score}</div>`;const mt=(r.pages!=null?r.pages+' pages · ':'')+r.when;return `<a href="/report/${r.name}" target="_blank" class="rep">${sc}<div style="flex:1"><div class="nm">${r.name}</div><div class="mt">${mt}</div></div>${dl}</a>`}).join('') : '<div class="note2">No reports yet. Run your first audit.</div>';
  var cs=$('calsite'); if(cs){var cur=cs.value; cs.innerHTML='<option value="">— pick a crawled site —</option>'+list.map(r=>'<option value="'+r.name+'">'+r.name+'</option>').join(''); cs.value=cur;}
  ['cra','crb'].forEach(id=>{var sel=$(id); if(sel){var cur=sel.value, ph=sel.options[0].text; sel.innerHTML='<option value="">'+ph+'</option>'+list.map(r=>'<option value="'+r.name+'">'+r.name+'</option>').join(''); sel.value=cur;}});
  })}
loadRecent();
function loadWatches(){fetch('/watches').then(r=>r.json()).then(d=>{var el=$('watchlist'); if(!el)return;
  var ws=d.watches||[]; el.innerHTML = ws.length ? ws.map(w=>{var ls=(w.last_score==null?'-':w.last_score); return '<div class="rep" style="cursor:default"><div style="flex:1"><div class="nm">'+w.url+'</div><div class="mt">'+(w.cadence||'weekly')+' · last '+ls+'</div></div><button class="optbtn" style="padding:4px 10px" onclick="removeWatch(\''+w.url.replace(/\x27/g,"")+'\')">Stop</button></div>';}).join('') : '<div class="note2">No watched sites yet.</div>';
  }).catch(()=>{});}
function addWatch(){var u=$('watchurl').value.trim(); if(!u)return; if(!/^https?:/.test(u))u='https://'+u;
  fetch('/watch-add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})}).then(r=>r.json()).then(()=>{$('watchurl').value='';loadWatches();}).catch(()=>{});}
function removeWatch(u){fetch('/watch-remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})}).then(()=>loadWatches()).catch(()=>{});}
loadWatches();
function pollJob(job, outId){var out=$(outId); out.textContent='Working...';
  var t=setInterval(()=>fetch('/status/'+job).then(r=>r.json()).then(j=>{ if(!j)return;
    if(j.lines&&j.lines.length) out.textContent=j.lines[j.lines.length-1];
    if(j.finished){clearInterval(t); if(j.error){out.innerHTML='<span class=err>'+j.error+'</span>';return;}
      out.innerHTML='<a class="open" href="'+j.report+'" target="_blank">Open report</a>'; loadRecent();}}),1000);}
function runDraft(){var c=$('draftc').value.trim(), u=$('drafturl').value.trim(); if(!c&&!u){$('draftout').innerHTML='<span class=err>Paste a draft or enter a URL.</span>';return;}
  fetch('/draft',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:c,url:u})}).then(r=>r.json()).then(d=>{if(d.error){$('draftout').innerHTML='<span class=err>'+d.error+'</span>';return;}pollJob(d.job,'draftout');});}
function runCompare(){var y=$('cmpyou').value.trim(), rv=$('cmprival').value.trim(); if(!y||!rv){$('cmpout').innerHTML='<span class=err>Enter both URLs.</span>';return;}
  fetch('/compare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({your_url:y,competitor_url:rv})}).then(r=>r.json()).then(d=>{if(d.error){$('cmpout').innerHTML='<span class=err>'+d.error+'</span>';return;}pollJob(d.job,'cmpout');});}
function runValue(){var u=$('valurl').value.trim(), csv=$('valcsv').value; if(!u||!csv.trim()){$('valout').innerHTML='<span class=err>Enter a URL and paste your CSV.</span>';return;} if(!/^https?:/.test(u))u='https://'+u;
  fetch('/value',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u,csv:csv})}).then(r=>r.json()).then(d=>{if(d.error){$('valout').innerHTML='<span class=err>'+d.error+'</span>';return;}pollJob(d.job,'valout');});}
function runCompareReports(){var a=$('cra').value, b=$('crb').value; if(!a||!b){$('crout').innerHTML='<span class=err>Pick two reports.</span>';return;}
  $('crout').textContent='Comparing...';
  fetch('/compare-reports?a='+encodeURIComponent(a)+'&b='+encodeURIComponent(b)).then(r=>r.json()).then(d=>{ if(d.error){$('crout').innerHTML='<span class=err>'+d.error+'</span>';return;}
    var pd=Object.entries(d.pillar_deltas||{}).map(([k,v])=>k+' '+(v>0?'+':'')+v).join(', ');
    var fx=(d.fixed||[]).map(f=>f.label).join('; ')||'none'; var rg=(d.regressed||[]).map(f=>f.label).join('; ')||'none';
    $('crout').innerHTML='<div class="note2">Score '+(d.score_delta>0?'+':'')+d.score_delta+' ('+d.older.overall+' &rarr; '+d.newer.overall+')<br>Pillars: '+pd+'<br><b>Fixed:</b> '+fx+'<br><b>Regressed:</b> '+rg+'</div>';
  }).catch(()=>{$('crout').innerHTML='<span class=err>Compare failed.</span>';});}
fetch('/update-check').then(r=>r.json()).then(d=>{
  if(d&&d.update){ $('updmsg').textContent='Version '+d.latest+' is available';
    $('upddesc').textContent='You are on v'+d.current+'. The update takes about a minute.';
    $('upd').classList.remove('hide'); }
}).catch(()=>{});
let poll=null;
function run(){
  const url=$('url').value.trim(); if(!url)return;
  $('run').disabled=true; $('form').classList.add('hide'); $('progress').classList.remove('hide'); $('done').classList.add('hide');
  $('log').textContent=''; $('phase').textContent='Discovering URLs...'; $('fill').style.width='0';
  fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,max_pages:$('maxp').value,workers:$('workers').value,client:$('client').value,intro:$('intro').value,agency:$('agency').value,logo:$('logo').value,links:$('dolinks').checked,site_type:$('stype').value})})
    .then(r=>r.json()).then(d=>{ if(d.error){$('phase').innerHTML='<span class=err>'+d.error+'</span>';return;} poll=setInterval(()=>check(d.job),1000); });
}
function check(job){fetch('/status/'+job).then(r=>r.json()).then(j=>{
  if(!j)return;
  const pct = j.total ? Math.round(100*j.done/j.total) : 0;
  $('fill').style.width=pct+'%';
  $('phase').textContent = j.phase==='crawl' ? 'Crawling + rendering...' : j.phase==='links' ? 'Checking outbound links...' : j.phase==='site' ? 'Site-wide checks...' : j.phase==='discover' ? 'Discovering URLs...' : j.phase==='done' ? 'Done' : 'Working...';
  $('count').textContent = j.total ? (j.done+' / '+j.total+(j.phase==='links'?' links':' pages')) : '';
  if(j.lines) $('log').textContent = j.lines.join('\n');
  $('log').scrollTop = $('log').scrollHeight;
  if(j.finished){ clearInterval(poll);
    if(j.error){ $('phase').innerHTML='<span class=err>Error: '+j.error+'</span>'; return; }
    const s=j.summary||{};
    $('tiles').innerHTML =
      tile(s.overall,'Rubric') + tile(s.pages,'Pages') +
      Object.entries(s.pillars||{}).map(([k,v])=>tile(v,k)).join('');
    $('openbtn').href = j.report;
    $('done').classList.remove('hide'); loadRecent();
  }});}
function tile(n,l){return `<div class="tile"><div class="n">${n==null?'-':n}</div><div class="l">${l}</div></div>`}
function reset(){$('progress').classList.add('hide'); $('form').classList.remove('hide'); $('run').disabled=false; $('url').value='';}
function dismissUpd(){$('upd').classList.add('hide')}
function doUpdate(){ fetch('/open-update').catch(()=>{}); $('upddesc').textContent='Downloading in your browser - run the file when it finishes to update.'; }
function toggleOpts(){$('opts').classList.toggle('on')}
function rhoSpan(v){const c=v>0.1?'up':v<0?'dn':'z';return '<span class="rho '+c+'">'+(v>0?'+':'')+v.toFixed(2)+'</span>';}
function runCal(){
  const domain=$('calsite').value, csv=$('calcsv').value;
  if(!domain){$('calout').innerHTML='<span class=err>Pick a crawled site first.</span>';return;}
  $('calbtn').disabled=true; $('calout').textContent='Correlating...';
  fetch('/calibrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({domain,csv})})
   .then(r=>r.json()).then(d=>{$('calbtn').disabled=false;
     if(!d||d.error){$('calout').innerHTML='<span class=err>'+((d&&d.error)||'No result.')+'</span>';return;}
     let h='<div class="note2">Matched '+d.matched+' of '+d.total+' pages. Spearman rho vs real citations - higher predicts citations better:</div><table>';
     h+='<tr><td>Overall</td><td>'+rhoSpan(d.overall)+'</td></tr>';
     h+=Object.entries(d.engines).sort((a,b)=>b[1]-a[1]).map(e=>'<tr><td>'+e[0]+'</td><td>'+rhoSpan(e[1])+'</td></tr>').join('');
     h+='</table>';
     const up=d.checks.filter(c=>c.verdict=='up-weight').slice(0,8);
     h+='<div class="note2" style="margin-top:10px">Signals that vary AND predict here (up-weight):</div><table>';
     h+= up.length? up.map(c=>'<tr><td>'+c.label+'</td><td>'+rhoSpan(c.rho)+'</td><td><span class="tag up">up-weight</span></td></tr>').join('')
                  : '<tr><td class="note2">Nothing separated cited from uncited on this sample - your passing checks are table stakes.</td></tr>';
     h+='</table>';
     $('calout').innerHTML=h;}).catch(e=>{$('calbtn').disabled=false;$('calout').innerHTML='<span class=err>'+e+'</span>';});
}
let bpoll=null;
function runBench(){
  const urls=$('benurls').value.split('\n').map(s=>s.trim()).filter(Boolean);
  if(urls.length<2){$('benout').innerHTML='<span class=err>Enter your site plus at least one competitor.</span>';return;}
  $('benbtn').disabled=true; $('benout').textContent='Crawling '+urls.length+' sites (capped for speed)...';
  fetch('/benchmark',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls})})
   .then(r=>r.json()).then(d=>{ if(!d||d.error){$('benbtn').disabled=false;$('benout').innerHTML='<span class=err>'+((d&&d.error)||'error')+'</span>';return;}
     bpoll=setInterval(()=>checkBench(d.job),1500);}).catch(e=>{$('benbtn').disabled=false;$('benout').innerHTML='<span class=err>'+e+'</span>';});
}
function checkBench(job){fetch('/status/'+job).then(r=>r.json()).then(j=>{
  if(!j)return;
  if(!j.finished){$('benout').textContent=(j.lines&&j.lines.length?j.lines[j.lines.length-1]:'Crawling...');return;}
  clearInterval(bpoll); $('benbtn').disabled=false;
  if(j.error){$('benout').innerHTML='<span class=err>Error: '+j.error+'</span>';return;}
  const rows=(j.benchmark||[]).filter(x=>x.overall!=null);
  if(!rows.length){$('benout').innerHTML='<span class=err>No sites could be scored - check the URLs.</span>';return;}
  const eng=['ChatGPT','Perplexity','AI Overviews','Gemini','Copilot','Claude'];
  const cols=['overall','Known','Findable','Trusted'].concat(eng);
  const abbr={overall:'Rubric',Known:'Kn',Findable:'Fi',Trusted:'Tr','AI Overviews':'AIO',ChatGPT:'GPT',Perplexity:'PPLX',Gemini:'GEM',Copilot:'CPLT',Claude:'CLDE'};
  const val=(r,c)=> c=='overall'?r.overall : (c=='Known'||c=='Findable'||c=='Trusted')?r.pillars[c] : r.engines[c];
  const best={}; cols.forEach(c=>best[c]=Math.max.apply(0,rows.map(r=>val(r,c)||0)));
  let h='<table><tr><th>Site</th>'+cols.map(c=>'<th>'+(abbr[c]||c)+'</th>').join('')+'</tr>';
  h+=rows.map(r=>'<tr><td>'+r.domain+'</td>'+cols.map(c=>{const v=val(r,c);return '<td class="'+(v===best[c]?'best':'')+'">'+(v==null?'-':v)+'</td>';}).join('')+'</tr>').join('');
  h+='</table><div class="note2" style="margin-top:8px">Best in each column highlighted. Crawl is page-capped - run a full audit on a site for its detail.</div>';
  $('benout').innerHTML=h;});
}
</script></body></html>"""

ACTIVATE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Activate Rubric</title><link rel="icon" href="__FAV__"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400..900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#14140f;--panel:#191914;--panel2:#1e1e18;--line:#2a2a24;--muted:#a8a495;--txt:#f2f0e4;--grn:#db0632;--grn2:#ef1a48;--ok:#3DD68C;--red:#db0632;--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--display:'Archivo',sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 'Archivo',-apple-system,Segoe UI,Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.box{width:100%;max-width:440px}
.logo{display:inline-flex;align-items:center;gap:11px;margin-bottom:24px}
.logo .wm{display:inline-flex;align-items:baseline;gap:8px}
.logo .lw{font-family:'Archivo',sans-serif;font-weight:800;font-size:25px;letter-spacing:-.056em;color:var(--txt);line-height:1;margin-left:-1px}
.logo .ls{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.24em;color:var(--muted);text-transform:uppercase}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:28px}
h1{font-family:var(--display);font-weight:800;font-size:26px;letter-spacing:-.4px;margin:0 0 8px}
.sub{color:var(--muted);font-size:15px;margin:0 0 6px}
label{display:block;font-family:var(--display);font-weight:800;text-transform:uppercase;letter-spacing:.6px;font-size:11px;color:var(--muted);margin:18px 0 7px}
input{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:11px;padding:13px 14px;font-size:16px;outline:none}
input:focus{border-color:var(--grn)}
input#code{font-family:var(--mono);letter-spacing:.22em;text-transform:uppercase}
.btn{width:100%;background:var(--grn);color:#0a0a0a;border:0;border-radius:11px;padding:15px;font-family:var(--display);font-weight:800;font-size:16px;text-transform:uppercase;letter-spacing:.5px;cursor:pointer;margin-top:22px}
.btn:hover:not(:disabled){background:var(--grn2)}.btn:disabled{opacity:.5;cursor:default}
.msg{margin-top:16px;font-size:14px;min-height:20px}
.msg.err{color:var(--red)}.msg.ok{color:var(--ok)}
.alt{margin-top:20px;text-align:center;font-size:13px;color:var(--muted)}
.alt a{color:var(--grn);cursor:pointer}
.more{display:none;margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
.more.on{display:block}
.hint{color:var(--muted);font-size:12px;margin-top:7px}
.foot{text-align:center;color:var(--muted);font-size:12px;margin-top:20px}
</style></head><body><div class="box">
<div class="logo"><svg viewBox='0 0 97 100' width='17' height='18' style='flex:none'><path fill-rule='evenodd' d='M0 0 H61.8 A35 35 0 0 1 74.8 67.5 L96.6 100 H68.6 Z M30.5 23 H55.3 A12.5 12.5 0 0 1 55.3 48 H30.5 Z' fill='#db0632'/></svg><span class="wm"><span class="lw">ubric</span></span></div>
<div class="card">
  <h1>Activate your copy</h1>
  <p class="sub">Enter the code from your download email. Free for life, up to 500 URLs.</p>
  <label for="email">Email</label>
  <input id="email" type="email" placeholder="you@company.com" autofocus>
  <label for="code">Activation code</label>
  <input id="code" placeholder="XXXXXX" maxlength="6">
  <button class="btn" id="go" onclick="activate()">Activate</button>
  <div class="msg" id="msg"></div>
  <div class="alt">Don&#39;t have a code? <a onclick="toggleMore()">Email me one</a></div>
  <div class="more" id="more">
    <label for="book">Book code (optional)</label>
    <input id="book" placeholder="From the back of the book">
    <div class="hint">Have the book? Enter its code for a free year of everything.</div>
    <button class="btn" id="send" onclick="sendCode()" style="margin-top:14px">Email me a code</button>
  </div>
</div>
<div class="foot">One-time activation. Your email unlocks the app and nothing else.</div>
</div>
<script>
const $=id=>document.getElementById(id);
function msg(t,c){const m=$('msg'); m.textContent=t; m.className='msg '+(c||'');}
function toggleMore(){$('more').classList.toggle('on')}
function activate(){
  const email=$('email').value.trim(), code=$('code').value.trim();
  if(!email||!code){msg('Enter your email and the code.','err');return;}
  $('go').disabled=true; msg('Activating...','');
  fetch('/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,code})})
    .then(r=>r.json()).then(d=>{
      if(d.ok){msg('Activated. Loading Rubric...','ok'); setTimeout(()=>location.href='/',700);}
      else{$('go').disabled=false; msg(d.error||'That code did not work.','err');}
    }).catch(()=>{$('go').disabled=false; msg('Could not reach the server.','err');});
}
function sendCode(){
  const email=$('email').value.trim(), book=$('book').value.trim();
  if(!email){msg('Enter your email first.','err');return;}
  $('send').disabled=true; msg('Sending...','');
  fetch('/request-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,book_code:book})})
    .then(r=>r.json()).then(d=>{$('send').disabled=false;
      if(d.ok){
        if(d.code){msg('Your code: '+d.code+'  (also emailed). Paste it above to activate.','ok');}
        else{msg('Sent. Check your inbox for the code.','ok');}
      }
      else{msg(d.error||'Could not send a code.','err');}
    }).catch(()=>{$('send').disabled=false; msg('Could not reach the server.','err');});
}
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def _json(self, code, obj): self._send(code, json.dumps(obj), "application/json")
    def log_message(self, *a): pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            page = INDEX if (is_activated() or _pro_ok()) else ACTIVATE
            return self._send(200, page.replace("__FAV__", A.FAVICON))
        if u.path == "/chrome": return self._json(200, {"chrome": A.CHROME, "version": VERSION})
        if u.path == "/update-check": return self._json(200, {**check_update(), "current": APP_VERSION})
        if u.path == "/mcp-status": return self._json(200, mcp_status())
        if u.path == "/mcp-config": return self._json(200, {"snippet": mcp_config_snippet()})
        if u.path == "/connect": return self._send(200, _wshell("Rubric", _WCONNECT, "connect"))
        if u.path == "/reports-view": return self._send(200, _wshell("Reports - Rubric", _WREPORTS, "reports"))
        if u.path == "/activate-pro": return self._send(200, _wshell("Unlock Rubric", _ACTIVATE_WIN, ""))
        if u.path == "/connect-status":
            import connectors
            tools = connectors.list_tools()
            tools.append({"id": "chatgpt", "name": "ChatGPT", "kind": "remote", "status": "coming_soon"})
            return self._json(200, tools)
        if u.path == "/audit": return self._send(200, _shell("Run audit - Rubric", _AUDIT_BODY, "audit"))
        if u.path == "/watches-page": return self._send(200, _shell("Watches - Rubric", _WATCHES_BODY, "watches"))
        if u.path == "/settings": return self._send(200, _wshell("Settings - Rubric", _SETTINGS_BODY, "settings"))
        if u.path == "/licence-status":
            import licence
            try:
                d = licence.load() or {}
                return self._json(200, {"is_pro": bool(licence.is_pro()),
                                        "key_prefix": licence.key_prefix(d.get("key")) if d.get("key") else None})
            except Exception as e:
                return self._json(200, {"is_pro": False, "error": str(e)[:120]})
        if u.path == "/startup-status":
            import startup
            return self._json(200, {"on": startup.is_enabled()})
        if u.path == "/skills-status":
            import skills_install
            return self._json(200, {"installed": skills_install.installed(),
                                    "bundled": skills_install.bundled_count(),
                                    "dir": skills_install.default_dir()})
        if u.path == "/open-reports":
            try: os.startfile(REPORTS)                              # Windows; opens the local report folder
            except Exception: pass
            return self._json(200, {"ok": True})
        if u.path == "/telemetry-status": return self._json(200, {"consent": telemetry_consent()})
        if u.path == "/open-update":
            try: webbrowser.open(f"https://github.com/{GITHUB_REPO}/releases/latest/download/CITED-Score.exe")
            except Exception: pass
            return self._json(200, {"ok": True})
        if u.path == "/reports":
            files = [f for f in glob.glob(os.path.join(REPORTS, "*.html"))]
            files.sort(key=os.path.getmtime, reverse=True)
            files = files[:50]                                   # library: keep the full local history on disk; list the 50 most recent (no destructive prune)
            items = []
            for f in files:
                it = {"name": os.path.basename(f)[:-5],
                      "when": time.strftime("%d %b %Y, %H:%M", time.localtime(os.path.getmtime(f)))}
                try:
                    jd = json.load(open(f[:-5] + ".json", encoding="utf-8"))
                    it["score"] = jd.get("overall"); it["pages"] = jd.get("pages_crawled")
                    it["delta"] = (jd.get("diff") or {}).get("overall_d")
                except Exception: pass
                items.append(it)
            return self._json(200, items)
        if u.path == "/watches":
            try:
                import watch_store
                return self._json(200, {"watches": watch_store.list_watches(), "alerts": watch_store.list_alerts(10)})
            except Exception as e:
                return self._json(200, {"watches": [], "alerts": [], "error": str(e)[:120]})
        if u.path == "/compare-reports":
            q = urllib.parse.parse_qs(u.query)
            a = safe(q.get("a", [""])[0]); b = safe(q.get("b", [""])[0])
            try:
                da = json.load(open(os.path.join(REPORTS, a + ".json"), encoding="utf-8"))
                db = json.load(open(os.path.join(REPORTS, b + ".json"), encoding="utf-8"))
            except Exception:
                return self._json(400, {"error": "Pick two crawled reports to compare."})
            return self._json(200, _compare_reports(da, db))
        if u.path.startswith("/status/"):
            return self._json(200, JOBS.get(u.path.rsplit("/", 1)[-1], {}))
        if u.path.startswith("/report/"):
            name = safe(urllib.parse.unquote(u.path.split("/", 2)[2]))
            p = os.path.join(REPORTS, name + ".html")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f: return self._send(200, f.read())
            return self._send(404, "report not found")
        return self._send(404, "not found")

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        try: body = json.loads(self.rfile.read(ln) or "{}")
        except Exception: body = {}
        if self.path == "/activate": return self._activate(body)
        if self.path == "/request-code": return self._request_code(body)
        if self.path == "/connect-mcp": record_usage("feature", f="mcp_connect"); return self._json(200, connect_mcp())
        if self.path == "/disconnect-mcp": return self._json(200, disconnect_mcp())
        if self.path == "/connect-tool":
            import connectors
            tool = (body.get("tool") or "").strip()
            if tool not in connectors.TOOLS:                       # rejects chatgpt (grey) + unknown ids
                return self._json(400, {"ok": False, "error": "That tool cannot be connected here."})
            record_usage("feature", f="connect_" + tool)
            return self._json(200, connectors.install(tool))
        if self.path == "/startup-toggle":
            import startup
            on = bool(body.get("on"))
            ok = startup.enable() if on else startup.disable()
            return self._json(200, {"ok": ok, "on": startup.is_enabled()})
        if self.path == "/activate-key":
            import licence
            ok, msg = licence.activate((body.get("key") or "").strip())   # verifies online, never logs the key
            return self._json(200, {"ok": bool(ok), "message": msg})
        if self.path == "/skills-install":
            import skills_install
            try:
                res = skills_install.install()
                record_usage("feature", f="skills_install")
                return self._json(200, res)
            except Exception as e:
                return self._json(200, {"ok": False, "error": str(e)[:160]})
        if self.path == "/schedule": record_usage("feature", f="schedule"); return self._json(200, schedule_crawl((body.get("url") or "").strip()))
        if self.path == "/watch-add":
            import watch_store; wu = (body.get("url") or "").strip()
            if not wu: return self._json(400, {"error": "Enter a URL to watch."})
            watch_store.add(wu, (body.get("cadence") or "weekly")); return self._json(200, {"ok": True})
        if self.path == "/watch-remove":
            import watch_store; return self._json(200, {"ok": watch_store.remove((body.get("url") or "").strip())})
        if self.path == "/telemetry-consent": return self._json(200, {"ok": set_telemetry_consent(body.get("consent"))})
        if self.path == "/self-update": return self._json(200, self_update())   # BETA/gated; banner still uses /open-update
        if not (is_activated() or _pro_ok()): return self._json(403, {"error": "Activate Rubric, or unlock with a Pro licence (rubric activate), to run audits."})
        if self.path == "/run": return self._run(body)
        if self.path == "/calibrate": record_usage("feature", f="calibrate"); return self._calibrate(body)
        if self.path == "/benchmark": record_usage("feature", f="benchmark"); return self._benchmark(body)
        if self.path == "/draft": record_usage("feature", f="draft"); return self._draft(body)
        if self.path == "/compare": record_usage("feature", f="compare"); return self._compare(body)
        if self.path == "/value": record_usage("feature", f="value"); return self._value(body)
        return self._send(404, "not found")

    def _job_report(self, name, worker_body, total=1):
        """Shared async-job runner for the draft/compare/value cards: writes a report into REPORTS and exposes
        /report/<name> + a /status/<job> the card polls. worker_body(job, setrep) does the engine work."""
        job = str(int(time.time() * 1000))
        JOBS[job] = {"phase": "start", "done": 0, "total": total, "lines": [], "finished": False, "report": None, "error": None, "summary": None}
        def prog(phase, done, tot, msg):
            j = JOBS[job]; j["phase"] = phase; j["done"] = done; j["total"] = tot or total; j["lines"] = (j["lines"] + [msg])[-14:]
        def run():
            try: worker_body(job, prog, "/report/" + name, os.path.join(REPORTS, name + ".html"))
            except Exception as e: JOBS[job]["error"] = str(e)
            JOBS[job]["finished"] = True
        threading.Thread(target=run, daemon=True).start()
        return self._json(200, {"job": job})

    def _draft(self, body):
        content = (body.get("content") or "").strip(); url = (body.get("url") or "").strip()
        if not content and not url: return self._json(400, {"error": "Paste draft content or enter a URL."})
        def wk(job, prog, rep, path):
            c = content
            if not c and url:                                    # URL-only: fetch the page so we check its real content, not an empty doc
                st, _h, html, _ms = A.fetch_raw(url)
                if not html: raise RuntimeError("Could not fetch that URL to check (it may be down or blocking crawlers).")
                c = html
            res = A.check_draft(c, url)
            A.write_draft_html(res, path)
            JOBS[job]["report"] = rep; JOBS[job]["summary"] = {"verdict": res.get("verdict")}
        return self._job_report("draft-" + str(int(time.time())), wk)

    def _compare(self, body):
        your_url = (body.get("your_url") or "").strip(); comp = (body.get("competitor_url") or "").strip()
        if not your_url or not comp: return self._json(400, {"error": "Enter your page URL and a competitor page URL."})
        dom = urllib.parse.urlparse(your_url if your_url.startswith("http") else "https://" + your_url).netloc.replace("www.", "")
        def wk(job, prog, rep, path):
            data = A.page_compare(your_url, comp, progress=prog)
            A.write_compare_html(data, path)
            JOBS[job]["report"] = rep; JOBS[job]["summary"] = {"overall": (data.get("you") or {}).get("score")}
        return self._job_report("compare-" + safe(dom or "pages"), wk, total=2)

    def _value(self, body):
        url = (body.get("url") or "").strip(); csv_text = (body.get("csv") or "")
        if not url or not csv_text.strip(): return self._json(400, {"error": "Enter a URL and paste your Search Console Pages CSV (url,clicks)."})
        dom = urllib.parse.urlparse(url if url.startswith("http") else "https://" + url).netloc.replace("www.", "")
        name = "value-" + safe(dom or "site")
        def wk(job, prog, rep, path):
            csv_path = os.path.join(REPORTS, name + "-perf.csv")
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                f.write(csv_text if csv_text.endswith("\n") else csv_text + "\n")
            vb = A.value_bridge(url, csv_path, progress=prog)
            A.write_value_html(vb, path)
            JOBS[job]["report"] = rep; JOBS[job]["summary"] = {"overall": None}
        return self._job_report(name, wk)

    def _activate(self, body):
        email = (body.get("email") or "").strip()
        code = (body.get("code") or "").strip()
        if not email or not code:
            return self._json(400, {"error": "Enter your email and the code."})
        status, d = sb_post("verify-code", {"email": email, "code": code, "app_version": APP_VERSION})
        if status == 200 and d.get("ok") and d.get("token"):
            save_activation({"token": d["token"], "email": d.get("email") or email,
                             "segment": d.get("segment"), "tier": d.get("tier"),
                             "app_version": APP_VERSION, "activated_at": int(time.time())})
            return self._json(200, {"ok": True, "segment": d.get("segment"), "tier": d.get("tier")})
        return self._json(200, {"ok": False, "error": d.get("error") or "That code did not work."})

    def _request_code(self, body):
        email = (body.get("email") or "").strip()
        book = (body.get("book_code") or "").strip()
        if not email:
            return self._json(400, {"error": "Enter your email first."})
        base = {"email": email, "source": "app"}
        if book: base["book_code"] = book
        # Attempt the email (best-effort). The Loops workflow may NOT re-fire for a contact that already
        # entered it once, so a re-request can silently send nothing - which is the bug this fixes.
        status, d = sb_post("request-code", {**base, "send": True})
        # Durable fix: also read the code directly (send:false returns it WITHOUT triggering the one-shot
        # email workflow) so the app can ALWAYS show it, regardless of whether the email went out.
        code = None
        try:
            _s2, d2 = sb_post("request-code", {**base, "send": False})
            if _s2 == 200 and isinstance(d2, dict):
                code = d2.get("code") or d2.get("personalCode") or d2.get("personal_code")
        except Exception:
            pass
        if status == 200 and (d.get("ok") or code):
            out = {"ok": True}
            if code: out["code"] = code   # shown in the UI so email delivery is never the only path
            return self._json(200, out)
        err = (d.get("error") or "Could not send a code.")
        if "not configured" in err:
            err = "Code email isn't switched on yet - use the code from your download email."
        return self._json(200, {"ok": False, "error": err})

    def _calibrate(self, body):
        dom = safe((body.get("domain") or "").strip())
        if not dom: return self._json(400, {"error": "Pick a crawled site to calibrate."})
        jp = os.path.join(REPORTS, dom + ".json")
        if not os.path.exists(jp): return self._json(400, {"error": "No crawl found for that site - run an audit first."})
        try:
            d = json.load(open(jp, encoding="utf-8"))
            return self._json(200, A.calibrate_data(d, A.parse_cites(body.get("csv") or "")))
        except Exception as e:
            return self._json(500, {"error": str(e)[:200]})

    def _benchmark(self, body):
        urls = [u.strip() for u in (body.get("urls") or []) if u and u.strip()]
        if len(urls) < 2: return self._json(400, {"error": "Enter at least two sites (yours + a competitor)."})
        if len(urls) > 5: urls = urls[:5]
        try: maxp = int(body.get("max_pages") or 25)
        except ValueError: maxp = 25
        job = str(int(time.time() * 1000))
        JOBS[job] = {"phase": "start", "done": 0, "total": len(urls), "lines": [], "finished": False, "benchmark": None, "error": None}
        def prog(phase, done, total, msg):
            j = JOBS[job]; j["phase"] = phase; j["done"] = done; j["total"] = total; j["lines"] = (j["lines"] + [msg])[-14:]
        def worker():
            try: JOBS[job]["benchmark"] = A.benchmark(urls, max_pages=maxp, progress=prog)
            except Exception as e: JOBS[job]["error"] = str(e)
            JOBS[job]["finished"] = True
        threading.Thread(target=worker, daemon=True).start()
        return self._json(200, {"job": job})

    def _run(self, body):
        url = (body.get("url") or "").strip()
        if not url: return self._json(400, {"error": "Enter a website URL."})
        try:
            maxp = int(body.get("max_pages") or 0)
            workers = int(body.get("workers") or A.WORKERS)
        except ValueError:
            return self._json(400, {"error": "Max pages / workers must be numbers."})
        parsed = urllib.parse.urlparse(url if url.startswith("http") else "https://" + url)
        dom = parsed.netloc.replace("www.", "")
        if not dom: return self._json(400, {"error": "That does not look like a URL."})
        client = (body.get("client") or "").strip() or None
        stype = (body.get("site_type") or "").strip() or None
        intro = (body.get("intro") or "").strip() or None
        agency = (body.get("agency") or "").strip() or None
        logo = (body.get("logo") or "").strip() or None
        dolinks = body.get("links") is not False   # default True unless explicitly unchecked
        base = os.path.join(REPORTS, safe(dom))
        job = str(int(time.time() * 1000))
        JOBS[job] = {"phase": "start", "done": 0, "total": 0, "lines": [], "finished": False,
                     "report": None, "domain": dom, "error": None, "summary": None}
        def prog(phase, done, total, msg):
            j = JOBS[job]; j["phase"] = phase; j["done"] = done; j["total"] = total
            j["lines"] = (j["lines"] + [msg])[-14:]
        def worker():
            try:
                data = A.run_audit(url, out=base, max_pages=maxp, workers=workers, progress=prog, client=client, intro=intro, links=dolinks, site_type=stype, agency=agency, logo=logo)
                JOBS[job]["report"] = "/report/" + safe(dom)
                JOBS[job]["summary"] = {"overall": data["overall"], "pages": data["pages_crawled"],
                                        "pillars": data["pillars"], "engines": data["engines"]}
                try:                                             # anonymous usage event - no URL/domain/content leaves
                    _hist = base + "-history.jsonl"; _n = 0
                    if os.path.exists(_hist):
                        with open(_hist, encoding="utf-8") as _hf: _n = sum(1 for _ in _hf)
                    record_usage("audit", pages=_pbucket(data.get("pages_crawled")),
                                 site_type=(data.get("site_type") or "general"),
                                 score=_sbucket(data.get("overall")), rerun=(_n > 1),
                                 wl=bool(client), links=bool(dolinks))
                    threading.Thread(target=upload_telemetry, daemon=True).start()
                except Exception: pass
            except Exception as e:
                JOBS[job]["error"] = str(e)
            JOBS[job]["finished"] = True
        threading.Thread(target=worker, daemon=True).start()
        return self._json(200, {"job": job})

# ============================================================================
# Launcher pages (tray-opened focused surfaces). Small, shared shell reusing the
# Rubric web palette + logo. These are what the tray menu items open in the browser,
# so auditing and reading a report never need an AI, and the Connect grid (Ollama
# style) wires the local MCP into each AI tool. See the tray-launcher design spec.
# ============================================================================
_LOGO = ("<div class='logo'><svg viewBox='0 0 97 100' width='17' height='18' style='flex:none'>"
         "<path fill-rule='evenodd' d='M0 0 H61.8 A35 35 0 0 1 74.8 67.5 L96.6 100 H68.6 Z "
         "M30.5 23 H55.3 A12.5 12.5 0 0 1 55.3 48 H30.5 Z' fill='#db0632'/></svg>"
         "<span class='wm'><span class='lw'>ubric</span></span></div>")

_HEAD = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__T__</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400..900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>:root{--bg:#14140f;--panel:#191914;--panel2:#1e1e18;--line:#2a2a24;--muted:#a8a495;--txt:#f2f0e4;--grn:#db0632;--grn2:#ef1a48;--ok:#3DD68C;--red:#db0632;--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--display:'Archivo',sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 'Archivo',-apple-system,Segoe UI,Arial,sans-serif}
.wrap{max-width:820px;margin:0 auto;padding:26px 26px 60px}
a{color:var(--grn);text-decoration:none}
.logo{display:inline-flex;align-items:baseline;gap:0;margin-bottom:6px}
.logo .wm{display:inline-flex;align-items:baseline;gap:8px}.logo .lw{font-family:'Archivo',sans-serif;font-weight:800;font-size:25px;letter-spacing:-.056em;color:var(--txt);line-height:1;margin-left:-1px}
.h1{font-family:var(--display);font-weight:800;font-size:30px;line-height:1.02;letter-spacing:-.02em;margin:10px 0 6px}
.lede{color:var(--muted);margin:0 0 22px;max-width:600px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px;margin-bottom:18px}
.inp{display:flex;align-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:0 14px}.inp .pfx{color:var(--muted)}.inp input{flex:1;background:none;border:0;color:var(--txt);font-size:16px;padding:14px 6px;outline:none}
.btn{background:var(--grn);color:#0a0a0a;border:0;border-radius:10px;padding:12px 18px;font-family:var(--display);font-weight:800;text-transform:uppercase;letter-spacing:.4px;font-size:14px;cursor:pointer}.btn:hover:not(:disabled){background:var(--grn2)}.btn:disabled{opacity:.5;cursor:default}
.ghost{background:none;border:1px solid var(--line);color:var(--txt)}
.muted{color:var(--muted);font-size:13px}.err{color:var(--red)}.ok{color:var(--ok)}
.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.nav{display:flex;gap:18px;margin-bottom:22px;font-size:14px}.nav a{color:var(--muted)}.nav a.on{color:var(--txt);font-weight:700}
.field{margin-top:12px}.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
.field input{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:11px 12px;font-size:14px;outline:none}
.bar{height:9px;background:#2a2320;border-radius:6px;overflow:hidden;margin:14px 0}.bar i{display:block;height:100%;background:var(--grn);width:0;transition:width .3s}
.log{font:12px/1.5 var(--mono);color:var(--muted);background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px;max-height:160px;overflow:auto;white-space:pre-wrap}
</style></head><body><div class="wrap">__LOGO__
<div class="nav"><a href="/audit" __A_audit__>Run audit</a><a href="/connect" __A_connect__>Connect</a><a href="/watches-page" __A_watches__>Watches</a><a href="/settings" __A_settings__>Settings</a></div>
"""

def _shell(title, body, active=""):
    head = _HEAD.replace("__T__", title).replace("__LOGO__", _LOGO)
    for k in ("audit", "connect", "watches", "settings"):
        head = head.replace("__A_" + k + "__", "class='on'" if k == active else "")
    return head + body + "</div></body></html>"

_CONNECT_BODY = r"""
<h1 class="h1">Connect Rubric to your AI tool</h1>
<div class="lede">One click wires the local Rubric MCP into your tool, so you can ask it to audit a site and reason on the result in context. Auditing a site and reading a report never need an AI, use Run audit and the reports directly.</div>
<div id="grid" class="grid"></div>
<div id="msg" class="muted" style="margin:16px 0"></div>
<style>
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
.gt{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}
.gt .ic{width:40px;height:40px;border-radius:10px;background:var(--panel2);display:flex;align-items:center;justify-content:center;font-family:var(--display);font-weight:800;font-size:15px;color:var(--txt);flex:none}
.gt .nm{font-weight:800}.gt .st{font-size:12px;color:var(--muted);margin-top:2px}
.gt .sp{flex:1}
.gt button{background:var(--grn);color:#0a0a0a;border:0;border-radius:9px;padding:9px 15px;font-family:var(--display);font-weight:800;font-size:12px;text-transform:uppercase;letter-spacing:.4px;cursor:pointer}
.gt button:hover:not(:disabled){background:var(--grn2)}
.gt.soon{opacity:.5}.gt.soon button{background:none;border:1px solid var(--line);color:var(--muted);cursor:default}
.gt.connected button{background:none;border:1px solid var(--line);color:var(--ok);cursor:default}
.gt.connected .ic{color:var(--ok)}
.snip{grid-column:1/-1;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:14px;font:12px/1.5 var(--mono);color:var(--muted);white-space:pre-wrap}
</style>
<script>
const ICON={'claude-desktop':'CD','claude-code':'CC','cursor':'Cu','codex':'Cx','cline':'Cn','chatgpt':'GP'};
function statusText(t){
  if(t.status==='coming_soon')return 'Needs an internet-reachable server';
  if(t.status==='connected')return 'Rubric is connected';
  if(t.status==='manual')return 'Manual paste (verify)';
  if(t.status==='unknown')return 'Click to connect';
  return 'Not connected';
}
async function load(){
  const r=await fetch('/connect-status'); const tools=await r.json();
  const g=document.getElementById('grid'); g.innerHTML='';
  for(const t of tools){
    const soon=t.status==='coming_soon', conn=t.status==='connected', manual=t.status==='manual';
    const d=document.createElement('div'); d.className='gt'+(soon?' soon':'')+(conn?' connected':'');
    const label=soon?'Coming soon':(conn?'Connected':(manual?'Show config':'Connect'));
    d.innerHTML='<div class="ic">'+(ICON[t.id]||'?')+'</div><div><div class="nm">'+t.name+'</div><div class="st">'+statusText(t)+'</div></div><div class="sp"></div><button '+((soon||conn)?'disabled':'')+' onclick="connect(\''+t.id+'\',this)">'+label+'</button>';
    g.appendChild(d);
  }
}
function showMsg(text,warn){ const m=document.getElementById('msg'); m.textContent=text||''; m.className=warn?'err':'muted'; }
async function connect(id,btn){
  btn.disabled=true; btn.textContent='...'; showMsg('');
  let d={};
  try{ const r=await fetch('/connect-tool',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tool:id})}); d=await r.json(); }
  catch(e){ showMsg('Could not reach the local Rubric server.',true); btn.disabled=false; return; }
  if(d.snippet){ const g=document.getElementById('grid'); const s=document.createElement('div'); s.className='snip'; s.textContent=(d.message||'')+"\n\n"+d.snippet; g.appendChild(s); }
  else if(d.error){ showMsg(d.error,true); }
  else if(d.message){ showMsg(d.message, d.ok===false || !!d.replaced); }  // surface the backup/replaced warning, not just a silent green tile
  setTimeout(load,300);
}
load();
</script>
"""

_AUDIT_BODY = r"""
<h1 class="h1">Run an audit</h1>
<div class="lede">Paste a URL. Rubric crawls every page locally and scores how citable it is for six AI engines. Private, staging and localhost sites work too, nothing leaves this machine.</div>
<div class="card">
  <div class="inp"><span class="pfx">https://</span><input id="url" placeholder="www.example.com" autofocus></div>
  <div class="field"><label>Agency name (optional, white-labels the report)</label><input id="agency" placeholder="Your agency"></div>
  <div class="row" style="margin-top:16px"><button class="btn" id="go" onclick="run()">Run audit</button><span class="muted" id="msg"></span></div>
  <div id="prog" style="display:none"><div class="bar"><i id="pbar"></i></div><div class="log" id="log"></div></div>
  <div id="done" style="display:none;margin-top:16px"><a class="btn" id="open" href="#" target="_blank">Open report</a></div>
</div>
<script>
let job=null,timer=null;
function setmsg(t,cls){ const m=document.getElementById('msg'); m.textContent=t||''; m.className=cls||'muted'; }
async function run(){
  const url=document.getElementById('url').value.trim(); if(!url)return;
  const agency=document.getElementById('agency').value.trim();
  document.getElementById('go').disabled=true; setmsg('Starting...');
  document.getElementById('prog').style.display='block'; document.getElementById('done').style.display='none';
  const r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url,agency:agency})});
  const d=await r.json();
  if(d.error){ setmsg(d.error,'err'); document.getElementById('go').disabled=false; return; }
  job=d.job; timer=setInterval(poll,900);
}
async function poll(){
  const r=await fetch('/status/'+job); const j=await r.json();
  const pct=j.total?Math.round(100*j.done/j.total):5;
  document.getElementById('pbar').style.width=Math.max(pct,5)+'%';
  document.getElementById('log').textContent=(j.lines||[]).join('\n');
  setmsg(j.phase||'');
  if(j.finished){
    clearInterval(timer); document.getElementById('go').disabled=false;
    if(j.error){ setmsg(j.error,'err'); return; }
    if(j.report){ const o=document.getElementById('open'); o.href=j.report; document.getElementById('done').style.display='block';
      const sc=(j.summary||{}).overall; setmsg('Done'+(sc!=null?', score '+sc:''),'ok'); window.open(j.report,'_blank'); }
  }
}
document.getElementById('url').addEventListener('keydown',e=>{if(e.key==='Enter')run();});
</script>
"""

_WATCHES_BODY = r"""
<h1 class="h1">Watches</h1>
<div class="lede">Rubric re-crawls a watched site on a schedule and flags a score change. Re-crawls only, never an AI-answer capture.</div>
<div class="card"><div class="row"><div class="inp" style="flex:1"><span class="pfx">https://</span><input id="wurl" placeholder="www.example.com"></div><button class="btn" onclick="add()">Watch</button></div></div>
<div class="card"><div id="list" class="muted">Loading...</div></div>
<div class="card"><div style="font-weight:800;margin-bottom:8px">Recent alerts</div><div id="alerts" class="muted">None yet.</div></div>
<script>
function esc(s){return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function load(){
  const r=await fetch('/watches'); const d=await r.json();
  const l=document.getElementById('list');
  if(!(d.watches||[]).length){ l.textContent='No watched sites yet.'; }
  else{ l.innerHTML=d.watches.map(w=>'<div class="row" style="justify-content:space-between;padding:9px 0;border-top:1px solid var(--line)"><span>'+esc(w.url)+' <span class=muted>['+esc(w.cadence||'')+'] last '+esc(w.last_score!=null?w.last_score:'-')+'</span></span><a href="#" class="rmv muted" data-url="'+esc(w.url)+'">Remove</a></div>').join(''); }
  const a=document.getElementById('alerts');
  a.innerHTML=(d.alerts||[]).length? d.alerts.map(x=>'<div style="padding:6px 0">'+esc(x.url)+': '+esc(x.old)+' &rarr; '+esc(x.new)+'</div>').join('') : 'None yet.';
}
async function add(){ const u=document.getElementById('wurl').value.trim(); if(!u)return; await fetch('/watch-add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})}); document.getElementById('wurl').value=''; load(); }
async function rm(u){ await fetch('/watch-remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u})}); load(); }
document.getElementById('list').addEventListener('click',e=>{const a=e.target.closest('.rmv'); if(a){e.preventDefault(); rm(a.getAttribute('data-url'));}});
load();
</script>
"""

_SETTINGS_BODY = r"""
<h1 class="h1">Licence &amp; settings</h1>
<div class="card"><div style="font-weight:800;margin-bottom:6px">Licence</div><div id="lic" class="muted">Checking...</div></div>
<div class="card"><div class="row" style="justify-content:space-between">
  <div><div style="font-weight:800">Start Rubric with Windows</div><div class="muted">Keeps the tray running so watches fire and Connect is one click.</div></div>
  <label class="row" style="gap:8px"><input type="checkbox" id="startup" onchange="toggle()"> <span id="stlab" class="muted">Off</span></label>
</div></div>
<div class="card"><div style="font-weight:800;margin-bottom:6px">Reports</div><div class="muted">Your crawl reports live on this machine.</div>
  <div style="margin-top:10px"><button class="btn ghost" onclick="fetch('/open-reports')">Open reports folder</button></div></div>
<script>
async function lic(){ const r=await fetch('/licence-status'); const d=await r.json(); document.getElementById('lic').innerHTML = d.is_pro? ('<span class=ok>Pro unlocked</span> - key '+(d.key_prefix||'')) : '<a href="/activate-pro">Unlock Rubric Pro</a> to enable crawling and connections.'; }
async function st(){ const r=await fetch('/startup-status'); const d=await r.json(); document.getElementById('startup').checked=!!d.on; document.getElementById('stlab').textContent=d.on?'On':'Off'; }
async function toggle(){ const on=document.getElementById('startup').checked; await fetch('/startup-toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on:on})}); st(); }
lic(); st();
</script>
"""

# ============================================================================
# Ollama-style launcher WINDOWS (light). The tray opens these as chromeless app windows
# (Edge/Chrome --app), not browser tabs. Connect is the home (left-click / "Open"); Reports and
# Settings are reachable from the top nav. Clean light design modelled on Ollama's app launcher.
# ============================================================================
_WHEAD = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>__T__</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400..900&display=swap" rel="stylesheet">
<style>:root{--bg:#f6f5f1;--panel:#fff;--panel2:#faf9f6;--line:#e7e5dd;--muted:#8b8878;--txt:#1b1b17;--red:#db0632;--red2:#ef1a48;--ok:#1a9d5a;--display:'Archivo',sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 'Archivo',-apple-system,Segoe UI,Arial,sans-serif}
.top{display:flex;align-items:center;gap:12px;padding:15px 24px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.logo{display:inline-flex;align-items:baseline;gap:0}.logo .wm{display:inline-flex;align-items:baseline}.logo .lw{font-family:'Archivo',sans-serif;font-weight:800;font-size:22px;letter-spacing:-.056em;color:var(--txt);line-height:1;margin-left:-1px}
.nav{display:flex;gap:4px;margin-left:auto}
.nav a{color:var(--muted);padding:7px 13px;border-radius:9px;font-size:14px;font-weight:600;text-decoration:none}
.nav a.on{background:#efeee8;color:var(--txt)}.nav a:hover{background:#f2f1eb}
.wrap{max-width:880px;margin:0 auto;padding:30px 24px 60px}
.h1{font-family:var(--display);font-weight:800;font-size:26px;letter-spacing:-.02em;margin:0 0 6px}
.lede{color:var(--muted);margin:0 0 12px;max-width:580px}
.seclabel{font-family:var(--display);font-weight:800;text-transform:uppercase;letter-spacing:.14em;font-size:11px;color:var(--muted);margin:24px 0 14px}
.muted{color:var(--muted);font-size:13px}.err{color:var(--red)}.ok{color:var(--ok)}
.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
.btn{background:var(--red);color:#fff;border:0;border-radius:9px;padding:10px 16px;font-family:var(--display);font-weight:800;font-size:13px;cursor:pointer}.btn:hover{background:var(--red2)}
.ghost{background:#fff;border:1px solid var(--line);color:var(--txt)}
.field{margin-top:12px}.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
.field input{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:11px 12px;font-size:14px}
</style></head><body>
<div class="top">__LOGO__<div class="nav"><a href="/connect" __A_connect__>Connect</a><a href="/reports-view" __A_reports__>Reports</a><a href="/settings" __A_settings__>Settings</a></div></div>
<div class="wrap">"""

def _wshell(title, body, active=""):
    head = _WHEAD.replace("__T__", title).replace("__LOGO__", _LOGO)
    for k in ("connect", "reports", "settings"):
        head = head.replace("__A_" + k + "__", "class='on'" if k == active else "")
    return head + body + "</div></body></html>"

_WCONNECT = r"""
<h1 class="h1">Connect Rubric to your AI tool</h1>
<div class="lede">One click wires Rubric's local engine into your tool. Then ask it to audit a site and reason on the result in context. Reports open under Reports, no AI needed.</div>
<div class="card" style="display:flex;align-items:center;gap:14px">
  <div style="flex:1"><div style="font-weight:800">Rubric skills</div><div class="muted" id="skmsg">Install the skill pack so Claude runs the fix loop and the focused workflows.</div></div>
  <button class="btn" id="skbtn" onclick="installSkills()">Install skills</button>
</div>
<div class="seclabel">Apps</div>
<div id="grid" class="grid"></div>
<div id="msg" class="muted" style="margin-top:16px"></div>
<style>
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
@media(max-width:620px){.grid{grid-template-columns:1fr}}
.gt{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:15px 16px;transition:box-shadow .12s}
.gt:hover{box-shadow:0 2px 14px rgba(0,0,0,.05)}
.gt .ic{width:40px;height:40px;border-radius:10px;background:#efeee8;display:flex;align-items:center;justify-content:center;font-family:var(--display);font-weight:800;font-size:14px;color:var(--txt);flex:none}
.gt .nm{font-weight:800;font-size:15px}.gt .st{font-size:12px;color:var(--muted);margin-top:1px}
.gt .sp{flex:1}
.gt button{background:var(--red);color:#fff;border:0;border-radius:9px;padding:9px 15px;font-family:var(--display);font-weight:800;font-size:12px;cursor:pointer}
.gt button:hover:not(:disabled){background:var(--red2)}
.gt.soon{opacity:.5}.gt.soon button{background:#efeee8;color:var(--muted);cursor:default}
.gt.connected button{background:#eaf6ef;color:var(--ok);cursor:default}
.gt.connected .ic{background:#eaf6ef;color:var(--ok)}
.snip{grid-column:1/-1;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:13px;font:12px/1.5 ui-monospace,Consolas,monospace;color:#555;white-space:pre-wrap}
</style>
<script>
const ICON={'claude-desktop':'CD','claude-code':'CC','cursor':'Cu','codex':'Cx','cline':'Cn','chatgpt':'GP'};
function statusText(t){
  if(t.status==='coming_soon')return 'Needs an internet-reachable server';
  if(t.status==='connected')return 'Rubric is connected';
  if(t.status==='manual')return 'Manual paste (verify)';
  if(t.status==='unknown')return 'Click to connect';
  return 'Not connected';
}
function showMsg(text,warn){ const m=document.getElementById('msg'); m.textContent=text||''; m.className=warn?'err':'muted'; }
async function load(){
  const r=await fetch('/connect-status'); const tools=await r.json();
  const g=document.getElementById('grid'); g.innerHTML='';
  for(const t of tools){
    const soon=t.status==='coming_soon', conn=t.status==='connected', manual=t.status==='manual';
    const d=document.createElement('div'); d.className='gt'+(soon?' soon':'')+(conn?' connected':'');
    const label=soon?'Coming soon':(conn?'Connected':(manual?'Show config':'Connect'));
    d.innerHTML='<div class="ic">'+(ICON[t.id]||'?')+'</div><div><div class="nm">'+t.name+'</div><div class="st">'+statusText(t)+'</div></div><div class="sp"></div><button '+((soon||conn)?'disabled':'')+' onclick="connect(\''+t.id+'\',this)">'+label+'</button>';
    g.appendChild(d);
  }
}
async function connect(id,btn){
  btn.disabled=true; btn.textContent='...'; showMsg('');
  let d={};
  try{ const r=await fetch('/connect-tool',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tool:id})}); d=await r.json(); }
  catch(e){ showMsg('Could not reach the local Rubric server.',true); btn.disabled=false; return; }
  if(d.snippet){ const g=document.getElementById('grid'); const s=document.createElement('div'); s.className='snip'; s.textContent=(d.message||'')+"\n\n"+d.snippet; g.appendChild(s); }
  else if(d.error){ showMsg(d.error,true); }
  else if(d.message){ showMsg(d.message, d.ok===false || !!d.replaced); }
  setTimeout(load,300);
}
async function skillsStatus(){
  try{ const r=await fetch('/skills-status'); const d=await r.json(); const have=d.installed||[];
    document.getElementById('skmsg').textContent = have.length ? ('Installed '+have.length+' of '+d.bundled+' skills in '+d.dir) : ('Install '+d.bundled+' skills into '+d.dir+' so Claude runs the fix loop and the focused workflows.');
    if(have.length){ document.getElementById('skbtn').textContent='Reinstall'; }
  }catch(e){}
}
async function installSkills(){
  const b=document.getElementById('skbtn'); b.disabled=true; b.textContent='...';
  try{ const r=await fetch('/skills-install',{method:'POST'}); const d=await r.json();
    document.getElementById('skmsg').textContent = d.ok ? ('Installed '+(d.installed||[]).length+' skills to '+d.dir+'. Restart your AI tool to load them.') : 'Could not install the skills.';
  }catch(e){ document.getElementById('skmsg').textContent='Could not reach the local Rubric server.'; }
  b.disabled=false; b.textContent='Reinstall'; skillsStatus();
}
load(); skillsStatus();
</script>
"""

_WREPORTS = r"""
<h1 class="h1">Recent reports</h1>
<div class="lede">Every crawl Rubric has run on this machine. Click to open a report.</div>
<div id="list" class="muted">Loading...</div>
<style>
.rrow{display:flex;align-items:center;gap:16px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px;cursor:pointer;transition:box-shadow .12s}
.rrow:hover{box-shadow:0 2px 14px rgba(0,0,0,.05)}
.rrow .sc{font-family:var(--display);font-weight:800;font-size:30px;color:var(--red);width:56px;flex:none;text-align:center}
.rrow .nm{font-weight:800}.rrow .mt{color:var(--muted);font-size:12px;margin-top:2px}
.rrow .op{margin-left:auto;color:var(--red);font-weight:800;font-size:13px;white-space:nowrap}
</style>
<script>
function esc(s){return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function load(){
  const r=await fetch('/reports'); const items=await r.json();
  const l=document.getElementById('list');
  if(!items.length){ l.textContent='No reports yet. Ask a connected AI tool to audit a site, or run one from the CLI (rubric audit --url ...).'; return; }
  l.className='';
  l.innerHTML=items.map(it=>'<div class="rrow" onclick="open2(\''+encodeURIComponent(it.name)+'\')"><div class="sc">'+(it.score!=null?it.score:'-')+'</div><div><div class="nm">'+esc(it.name)+'</div><div class="mt">'+esc(it.when||'')+(it.pages!=null?' | '+it.pages+' pages':'')+'</div></div><div class="op">Open &rarr;</div></div>').join('');
}
function open2(n){ location.href='/report/'+n; }
load();
</script>
"""

_ACTIVATE_WIN = r"""
<h1 class="h1">Unlock Rubric Pro</h1>
<div class="lede">Paste your Rubric Pro key to unlock this machine. You will find it in your account at cited.gogochimp.com. It unlocks private and staging crawling, the local MCP, and the skill pack, all running locally.</div>
<div class="card">
  <div class="field"><label>Pro key</label><input id="key" placeholder="cs_live_..." autofocus autocomplete="off"></div>
  <div class="row" style="margin-top:14px"><button class="btn" id="go" onclick="activate()">Unlock</button><span class="muted" id="msg"></span></div>
</div>
<div class="muted" style="margin-top:8px">No key yet? Get one at <a href="https://cited.gogochimp.com/pricing" target="_blank">cited.gogochimp.com/pricing</a>.</div>
<script>
async function activate(){
  const key=document.getElementById('key').value.trim(); if(!key) return;
  const b=document.getElementById('go'), m=document.getElementById('msg');
  b.disabled=true; m.className='muted'; m.textContent='Checking...';
  try{
    const r=await fetch('/activate-key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key})});
    const d=await r.json();
    m.textContent=d.message||''; m.className=d.ok?'ok':'err';
    if(d.ok){ setTimeout(()=>location.href='/connect', 900); }
  }catch(e){ m.className='err'; m.textContent='Could not reach the local Rubric server.'; }
  b.disabled=false;
}
document.getElementById('key').addEventListener('keydown',e=>{if(e.key==='Enter')activate();});
</script>
"""

def start_server(port=PORT):
    """Start the HTTP server on a daemon thread; return the actual bound port (0 = OS picks)."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    def _heartbeat():
        record_usage("launch")                                       # anonymous launch ping -> active installs + return-days
        upload_telemetry()
    threading.Thread(target=_heartbeat, daemon=True).start()         # default-on, no-op unless consented
    return srv.server_address[1]

def main():
    port = start_server(PORT)
    url = f"http://127.0.0.1:{port}/"
    print(f"Rubric dashboard is running at {url}")
    print("This full window is optional. The primary way to use Rubric is the tray: rubric tray")
    print("Leave this window open. Close it (Ctrl+C) to stop the dashboard.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try: threading.Event().wait()
    except KeyboardInterrupt: print("\nStopped.")

if __name__ == "__main__": main()
