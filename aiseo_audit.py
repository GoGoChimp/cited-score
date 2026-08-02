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
 "schema":     {"label":"Content-type schema (Article/Service/etc.)","pillar":"Known","ch":"Ch4","phase":1,"effort":"Med",
                "ev":"Schema classifies the page as an entity the engine can attribute (Ch4)."},
 "parity":     {"label":"Schema readable without JavaScript","pillar":"Known","ch":"Ch4","phase":1,"effort":"Low",
                "ev":"Non-JS AI crawlers never run your JavaScript, so JS-injected schema is invisible to them (Ch4)."},
 "canonical":  {"label":"Canonical tag present","pillar":"Known","ch":"Ch4","phase":1,"effort":"Low",
                "ev":"A self-referencing canonical stops duplicate-entity confusion (Ch4)."},
 "internal":   {"label":"Internal links in content (>=3)","pillar":"Known","ch":"Ch4","phase":2,"effort":"Med",
                "ev":"Internal links build the topical cluster engines read as authority (Ch4)."},
 "entity":     {"label":"Entity clarity (Organization/Person + sameAs)","pillar":"Known","ch":"Ch4","phase":1,"effort":"Med",
                "ev":"sameAs to Wikipedia/Wikidata/LinkedIn plus a stable @id let the engine resolve WHO you are - the core of AI visibility (entity recognition, Ch4)."},
 "schemacomplete":{"label":"Schema is complete, not just present","pillar":"Known","ch":"Ch4","phase":1,"effort":"Med",
                "ev":"Attribute-rich schema (author, dates, image, ids) is cited more than a bare @type (Fischman 61.7 vs 41.6, Ch4)."},
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
 "video":      {"label":"Video / YouTube present","pillar":"Findable","ch":"Ch5","phase":2,"effort":"High",
                "ev":"AI Overviews leans on YouTube; an embedded video or VideoObject adds a citable modality (Ch5)."},
 "comparison": {"label":"Comparison / best-of content on the site","pillar":"Findable","ch":"Ch5","phase":2,"effort":"High",
                "ev":"Comparison and best-of pages are ~33% of AI citations; a site with none forfeits the format engines cite most (Ch5)."},
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
 "author":     {"label":"Named author / E-E-A-T attribution","pillar":"Trusted","ch":"Ch4","phase":2,"effort":"Med",
                "ev":"A named author (Person schema + visible byline) is a trust signal engines weight for citation (E-E-A-T, Ch4)."},
 "sourced":    {"label":"Statistics carry a source","pillar":"Trusted","ch":"Ch4","phase":2,"effort":"Med",
                "ev":"Unsourced numbers read as unverifiable; sourced stats lift citation likelihood (Princeton GEO, Ch4)."},
 "freshness":  {"label":"Fresh (updated < 12 months)","pillar":"Trusted","ch":"Ch4","phase":1,"effort":"Low",
                "ev":"Engines weight recency; undated content loses to dated (Ch4)."},
 "entitydensity":{"label":"Entity density (named entities in prose)","pillar":"Known","ch":"Ch4","phase":2,"effort":"Med",
                "ev":"Cited passages average ~20.6% proper nouns vs 5-8% typical; named entities let the engine attribute the claim (Indig 1.2M-answer study 2026, Ch4)."},
 "readability":{"label":"Readable prose (grade <= ~16)","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"Cited text reads at ~grade 16 vs ~19 for uncited; overly complex prose is lifted less (Indig 2026, Ch5)."},
 "definitional":{"label":"Definitional opener ([X] is ...)","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Low",
                "ev":"Cited passages use definitional '[Entity] is' constructions ~2x more; a clean definition is the liftable answer (Indig 2026, Ch5)."},
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
 "schema":"Add server-rendered content-type JSON-LD: Article/BlogPosting for posts, Service/SoftwareApplication/Book/etc. for other pages.",
 "faq":"Add 6-10 FAQPage Q&A pairs, each a self-contained 40-60 word answer.",
 "parity":"Server-render the JSON-LD so non-JS AI crawlers can read it (Page Settings head, not a JS embed).",
 "freshness":"Add a visible last-updated date and refresh the content.",
 "entitydensity":"Name the specific entities (brands, tools, people, places, methods) in your prose instead of generic nouns.",
 "readability":"Simplify sentences and vocabulary so the key answer reads at roughly grade 12-16.",
 "definitional":"Open with a direct definition: '[Topic] is ...' in the first sentence.",
 "alt":"Add descriptive alt text to every meaningful image.",
 "internal":"Link to 3+ related pages from the body to build the cluster.",
 "canonical":"Add a self-referencing canonical tag.",
 "robots":"Allow GPTBot, PerplexityBot, ClaudeBot, Google-Extended, Bingbot, OAI-SearchBot in robots.txt.",
 "sitemap":"Publish and submit an XML sitemap.",
 "reachability":"Unblock the AI search bots at the WAF / Cloudflare layer.",
 "entity":"Add Organization + Person JSON-LD with a stable @id and sameAs to your Wikipedia/Wikidata/LinkedIn/Crunchbase profiles.",
 "schemacomplete":"Fill the schema out: author, datePublished, dateModified, headline and image, not just @type and name.",
 "video":"Embed a relevant YouTube video or add VideoObject schema for the key explainer or how-to.",
 "author":"Add a named author (Person schema with name + sameAs) and a visible byline with credentials.",
 "sourced":"Cite an authoritative external source next to each statistic, as an inline link.",
 "comparison":"Publish at least one comparison or best-of page (X vs Y, best X for Y) with a structured table.",
}
# per-engine weights (which signals each engine actually weights). site ids allowed.
# Render-parity CORRECTED 2026-07-19 after deep research (book/research/schema-render-parity-...):
# GPTBot / PerplexityBot / ClaudeBot do NOT execute JavaScript (Vercel/MERJ, 500M+ fetches), so
# JS-injected schema is invisible to ChatGPT / Perplexity / Claude - parity is a hard visibility
# gate there (weight 3). Bing/Copilot renders but unreliably (weight 2). Google renders JS, so
# Gemini + AI Overviews DO eventually see JS-injected schema, so parity is removed for them (it is
# a reliability/speed issue there, not visibility). schema itself still weighted for Gemini.
ENGINE_WEIGHTS = {
 "ChatGPT":     {"wordcount":3,"statdensity":3,"citations":3,"parity":3,"entity":2,"entitydensity":2,"schemacomplete":2,"author":2,"sourced":2,"answerfirst":2,"sections":2,"schema":1,"definitional":1,"readability":1,"freshness":1,"qheadings":1},
 "Perplexity":  {"freshness":3,"citations":3,"parity":3,"sourced":2,"entity":2,"entitydensity":2,"statdensity":2,"answerfirst":2,"liststables":2,"sections":2,"reachability":2,"definitional":1,"qheadings":1,"author":1,"comparison":1,"video":1},
 "AI Overviews":{"answerfirst":3,"qheadings":3,"definitional":2,"sections":2,"schema":2,"faq":2,"liststables":2,"entity":2,"entitydensity":1,"readability":1,"schemacomplete":2,"video":2,"freshness":1,"canonical":1,"sitemap":1,"title":1,"meta":1,"comparison":1,"author":1},
 "Gemini":      {"schema":3,"entity":3,"entitydensity":2,"schemacomplete":2,"statdensity":2,"answerfirst":2,"canonical":1,"sitemap":1,"faq":1,"citations":1,"author":1},
 "Copilot":     {"schema":3,"sitemap":2,"reachability":2,"liststables":2,"statdensity":2,"answerfirst":2,"parity":2,"entity":2,"entitydensity":1,"schemacomplete":2,"faq":2,"freshness":1,"sourced":1,"comparison":1,"video":1},
 "Claude":      {"sections":3,"parity":3,"answerfirst":2,"definitional":1,"readability":1,"statdensity":2,"qheadings":2,"liststables":2,"entity":2,"entitydensity":1,"citations":1,"author":1,"sourced":1},
}
ENGINE_NOTE = {
 "ChatGPT":"Favours comprehensive, authoritative, source-cited content + strong entity grounding. Cites few sources per answer, so be THE definitive page.",
 "Perplexity":"Live-searches every query. Rewards freshness, extractable facts and external citations. Cites many sources, so breadth helps.",
 "AI Overviews":"Rank-coupled + query fan-out. Answer-first, question headings, schema and self-contained sections win.",
 "Gemini":"Google index + Knowledge Graph. Entity/schema clarity, sameAs and canonical carry visibility across.",
 "Copilot":"Bing-grounded and highly citation-friendly. Schema, sitemap/IndexNow, listicles and extractable facts win here.",
 "Claude":"Synthesises rather than quotes. Rewards clean logical chunking, factual density and clear structure.",
}
SITE_IDS = {"robots","llms","sitemap","reachability","comparison"}
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
    "home":     {"answerfirst","qheadings","sections","faq","freshness","schema","schemacomplete","author","sourced","video","readability","entitydensity","definitional"},
    "listing":  {"answerfirst","qheadings","sections","faq","freshness","schema","statdensity","citations","wordcount","schemacomplete","author","sourced","video","entity","readability","entitydensity","definitional"},
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
    fp=None; fptext=""
    for p in root.find_all("p"):
        pt=p.get_text(" ",strip=True); n=len(words(pt))
        if n>=12: fp=n; fptext=pt; break
    C.append(chk("answerfirst","good" if fp and 40<=fp<=60 else ("warn" if fp and 30<=fp<=90 else "bad"),f"opening para {fp or 0} words"))
    defn=bool(re.match(r"^\W{0,3}[A-Z][\w&/.\- ]{1,60}?\s+(is|are|means|refers to)\b", fptext))
    C.append(chk("definitional","good" if defn else "warn","definitional opener" if defn else "opener is not a definition"))
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
    _sents=max(1,len(re.findall(r"[.!?]+(?:\s|$)",body)))
    def _syl(w):
        w=w.lower(); v="aeiouy"; c=0; pv=False
        for ch in w:
            iv=ch in v
            if iv and not pv: c+=1
            pv=iv
        if w.endswith("e"): c=max(1,c-1)
        return max(1,c)
    _bw=words(body); _syls=sum(_syl(w) for w in _bw) if _bw else 0
    fk=round(0.39*(wc/_sents)+11.8*(_syls/max(1,wc))-15.59,1) if wc else 0
    C.append(chk("readability","good" if 0<fk<=16 else ("warn" if fk<=20 else "bad"),f"grade {fk}"))
    _pn=0; _tot=0
    for _s in re.split(r"[.!?]+\s+",body):
        _ws=re.findall(r"[A-Za-z][A-Za-z'&./\-]*",_s)
        for _i,_w in enumerate(_ws):
            _tot+=1
            if _i>0 and _w[0].isupper(): _pn+=1
    pnd=round(100*_pn/_tot,1) if _tot else 0
    C.append(chk("entitydensity","good" if pnd>=12 else ("warn" if pnd>=6 else "bad"),f"{pnd}% named entities"))
    ext=0
    for a in root.find_all("a",href=True):
        if a["href"].startswith("http") and domain not in urllib.parse.urlparse(a["href"]).netloc.replace("www.",""): ext+=1
    C.append(chk("citations","good" if ext>=2 else ("warn" if ext==1 else "bad"),f"{ext} external links"))
    tset=set(types_r)
    _ART={"Article","BlogPosting","NewsArticle","TechArticle"}
    # Article pages need an Article type. Other page types are correctly classified by an
    # appropriate content-type schema (Service, SoftwareApplication, Book, Product, etc.);
    # generic WebPage/WebSite/Breadcrumb/Speakable alone do not classify the page.
    _CONTENT={"Service","SoftwareApplication","WebApplication","MobileApplication","Book","Product",
              "Course","Event","Recipe","HowTo","CollectionPage","CreativeWork","ItemList",
              "DefinedTermSet","AboutPage","ContactPage","QAPage","ProfessionalService","LocalBusiness"}
    has_art = bool(tset & _ART) if ptype=="article" else bool(tset & (_ART|_CONTENT))
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
    # ---- entity / schema-completeness / author (deep JSON-LD read) ----
    objs=jsonld_objs(soup)
    nodes=[]; _st=list(objs)
    while _st:
        n=_st.pop()
        if isinstance(n,dict): nodes.append(n); _st.extend(v for v in n.values() if isinstance(v,(dict,list)))
        elif isinstance(n,list): _st.extend(n)
    def _t(n,ts):
        t=n.get("@type"); return bool(set(t)&ts) if isinstance(t,list) else (t in ts)
    orgp=[n for n in nodes if _t(n,{"Organization","Person","LocalBusiness","Corporation","ProfessionalService"})]
    sameas=[]
    for n in orgp:
        sa=n.get("sameAs")
        if isinstance(sa,str): sameas.append(sa)
        elif isinstance(sa,list): sameas.extend(x for x in sa if isinstance(x,str))
    strong=[u for u in sameas if any(d in u.lower() for d in ("wikipedia.org","wikidata.org","linkedin.com","crunchbase.com"))]
    has_id=any(n.get("@id") for n in orgp)
    if orgp and (strong or (len(sameas)>=2 and has_id)): C.append(chk("entity","good",f"{len(orgp)} entity node(s), {len(sameas)} sameAs"))
    elif orgp: C.append(chk("entity","warn",f"entity present, weak sameAs ({len(sameas)})"))
    else: C.append(chk("entity","bad","no Organization/Person entity"))
    art=[n for n in nodes if _t(n,{"Article","BlogPosting","NewsArticle","TechArticle","Product","Recipe","HowTo","Review"})]
    if not objs: C.append(chk("schemacomplete","na","no structured data"))
    elif art:
        an=art[0]; have=[k for k in ("author","datePublished","dateModified","headline","name","image","publisher") if an.get(k)]
        keyn=sum(1 for k in ("author","datePublished","image") if an.get(k))+(1 if (an.get("headline") or an.get("name")) else 0)
        C.append(chk("schemacomplete","good" if keyn>=3 else ("warn" if keyn>=1 else "bad"),f"{len(have)} fields, {keyn} key"))
    else: C.append(chk("schemacomplete","warn","no content-type schema"))
    anames=[]
    for n in nodes:
        a=n.get("author")
        for x in (a if isinstance(a,list) else [a]):
            if isinstance(x,dict) and x.get("name"): anames.append(str(x["name"]))
            elif isinstance(x,str) and x.strip(): anames.append(x)
    byline=bool(soup.select_one('[rel="author"],[class*="author" i],[class*="byline" i]')) or bool(re.search(r"\bby\s+[A-Z][a-z]+\s+[A-Z][a-z]",body[:400]))
    if anames: C.append(chk("author","good","by "+anames[0][:40]))
    elif byline: C.append(chk("author","warn","byline text, no author schema"))
    else: C.append(chk("author","bad","no author"))
    if dens<0.6: C.append(chk("sourced","good","few stats to source"))
    elif ext>=2: C.append(chk("sourced","good",f"{ext} sources for {nums} numbers"))
    elif ext==1: C.append(chk("sourced","warn","only 1 source for the stats"))
    else: C.append(chk("sourced","bad",f"{nums} numbers, no external source"))
    yt=bool(soup.find("iframe",src=re.compile(r"youtube\.com|youtu\.be|vimeo\.com|wistia",re.I))) or bool(soup.find("video")) or ("VideoObject" in types_r)
    C.append(chk("video","good" if yt else "warn","video present" if yt else "no video / VideoObject"))
    na=NA_BY_TYPE.get(ptype,set())
    for c in C:
        if c["id"] in na: c["status"]="na"
    metrics={"words":wc,"headings":nh,"question_pct":qpct,"walls":walls,"stat_density":dens,
             "readability":fk,"entity_density":pnd,"definitional":defn,
             "ext_links":ext,"internal_links":il,"schema_types":sorted(tset),"images":len(imgs),"alt_pct":apct}
    return {"path":path,"type":ptype,"title":title,"checks":C,"metrics":metrics,"rendered":rendered is not None}

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
    # site-level: does the site publish comparison / best-of content (a top AI-cited format)?
    CMP_RE=re.compile(r"(vs\.?|versus|comparison|compare|best[- ]|top[- ]?\d+|alternativ|which[- ])",re.I)
    _cmp=[p for p in pages if CMP_RE.search((p.get("path") or "")+" "+(p.get("title") or ""))]
    sitecx=list(sitecx)+[chk("comparison","good" if _cmp else "warn",(str(len(_cmp))+" comparison/best-of page(s)") if _cmp else "no comparison / best-of content found")]
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

