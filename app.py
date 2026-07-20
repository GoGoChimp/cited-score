#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CITED Score - local desktop app / shell.
Run:  python app.py    (opens http://127.0.0.1:5000 in your browser)
Enter any website, click Run, watch the crawl, open the report. No command line needed.
Uses the same engine as aiseo_audit.py (your installed Chrome renders each page).
Zero third-party web deps - Python standard library only, so it packages cleanly to an .exe.
"""
import os, re, sys, json, threading, time, webbrowser, urllib.parse, urllib.request, glob, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import aiseo_audit as A

APP_VERSION = "0.1.0"                 # semver; bump on every release + tag the GitHub release to match
GITHUB_REPO = "GoGoChimp/cited-score" # public repo that hosts the releases (update check reads /releases/latest)
VERSION = f"v{APP_VERSION} - July 2026"

_update = {"checked": False, "update": False, "latest": None, "url": None}
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

INDEX = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CITED Score</title><link rel="icon" href="__FAV__"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Anton&family=Figtree:wght@300..900&display=swap" rel="stylesheet">
<style>
:root{--bg:#0E110E;--panel:#171B16;--panel2:#14251A;--line:#26281F;--muted:#9A9284;--txt:#F0EBE0;--grn:#42D949;--grn2:#6BEA71;--amber:#F5A623;--red:#F16A5F;--chip:#D63B2F}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 'Figtree',-apple-system,Segoe UI,Arial,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:40px 22px}
.logo{font-family:'Anton',sans-serif;font-size:34px;font-weight:400;letter-spacing:-.4px;text-transform:uppercase}.chip{background:var(--grn);color:#0a0a0a;font-family:'Figtree',sans-serif;font-weight:800;font-size:13px;padding:2px 6px;border-radius:5px;vertical-align:super;margin-left:5px}
.sub{color:var(--muted);margin:6px 0 28px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:20px}
label{display:block;font-size:12px;color:var(--muted);margin:0 0 6px}
input,select{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:10px;padding:13px 14px;font-size:16px}
.row{display:flex;gap:14px;margin-top:14px}.row>div{flex:1}
button{margin-top:18px;width:100%;background:var(--grn);color:#08110a;border:0;border-radius:10px;padding:14px;font-size:16px;font-weight:800;cursor:pointer}
button:disabled{opacity:.5;cursor:default}button:hover:not(:disabled){background:var(--grn2)}
.bar{height:10px;background:#243024;border-radius:6px;overflow:hidden;margin:10px 0}.bar i{display:block;height:100%;background:var(--grn);width:0;transition:width .3s}
.log{font:12px/1.5 ui-monospace,Consolas,monospace;color:var(--muted);background:#0b0e0b;border:1px solid var(--line);border-radius:10px;padding:12px;height:170px;overflow:auto;white-space:pre-wrap}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}
.tile{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px;text-align:center}.tile .n{font-size:24px;font-weight:800}.tile .l{font-size:11px;color:var(--muted)}
.open{display:inline-block;margin-top:6px;background:var(--grn);color:#08110a;font-weight:800;padding:11px 18px;border-radius:9px}
.recent a{display:flex;justify-content:space-between;color:var(--txt);text-decoration:none;padding:9px 12px;border:1px solid var(--line);border-radius:9px;margin-bottom:8px;background:var(--panel2)}
.recent a:hover{border-color:var(--grn)}.recent .d{color:var(--muted);font-size:12px}
a{color:var(--grn)}.hide{display:none}.err{color:var(--red)}
.upd{background:var(--panel2);border:1px solid var(--grn);border-radius:12px;padding:12px 16px;margin-bottom:18px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.upd a{background:var(--grn);color:#08110a;font-weight:800;padding:8px 14px;border-radius:8px;white-space:nowrap;text-decoration:none}
</style></head><body><div class="wrap">
<div class="upd hide" id="upd"><span id="updmsg"></span><a id="updlink" target="_blank">Update now</a></div>
<div class="logo">CITED<span class="chip">Score</span></div>
<div class="sub">Enter a website; it crawls every page and scores how citable it is for ChatGPT, Perplexity, AI Overviews, Gemini, Copilot and Claude.</div>

<div class="card" id="form">
  <label>Website URL</label>
  <input id="url" placeholder="www.example.com" autofocus>
  <div class="row">
    <div><label>Max pages (blank = entire site)</label><input id="maxp" type="number" placeholder="all" min="1"></div>
    <div><label>Parallel renderers</label><select id="workers"><option>4</option><option selected>6</option><option>8</option><option>10</option></select></div>
  </div>
  <button id="run" onclick="run()">Run audit</button>
  <div id="chrome" class="sub" style="margin:12px 0 0"></div>
</div>

<div class="card hide" id="progress">
  <div id="phase" style="font-weight:700">Starting...</div>
  <div class="bar"><i id="fill"></i></div>
  <div id="count" class="sub" style="margin:0 0 10px"></div>
  <div class="log" id="log"></div>
  <div id="done" class="hide">
    <div class="tiles" id="tiles"></div>
    <a class="open" id="openbtn" target="_blank">Open full report</a>
    <button onclick="reset()" style="width:auto;margin-left:10px;background:var(--panel2);color:var(--txt);border:1px solid var(--line)">Run another</button>
  </div>
</div>

<h3 style="margin-top:34px">Recent reports</h3>
<div class="recent" id="recent"></div>
<div id="ver" style="margin-top:30px;color:var(--muted);font-size:12px"></div>
</div>
<script>
const $=id=>document.getElementById(id);
fetch('/chrome').then(r=>r.json()).then(d=>{
  $('ver').textContent='CITED Score '+d.version;
  $('chrome').innerHTML = d.chrome ? 'Renderer: '+d.chrome : '<b class="err">CITED Score needs a browser to read pages.</b><br>It renders each page with Chrome or Edge, and neither was found on this computer. Install Google Chrome or Microsoft Edge, then reopen CITED Score.';});
function loadRecent(){fetch('/reports').then(r=>r.json()).then(list=>{
  $('recent').innerHTML = list.length ? list.map(r=>`<a href="/report/${r.name}" target="_blank"><span>${r.name}</span><span class="d">${r.when}</span></a>`).join('') : '<div class="sub">None yet.</div>';})}
loadRecent();
fetch('/update-check').then(r=>r.json()).then(d=>{
  if(d&&d.update){ $('updmsg').innerHTML='A newer CITED Score is available (<b>'+d.latest+'</b>). You have v'+d.current+'.';
    $('updlink').href=d.url; $('upd').classList.remove('hide'); }
}).catch(()=>{});
let poll=null;
function run(){
  const url=$('url').value.trim(); if(!url)return;
  $('run').disabled=true; $('form').classList.add('hide'); $('progress').classList.remove('hide'); $('done').classList.add('hide');
  $('log').textContent=''; $('phase').textContent='Discovering URLs...'; $('fill').style.width='0';
  fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,max_pages:$('maxp').value,workers:$('workers').value})})
    .then(r=>r.json()).then(d=>{ if(d.error){$('phase').innerHTML='<span class=err>'+d.error+'</span>';return;} poll=setInterval(()=>check(d.job),1000); });
}
function check(job){fetch('/status/'+job).then(r=>r.json()).then(j=>{
  if(!j)return;
  const pct = j.total ? Math.round(100*j.done/j.total) : 0;
  $('fill').style.width=pct+'%';
  $('phase').textContent = j.phase==='crawl' ? 'Crawling + rendering...' : j.phase==='site' ? 'Site-wide checks...' : j.phase==='discover' ? 'Discovering URLs...' : j.phase==='done' ? 'Done' : 'Working...';
  $('count').textContent = j.total ? (j.done+' / '+j.total+' pages') : '';
  if(j.lines) $('log').textContent = j.lines.join('\n');
  $('log').scrollTop = $('log').scrollHeight;
  if(j.finished){ clearInterval(poll);
    if(j.error){ $('phase').innerHTML='<span class=err>Error: '+j.error+'</span>'; return; }
    const s=j.summary||{};
    $('tiles').innerHTML =
      tile(s.overall,'CITED Score') + tile(s.pages,'Pages') +
      Object.entries(s.pillars||{}).map(([k,v])=>tile(v,k)).join('');
    $('openbtn').href = j.report;
    $('done').classList.remove('hide'); loadRecent();
  }});}
function tile(n,l){return `<div class="tile"><div class="n">${n==null?'-':n}</div><div class="l">${l}</div></div>`}
function reset(){$('progress').classList.add('hide'); $('form').classList.remove('hide'); $('run').disabled=false; $('url').value='';}
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
        if u.path == "/": return self._send(200, INDEX.replace("__FAV__", A.FAVICON))
        if u.path == "/chrome": return self._json(200, {"chrome": A.CHROME, "version": VERSION})
        if u.path == "/update-check": return self._json(200, {**check_update(), "current": APP_VERSION})
        if u.path == "/reports":
            files = [f for f in glob.glob(os.path.join(REPORTS, "*.html"))]
            files.sort(key=os.path.getmtime, reverse=True)
            items = [{"name": os.path.basename(f)[:-5],
                      "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(f)))} for f in files]
            return self._json(200, items)
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
        if self.path != "/run": return self._send(404, "not found")
        ln = int(self.headers.get("Content-Length", 0))
        try: body = json.loads(self.rfile.read(ln) or "{}")
        except Exception: body = {}
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
        base = os.path.join(REPORTS, safe(dom))
        job = str(int(time.time() * 1000))
        JOBS[job] = {"phase": "start", "done": 0, "total": 0, "lines": [], "finished": False,
                     "report": None, "domain": dom, "error": None, "summary": None}
        def prog(phase, done, total, msg):
            j = JOBS[job]; j["phase"] = phase; j["done"] = done; j["total"] = total
            j["lines"] = (j["lines"] + [msg])[-14:]
        def worker():
            try:
                data = A.run_audit(url, out=base, max_pages=maxp, workers=workers, progress=prog)
                JOBS[job]["report"] = "/report/" + safe(dom)
                JOBS[job]["summary"] = {"overall": data["overall"], "pages": data["pages_crawled"],
                                        "pillars": data["pillars"], "engines": data["engines"]}
            except Exception as e:
                JOBS[job]["error"] = str(e)
            JOBS[job]["finished"] = True
        threading.Thread(target=worker, daemon=True).start()
        return self._json(200, {"job": job})

def start_server(port=PORT):
    """Start the HTTP server on a daemon thread; return the actual bound port (0 = OS picks)."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]

def main():
    port = start_server(PORT)
    url = f"http://127.0.0.1:{port}/"
    print(f"CITED Score is running at {url}")
    print("Leave this window open. Close it (Ctrl+C) to stop the app.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try: threading.Event().wait()
    except KeyboardInterrupt: print("\nStopped.")

if __name__ == "__main__": main()
