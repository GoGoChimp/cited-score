#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CITED Score - the companion auditor to the book CITED (Chris McCarron / GoGoChimp).
A "Screaming Frog for AEO/GEO/AI-SEO": crawls the entire site, renders each page
(headless Chrome), and scores how citable/extractable it is for AI search - overall,
by pillar (Known / Findable / Trusted = the three questions an engine asks), and per
engine (ChatGPT, Perplexity, Google AI Overviews, Gemini, Copilot, Claude).

Outputs a branded, tabbed HTML report that ENDS IN AN ACTION PLAN (ranked fixes with
projected score gain + a 30/60/90-day roadmap), plus JSON and CSV.

Ruleset obeys CITED, not generic GEO folklore:
  - llms.txt is INFORMATIONAL, not scored (no citation correlation, ch5).
  - sections are judged "self-contained, no walls of text", not a word-count band (ch5).
  - answer capsule target is 40-60 words (ch5).
Every check carries an evidence line + chapter ref. The score estimates citability;
it does not measure citations. Calibrate against real Bing data with --calibrate.

  python aiseo_audit.py --url https://www.example.com --out report [--max-pages 0]
  python aiseo_audit.py --calibrate citations.csv --report report.json   # tune vs real citations
"""
import argparse, json, os, re, subprocess, sys, csv, html as H, datetime, time, threading, tempfile, base64
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict
import warnings
from bs4 import BeautifulSoup
try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception: pass

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
ASSET_RE = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|svg|avif|css|js|mjs|json|xml|pdf|zip|mp4|webm|ico|woff2?|ttf)(?:\?|$)", re.I)
QSTART = re.compile(r"^\s*(what|how|why|when|which|who|where|does|do|can|is|are|should|will|has|have)\b", re.I)
NUM_RE = re.compile(r"(?<![\w-])\d[\d,.]*\s?%?")
WORKERS = 6

# ------------------------------------------------------------------ check catalog
# pillar: Known (Discovery, do I know you?) / Findable (Retrieval, can I find your
# answer?) / Trusted (Citation, do I trust you enough to name you?). ch = CITED chapter.
CHECK_META = {
 # KNOWN - entity recognition (ch4 Entities & Trust)
 "schema":     {"label":"Article / BlogPosting schema","pillar":"Known","ch":"Ch4","phase":1,"effort":"Med",
                "ev":"Schema classifies the page as an entity the engine can attribute (Ch4)."},
 "parity":     {"label":"Schema readable without JavaScript","pillar":"Known","ch":"Ch4","phase":1,"effort":"Low",
                "ev":"Non-JS AI crawlers never run your JavaScript, so JS-injected schema is invisible to them (Ch4)."},
 "canonical":  {"label":"Canonical tag present","pillar":"Known","ch":"Ch4","phase":1,"effort":"Low",
                "ev":"A self-referencing canonical stops duplicate-entity confusion (Ch4)."},
 "internal":   {"label":"Internal links in content (>=3)","pillar":"Known","ch":"Ch4","phase":2,"effort":"Med",
                "ev":"Internal links build the topical cluster engines read as authority (Ch4)."},
 # FINDABLE - retrieval + structure + access (ch5 Structure, ch3 Retrieval, ch7 engines)
 "http":       {"label":"HTTP 200 OK","pillar":"Findable","ch":"Ch3","phase":1,"effort":"Low",
                "ev":"Engines drop non-200 URLs before retrieval (engine documentation, Ch3)."},
 "title":      {"label":"Title tag (15-65 chars)","pillar":"Findable","ch":"Ch5","phase":1,"effort":"Low",
                "ev":"The title frames the page for retrieval and rank (Ch5)."},
 "meta":       {"label":"Meta description (50-160)","pillar":"Findable","ch":"Ch5","phase":1,"effort":"Low",
                "ev":"AI and SERP snippets are drawn from the meta description (Ch5)."},
 "h1":         {"label":"Exactly one H1","pillar":"Findable","ch":"Ch5","phase":1,"effort":"Low",
                "ev":"One H1 states the page topic unambiguously (Ch5)."},
 "answerfirst":{"label":"Answer-first opener (40-60 words)","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"The opening 40-60 words are the chunk the machine lifts; AIO answers run ~67 words median (Pew 2026, Ch5)."},
 "qheadings":  {"label":"Question / claim-shaped H2-H3s","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"Question headings match the retrieval query (Google AIO guidance, Ch5)."},
 "sections":   {"label":"Self-contained sections (no walls of text)","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"Retrieval reranks 40-180 word passages; a wall of text hands the engine no clean chunk (Firecrawl 2026, Ch5). Not a word-count target: each section just needs its own liftable answer."},
 "liststables":{"label":"Tables / lists present","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"80% of AI-cited pages use lists or structured elements (Profound 2026, Ch5)."},
 "faq":        {"label":"FAQPage / HowTo schema","pillar":"Findable","ch":"Ch5","phase":1,"effort":"Med",
                "ev":"FAQ schema hands the engine pre-chunked question-answer pairs (Ch5)."},
 "alt":        {"label":"Image alt coverage (>=90%)","pillar":"Findable","ch":"Ch5","phase":1,"effort":"Low",
                "ev":"Alt text lets engines read and reuse your images (Ch5)."},
 "robots":     {"label":"robots.txt allows AI search bots","pillar":"Findable","ch":"Ch7","phase":1,"effort":"Low",
                "ev":"A Disallow on GPTBot / PerplexityBot / Google-Extended blocks citation outright (Ch7)."},
 "sitemap":    {"label":"XML sitemap present","pillar":"Findable","ch":"Ch7","phase":1,"effort":"Low",
                "ev":"The sitemap is how engines discover every page (Ch7)."},
 "reachability":{"label":"AI crawlers not network-blocked","pillar":"Findable","ch":"Ch7","phase":1,"effort":"Low",
                "ev":"A Cloudflare/WAF 403 on GPTBot silently blocks citation (live reachability test, Ch7)."},
 # TRUSTED - authority + citability (ch4 trust, Princeton GEO)
 "wordcount":  {"label":"Substantive content (>=300 words)","pillar":"Trusted","ch":"Ch4","phase":3,"effort":"High",
                "ev":"Thin pages rarely earn citations; depth signals a real answer (Ch4)."},
 "statdensity":{"label":"Statistic density (>=1.5 / 100 words)","pillar":"Trusted","ch":"Ch4","phase":3,"effort":"High",
                "ev":"Statistics lift citation likelihood +32% (Princeton GEO 2024, Ch4)."},
 "citations":  {"label":"External source links (>=2)","pillar":"Trusted","ch":"Ch4","phase":3,"effort":"Med",
                "ev":"Inline citations lift citation likelihood +30% (Princeton GEO 2024, Ch4)."},
 "freshness":  {"label":"Fresh (updated < 12 months)","pillar":"Trusted","ch":"Ch4","phase":1,"effort":"Low",
                "ev":"Engines weight recency; undated content loses to dated (Ch4)."},
 # INFORMATIONAL - not scored
 "llms":       {"label":"llms.txt (informational, not scored)","pillar":"Info","ch":"Ch5","phase":0,"effort":"Low",
                "ev":"SE Ranking found NO correlation between llms.txt and citations; their model got more accurate without it (Ch5). Shown for reference; harmless, not required."},
}
PILLARS = ["Known","Findable","Trusted"]
FIX = {
 "http":"Return 200 or 301 the URL to a live page.",
 "title":"Write a 15-65 char title leading with the topic, not the brand.",
 "meta":"Write a 50-160 char meta description that opens with the answer.",
 "h1":"Use exactly one H1 that states the page topic.",
 "wordcount":"Add substantive, unique depth so the page has a real answer to lift.",
 "answerfirst":"Put a direct, self-contained 40-60 word answer in the first two sentences.",
 "qheadings":"Rephrase H2/H3s as the questions or claims users actually search.",
 "sections":"Break walls of text so each section carries its own liftable answer (no artificial chopping - just one idea per block).",
 "liststables":"Add an HTML comparison table or numbered list for the key facts.",
 "statdensity":"Add sourced numbers, roughly one verifiable fact per 80 words.",
 "citations":"Cite two or more external authorities with inline links.",
 "schema":"Add server-rendered Article/BlogPosting JSON-LD.",
 "faq":"Add 6-10 FAQPage Q&A pairs, each a self-contained 40-60 word answer.",
 "parity":"Server-render the JSON-LD so non-JS AI crawlers can read it (Page Settings head, not a JS embed).",
 "freshness":"Add a visible last-updated date and refresh the content.",
 "alt":"Add descriptive alt text to every meaningful image.",
 "internal":"Link to 3+ related pages from the body to build the cluster.",
 "canonical":"Add a self-referencing canonical tag.",
 "robots":"Allow GPTBot, PerplexityBot, ClaudeBot, Google-Extended, Bingbot, OAI-SearchBot in robots.txt.",
 "sitemap":"Publish and submit an XML sitemap.",
 "reachability":"Unblock the AI search bots at the WAF / Cloudflare layer.",
}
# per-engine weights (which signals each engine actually weights). site ids allowed.
# Render-parity CORRECTED 2026-07-19 after deep research (book/research/schema-render-parity-...):
# GPTBot / PerplexityBot / ClaudeBot do NOT execute JavaScript (Vercel/MERJ, 500M+ fetches), so
# JS-injected schema is invisible to ChatGPT / Perplexity / Claude - parity is a hard visibility
# gate there (weight 3). Bing/Copilot renders but unreliably (weight 2). Google renders JS, so
# Gemini + AI Overviews DO eventually see JS-injected schema, so parity is removed for them (it is
# a reliability/speed issue there, not visibility). schema itself still weighted for Gemini.
ENGINE_WEIGHTS = {
 "ChatGPT":     {"wordcount":3,"statdensity":3,"citations":3,"parity":3,"answerfirst":2,"sections":2,"schema":2,"freshness":1,"qheadings":1},
 "Perplexity":  {"freshness":3,"citations":3,"parity":3,"statdensity":2,"answerfirst":2,"liststables":2,"sections":2,"qheadings":1,"reachability":2},
 "AI Overviews":{"answerfirst":3,"qheadings":3,"sections":2,"schema":2,"faq":2,"liststables":2,"freshness":1,"canonical":1,"sitemap":1,"title":1,"meta":1},
 "Gemini":      {"schema":3,"canonical":1,"sitemap":1,"statdensity":2,"answerfirst":2,"faq":1,"citations":1},
 "Copilot":     {"schema":3,"sitemap":2,"reachability":2,"liststables":2,"statdensity":2,"answerfirst":2,"parity":2,"freshness":1,"faq":2},
 "Claude":      {"sections":3,"parity":3,"answerfirst":2,"statdensity":2,"qheadings":2,"liststables":2,"schema":1,"citations":1},
}
ENGINE_NOTE = {
 "ChatGPT":"Favours comprehensive, authoritative, source-cited content + strong entity grounding. Cites few sources per answer, so be THE definitive page.",
 "Perplexity":"Live-searches every query. Rewards freshness, extractable facts and external citations. Cites many sources, so breadth helps.",
 "AI Overviews":"Rank-coupled + query fan-out. Answer-first, question headings, schema and self-contained sections win.",
 "Gemini":"Google index + Knowledge Graph. Entity/schema clarity, sameAs and canonical carry visibility across.",
 "Copilot":"Bing-grounded and highly citation-friendly. Schema, sitemap/IndexNow, listicles and extractable facts win here.",
 "Claude":"Synthesises rather than quotes. Rewards clean logical chunking, factual density and clear structure.",
}
SITE_IDS = {"robots","llms","sitemap","reachability"}
STAT = {"good":1.0,"warn":0.35,"bad":0.0}   # tightened: a warning is worth less than half

# ------------------------------------------------------------------ crawl + render
def find_chrome():
    for c in [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]:
        if os.path.exists(c): return c
    return None
CHROME = find_chrome()
_tls = threading.local()
_PROFROOT = os.path.join(tempfile.gettempdir(), "cited-score-chrome")  # writable when frozen
def _profile():
    if not getattr(_tls, "prof", None):
        _tls.prof = os.path.join(_PROFROOT, "p%d" % (threading.get_ident() % 100000))
        os.makedirs(_tls.prof, exist_ok=True)
    return _tls.prof

def fetch_raw(url, ua=UA, timeout=25):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(); enc = r.headers.get_content_charset() or "utf-8"
            return r.status, dict(r.headers), data.decode(enc, "ignore"), int((time.time()-t0)*1000)
    except urllib.error.HTTPError as e:
        return e.code, {}, "", int((time.time()-t0)*1000)
    except Exception:
        return None, {}, "", int((time.time()-t0)*1000)

def render(url, timeout=35):
    if not CHROME: return None, 0
    t0 = time.time()
    try:
        out = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--disable-extensions", "--user-data-dir=" + _profile(), "--dump-dom", url],
            capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="ignore")
        dom = out.stdout
        return (dom if dom and len(dom) > 500 else None), int((time.time()-t0)*1000)
    except Exception:
        return None, int((time.time()-t0)*1000)

def jsonld_types(soup):
    types = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try: data = json.loads(tag.string or tag.get_text() or "{}")
        except Exception: continue
        st = [data]
        while st:
            n = st.pop()
            if isinstance(n, dict):
                t = n.get("@type")
                if isinstance(t, list): types.extend(t)
                elif t: types.append(t)
                st.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list): st.extend(n)
    return types

def jsonld_objs(soup):
    o = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try: o.append(json.loads(tag.string or tag.get_text() or "{}"))
        except Exception: pass
    return o

def main_content(soup):
    root = soup.find("main") or soup.find("article") or soup.body or soup
    for sel in ["script","style","nav","header","footer","aside","form","noscript","svg"]:
        for t in root.find_all(sel): t.decompose()
    return root

def words(t): return re.findall(r"[A-Za-z0-9À-ɏ']+", t or "")

def section_counts(root):
    out = []
    for h in root.find_all(["h2","h3"]):
        seg = []
        for sib in h.next_siblings:
            if getattr(sib,"name",None) in ("h2","h3"): break
            txt = sib.get_text(" ",strip=True) if hasattr(sib,"get_text") else str(sib).strip()
            if txt: seg.append(txt)
        out.append(len(words(" ".join(seg))))
    return out

def find_date(soup, objs):
    ds = []
    def dig(n,key):
        if isinstance(n,dict):
            if isinstance(n.get(key),str): ds.append(n[key])
            for v in n.values(): dig(v,key)
        elif isinstance(n,list):
            for v in n: dig(v,key)
    for o in objs:
        dig(o,"dateModified"); dig(o,"datePublished")
    for t in soup.find_all("time"):
        d = t.get("datetime") or t.get_text(strip=True)
        if d: ds.append(d)
    p = []
    for d in ds:
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", d)
        if m:
            try: p.append(datetime.date(int(m[1]),int(m[2]),int(m[3])))
            except Exception: pass
    return max(p) if p else None

def classify(path, types):
    if path in ("", "/"): return "home"
    segs = [s for s in path.strip("/").split("/") if s]
    if path.rstrip("/") in ("/blog","/case-studies","/blogs","/resources","/guides","/tools"): return "listing"
    if segs and segs[0] == "blog": return "article"
    if {"Article","BlogPosting","NewsArticle","TechArticle"} & set(types): return "article"
    if segs and segs[0] in ("services","service"): return "service"
    if len(segs) == 1: return "page"
    return "page"

# checks that genuinely do not apply to a page type -> excluded from scoring.
# Deliberately NARROW (the score must not be kind): a homepage still owes citations,
# statistics and depth, so those stay in.
NA_BY_TYPE = {
    "home":     {"answerfirst","qheadings","sections","faq","freshness","schema"},
    "listing":  {"answerfirst","qheadings","sections","faq","freshness","schema","statdensity","citations","wordcount"},
}

def chk(cid, status, detail):
    m = CHECK_META[cid]
    return {"id":cid,"label":m["label"],"status":status,"detail":detail,
            "pillar":m["pillar"],"ch":m["ch"],"ev":m["ev"]}

def analyze(url, status, raw, rendered, domain):
    path = urllib.parse.urlparse(url).path or "/"
    soup = BeautifulSoup(rendered or raw or "", "lxml")
    rawsoup = BeautifulSoup(raw or "", "lxml")
    root = main_content(BeautifulSoup(rendered or raw or "", "lxml"))
    types_r = jsonld_types(soup); types_raw = jsonld_types(rawsoup)
    ptype = classify(path, types_r)
    C = []
    C.append(chk("http","good" if status==200 else "bad",f"status {status}"))
    title = soup.title.get_text(strip=True) if soup.title else ""
    C.append(chk("title","good" if 15<=len(title)<=65 else ("warn" if title else "bad"),f"{len(title)} chars"))
    md = soup.find("meta",attrs={"name":"description"}); mdc=(md.get("content") if md else "") or ""
    C.append(chk("meta","good" if 50<=len(mdc)<=160 else ("warn" if mdc else "bad"),f"{len(mdc)} chars"))
    h1s = soup.find_all("h1")
    C.append(chk("h1","good" if len(h1s)==1 else "bad",f"{len(h1s)} H1s"))
    body = root.get_text(" ",strip=True); wc=len(words(body))
    C.append(chk("wordcount","good" if wc>=300 else "warn",f"{wc} words"))
    fp=None
    for p in root.find_all("p"):
        n=len(words(p.get_text(" ",strip=True)))
        if n>=12: fp=n; break
    C.append(chk("answerfirst","good" if fp and 40<=fp<=60 else ("warn" if fp and 30<=fp<=90 else "bad"),f"opening para {fp or 0} words"))
    heads=root.find_all(["h2","h3"]); nh=len(heads)
    qh=sum(1 for h in heads if h.get_text(strip=True).endswith("?") or QSTART.match(h.get_text(strip=True)))
    qpct=round(100*qh/nh) if nh else 0
    C.append(chk("qheadings","good" if qpct>=30 else ("warn" if qpct>=10 else "bad"),f"{qh}/{nh} ({qpct}%)"))
    # self-contained sections: flag WALLS of text (un-liftable chunks), not short blocks
    secs=section_counts(root); walls=sum(1 for s in secs if s>220)
    if not secs: sec_status="warn"; sec_detail="no H2/H3 sections"
    elif walls==0: sec_status="good"; sec_detail=f"{len(secs)} sections, none over 220w"
    elif walls<=max(1,len(secs)//5): sec_status="warn"; sec_detail=f"{walls} wall(s) of text over 220w"
    else: sec_status="bad"; sec_detail=f"{walls}/{len(secs)} sections are walls of text"
    C.append(chk("sections",sec_status,sec_detail))
    ntab=len(root.find_all("table")); nlist=len(root.find_all(["ol","ul"]))
    C.append(chk("liststables","good" if ntab+nlist>=1 else "warn",f"{ntab} tables, {nlist} lists"))
    nums=len(NUM_RE.findall(body)); dens=round(100*nums/wc,2) if wc else 0
    C.append(chk("statdensity","good" if dens>=1.5 else ("warn" if dens>=0.6 else "bad"),f"{nums} numbers, {dens}/100w"))
    ext=0
    for a in root.find_all("a",href=True):
        if a["href"].startswith("http") and domain not in urllib.parse.urlparse(a["href"]).netloc.replace("www.",""): ext+=1
    C.append(chk("citations","good" if ext>=2 else ("warn" if ext==1 else "bad"),f"{ext} external links"))
    tset=set(types_r)
    has_art = bool(tset & {"Article","BlogPosting","NewsArticle","TechArticle"})
    C.append(chk("schema","good" if has_art else "bad",", ".join(sorted(tset)) or "none"))
    C.append(chk("faq","good" if tset&{"FAQPage","HowTo","QAPage"} else "warn",", ".join(sorted(tset&{"FAQPage","HowTo","QAPage"})) or "none"))
    inj=sorted(set(types_r)-set(types_raw))
    C.append(chk("parity","bad" if inj else "good",("JS-injected only: "+", ".join(inj)) if inj else "in raw HTML"))
    d=find_date(soup,jsonld_objs(soup))
    if d:
        age=(datetime.date.today()-d).days
        C.append(chk("freshness","good" if age<=365 else ("warn" if age<=730 else "bad"),f"{d.isoformat()} ({age}d)"))
    else:
        C.append(chk("freshness","bad","no date found"))
    imgs=soup.find_all("img"); alts=sum(1 for i in imgs if (i.get("alt") or "").strip()); apct=round(100*alts/len(imgs)) if imgs else 100
    C.append(chk("alt","good" if apct>=90 else "warn",f"{alts}/{len(imgs)} ({apct}%)"))
    il=sum(1 for a in root.find_all("a",href=True) if a["href"].startswith("/") or domain in a["href"])
    C.append(chk("internal","good" if il>=3 else "warn",f"{il} internal links"))
    can=soup.find("link",attrs={"rel":"canonical"})
    C.append(chk("canonical","good" if can and can.get("href") else "warn",can.get("href") if can else "none"))
    na=NA_BY_TYPE.get(ptype,set())
    for c in C:
        if c["id"] in na: c["status"]="na"
    metrics={"words":wc,"headings":nh,"question_pct":qpct,"walls":walls,"stat_density":dens,
             "ext_links":ext,"internal_links":il,"schema_types":sorted(tset),"images":len(imgs),"alt_pct":apct}
    return {"path":path,"type":ptype,"checks":C,"metrics":metrics,"rendered":rendered is not None}

def site_checks(origin, domain):
    out=[]
    st,_,robots,_=fetch_raw(origin+"/robots.txt")
    searchbots={"GPTBot","PerplexityBot","ClaudeBot","Google-Extended","Bingbot","OAI-SearchBot"}
    blocked=[]
    if robots:
        for b in re.split(r"(?i)user-agent:", robots):
            if re.search(r"(?im)^\s*disallow:\s*/\s*$", b):
                for bot in searchbots:
                    if bot.lower() in b.lower().split("\n")[0]: blocked.append(bot)
    out.append(chk("robots","bad" if blocked else "good",("robots.txt "+("found" if robots else "missing"))+(("; BLOCKS "+", ".join(sorted(set(blocked)))) if blocked else "; none blocked")))
    st3,_,sm,_=fetch_raw(origin+"/sitemap.xml")
    out.append(chk("sitemap","good" if st3==200 and "<url" in sm.lower() else "warn","found" if st3==200 else "missing"))
    reach=[]
    for ua in ["GPTBot/1.0","PerplexityBot/1.0"]:
        s,_,_,_=fetch_raw(origin+"/",ua=ua); reach.append((ua.split("/")[0],s))
    bl=[b for b,s in reach if s in (401,403,429) or s is None]
    out.append(chk("reachability","bad" if bl else "good","; ".join(f"{b}:{s}" for b,s in reach)))
    st2,_,llms,_=fetch_raw(origin+"/llms.txt")
    if st2==200 and llms:
        out.append(chk("llms","info","present"+("" if "](" in llms else " (no markdown links)")))
    else:
        out.append(chk("llms","info","not found"))
    return out

def all_urls(origin, domain, cap):
    urls=[]
    st,_,sm,_=fetch_raw(origin+"/sitemap.xml")
    if st==200 and sm:
        urls=re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sm)
    s,_,raw,_=fetch_raw(origin+"/")
    if raw:
        for a in BeautifulSoup(raw,"lxml").find_all("a",href=True):
            u=urllib.parse.urljoin(origin,a["href"].strip()); u,_=urllib.parse.urldefrag(u)
            p=urllib.parse.urlparse(u)
            if p.scheme in ("http","https") and domain in p.netloc.replace("www.","") and not ASSET_RE.search(p.path):
                urls.append(u)
    seen=[]; done=set()
    for u in [origin+"/"]+urls:
        k=u.rstrip("/")
        if k not in done: done.add(k); seen.append(u)
    if cap and cap>0: seen=seen[:cap]
    return seen

def process(url, domain):
    st,hdrs,raw,fms=fetch_raw(url)
    rend,rms=(render(url) if st==200 else (None,0))
    page=analyze(url,st,raw,rend,domain)
    page.update({"url":url,"status":st,"fetch_ms":fms,"render_ms":rms,
                 "depth":len([x for x in urllib.parse.urlparse(url).path.strip("/").split("/") if x]),
                 "server":hdrs.get("Server","")})
    return page

# ------------------------------------------------------------------ scoring
def _score(statuses, weights):
    tot=got=0.0
    for cid,w in weights.items():
        s=statuses.get(cid)
        if not s or s in ("na","info"): continue
        tot+=w; got+=w*STAT[s]
    return round(100*got/tot) if tot else None

def page_scores(statuses):
    """statuses: {id: good/warn/bad/na/info} incl site ids. -> overall, pillars, engines."""
    overall = _score(statuses, {c:1 for c in statuses if c not in SITE_IDS})
    pill={}
    for p in PILLARS:
        w={cid:1 for cid,m in CHECK_META.items() if m["pillar"]==p and cid in statuses}
        pill[p]=_score(statuses,w)
    eng={e:_score(statuses,w) for e,w in ENGINE_WEIGHTS.items()}
    return (overall if overall is not None else 0,
            {k:(v if v is not None else 0) for k,v in pill.items()},
            {k:(v if v is not None else 0) for k,v in eng.items()})

def build(domain, origin, pages, sitecx):
    scx={c["id"]:c["status"] for c in sitecx}
    for p in pages:
        st={c["id"]:c["status"] for c in p["checks"]}; st.update(scx)
        o,pl,en=page_scores(st)
        p["cs"]=st; p["score"]=o; p["pillars"]=pl; p["engines"]=en
    ok=[p for p in pages if p["status"]==200]
    def avg(f): return round(sum(f(p) for p in ok)/len(ok)) if ok else 0
    overall=avg(lambda p:p["score"])
    pill={pl:avg(lambda p:p["pillars"][pl]) for pl in PILLARS}
    eng={e:avg(lambda p:p["engines"][e]) for e in ENGINE_WEIGHTS}
    # ---- issues aggregated (+ pillar/chapter/evidence/effort) ----
    agg=defaultdict(lambda:{"warn":[],"bad":[]})
    for p in pages:
        for c in p["checks"]:
            if c["status"] in ("warn","bad"): agg[c["id"]]["bad" if c["status"]=="bad" else "warn"].append(p["url"])
    for c in sitecx:
        if c["status"] in ("warn","bad"): agg[c["id"]]["bad" if c["status"]=="bad" else "warn"].append(origin)
    issues=[]
    for cid,d in agg.items():
        m=CHECK_META[cid]; sev="bad" if d["bad"] else "warn"
        issues.append({"id":cid,"label":m["label"],"pillar":m["pillar"],"ch":m["ch"],"ev":m["ev"],
                       "effort":m["effort"],"phase":m["phase"],"fix":FIX.get(cid,""),
                       "severity":sev,"bad":d["bad"],"warn":d["warn"],"count":len(d["bad"])+len(d["warn"])})
    # ---- ACTION PLAN: simulate fixing each issue, measure projected gain ----
    base=overall; base_eng=eng
    for it in issues:
        cid=it["id"]; affected=set(it["bad"])|set(it["warn"])
        sim=[]
        site_fix = cid in SITE_IDS
        for p in ok:
            st=dict(p["cs"])
            if site_fix: st[cid]="good"
            elif p["url"] in affected: st[cid]="good"
            o,_,en2=page_scores(st); sim.append((o,en2))
        it["gain_overall"]=round(sum(s[0] for s in sim)/len(sim))-base if sim else 0
        eg={}
        for e in ENGINE_WEIGHTS:
            eg[e]=round(sum(s[1][e] for s in sim)/len(sim))-base_eng[e] if sim else 0
        it["gain_engines"]=eg
        te=max(eg.items(),key=lambda x:x[1]) if eg else ("",0)
        it["top_engine"]=te[0]; it["top_engine_gain"]=te[1]
    # rank: projected overall gain, then severity, then pages
    issues.sort(key=lambda x:(-x["gain_overall"],x["severity"]!="bad",-x["count"]))
    plan_phases={1:[],2:[],3:[]}
    for it in issues:
        if it["gain_overall"]<=0 and it["severity"]!="bad": continue
        plan_phases[it["phase"] if it["phase"] in (1,2,3) else 3].append(it["id"])
    tot=Counter()
    for p in pages:
        for c in p["checks"]:
            if c["status"] not in ("na","info"): tot[c["status"]]+=1
    return {"tool":"CITED Score","domain":domain,"origin":origin,
            "generated":datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "date":datetime.date.today().isoformat(),
            "pages_crawled":len(pages),"overall":overall,"pillars":pill,"engines":eng,
            "engine_note":ENGINE_NOTE,"engine_weights":ENGINE_WEIGHTS,"check_meta":CHECK_META,
            "totals":dict(tot),"issues":issues,"site_checks":sitecx,
            "types":dict(Counter(p["type"] for p in pages)),
            "plan_phases":plan_phases,"pages":pages}

# ------------------------------------------------------------------ re-crawl diff
def apply_diff(data, outbase):
    hist=outbase+"-history.jsonl"; prev=None
    if os.path.exists(hist):
        try:
            with open(hist,encoding="utf-8") as f:
                lines=[l for l in f if l.strip()]
            if lines: prev=json.loads(lines[-1])
        except Exception: prev=None
    if prev:
        pm={u:s for u,s in prev.get("pages",{}).items()}
        moved=[]
        for p in data["pages"]:
            if p["url"] in pm and pm[p["url"]]!=p["score"]:
                moved.append({"url":p["url"],"was":pm[p["url"]],"now":p["score"],"d":p["score"]-pm[p["url"]]})
        moved.sort(key=lambda x:abs(x["d"]),reverse=True)
        data["diff"]={"since":prev.get("date"),"overall_was":prev.get("overall"),
                      "overall_d":data["overall"]-prev.get("overall",data["overall"]),
                      "pillars_was":prev.get("pillars",{}),"engines_was":prev.get("engines",{}),
                      "improved":[m for m in moved if m["d"]>0][:8],
                      "declined":[m for m in moved if m["d"]<0][:8]}
    snap={"date":data["date"],"generated":data["generated"],"overall":data["overall"],
          "pillars":data["pillars"],"engines":data["engines"],
          "pages":{p["url"]:p["score"] for p in data["pages"]}}
    with open(hist,"a",encoding="utf-8") as f: f.write(json.dumps(snap)+"\n")

# ------------------------------------------------------------------ calibration
def spearman(xs, ys):
    def rank(v):
        order=sorted(range(len(v)),key=lambda i:v[i]); r=[0]*len(v)
        i=0
        while i<len(order):
            j=i
            while j+1<len(order) and v[order[j+1]]==v[order[i]]: j+=1
            avg=(i+j)/2+1
            for k in range(i,j+1): r[order[k]]=avg
            i=j+1
        return r
    rx,ry=rank(xs),rank(ys); n=len(xs)
    mx=sum(rx)/n; my=sum(ry)/n
    num=sum((a-mx)*(b-my) for a,b in zip(rx,ry))
    den=(sum((a-mx)**2 for a in rx)*sum((b-my)**2 for b in ry))**0.5
    return num/den if den else 0.0

def calibrate(report_json, citations_csv):
    d=json.load(open(report_json,encoding="utf-8"))
    cites={}
    with open(citations_csv,encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row)<2: continue
            u=row[0].strip()
            try: cites[u.rstrip("/")]=float(row[1])
            except ValueError: continue
    rows=[p for p in d["pages"] if p["url"].rstrip("/") in cites]
    if len(rows)<8:
        print(f"Only {len(rows)} pages matched citations.csv (need >=8 for a stable correlation).")
        print("CSV format: url,citations  (export per-URL citation counts from Bing WMT AI Performance).")
        return
    y=[cites[p["url"].rstrip("/")] for p in rows]
    print(f"\n=== CITED Score calibration vs {len(rows)} pages with real citations ===")
    print(f"Overall score  <-> citations : rho {spearman([p['score'] for p in rows],y):+.2f}")
    for pl in PILLARS:
        print(f"{pl:13} <-> citations : rho {spearman([p['pillars'][pl] for p in rows],y):+.2f}")
    for e in ENGINE_WEIGHTS:
        print(f"{e:13} <-> citations : rho {spearman([p['engines'][e] for p in rows],y):+.2f}")
    print("\nPer-check predictive power (which signals separate cited from uncited pages here):")
    print("  Note: a check every page passes has no variance, so rho ~0 means 'table stakes,")
    print("  not measurable on this site' - NOT 'unimportant'. Only act on checks that VARY.")
    res=[]
    for cid in CHECK_META:
        xs=[STAT.get(p["cs"].get(cid),None) for p in rows]
        vals=[x for x in xs if x is not None]
        pairs=[(x,yy) for x,yy in zip(xs,y) if x is not None]
        if len(pairs)<8: continue
        modal=max(Counter(vals).values())/len(vals) if vals else 1.0   # share at most-common status
        res.append((cid,spearman([a for a,_ in pairs],[b for _,b in pairs]),len(pairs),modal))
    res.sort(key=lambda x:x[1],reverse=True)
    for cid,rho,n,modal in res:
        if modal>=0.95: flag=f" <- table stakes ({round(modal*100)}% pass, no variance here)"
        elif rho>=0.2: flag=" <- VARIES + predicts: up-weight"
        elif rho<=0.0: flag=" <- varies but does not predict (or inverts): investigate, do not blindly drop"
        else: flag=""
        print(f"  {cid:13} rho {rho:+.2f}  (n={n}, {round(modal*100)}% at modal){flag}")
    print("\nRead the SIGNAL, not the noise: retune ENGINE_WEIGHTS toward the 'VARIES + predicts' checks.")
    print("Caveat: heavy-tailed sample (2 pages = ~75% of citations), single site, one engine (Copilot).")
    print("Treat as directional; recalibrate as more citation data and other sites are added.")

# ------------------------------------------------------------------ outputs
def write_outputs(data, outbase):
    with open(outbase+".json","w",encoding="utf-8") as f: json.dump(data,f,indent=2,ensure_ascii=False)
    with open(outbase+".csv","w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f); w.writerow(["url","type","depth","status","score"]+PILLARS+list(ENGINE_WEIGHTS)+["fetch_ms","render_ms","words","issues"])
        for p in data["pages"]:
            bad=[c["label"] for c in p["checks"] if c["status"]=="bad"]
            w.writerow([p["url"],p["type"],p["depth"],p["status"],p["score"]]+[p["pillars"][pl] for pl in PILLARS]+
                       [p["engines"][e] for e in ENGINE_WEIGHTS]+[p["fetch_ms"],p["render_ms"],p["metrics"]["words"],"; ".join(bad)])
    write_html(data, outbase+".html")

try:
    from favicon_data import FAVICON_PNG_B64          # 64px Anton mark, generated by make_icon.py
    FAVICON = "data:image/png;base64," + FAVICON_PNG_B64
except Exception:                                     # fallback: plain green square
    FAVICON = "data:image/svg+xml;base64," + base64.b64encode(
        b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'><rect x='4' y='4' width='248' height='248' rx='48' fill='#42D949'/></svg>").decode()

def write_html(d, path):
    payload=json.dumps(d,ensure_ascii=False).replace("</","<\\/")
    css=r"""