def parse_cites(text):
    """Parse 'url,citations' rows (Bing WMT AI Performance export) -> {url_no_slash: float}."""
    cites={}
    for row in csv.reader(text.splitlines()):
        if len(row)<2: continue
        try: cites[row[0].strip().rstrip("/")]=float(str(row[1]).replace(",","").strip())
        except ValueError: continue
    return cites

def calibrate_data(d, cites):
    """Spearman-correlate the report's scores vs real per-URL citations. Returns a dict for UI/CLI."""
    rows=[p for p in d["pages"] if p["url"].rstrip("/") in cites]
    if len(rows)<8:
        return {"error":f"Only {len(rows)} of {len(d['pages'])} crawled pages matched the citations file (need at least 8 for a stable correlation).","matched":len(rows)}
    y=[cites[p["url"].rstrip("/")] for p in rows]
    out={"matched":len(rows),"total":len(d["pages"]),
         "overall":spearman([p["score"] for p in rows],y),
         "pillars":{pl:spearman([p["pillars"][pl] for p in rows],y) for pl in PILLARS},
         "engines":{e:spearman([p["engines"][e] for p in rows],y) for e in ENGINE_WEIGHTS},
         "checks":[]}
    for cid in CHECK_META:
        xs=[STAT.get(p["cs"].get(cid),None) for p in rows]
        pairs=[(x,yy) for x,yy in zip(xs,y) if x is not None]
        if len(pairs)<8: continue
        vals=[a for a,_ in pairs]; modal=max(Counter(vals).values())/len(vals) if vals else 1.0
        rho=spearman(vals,[b for _,b in pairs])
        verdict=("table-stakes" if modal>=0.95 else "up-weight" if rho>=0.2 else "neutral" if rho>0 else "investigate")
        out["checks"].append({"id":cid,"label":CHECK_META[cid]["label"],"rho":rho,"n":len(pairs),"modal":round(modal*100),"verdict":verdict})
    out["checks"].sort(key=lambda c:c["rho"],reverse=True)
    return out

def calibrate(report_json, citations_csv):
    d=json.load(open(report_json,encoding="utf-8"))
    r=calibrate_data(d, parse_cites(open(citations_csv,encoding="utf-8-sig").read()))
    if not r or r.get("error"):
        print((r or {}).get("error","no data")); print("CSV format: url,citations (Bing WMT > AI Performance)."); return
    print(f"\n=== CITED Score calibration vs {r['matched']} pages with real citations ===")
    print(f"Overall  <-> citations : rho {r['overall']:+.2f}")
    for pl,v in r["pillars"].items(): print(f"{pl:13} <-> citations : rho {v:+.2f}")
    for e,v in r["engines"].items(): print(f"{e:13} <-> citations : rho {v:+.2f}")
    print("\nPer-check predictive power (rho ~0 at high modal = table stakes, not measurable here - do NOT drop):")
    for c in r["checks"]:
        print(f"  {c['id']:14} rho {c['rho']:+.2f}  (n={c['n']}, {c['modal']}% modal)  {c['verdict']}")
    print("\nCaveat: directional. Heavy-tailed samples and single sites mislead; recalibrate as data grows.")

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
        b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'><rect x='4' y='4' width='248' height='248' rx='48' fill='#ff4d00'/></svg>").decode()

