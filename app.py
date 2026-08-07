#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CITED Score - local desktop app / shell.
Run:  python app.py    (opens http://127.0.0.1:5000 in your browser)
Enter any website, click Run, watch the crawl, open the report. No command line needed.
Uses the same engine as aiseo_audit.py (your installed Chrome renders each page).
Zero third-party web deps - Python standard library only, so it packages cleanly to an .exe.
"""
import os, re, sys, json, threading, time, webbrowser, urllib.parse, urllib.request, urllib.error, glob, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import aiseo_audit as A

APP_VERSION = "0.12.3"                # semver; bump on every release + tag the GitHub release to match
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

def sb_post(fn, payload, timeout=12):
    """POST to a CITED Score Edge Function. Returns (status, dict). No secret ever ships here -
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

INDEX = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CITED Score</title><link rel="icon" href="__FAV__"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400..900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0F1410;--panel:#161D18;--panel2:#0A0F0B;--line:#26302A;--muted:#6E7A6F;--txt:#EDF0EB;--grn:#42D848;--grn2:#74E67A;--ok:#3DD68C;--red:#E0533D;--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--display:'Archivo',sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 'Archivo',-apple-system,Segoe UI,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:26px 26px 60px}
a{color:var(--grn);text-decoration:none}
.logo{display:inline-flex;align-items:center;gap:11px}
.logo .wm{display:inline-flex;align-items:baseline;gap:8px}
.logo .lw{font-family:'Archivo',sans-serif;font-weight:900;font-size:23px;letter-spacing:-.02em;color:var(--txt);line-height:1}
.logo .ls{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.24em;color:var(--muted);text-transform:uppercase}
.upd{display:flex;align-items:center;gap:16px;background:rgba(66,216,72,.07);border:1px solid rgba(66,216,72,.32);border-radius:14px;padding:14px 18px;margin-bottom:26px}
.upd .uc{background:var(--grn);color:#0a0a0a;font-weight:800;font-size:11px;letter-spacing:.5px;padding:3px 8px;border-radius:5px}
.upd .ut{font-weight:800}.upd .ud{color:var(--muted);font-size:13px}.upd .sp{flex:1}
.updbtn{background:var(--grn);color:#0a0a0a;font-weight:800;padding:9px 16px;border-radius:9px;white-space:nowrap}
.later{color:#fff;font-weight:600;cursor:pointer;padding:9px 10px}
.cols{display:grid;grid-template-columns:1fr 360px;gap:30px;align-items:start}
@media(max-width:960px){.cols{grid-template-columns:1fr}.side{margin-top:0}}
.h1{font-family:var(--display);font-weight:800;text-transform:uppercase;font-size:44px;line-height:1.03;letter-spacing:-.5px;margin:16px 0 0}
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
.opts{display:none;gap:14px;margin-top:14px}.opts.on{display:flex}.opts>div{flex:1}
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
   <div class="logo"><svg viewBox='0 0 200 200' width='29' height='29' style='flex:none'><path d='M148.3,50.1 A72,72 0 1 0 158.7,127' fill='none' stroke='#EDF0EB' stroke-width='17' stroke-linecap='square'/><path d='M70,101 L92,123 L135.7,67.5' fill='none' stroke='#42D848' stroke-width='17' stroke-linecap='square'/></svg><span class="wm"><span class="lw">CITED</span><span class="ls">Score</span></span></div>
   <h1 class="h1">Score every page the way an AI crawler would</h1>
   <div class="lede">Enter a website. CITED Score crawls every page and grades how citable it is for six engines, then tells you which fix moves the number fastest.</div>
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
       <div style="display:flex;align-items:center;gap:8px;grid-column:1/-1"><input id="dolinks" type="checkbox" checked style="width:auto"><label style="margin:0">Check for broken links (adds ~30-60s at the end)</label></div>
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
</div>

<div class="foot"><span id="ver">CITED Score · free for life with the book</span><span class="lk"><a href="https://github.com/GoGoChimp/cited-score" target="_blank">Read the docs</a></span></div>
</div>
<script>
const $=id=>document.getElementById(id);
fetch('/chrome').then(r=>r.json()).then(d=>{
  $('ver').textContent='CITED Score '+d.version+' · free for life with the book';
  $('chrome').innerHTML = d.chrome ? '' : '<b class="err">CITED Score needs Chrome or Edge to read pages.</b> Install one, then reopen.';});
function loadRecent(){fetch('/reports').then(r=>r.json()).then(list=>{
  $('recent').innerHTML = list.length ? list.map(r=>{const d=r.delta;const dl=(d==null)?'<span class="dl z">first run</span>':`<span class="dl ${d>0?'up':d<0?'dn':'z'}">${d>0?'▲':d<0?'▼':'▬'} ${Math.abs(d)}</span>`;const sc=r.score==null?'':`<div class="sc">${r.score}</div>`;const mt=(r.pages!=null?r.pages+' pages · ':'')+r.when;return `<a href="/report/${r.name}" target="_blank" class="rep">${sc}<div style="flex:1"><div class="nm">${r.name}</div><div class="mt">${mt}</div></div>${dl}</a>`}).join('') : '<div class="note2">No reports yet. Run your first audit.</div>';
  var cs=$('calsite'); if(cs){var cur=cs.value; cs.innerHTML='<option value="">— pick a crawled site —</option>'+list.map(r=>'<option value="'+r.name+'">'+r.name+'</option>').join(''); cs.value=cur;}
  })}
loadRecent();
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
  fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,max_pages:$('maxp').value,workers:$('workers').value,client:$('client').value,intro:$('intro').value,links:$('dolinks').checked,site_type:$('stype').value})})
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
      tile(s.overall,'CITED Score') + tile(s.pages,'Pages') +
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
  const abbr={overall:'CITED',Known:'Kn',Findable:'Fi',Trusted:'Tr','AI Overviews':'AIO',ChatGPT:'GPT',Perplexity:'PPLX',Gemini:'GEM',Copilot:'CPLT',Claude:'CLDE'};
  const val=(r,c)=> c=='overall'?r.overall : (c=='Known'||c=='Findable'||c=='Trusted')?r.pillars[c] : r.engines[c];
  const best={}; cols.forEach(c=>best[c]=Math.max.apply(0,rows.map(r=>val(r,c)||0)));
  let h='<table><tr><th>Site</th>'+cols.map(c=>'<th>'+(abbr[c]||c)+'</th>').join('')+'</tr>';
  h+=rows.map(r=>'<tr><td>'+r.domain+'</td>'+cols.map(c=>{const v=val(r,c);return '<td class="'+(v===best[c]?'best':'')+'">'+(v==null?'-':v)+'</td>';}).join('')+'</tr>').join('');
  h+='</table><div class="note2" style="margin-top:8px">Best in each column highlighted. Crawl is page-capped - run a full audit on a site for its detail.</div>';
  $('benout').innerHTML=h;});
}
</script></body></html>"""

ACTIVATE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Activate CITED Score</title><link rel="icon" href="__FAV__"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400..900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#0F1410;--panel:#161D18;--panel2:#0A0F0B;--line:#26302A;--muted:#6E7A6F;--txt:#EDF0EB;--grn:#42D848;--grn2:#74E67A;--ok:#3DD68C;--red:#E0533D;--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--display:'Archivo',sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 'Archivo',-apple-system,Segoe UI,Arial,sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px}
.box{width:100%;max-width:440px}
.logo{display:inline-flex;align-items:center;gap:11px;margin-bottom:24px}
.logo .wm{display:inline-flex;align-items:baseline;gap:8px}
.logo .lw{font-family:'Archivo',sans-serif;font-weight:900;font-size:23px;letter-spacing:-.02em;color:var(--txt);line-height:1}
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
<div class="logo"><svg viewBox='0 0 200 200' width='29' height='29' style='flex:none'><path d='M148.3,50.1 A72,72 0 1 0 158.7,127' fill='none' stroke='#EDF0EB' stroke-width='17' stroke-linecap='square'/><path d='M70,101 L92,123 L135.7,67.5' fill='none' stroke='#42D848' stroke-width='17' stroke-linecap='square'/></svg><span class="wm"><span class="lw">CITED</span><span class="ls">Score</span></span></div>
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
      if(d.ok){msg('Activated. Loading CITED Score...','ok'); setTimeout(()=>location.href='/',700);}
      else{$('go').disabled=false; msg(d.error||'That code did not work.','err');}
    }).catch(()=>{$('go').disabled=false; msg('Could not reach the server.','err');});
}
function sendCode(){
  const email=$('email').value.trim(), book=$('book').value.trim();
  if(!email){msg('Enter your email first.','err');return;}
  $('send').disabled=true; msg('Sending...','');
  fetch('/request-code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,book_code:book})})
    .then(r=>r.json()).then(d=>{$('send').disabled=false;
      if(d.ok){msg('Sent. Check your inbox for the code.','ok');}
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
            page = INDEX if is_activated() else ACTIVATE
            return self._send(200, page.replace("__FAV__", A.FAVICON))
        if u.path == "/chrome": return self._json(200, {"chrome": A.CHROME, "version": VERSION})
        if u.path == "/update-check": return self._json(200, {**check_update(), "current": APP_VERSION})
        if u.path == "/open-update":
            try: webbrowser.open(f"https://github.com/{GITHUB_REPO}/releases/latest/download/CITED-Score.exe")
            except Exception: pass
            return self._json(200, {"ok": True})
        if u.path == "/reports":
            files = [f for f in glob.glob(os.path.join(REPORTS, "*.html"))]
            files.sort(key=os.path.getmtime, reverse=True)
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
        if not is_activated(): return self._json(403, {"error": "Activate CITED Score to run audits."})
        if self.path == "/run": return self._run(body)
        if self.path == "/calibrate": return self._calibrate(body)
        if self.path == "/benchmark": return self._benchmark(body)
        return self._send(404, "not found")

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
        payload = {"email": email, "source": "app", "send": True}
        if book: payload["book_code"] = book
        status, d = sb_post("request-code", payload)
        if status == 200 and d.get("ok"):
            return self._json(200, {"ok": True})
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
                data = A.run_audit(url, out=base, max_pages=maxp, workers=workers, progress=prog, client=client, intro=intro, links=dolinks, site_type=stype)
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