:root{--bg:#0E110E;--panel:#171B16;--panel2:#14251A;--line:#26281F;--muted:#9A9284;--txt:#F0EBE0;
 --grn:#42D949;--grn2:#6BEA71;--deep:#15803D;--amber:#F5A623;--red:#F16A5F;--chip:#D63B2F}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 'Figtree',-apple-system,Segoe UI,Arial,sans-serif}
a{color:var(--grn);text-decoration:none}a:hover{text-decoration:underline}
header{padding:18px 26px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{font-family:'Anton',sans-serif;font-size:26px;font-weight:400;letter-spacing:-.3px;text-transform:uppercase}
.chip{background:var(--grn);color:#0a0a0a;font-family:'Figtree',sans-serif;font-weight:800;font-size:11px;padding:1px 5px;border-radius:4px;vertical-align:super;margin-left:4px}
header .m{color:var(--muted);font-size:13px}
.btns{margin-left:auto;display:flex;gap:8px}
button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer;font-size:12px}
button:hover{border-color:var(--grn);color:var(--grn2)}
.tabs{display:flex;flex-wrap:wrap;gap:2px;padding:0 18px;border-bottom:1px solid var(--line);background:#10130F;position:sticky;top:0;z-index:5}
.tab{padding:11px 13px;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;font-size:13px;white-space:nowrap}
.tab:hover{color:var(--txt)}.tab.on{color:#fff;border-bottom-color:var(--grn)}
.tab.sep{opacity:.4;pointer-events:none;padding:11px 4px}
.wrap{padding:22px 26px;max-width:1320px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.card .n{font-size:30px;font-weight:800}.card .l{color:var(--muted);font-size:12px;margin-top:2px}
.pillcards{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:20px}
.pill3{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.pill3 .q{color:var(--muted);font-size:12px;margin-top:2px}
.ring{--p:0;width:74px;height:74px;flex:0 0 74px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:18px;
 background:conic-gradient(var(--c) calc(var(--p)*1%),#243024 0)}.ring i{width:58px;height:58px;border-radius:50%;background:var(--panel);display:grid;place-items:center;font-style:normal}
.engs{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.eng{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;display:flex;gap:14px;align-items:center;cursor:pointer}
.eng .b{font-weight:700}.eng .d{color:var(--muted);font-size:12px;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:42px;background:#10130F}
tr:hover td{background:#1a2016}.sc{font-weight:800;border-radius:6px;padding:2px 8px;color:#08110a;display:inline-block;min-width:30px;text-align:center}
.badge{font-size:10px;padding:1px 6px;border-radius:20px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.badge.Known{border-color:#3aa0ff55;color:#7bbcff}.badge.Findable{border-color:#42d94955;color:var(--grn2)}.badge.Trusted{border-color:#f5a62355;color:var(--amber)}
.dot{font-weight:800}.dot.good{color:var(--grn)}.dot.warn{color:var(--amber)}.dot.bad{color:var(--red)}
.issue{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
.issue h4{margin:0 0 4px;font-size:15px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.issue .ev{color:var(--muted);font-size:12px;margin:6px 0}.issue .fix{color:#d9dcc9;margin-top:4px}
.issue .urls{margin-top:8px;font-size:12px;color:var(--muted);max-height:160px;overflow:auto;display:none}
.issue.open .urls{display:block}
.sev{font-size:11px;padding:2px 8px;border-radius:20px}.sev.bad{background:#3a1a18;color:var(--red)}.sev.warn{background:#3a2f12;color:var(--amber)}
.gain{font-size:11px;padding:2px 8px;border-radius:20px;background:#12331d;color:var(--grn2);font-weight:700}
.rank{background:var(--grn);color:#08110a;font-weight:900;width:24px;height:24px;border-radius:50%;display:inline-grid;place-items:center;font-size:12px}
.phase{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:6px 14px 14px;margin-bottom:16px}
.phase h3{color:var(--grn2);margin:10px 0}
.muted{color:var(--muted)}.hide{display:none}h3{margin:18px 0 10px;font-size:15px}
.bar{height:8px;background:#243024;border-radius:6px;overflow:hidden;min-width:120px}.bar i{display:block;height:100%}
input.search{background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:7px 10px;font-size:13px;width:260px;margin-bottom:12px}
.foot{color:var(--muted);font-size:12px;padding:20px 26px;border-top:1px solid var(--line);max-width:1000px}
.diffline{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:13px}
#printroot{display:none}
@media print{
 body{background:#fff;color:#111}header,.tabs,.btns{background:#fff}.tab,button{display:none}
 #app{display:none}#printroot{display:block;padding:0 12px}
 .card,.issue,.pill3,.phase,.diffline{border:1px solid #ccc;background:#fff;break-inside:avoid}
 a{color:#111}.muted,.card .l{color:#555}th{background:#eee;color:#333;position:static}
 .sc{color:#fff}h2{break-before:page}
}
"""
    js=r"""
const D=window.__DATA__;
const col=s=>s>=75?'#42D949':s>=50?'#F5A623':'#F16A5F';
const dot=s=>`<span class="dot ${s}">●</span>`;
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const rel=u=>esc(u.replace(D.origin,'')||'/');
const ring=v=>`<div class="ring" style="--p:${v};--c:${col(v)}"><i>${v}</i></div>`;
const scb=v=>`<span class="sc" style="background:${col(v)}">${v}</span>`;
const bd=p=>`<span class="badge ${p}">${p}</span>`;
const ECOLS=['ChatGPT','Perplexity','AI Overviews','Gemini','Copilot','Claude'];
const TABS=['Overview','Action Plan','Issues','Pages','|','ChatGPT','Perplexity','AI Overviews','Gemini','Copilot','Claude','|','Site structure','Response times'];
let cur='Overview',sortk='score',sortd=1,pageFilter='';
function tabsbar(){document.getElementById('tabs').innerHTML=TABS.map(t=>t=='|'?`<div class="tab sep">|</div>`:`<div class="tab ${t==cur?'on':''}" onclick="go('${t}')">${t}</div>`).join('')}
function go(t){cur=t;pageFilter='';tabsbar();render()}
function render(){const w=document.getElementById('view');
 if(cur=='Overview')return w.innerHTML=ovw();
 if(cur=='Action Plan')return w.innerHTML=plan();
 if(cur=='Issues')return w.innerHTML=issuesView();
 if(cur=='Pages')return w.innerHTML=pagesView(null);
 if(cur=='Site structure')return w.innerHTML=structure();
 if(cur=='Response times')return w.innerHTML=speed();
 return w.innerHTML=engine(cur);}

function diffCard(){if(!D.diff)return '';const x=D.diff;const s=(x.overall_d>0?'+':'')+x.overall_d;
 let mv='';if(x.improved.length)mv+=' Most improved: '+x.improved.slice(0,3).map(m=>rel(m.url)+' '+m.was+'→'+m.now).join(', ')+'.';
 if(x.declined.length)mv+=' Declined: '+x.declined.slice(0,3).map(m=>rel(m.url)+' '+m.was+'→'+m.now).join(', ')+'.';
 return `<div class="diffline"><b>Since ${esc(x.since)}:</b> overall ${x.overall_was} → ${D.overall} (${s}).${mv}</div>`}

function ovw(){const t=D.totals;const err=D.issues.filter(i=>i.severity=='bad').reduce((a,i)=>a+i.count,0);const wr=D.issues.filter(i=>i.severity=='warn').reduce((a,i)=>a+i.count,0);
 const Q={Known:'Do the engines know you exist?',Findable:'Can they find your answer?',Trusted:'Do they trust you enough to name you?'};
 return diffCard()+
 `<div class="grid">
  <div class="card"><div class="n" style="color:${col(D.overall)}">${D.overall}</div><div class="l">CITED Score</div></div>
  <div class="card"><div class="n">${D.pages_crawled}</div><div class="l">Pages crawled</div></div>
  <div class="card"><div class="n" style="color:var(--red)">${err}</div><div class="l">Errors</div></div>
  <div class="card"><div class="n" style="color:var(--amber)">${wr}</div><div class="l">Warnings</div></div>
  <div class="card"><div class="n" style="color:var(--grn)">${t.good||0}</div><div class="l">Checks passed</div></div>
 </div>
 <h3>The three questions <span class="muted">(Ch3)</span></h3>
 <div class="pillcards">${PILL(Q)}</div>
 <h3>Readiness by AI engine <span class="muted">(each weights different signals, Ch6-7)</span></h3>
 <div class="engs">${ECOLS.map(e=>`<div class="eng" onclick="go('${e}')">${ring(D.engines[e])}<div><div class="b">${e}</div><div class="d">${esc(D.engine_note[e]).slice(0,64)}...</div></div></div>`).join('')}</div>
 <h3>Do these first</h3>${D.issues.slice(0,3).map((i,x)=>planRow(i,x)).join('')}
 <p class="muted">Full ranked plan and 30/60/90-day roadmap in the <a onclick="go('Action Plan')">Action Plan</a> tab.</p>
 <h3>Pages by type</h3><div class="grid">${Object.entries(D.types).map(([k,v])=>`<div class="card"><div class="n">${v}</div><div class="l">${k}</div></div>`).join('')}</div>`}
function PILL(Q){return ['Known','Findable','Trusted'].map(p=>`<div class="pill3"><div style="display:flex;gap:14px;align-items:center">${ring(D.pillars[p])}<div><div class="b" style="font-weight:800">${p}</div><div class="q">${Q[p]}</div></div></div></div>`).join('')}

function planRow(i,x){const eg=Object.entries(i.gain_engines).filter(([e,v])=>v>0).sort((a,b)=>b[1]-a[1]).slice(0,3);
 return `<div class="issue" onclick="this.classList.toggle('open')" style="cursor:pointer"><h4><span class="rank">${x+1}</span> ${esc(i.label)} ${bd(i.pillar)} <span class="badge">${i.ch}</span> <span class="badge">${i.effort} effort</span>
   ${i.gain_overall>0?`<span class="gain">+${i.gain_overall} overall</span>`:''}${eg.map(([e,v])=>`<span class="gain">+${v} ${e}</span>`).join('')}</h4>
   <div class="ev">${esc(i.ev)}</div>
   <div class="fix"><b>Fix (${i.count} page${i.count>1?'s':''}):</b> ${esc(i.fix)} <span class="muted">- click to list pages</span></div>
   <div class="urls">${[...i.bad.map(u=>'● '+rel(u)),...i.warn.map(u=>'○ '+rel(u))].join('<br>')}</div></div>`}
function plan(){let h=`<p class="muted">Ranked by projected CITED Score gain if fixed across every affected page. Projection re-scores the site with each fix applied.</p>`;
 h+=D.issues.map((i,x)=>planRow(i,x)).join('');
 const names={1:'Phase 1 — 0 to 30 days: foundation & quick technical wins',2:'Phase 2 — 30 to 60 days: structure for retrieval',3:'Phase 3 — 60 to 90 days: authority & depth'};
 h+='<h3>90-day roadmap</h3>';
 [1,2,3].forEach(ph=>{const ids=D.plan_phases[ph]||[];if(!ids.length)return;
  h+=`<div class="phase"><h3>${names[ph]}</h3>`+ids.map(id=>{const i=D.issues.find(y=>y.id==id);return `<div style="padding:4px 0">${bd(i.pillar)} <b>${esc(i.label)}</b> <span class="muted">- ${esc(i.fix)}</span> ${i.gain_overall>0?`<span class="gain">+${i.gain_overall}</span>`:''}</div>`}).join('')+`</div>`});
 return h}
function issuesView(){let h='';['Known','Findable','Trusted'].forEach(p=>{const gs=D.issues.filter(i=>i.pillar==p);if(!gs.length)return;
  h+=`<h3>${bd(p)} ${p==='Known'?'Do they know you?':p==='Findable'?'Can they find your answer?':'Do they trust you?'}</h3>`;
  h+=gs.map(i=>`<div class="issue" onclick="this.classList.toggle('open')"><h4>${dot(i.severity)} ${esc(i.label)} <span class="badge">${i.ch}</span> <span class="sev ${i.severity}">${i.count} affected</span> ${i.gain_overall>0?`<span class="gain">+${i.gain_overall} if fixed</span>`:''}</h4>
    <div class="ev"><b>Why:</b> ${esc(i.ev)}</div><div class="fix"><b>Fix:</b> ${esc(i.fix)}</div>
    <div class="urls">${[...i.bad.map(u=>'● '+rel(u)),...i.warn.map(u=>'○ '+rel(u))].join('<br>')}</div></div>`).join('')});
 return h||'<p class="muted">No issues.</p>'}
function sc(k){if(sortk==k)sortd*=-1;else{sortk=k;sortd=1}render()}
function pageFilterFn(v){pageFilter=v.toLowerCase();document.getElementById('ptbody').innerHTML=ptRows(window._peng)}
function ptRows(eng){let ps=[...D.pages];if(pageFilter)ps=ps.filter(p=>p.url.toLowerCase().includes(pageFilter));
 ps.sort((a,b)=>{let x=eng?a.engines[eng]:a[sortk],y=eng?b.engines[eng]:b[sortk];if(typeof x=='string')return(x>y?1:-1)*sortd;return(x-y)*sortd});
 return ps.map(p=>`<tr><td>${scb(p.score)}</td><td><a href="${esc(p.url)}" target="_blank">${rel(p.url)}</a></td><td><span class="badge">${p.type}</span></td>
   <td>${p.pillars.Known}</td><td>${p.pillars.Findable}</td><td>${p.pillars.Trusted}</td>
   ${ECOLS.map(e=>`<td style="color:${col(p.engines[e])}">${p.engines[e]}</td>`).join('')}
   <td class="muted">${p.fetch_ms+p.render_ms}</td>
   <td class="muted">${p.checks.filter(c=>c.status=='bad').map(c=>c.label).join(', ')||'-'}</td></tr>`).join('')}
function pagesView(eng){window._peng=eng;
 return `<div style="display:flex;gap:10px;align-items:center"><input class="search" placeholder="Filter by URL..." oninput="pageFilterFn(this.value)"><button onclick="exportPages()">Export CSV</button></div>
 <table><thead><tr><th onclick="sc('score')">Score</th><th onclick="sc('url')">URL</th><th onclick="sc('type')">Type</th>
  <th title="Do they know you?">Kn</th><th title="Can they find your answer?">Fi</th><th title="Do they trust you?">Tr</th>
  ${ECOLS.map(e=>`<th title="${esc(D.engine_note[e])}">${e.split(' ')[0]}</th>`).join('')}
  <th onclick="sc('fetch_ms')">ms</th><th>Errors</th></tr></thead><tbody id="ptbody">${ptRows(eng)}</tbody></table>`}
function engine(e){const ws=D.engine_weights[e];const ok=D.pages.filter(p=>p.status==200);const siteIds=new Set(D.site_checks.map(c=>c.id));
 const meta=Object.keys(ws).map(id=>{const rel2=ok.filter(p=>{const s=p.cs[id];return s&&s!='na'&&s!='info'});const good=rel2.filter(p=>p.cs[id]=='good');const pr=rel2.length?Math.round(100*good.length/rel2.length):0;const lab=(D.check_meta[id]||{}).label||id;return {id,lab,w:ws[id],pr}}).sort((a,b)=>b.w-a.w);
 const worst=[...ok].sort((a,b)=>a.engines[e]-b.engines[e]).slice(0,15);
 return `<div class="grid"><div class="card">${ring(D.engines[e])}<div class="l" style="margin-top:8px">${e} readiness</div></div>
   <div class="card" style="grid-column:span 3"><div class="l">${esc(D.engine_note[e])}</div></div></div>
  <h3>What ${e} weights most</h3><table><thead><tr><th>Signal</th><th>Weight</th><th>Site pass rate</th></tr></thead><tbody>
  ${meta.map(m=>{let det;if(siteIds.has(m.id)){const s=D.site_checks.find(c=>c.id==m.id)||{};det=`Site-wide check: <b>${s.status||''}</b> - ${esc(s.detail||'')}`;}else{const f=ok.filter(p=>p.cs[m.id]=='bad'||p.cs[m.id]=='warn');det=f.length?f.map(p=>(p.cs[m.id]=='bad'?'● ':'○ ')+`<a href="${esc(p.url)}" target="_blank">${rel(p.url)}</a>`).join('<br>'):'<span class="muted">No pages fail this check.</span>';}
   return `<tr onclick="var r=this.nextElementSibling;r.style.display=r.style.display=='table-row'?'none':'table-row'" style="cursor:pointer"><td>&#9656; ${esc(m.lab)}</td><td>${'★'.repeat(m.w)}</td><td><div class="bar"><i style="width:${m.pr}%;background:${col(m.pr)}"></i></div><span class="muted">${m.pr}%</span></td></tr><tr style="display:none"><td colspan="3"><div style="max-height:220px;overflow:auto;font-size:12px;padding:6px 0">${det}</div></td></tr>`}).join('')}
  </tbody></table>
  <h3>Worst pages for ${e}</h3><table><thead><tr><th>${e}</th><th>URL</th><th>Overall</th></tr></thead><tbody>
  ${worst.map(p=>`<tr><td>${scb(p.engines[e])}</td><td><a href="${esc(p.url)}" target="_blank">${rel(p.url)}</a></td><td class="muted">${p.score}</td></tr>`).join('')}</tbody></table>`}
function structure(){const byDir={},byDepth={};D.pages.forEach(p=>{const seg=p.path.split('/').filter(Boolean)[0]||'(root)';(byDir[seg]=byDir[seg]||[]).push(p);(byDepth[p.depth]=byDepth[p.depth]||[]).push(p)});
 const row=(k,ps)=>`<tr><td>${esc(k)}</td><td>${ps.length}</td><td>${scb(Math.round(ps.reduce((a,p)=>a+p.score,0)/ps.length))}</td></tr>`;
 return `<h3>By top-level section</h3><table><thead><tr><th>Section</th><th>Pages</th><th>Avg score</th></tr></thead><tbody>${Object.entries(byDir).sort((a,b)=>b[1].length-a[1].length).map(([k,v])=>row('/'+k,v)).join('')}</tbody></table>
  <h3>By crawl depth</h3><table><thead><tr><th>Depth</th><th>Pages</th><th>Avg score</th></tr></thead><tbody>${Object.keys(byDepth).sort().map(k=>row('depth '+k,byDepth[k])).join('')}</tbody></table>`}
function speed(){const sc2=ms=>ms<=800?'#42D949':ms<=1800?'#F5A623':'#F16A5F';const sl=ms=>ms<=800?'Fast':ms<=1800?'OK':'Slow';const ps=[...D.pages].sort((a,b)=>b.fetch_ms-a.fetch_ms);const avgf=Math.round(ps.reduce((a,p)=>a+p.fetch_ms,0)/ps.length);const avgr=Math.round(ps.reduce((a,p)=>a+p.render_ms,0)/ps.length);const fast=ps.filter(p=>p.fetch_ms<=800).length,okc=ps.filter(p=>p.fetch_ms>800&&p.fetch_ms<=1800).length,slow=ps.filter(p=>p.fetch_ms>1800).length;
 return `<div class="grid"><div class="card"><div class="n" style="color:${sc2(avgf)}">${avgf}<span class="l">ms</span></div><div class="l">Avg server response - <b style="color:${sc2(avgf)}">${sl(avgf)}</b></div></div>
  <div class="card"><div class="n" style="color:var(--grn)">${fast}</div><div class="l">Fast &le;0.8s</div></div>
  <div class="card"><div class="n" style="color:var(--amber)">${okc}</div><div class="l">OK &le;1.8s</div></div>
  <div class="card"><div class="n" style="color:var(--red)">${slow}</div><div class="l">Slow &gt;1.8s</div></div>
  <div class="card"><div class="n">${avgr}<span class="l">ms</span></div><div class="l">Avg render (tool overhead)</div></div></div>
  <p class="muted">Server response graded on Google's TTFB thresholds: Fast &le; 0.8s, OK &le; 1.8s, Slow &gt; 1.8s. Render time is the tool's headless-Chrome overhead, not your site's speed.</p>
  <h3>Slowest pages (by server response)</h3><table><thead><tr><th>Server response</th><th>URL</th><th>Render ms</th></tr></thead><tbody>
  ${ps.slice(0,40).map(p=>`<tr><td><span class="sc" style="background:${sc2(p.fetch_ms)}">${p.fetch_ms} ms</span> <span class="muted">${sl(p.fetch_ms)}</span></td><td><a href="${esc(p.url)}" target="_blank">${rel(p.url)}</a></td><td class="muted">${p.render_ms}</td></tr>`).join('')}</tbody></table>`}
function dl(name,rows){const csv=rows.map(r=>r.map(c=>`"${String(c==null?'':c).replace(/"/g,'""')}"`).join(',')).join('\n');
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download=name;a.click()}
function exportPages(){const rows=[['url','type','score','Known','Findable','Trusted',...ECOLS,'ms','errors']];
 D.pages.forEach(p=>rows.push([p.url,p.type,p.score,p.pillars.Known,p.pillars.Findable,p.pillars.Trusted,...ECOLS.map(e=>p.engines[e]),p.fetch_ms+p.render_ms,p.checks.filter(c=>c.status=='bad').map(c=>c.label).join('; ')]));
 dl(`cited-score-${D.domain}-pages.csv`,rows)}
function printReport(){const Q={Known:'Do they know you?',Findable:'Can they find your answer?',Trusted:'Do they trust you?'};
 document.getElementById('printroot').innerHTML=`<h1>CITED Score — ${esc(D.domain)}</h1><p>${D.pages_crawled} pages · ${esc(D.generated)} · Overall ${D.overall}/100</p>`+
  `<h2>Action plan</h2>`+D.issues.map((i,x)=>`<p><b>${x+1}. ${esc(i.label)}</b> [${i.pillar}, ${i.ch}, ${i.effort}] ${i.gain_overall>0?'(+'+i.gain_overall+' overall)':''}<br>${esc(i.fix)} — ${i.count} pages</p>`).join('')+
  `<h2>All pages</h2><table><thead><tr><th>Score</th><th>URL</th><th>Kn</th><th>Fi</th><th>Tr</th></tr></thead><tbody>`+
  D.pages.map(p=>`<tr><td>${p.score}</td><td>${rel(p.url)}</td><td>${p.pillars.Known}</td><td>${p.pillars.Findable}</td><td>${p.pillars.Trusted}</td></tr>`).join('')+`</tbody></table>`;
 window.print()}
tabsbar();render();
"""
    doc=("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>CITED Score: {H.escape(d['domain'])}</title><link rel='icon' href=\"{FAVICON}\">"
         "<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
         "<link href='https://fonts.googleapis.com/css2?family=Anton&family=Figtree:wght@300..900&display=swap' rel='stylesheet'>"
         f"<style>{css}</style></head><body>"
         f"<header><span class='logo'>CITED<span class='chip'>Score</span></span>"
         f"<span class='m'><a href='{H.escape(d['origin'])}' target='_blank' style='color:var(--grn)'>{H.escape(d['domain'])}</a> &middot; {d['pages_crawled']} pages &middot; {d['generated']}</span>"
         "<span class='btns'><button onclick='printReport()'>Print / PDF</button><button onclick='exportPages()'>Export CSV</button></span></header>"
         "<div class='tabs' id='tabs'></div><div id='app'><div class='wrap' id='view'></div>"
         "<div class='foot'>The CITED Score <b>estimates citability</b> from on-page, structural and technical signals. "
         "It does <b>not</b> measure citations. For measured citations, calibrate the model against your Bing Webmaster Tools AI Performance export "
         "(<code>--calibrate citations.csv</code>). Every check carries a source (engine documentation, first-party citation data, or a CITED chapter). "
         "llms.txt is shown for reference only and is not scored (no citation correlation, Ch5).</div></div>"
         "<div id='printroot'></div>"
         f"<script>window.__DATA__={payload};</script><script>{js}</script></body></html>")
    with open(path,"w",encoding="utf-8") as f: f.write(doc)

def run_audit(url, out="report", max_pages=0, workers=WORKERS, progress=None):
    """Crawl + score a whole site and write out.html/.json/.csv. progress(phase, done,
    total, msg) is called through the run so a UI can show live status. Returns the data."""
    if not url.startswith("http"): url="https://"+url
    p=urllib.parse.urlparse(url); domain=p.netloc.replace("www.",""); origin=f"{p.scheme}://{p.netloc}"
    def emit(phase,done,total,msg):
        if progress: progress(phase,done,total,msg)
    emit("discover",0,0,f"Discovering URLs for {domain}...")
    urls=all_urls(origin,domain,max_pages)
    total=len(urls); done=[0]
    emit("discover",0,total,f"{total} URLs to crawl")
    def work(u):
        r=process(u,domain); done[0]+=1
        emit("crawl",done[0],total,f"{r['status']} {u}")
        return r
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages=list(ex.map(work,urls))
    emit("site",total,total,"Site-wide checks...")
    sitecx=site_checks(origin,domain)
    data=build(domain,origin,pages,sitecx); apply_diff(data,out); write_outputs(data,out)
    emit("done",total,total,f"{domain}: {data['overall']}/100, {data['pages_crawled']} pages")
    return data

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--url"); ap.add_argument("--out",default="report")
    ap.add_argument("--max-pages",type=int,default=0,help="0 = entire site")
    ap.add_argument("--workers",type=int,default=WORKERS)
    ap.add_argument("--calibrate",help="citations.csv (url,citations) to correlate against")
    ap.add_argument("--report",help="existing report.json for --calibrate")
    a=ap.parse_args()
    if a.calibrate:
        calibrate(a.report or (a.out+".json"), a.calibrate); return
    if not a.url: ap.error("--url required (or use --calibrate with --report)")
    print(f"Chrome: {CHROME or 'NONE (raw only)'}")
    def prog(phase,done,total,msg):
        print(f"  [{done}/{total}] {msg}" if phase=="crawl" else msg, flush=True)
    data=run_audit(a.url,out=a.out,max_pages=a.max_pages,workers=a.workers,progress=prog)
    print(f"\n=== CITED Score: {data['domain']} === {data['overall']}/100 | {data['pages_crawled']} pages")
    print("Pillars: "+" | ".join(f"{k} {v}" for k,v in data['pillars'].items()))
    print("Engines: "+" | ".join(f"{e} {v}" for e,v in data['engines'].items()))
    top=data['issues'][:3]
    print("Do first: "+" ; ".join(f"{i['label']} (+{i['gain_overall']})" for i in top))
    print(f"Report: {a.out}.html / .json / .csv")

if __name__=="__main__": main()