def write_html(d, path):
    payload=json.dumps(d,ensure_ascii=False).replace("</","<\\/")
    css=r"""
:root{--bg:#100D0B;--panel:#1A1613;--panel2:#241C15;--line:#2E2820;--muted:#9A9284;--txt:#F0EBE0;
 --grn:#ff4d00;--grn2:#ff7a33;--deep:#c23a00;--amber:#F5A623;--red:#F16A5F;--chip:#D63B2F}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 'Figtree',-apple-system,Segoe UI,Arial,sans-serif}
a{color:var(--txt);text-decoration:none}a:hover{color:var(--grn)}
header{padding:18px 26px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{font-family:Impact,'Haettenschweiler','Arial Narrow',sans-serif;font-size:26px;font-weight:400;letter-spacing:-.3px;text-transform:uppercase}
.chip{background:var(--grn);color:#0a0a0a;font-family:'Figtree',sans-serif;font-weight:800;font-size:11px;padding:1px 5px;border-radius:4px;vertical-align:super;margin-left:4px}
header .m{color:var(--muted);font-size:13px}
.btns{margin-left:auto;display:flex;gap:8px}
button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 12px;cursor:pointer;font-size:12px}
button:hover{border-color:var(--grn);color:var(--grn2)}
.tabs{display:flex;flex-wrap:wrap;gap:2px;padding:0 18px;border-bottom:1px solid var(--line);background:#120F0C;position:sticky;top:0;z-index:5}
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
 background:conic-gradient(var(--c) calc(var(--p)*1%),#2C231C 0)}.ring i{width:58px;height:58px;border-radius:50%;background:var(--panel);display:grid;place-items:center;font-style:normal}
.engs{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.eng{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;display:flex;gap:14px;align-items:center;cursor:pointer}
.eng .b{font-weight:800}.eng .d{color:var(--muted);font-size:12px;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:42px;background:#120F0C}
tr:hover td{background:#201A14}.sc{font-weight:800;border-radius:6px;padding:2px 8px;color:#08110a;display:inline-block;min-width:30px;text-align:center}
.badge{font-size:10px;padding:1px 6px;border-radius:20px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.badge.Known{border-color:#3aa0ff55;color:#7bbcff}.badge.Findable{border-color:#ff4d0055;color:var(--grn2)}.badge.Trusted{border-color:#f5a62355;color:var(--amber)}
.dot{font-weight:800}.dot.good{color:var(--grn)}.dot.warn{color:var(--amber)}.dot.bad{color:var(--red)}
.issue{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:10px}
.issue h4{margin:0 0 4px;font-size:15px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.issue .ev{color:var(--muted);font-size:12px;margin:6px 0}.issue .fix{color:#d9dcc9;margin-top:4px}
.issue .urls{margin-top:8px;font-size:12px;color:var(--muted);max-height:160px;overflow:auto;display:none}
.issue.open .urls{display:block}
.sev{font-size:11px;padding:2px 8px;border-radius:20px}.sev.bad{background:#3a1a18;color:var(--red)}.sev.warn{background:#3a2f12;color:var(--amber)}
.gain{font-size:11px;padding:2px 8px;border-radius:20px;background:#3A1C0E;color:var(--grn2);font-weight:800}
.rank{background:var(--grn);color:#08110a;font-weight:800;width:24px;height:24px;border-radius:50%;display:inline-grid;place-items:center;font-size:12px}
.phase{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:6px 14px 14px;margin-bottom:16px}
.phase h3{color:var(--grn2);margin:10px 0}
.muted{color:var(--muted)}.hide{display:none}h3{margin:18px 0 10px;font-size:15px}
.bar{height:8px;background:#2C231C;border-radius:6px;overflow:hidden;min-width:120px}.bar i{display:block;height:100%}
input.search{background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:7px 10px;font-size:13px;width:260px;margin-bottom:12px}
.foot{color:var(--muted);font-size:12px;padding:20px 26px;border-top:1px solid var(--line);max-width:1000px}
.diffline{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:13px}
:root{--display:Impact,'Haettenschweiler','Arial Narrow',sans-serif;--ok:#3ecf8e;--warn2:#f2b53c;--err2:#ff4d3d}
.ov{display:grid;grid-template-columns:1fr 336px;gap:22px;align-items:start}
@media(max-width:1080px){.ov{grid-template-columns:1fr}}
.ov2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:640px){.ov2{grid-template-columns:1fr}}
.sech{font-family:var(--display);text-transform:uppercase;letter-spacing:.6px;font-size:17px;color:var(--txt);font-weight:400;margin:26px 0 12px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.sech .s{font-family:'Figtree',sans-serif;text-transform:none;letter-spacing:0;font-size:12px;color:var(--muted);font-weight:400}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px}
.hero{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.sring{--p:0;--c:var(--grn);width:150px;height:150px;flex:0 0 150px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--c) calc(var(--p)*1%),#2C231C 0)}
.sring i{width:120px;height:120px;border-radius:50%;background:var(--panel);display:flex;flex-direction:column;align-items:center;justify-content:center;font-style:normal;gap:2px}
.sring .v{font-family:var(--display);font-size:52px;line-height:.85;color:var(--txt)}
.sring .o{font-size:10px;letter-spacing:1.5px;color:var(--muted)}
.htitle{font-family:var(--display);text-transform:uppercase;font-size:22px;letter-spacing:.5px}
.hsub{color:var(--muted);font-size:13px;margin:4px 0 10px;max-width:290px}
.hdelta{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:3px 12px;font-size:12px;font-weight:600}
.hc{flex:1;min-width:250px;border-left:1px solid var(--line);padding-left:24px}
.hclabel{font-family:var(--display);text-transform:uppercase;font-size:13px;letter-spacing:.6px;color:var(--muted)}
.chbar{display:flex;height:12px;border-radius:999px;overflow:hidden;gap:2px;margin:10px 0 14px}
.chstat{display:flex;gap:26px}
.chstat .n{font-family:var(--display);font-size:26px;line-height:1}
.chstat .l{font-size:11px;color:var(--muted)}
.trow{display:grid;grid-template-columns:170px 1fr 62px;gap:16px;align-items:center;padding:13px 0;border-bottom:1px solid var(--line)}
.trow:last-child{border-bottom:0}
.trow.eng{grid-template-columns:170px 1fr 100px 62px}
.trow .q{font-weight:800}.trow .qd,.qd{color:var(--muted);font-size:12px}
.tbar{position:relative;height:12px;background:#2C231C;border-radius:999px}
.tbar i{position:absolute;left:0;top:0;height:100%;border-radius:999px}
.tbar .thr{position:absolute;top:-4px;height:20px;width:2px;background:var(--muted);opacity:.55}
.tval{text-align:right;font-family:var(--display);font-size:26px;line-height:1;white-space:nowrap}
.tnote{color:var(--err2);font-size:12px;font-weight:600;text-align:right}
.d{font-size:12px;font-weight:700}.d.up{color:var(--ok)}.d.dn{color:var(--err2)}.d.z{color:var(--muted)}
.df{border:1px solid var(--line);border-radius:14px;padding:18px}
.dfh{display:flex;justify-content:space-between;align-items:baseline}
.dfh .t{font-family:var(--display);text-transform:uppercase;font-size:15px;letter-spacing:.5px}
.dfrow{border-top:1px solid var(--line);padding:16px 0}.dfrow:first-of-type{border-top:0;padding-top:12px}
.dfrow:hover{background:#ffffff06}
.dfnum{width:22px;height:22px;border-radius:50%;border:1px solid var(--grn);color:var(--grn);font-size:12px;font-weight:800;display:inline-grid;place-items:center;margin-right:6px}
.dfgain{font-family:var(--display);font-size:22px;color:var(--grn);text-align:right;line-height:1}.dfgl{font-size:9px;color:var(--muted);letter-spacing:.5px}
.chip2{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:2px 8px;font-size:11px;font-weight:600;margin:0 6px 4px 0}
.listbtn{border:1px solid var(--line);color:var(--txt);border-radius:8px;padding:5px 12px;font-size:12px;font-weight:700;background:none;cursor:pointer}
.dffull{display:block;text-align:center;background:var(--grn);color:#0a0a0a;border-radius:12px;padding:11px;font-weight:800;margin-top:14px;cursor:pointer;text-decoration:none}
.lg{border:1px dashed var(--line);border-radius:14px;padding:18px;margin-top:16px}
.lgrow{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-top:1px solid var(--line)}.lgrow:first-of-type{border-top:0}
.note{border:1px dashed var(--line);border-radius:12px;padding:12px 16px;margin-top:16px;color:var(--muted);font-size:12px;display:flex;align-items:center;gap:8px}
.note::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--ok);flex:0 0 8px}
.bl{display:flex;align-items:center;gap:12px;padding:6px 0}
.bl .lab{width:64px;color:var(--muted);font-size:13px}
.bl .bar2{height:8px;background:#2C231C;border-radius:999px;flex:1;position:relative}
.bl .bar2 i{position:absolute;left:0;top:0;height:100%;border-radius:999px;background:var(--grn)}
.bl .num{font-family:var(--display);font-size:16px;width:28px;text-align:right}
.rch{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-top:1px solid var(--line);font-size:13px}.rch:first-of-type{border-top:0}
.rst{font-size:13px}.rst.ok{color:var(--ok)}.rst.wn{color:var(--warn2)}.rst.er{color:var(--err2)}.rst::before{content:'\25CF '}
.sech,.htitle,.hclabel,.sring .v,.tval,.chstat .n,.dfgain,.bl .num,.dfh .t{font-family:'Figtree',sans-serif;font-weight:800;letter-spacing:0}
/* ===== Action Plan + Issues (mockup redesign) ===== */
.ap2{display:grid;grid-template-columns:1fr 380px;gap:24px;align-items:start}
.apmain{display:flex;flex-direction:column;gap:20px;min-width:0}
.apside{display:flex;flex-direction:column;gap:20px;position:sticky;top:12px}
.apsum,.issum{background:linear-gradient(180deg,#17130f,#141110);border:1px solid var(--line);border-radius:14px;padding:22px 26px}
.apk{font-size:11px;font-weight:600;letter-spacing:.1em;color:var(--muted)}
.apbn{font-family:'Figtree',sans-serif;font-size:46px;font-weight:900;line-height:1;letter-spacing:-.02em}
.apintro{font-size:13px;line-height:1.6;color:#9d9691;max-width:900px}
.aptier{display:flex;flex-direction:column;gap:12px}
.aptierh{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.aptierh h3{margin:0;font-size:16px;letter-spacing:.06em;font-family:'Figtree',sans-serif;font-weight:800}
.aptierh .sq{width:9px;height:9px;border-radius:2px;flex:none}
.aptierh .meta{font-size:12px;color:var(--muted)}
.apbox{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:2px 22px 6px}
.apbox.hot{border-color:#ff4d0047}
.apissue{border-bottom:1px solid #ffffff10}
.apissue:last-child{border-bottom:0}
.aprow{display:grid;gap:18px;align-items:center;padding:15px 0}
.aprow:hover{background:#ffffff06}
.eb{width:5px;height:11px;border-radius:1px;display:inline-block}
.ebs{display:inline-flex;gap:2px;margin-right:6px;vertical-align:middle}
.apchip{display:inline-block;font-size:11px;color:#c9c2bd;background:#ffffff10;padding:2px 7px;border-radius:4px;margin:0 6px 2px 0}
.apgain{font-family:'Figtree',sans-serif;font-weight:900;letter-spacing:-.02em;line-height:1}
.pbtn{font:inherit;font-size:12px;font-weight:600;color:var(--grn);background:transparent;border:1px solid #ff4d0066;border-radius:5px;padding:5px 10px;cursor:pointer}
.pbtn:hover{background:#ff4d001f}
.pbtn.g{color:#c9c2bd;border-color:#ffffff28}.pbtn.g:hover{color:#fff;border-color:#ffffff5c}
.apurls{display:none;font-size:12px;padding:2px 0 12px;columns:2;column-gap:24px}
.apurls a{color:var(--muted)}.apurls a:hover{color:var(--txt)}
.apurls .b{display:block;padding:2px 0;break-inside:avoid;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dotb{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle;flex:none}
.rmc{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.rmch{padding:16px 20px 12px;border-bottom:1px solid #ffffff12}
.rmch h3{margin:0;font-size:15px;letter-spacing:.06em;font-family:'Figtree',sans-serif;font-weight:800}
.rmph{padding:16px 20px;border-bottom:1px solid #ffffff12}
.rmph:last-child{border-bottom:0}
.rmnum{font-size:12px;font-weight:800;padding:3px 8px;border-radius:4px;flex:none}
.card2{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.card2 h3{margin:0 0 10px;font-size:15px;letter-spacing:.06em;font-family:'Figtree',sans-serif;font-weight:800}
.rowsb{display:flex;align-items:center;justify-content:space-between;font-size:12px;padding:4px 0}
.bigbtn{display:block;width:100%;text-align:center;font:inherit;font-size:13px;font-weight:700;color:#140b06;background:var(--grn);border:none;border-radius:8px;padding:12px;cursor:pointer}
.bigbtn:hover{background:var(--grn2);color:#0a0a0a}
.dashnote{display:flex;align-items:center;gap:10px;padding:14px 18px;border:1px dashed #ffffff24;border-radius:12px;font-size:11px;color:var(--muted);line-height:1.5}
.issum{display:flex;align-items:center;gap:30px;flex-wrap:wrap}
.issum .big{font-size:44px;font-weight:900;line-height:1;font-family:'Figtree',sans-serif;letter-spacing:-.02em}
.istat{display:flex;flex-direction:column;gap:5px;justify-content:center}
.inumwrap{display:flex;align-items:center;gap:8px;min-height:34px}
.inum{font-family:'Figtree',sans-serif;font-size:34px;font-weight:900;line-height:1;letter-spacing:-.02em}
.isub{font-size:12px;color:var(--muted);line-height:1.35}
.vr{width:1px;align-self:stretch;min-height:44px;background:#ffffff14}
.fpill{font:inherit;font-size:12px;font-weight:600;color:#c9c2bd;background:transparent;border:1px solid #ffffff28;border-radius:999px;padding:7px 14px;cursor:pointer}
.fpill:hover{color:#fff}
.fpill.on{color:#140b06;background:var(--grn);border-color:var(--grn)}
.wpage{display:flex;align-items:center;gap:14px;padding:13px 20px;border-bottom:1px solid #ffffff10;cursor:pointer;color:inherit}
.wpage:hover{background:#ffffff08}
.wpage:last-child{border-bottom:0}
.wpage .s{font-size:20px;font-weight:800;width:34px;font-family:'Figtree',sans-serif}
@media(max-width:1080px){.ap2{grid-template-columns:1fr}.apside{position:static}.apurls{columns:1}}
/* ===== Pages (mockup redesign) ===== */
.pgsum{background:linear-gradient(180deg,#17130f,#141110);border:1px solid var(--line);border-radius:14px;padding:20px 26px;display:flex;align-items:center;gap:30px;flex-wrap:wrap}
.pgdist{display:flex;align-items:flex-end;gap:8px;height:76px}
.pgdist .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:5px}
.pgdist .trk{height:44px;display:flex;align-items:flex-end}
.pgdist .trk i{width:100%;border-radius:4px 4px 0 0;display:block}
.pgscroll{overflow-x:auto}
.pgtbl{background:var(--panel2);border:1px solid var(--line);border-radius:14px;overflow:hidden;min-width:1060px}
.pgcols{display:grid;grid-template-columns:58px 1fr 40px 40px 40px 50px 50px 50px 50px 50px 50px 56px 220px;gap:9px;align-items:center}
.pghead{padding:12px 20px;background:#171412;border-bottom:1px solid var(--line);font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--muted)}
.pghead span[data-s]{cursor:pointer}.pghead span[data-s]:hover{color:#fff}
.pgrow{padding:11px 20px;border-bottom:1px solid #ffffff0d}
.pgrow:last-child{border-bottom:0}
.pgrow a{font-size:13px;font-weight:600;color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pgrow a:hover{color:var(--grn)}
.tybadge{font-size:10px;color:#6f6864;border:1px solid #ffffff1f;border-radius:3px;padding:1px 5px;flex:none}
.schip{font-size:15px;font-weight:800;text-align:center;padding:5px 0;border-radius:6px;font-family:'Figtree',sans-serif}
.ecell{font-size:12px;font-weight:600;text-align:center;padding:4px 0;border-radius:4px}
.pgpill{font:inherit;font-size:12px;font-weight:600;color:#c9c2bd;background:transparent;border:1px solid #ffffff28;border-radius:999px;padding:7px 13px;cursor:pointer}
.pgpill:hover{color:#fff}
.pgpill.on{color:#140b06;background:var(--grn);border-color:var(--grn)}
.pgpill.bel.on{color:#ff9c88;background:transparent;border-color:#ff4d3d}
.pgleg{display:flex;align-items:center;gap:22px;padding:13px 20px;background:#0f0d0c;border-top:1px solid var(--line);font-size:11px;color:#6f6864;flex-wrap:wrap}
.pgsearch{display:flex;align-items:center;gap:9px;background:var(--panel2);border:1px solid #ffffff24;border-radius:8px;padding:0 14px;width:280px}
.pgsearch input{flex:1;background:transparent;border:none;outline:none;font:inherit;font-size:13px;color:#fff;padding:10px 0}
.pgsearch input::placeholder{color:#6f6864}
/* ===== Engines + Site structure + Response times ===== */
.engcols{display:grid;grid-template-columns:1fr 92px 230px 84px;gap:18px;align-items:center}
.enghead{padding:12px 0;border-bottom:1px solid var(--line);font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--muted)}
.engrow{padding:15px 0;border-bottom:1px solid #ffffff0f;cursor:pointer}
.engrow:last-child{border-bottom:0}
.engrow:hover{background:#ffffff06}
.egcar{font-size:9px;color:var(--muted);vertical-align:middle}
.engdet{display:none;font-size:12px;padding:2px 0 14px;columns:2;column-gap:26px}
.engdet .egp{display:block;padding:2px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;break-inside:avoid}
.engdet .egp a{color:var(--muted)}.engdet .egp a:hover{color:var(--txt)}
.egh{font-size:10px;letter-spacing:.05em;color:var(--muted);text-transform:uppercase;margin:2px 0 4px;column-span:all}
.engbar{height:9px;border-radius:5px;background:#221d1a;overflow:hidden}
.engbar i{display:block;height:100%;border-radius:5px}
.wdots{font-size:13px;letter-spacing:.12em;white-space:nowrap}
.engwcols{display:grid;grid-template-columns:70px 1fr 220px 66px 74px;gap:16px;align-items:center}
.engwrow{padding:12px 20px;border-bottom:1px solid #ffffff0d}
.engwrow:last-child{border-bottom:0}
.engwhead{padding:12px 20px;background:#171412;border-bottom:1px solid var(--line);font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--muted)}
.card2.hot{border-color:#ff4d004d}
.numbadge{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:800;flex:none}
.stbar{position:relative;height:8px;border-radius:5px;background:#221d1a;overflow:hidden;flex:1}
.stbar i{display:block;height:100%;border-radius:5px}
.stthr{position:absolute;left:70%;top:0;height:100%;width:2px;background:var(--muted);opacity:.45;z-index:1}
.strow:hover{background:#ffffff06}
.stdet{display:none;padding:2px 0 12px}
.stp{display:flex;align-items:flex-start;gap:14px;padding:8px 0;font-size:13px;border-top:1px solid #ffffff08}
.stp:first-child{border-top:0}
.stp .schip{width:40px;flex:none;font-size:12px;padding:3px 0}
.stp a{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stp a:hover{color:var(--txt)}.stp .qd{font-size:12px}
.stcks{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.stck{font-size:10px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);white-space:nowrap}
.stck.bad{color:#ff9c88;border-color:#ff4d3d44}.stck.warn{color:#f2c574;border-color:#f2b53c44}
.strow{display:grid;grid-template-columns:170px 70px 1fr 52px;gap:16px;align-items:center;padding:12px 0;border-bottom:1px solid #ffffff0f}
.strow:last-child{border-bottom:0}
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
.statcard{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.statcard .n{font-family:'Figtree',sans-serif;font-size:30px;font-weight:900;line-height:1;letter-spacing:-.02em}
.statcard .l{font-size:11px;color:var(--muted);margin-top:6px}
@media(max-width:1080px){.engcols{grid-template-columns:1fr 80px 160px 60px}.engwcols{grid-template-columns:60px 1fr 140px 56px 64px}}
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
const col=s=>s>=75?'#ff4d00':s>=50?'#F5A623':'#F16A5F';
const bcol=v=>v>=70?'#ff4d00':v>=50?'#f2b53c':'#ff4d3d';
const dlt=(now,was)=>{if(was==null)return'';const d=now-was,c=d>0?'up':d<0?'dn':'z',s=(d>0?'+':'')+d;return ` <span class="d ${c}">${s}</span>`};
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
 if(cur=='Overview')return w.innerHTML=ovw2();
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

function ovw2(){
 const df=D.diff||{}, pw=df.pillars_was||{}, ew=df.engines_was||{}, THR=70;
 const g=(D.totals.good)||0, w=(D.totals.warn)||0, b=(D.totals.bad)||0, tc=g+w+b, pass=tc?Math.round(100*g/tc):0;
 const od=df.overall_d;
 const odtxt = (od==null)?'' : `<span class="hdelta">${od>0?'▲':od<0?'▼':'▬'} ${Math.abs(od)} pt${Math.abs(od)==1?'':'s'} vs ${esc(df.since||'last crawl')}</span>`;
 const Q={Known:'Do the engines know you exist?',Findable:'Can they find your answer?',Trusted:'Do they trust you enough to name you?'};
 const prow=(label,desc,val,was)=>`<div class="trow"><div><div class="q">${label}</div><div class="qd">${desc}</div></div><div class="tbar"><i style="width:${Math.max(2,val)}%;background:${bcol(val)}"></i><span class="thr" style="left:${THR}%"></span></div><div class="tval">${val}${dlt(val,was)}</div></div>`;
 const engs=ECOLS.slice().sort((a,c)=>D.engines[a]-D.engines[c]);
 const erow=e=>{const val=D.engines[e],pts=THR-val;return `<div class="trow"><div><div class="q">${e}</div><div class="qd">${pts>0?pts+' pts to quotable':'clears 70 · quotable'}</div></div><div class="tbar"><i style="width:${Math.max(2,val)}%;background:${bcol(val)}"></i><span class="thr" style="left:${THR}%"></span></div><div class="tval">${val}${dlt(val,ew[e])}</div></div>`};
 const dcard=(i,x)=>{const eg=Object.entries(i.gain_engines||{}).filter(([e,v])=>v>0).sort((a,c)=>c[1]-a[1]).slice(0,3);
   return `<div class="dfrow" onclick="tgl('pd_${i.id}')" style="cursor:pointer"><div style="display:flex;justify-content:space-between;gap:10px"><div style="font-weight:800;display:flex;align-items:flex-start;flex:1;min-width:0"><span class="dfnum">${x+1}</span><span>${esc(i.label)} <span class="egcar">▾</span></span></div><div class="dfgain">+${i.gain_overall||0}<div class="dfgl">overall</div></div></div><div class="qd" style="margin:6px 0 4px 28px">${esc(i.ev)}</div><div style="margin-left:28px">${eg.map(([e,v])=>`<span class="chip2">${e} +${v}</span>`).join('')}</div><div style="margin:10px 0 0 28px"><span class="qd">${i.pillar} · ${i.ch} · ${i.effort} effort · ${i.count} page${i.count>1?'s':''}</span></div><div style="margin-left:28px">${pdet(i.id)}</div></div>`};
 const decl=df.declined||[];
 const lg=decl.length?`<div class="lg"><div class="dfh"><div class="t">Losing ground</div><span class="qd">since ${esc(df.since||'')}</span></div>${decl.slice(0,4).map(m=>`<div class="lgrow"><span>${rel(m.url)}</span><span><span class="qd">${m.was} → </span><b>${m.now}</b> <span class="d dn">${m.d}</span></span></div>`).join('')}</div>`:'';
 const types=Object.entries(D.types||{}).sort((a,c)=>c[1]-a[1]),tmax=Math.max.apply(0,types.map(t=>t[1]).concat(1));
 const typesH=types.map(([k,v])=>`<div class="bl"><div class="lab">${k}</div><div class="bar2"><i style="width:${Math.round(100*v/tmax)}%"></i></div><div class="num">${v}</div></div>`).join('');
 const P=D.pages.length, parityBad=D.pages.filter(p=>p.cs&&p.cs.parity=='bad').length, reach=((D.site_checks||[]).find(s=>s.id=='reachability')||{}).status||'good';
 const rr=(name,st,txt)=>`<div class="rch"><span>${name}</span><span class="rst ${st=='good'?'ok':st=='warn'?'wn':'er'}">${txt}</span></div>`;
 const reachH=rr('GPTBot',reach,reach=='good'?`allowed · ${P}/${P} pages`:reach=='warn'?'partial':'blocked')+rr('PerplexityBot',reach,reach=='good'?`allowed · ${P}/${P} pages`:reach=='warn'?'partial':'blocked')+rr('Google-Extended',reach=='bad'?'bad':'warn',reach=='bad'?'blocked':'partial')+rr('Server-rendered schema',parityBad?'bad':'good',parityBad?`${parityBad} pages JS-injected only`:`all ${P} pages server-rendered`);
 return `<div class="ov"><div>
   <div class="panel"><div class="hero">
     <div class="sring" style="--p:${D.overall};--c:var(--grn)"><i><span class="v">${D.overall}</span><span class="o">OF 100</span></i></div>
     <div style="flex:1;min-width:190px"><div class="htitle">CITED Score</div><div class="hsub">Weighted across six engines. Pages score as <b>quotable</b> at 70.</div>${odtxt}</div>
     <div class="hc"><div style="display:flex;justify-content:space-between;align-items:baseline"><div class="hclabel">Check health · ${tc} checks</div><span class="qd">${pass}% passing</span></div>
       <div class="chbar"><div class="chseg" style="flex:${g||1};background:var(--ok)"></div><div class="chseg" style="flex:${w||0.01};background:var(--warn2)"></div><div class="chseg" style="flex:${b||0.01};background:var(--err2)"></div></div>
       <div class="chstat"><div><div class="n" style="color:var(--err2)">${b}</div><div class="l">Errors — blocking</div></div><div><div class="n" style="color:var(--warn2)">${w}</div><div class="l">Warnings — weakening</div></div><div><div class="n" style="color:var(--ok)">${g}</div><div class="l">Passed</div></div></div></div>
   </div></div>
   <div class="sech">The three questions <span class="s">Ch3 · same scale, so you can see which one is dragging</span></div>
   <div class="panel">${['Known','Findable','Trusted'].map(p=>prow(p,Q[p],D.pillars[p],pw[p])).join('')}<div class="qd" style="margin-top:10px">| quotable threshold, 70</div></div>
   <div class="sech">Readiness by engine <span class="s">Ch6-7 · ranked worst first</span></div>
   <div class="panel">${engs.map(erow).join('')}<div class="qd" style="margin-top:10px">| quotable threshold, 70</div></div>
   <div class="ov2">
     <div><div class="sech">Pages by type</div><div class="panel">${typesH}</div></div>
     <div><div class="sech">Crawler reachability</div><div class="panel">${reachH}</div></div>
   </div>
 </div>
 <div class="ovside">
   <div class="df"><div class="dfh"><div class="t">Do these first</div><span class="qd">${Math.min(3,D.issues.length)} of ${D.issues.length} fixes</span></div><div class="qd" style="margin-bottom:2px">Ranked by score movement per hour of work.</div>${D.issues.slice(0,3).map((i,x)=>dcard(i,x)).join('')}</div>
   ${lg}
   <div class="note">Crawl ran locally. No page data left this machine.</div>
 </div></div>`;
}
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
// ---- shared helpers for Action Plan + Issues ----
const PDOT={Known:'#6f9dff',Findable:'#f2b53c',Trusted:'#3ecf8e'};
const TTYPE={schema:'template',parity:'template',faq:'template',canonical:'template',robots:'template',sitemap:'template',reachability:'template',freshness:'template',internal:'template',http:'template',entity:'template',schemacomplete:'template',
 answerfirst:'copy',definitional:'copy',readability:'copy',entitydensity:'copy',qheadings:'copy',sections:'copy',liststables:'copy',wordcount:'copy',statdensity:'copy',h1:'copy',author:'copy',sourced:'copy',video:'copy',comparison:'copy',
 meta:'meta',title:'meta',alt:'meta',citations:'meta'};
const EBARS=e=>{const n=(e=='High')?3:(e=='Med')?2:1,c=n==1?'#3ecf8e':n==2?'#f2b53c':'#ff4d00',lab=n==1?'low':n==2?'medium':'high';
 let b='';for(let k=0;k<3;k++)b+=`<span class="eb" style="background:${k<n?c:'#2C231C'}"></span>`;
 return `<span class="ebs">${b}</span><span style="color:${c}">${lab}</span>`};
const ACT=()=>D.issues.filter(i=>i.pillar!='Info'&&(i.gain_overall>0||i.severity=='bad'));
const AFFPAGES=()=>D.pages.filter(p=>p.checks.some(c=>c.status=='bad'||c.status=='warn')).length;
const gpos=i=>i.gain_overall>0?i.gain_overall:0;
function tgl(id){const e=document.getElementById(id);if(e)e.style.display=e.style.display=='block'?'none':'block'}
function bull(u,c){return `<span class="b"><span class="dotb" style="background:${c}"></span><a href="${esc(u)}" target="_blank">${rel(u)}</a></span>`}
function urlList(id,i){return `<div id="${id}" class="apurls">${[...i.bad.map(u=>bull(u,'#ff4d3d')),...i.warn.map(u=>bull(u,'#f2b53c'))].join('')||'<span class="qd">No affected pages.</span>'}</div>`}
function pdet(id){
 if(SITEIDS.has(id)){const sc=(D.site_checks||[]).find(c=>c.id==id)||{};const cl=sc.status=='good'?'ok':sc.status=='warn'?'wn':'er';return `<div id="pd_${id}" class="engdet"><span class="rst ${cl}">Site-wide check: ${sc.status||'n/a'}</span> <span class="qd">${esc(sc.detail||'')}</span></div>`;}
 const ok=D.pages.filter(p=>p.status==200), dcx=s=>s=='good'?'#3ecf8e':s=='warn'?'#f2b53c':'#ff4d3d';
 const rr=ok.map(p=>[p,p.cs[id]]).filter(x=>x[1]&&x[1]!='na'&&x[1]!='info');
 if(!rr.length)return `<div id="pd_${id}" class="engdet"><span class="qd">No applicable pages for this check.</span></div>`;
 const fl=rr.filter(x=>x[1]!='good'),ps=rr.filter(x=>x[1]=='good');
 const ln=x=>`<span class="egp"><span class="dotb" style="background:${dcx(x[1])}"></span><a href="${esc(x[0].url)}" target="_blank">${rel(x[0].url)}</a></span>`;
 return `<div id="pd_${id}" class="engdet">${fl.length?'<div class="egh">Failing here ('+fl.length+')</div>'+fl.map(ln).join(''):''}${ps.length?'<div class="egh"'+(fl.length?' style="margin-top:10px"':'')+'>Passing ('+ps.length+')</div>'+ps.map(ln).join(''):''}</div>`}

function plan(){
 const overall=D.overall, act=ACT();
 if(!act.length)return `<div class="dashnote"><span class="dotb" style="background:var(--ok)"></span>Nothing to fix — every scored check passes across the crawl.</div>`;
 const tg=act.reduce((a,i)=>a+gpos(i),0), proj=Math.min(100,overall+tg);
 const affS=new Set();act.forEach(i=>{i.bad.forEach(u=>affS.add(u));i.warn.forEach(u=>affS.add(u))});
 const edits=act.reduce((a,i)=>a+i.count,0), affp=affS.size;
 const biggest=act.filter(i=>i.gain_overall>=2);
 const polish=act.filter(i=>i.gain_overall<2&&i.effort=='Low');
 const worth=act.filter(i=>biggest.indexOf(i)<0&&polish.indexOf(i)<0);
 const gsum=a=>a.reduce((s,i)=>s+gpos(i),0);
 const bG=gsum(biggest),wG=gsum(worth),pG=gsum(polish),TG=(bG+wG+pG)||1;
 const ordered=[...biggest,...worth,...polish];
 window._ordered=ordered; window._rk={}; ordered.forEach((i,x)=>window._rk[i.id]=x+1);
 let h=`<div class="ap2"><div class="apmain">`;
 h+=`<div class="apsum" style="display:grid;grid-template-columns:auto 1fr;gap:34px;align-items:center">
   <div style="display:flex;align-items:center;gap:18px">
     <div><div class="apk">TODAY</div><div class="apbn" style="color:var(--muted)">${overall}</div></div>
     <div style="font-size:24px;color:#4e4945">&rarr;</div>
     <div><div class="apk" style="color:var(--grn)">ALL ${act.length} FIXES APPLIED</div>
       <div style="display:flex;align-items:baseline;gap:8px"><div class="apbn">${proj}<span style="font-size:18px;color:var(--muted);font-weight:800">/100</span></div><div style="font-size:14px;font-weight:700;color:var(--ok)">+${proj-overall} pts</div></div></div>
   </div>
   <div style="border-left:1px solid var(--line);padding-left:30px;display:flex;flex-direction:column;gap:11px">
     <div style="display:flex;justify-content:space-between;gap:12px;font-size:12px;color:var(--muted)"><span style="font-weight:600;letter-spacing:.08em">WHERE THE ${tg} POINTS COME FROM</span><span>${edits} page edits across ${affp} pages</span></div>
     <div style="display:flex;height:14px;border-radius:7px;overflow:hidden;background:#221d1a">
       <div style="width:${Math.round(100*bG/TG)}%;background:#ff4d00"></div>
       <div style="width:${Math.round(100*wG/TG)}%;background:#ff8a3d"></div>
       <div style="width:${Math.round(100*pG/TG)}%;background:#5a4034"></div></div>
     <div style="display:flex;gap:22px;font-size:12px;color:#9d9691;flex-wrap:wrap">
       ${[['#ff4d00','Biggest movers',biggest.length,bG],['#ff8a3d','Worth doing',worth.length,wG],['#5a4034','Polish',polish.length,pG]].map(a=>`<span style="display:flex;align-items:center;gap:7px"><span style="width:8px;height:8px;border-radius:2px;background:${a[0]}"></span>${a[1]}, ${a[2]} fixes <b style="color:#fff">+${a[3]}</b></span>`).join('')}</div>
   </div></div>`;
 h+=`<div class="apintro">Ranked by projected CITED Score gain if the fix is applied to every affected page. The projection re-scores the site after each fix, so gains do not double-count.</div>`;
 const tiers=[['#ff4d00','BIGGEST MOVERS',biggest,bG,'do these this month','apbox hot','big'],
              ['#ff8a3d','WORTH DOING',worth,wG,'structure for retrieval','apbox','mid'],
              ['#5a4034','POLISH',polish,pG,'low effort','apbox','sm']];
 tiers.forEach(t=>{const arr=t[2];if(!arr.length)return;
   h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="background:${t[0]}"></span><h3>${t[1]}</h3><span class="meta">${arr.length} fix${arr.length==1?'':'es'} &middot; +${t[3]} points &middot; ${t[4]}</span></div><div class="${t[5]}">`;
   arr.forEach(i=>h+=planFix(i,t[6],t[0]));
   h+=`</div></section>`;});
 h+=`</div><div class="apside">`+roadmapCard()+effortCard(act,edits)+`<button class="bigbtn" onclick="exportPlan()">Export plan as CSV</button></div></div>`;
 return h}
function planFix(i,size,accent){
 const r=window._rk[i.id];
 const eg=Object.entries(i.gain_engines||{}).filter(x=>x[1]>0).sort((a,b)=>b[1]-a[1]).slice(0,3);
 const gv=i.gain_overall>0?('+'+i.gain_overall):'—';
 const pdot=`<span class="dotb" style="background:${PDOT[i.pillar]||'#888'}"></span>`;
 const pages=`<span style="font-size:13px;color:#b7afaa">${i.count} page${i.count>1?'s':''}</span>`;
 const urls=pdet(i.id);
 const cols=`grid-template-columns:30px 1fr 108px 96px 60px 70px;cursor:pointer`;
 if(size=='sm'){
   return `<div class="apissue"><div class="aprow" style="${cols}" onclick="tgl('pd_${i.id}')">
     <span style="font-size:14px;font-weight:700;color:var(--muted)">${r}</span>
     <div style="display:flex;align-items:center;gap:12px;min-width:0"><span style="font-size:14px;font-weight:600;white-space:nowrap">${esc(i.label)} <span class="egcar">▾</span></span><span class="qd" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(i.ev)}</span></div>
     <span style="font-size:12px;color:#b7afaa">${pdot}${i.pillar}</span>
     <span style="font-size:12px">${EBARS(i.effort)}</span>${pages}
     <span class="apgain" style="font-size:16px;text-align:right;color:${i.gain_overall>0?'#b7afaa':'var(--muted)'}">${gv}</span>
   </div>${urls}</div>`}
 const big=size=='big';
 return `<div class="apissue"><div class="aprow" style="${cols}" onclick="tgl('pd_${i.id}')">
   <span style="font-size:${big?'20px':'17px'};font-weight:900;color:${accent}">${r}</span>
   <div style="display:flex;flex-direction:column;gap:5px;min-width:0">
     <div style="display:flex;align-items:center;gap:10px"><span style="font-size:${big?'15px':'14px'};font-weight:${big?'700':'600'}">${esc(i.label)} <span class="egcar">▾</span></span><span style="font-size:11px;color:#6f6864">${i.ch}${!big&&eg.length?' &middot; '+eg[0][0]+' +'+eg[0][1]:''}</span></div>
     <div class="qd" style="line-height:1.5">${esc(i.ev)}</div>
     ${big&&eg.length?`<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:1px">${eg.map(x=>`<span class="apchip">${x[0]} +${x[1]}</span>`).join('')}</div>`:''}
   </div>
   <span style="font-size:12px;color:#b7afaa">${pdot}${i.pillar}</span>
   <span style="font-size:12px">${EBARS(i.effort)}</span>${pages}
   <span class="apgain" style="font-size:${big?'26px':'20px'};text-align:right;color:${accent}">${gv}</span>
 </div>${urls}</div>`}
function roadmapCard(){
 const info={1:['Days 0–30 &middot; foundation','Quick technical wins, mostly template-level'],2:['Days 30–60 &middot; structure','Rewrite for retrieval, page by page'],3:['Days 60–90 &middot; polish','Small edits, then re-crawl and compare']};
 const acc={1:['#140b06','#ff4d00'],2:['#ff8a3d','#ff4d0029'],3:['#b7afaa','#ffffff14']};
 let cum=D.overall,out='';
 [1,2,3].forEach(ph=>{const ids=(D.plan_phases&&D.plan_phases[ph])||[];if(!ids.length)return;
   const items=ids.map(id=>D.issues.find(y=>y.id==id)).filter(Boolean);
   const g=items.reduce((a,i)=>a+gpos(i),0);cum=Math.min(100,cum+g);
   out+=`<div class="rmph"><div style="display:flex;align-items:flex-start;gap:12px">
     <span class="rmnum" style="color:${acc[ph][0]};background:${acc[ph][1]}">0${ph}</span>
     <div style="flex:1"><div style="font-size:14px;font-weight:700">${info[ph][0]}</div><div class="qd">${info[ph][1]}</div></div>
     <div style="text-align:right"><div style="font-size:20px;font-weight:900;font-family:'Figtree',sans-serif;letter-spacing:-.02em">${cum}</div><div style="font-size:11px;color:var(--ok)">+${g}</div></div></div>`;
   if(ph==3){out+=`<div class="qd" style="line-height:1.5;padding-left:4px;margin-top:8px">${items.slice(0,6).map(i=>esc(i.label.split('(')[0].split(',')[0].trim())).join(', ')} &mdash; ${items.reduce((a,i)=>a+i.count,0)} pages.</div>`}
   else{out+=`<div style="display:flex;flex-direction:column;gap:7px;font-size:12px;color:#b7afaa;padding-left:4px;margin-top:10px">${items.slice(0,6).map(i=>`<div style="display:flex;align-items:center;gap:9px"><span class="dotb" style="background:${PDOT[i.pillar]}"></span>${esc(i.label.split('(')[0].trim())}</div>`).join('')}</div>`}
   out+=`</div>`});
 return `<div class="rmc"><div class="rmch"><h3>90-Day Roadmap</h3><div class="qd" style="margin-top:4px">Sequenced so each phase makes the next one cheaper.</div></div>${out}</div>`}
function effortCard(act,edits){
 const bk={template:['Template-level fixes',0,0],copy:['Per-page copy work',0,0],meta:['One-off metadata edits',0,0]};
 act.forEach(i=>{const t=TTYPE[i.id]||'copy';bk[t][1]++;bk[t][2]+=gpos(i)});
 const tplN=bk.template[1], tplEdits=act.filter(i=>TTYPE[i.id]=='template').reduce((a,i)=>a+i.count,0);
 const rows=Object.keys(bk).filter(k=>bk[k][1]).map(k=>`<div class="rowsb"><span style="color:#b7afaa">${bk[k][0]}</span><span style="color:#fff;font-weight:600">${bk[k][1]} fix${bk[k][1]==1?'':'es'} &middot; +${bk[k][2]}</span></div>`).join('');
 const note=tplN?`Start with the ${tplN} template fixes: they touch ${tplEdits} of ${edits} page edits in one change.`:'';
 return `<div class="card2"><h3>Effort at a glance</h3>${rows}${note?`<div class="qd" style="line-height:1.5;border-top:1px solid var(--line);padding-top:11px;margin-top:6px">${note}</div>`:''}</div>`}
function exportPlan(){const rows=[['rank','fix','pillar','chapter','effort','pages','gain_overall',...ECOLS.map(e=>'gain_'+e.split(' ')[0])]];
 (window._ordered||D.issues).forEach((i,x)=>rows.push([x+1,i.label,i.pillar,i.ch,i.effort,i.count,i.gain_overall,...ECOLS.map(e=>(i.gain_engines||{})[e]||0)]));
 dl(`cited-score-${D.domain}-action-plan.csv`,rows)}

function ifilt(v){window._ifilter=v;render()}
function issuesView(){
 const iss=D.issues.filter(i=>i.pillar!='Info');
 if(!iss.length)return `<div class="dashnote"><span class="dotb" style="background:var(--ok)"></span>No issues — every scored check passes across the crawl.</div>`;
 const errs=iss.filter(i=>i.severity=='bad'), warns=iss.filter(i=>i.severity=='warn');
 const tg=iss.reduce((a,i)=>a+gpos(i),0);
 const totalChecks=Object.keys(D.check_meta).filter(id=>D.check_meta[id].pillar!='Info').length;
 const passed=Math.max(0,totalChecks-iss.length);
 const f=window._ifilter||'all';
 const show=i=>f=='all'||(f=='error'&&i.severity=='bad')||(f=='warn'&&i.severity=='warn');
 const P=D.pages_crawled;
 const weakest=['Known','Findable','Trusted'].reduce((a,b)=>D.pillars[b]<D.pillars[a]?b:a);
 let h=`<div class="ap2"><div class="apmain">`;
 const proj=Math.min(100,D.overall+tg);
 const istat=(label,num,sub)=>`<div class="istat"><div class="apk">${label}</div><div class="inumwrap">${num}</div><div class="isub">${sub}</div></div>`;
 h+=`<div class="issum">
   ${istat('CHECKS FAILING',`<span class="inum">${iss.length}</span>`,`of ${totalChecks} checks`)}
   <div class="vr"></div>
   ${istat('ERRORS',`<span class="dotb" style="margin:0;background:var(--err2)"></span><span class="inum" style="color:var(--err2)">${errs.length}</span>`,'blocking citation')}
   ${istat('WARNINGS',`<span class="dotb" style="margin:0;background:var(--warn2)"></span><span class="inum" style="color:var(--warn2)">${warns.length}</span>`,'weakening it')}
   <div class="vr"></div>
   ${istat('IF ALL FIXED',`<span class="inum" style="color:var(--ok)">+${proj-D.overall}</span>`,`&rarr; ${proj}/100`)}
   <div style="flex:1"></div>
   <div style="display:flex;gap:8px">
     <button class="fpill ${f=='all'?'on':''}" onclick="ifilt('all')">All ${iss.length}</button>
     <button class="fpill ${f=='error'?'on':''}" onclick="ifilt('error')">Errors ${errs.length}</button>
     <button class="fpill ${f=='warn'?'on':''}" onclick="ifilt('warn')">Warnings ${warns.length}</button>
   </div></div>`;
 const QL={Known:'KNOWN &mdash; DO THEY KNOW YOU?',Findable:'FINDABLE &mdash; CAN THEY FIND YOUR ANSWER?',Trusted:'TRUSTED &mdash; DO THEY TRUST YOU?'};
 ['Known','Findable','Trusted'].forEach(p=>{
   const gs=iss.filter(i=>i.pillar==p&&show(i));if(!gs.length)return;
   const avail=iss.filter(i=>i.pillar==p).reduce((a,i)=>a+gpos(i),0);
   const failing=iss.filter(i=>i.pillar==p).length;
   const bc=p=='Findable'?'#f2b53c33':'#ffffff14';
   h+=`<section class="aptier"><div class="aptierh"><span class="dotb" style="width:9px;height:9px;margin:0;background:${PDOT[p]}"></span><h3>${QL[p]}</h3><span class="meta">score ${D.pillars[p]} &middot; ${failing} check${failing>1?'s':''} failing &middot; +${avail} available${p==weakest?' &middot; weakest pillar':''}</span></div><div class="apbox" style="border-color:${bc}">`;
   gs.forEach(i=>h+=issueRow(i,P));
   h+=`</div></section>`});
 h+=`</div><div class="apside">`+worstPagesCard()+oneChangeCard(errs,iss)+`<div class="dashnote"><span class="dotb" style="background:var(--ok)"></span>${passed} checks passed on every page &mdash; not listed here.</div></div></div>`;
 return h}
function issueRow(i,P){
 const bad=i.severity=='bad', dc=bad?'#ff4d3d':'#f2b53c';
 const w=Math.min(100,Math.round(100*i.count/(P||1)));
 const gv=i.gain_overall>0?('+'+i.gain_overall):'—', gc=i.gain_overall>0?'var(--ok)':'var(--muted)';
 let body;
 if(bad){body=`<div style="display:flex;flex-direction:column;gap:5px;min-width:0">
   <div style="display:flex;align-items:center;gap:10px"><span class="dotb" style="background:${dc}"></span><span style="font-size:15px;font-weight:700">${esc(i.label)} <span class="egcar">▾</span></span><span style="font-size:11px;color:#6f6864">${i.ch}</span></div>
   <div class="qd" style="line-height:1.5;padding-left:17px">${esc(i.ev)}</div>
   <div style="font-size:12px;color:#c9c2bd;line-height:1.5;padding-left:17px"><span style="color:#fff;font-weight:600">Fix</span> ${esc(i.fix)}</div></div>`}
 else{body=`<div style="display:flex;flex-direction:column;gap:4px;min-width:0">
   <div style="display:flex;align-items:center;gap:10px"><span class="dotb" style="background:${dc}"></span><span style="font-size:14px;font-weight:600">${esc(i.label)} <span class="egcar">▾</span></span><span style="font-size:11px;color:#6f6864">${i.ch}</span></div>
   <div class="qd" style="line-height:1.5;padding-left:17px"><span style="font-weight:600">Fix</span> ${esc(i.fix||i.ev)}</div></div>`}
 return `<div class="apissue"><div class="aprow" style="grid-template-columns:1fr 210px 66px;gap:20px;cursor:pointer" onclick="tgl('pd_${i.id}')">
   ${body}
   <div style="display:flex;flex-direction:column;gap:6px"><div style="height:8px;border-radius:4px;background:#221d1a;overflow:hidden"><div style="width:${w}%;height:100%;background:${dc}"></div></div><div style="font-size:11px;color:#9d9691">${i.count} of ${P} pages affected</div></div>
   <span style="font-size:13px;font-weight:700;color:${gc};text-align:right">${gv}</span>
 </div>${pdet(i.id)}</div>`}
function worstPagesCard(){
 const rows=D.pages.map(p=>({p,f:p.checks.filter(c=>c.status=='bad'||c.status=='warn').length,e:p.checks.filter(c=>c.status=='bad').length})).filter(x=>x.f>0).sort((a,b)=>b.f-a.f||a.p.score-b.p.score).slice(0,5);
 if(!rows.length)return '';
 const out=rows.map(x=>`<div class="wpage" onclick="go('Pages')"><span class="s" style="color:${bcol(x.p.score)}">${x.p.score}</span><span style="flex:1;min-width:0;display:flex;flex-direction:column;gap:2px"><span style="font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${rel(x.p.url)}</span><span class="qd">${x.f} checks failing &middot; ${x.e} error${x.e==1?'':'s'}</span></span></div>`).join('');
 return `<div class="rmc"><div class="rmch" style="display:flex;justify-content:space-between;align-items:baseline"><h3>Worst Pages</h3><span class="qd">by failing checks</span></div>${out}</div>`}
function oneChangeCard(errs,iss){
 const tplErr=errs.filter(i=>TTYPE[i.id]=='template');
 const totInst=iss.reduce((a,i)=>a+i.count,0);
 const tplInst=iss.filter(i=>TTYPE[i.id]=='template').reduce((a,i)=>a+i.count,0);
 const top=iss.filter(i=>TTYPE[i.id]=='template').sort((a,b)=>b.count-a.count).slice(0,3);
 const lead=tplErr.length?`${tplErr.length} of the ${errs.length} error${errs.length==1?'':'s'} ${tplErr.length==1?'is':'are'} template-level. Fixing the page template clears ${tplInst} of the ${totInst} affected page instances without touching copy.`:`Most fixes here are per-page copy work &mdash; work through the action plan in priority order.`;
 const rows=top.map(i=>`<div class="rowsb"><span style="color:#b7afaa">${esc(i.label.split('(')[0].trim())}</span><span style="color:#fff;font-weight:600">${i.count} page${i.count>1?'s':''}</span></div>`).join('');
 return `<div class="card2"><h3>One Change, Most Pages</h3><div class="qd" style="line-height:1.6">${lead}</div>${rows}<button class="bigbtn" style="border-radius:6px;margin-top:2px" onclick="go('Action Plan')">Open the action plan</button></div>`}
function sc(k){if(sortk==k)sortd*=-1;else{sortk=k;sortd=1}render()}
const PBAND=v=>v<50?['rgba(255,77,61,.22)','#ff9c88']:v<70?['rgba(242,181,60,.18)','#f2c574']:v<85?['rgba(62,207,142,.14)','#8fe0b8']:['rgba(62,207,142,.28)','#b6f0d4'];
const EABBR={'ChatGPT':'GPT','Perplexity':'PPLX','AI Overviews':'AIO','Gemini':'GEM','Copilot':'CPLT','Claude':'CLDE'};
function pgVal(p,k){if(k=='url')return p.path||p.url;if(k=='Known'||k=='Findable'||k=='Trusted')return p.pillars[k];if(ECOLS.indexOf(k)>=0)return p.engines[k];if(k=='fetch_ms')return p.fetch_ms;return p.score}
function pgFiltered(){const q=(window._pq||'').toLowerCase(),ty=window._ptype||'all',bel=window._pbelow;
 let ps=D.pages.filter(p=>(ty=='all'||p.type==ty)&&(!bel||p.score<70)&&(!q||p.url.toLowerCase().includes(q)));
 const k=sortk||'score';ps.sort((a,b)=>{let x=pgVal(a,k),y=pgVal(b,k);if(typeof x=='string')return(x>y?1:x<y?-1:0)*sortd;return(x-y)*sortd});
 return ps}
function pageRow(p){
 const[sbg,sfg]=PBAND(p.score);
 const fails=p.checks.filter(c=>c.status=='bad'||c.status=='warn');
 const nb=p.checks.filter(c=>c.status=='bad').length;
 const cc=nb?['#ff4d3d','rgba(255,77,61,.14)']:fails.length?['#f2b53c','rgba(242,181,60,.14)']:['#3ecf8e','rgba(62,207,142,.14)'];
 const labels=[...fails.filter(c=>c.status=='bad'),...fails.filter(c=>c.status=='warn')].map(c=>esc(c.label)).join(', ');
 const pill=v=>`<span style="font-size:12px;text-align:center;color:${v?'#b7afaa':'#ff8f6b'}">${v}</span>`;
 const ecell=v=>{const[b,f]=PBAND(v);return `<span class="ecell" style="background:${b};color:${f}">${v}</span>`};
 return `<div class="pgcols pgrow">
  <span class="schip" style="background:${sbg};color:${p.score<70?'#ffb3a1':sfg}">${p.score}</span>
  <span style="display:flex;align-items:center;gap:9px;min-width:0"><a href="${esc(p.url)}" target="_blank">${rel(p.url)}</a><span class="tybadge">${esc(p.type)}</span></span>
  ${pill(p.pillars.Known)}${pill(p.pillars.Findable)}${pill(p.pillars.Trusted)}
  ${ECOLS.map(e=>ecell(p.engines[e])).join('')}
  <span style="font-size:11px;text-align:right;color:#8b8480">${(p.fetch_ms/1000).toFixed(1)}s</span>
  <span style="display:flex;align-items:center;gap:8px;min-width:0"><span style="font-size:11px;font-weight:700;color:${cc[0]};background:${cc[1]};padding:2px 7px;border-radius:4px;flex:none">${fails.length}</span><span style="font-size:11px;color:${fails.length?'#8b8480':'#6f6864'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${labels||'all checks pass'}</span></span>
 </div>`}
function pgPillsHTML(){const ty=window._ptype||'all',bel=window._pbelow;
 const order=['page','article','listing','product','home'];const types=Object.keys(D.types).sort((a,b)=>{let i=order.indexOf(a),j=order.indexOf(b);return(i<0?9:i)-(j<0?9:j)});
 let h=`<button class="pgpill ${ty=='all'?'on':''}" onclick="window._ptype='all';pgRefresh()">All ${D.pages.length}</button>`;
 h+=types.map(t=>`<button class="pgpill ${ty==t?'on':''}" onclick="window._ptype='${t}';pgRefresh()">${esc(t)} ${D.types[t]}</button>`).join('');
 h+=`<button class="pgpill bel ${bel?'on':''}" onclick="window._pbelow=!window._pbelow;pgRefresh()">Below 70 only</button>`;
 return h}
function pgRefresh(){const ps=pgFiltered();
 const rb=document.getElementById('pgrows');if(rb)rb.innerHTML=ps.map(pageRow).join('')||'<div class="pgrow" style="color:var(--muted);grid-template-columns:1fr">No pages match.</div>';
 const c=document.getElementById('pgcount');if(c)c.textContent=ps.length+(ps.length==1?' page':' pages');
 const pl=document.getElementById('pgpills');if(pl)pl.innerHTML=pgPillsHTML()}
function pgsort(k){if(sortk==k)sortd*=-1;else{sortk=k;sortd=(k=='url')?1:1}pgRefresh();
 const hd=document.getElementById('pghead');if(hd)hd.querySelectorAll('span[data-s]').forEach(s=>{s.innerHTML=s.dataset.lab+(s.dataset.s==k?(sortd>0?' ↑':' ↓'):'')})}
function pagesView(){
 const scores=D.pages.map(p=>p.score).sort((a,b)=>a-b),n=scores.length;
 const median=n?(n%2?scores[(n-1)/2]:Math.round((scores[n/2-1]+scores[n/2])/2)):0;
 const dist=[0,0,0,0];D.pages.forEach(p=>{const s=p.score;dist[s<70?0:s<80?1:s<90?2:3]++});
 const clear=D.pages.filter(p=>p.score>=70).length,maxd=Math.max(...dist,1);
 const dcol=['#ff4d3d','#f2b53c','#3ecf8e','#2f9c6c'],dnum=['#ff8f6b','#f2c574','#7fdcae','#7fdcae'],dlab=['under 70','70–79','80–89','90+'];
 const wt={};ECOLS.forEach(e=>wt[e]=0);D.pages.forEach(p=>{let mn=1e9,me=null;ECOLS.forEach(e=>{if(p.engines[e]<mn){mn=p.engines[e];me=e}});if(me)wt[me]++});
 const ws=Object.entries(wt).filter(x=>x[1]>0).sort((a,b)=>b[1]-a[1]).slice(0,4),wmax=Math.max(...ws.map(x=>x[1]),1),wc=['#ff5b2e','#ff8a3d','#f2b53c','#c98f2e'];
 if(!sortk)sortk='score';
 let h=`<div style="display:flex;flex-direction:column;gap:18px">`;
 // summary
 h+=`<div class="pgsum">
   <div><div class="apk">MEDIAN PAGE</div><div style="display:flex;align-items:baseline;gap:9px"><span style="font-size:40px;font-weight:900;line-height:1;font-family:'Figtree',sans-serif;letter-spacing:-.02em">${median}</span><span class="qd">quotable at 70</span></div></div>
   <div class="vr"></div>
   <div style="flex:1;min-width:280px;display:flex;flex-direction:column;gap:9px">
     <div style="display:flex;justify-content:space-between;align-items:baseline"><span class="apk">SCORE DISTRIBUTION</span><span class="qd">${clear} of ${D.pages.length} pages clear 70</span></div>
     <div class="pgdist">${dist.map((d,i)=>`<div class="col"><span style="font-size:11px;color:${dnum[i]};text-align:center">${d}</span><div class="trk"><i style="height:${Math.round(100*d/maxd)}%;background:${dcol[i]}"></i></div><span style="font-size:10px;color:#6f6864;text-align:center">${dlab[i]}</span></div>`).join('')}</div>
   </div>
   <div class="vr"></div>
   <div style="width:260px;display:flex;flex-direction:column;gap:9px">
     <div class="apk">WEAKEST ENGINE PER PAGE</div>
     <div style="display:flex;flex-direction:column;gap:7px;font-size:12px;color:#b7afaa">${ws.map((x,i)=>`<div style="display:flex;align-items:center;gap:9px"><span style="width:92px">${x[0]}</span><span style="flex:1;height:7px;border-radius:4px;background:#221d1a;overflow:hidden"><span style="display:block;width:${Math.round(100*x[1]/wmax)}%;height:100%;background:${wc[i]}"></span></span><span style="color:#fff;font-weight:600">${x[1]}</span></div>`).join('')}</div>
   </div></div>`;
 // filter bar
 h+=`<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
   <span class="pgsearch"><span style="color:#6f6864">&#9906;</span><input placeholder="Filter by URL" oninput="window._pq=this.value;pgRefresh()" value="${esc(window._pq||'')}"></span>
   <div id="pgpills" style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">${pgPillsHTML()}</div>
   <div style="flex:1"></div><span id="pgcount" class="qd">${D.pages.length} pages</span></div>`;
 // table
 const HCELL=(k,lab,al)=>`<span data-s="${k}" data-lab="${lab}" onclick="pgsort('${k}')" style="text-align:${al||'left'}">${lab}${sortk==k?(sortd>0?' ↑':' ↓'):''}</span>`;
 h+=`<div class="pgscroll"><div class="pgtbl">
   <div id="pghead" class="pgcols pghead">${HCELL('score','SCORE')}${HCELL('url','URL')}
     <span title="Known" style="text-align:center;color:#6f9dff">KN</span><span title="Findable" style="text-align:center;color:#f2b53c">FI</span><span title="Trusted" style="text-align:center;color:#3ecf8e">TR</span>
     ${ECOLS.map(e=>HCELL(e,EABBR[e],'center')).join('')}
     ${HCELL('fetch_ms','LOAD','right')}<span>FAILING CHECKS</span></div>
   <div id="pgrows">${pgFiltered().map(pageRow).join('')}</div>
   <div class="pgleg">
     <span style="display:flex;align-items:center;gap:7px"><span style="width:11px;height:11px;border-radius:3px;background:rgba(255,77,61,.22)"></span>under 50</span>
     <span style="display:flex;align-items:center;gap:7px"><span style="width:11px;height:11px;border-radius:3px;background:rgba(242,181,60,.18)"></span>50–69</span>
     <span style="display:flex;align-items:center;gap:7px"><span style="width:11px;height:11px;border-radius:3px;background:rgba(62,207,142,.14)"></span>70–84</span>
     <span style="display:flex;align-items:center;gap:7px"><span style="width:11px;height:11px;border-radius:3px;background:rgba(62,207,142,.28)"></span>85+</span>
     <span style="flex:1"></span><span>Kn / Fi / Tr are the three pillars. Click a column header to sort.</span></div>
 </div></div>`;
 h+=`</div>`;
 return h}
const SITEIDS=new Set(['robots','llms','sitemap','reachability','comparison']);
const SHORT={parity:'Schema in JS',answerfirst:'no opener',definitional:'no definition',readability:'hard to read',entitydensity:'few entities',sections:'walls of text',schema:'Article schema',wordcount:'thin content',freshness:'stale',qheadings:'H2s',faq:'no FAQ',liststables:'no tables',meta:'meta desc',title:'title',alt:'alt text',citations:'few sources',internal:'few links',statdensity:'few stats',canonical:'canonical',h1:'H1',robots:'bot blocked',sitemap:'no sitemap',reachability:'blocked',entity:'no entity',schemacomplete:'thin schema',author:'no author',sourced:'unsourced stats',video:'no video',comparison:'no comparison'};
const EBOTS={'ChatGPT':['GPTBot','OAI-SearchBot'],'Perplexity':['PerplexityBot'],'AI Overviews':['Googlebot','Google-Extended'],'Gemini':['Google-Extended'],'Copilot':['Bingbot'],'Claude':['ClaudeBot','anthropic-ai']};
const EOWNER={'ChatGPT':'OpenAI','Perplexity':'Perplexity','AI Overviews':'Google','Gemini':'Google','Copilot':'Microsoft','Claude':'Anthropic'};
const ORD=['','strongest','second-strongest','third-strongest','fourth-strongest','fifth-strongest','sixth-strongest'];
function engine(e){
 const ws=D.engine_weights[e], ok=D.pages.filter(p=>p.status==200), P=ok.length, TH=70;
 const IM={};D.issues.forEach(i=>IM[i.id]=i);
 const sig=Object.keys(ws).map(id=>{let good,total,pr;
   if(SITEIDS.has(id)){const s=D.site_checks.find(c=>c.id==id)||{},g=s.status=='good';pr=g?100:s.status=='warn'?50:0;good=g?P:0;total=P}
   else{const r=ok.filter(p=>{const s=p.cs[id];return s&&s!='na'&&s!='info'});good=r.filter(p=>p.cs[id]=='good').length;total=r.length;pr=total?Math.round(100*good/total):100}
   return {id,lab:(D.check_meta[id]||{}).label||id,ev:(D.check_meta[id]||{}).ev||'',w:ws[id],pr,good,total,lift:IM[id]?(IM[id].gain_engines[e]||0):0}});
 const strong=sig.filter(s=>s.pr>=90).sort((a,b)=>b.w-a.w);
 const high=sig.filter(s=>s.pr<90&&s.w>=2).sort((a,b)=>b.lift-a.lift||b.w-a.w);
 const low=sig.filter(s=>s.pr<90&&s.w<2).sort((a,b)=>b.lift-a.lift);
 const score=D.engines[e], belowFull=sig.filter(s=>s.pr<100).length;
 const totLift=high.concat(low).reduce((a,s)=>a+(s.lift>0?s.lift:0),0);
 const pagesBelow=ok.filter(p=>p.engines[e]<TH).length, gap=TH-score;
 const rank=1+new Set(Object.values(D.engines).filter(v=>v>score)).size;
 const best=Object.entries(D.engines).sort((a,b)=>b[1]-a[1])[0];
 const topFix=high[0]||low[0];
 const hint=gap>0?(topFix?(TTYPE[topFix.id]=='template'?'One template fix clears most of the gap.':`The top fix adds +${topFix.lift}.`):''):'Already past the quotable threshold.';
 const WD=(w,c)=>`<span class="wdots" style="color:${c}">${'●'.repeat(w)}<span style="color:#3a3227">${'●'.repeat(Math.max(0,3-w))}</span></span>`;
 const barC=pr=>pr>=90?'#3ecf8e':pr>=40?'#f2b53c':'#ff4d3d';
 const engDetail=id=>{
   if(SITEIDS.has(id)){const sc=(D.site_checks||[]).find(c=>c.id==id)||{};const cl=sc.status=='good'?'ok':sc.status=='warn'?'wn':'er';return `<div id="eg_${id}" class="engdet"><span class="rst ${cl}">Site-wide check: ${sc.status||'n/a'}</span> <span class="qd">${esc(sc.detail||'')}</span></div>`;}
   const dcx=s=>s=='good'?'#3ecf8e':s=='warn'?'#f2b53c':'#ff4d3d';
   const rr=ok.map(p=>[p,p.cs[id]]).filter(x=>x[1]&&x[1]!='na'&&x[1]!='info');
   if(!rr.length)return `<div id="eg_${id}" class="engdet"><span class="qd">No applicable pages for this signal.</span></div>`;
   const fl=rr.filter(x=>x[1]!='good'),ps=rr.filter(x=>x[1]=='good');
   const ln=x=>`<span class="egp"><span class="dotb" style="background:${dcx(x[1])}"></span><a href="${esc(x[0].url)}" target="_blank">${rel(x[0].url)}</a></span>`;
   return `<div id="eg_${id}" class="engdet">${fl.length?'<div class="egh">Failing here ('+fl.length+')</div>'+fl.map(ln).join(''):''}${ps.length?'<div class="egh"'+(fl.length?' style="margin-top:10px"':'')+'>Passing ('+ps.length+')</div>'+ps.map(ln).join(''):''}</div>`;};
 const esig=(s,hi)=>{const dcol=s.w>=3?'#FF4D00':s.w==2?'#ff8a3d':'#8b8480';const lc=hi?(s.lift>=5?'#FF4D00':'#ff8a3d'):'#b7afaa';
   const bar=`<div style="display:flex;flex-direction:column;gap:5px"><div class="engbar"><i style="width:${Math.max(s.pr,2)}%;background:${barC(s.pr)}"></i></div><span style="font-size:11px;color:${s.pr==0?'#ff9c88':'#9d9691'}">${s.pr}% pass · ${s.good} of ${s.total} pages</span></div>`;
   const lift=`<span style="font-size:${hi?'20px':'16px'};font-weight:${hi?'800':'700'};color:${lc};text-align:right">${s.lift>0?'+'+s.lift:'—'}</span>`;
   const lab=hi?`<div style="display:flex;flex-direction:column;gap:4px;min-width:0"><span style="font-size:15px;font-weight:700">${esc(s.lab)} <span class="egcar">▾</span></span><span class="qd" style="line-height:1.5">${esc(s.ev)}</span></div>`
             :`<div style="display:flex;align-items:center;gap:12px;min-width:0"><span style="font-size:14px;font-weight:600;white-space:nowrap">${esc(s.lab)} <span class="egcar">▾</span></span><span class="qd" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.ev)}</span></div>`;
   return `<div class="engcols engrow" onclick="tgl('eg_${s.id}')" title="Show pages">${lab}${WD(s.w,dcol)}${bar}${lift}</div>${engDetail(s.id)}`};
 const estrong=s=>`<div><div class="engcols engrow" onclick="tgl('eg_${s.id}')" title="Show pages" style="padding:2px 0;border:0"><span style="font-size:13px;color:#c9c2bd">${esc(s.lab)} <span class="egcar">▾</span></span>${WD(s.w,'#3ecf8e')}<div style="display:flex;align-items:center;gap:10px"><div class="engbar" style="height:7px;flex:1"><i style="width:${s.pr}%;background:#3ecf8e"></i></div><span style="font-size:11px;color:${s.pr>=100?'#3ecf8e':'#7fdcae'};width:74px;flex:none">${s.pr}% · ${s.good}/${s.total}</span></div><span style="text-align:right;color:#6f6864">—</span></div>${engDetail(s.id)}</div>`;
 const secH=(c,name,sub)=>`<div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:${c}"></span><h3>${name}</h3><span class="meta">${sub}</span></div>`;
 let h=`<div class="ap2"><div class="apmain">`;
 h+=`<div class="apsum" style="display:grid;grid-template-columns:auto 1fr;gap:34px;align-items:center;padding:26px 28px">
   <div style="display:flex;align-items:center;gap:22px">
     <div class="sring" style="--p:${score}"><i><span class="v">${score}</span><span class="o">OF 100</span></i></div>
     <div style="display:flex;flex-direction:column;gap:9px">
       <div class="htitle" style="font-size:20px;margin:0">${e} READINESS</div>
       <div style="display:flex;align-items:center;gap:8px"><span style="font-size:12px;font-weight:600;color:${gap>0?'#f2c574':'#3ecf8e'};background:${gap>0?'rgba(242,181,60,.14)':'rgba(62,207,142,.14)'};border:1px solid ${gap>0?'rgba(242,181,60,.3)':'rgba(62,207,142,.3)'};padding:4px 9px;border-radius:999px">${gap>0?gap+' pts to quotable':'clears 70 · quotable'}</span><span class="qd">threshold ${TH}</span></div>
       <div class="qd" style="line-height:1.5;max-width:230px">Your ${ORD[rank]||'lower-ranked'} engine. ${hint}</div>
     </div>
   </div>
   <div style="border-left:1px solid var(--line);padding-left:32px;display:flex;flex-direction:column;gap:14px">
     <div class="apk">HOW THIS ENGINE DECIDES</div>
     <div style="font-size:14px;line-height:1.6;color:#c9c2bd;max-width:640px;min-height:67px">${esc(D.engine_note[e])}</div>
     <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;padding-top:2px">
       <div><div style="font-size:22px;font-weight:800">${belowFull}</div><div class="qd">of ${sig.length} signals below full pass</div></div>
       <div><div style="font-size:22px;font-weight:800;color:var(--ok)">+${totLift}</div><div class="qd">points available</div></div>
       <div><div style="font-size:22px;font-weight:800">${pagesBelow}</div><div class="qd">pages below the threshold</div></div>
     </div>
   </div></div>`;
 if(high.length)h+=`<section class="aptier">${secH('#ff4d00','HIGH WEIGHT, LOW PASS RATE','where '+e+' is costing you the most — fix in this order')}<div class="apbox hot" style="padding:0 24px"><div class="engcols enghead"><span>SIGNAL</span><span>WEIGHT</span><span>SITE PASS RATE</span><span style="text-align:right">LIFT</span></div>${high.map(s=>esig(s,1)).join('')}</div></section>`;
 if(low.length)h+=`<section class="aptier">${secH('#f2b53c','LOWER WEIGHT, WORTH TIDYING',low.length+' signal'+(low.length>1?'s':'')+' · +'+low.reduce((a,s)=>a+(s.lift>0?s.lift:0),0)+' between them')}<div class="apbox" style="padding:2px 24px 6px">${low.map(s=>esig(s,0)).join('')}</div></section>`;
 if(strong.length)h+=`<section class="aptier">${secH('#3ecf8e','ALREADY STRONG',strong.length+' signal'+(strong.length>1?'s':'')+' · protect these when you edit')}<div class="apbox" style="padding:16px 24px;display:flex;flex-direction:column;gap:12px">${strong.map(estrong).join('')}</div></section>`;
 const worst=[...ok].sort((a,b)=>a.engines[e]-b.engines[e]),w8=worst.slice(0,8);
 h+=`<section class="aptier"><div class="aptierh" style="justify-content:space-between"><div style="display:flex;align-items:center;gap:10px"><h3>WORST PAGES FOR ${e}</h3><span class="meta">${pagesBelow} of ${P} below ${TH} · showing the ${w8.length} weakest</span></div><span onclick="go('Pages')" style="font-size:12px;font-weight:600;color:var(--grn);cursor:pointer">See all ${P} pages →</span></div>
   <div style="background:var(--panel2);border:1px solid var(--line);border-radius:14px;overflow:hidden">
   <div class="engwcols engwhead"><span style="text-align:center">${EABBR[e]||e}</span><span>URL</span><span>WHAT IT FAILS HERE</span><span style="text-align:center">OVERALL</span><span style="text-align:right">VS ${TH}</span></div>
   ${w8.map(p=>{const b=PBAND(p.engines[e]),wf=p.checks.filter(c=>(c.status=='bad'||c.status=='warn')&&ws[c.id]).map(c=>SHORT[c.id]||c.label).slice(0,4).join(', ')||'—',d=p.engines[e]-TH,dc=d>=0?'#8b8480':d<=-10?'#ff9c88':'#f2c574';
     return `<div class="engwcols engwrow"><span class="schip" style="background:${b[0]};color:${b[1]}">${p.engines[e]}</span><a href="${esc(p.url)}" target="_blank" style="font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${rel(p.url)}</a><span style="font-size:11px;color:#8b8480;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(wf)}</span><span style="font-size:13px;color:#b7afaa;text-align:center">${p.score}</span><span style="font-size:12px;font-weight:700;color:${dc};text-align:right">${d>=0?'+'+d:'−'+Math.abs(d)}</span></div>`}).join('')}</div></section>`;
 h+=`</div>`;
 // sidebar
 const eranked=Object.entries(D.engines).sort((a,b)=>b[1]-a[1]);
 const acr=eranked.map(x=>`<div style="display:flex;align-items:center;gap:10px"><span style="width:82px;color:${x[0]==e?'#fff':'#b7afaa'};font-weight:${x[0]==e?'700':'400'}">${x[0]}</span><span style="flex:1;height:8px;border-radius:4px;background:#221d1a;overflow:hidden"><span style="display:block;width:${x[1]}%;height:100%;background:${x[0]==e?'#FF4D00':'#3a3227'}"></span></span><span style="width:24px;text-align:right;color:${x[0]==e?'#fff':'#b7afaa'};font-weight:${x[0]==e?'700':'400'}">${x[1]}</span></div>`).join('');
 const bnote=e==best[0]?`Same pages, different weightings. ${e} is your strongest surface — protect it as you edit.`:`Same pages, different weightings. ${e} runs ${best[1]-score} point${best[1]-score==1?'':'s'} behind ${best[0]}, your strongest.`;
 const fixes=high.concat(low).filter(s=>s.lift>0).slice(0,3);
 const doHtml=fixes.length?fixes.map((s,i)=>{const iss=IM[s.id]||{},title=(iss.fix||s.lab).split(' - ')[0].split('. ')[0].trim(),cnt=iss.count||(s.total-s.good),nb=i==0?'color:#140b06;background:#FF4D00':'color:#ff8f6b;background:rgba(255,77,0,.2)';
   return `<div style="display:flex;align-items:flex-start;gap:12px"><span class="numbadge" style="${nb}">${i+1}</span><div style="display:flex;flex-direction:column;gap:3px"><span style="font-size:13px;font-weight:600">${esc(title)}</span><span class="qd" style="line-height:1.5">${cnt} page${cnt==1?'':'s'} · +${s.lift} ${e}</span></div></div>`}).join(''):`<div class="qd">No fixes needed — ${e} passes every weighted signal.</div>`;
 const reach=(D.site_checks.find(c=>c.id=='reachability')||{}).status||'good';
 const rT=reach=='good'?['#3ecf8e','● allowed · '+P+'/'+P]:reach=='warn'?['#f2b53c','● partial']:['#ff9c88','● blocked'];
 const parityBad=ok.filter(p=>p.cs.parity=='bad'||p.cs.parity=='warn').length;
 const bots=(EBOTS[e]||[]).map(b=>`<div style="display:flex;justify-content:space-between;font-size:12px"><span style="color:#b7afaa">${b}</span><span style="color:${rT[0]}">${rT[1]}</span></div>`).join('');
 const caNote=reach=='bad'?'Bots are blocked at the WAF — unblock them first.':parityBad?'Access is fine. The problem is what the crawler can read once it arrives.':'Access and rendering both look clean.';
 h+=`<div class="apside">
   <div class="card2"><div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px"><h3 style="margin:0">Across Your Engines</h3><span class="qd">site level</span></div><div style="display:flex;flex-direction:column;gap:10px;font-size:12px">${acr}</div><div class="qd" style="line-height:1.5;border-top:1px solid var(--line);padding-top:11px;margin-top:12px">${bnote}</div></div>
   <div class="card2 hot"><h3>Do this for ${e}</h3><div style="display:flex;flex-direction:column;gap:13px">${doHtml}</div>${fixes.length?`<button class="bigbtn" style="border-radius:6px;margin-top:6px" onclick="go('Action Plan')">Open the action plan</button>`:''}</div>
   <div class="card2"><h3>Crawler Access</h3><div style="display:flex;flex-direction:column;gap:11px">${bots}<div style="display:flex;justify-content:space-between;font-size:12px"><span style="color:#b7afaa">Server-rendered schema</span><span style="color:${parityBad?'#ff9c88':'#3ecf8e'}">● ${parityBad?parityBad+' pages JS-only':'all '+P+' server-rendered'}</span></div></div><div class="qd" style="line-height:1.5;border-top:1px solid var(--line);padding-top:11px;margin-top:2px">${caNote}</div></div>
 </div></div>`;
 return h}
function structure(){
 const byDir={},byDepth={};D.pages.forEach(p=>{const seg=(p.path||'/').split('/').filter(Boolean)[0]||'(root)';(byDir[seg]=byDir[seg]||[]).push(p);(byDepth[p.depth]=byDepth[p.depth]||[]).push(p)});
 const secs=Object.entries(byDir).sort((a,b)=>b[1].length-a[1].length),maxc=Math.max(...secs.map(s=>s[1].length),1);
 const avg=ps=>Math.round(ps.reduce((a,p)=>a+p.score,0)/ps.length);
 const ss=D.pages.map(p=>p.score).sort((a,b)=>a-b),med=ss[Math.floor(ss.length/2)];
 const cards=[['Pages',D.pages.length],['Sections',secs.length],['Max depth',Math.max(...D.pages.map(p=>p.depth))],['Median score',med]];
 let h=`<div style="display:flex;flex-direction:column;gap:18px">`;
 h+=`<div class="statgrid">${cards.map(c=>`<div class="statcard"><div class="n">${c[1]}</div><div class="l">${c[0]}</div></div>`).join('')}</div>`;
 const stpage=p=>{const b=PBAND(p.score);
   const bad=p.checks.filter(c=>c.status=='bad'),warn=p.checks.filter(c=>c.status=='warn');
   const sum=[bad.length?bad.length+' error'+(bad.length>1?'s':''):'',warn.length?warn.length+' warning'+(warn.length>1?'s':''):''].filter(Boolean).join(' · ')||'all checks pass';
   const chips=[...bad.map(c=>`<span class="stck bad">${esc(c.label)}</span>`),...warn.map(c=>`<span class="stck warn">${esc(c.label)}</span>`)].join('');
   return `<div class="stp"><span class="schip" style="background:${b[0]};color:${b[1]}">${p.score}</span><div style="flex:1;min-width:0"><div style="display:flex;gap:10px;align-items:baseline"><a href="${esc(p.url)}" target="_blank">${rel(p.url)}</a><span class="qd" style="flex:none;color:${bad.length?'#ff9c88':warn.length?'#f2c574':'var(--ok)'}">${sum}</span></div>${chips?`<div class="stcks">${chips}</div>`:''}</div></div>`};
 const rows=(list,lab)=>list.map((x,i)=>{const a=avg(x[1]),b=PBAND(a),id='st_'+lab+i;return `<div class="strow" onclick="tgl('${id}')" style="cursor:pointer"><span style="font-weight:600">${esc(x[0])} <span class="egcar">▾</span></span><span class="qd">${x[1].length} page${x[1].length>1?'s':''}</span><div class="stbar" title="avg score ${a}/100"><span class="stthr"></span><i style="width:${a}%;background:var(--grn)"></i></div><span class="schip" style="background:${b[0]};color:${b[1]}">${a}</span></div><div id="${id}" class="stdet">${[...x[1]].sort((p,q)=>p.score-q.score).map(stpage).join('')}</div>`}).join('');
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#ff4d00"></span><h3>BY TOP-LEVEL SECTION</h3><span class="meta">${secs.length} section${secs.length>1?'s':''}</span></div><div class="apbox" style="padding:6px 22px">${rows(secs.map(s=>['/'+s[0],s[1]]),'sec')}</div></section>`;
 const depths=Object.keys(byDepth).map(Number).sort((a,b)=>a-b),maxd=Math.max(...depths.map(d=>byDepth[d].length),1);
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2b53c"></span><h3>BY CRAWL DEPTH</h3><span class="meta">clicks from the homepage</span></div><div class="apbox" style="padding:6px 22px">${rows(depths.map(d=>['Depth '+d,byDepth[d]]),'dep')}</div></section>`;
 h+=`</div>`;return h}
function speed(){
 const sc2=ms=>ms<=800?'#3ecf8e':ms<=1800?'#f2b53c':'#ff4d3d',sl=ms=>ms<=800?'Fast':ms<=1800?'OK':'Slow';
 const ps=[...D.pages].sort((a,b)=>b.fetch_ms-a.fetch_ms);
 const avgf=Math.round(ps.reduce((a,p)=>a+p.fetch_ms,0)/ps.length),avgr=Math.round(ps.reduce((a,p)=>a+p.render_ms,0)/ps.length);
 const fast=ps.filter(p=>p.fetch_ms<=800).length,okc=ps.filter(p=>p.fetch_ms>800&&p.fetch_ms<=1800).length,slow=ps.filter(p=>p.fetch_ms>1800).length;
 let h=`<div style="display:flex;flex-direction:column;gap:18px">`;
 h+=`<div class="statgrid">
   <div class="statcard"><div class="n" style="color:${sc2(avgf)}">${avgf}<span style="font-size:14px;color:var(--muted)"> ms</span></div><div class="l">Avg server response · <b style="color:${sc2(avgf)}">${sl(avgf)}</b></div></div>
   <div class="statcard"><div class="n" style="color:#3ecf8e">${fast}</div><div class="l">Fast ≤ 0.8s</div></div>
   <div class="statcard"><div class="n" style="color:#f2b53c">${okc}</div><div class="l">OK ≤ 1.8s</div></div>
   <div class="statcard"><div class="n" style="color:#ff4d3d">${slow}</div><div class="l">Slow &gt; 1.8s</div></div>
   <div class="statcard"><div class="n">${avgr}<span style="font-size:14px;color:var(--muted)"> ms</span></div><div class="l">Avg render (tool overhead)</div></div></div>`;
 h+=`<div class="qd" style="line-height:1.6;max-width:900px">Server response graded on Google's TTFB thresholds: Fast ≤ 0.8s, OK ≤ 1.8s, Slow &gt; 1.8s. Render time is the tool's headless-Chrome overhead, not your site's speed.</div>`;
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#ff4d00"></span><h3>SLOWEST PAGES</h3><span class="meta">by server response time</span></div>
   <div style="background:var(--panel2);border:1px solid var(--line);border-radius:14px;overflow:hidden">
   <div style="display:grid;grid-template-columns:140px 1fr 90px;gap:16px;padding:12px 20px;background:#171412;border-bottom:1px solid var(--line);font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--muted)"><span>SERVER RESPONSE</span><span>URL</span><span style="text-align:right">RENDER MS</span></div>
   ${ps.slice(0,40).map(p=>`<div style="display:grid;grid-template-columns:140px 1fr 90px;gap:16px;align-items:center;padding:11px 20px;border-bottom:1px solid #ffffff0d"><span><span style="font-size:11px;font-weight:700;color:${sc2(p.fetch_ms)};background:${sc2(p.fetch_ms)}22;padding:3px 8px;border-radius:4px">${p.fetch_ms} ms</span> <span class="qd">${sl(p.fetch_ms)}</span></span><a href="${esc(p.url)}" target="_blank" style="font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${rel(p.url)}</a><span class="qd" style="text-align:right">${p.render_ms}</span></div>`).join('')}
   </div></section></div>`;
 return h}
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
         "<link href='https://fonts.googleapis.com/css2?family=Figtree:wght@300..900&display=swap' rel='stylesheet'>"
         f"<style>{css}</style></head><body>"
         f"<header><span class='logo'>CITED<span class='chip'>Score</span></span>"
         f"<span class='m'><a href='{H.escape(d['origin'])}' target='_blank' style='color:var(--txt);font-weight:600'>{H.escape(d['domain'])}</a> &middot; {d['pages_crawled']} pages &middot; {d['generated']}</span>"
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
    data=build(domain,origin,pages,sitecx)
    if out: apply_diff(data,out); write_outputs(data,out)     # out=None -> crawl + score only, no files (used by benchmark)
    emit("done",total,total,f"{domain}: {data['overall']}/100, {data['pages_crawled']} pages")
    return data

def benchmark(urls, max_pages=25, workers=WORKERS, progress=None):
    """Crawl each site (capped for speed) and return side-by-side CITED Score / pillars / engines."""
    out=[]
    for i,u in enumerate(urls):
        if not (u or "").strip(): continue
        if progress: progress("bench",i,len(urls),f"Crawling {u} ...")
        try:
            d=run_audit(u, out=None, max_pages=max_pages, workers=workers)
            out.append({"domain":d["domain"],"origin":d["origin"],"overall":d["overall"],
                        "pillars":d["pillars"],"engines":d["engines"],"pages":d["pages_crawled"],"error":None})
        except Exception as e:
            out.append({"domain":u,"overall":None,"pillars":{},"engines":{},"pages":0,"error":str(e)[:180]})
    return out

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
