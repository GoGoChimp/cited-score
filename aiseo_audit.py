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
import argparse, json, os, re, subprocess, sys, csv, html as H, datetime, time, threading, tempfile, base64, hashlib
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
                "ev":"A Cloudflare/WAF 403 on the citation-serving bots (ChatGPT-User, OAI-SearchBot, Googlebot, Bingbot) silently blocks citation, and blocking Google/Bing is a search-indexing risk too (live reachability test, Ch7)."},
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
 "noindex":    {"label":"Indexable (no noindex directive)","pillar":"Findable","ch":"Ch7","phase":1,"effort":"Low",
                "ev":"A meta-robots or X-Robots-Tag noindex removes the page from the index that feeds AI Overviews, Gemini and Copilot, so it cannot be cited (Ch7)."},
 "speed":      {"label":"Fast server response (retrieval-ready)","pillar":"Findable","ch":"Ch3","phase":1,"effort":"Med",
                "ev":"Live-retrieval engines abandon slow pages (499 timeouts) before they can cite them; a fast time-to-first-byte keeps the page retrieval-eligible (Ch3)."},
 "schemavalidity":{"label":"Valid structured data (parses cleanly)","pillar":"Known","ch":"Ch4","phase":2,"effort":"Med",
                "ev":"Malformed JSON-LD is silently dropped by the parser, so a broken block gives the engine no entity signal at all (Ch4)."},
 "duplicate":  {"label":"Unique title & meta (not duplicated)","pillar":"Known","ch":"Ch4","phase":2,"effort":"Med",
                "ev":"Duplicate titles or descriptions blur which page is the entity and split the signal across near-identical pages (Ch4)."},
 "rankedlist": {"label":"Ranked Top-N list (on listicle pages)","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"63% of AI citations are listicles; a page that promises 'best/top N' but has no ranked list forfeits the format engines cite most (Ch5)."},
 "answerthird":{"label":"Answer in the first third of the page","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"44.2% of ChatGPT citations come from the first third of the page; a buried answer is lifted far less (Indig 2026, Ch5)."},
 "h2answer":   {"label":"Headings paired with a liftable answer","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"Retrieval lifts a heading plus its 40-180 word answer as one chunk; a heading over a wall of text hands the engine nothing clean (Ch5)."},
 "orphans":    {"label":"Reachable (internally linked + in sitemap)","pillar":"Findable","ch":"Ch7","phase":2,"effort":"Med",
                "ev":"A page nothing links to, or one missing from the sitemap, is hard for a crawler to discover and rarely cited (Ch7)."},
 "brokenlinks":{"label":"No broken outbound links","pillar":"Findable","ch":"Ch3","phase":3,"effort":"Low",
                "ev":"Dead links (4xx/5xx) waste crawl budget and read as an unmaintained page; a crawler drops non-200 targets (Ch3)."},
 "nearduplicate":{"label":"Distinct content (not near-duplicate)","pillar":"Known","ch":"Ch4","phase":3,"effort":"High",
                "ev":"Near-duplicate pages split the entity signal and cannibalise each other; the engine keeps one and drops the rest (Ch4)."},
 "reviewschema":{"label":"Review / rating schema (social proof)","pillar":"Trusted","ch":"Ch4","phase":3,"effort":"Low",
                "ev":"AI recommends well-reviewed businesses (claiming a Trustpilot profile lifted citation 1%->54%); Review / AggregateRating schema exposes your ratings to Google rich results and AI in machine-readable form (Trustpilot/Seer + Uberall 2026, Ch4)."},
 # INFORMATIONAL - not scored
 "llms":       {"label":"llms.txt (informational, not scored)","pillar":"Info","ch":"Ch5","phase":0,"effort":"Low",
                "ev":"No evidence llms.txt affects AI citation: SE Ranking found no correlation (Ch5); Google's John Mueller says no AI system currently uses it; Ahrefs found 97% of llms.txt files got zero bot requests; and Mark Williams-Cook's cats.txt experiment showed a fabricated 'standard' clears the same 'proofs' people cite (crawled, indexed, retrieved, endorsed), so those signals prove nothing. Shown for reference; harmless, not required."},
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
 "noindex":"Remove the noindex from the meta-robots tag or the X-Robots-Tag header so the page can be indexed and cited.",
 "speed":"Speed up the server response (cache, CDN, lighter HTML) so time-to-first-byte stays under ~0.8s (Google's 'good' TTFB).",
 "schemavalidity":"Fix the JSON-LD so every block is valid JSON with an @type (validate at schema.org / Rich Results Test).",
 "duplicate":"Give each page a unique title and meta description so engines can tell them apart.",
 "rankedlist":"On a 'best/top N' page, present the items as a numbered, ranked list, not prose.",
 "answerthird":"Move the core answer into the first third of the page, not below the fold.",
 "h2answer":"Follow each H2/H3 with a self-contained 40-180 word answer to that heading.",
 "orphans":"Link to every page from related content and include it in the XML sitemap.",
 "brokenlinks":"Fix or remove the dead outbound links (correct the URL, or 301 the target).",
 "nearduplicate":"Consolidate or differentiate near-duplicate pages (merge + 301, or make each genuinely distinct).",
 "reviewschema":"Add Review / AggregateRating JSON-LD (real rating value + review count) to your service, product and local pages.",
}
# per-engine weights (which signals each engine actually weights). site ids allowed.
# Render-parity CORRECTED 2026-07-19 after deep research (book/research/schema-render-parity-...):
# GPTBot / PerplexityBot / ClaudeBot do NOT execute JavaScript (Vercel/MERJ, 500M+ fetches), so
# JS-injected schema is invisible to ChatGPT / Perplexity / Claude - parity is a hard visibility
# gate there (weight 3). Bing/Copilot renders but unreliably (weight 2). Google renders JS, so
# Gemini + AI Overviews DO eventually see JS-injected schema, so parity is removed for them (it is
# a reliability/speed issue there, not visibility). schema itself still weighted for Gemini.
ENGINE_WEIGHTS = {
 "ChatGPT":     {"wordcount":3,"statdensity":3,"citations":3,"parity":3,"entity":2,"entitydensity":2,"schemacomplete":2,"author":2,"sourced":2,"answerfirst":2,"sections":2,"schema":1,"definitional":1,"readability":1,"freshness":1,"qheadings":1,"noindex":2,"speed":2,"schemavalidity":1,"answerthird":1,"h2answer":1},
 "Perplexity":  {"freshness":3,"citations":3,"parity":3,"sourced":2,"entity":2,"entitydensity":2,"statdensity":2,"answerfirst":2,"liststables":2,"sections":2,"reachability":2,"definitional":1,"qheadings":1,"author":1,"comparison":1,"video":1,"noindex":2,"speed":2,"answerthird":1,"h2answer":1,"rankedlist":1,"orphans":1},
 "AI Overviews":{"answerfirst":3,"qheadings":3,"definitional":2,"sections":2,"schema":2,"faq":2,"liststables":2,"entity":2,"entitydensity":1,"readability":1,"schemacomplete":2,"video":2,"freshness":1,"canonical":1,"sitemap":1,"title":1,"meta":1,"comparison":1,"author":1,"noindex":3,"speed":1,"answerthird":2,"h2answer":1,"rankedlist":1,"schemavalidity":1,"duplicate":1,"orphans":1,"brokenlinks":1,"nearduplicate":1,"reviewschema":1},
 "Gemini":      {"schema":3,"entity":3,"entitydensity":2,"schemacomplete":2,"statdensity":2,"answerfirst":2,"canonical":1,"sitemap":1,"faq":1,"citations":1,"author":1,"noindex":3,"schemavalidity":2,"duplicate":1,"nearduplicate":1,"reviewschema":1},
 "Copilot":     {"schema":3,"sitemap":2,"reachability":2,"liststables":2,"statdensity":2,"answerfirst":2,"parity":2,"entity":2,"entitydensity":1,"schemacomplete":2,"faq":2,"freshness":1,"sourced":1,"comparison":1,"video":1,"noindex":3,"speed":1,"schemavalidity":1,"rankedlist":1,"orphans":1,"duplicate":1,"brokenlinks":1,"nearduplicate":1,"reviewschema":1},
 "Claude":      {"sections":3,"parity":3,"answerfirst":2,"definitional":1,"readability":1,"statdensity":2,"qheadings":2,"liststables":2,"entity":2,"entitydensity":1,"citations":1,"author":1,"sourced":1,"noindex":2,"h2answer":2,"answerthird":1},
}
ENGINE_NOTE = {
 "ChatGPT":"Favours comprehensive, authoritative, source-cited content + strong entity grounding. Cites few sources per answer, so be THE definitive page.",
 "Perplexity":"Live-searches every query. Rewards freshness, extractable facts and external citations. Cites many sources, so breadth helps.",
 "AI Overviews":"Rank-coupled + query fan-out. Answer-first, question headings, schema and self-contained sections win.",
 "Gemini":"Google index + Knowledge Graph. Entity/schema clarity, sameAs and canonical carry visibility across.",
 "Copilot":"Bing-grounded and highly citation-friendly. Schema, sitemap/IndexNow, listicles and extractable facts win here.",
 "Claude":"Synthesises rather than quotes. Rewards clean logical chunking, factual density and clear structure.",
}
# Grok (xAI) is a large surface (~117M users) but a near-zero referral driver, and its
# one distinctive lever (heavy weighting of live X posts) is off-page and un-scoreable by
# an on-page crawler. There is no Grok citation export to calibrate against, so scoring it
# would be an uncalibrated Perplexity clone. Shown as an ADVISORY, not a scored engine -
# the same informational treatment the tool gives llms.txt. Graduates to a real engine the
# day xAI ships a citation/referral export that --calibrate can read.
GROK_ADVISORY = {
 "name":"Grok (xAI)",
 "how":"Grok grounds every answer in two pools at once: the open web and the live X (Twitter) post graph, scored for relevance and recency, then attributed to a page or a post.",
 "why":"Grok's open-web retrieval reads the same signals as Perplexity and ChatGPT (live web, freshness, extractable facts, server-rendered content), so those two engine scores already tell you how Grok sees your pages. There is no separate on-page Grok lever to score.",
 "lever":"Grok's one distinctive signal is heavy weighting of recent X posts (it can cite a post from hours ago). That is an off-page, X-presence play this on-page audit cannot measure.",
 "caveat":"Grok scored lowest of the major models for citation accuracy in the Columbia Journalism Review test, so its attributions are the least reliable of any engine here.",
 "trigger":"No Grok citation or referral export exists yet. When xAI ships one, Grok graduates from an advisory note to a calibrated, weighted engine like the other six.",
 "proxy":["Perplexity","ChatGPT"],
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

def fetch_raw(url, ua=UA, timeout=25, capture=None, _retry=True):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(); enc = r.headers.get_content_charset() or "utf-8"
            if capture is not None: capture["final_url"] = r.geturl()   # after redirects
            return r.status, dict(r.headers), data.decode(enc, "ignore"), int((time.time()-t0)*1000)
    except urllib.error.HTTPError as e:
        if _retry and e.code in (429, 503):                             # transient rate-limit / overload -> back off once
            time.sleep(2.0); return fetch_raw(url, ua, timeout, capture, _retry=False)
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

_MONTHS = {m: i for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"], 1)}
def _parse_dates(s):
    out = []
    for m in re.finditer(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s or ""):          # 2026-08-07 / 2026/08/07
        try: out.append(datetime.date(int(m[1]), int(m[2]), int(m[3])))
        except Exception: pass
    for m in re.finditer(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})\b", s or ""): # 7 August 2026
        mo = _MONTHS.get(m[2][:3].lower())
        if mo:
            try: out.append(datetime.date(int(m[3]), mo, int(m[1])))
            except Exception: pass
    for m in re.finditer(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", s or ""): # August 7, 2026
        mo = _MONTHS.get(m[1][:3].lower())
        if mo:
            try: out.append(datetime.date(int(m[3]), mo, int(m[2])))
            except Exception: pass
    return [d for d in out if 2000 <= d.year <= datetime.date.today().year + 1]

def find_date(soup, objs):
    """Freshest date from, in priority order, schema (dateModified/datePublished), visible <time>,
    and Open Graph / meta date tags. Falls back to a visible text date next to an Updated/Published cue
    only when no machine-readable date exists, so a stray in-content date cannot fake freshness."""
    machine = []
    def dig(n, key):
        if isinstance(n, dict):
            if isinstance(n.get(key), str): machine.append(n[key])
            for v in n.values(): dig(v, key)
        elif isinstance(n, list):
            for v in n: dig(v, key)
    for o in objs:
        dig(o, "dateModified"); dig(o, "datePublished")                          # schema
    for t in soup.find_all("time"):                                              # visible <time>
        d = t.get("datetime") or t.get_text(strip=True)
        if d: machine.append(d)
    for attrs in ({"property": "article:modified_time"}, {"property": "article:published_time"},
                  {"itemprop": "dateModified"}, {"itemprop": "datePublished"},
                  {"name": "date"}, {"name": "last-modified"}, {"name": "revised"}):  # OG / meta dates
        for tag in soup.find_all("meta", attrs=attrs):
            if tag.get("content"): machine.append(tag["content"])
    dates = [d for s in machine for d in _parse_dates(s)]
    if not dates:                                                                # last resort: visible text date by a cue
        txt = soup.get_text(" ", strip=True)
        for m in re.finditer(r"(?:last\s+updated|updated|published|posted|modified|reviewed)\b[:\s]*([^.\n|]{0,34})", txt, re.I):
            dates.extend(_parse_dates(m.group(1)))
    return max(dates) if dates else None

def classify(path, types):
    if path in ("", "/"): return "home"
    segs = [s for s in path.strip("/").split("/") if s]
    if path.rstrip("/") in ("/blog","/case-studies","/blogs","/resources","/guides","/tools"): return "listing"
    if segs and segs[0] == "blog": return "article"
    if {"Article","BlogPosting","NewsArticle","TechArticle"} & set(types): return "article"
    _t = set(types)                                                  # e-commerce page types (were falling into generic "page")
    if {"Product","IndividualProduct","ProductGroup"} & _t: return "product"
    if re.search(r"/(products?|product|p|item|sku|dp|buy)/[^/]", path, re.I): return "product"
    if re.search(r"/(collections?|categor(?:y|ies)|shop|store|department|ranges?|brands?)(/|$)", path, re.I): return "category"
    if "AggregateOffer" in _t: return "product"                     # priced page, no category path -> product
    if segs and segs[0] in ("services","service"): return "service"
    if len(segs) == 1: return "page"
    return "page"

# checks that genuinely do not apply to a page type -> excluded from scoring.
# Deliberately NARROW (the score must not be kind): a homepage still owes citations,
# statistics and depth, so those stay in.
NA_BY_TYPE = {
    "home":     {"answerfirst","qheadings","sections","faq","freshness","schema","schemacomplete","author","sourced","video","readability","entitydensity","definitional","answerthird","h2answer","reviewschema"},
    "listing":  {"answerfirst","qheadings","sections","faq","freshness","schema","statdensity","citations","wordcount","schemacomplete","author","sourced","video","entity","readability","entitydensity","definitional","answerthird","h2answer","reviewschema"},
    "article":  {"reviewschema"},
    # e-commerce: a product/category page is not article/Q&A content, so the article-shaped checks do not apply
    # (a product page has no human author and no "X is a..." opener; it owes schema, entity, reviews, freshness).
    "product":  {"author","definitional","answerfirst","answerthird","qheadings","h2answer","sourced","citations"},
    "category": {"author","definitional","answerfirst","answerthird","qheadings","h2answer","sourced","citations","wordcount","statdensity"},
}

# --- Website-type scoring profiles: a re-weighting LAYER on the calibrated 0-100 (NOT a separate score).
# A profile is a {check_id: weight_multiplier} map (>1 up-weights, <1 down-weights, missing = 1.0). Per-page
# NA_BY_TYPE still governs applicability; "general" has no profile so a general site scores exactly as before.
# Directional defaults from the deep-research report; to be calibration-tuned, never treat as final weights.
PROFILES = {
    "ecommerce": {"schema":1.6,"schemacomplete":1.6,"entity":1.4,"reviewschema":1.6,"freshness":1.4,"faq":1.3,"liststables":1.3,
                  "wordcount":0.4,"statdensity":0.4,"sourced":0.4,"citations":0.5,"author":0.5,"definitional":0.5},
    "blog":      {"statdensity":1.5,"sourced":1.5,"citations":1.5,"freshness":1.4,"answerfirst":1.4,"answerthird":1.3,"h2answer":1.3,
                  "author":1.4,"entity":1.3,"qheadings":1.3,"definitional":1.2,"schema":0.6,"faq":0.5,"reviewschema":0.4},
    "b2b_saas":  {"entity":1.5,"comparison":1.5,"author":1.3,"citations":1.3,"reviewschema":0.5,"wordcount":0.5,"schema":0.9},
}
SITE_PROFILE_LABEL = {"ecommerce":"E-commerce","blog":"Blog / publisher","b2b_saas":"B2B SaaS","general":"General"}

def classify_site(pages):
    """Aggregate per-page signals into a dominant SITE type, to pick a scoring profile. Deliberately
    conservative; the chosen type is exposed in the report and is meant to be overridable."""
    n=len(pages) or 1
    ECOM_PATH=re.compile(r"/(shop|store|collections?|cart|checkout|basket|bag)(/|$|\?)",re.I)   # shop-specific; bare /product(s)/ dropped (SaaS uses it, e.g. moz /products/api, notion /product)
    SAAS_PATH=re.compile(r"/(pricing|features?|integrations?|solutions?|demo|sign-?up|api|docs)(/|$|\?)",re.I)
    ECOM_SCHEMA={"Product","AggregateOffer"}; SAAS_SCHEMA={"SoftwareApplication","WebApplication"}   # bare "Offer" excluded: services/pricing pages use it too
    prod=ecom=saas=article=service=0
    for p in pages:
        path=(p.get("path") or "").lower(); tset=set((p.get("metrics") or {}).get("schema_types") or [])
        is_article=p.get("type")=="article"
        shop_schema=bool(ECOM_SCHEMA & tset) and not is_article     # Product schema on a news/blog article (affiliate/review widget) is NOT a shop signal (e.g. theguardian)
        if shop_schema: prod+=1
        if (ECOM_PATH.search(path) and not is_article) or shop_schema: ecom+=1
        if SAAS_PATH.search(path) or (SAAS_SCHEMA & tset): saas+=1
        if is_article: article+=1
        if p.get("type")=="service" or ("Service" in tset): service+=1   # a service BUSINESS (agency/consultancy) signal - a publisher has none
    if prod>=3 or ecom/n>=0.20: return "ecommerce"          # Product schema on non-article pages + shop paths; big retailers under-detect on shallow crawls (overridable) rather than risk SaaS/news false positives
    if article/n>=0.55 and article>=3 and service<2: return "blog"   # blog-dominant, min-evidence guard, AND not a service business with a content blog (agencies/consultancies -> general, not "publisher")
    if saas/n>=0.08 and prod==0: return "b2b_saas"           # SaaS markers, not a shop (shallow crawls surface few, so no count floor)
    # mixed / thin-evidence sites fall through to general (auto-guess is overridable in the app)
    return "general"

def chk(cid, status, detail):
    m = CHECK_META[cid]
    return {"id":cid,"label":m["label"],"status":status,"detail":detail,
            "pillar":m["pillar"],"ch":m["ch"],"ev":m["ev"]}

def analyze(url, status, raw, rendered, domain, hdrs=None, fetch_ms=0):
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
    # answer-in-first-third: does a substantive (>=40w) answer sit in the first ~30% of the body?
    _pws=[len(words(pp.get_text(" ",strip=True))) for pp in root.find_all("p")]
    _totpw=sum(_pws) or 1; _ansat=None; _cw=0
    for _n in _pws:
        if _n>=40 and _ansat is None: _ansat=_cw
        _cw+=_n
    if _ansat is None: _af=("warn","no 40+ word answer paragraph")
    else:
        _fr=_ansat/_totpw
        _af=("good",f"answer at {round(100*_fr)}% of page") if _fr<=0.34 else (("warn",f"answer at {round(100*_fr)}%") if _fr<=0.66 else ("bad",f"answer buried at {round(100*_fr)}%"))
    C.append(chk("answerthird",_af[0],_af[1]))
    heads=root.find_all(["h2","h3"]); nh=len(heads)
    def _qshape(t):
        t=(t or "").strip()
        hasfig=bool(re.search(r"\d",t))
        # "How we work" / "What we do" = about-us nav labels, not retrieval queries (unless they carry a figure)
        if re.match(r"^\s*(?:how|what|why|who|where)\s+(?:we|our|i|us|you)\b",t,re.I) and not hasfig: return False
        if t.endswith("?") or QSTART.match(t): return True
        # claim-shaped: a heading carrying a concrete figure reads as a liftable factual claim
        if hasfig and len(t.split())>=3: return True
        return False
    qh=sum(1 for h in heads if _qshape(h.get_text(strip=True)))
    qpct=round(100*qh/nh) if nh else 0
    C.append(chk("qheadings","good" if qpct>=30 else ("warn" if qpct>=10 else "bad"),f"{qh}/{nh} ({qpct}%)"))
    # self-contained sections: flag WALLS of text (un-liftable chunks), not short blocks
    secs=section_counts(root); walls=sum(1 for s in secs if s>220)
    if not secs: sec_status="warn"; sec_detail="no H2/H3 sections"
    elif walls==0: sec_status="good"; sec_detail=f"{len(secs)} sections, none over 220w"
    elif walls<=max(1,len(secs)//5): sec_status="warn"; sec_detail=f"{walls} wall(s) of text over 220w"
    else: sec_status="bad"; sec_detail=f"{walls}/{len(secs)} sections are walls of text"
    C.append(chk("sections",sec_status,sec_detail))
    # H2-to-answer pairing: is each heading followed by a liftable 40-180 word answer?
    _live=[s for s in secs if 40<=s<=180]
    if not secs: _hp=("warn","no H2/H3 sections")
    else:
        _hpct=round(100*len(_live)/len(secs))
        _hp=("good" if _hpct>=50 else ("warn" if _hpct>=25 else "bad"),f"{len(_live)}/{len(secs)} liftable answer sections ({_hpct}%)")
    C.append(chk("h2answer",_hp[0],_hp[1]))
    ntab=len(root.find_all("table")); nlist=len(root.find_all(["ol","ul"]))
    C.append(chk("liststables","good" if ntab+nlist>=1 else "warn",f"{ntab} tables, {nlist} lists"))
    # ranked list: only judged where the page promises a listicle ("best/top N" in title or H1)
    _h1t=h1s[0].get_text(" ",strip=True) if h1s else ""; _tt2=title+" "+_h1t
    # listicle intent: "best/top N" (real count >=3, not a superlative like "Top 1%") OR "best/top ... <listicle noun>"
    _lm=re.search(r"\b(?:top|best)\s+(\d{1,3})\b(?!\s*%)",_tt2,re.I)
    _numint=bool(_lm) and int(_lm.group(1))>=3
    _nounint=bool(re.search(r"\b(?:best|top)\b[\w\s/&,'-]{0,45}?\b(?:tools?|software|apps?|platforms?|services?|options?|alternatives?|agenc\w+|companies|solutions?|plugins?|providers?|vendors?|examples?|tips?|reasons?|strategies|frameworks?|templates?|books?|courses?)\b",_tt2,re.I))
    _listintent=_numint or _nounint
    _ranked=any(len(o.find_all("li"))>=3 for o in root.find_all("ol"))
    if not _listintent: C.append(chk("rankedlist","na","not a ranked-list page"))
    else: C.append(chk("rankedlist","good" if _ranked else "warn","ranked Top-N list present" if _ranked else "listicle title but no ranked (ol) list"))
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
    # schema validity: do the JSON-LD blocks parse? (malformed = silently invisible entity signal)
    _ldt=soup.find_all("script",attrs={"type":"application/ld+json"}); _ldn=len(_ldt); _ldok=0; _ldtyped=0
    def _hastype(n):
        if isinstance(n,dict): return bool(n.get("@type")) or any(_hastype(v) for v in n.values())
        if isinstance(n,list): return any(_hastype(x) for x in n)
        return False
    for _t2 in _ldt:
        try:
            _o=json.loads(_t2.string or _t2.get_text() or "{}"); _ldok+=1
            if _hastype(_o): _ldtyped+=1
        except Exception: pass
    if _ldn==0: C.append(chk("schemavalidity","na","no structured data"))
    elif _ldok<_ldn: C.append(chk("schemavalidity","bad",f"{_ldn-_ldok} of {_ldn} JSON-LD block(s) fail to parse"))
    elif _ldtyped<_ldok: C.append(chk("schemavalidity","warn",f"{_ldok-_ldtyped} block(s) missing @type"))
    else: C.append(chk("schemavalidity","good",f"{_ldok} valid JSON-LD block(s)"))
    d=find_date(soup,jsonld_objs(soup))
    if d:
        age=(datetime.date.today()-d).days
        C.append(chk("freshness","good" if age<=365 else ("warn" if age<=730 else "bad"),f"{d.isoformat()} ({age}d)"))
    else:
        C.append(chk("freshness","bad","no date found"))
    imgs=soup.find_all("img"); alts=sum(1 for i in imgs if (i.get("alt") or "").strip()); apct=round(100*alts/len(imgs)) if imgs else 100
    C.append(chk("alt","good" if apct>=90 else "warn",f"{alts}/{len(imgs)} ({apct}%)"))
    il=0; il_targets=set()
    for a in root.find_all("a",href=True):
        h=a["href"]
        if h.startswith("/") or domain in h:
            il+=1
            try: il_targets.add(urllib.parse.urlparse(urllib.parse.urljoin("https://"+domain,h)).path.rstrip("/") or "/")
            except Exception: pass
    C.append(chk("internal","good" if il>=3 else "warn",f"{il} internal links"))
    can=soup.find("link",attrs={"rel":"canonical"})
    C.append(chk("canonical","good" if can and can.get("href") else "warn",can.get("href") if can else "none"))
    # noindex: a meta-robots or X-Robots-Tag noindex is a hard citability gate (rendered DOM, since Google renders JS)
    def _noidx(v):
        toks=[t.strip() for t in (v or "").lower().replace(";",",").split(",")]
        return "noindex" in toks or "none" in toks
    noidx=""
    for m in soup.find_all("meta"):
        if (m.get("name") or "").lower() in ("robots","googlebot","bingbot") and _noidx(m.get("content")):
            noidx="meta "+(m.get("name") or "robots").lower(); break
    if not noidx and hdrs:
        for k,v in hdrs.items():
            if k.lower()=="x-robots-tag" and _noidx(v): noidx="X-Robots-Tag"; break
    C.append(chk("noindex","bad" if noidx else "good",("noindex via "+noidx) if noidx else "indexable"))
    # retrieval speed: raw-HTML fetch time as a time-to-first-byte proxy; live engines drop slow pages
    fm=fetch_ms or 0
    C.append(chk("speed","na" if fm<=0 else ("good" if fm<=800 else ("warn" if fm<=1800 else "bad")),f"{fm} ms fetch"))
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
    # review / rating schema (social-proof signal for commercial pages)
    hasrev=bool({"Review","AggregateRating"} & set(types_r)) or any(isinstance(n,dict) and (n.get("aggregateRating") or n.get("review") or n.get("reviewRating") or n.get("ratingValue")) for n in nodes)
    C.append(chk("reviewschema","good" if hasrev else "warn","Review/AggregateRating present" if hasrev else "no Review/AggregateRating schema"))
    # ---- agent-readiness (advisory, not scored): can an AI agent ACT on this page? ACTIONABLE schema only ----
    _ACT={"OrderAction","BuyAction","ReserveAction","ScheduleAction","ContactAction","SubscribeAction","RegisterAction","PayAction","RentAction","BookAction","ChooseAction","SellAction"}
    def _offval(n):
        o=n.get("offers"); return o if isinstance(o,dict) else (o[0] if isinstance(o,list) and o and isinstance(o[0],dict) else {})
    _ag_action=bool(_ACT & set(types_r)) or any(isinstance(n,dict) and n.get("potentialAction") for n in nodes)
    _ag_offer=("Offer" in types_r) or any(isinstance(n,dict) and (n.get("offers") or n.get("price") is not None) for n in nodes)
    _ag_price=any(isinstance(n,dict) and (n.get("price") is not None or _offval(n).get("price") is not None) for n in nodes)
    _ag_avail=any(isinstance(n,dict) and (n.get("availability") or _offval(n).get("availability")) for n in nodes)
    _ag_prodserv=bool({"Product","Service","SoftwareApplication","WebApplication","Event","Course","MenuItem","Reservation","Trip"} & set(types_r))
    _ag_contact=("ContactPoint" in types_r) or any(isinstance(n,dict) and n.get("contactPoint") for n in nodes)
    agent={"action":_ag_action,"offer":_ag_offer,"price":_ag_price,"avail":_ag_avail,"prodserv":_ag_prodserv,"contact":_ag_contact}
    # ---- information-gain proxy (advisory, not scored): does this page add ORIGINAL information? ----
    # A local crawler cannot prove true originality (no web-corpus to diff against), so we score a
    # labelled PROXY: distinct-figure density + first-hand-research language + a named proprietary asset
    # + a real data table (near-duplication is folded in at build). Indig: 15+ distinct figures -> IG 62.1 vs 40.2 for <=1.
    _ig_figs=len(set(m.group(0).strip() for m in NUM_RE.finditer(body)))
    _ig_firsthand=bool(re.search(r"\b(?:our (?:data|study|survey|research|analysis|experiment|test(?:ing|s)?|findings?|results?|dataset|benchmark|audit|methodology|numbers)|we (?:surveyed|tested|analy[sz]ed|measured|found|ran|studied|tracked|collected|compared|interviewed)|in our (?:study|test|research|experience|analysis|data)|according to our)\b", body, re.I))
    _ig_propr=bool(re.search(r"\bthe [A-Z][A-Za-z0-9]+(?:[ /-][A-Z0-9][A-Za-z0-9]+){0,4}\s+(?:Method|Framework|Model|Rule|Gap|Index|Score|Study|Survey|Report|Formula|System|Protocol|Stack|Playbook|Benchmark)\b", body)) or bool(re.search(r"\bour (?:proprietary|own|original|in-house|internal|custom)\b", body, re.I))
    _ig_datatable=any(len(NUM_RE.findall(t.get_text(' ',strip=True)))>=4 for t in root.find_all("table"))
    infogain={"figures":_ig_figs,"firsthand":_ig_firsthand,"proprietary":_ig_propr,"datatable":_ig_datatable}
    # ---- phase-3 data: outbound content links (for broken-link check) + content fingerprints (near-dup) ----
    outlinks=set()
    for a in root.find_all("a",href=True):
        _h=(a.get("href") or "").strip()
        if not _h or _h.startswith(("#","mailto:","tel:","javascript:")): continue
        _u=urllib.parse.urljoin(url,_h); _u,_=urllib.parse.urldefrag(_u)
        _pu=urllib.parse.urlparse(_u)
        if _pu.scheme in ("http","https") and not ASSET_RE.search(_pu.path): outlinks.add(_u)
    _cn=re.sub(r"\s+"," ",body.lower()).strip()
    chash=hashlib.md5(_cn.encode("utf-8","ignore")).hexdigest() if _cn else ""
    _sh=([" ".join(_bw[i:i+4]) for i in range(len(_bw)-3)][:1500] if len(_bw)>=4 else _bw[:1500])
    simhash=_simhash(_sh)
    na=NA_BY_TYPE.get(ptype,set())
    for c in C:
        if c["id"] in na: c["status"]="na"
    metrics={"words":wc,"headings":nh,"question_pct":qpct,"walls":walls,"stat_density":dens,
             "readability":fk,"entity_density":pnd,"definitional":defn,
             "ext_links":ext,"internal_links":il,"schema_types":sorted(tset),"images":len(imgs),"alt_pct":apct}
    return {"path":path,"type":ptype,"title":title,"meta":mdc,"checks":C,"metrics":metrics,"rendered":rendered is not None,
            "links":sorted(il_targets),"outlinks":sorted(outlinks),"simhash":simhash,"chash":chash,"sameas":sameas,"agent":agent,"infogain":infogain,
            "search_forms":find_search_forms(soup, url)}

SEARCH_PARAM_NAMES={"q","s","query","search","keyword","kw","term","search_query","searchword","wc-search","field-keywords"}
def find_search_forms(soup, base_url):
    """Detect on-site search forms from a rendered page. GET forms only (results carried in the URL, so we
    can probe them). Conservative: needs a type=search input, a known search param, or a role=search /
    search-labelled form with a text input. Returns a deduped [{action, param}]."""
    out=[]
    for f in soup.find_all("form"):
        method=(f.get("method") or "get").strip().lower()
        if method and method!="get": continue                       # POST search can't be URL-probed
        role=(f.get("role") or "").lower()
        idcls=(" ".join([f.get("id") or ""]+(f.get("class") or []))).lower()
        searchish = role=="search" or "search" in idcls
        param=None
        for inp in f.find_all(("input","textarea")):
            itype=(inp.get("type") or "text").strip().lower()
            nm=(inp.get("name") or "").strip()
            if not nm or itype in ("hidden","submit","button","reset","checkbox","radio","image","file"): continue
            if itype=="search" or nm.lower() in SEARCH_PARAM_NAMES:
                param=nm; searchish=True; break
            if searchish and param is None and itype in ("text","search",""):
                param=nm                                            # first text field of an already-search-labelled form
        if not (searchish and param): continue
        action=urllib.parse.urljoin(base_url,(f.get("action") or base_url)).split("#")[0]
        out.append({"action":action,"param":param})
    seen=set(); uniq=[]
    for sf in out:
        k=(sf["action"],sf["param"])
        if k not in seen: seen.add(k); uniq.append(sf)
    return uniq

_SEARCH_STOP=set("the a an and or but of for to in on at by with from is are was were be been am your you our we us it its this that these those how what why when where who which best top guide guides vs review reviews home page pages welcome new get more all about contact blog news read learn help".split())
def audit_internal_search(pages, origin, timeout=10):
    """Citation-to-landing (b): probe the site's OWN internal search with a term taken from its own page
    titles (so a working search MUST return results), then report FACTUALLY. Never claims 'broken' on a guess:
    outcome is works / empty / unclear / unreachable / no_term, and 'empty' needs an explicit no-results signal."""
    cand=Counter()
    for p in pages:
        for sf in (p.get("search_forms") or []): cand[(sf["action"],sf["param"])]+=1
    if not cand: return {"detected":False}
    (action,param),_=cand.most_common(1)[0]
    wc=Counter()
    for p in pages:
        for w in re.findall(r"[A-Za-z][A-Za-z0-9'-]{2,}", (p.get("title") or "").lower()):
            if len(w)<4 or w in _SEARCH_STOP: continue
            wc[w]+=1
    term=next((w for w,c in wc.most_common() if c>=2), None) or (wc.most_common(1)[0][0] if wc else None)
    base={"detected":True,"action":action,"param":param}
    if not term: return {**base,"probed":False,"outcome":"no_term"}
    sep="&" if urllib.parse.urlparse(action).query else "?"
    probe=f"{action}{sep}{urllib.parse.quote(param)}={urllib.parse.quote(term)}"
    cap={}
    try: st,hdrs,body,ms=fetch_raw(probe, capture=cap, timeout=timeout)
    except Exception: st,body=None,""
    if st!=200 or not body: return {**base,"term":term,"probed":True,"outcome":"unreachable","status":st}
    try: rend,_=render(probe, timeout=20)        # most search results are client-side, so JS-render the probe
    except Exception: rend=None
    doc=rend or body
    soup=BeautifulSoup(doc,"lxml")
    low=soup.get_text(" ",strip=True).lower()
    nores=bool(re.search(r"(no results|nothing found|no matches|0 results|no products found|couldn'?t find|did not match|no results found|sorry[, ].{0,20}\bno\b)", low))
    dom=urllib.parse.urlparse(origin).netloc.replace("www.","")
    root=main_content(BeautifulSoup(doc,"lxml"))
    links=0
    for a in root.find_all("a",href=True):
        pu=urllib.parse.urlparse(urllib.parse.urljoin(probe,a["href"]))
        if pu.netloc.replace("www.","")==dom and (pu.path or "").rstrip("/"): links+=1
    mr=soup.find("meta",attrs={"name":re.compile(r"^robots$",re.I)})
    noindex=bool(mr and "noindex" in (mr.get("content") or "").lower())
    outcome="empty" if (nores and links<3) else ("works" if links>=3 else "unclear")
    return {**base,"term":term,"probed":True,"outcome":outcome,"result_links":links,"noindex":noindex,"status":200}

_QSTOP={"the","a","an","and","or","of","for","to","in","on","at","with","from","is","are","how","what","why","your","you","vs"}
def _qtok(s):
    return {w for w in re.findall(r"[a-z0-9]{2,}", (s or "").lower()) if w not in _QSTOP}

def load_queries(path):
    """Parse a grounding-query CSV (Bing AI Performance 'AI Search Queries' export:
    Grounding Query, Intent, Topic, Citations, Citation Share). Tolerant of header casing."""
    out=[]
    if not path or not os.path.exists(path): return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            q=(r.get("Grounding Query") or r.get("Query") or r.get("query") or "").strip()
            if not q: continue
            try: c=int(float(r.get("Citations") or r.get("citations") or 0))
            except Exception: c=0
            sh=(r.get("Citation Share") or r.get("citation_share") or "").replace("%","").strip()
            try: shv=float(sh)/100 if sh else None
            except Exception: shv=None
            out.append({"query":q,"citations":c,"share":shv,
                        "intent":(r.get("Intent") or "").strip(),"topic":(r.get("Topic") or "").strip()})
    return out

def query_coverage(pages, queries, max_q=50):
    """The query-match layer: does the site have a page targeting the queries AI actually cites?
    Gap = a high-citation query no page targets (the opportunity). Orphan = a well-scored page that
    targets no cited query (effort not aimed at demand). Matching is idf-weighted title/meta/URL overlap
    so distinctive terms (heatmap, ab) dominate common ones (best, 2026). Directional, not exact."""
    def toks_for(p):
        slug=re.findall(r"[a-z0-9]{2,}", urllib.parse.urlparse(p.get("url") or p.get("path") or "").path.lower())
        return _qtok((p.get("title") or "")+" "+(p.get("meta") or "")+" "+" ".join(slug))
    ptoks=[toks_for(p) for p in pages]
    df=Counter(t for toks in ptoks for t in toks); N=len(pages) or 1
    w=lambda t: 1.0/(1+df.get(t,0))                                   # rare terms weigh more
    qs=sorted([q for q in queries if q["citations"]>0], key=lambda q:-q["citations"])[:max_q]
    covered=[]; gaps=[]; strong_page=set()
    for q in qs:
        qt=_qtok(q["query"])
        if not qt: continue
        wsum=sum(w(t) for t in qt) or 1.0
        best_i,best_s=-1,0.0
        for i,toks in enumerate(ptoks):
            s=sum(w(t) for t in qt if t in toks)/wsum
            if s>best_s: best_i,best_s=i,s
        rec={"query":q["query"],"citations":q["citations"],"share":q.get("share"),
             "best_score":round(best_s,2),"best_page":(pages[best_i].get("path") or pages[best_i].get("url")) if best_i>=0 else None}
        if best_s>=0.6:
            covered.append(rec); strong_page.add(best_i)
        else:
            gaps.append(rec)
    orphans=sorted([{"path":p.get("path") or p.get("url"),"score":p.get("score")}
                    for i,p in enumerate(pages) if (p.get("score") or 0)>=75 and i not in strong_page],
                   key=lambda x:-(x["score"] or 0))
    gaps.sort(key=lambda x:-x["citations"])
    return {"n_queries":len(qs),"covered":len(covered),"n_gaps":len(gaps),"gaps":gaps[:12],
            "n_orphans":len(orphans),"orphans":orphans[:12]}

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
    # live reachability: probe the SERVING bots that fetch at CITATION time (not GPTBot, a training
    # bot). A WAF 403 here means the engine cannot fetch the page to cite it, even if robots "allows"
    # the bot. Googlebot/Bingbot blocked = a classic-search-indexing risk on top of the AI one.
    def _rprobe(item):
        nm,ua=item; s,_,_,_=fetch_raw(origin+"/",ua=ua)
        if s==429: time.sleep(1.5); s,_,_,_=fetch_raw(origin+"/",ua=ua)   # 429 = rate limit, often our own burst -> retry once
        return (nm,s)
    with ThreadPoolExecutor(max_workers=3) as _ex:                        # gentler burst so we do not self-trigger the WAF
        reach=list(_ex.map(_rprobe, SERVING_UAS))
    _hardblk=lambda s: s in (401,403) or s is None   # a real firewall deny
    bl=[b for b,s in reach if _hardblk(s)]
    rl=[b for b,s in reach if s==429]                # rate-limited: inconclusive, likely our own probe, NOT a policy block
    srch=[b for b,s in reach if _hardblk(s) and b in ("Googlebot","Bingbot")]
    _det="; ".join(f"{b}:{s if s is not None else 'x'}" for b,s in reach)
    if srch: _det+=" | search crawler blocked ("+", ".join(srch)+"): Google/Bing indexing risk, not just AI"
    if rl and not bl: _det+=" | "+", ".join(rl)+" rate-limited (429) after retry, likely our probe burst not a block"
    out.append(chk("reachability","bad" if bl else ("warn" if rl else "good"),_det))
    st2,_,llms,_=fetch_raw(origin+"/llms.txt")
    if st2==200 and llms:
        out.append(chk("llms","info","present"+("" if "](" in llms else " (no markdown links)")))
    else:
        out.append(chk("llms","info","not found"))
    return out

def all_urls(origin, domain, cap, start=None):
    """Discover page URLs: sitemaps (following sitemap-INDEX files into their child sitemaps,
    located via robots.txt 'Sitemap:' lines + common names), plus links from the homepage and
    the start page. Fixes two gaps: index sitemaps were read as if they were page lists, and
    only the homepage was link-crawled so entering a hub like /blog found nothing."""
    def same(u):
        p=urllib.parse.urlparse(u); pl=p.path.lower()
        return (p.scheme in ("http","https") and domain in p.netloc.replace("www.","")
                and not ASSET_RE.search(p.path)
                and not pl.endswith((".xml",".xml.gz",".md",".txt",".json",".yaml",".yml",".rss",".atom"))  # machine/agent files (agents.md, llms.txt, ...) are not scored pages
                and "/.well-known/" not in pl)
    # 1. locate sitemaps: robots.txt Sitemap: directives first, then common defaults
    sm_seed=[]
    _,_,rob,_=fetch_raw(origin+"/robots.txt")
    if rob: sm_seed += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rob)
    sm_seed += [origin+"/sitemap.xml", origin+"/sitemap_index.xml", origin+"/sitemap-index.xml", origin+"/wp-sitemap.xml"]
    # 2. fetch sitemaps, recursing one-or-more levels through index files, until we have enough page urls
    want=(cap*3 if cap and cap>0 else 5000)
    sm_urls=[]; seen_sm=set(); queue=list(dict.fromkeys(sm_seed)); fetched=0
    while queue and len(sm_urls)<want and fetched<300:
        smu=queue.pop(0)
        if smu in seen_sm: continue
        seen_sm.add(smu); fetched+=1
        st,_,body,_=fetch_raw(smu)
        if st!=200 or not body: continue
        locs=re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        if "<sitemapindex" in body.lower():                 # index -> every loc is a child sitemap
            for loc in locs:
                loc=loc.strip()
                if loc not in seen_sm: queue.append(loc)
        else:                                               # urlset -> locs are pages
            for loc in locs:
                loc=loc.strip()
                if same(loc): sm_urls.append(loc)
    # 3. links from the homepage AND the start page (so entering /blog discovers its articles)
    link_urls=[]
    hops=[origin+"/"]
    if start and same(start) and start.rstrip("/")!=(origin+"/").rstrip("/"): hops.append(start)
    for pg in dict.fromkeys(hops):
        _,_,raw,_=fetch_raw(pg)
        if raw:
            for a in BeautifulSoup(raw,"lxml").find_all("a",href=True):
                u=urllib.parse.urljoin(pg,a["href"].strip()); u,_=urllib.parse.urldefrag(u)
                if same(u): link_urls.append(u)
    # 4. assemble: homepage + start + sitemap pages (real content first) + discovered links, de-duped
    ordered=[origin+"/"]
    if start and same(start): ordered.append(start)
    ordered += sm_urls + link_urls
    seen=[]; done=set()
    for u in ordered:
        k=u.rstrip("/")
        if k not in done: done.add(k); seen.append(u)
    if cap and cap>0: seen=seen[:cap]
    sitemap_paths={(urllib.parse.urlparse(u).path.rstrip("/") or "/") for u in sm_urls}
    return seen, sitemap_paths

def _redirect_home(url, final_url):
    """Citation-to-landing flag: a deep URL that silently redirects to the site homepage
    wastes any AI citation of it. Returns final_url when that happened, else None.
    Deliberately ignores http->https, www, trailing-slash and deep->deep moves (all legitimate)."""
    try:
        rp=urllib.parse.urlparse(url); fp=urllib.parse.urlparse(final_url)
        req_path=(rp.path or "/").rstrip("/") or "/"; fin_path=(fp.path or "/").rstrip("/") or "/"
        same_host=fp.netloc.replace("www.","")==rp.netloc.replace("www.","")
        if same_host and req_path!="/" and fin_path=="/" and not fp.query:
            return final_url
    except Exception: pass
    return None

def process(url, domain):
    _cap={}
    st,hdrs,raw,fms=fetch_raw(url, capture=_cap)
    final_url=_cap.get("final_url") or url
    rend,rms=(render(url) if st==200 else (None,0))
    page=analyze(url,st,raw,rend,domain,hdrs,fms)
    page.update({"url":url,"status":st,"fetch_ms":fms,"render_ms":rms,
                 "depth":len([x for x in urllib.parse.urlparse(url).path.strip("/").split("/") if x]),
                 "server":hdrs.get("Server","")})
    rh=_redirect_home(url, final_url)
    if rh: page["redirect_home"]=rh
    return page

# ------------------------------------------------------------------ phase-3: near-dup + broken links
def _simhash(tokens, bits=64):
    if not tokens: return 0
    v=[0]*bits
    for t in tokens:
        h=int(hashlib.md5(t.encode("utf-8","ignore")).hexdigest(),16)
        for i in range(bits):
            v[i]+=1 if (h>>i)&1 else -1
    out=0
    for i in range(bits):
        if v[i]>0: out|=(1<<i)
    return out
def _hamming(a,b): return bin(a^b).count("1")

def check_links(pages, cap=800, workers=16, progress=None):
    """HEAD-check (GET fallback) the unique outbound links across the crawl, reusing statuses
    already known from crawled pages. Returns {url: status_or_None}. Bounded by `cap`.
    progress(done, total, msg) is called per link so a UI can show live status."""
    known={}
    for p in pages:
        if p.get("url") and p.get("status") is not None: known[p["url"].rstrip("/")]=p["status"]
    alllinks=set()
    for p in pages:
        for u in (p.get("outlinks") or []): alllinks.add(u)
    status={}
    for u in alllinks:
        k=u.rstrip("/")
        if k in known: status[u]=known[k]
    to_check=sorted(u for u in alllinks if u.rstrip("/") not in known)[:cap]
    def head(u):
        for method in ("HEAD","GET"):
            try:
                req=urllib.request.Request(u,method=method,headers={"User-Agent":UA})
                with urllib.request.urlopen(req,timeout=8) as r: return u,r.status
            except urllib.error.HTTPError as e:
                if method=="HEAD" and e.code in (403,405,406,501): continue
                return u,e.code
            except Exception:
                if method=="HEAD": continue
                return u,None
        return u,None
    if to_check:
        _tot=len(to_check); _done=0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for u,stt in ex.map(head,to_check):
                status[u]=stt; _done+=1
                if progress: progress(_done,_tot,f"{stt if stt is not None else 'x'} {u}")
    return status

def agent_protocols(origin):
    """Advisory probe of the emerging agent protocol / discovery files (Protocol & Interaction
    dimension of agent-readiness). Parallel, short timeout, returns {label: {found, path}}."""
    PATHS=[("MCP server card","/.well-known/mcp.json"),
           ("Plugin manifest","/.well-known/ai-plugin.json"),
           ("Agent manifest","/.well-known/agent.json"),
           ("Agent skills index","/agents.json"),
           ("OpenAPI / API catalog","/openapi.json"),
           ("OAuth discovery","/.well-known/oauth-authorization-server")]
    def probe(item):
        name,path=item
        st,_,body,_=fetch_raw(origin+path,timeout=8)
        return name,{"found":bool(st==200 and body),"path":path}
    out={}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for name,r in ex.map(probe,PATHS): out[name]=r
    return out

# ------------------------------------------------------------------ AI-crawler exposure monitor
# The major AI crawlers, split by ROLE: "serving" bots fetch to answer/cite live (blocking one
# removes you from that engine's answers); "training" bots feed model training (blocking is a
# legitimate content-protection choice that does NOT stop live-search citation).
AI_BOTS=[
 ("GPTBot","GPTBot","OpenAI","training","ChatGPT model training + browsing"),
 ("OAI-SearchBot","OAI-SearchBot","OpenAI","serving","ChatGPT search-result citations"),
 ("ChatGPT-User","ChatGPT-User","OpenAI","serving","ChatGPT user-triggered fetch"),
 ("ClaudeBot","ClaudeBot","Anthropic","serving","Claude training + answer citations"),
 ("anthropic-ai","anthropic-ai","Anthropic","training","Claude (legacy UA)"),
 ("Claude-User","Claude-User","Anthropic","serving","Claude user-triggered fetch"),
 ("Googlebot","Googlebot","Google","serving","Google Search + AI Overviews serving"),
 ("Google-Extended","Google-Extended","Google","training","Gemini / Vertex training (not AIO serving)"),
 ("PerplexityBot","PerplexityBot","Perplexity","serving","Perplexity answer index"),
 ("Perplexity-User","Perplexity-User","Perplexity","serving","Perplexity user-triggered fetch"),
 ("Bingbot","Bingbot","Microsoft","serving","Copilot + Bing AI answers"),
 ("Applebot-Extended","Applebot-Extended","Apple","training","Apple Intelligence training"),
 ("Meta-ExternalAgent","Meta-ExternalAgent","Meta","training","Meta AI training / serving"),
 ("Bytespider","Bytespider","ByteDance","training","TikTok / Doubao AI"),
 ("CCBot","CCBot","Common Crawl","training","open corpus feeding many LLMs"),
]
# The bots that fetch at CITATION time (serving), with realistic UA strings for the live WAF probe.
# GPTBot is deliberately EXCLUDED: it is a training crawler, so probing it misleads (a site can
# allow GPTBot yet firewall-block ChatGPT-User, the bot that actually fetches to cite). Googlebot
# and Bingbot are included because a WAF "block AI training" rule can 403 them too (indexing risk).
SERVING_UAS=[
 ("ChatGPT-User","ChatGPT-User/1.0 (+https://openai.com/bot)"),
 ("OAI-SearchBot","OAI-SearchBot/1.0 (+https://openai.com/searchbot)"),
 ("PerplexityBot","Mozilla/5.0 (compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"),
 ("ClaudeBot","Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
 ("Googlebot","Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"),
 ("Bingbot","Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"),
]
def _parse_robots(text):
    """Parse robots.txt into User-agent groups: [{agents:set, dis:[...], allow:[...]}]. Consecutive
    User-agent lines (before any rule) share the following ruleset per the standard."""
    groups=[]; cur=None; after_rule=False
    for raw in (text or "").splitlines():
        line=raw.split("#",1)[0].strip()
        if not line or ":" not in line: continue
        field,_,val=line.partition(":"); field=field.strip().lower(); val=val.strip()
        if field=="user-agent":
            if cur is None or after_rule:
                cur={"agents":set(),"dis":[],"allow":[]}; groups.append(cur)
            cur["agents"].add(val.lower()); after_rule=False
        elif field=="disallow" and cur is not None:
            cur["dis"].append(val); after_rule=True
        elif field=="allow" and cur is not None:
            cur["allow"].append(val); after_rule=True
    return groups
def _bot_status(groups, ua):
    """allowed / partial / blocked for a bot UA: exact group match wins, else the * fallback."""
    ua=ua.lower(); grp=None
    for g in groups:
        if ua in g["agents"]: grp=g; break
    if grp is None:
        for g in groups:
            if "*" in g["agents"]: grp=g; break
    if grp is None: return "allowed"
    if any((a or "").strip()=="/" for a in grp["allow"]): return "allowed"   # explicit root Allow wins the tie
    if any((d or "").strip()=="/" for d in grp["dis"]): return "blocked"
    if any((d or "").strip() for d in grp["dis"]): return "partial"
    return "allowed"
def ai_crawler_matrix(origin):
    """AI-bot access matrix: robots.txt rules for every bot, PLUS a live reachability probe of the
    citation-serving bots, so the tab can show robots-allowed-but-firewall-blocked. Per bot, `reach`
    is the HTTP status fetching the homepage as that bot (0 = unreachable/failed; null = not probed)."""
    st,_,robots,_=fetch_raw(origin+"/robots.txt")
    groups=_parse_robots(robots)
    _uamap=dict(SERVING_UAS)
    def _probe(nm):
        s,_,_,_=fetch_raw(origin+"/",ua=_uamap[nm])
        if s==429:                              # 429 = rate limit, usually our own concurrent burst -> back off + retry once
            time.sleep(1.5); s,_,_,_=fetch_raw(origin+"/",ua=_uamap[nm])
        return (nm, s if s is not None else 0)
    reachmap={}
    with ThreadPoolExecutor(max_workers=3) as ex:   # gentler burst so we do not self-trigger the WAF rate limit
        for nm,s in ex.map(_probe,[n for n,_ in SERVING_UAS]): reachmap[nm]=s
    bots=[{"name":n,"ua":ua,"op":op,"role":role,"purpose":pur,
           "status":_bot_status(groups,ua),"reach":reachmap.get(n)} for n,ua,op,role,pur in AI_BOTS]
    return {"has_robots":bool(st==200 and robots),"bots":bots}

# ------------------------------------------------------------------ log analysis (bring-your-own access log)
_LOG_LINE=re.compile(r'"[A-Z]+\s+(\S+)\s+[^"]*"\s+(\d{3})\s+\S+\s+"[^"]*"\s+"([^"]*)"')  # Apache/Nginx combined
_LOG_DAY=re.compile(r'\[(\d{2})/([A-Za-z]{3})/(\d{4})')
def load_access_log(path, max_lines=3_000_000):
    """Parse a server access log (Apache/Nginx combined format). Returns [(ua, url, status, 'YYYY-Mon-DD')]."""
    out=[]
    if not path or not os.path.exists(path): return out
    with open(path, encoding="utf-8", errors="ignore") as f:
        for i,ln in enumerate(f):
            if i>=max_lines: break
            m=_LOG_LINE.search(ln)
            if not m: continue
            dm=_LOG_DAY.search(ln)
            out.append((m.group(3), m.group(1), int(m.group(2)),
                        f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""))
    return out

def analyze_logs(path):
    """Filter an access log to the AI bots and report what REALLY happened: which AI bots fetched, how
    often, with what status, and where. This is ground truth the reachability probe only estimates:
    citation-time bots (ChatGPT-User, Perplexity-User, Claude-User) fetching = live citation activity."""
    rows=load_access_log(path)
    if not rows: return {"parsed":0,"bots":{}}
    CITE_TIME={"ChatGPT-User","Perplexity-User","Claude-User","OAI-SearchBot"}
    bots={}
    for name,ua_id,op,role,pur in AI_BOTS:
        pat=re.compile(re.escape(ua_id), re.I)
        hits=[r for r in rows if pat.search(r[0])]
        if not hits: continue
        st=Counter(r[2] for r in hits); urls=Counter(r[1] for r in hits)
        bots[name]={"hits":len(hits),"role":role,"op":op,"purpose":pur,
                    "citation_time":name in CITE_TIME,
                    "statuses":dict(sorted(st.items())),
                    "blocked":sum(v for s,v in st.items() if s in (401,403,429)),
                    "top_urls":[[u,c] for u,c in urls.most_common(8)],
                    "days_seen":len({r[3] for r in hits if r[3]})}
    return {"parsed":len(rows),"total_bot_hits":sum(b["hits"] for b in bots.values()),
            "bots":dict(sorted(bots.items(), key=lambda kv:-kv[1]["hits"]))}

# ------------------------------------------------------------------ log MONITOR (time-series, ground truth)
_MON={"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06",
      "Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
def _isodate(d):
    """'YYYY-Mon-DD' (load_access_log format) -> sortable 'YYYY-MM-DD'; '' if unparseable."""
    try:
        y,mon,day=d.split("-"); return f"{y}-{_MON.get(mon,'00')}-{int(day):02d}"
    except Exception: return ""

def _summarize_monitor(merged, CITE_TIME, parsed, history_path, hist_loaded):
    """Turn a {isodate: {bot: {hits,blocked,cite}}} map into the monitor view (trends, alerts, per-bot series)."""
    dates=sorted(d for d in merged if d)
    bots={}; daily_totals={}
    for iso in dates:
        tot={"ai_hits":0,"cite":0,"blocked":0}
        for bot,c in merged[iso].items():
            b=bots.setdefault(bot,{"hits":0,"blocked":0,"cite":0,"days":set(),"daily":{},
                                   "citation_time":bot in CITE_TIME,"first":iso,"last":iso})
            b["hits"]+=c.get("hits",0); b["blocked"]+=c.get("blocked",0); b["cite"]+=c.get("cite",0)
            b["days"].add(iso); b["daily"][iso]=c.get("hits",0)
            b["first"]=min(b["first"],iso); b["last"]=max(b["last"],iso)
            tot["ai_hits"]+=c.get("hits",0); tot["cite"]+=c.get("cite",0); tot["blocked"]+=c.get("blocked",0)
        daily_totals[iso]=tot
    n=len(dates)
    def per_day(sub): return round(sum(daily_totals[d]["ai_hits"] for d in sub)/max(1,len(sub)),1)
    trend={}
    if n>=2:
        h=n//2 or 1; fh=dates[:h]; sh=dates[h:]
        a=per_day(fh); b=per_day(sh)
        trend={"first_half_per_day":a,"second_half_per_day":b,
               "direction":"rising" if b>a*1.15 else ("falling" if b<a*0.85 else "flat")}
    total_ai=sum(t["ai_hits"] for t in daily_totals.values())
    total_cite=sum(t["cite"] for t in daily_totals.values())
    total_block=sum(t["blocked"] for t in daily_totals.values())
    alerts=[]
    for bot,b in bots.items():
        if b["blocked"]>0:
            alerts.append(f"{bot} blocked {b['blocked']}x (401/403/429) over {len(b['days'])} day(s) - a real block, not an estimate")
    if n>=1:
        last=dates[-1]
        prior=set().union(*[set(merged[d]) for d in dates[:-1]]) if n>1 else set()
        for bot in merged[last]:
            if bot not in prior: alerts.append(f"NEW AI bot first seen {last}: {bot}")
    outb={}
    for bot,b in sorted(bots.items(), key=lambda kv:-kv[1]["hits"]):
        outb[bot]={"hits":b["hits"],"blocked":b["blocked"],"citation_time_hits":b["cite"],
                   "citation_time":b["citation_time"],"days_active":len(b["days"]),
                   "first_seen":b["first"],"last_seen":b["last"],
                   "daily":{d:b["daily"].get(d,0) for d in dates}}
    return {"tool":"CITED Score log monitor","window":{"first":dates[0] if dates else None,
            "last":dates[-1] if dates else None,"days":n},
            "parsed_lines":parsed,"history_path":history_path,"history_days_carried":hist_loaded,
            "total_ai_hits":total_ai,"citation_time_hits":total_cite,
            "citation_time_rate_pct":round(100*total_cite/total_ai,1) if total_ai else 0,
            "blocked_hits":total_block,"trend":trend,"alerts":alerts,
            "daily_totals":daily_totals,"bots":outb}

def monitor_logs(path, history_path=None):
    """Standing log MONITOR — extends analyze_logs into a time series. Buckets AI-bot hits by DAY from the
    log's own timestamps and tracks per-bot crawl frequency, citation-time fetch rate (ChatGPT-User /
    Perplexity-User / Claude-User / OAI-SearchBot = live citation activity), blocks, and new-bot arrivals
    over time. If history_path is given, merges with prior uploads into a persistent per-day JSONL so
    successive logs accumulate (this log is authoritative for the dates it covers, so overlapping
    re-uploads never double-count). Parsed entirely LOCALLY. Returns a JSON-friendly dict."""
    rows=load_access_log(path)
    CITE_TIME={"ChatGPT-User","Perplexity-User","Claude-User","OAI-SearchBot"}
    matchers=[(name,re.compile(re.escape(ua_id),re.I)) for name,ua_id,op,role,pur in AI_BOTS]
    day_bot={}
    for ua,url,status,day in rows:
        iso=_isodate(day)
        if not iso: continue
        for name,pat in matchers:
            if pat.search(ua):
                d=day_bot.setdefault(iso,{}).setdefault(name,{"hits":0,"blocked":0,"cite":0})
                d["hits"]+=1
                if status in (401,403,429): d["blocked"]+=1
                if name in CITE_TIME: d["cite"]+=1
                break
    merged={k:dict(v) for k,v in day_bot.items()}
    hist_loaded=0
    if history_path:
        try:
            with open(history_path,encoding="utf-8") as f:
                for ln in f:
                    ln=ln.strip()
                    if not ln: continue
                    rec=json.loads(ln); iso=rec.get("date")
                    if iso and iso not in day_bot:            # keep prior days we didn't just re-ingest
                        merged[iso]=rec.get("bots",{}); hist_loaded+=1
        except FileNotFoundError: pass
        except Exception: pass
        try:                                                 # persist merged history, one line per date
            with open(history_path,"w",encoding="utf-8") as f:
                for iso in sorted(merged):
                    f.write(json.dumps({"date":iso,"bots":merged[iso]})+"\n")
        except Exception: pass
    return _summarize_monitor(merged, CITE_TIME, len(rows), history_path, hist_loaded)

def check_draft(content, url="https://draft.local/page"):
    """Pre-publish citability check: score arbitrary draft HTML the way a crawled page is scored, and
    return the content/extractability checks + the fixes, so an AI can lint a draft BEFORE it ships
    (no waiting weeks for citation lag). Takes the content as INPUT — source-agnostic (file, paste, or
    another tool's output). Pass HTML for the full check; plain text still yields the prose-level checks."""
    html = content if "<" in (content or "") else "<html><body>"+ "".join(
        f"<p>{p.strip()}</p>" for p in re.split(r"\n\s*\n", content or "") if p.strip()) +"</body></html>"
    dom=urllib.parse.urlparse(url).netloc.replace("www.","") or "draft.local"
    page=analyze(url, 200, html, html, dom)
    # Lint only what the WRITER controls in the draft body. Publish-wrapper checks (title/meta, schema,
    # date/freshness, byline, canonical, server) are set by the CMS/template at publish, not the draft.
    DRAFT_SKIP={"http","title","meta","canonical","noindex","speed","schema","schemavalidity",
                "parity","schemacomplete","entity","faq","reviewschema","freshness","author"}
    checks=[c for c in page.get("checks",[]) if c.get("id") not in DRAFT_SKIP and c.get("status")!="na"]
    fixes=[{"check":c["label"],"status":c["status"],"detail":c.get("detail",""),"why":c.get("ev","")}
           for c in checks if c["status"] in ("bad","warn")]
    bad=sum(1 for c in checks if c["status"]=="bad")
    passing=sum(1 for c in checks if c["status"]=="good")
    verdict=("weak - restructure before publishing" if bad>=3
             else "close - a few fixes from citable" if fixes else "citable-ready")
    return {"tool":"CITED Score check_draft","url":url,"page_type":page.get("type"),
            "words":page.get("metrics",{}).get("words"),"verdict":verdict,
            "passing_checks":passing,"bad":bad,"fix_count":len(fixes),"fixes":fixes,
            "note":"Lints the draft body's extractability; publish-wrapper checks (title, meta, schema, date, author) are excluded and handled at publish."}

# ------------------------------------------------------------------ reasoning tools (compose over data)
def click_resilience(page):
    """Per-page CLICK-RESILIENCE band: will AI still send a click, or does it answer inline? Derived from
    signals CITED Score already computes (page type, info-gain / original data, actionable schema, tables,
    definitional opener). Directional proxy, honestly labelled. Pass a processed page (from process())."""
    checks={c.get("id"):c.get("status") for c in (page.get("checks") or []) if c.get("id")}
    ig=page.get("infogain") or {}
    agent=page.get("agent") or {}
    typ=page.get("type"); _w=(page.get("metrics") or {}).get("words",0) or 0
    wc=_w if isinstance(_w,(int,float)) else 0
    figs=ig.get("figures",0) if isinstance(ig.get("figures",0),(int,float)) else 0; reasons=[]; score=0
    if figs>=15 or ig.get("firsthand") or ig.get("proprietary"):
        score+=2; reasons.append("original data / first-hand research the AI answer can't fully replace")
    if ig.get("datatable") or checks.get("liststables")=="good":
        score+=1; reasons.append("tables/lists the user may come back to")
    if typ in ("product","category"):
        score+=2; reasons.append("transactional page - the user must visit the site to buy")
    if any(agent.values()):
        score+=1; reasons.append("actionable/transactable signals (offer/price/action)")
    if checks.get("definitional")=="good" and checks.get("statdensity")=="bad" and not ig.get("datatable"):
        score-=2; reasons.append("a standalone definition with no unique data - AI answers it inline")
    if typ in ("article","page") and wc<600 and checks.get("liststables")!="good" and figs<6:
        score-=1; reasons.append("short generic explainer - little the AI can't just summarise")
    band="high" if score>=2 else ("low" if score<=-1 else "medium")
    advice={"high":"AI will cite you but users still need to visit - protect and expand this page.",
            "medium":"Mixed - part is answerable inline; strengthen the parts only your page provides (data, tools, specifics).",
            "low":"AI likely answers this fully - low click value; don't over-invest, or add original data / tools / interactivity to earn the click."}[band]
    return {"tool":"CITED Score click_resilience","url":page.get("url"),"type":typ,"words":wc,
            "band":band,"signal_score":score,"reasons":reasons,"advice":advice}

def correlate_data(pages, cites=None, logrows=None):
    """Deterministic per-URL JOIN of a crawl with citation counts (cites: {url:count}) and server-log AI-bot
    activity (logrows from load_access_log). Reliable join so the analysis doesn't depend on the model
    stitching three sources together. Returns joined rows + plain-language insights."""
    cites=cites or {}
    log_hits={}
    if logrows:
        matchers=[(name,re.compile(re.escape(ua_id),re.I)) for name,ua_id,op,role,pur in AI_BOTS]
        CITE_TIME={"ChatGPT-User","Perplexity-User","Claude-User","OAI-SearchBot"}
        for ua,u,status,day in logrows:
            for name,pat in matchers:
                if pat.search(ua):
                    h=log_hits.setdefault(u.rstrip("/"),{"ai":0,"cite":0}); h["ai"]+=1
                    if name in CITE_TIME: h["cite"]+=1
                    break
    rows=[]
    for p in pages:
        if p.get("status")!=200: continue
        k=p["url"].rstrip("/"); lh=log_hits.get(k) or {}
        rows.append({"url":p["url"],"type":p.get("type"),"score":p.get("score"),
                     "citations":cites.get(k),"ai_fetches":lh.get("ai"),"citation_time_fetches":lh.get("cite")})
    def _avg(xs): xs=[x for x in xs if x is not None]; return round(sum(xs)/len(xs),1) if xs else None
    insights=[]
    cited=[r for r in rows if (r["citations"] or 0)>0]
    uncited=[r for r in rows if r["citations"]==0]
    if cited and uncited:
        ac=_avg([r["score"] for r in cited]); au=_avg([r["score"] for r in uncited])
        if ac is not None and au is not None:
            insights.append(f"Cited pages average {ac}/100 vs {au}/100 uncited (a {round(ac-au,1):+} gap).")
    opp=[r for r in rows if r["citations"]==0 and (r["score"] or 0)>=75]
    if opp: insights.append(f"{len(opp)} well-scored pages (score >=75) earn ZERO citations - the clearest opportunity.")
    fnc=[r for r in rows if (r["ai_fetches"] or 0)>0 and not (r["citations"] or 0)]
    if fnc: insights.append(f"{len(fnc)} pages AI FETCHED but did not cite - retrieved yet not chosen; check framing/answerability.")
    return {"tool":"CITED Score correlate","joined":len(rows),"have_citations":bool(cites),"have_logs":bool(logrows),
            "insights":insights,
            "top_cited":sorted(cited,key=lambda r:-(r["citations"] or 0))[:15],
            "opportunities":sorted(opp,key=lambda r:-(r["score"] or 0))[:15]}

def estimate_ai_influence(ai_sessions=0, total_citations=0, ai_revenue=0.0, avg_deal_value=0.0,
                          close_rate=0.0, conversion_rate=0.0, self_reported_ai=0, period_days=30):
    """Honest, RANGED estimate of AI-INFLUENCED value (never 'attribution'). Takes numbers the client's AI
    can pull from GA4 (AI-Assistant channel sessions + revenue), citations (Bing/Clarity), and an optional
    self-report anchor. The signal is fundamentally weak; this assembles it honestly and never fakes precision."""
    assume=[]; notes=[]
    ev_per_lead=(avg_deal_value*close_rate) if (avg_deal_value and close_rate) else avg_deal_value
    if ai_revenue>0:
        floor=float(ai_revenue); basis="GA4 AI-Assistant channel revenue (measured, ecommerce)"
    elif ai_sessions and conversion_rate and ev_per_lead:
        conv=ai_sessions*conversion_rate; floor=conv*ev_per_lead
        basis=f"{ai_sessions} AI sessions x {round(conversion_rate*100,2)}% conv x EV/lead {ev_per_lead:g} = {round(conv,1)} leads"
    else:
        floor=0.0; basis="no measurable AI revenue/leads supplied (floor = 0)"
        notes.append("For a value floor, pass ai_revenue OR (ai_sessions + conversion_rate + avg_deal_value [+ close_rate]).")
    lo_f, hi_f = 1/0.20, 1/0.10                    # 80-90% mis-tag -> measured = 10-20% of true -> 5x..10x
    est_low, est_mid, est_high = floor, floor*((lo_f+hi_f)/2), floor*hi_f
    if floor>0:
        assume.append("Mis-tagging: 80-90% of AI-driven visits mis-tag as direct/organic (Khim/beomniscient), so the measured channel is ~10-20% of true AI-driven activity -> true ~5-10x the floor.")
    anchor=None
    if self_reported_ai and ev_per_lead:
        anchor=self_reported_ai*ev_per_lead
        assume.append(f"Self-report anchor: {self_reported_ai} leads said 'AI recommended us' x EV/lead {ev_per_lead:g} = {round(anchor)} (independent, more honest than the channel estimate).")
    iceberg=None
    if total_citations:
        iceberg=round(total_citations/2455)                # observed ~1 click / 2,455 citations
        notes.append(f"{int(total_citations):,} citations at ~1 click / 2,455 = ~{iceberg} clicks; the rest is zero-click INFLUENCE, not traffic.")
    return {"tool":"CITED Score estimate_ai_influence","period_days":period_days,
            "measured_floor":round(floor,2),"floor_basis":basis,
            "estimate_low":round(est_low,2),"estimate_mid":round(est_mid,2),"estimate_high":round(est_high,2),
            "self_report_anchor":(round(anchor,2) if anchor is not None else None),
            "citation_click_estimate":iceberg,"assumptions":assume,"notes":notes,
            "caveats":["An ESTIMATE with ranges, never 'attribution' - AI passes no referrer and GA4 under-counts.",
                       "Citations are visibility, not clicks or revenue; AI value is INFLUENCED, not attributed.",
                       "Ranges stay wide without the self-report anchor; instrument a 'did AI recommend us?' field to tighten them."]}

def actionable_readiness(data):
    """Scored 'Actionable' readiness (agent-transaction test): could an AI AGENT complete a task on the
    money pages? Derived from the agent-ready signals already crawled. Deliberately a STANDALONE advisory
    module, NOT folded into the core Known/Findable/Trusted score - graduate it to a real 4th pillar only
    once agentic commerce is mainstream ('declared but unread' risk today)."""
    ag=data.get("agentready") or {}; sig=ag.get("signals") or {}; money=ag.get("money_n") or 0
    protocols=ag.get("protocols") or {}
    if not money:
        return {"tool":"CITED Score actionable_readiness","score":None,"band":None,"money_pages":0,
                "note":"No transactable / money pages detected - agent-transaction readiness is N/A for this site."}
    weights={"offer":1.5,"price":1.5,"availability":1.0,"action":1.5,"contact":1.0,"prodserv":1.0}
    got=tot=0.0; gaps=[]
    for k,w in weights.items():
        s=sig.get(k) or {}; has=len(s.get("has") or []); miss=len(s.get("missing_money") or []); base=has+miss
        if base<=0: continue
        cover=has/base; tot+=w; got+=w*cover
        if cover<0.8 and miss: gaps.append({"signal":k,"missing_on_money_pages":miss,"coverage_pct":round(cover*100)})
    score=round(100*got/tot) if tot else None
    proto_any=any((v or {}).get("found") for v in protocols.values()) if protocols else False
    band=None if score is None else ("high" if score>=75 else "medium" if score>=45 else "low")
    return {"tool":"CITED Score actionable_readiness","score":score,"band":band,"money_pages":money,
            "agent_protocol_files":proto_any,"gaps":sorted(gaps,key=lambda g:-g["missing_on_money_pages"]),
            "note":"Advisory 'Actionable' pillar (can an AI agent transact here?) - NOT in the core score; the missing signals are what stops an agent completing a purchase/booking/contact. Watch agentic-commerce adoption before graduating it."}

def scan_ai_answer(answer_text, brand, domain="", competitors=""):
    """Mechanical layer of the AI GROUND-TRUTH audit: given a consumer-AI answer (pasted), does the BRAND
    appear, is its DOMAIN cited, and which COMPETITORS are named? The qualitative framing read stays with
    the model/human - this just does the deterministic presence checks so a service run is repeatable."""
    t=(answer_text or ""); tl=t.lower()
    brand_mentioned=bool(brand) and brand.lower() in tl
    domain_cited=bool(domain) and domain.lower().replace("https://","").replace("http://","").replace("www.","").rstrip("/") in tl
    comp=[c.strip() for c in re.split(r"[,\n]", competitors or "") if c.strip()]
    named=[c for c in comp if c.lower() in tl]
    # rough position: how early the brand appears (0-1, earlier = more prominent), None if absent
    pos=None
    if brand_mentioned:
        i=tl.find(brand.lower()); pos=round(i/max(1,len(tl)),3)
    return {"tool":"CITED Score scan_ai_answer","brand":brand,"brand_mentioned":brand_mentioned,
            "domain_cited":domain_cited,"competitors_named":named,"competitor_count":len(named),
            "brand_position":pos,"answer_chars":len(t),
            "note":"Deterministic presence check only. For FRAMING (sentiment/positioning/caveats) and WHO WINS, have the model read the answer - this tool does not judge quality."}

# ------------------------------------------------------------------ scoring
def _score(statuses, weights, mult=None):
    tot=got=0.0
    for cid,w in weights.items():
        s=statuses.get(cid)
        if not s or s in ("na","info"): continue
        m=mult.get(cid,1.0) if mult else 1.0          # website-type profile re-weight (1.0 = default)
        tot+=w*m; got+=w*m*STAT[s]
    return round(100*got/tot) if tot else None

def page_scores(statuses, mult=None):
    """statuses: {id: good/warn/bad/na/info} incl site ids. -> overall, pillars, engines.
    mult (optional): website-type profile weight multipliers {check_id: factor}; None = default rubric."""
    overall = _score(statuses, {c:1 for c in statuses if c not in SITE_IDS}, mult)
    pill={}
    for p in PILLARS:
        w={cid:1 for cid,m in CHECK_META.items() if m["pillar"]==p and cid in statuses}
        pill[p]=_score(statuses,w,mult)
    eng={e:_score(statuses,w,mult) for e,w in ENGINE_WEIGHTS.items()}
    return (overall if overall is not None else 0,
            {k:(v if v is not None else 0) for k,v in pill.items()},
            {k:(v if v is not None else 0) for k,v in eng.items()})

def build(domain, origin, pages, sitecx, sitemap_paths=None, linkstatus=None, client=None, intro=None, protocols=None, aicrawler=None, site_type_override=None, agency=None, logo=None):
    ok200=[p for p in pages if p.get("status")==200]
    site_type=(site_type_override if site_type_override in SITE_PROFILE_LABEL else classify_site(pages)); prof=PROFILES.get(site_type)   # override or auto-detect; None profile = general / no-op
    site_type_source="override" if site_type_override in SITE_PROFILE_LABEL else "auto"
    def _np(x): return (x or "/").rstrip("/") or "/"
    # site-level: does the site publish comparison / best-of content (a top AI-cited format)?
    CMP_RE=re.compile(r"(vs\.?|versus|comparison|compare|best[- ]|top[- ]?\d+|alternativ|which[- ])",re.I)
    _cmp=[p for p in pages if CMP_RE.search((p.get("path") or "")+" "+(p.get("title") or ""))]
    _cc=chk("comparison","good" if _cmp else "warn",(str(len(_cmp))+" comparison/best-of page(s)") if _cmp else "no comparison / best-of content found")
    _cc["urls"]=[p.get("url") for p in _cmp if p.get("url")]
    sitecx=list(sitecx)+[_cc]
    scx={c["id"]:c["status"] for c in sitecx}
    # per-page injected checks (need the whole page set): orphan/missing-from-sitemap + duplicate title/meta
    _linked=set()
    for p in pages: _linked|=set(p.get("links") or [])
    _titles=Counter(t for t in ((p.get("title") or "").strip().lower() for p in ok200) if t)
    _metas=Counter(m for m in ((p.get("meta") or "").strip().lower() for p in ok200) if m)
    # near-duplicate fingerprints: exact (md5) + near (simhash Hamming <= 3)
    _bychash=defaultdict(list)
    for p in ok200:
        if p.get("chash"): _bychash[p["chash"]].append(p.get("url"))
    _near=defaultdict(list); _simp=[p for p in ok200 if p.get("simhash")]
    if len(_simp)<=1500:
        for _i in range(len(_simp)):
            for _j in range(_i+1,len(_simp)):
                _a=_simp[_i]; _b=_simp[_j]
                if _a.get("chash") and _a.get("chash")==_b.get("chash"): continue
                if _hamming(_a["simhash"],_b["simhash"])<=3:
                    _near[_a.get("url")].append(_b.get("url")); _near[_b.get("url")].append(_a.get("url"))
    # broken outbound links (linkstatus empty on the benchmark path -> na). Only count genuinely-dead
    # targets: 404/410 (gone) + 5xx (server error). 401/403/429/None = bot-block or transient, NOT dead.
    def _brk(s): return isinstance(s,int) and (s in (404,410) or 500<=s<600)
    _brokenmap=defaultdict(list)
    for p in pages:
        _bad=[u for u in (p.get("outlinks") or []) if linkstatus and _brk(linkstatus.get(u))]
        p["_broken"]=_bad
        for u in _bad: _brokenmap[u].append(p.get("url"))
    for p in pages:
        _tt=(p.get("title") or "").strip().lower(); _md=(p.get("meta") or "").strip().lower()
        if _tt and _titles[_tt]>1: p["checks"].append(chk("duplicate","bad",f"title shared with {_titles[_tt]-1} other page(s)"))
        elif _md and _metas[_md]>1: p["checks"].append(chk("duplicate","warn",f"meta shared with {_metas[_md]-1} other page(s)"))
        else: p["checks"].append(chk("duplicate","good","unique title & meta"))
        _pp=_np(p.get("path"))
        if p.get("status")==200 and _pp not in ("","/"):
            _unl=_pp not in _linked; _mis=bool(sitemap_paths) and _pp not in sitemap_paths
            if _unl or _mis:
                _r=[]
                if _unl: _r.append("nothing links to it")
                if _mis: _r.append("not in sitemap")
                p["checks"].append(chk("orphans","warn"," + ".join(_r)))
            else:
                p["checks"].append(chk("orphans","good","linked"+(" and in sitemap" if sitemap_paths else "")))
        else:
            p["checks"].append(chk("orphans","good","home / entry page"))
        _ex=[u for u in _bychash.get(p.get("chash"),[]) if u!=p.get("url")]
        _nr=_near.get(p.get("url"),[])
        if p.get("status")==200 and _ex: p["checks"].append(chk("nearduplicate","bad",f"exact duplicate of {len(_ex)} page(s)"))
        elif p.get("status")==200 and _nr: p["checks"].append(chk("nearduplicate","warn",f"near-duplicate of {len(_nr)} page(s)"))
        else: p["checks"].append(chk("nearduplicate","good","distinct content"))
        if not linkstatus: p["checks"].append(chk("brokenlinks","na","not checked"))
        else:
            _nb=p.get("_broken") or []
            if not _nb: p["checks"].append(chk("brokenlinks","good","no broken outbound links"))
            elif len(_nb)<=2: p["checks"].append(chk("brokenlinks","warn",str(len(_nb))+" broken: "+", ".join(u.split("://")[-1][:38] for u in _nb[:2])))
            else: p["checks"].append(chk("brokenlinks","bad",str(len(_nb))+" broken outbound links"))
        st={c["id"]:c["status"] for c in p["checks"]}; st.update(scx)
        o,pl,en=page_scores(st, prof)
        p["cs"]=st; p["score"]=o; p["pillars"]=pl; p["engines"]=en
    broken_links=[{"url":u,"status":(linkstatus or {}).get(u),"sources":srcs} for u,srcs in _brokenmap.items()]
    broken_links.sort(key=lambda x:-len(x["sources"]))
    # ---- sitemap health: cross-reference the crawl x the XML sitemap x the internal-link graph.
    # AI/engines discover + trust pages that are BOTH in the sitemap AND internally linked; the buckets below
    # are the actionable gaps (add to sitemap / add internal links / remove noindexed URLs from the sitemap).
    sitemap={"has_sitemap":bool(sitemap_paths),"sitemap_urls":len(sitemap_paths or set()),"crawled":len(ok200)}
    if sitemap_paths:
        def _idx(p): return next((c["status"] for c in p.get("checks",[]) if c["id"]=="noindex"),"good")=="good"
        _crawled_paths={_np(p.get("path")) for p in ok200}
        _miss=sorted({(p.get("path") or p.get("url")) for p in ok200
                      if _np(p.get("path")) not in ("","/") and _np(p.get("path")) not in sitemap_paths and _idx(p)})
        _nox=sorted({(p.get("path") or p.get("url")) for p in ok200
                     if _np(p.get("path")) in sitemap_paths and not _idx(p)})
        _orl=sorted({(p.get("path") or p.get("url")) for p in ok200
                     if _np(p.get("path")) not in ("","/") and _np(p.get("path")) not in _linked})
        _ncr=sorted(sitemap_paths - _crawled_paths)
        sitemap.update({"missing_from_sitemap":_miss[:100],"missing_n":len(_miss),
                        "noindex_in_sitemap":_nox[:100],"noindex_n":len(_nox),
                        "orphan_no_internal_links":_orl[:100],"orphan_n":len(_orl),
                        "in_sitemap_not_crawled":_ncr[:100],"not_crawled_n":len(_ncr)})
    # off-page advisory: which authoritative third-party surfaces the brand DECLARES (schema sameAs)
    _allsame=sorted(set(u for p in pages for u in (p.get("sameas") or [])))
    _SURF=[("LinkedIn","linkedin.com"),("Crunchbase","crunchbase.com"),("Wikipedia","wikipedia.org"),
           ("YouTube","youtube.com"),("X / Twitter","twitter.com"),("Reddit","reddit.com"),("G2","g2.com"),
           ("Trustpilot","trustpilot.com"),("Capterra","capterra.com"),("GitHub","github.com")]
    def _decl(dm,nm):
        keys=[dm]+(["x.com"] if nm=="X / Twitter" else [])
        return any(any(k in u.lower() for k in keys) for u in _allsame)
    _declared=[nm for nm,dm in _SURF if _decl(dm,nm)]
    _HV=["Wikipedia","Reddit","YouTube","Trustpilot","G2","LinkedIn"]
    offpage={"declared":_declared,"missing":[s for s in _HV if s not in _declared],"sameas_count":len(_allsame)}
    # agent-readiness advisory: which pages expose each ACTIONABLE signal + which COMMERCIAL pages lack it.
    # "Money page" = a transactable page (a blog does not need an Offer; a service / pricing page does).
    _agkeys=["action","offer","price","avail","prodserv","contact"]
    _UTIL_RE=re.compile(r"(?:^|/)(?:about|privacy|terms|cookie|legal|sitemap|404|thank|thanks|login|log-in|account|cart|search|category|tag|author)(?:/|$|-)",re.I)
    def _ismoney(p):
        tset=set((p.get("metrics") or {}).get("schema_types") or []); ag=p.get("agent") or {}
        if {"Product","Service","Offer","SoftwareApplication","WebApplication","Event","Course"} & tset or ag.get("prodserv") or ag.get("offer"): return True
        if p.get("type") in ("article","listing"): return False              # editorial: not a transaction surface
        if _UTIL_RE.search(urllib.parse.urlparse((p.get("url") or "")).path.lower()): return False
        return True                                                          # a non-editorial, non-utility landing page is commercially actionable
    _money=[p for p in ok200 if _ismoney(p)]
    _agcount={k:sum(1 for p in ok200 if (p.get("agent") or {}).get(k)) for k in _agkeys}
    _agsignals={k:{"has":[p.get("url") for p in ok200 if (p.get("agent") or {}).get(k)][:80],
                   "missing_money":[p.get("url") for p in _money if not (p.get("agent") or {}).get(k)][:80]} for k in _agkeys}
    _agany=[p.get("url") for p in ok200 if any((p.get("agent") or {}).values())]
    agentready={"counts":_agcount,"total":len(ok200),"pages_any":_agany[:60],"any_n":len(_agany),
                "protocols":protocols or {},"signals":_agsignals,"money_n":len(_money),"money_urls":[p.get("url") for p in _money][:120]}
    # information-gain proxy (advisory): band each page from original-data signals; near-duplicate = derivative -> low.
    _igp=[]
    for p in ok200:
        ig=p.get("infogain") or {}
        _dup=bool([u for u in _bychash.get(p.get("chash"),[]) if u!=p.get("url")]) or bool(_near.get(p.get("url")))
        f=ig.get("figures",0)
        pts=(2 if f>=15 else (1 if f>=6 else 0))+(1 if ig.get("firsthand") else 0)+(1 if ig.get("proprietary") else 0)+(1 if ig.get("datatable") else 0)
        band="low" if _dup else ("high" if pts>=3 else ("medium" if pts>=1 else "low"))
        _igp.append({"url":p.get("url"),"figures":f,"firsthand":bool(ig.get("firsthand")),"proprietary":bool(ig.get("proprietary")),
                     "datatable":bool(ig.get("datatable")),"dup":_dup,"pts":pts,"band":band})
    _igorder={"high":0,"medium":1,"low":2}
    _igp.sort(key=lambda x:(_igorder[x["band"]],-x["pts"],-x["figures"]))
    infogain={"pages":_igp,"bands":{b:sum(1 for x in _igp if x["band"]==b) for b in ("high","medium","low")},"total":len(ok200)}
    # link graph for the Site-map visualisation: nodes = crawled pages (score/depth/orphan), edges = internal links.
    _idxof={_np(p.get("path")):i for i,p in enumerate(ok200)}
    _lgnodes=[{"p":(p.get("path") or "/"),"s":p.get("score"),"d":p.get("depth") or 0,
               "o":(_np(p.get("path")) not in _linked and _np(p.get("path")) not in ("","/"))} for p in ok200]
    _lgedges=[]
    for i,p in enumerate(ok200):
        for t in (p.get("links") or []):
            j=_idxof.get(t)
            if j is not None and j!=i: _lgedges.append([i,j])
    linkgraph={"nodes":_lgnodes[:250],"edges":[e for e in _lgedges if e[0]<250 and e[1]<250][:1400],"capped":len(ok200)>250}
    for p in pages:
        for _k in ("outlinks","_broken","simhash","chash","links","sameas","agent","infogain"): p.pop(_k,None)
    ok=ok200
    def avg(f): return round(sum(f(p) for p in ok)/len(ok)) if ok else 0
    overall=avg(lambda p:p["score"])
    pill={pl:avg(lambda p:p["pillars"][pl]) for pl in PILLARS}
    eng={e:avg(lambda p:p["engines"][e]) for e in ENGINE_WEIGHTS}
    # access gate: if AI bots are blocked (robots) or WAF-blocked (reachability), the whole site is
    # uncitable regardless of page quality -> cap the headline so a block visibly tanks the score.
    _scx2={c["id"]:c["status"] for c in sitecx}
    access_blocked = _scx2.get("robots")=="bad" or _scx2.get("reachability")=="bad"
    if access_blocked:
        overall=min(overall,25); pill["Findable"]=min(pill.get("Findable",0),25)
        eng={e:min(v,25) for e,v in eng.items()}
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
            o,_,en2=page_scores(st, prof); sim.append((o,en2))
        it["gain_overall"]=round(sum(s[0] for s in sim)/len(sim))-base if sim else 0
        eg={}
        for e in ENGINE_WEIGHTS:
            eg[e]=round(sum(s[1][e] for s in sim)/len(sim))-base_eng[e] if sim else 0
        it["gain_engines"]=eg
        te=max(eg.items(),key=lambda x:x[1]) if eg else ("",0)
        it["top_engine"]=te[0]; it["top_engine_gain"]=te[1]
    # true combined ceiling for the Action Plan headline: fix every flagged warn/bad to good and
    # re-score the whole site once. The independent per-fix gains undercount this, because fixing
    # multiple signals on a page compounds, so summing standalone gains understates the real total.
    _pa=[page_scores({k:('good' if v in ('warn','bad') else v) for k,v in p["cs"].items()}, prof)[0] for p in ok]
    proj_all = round(sum(_pa)/len(_pa)) if _pa else overall
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
            "grok_advisory":GROK_ADVISORY,
            "totals":dict(tot),"issues":issues,"site_checks":sitecx,"broken_links":broken_links,"sitemap":sitemap,"linkgraph":linkgraph,"offpage":offpage,"agentready":agentready,"infogain":infogain,"aicrawler":aicrawler or {},"proj_all":proj_all,
            "client":client,"intro":intro,"agency":(agency or "GoGoChimp"),"logo":logo,"access_blocked":access_blocked,
            "types":dict(Counter(p["type"] for p in pages)),
            "site_type":site_type,"site_type_label":SITE_PROFILE_LABEL.get(site_type,site_type),"site_type_source":site_type_source,
            "profile":(prof or {}),"profile_up":sorted([c for c,m in (prof or {}).items() if m>1]),
            "profile_down":sorted([c for c,m in (prof or {}).items() if m<1]),
            "redirect_home":[{"url":p["url"],"to":p.get("redirect_home")} for p in pages if p.get("redirect_home")],
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
        _prevbots=prev.get("aicrawler") or {}                       # AI-bot access change tracking
        _curbots={b["name"]:b["status"] for b in ((data.get("aicrawler") or {}).get("bots") or [])}
        data["diff"]["bot_changes"]=[{"bot":n,"was":_prevbots[n],"now":s} for n,s in _curbots.items() if n in _prevbots and _prevbots[n]!=s]
    # trend: the full score history (all prior snapshots + this crawl) for a sparkline
    _series=[]
    if os.path.exists(hist):
        try:
            with open(hist,encoding="utf-8") as f:
                for _l in f:
                    if _l.strip():
                        try:
                            _sn=json.loads(_l); _series.append({"date":_sn.get("date"),"overall":_sn.get("overall")})
                        except Exception: pass
        except Exception: pass
    _series.append({"date":data.get("date"),"overall":data.get("overall")})   # current crawl (appended to hist below)
    data["trend"]=[x for x in _series if isinstance(x.get("overall"),(int,float))][-30:]
    snap={"date":data["date"],"generated":data["generated"],"overall":data["overall"],
          "pillars":data["pillars"],"engines":data["engines"],
          "aicrawler":{b["name"]:b["status"] for b in ((data.get("aicrawler") or {}).get("bots") or [])},
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
    from favicon_data import FAVICON_PNG_B64          # 64px CITED mark (dark app icon), option 2C brand
    FAVICON = "data:image/png;base64," + FAVICON_PNG_B64
except Exception:                                     # fallback: dark app icon (crescent-C + node) as SVG
    FAVICON = "data:image/svg+xml;base64," + base64.b64encode(
        b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'><rect width='200' height='200' rx='34' fill='#0F1410'/><path d='M148.3,50.1 A72,72 0 1 0 158.7,127' fill='none' stroke='#EDF0EB' stroke-width='17' stroke-linecap='square'/><path d='M70,101 L92,123 L135.7,67.5' fill='none' stroke='#42D848' stroke-width='17' stroke-linecap='square'/></svg>").decode()

def write_html(d, path):
    payload=json.dumps(d,ensure_ascii=False).replace("</","<\\/")
    css=r"""
:root{--bg:#0F1410;--panel:#161D18;--panel2:#141B12;--line:#26302A;--muted:#6E7A6F;--txt:#EDF0EB;
 --grn:#42D848;--grn2:#74E67A;--deep:#2AA836;--amber:#F0B429;--red:#E0533D;--chip:#D63B2F;--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 'Archivo',-apple-system,Segoe UI,Arial,sans-serif}
a{color:var(--txt);text-decoration:none}a:hover{color:var(--grn)}
header{padding:18px 26px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{display:inline-flex;align-items:center;gap:11px}
.logo .wm{display:inline-flex;align-items:baseline;gap:8px}
.logo .lw{font-family:'Archivo',sans-serif;font-weight:900;font-size:22px;letter-spacing:-.02em;color:var(--txt);line-height:1}
.logo .ls{font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.24em;color:var(--muted);text-transform:uppercase}
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
.badge.Known{border-color:#3aa0ff55;color:#7bbcff}.badge.Findable{border-color:#42D84855;color:var(--grn2)}.badge.Trusted{border-color:#F0B42955;color:var(--amber)}
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
:root{--display:'Archivo',sans-serif;--ok:#3DD68C;--warn2:#F0B429;--err2:#E0533D}
.ov{display:grid;grid-template-columns:1fr 336px;gap:22px;align-items:start}
@media(max-width:1080px){.ov{grid-template-columns:1fr}}
.ov2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:640px){.ov2{grid-template-columns:1fr}}
.sech{font-family:var(--display);text-transform:uppercase;letter-spacing:.6px;font-size:17px;color:var(--txt);font-weight:400;margin:26px 0 12px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.sech .s{font-family:'Archivo',sans-serif;text-transform:none;letter-spacing:0;font-size:12px;color:var(--muted);font-weight:400}
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
.sech,.htitle,.hclabel,.sring .v,.tval,.chstat .n,.dfgain,.bl .num,.dfh .t{font-family:'Archivo',sans-serif;font-weight:800;letter-spacing:0}
/* ===== Action Plan + Issues (mockup redesign) ===== */
.ap2{display:grid;grid-template-columns:1fr 380px;gap:24px;align-items:start}
.apmain{display:flex;flex-direction:column;gap:20px;min-width:0}
.apside{display:flex;flex-direction:column;gap:20px;position:sticky;top:12px}
.apsum,.issum{background:linear-gradient(180deg,#17130f,#141110);border:1px solid var(--line);border-radius:14px;padding:22px 26px}
.apk{font-family:var(--mono);font-size:11px;font-weight:500;letter-spacing:.16em;color:var(--muted);text-transform:uppercase}
.apbn{font-family:'Archivo',sans-serif;font-size:46px;font-weight:900;line-height:1;letter-spacing:-.02em}
.apintro{font-size:13px;line-height:1.6;color:#9d9691;max-width:900px}
.aptier{display:flex;flex-direction:column;gap:12px}
.aptierh{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.aptierh h3{margin:0;font-size:16px;letter-spacing:.06em;font-family:'Archivo',sans-serif;font-weight:800}
.aptierh .sq{width:9px;height:9px;border-radius:2px;flex:none}
.aptierh .meta{font-size:12px;color:var(--muted)}
.apbox{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:2px 22px 6px}
.apbox.hot{border-color:#42D84847}
.apissue{border-bottom:1px solid #ffffff10}
.apissue:last-child{border-bottom:0}
.aprow{display:grid;gap:18px;align-items:center;padding:15px 0}
.aprow:hover{background:#ffffff06}
.eb{width:5px;height:11px;border-radius:1px;display:inline-block}
.ebs{display:inline-flex;gap:2px;margin-right:6px;vertical-align:middle}
.apchip{display:inline-block;font-size:11px;color:#c9c2bd;background:#ffffff10;padding:2px 7px;border-radius:4px;margin:0 6px 2px 0}
.apgain{font-family:'Archivo',sans-serif;font-weight:900;letter-spacing:-.02em;line-height:1}
.pbtn{font:inherit;font-size:12px;font-weight:600;color:var(--grn);background:transparent;border:1px solid #42D84866;border-radius:5px;padding:5px 10px;cursor:pointer}
.pbtn:hover{background:#42D8481f}
.pbtn.g{color:#c9c2bd;border-color:#ffffff28}.pbtn.g:hover{color:#fff;border-color:#ffffff5c}
.apurls{display:none;font-size:12px;padding:2px 0 12px;columns:2;column-gap:24px}
.apurls a{color:var(--muted)}.apurls a:hover{color:var(--txt)}
.apurls .b{display:block;padding:2px 0;break-inside:avoid;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dotb{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle;flex:none}
.rmc{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.rmch{padding:16px 20px 12px;border-bottom:1px solid #ffffff12}
.rmch h3{margin:0;font-size:15px;letter-spacing:.06em;font-family:'Archivo',sans-serif;font-weight:800}
.rmph{padding:16px 20px;border-bottom:1px solid #ffffff12}
.rmph:last-child{border-bottom:0}
.rmnum{font-size:12px;font-weight:800;padding:3px 8px;border-radius:4px;flex:none}
.card2{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.card2 h3{margin:0 0 10px;font-size:15px;letter-spacing:.06em;font-family:'Archivo',sans-serif;font-weight:800}
.rowsb{display:flex;align-items:center;justify-content:space-between;font-size:12px;padding:4px 0}
.bigbtn{display:block;width:100%;text-align:center;font:inherit;font-size:13px;font-weight:700;color:#140b06;background:var(--grn);border:none;border-radius:8px;padding:12px;cursor:pointer}
.bigbtn:hover{background:var(--grn2);color:#0a0a0a}
.dashnote{display:flex;align-items:center;gap:10px;padding:14px 18px;border:1px dashed #ffffff24;border-radius:12px;font-size:11px;color:var(--muted);line-height:1.5}
.issum{display:flex;align-items:center;gap:30px;flex-wrap:wrap}
.issum .big{font-size:44px;font-weight:900;line-height:1;font-family:'Archivo',sans-serif;letter-spacing:-.02em}
.istat{display:flex;flex-direction:column;gap:5px;justify-content:center}
.inumwrap{display:flex;align-items:center;gap:8px;min-height:34px}
.inum{font-family:'Archivo',sans-serif;font-size:34px;font-weight:900;line-height:1;letter-spacing:-.02em}
.isub{font-size:12px;color:var(--muted);line-height:1.35}
.vr{width:1px;align-self:stretch;min-height:44px;background:#ffffff14}
.fpill{font:inherit;font-size:12px;font-weight:600;color:#c9c2bd;background:transparent;border:1px solid #ffffff28;border-radius:999px;padding:7px 14px;cursor:pointer}
.fpill:hover{color:#fff}
.fpill.on{color:#140b06;background:var(--grn);border-color:var(--grn)}
.wpage{display:flex;align-items:center;gap:14px;padding:13px 20px;border-bottom:1px solid #ffffff10;cursor:pointer;color:inherit}
.wpage:hover{background:#ffffff08}
.wpage:last-child{border-bottom:0}
.wpage .s{font-size:20px;font-weight:800;width:34px;font-family:'Archivo',sans-serif}
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
.schip{font-size:15px;font-weight:800;text-align:center;padding:5px 0;border-radius:6px;font-family:'Archivo',sans-serif}
.ecell{font-size:12px;font-weight:600;text-align:center;padding:4px 0;border-radius:4px}
.pgpill{font:inherit;font-size:12px;font-weight:600;color:#c9c2bd;background:transparent;border:1px solid #ffffff28;border-radius:999px;padding:7px 13px;cursor:pointer}
.pgpill:hover{color:#fff}
.pgpill.on{color:#140b06;background:var(--grn);border-color:var(--grn)}
.pgpill.bel.on{color:#ff9c88;background:transparent;border-color:#E0533D}
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
.card2.hot{border-color:#42D8484d}
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
.stck.bad{color:#ff9c88;border-color:#E0533D44}.stck.warn{color:#f2c574;border-color:#F0B42944}
.strow{display:grid;grid-template-columns:170px 70px 1fr 52px;gap:16px;align-items:center;padding:12px 0;border-bottom:1px solid #ffffff0f}
.strow:last-child{border-bottom:0}
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
.statcard{background:var(--panel2);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.statcard .n{font-family:'Archivo',sans-serif;font-size:30px;font-weight:900;line-height:1;letter-spacing:-.02em}
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
const WL=!!D.client, SCORELABEL=WL?'AI Search Score':'CITED Score';   // white-label: de-brand the score name
const col=s=>s>=75?'#42D848':s>=50?'#F0B429':'#E0533D';
const bcol=v=>v>=70?'#42D848':v>=50?'#F0B429':'#E0533D';
const dlt=(now,was)=>{if(was==null)return'';const d=now-was,c=d>0?'up':d<0?'dn':'z',s=(d>0?'+':'')+d;return ` <span class="d ${c}">${s}</span>`};
const dot=s=>`<span class="dot ${s}">●</span>`;
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const rel=u=>esc(u.replace(D.origin,'')||'/');
const ring=v=>`<div class="ring" style="--p:${v};--c:${col(v)}"><i>${v}</i></div>`;
const scb=v=>`<span class="sc" style="background:${col(v)}">${v}</span>`;
const bd=p=>`<span class="badge ${p}">${p}</span>`;
const ECOLS=['ChatGPT','Perplexity','AI Overviews','Gemini','Copilot','Claude'];
const TABS=['Overview','Action Plan','Issues','Pages','Off-page','Agent-ready','Info gain','|','ChatGPT','Perplexity','AI Overviews','Gemini','Copilot','Claude','Grok','|','Site structure','Response times','Broken links','AI crawlers'];
let cur='Overview',sortk='score',sortd=1,pageFilter='';
function tabsbar(){document.getElementById('tabs').innerHTML=TABS.map(t=>t=='|'?`<div class="tab sep">|</div>`:`<div class="tab ${t==cur?'on':''}" onclick="go('${t}')">${t}${t=='Grok'?'<sup style="color:#8b8480;font-weight:700;font-size:9px;margin-left:2px">adv</sup>':''}</div>`).join('')}
function go(t){cur=t;pageFilter='';tabsbar();render()}
function render(){const w=document.getElementById('view');
 if(cur=='Overview')return w.innerHTML=ovw2();
 if(cur=='Action Plan')return w.innerHTML=plan();
 if(cur=='Issues')return w.innerHTML=issuesView();
 if(cur=='Pages')return w.innerHTML=pagesView(null);
 if(cur=='Site structure')return w.innerHTML=structure();
 if(cur=='Response times')return w.innerHTML=speed();
 if(cur=='Broken links')return w.innerHTML=brokenView();
 if(cur=='Off-page')return w.innerHTML=offpageView();
 if(cur=='Agent-ready')return w.innerHTML=agentView();
 if(cur=='Info gain')return w.innerHTML=infogainView();
 if(cur=='AI crawlers')return w.innerHTML=aicrawlerView();
 if(cur=='Grok')return w.innerHTML=grokView();
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
 return `${D.crawl_failed?`<div style="background:rgba(224,83,61,.18);border:1px solid rgba(224,83,61,.55);border-radius:12px;padding:16px 18px;margin-bottom:16px;color:#ff9c88;font-size:14px"><b>Couldn't crawl this site.</b> ${esc(D.crawl_note||'')}</div>`:''}${D.access_blocked?'<div style="background:rgba(255,77,61,.14);border:1px solid rgba(255,77,61,.4);border-radius:12px;padding:14px 18px;margin-bottom:16px;color:#ff9c88;font-size:14px"><b>AI crawlers are blocked.</b> robots.txt or your WAF is blocking GPTBot / PerplexityBot / Bingbot, so nothing on this site can be cited by AI until it is fixed. The overall score is capped to reflect that - see the robots / reachability checks.</div>':''}${(D.redirect_home||[]).length?`<div style="background:rgba(240,180,41,.12);border:1px solid rgba(240,180,41,.4);border-radius:12px;padding:14px 18px;margin-bottom:16px;font-size:14px"><div style="color:#f0c674;font-weight:700;margin-bottom:6px">${(D.redirect_home||[]).length} page${(D.redirect_home||[]).length==1?'':'s'} silently redirect to your homepage</div><div class="qd" style="line-height:1.6">If an AI engine cites one of these deep URLs, the reader is bounced to your homepage instead of the answer they were promised, so the citation is wasted and its authority is diluted. Restore the real page at each address, or 301 to the closest matching content, not the homepage.</div><div style="margin-top:9px;display:flex;flex-direction:column;gap:3px">${(D.redirect_home||[]).slice(0,5).map(r=>`<div style="font-size:13px"><span style="word-break:break-all">${rel(r.url)}</span> <span class="qd">&rarr; homepage</span></div>`).join('')}${(D.redirect_home||[]).length>5?`<div class="qd">+${(D.redirect_home||[]).length-5} more</div>`:''}</div></div>`:''}<div class="ov"><div>
   <div class="panel"><div class="hero">
     <div class="sring" style="--p:${D.overall};--c:var(--grn)"><i><span class="v">${D.overall}</span><span class="o">OF 100</span></i></div>
     <div style="flex:1;min-width:190px"><div class="htitle">${SCORELABEL}</div><div class="hsub">Weighted across six engines. Pages score as <b>quotable</b> at 70.</div>${odtxt}${(D.site_type&&D.site_type!=='general')?`<div style="margin-top:9px"><span style="display:inline-block;font-size:11px;font-weight:800;letter-spacing:.4px;color:#42D848;background:rgba(66,216,72,.12);border:1px solid rgba(66,216,72,.35);padding:3px 11px;border-radius:999px">SCORED AS ${esc((D.site_type_label||'').toUpperCase())}</span></div>`:''}</div>
     <div class="hc"><div style="display:flex;justify-content:space-between;align-items:baseline"><div class="hclabel">Check health · ${tc} checks</div><span class="qd">${pass}% passing</span></div>
       <div class="chbar"><div class="chseg" style="flex:${g||1};background:var(--ok)"></div><div class="chseg" style="flex:${w||0.01};background:var(--warn2)"></div><div class="chseg" style="flex:${b||0.01};background:var(--err2)"></div></div>
       <div class="chstat"><div><div class="n" style="color:var(--err2)">${b}</div><div class="l">Errors — blocking</div></div><div><div class="n" style="color:var(--warn2)">${w}</div><div class="l">Warnings — weakening</div></div><div><div class="n" style="color:var(--ok)">${g}</div><div class="l">Passed</div></div></div></div>
   </div></div>
   <div class="sech">The three questions <span class="s">Ch3 · same scale, so you can see which one is dragging</span></div>
   <div class="panel">${['Known','Findable','Trusted'].map(p=>prow(p,Q[p],D.pillars[p],pw[p])).join('')}<div class="qd" style="margin-top:10px">| quotable threshold, 70</div></div>
   <div class="sech">Readiness by engine <span class="s">Ch6-7 · ranked worst first</span></div>
   <div class="panel">${engs.map(erow).join('')}<div class="qd" style="margin-top:10px">| quotable threshold, 70</div>
     <div onclick="go('Grok')" style="display:flex;gap:9px;align-items:center;cursor:pointer;border-top:1px solid var(--line);margin-top:12px;padding-top:12px"><span style="font-size:11px;font-weight:700;color:#c9c2bd;background:rgba(139,132,128,.14);border:1px solid rgba(139,132,128,.3);padding:2px 8px;border-radius:999px;flex:none">Grok</span><span class="qd">Not scored. Grok reads the same web signals as Perplexity and ChatGPT, so those cover it. See the advisory &rarr;</span></div></div>
   ${(D.site_type&&D.site_type!=='general')?`<div class="sech">Website-type profile <span class="s">detected ${esc(D.site_type_label)} &middot; weighting tuned to how AI judges this type</span></div><div class="panel"><div class="qd" style="margin-bottom:12px">This site was ${D.site_type_source==='override'?'set by you as':'detected as'} <b>${esc(D.site_type_label)}</b>, so the checks that decide AI citation for this page type are weighted higher and the less relevant ones lower. Same 0-100 scale, every page's applicable checks unchanged - only the emphasis shifts. ${D.site_type_source==='override'?'Set manually':'Auto-detected'}; weights are directional and never remove a page's checks.</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:18px"><div><div style="font-size:11px;font-weight:800;letter-spacing:.5px;color:var(--ok);text-transform:uppercase;margin-bottom:8px">Weighted up</div><div style="display:flex;flex-wrap:wrap;gap:6px">${(D.profile_up||[]).map(cid=>`<span style="font-size:12px;padding:3px 9px;border-radius:999px;background:rgba(61,214,140,.12);border:1px solid rgba(61,214,140,.3);color:#8fe6b6">${esc((((D.check_meta||{})[cid])||{}).label||cid)}</span>`).join('')||'<span class="qd">none</span>'}</div></div><div><div style="font-size:11px;font-weight:800;letter-spacing:.5px;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Weighted down</div><div style="display:flex;flex-wrap:wrap;gap:6px">${(D.profile_down||[]).map(cid=>`<span style="font-size:12px;padding:3px 9px;border-radius:999px;background:rgba(139,132,128,.12);border:1px solid var(--line);color:#b7afaa">${esc((((D.check_meta||{})[cid])||{}).label||cid)}</span>`).join('')||'<span class="qd">none</span>'}</div></div></div></div>`:''}
   <div class="ov2">
     <div><div class="sech">Pages by type</div><div class="panel">${typesH}</div></div>
     <div><div class="sech">Crawler reachability</div><div class="panel">${reachH}</div></div>
   </div>
   ${(()=>{const s=D.internal_search;if(!s||!s.detected)return '';
     const ep=(esc((s.action||'').replace(/^https?:\/\/[^/]+/,''))||'/')+'?'+esc(s.param||'q')+'=';
     const map={works:['var(--ok)','Working'],empty:['#E0533D','Returned no results'],unclear:['#F0B429','Could not read the results'],unreachable:['#F0B429','Results page unreachable'],no_term:['#6E7A6F','Detected, not probed']};
     const m=map[s.outcome]||['#6E7A6F','Detected'];
     let msg='';
     if(s.outcome=='works') msg=`We searched <b>&ldquo;${esc(s.term)}&rdquo;</b> (a term from your own page titles) and your internal search returned <b>${s.result_links}</b> result link${s.result_links==1?'':'s'}. A large share of AI-referred visitors land here, so keep the results relevant and well-formatted.`;
     else if(s.outcome=='empty') msg=`We searched <b>&ldquo;${esc(s.term)}&rdquo;</b>, a term taken from your own page titles, and the results page reported no results. If your own search cannot surface your own content, AI-referred visitors who land here reach a dead end.`;
     else if(s.outcome=='unclear') msg=`We detected internal search and searched <b>&ldquo;${esc(s.term)}&rdquo;</b>, but could not confidently read the results page. Check by hand that it surfaces relevant content for a landing visitor.`;
     else if(s.outcome=='unreachable') msg=`We detected an internal search form, but the results page for <b>&ldquo;${esc(s.term)}&rdquo;</b> returned ${s.status?('HTTP '+s.status):'no response'}. Confirm the search endpoint still works.`;
     else msg=`We detected an internal search form but had no clean on-site term to probe it with.`;
     const nx=(s.noindex&&s.outcome!='empty')?`<div class="qd" style="margin-top:8px">The results page is <code>noindex</code>, which is normal and fine. AI-referred visitors still land there through links, so it is a real destination even though search engines skip it.</div>`:'';
     return `<div class="sech">Internal search <span class="s">~28.8% of AI-referred visitors land on internal site search (Previsible, 6.77M sessions)</span></div><div class="panel"><div style="display:flex;align-items:center;gap:9px"><span style="width:9px;height:9px;border-radius:50%;background:${m[0]};flex:none"></span><b>${m[1]}</b><span class="qd" style="margin-left:auto;font-family:var(--mono);font-size:12px;word-break:break-all">${ep}</span></div><div class="qd" style="line-height:1.6;margin-top:8px">${msg}</div>${nx}</div>`;
   })()}
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
  <div class="card"><div class="n" style="color:${col(D.overall)}">${D.overall}</div><div class="l">${SCORELABEL}</div></div>
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
const PDOT={Known:'#6f9dff',Findable:'#F0B429',Trusted:'#3DD68C'};
const TTYPE={schema:'template',parity:'template',faq:'template',canonical:'template',robots:'template',sitemap:'template',reachability:'template',freshness:'template',internal:'template',http:'template',entity:'template',schemacomplete:'template',noindex:'template',speed:'template',schemavalidity:'template',orphans:'template',brokenlinks:'template',reviewschema:'template',
 answerfirst:'copy',definitional:'copy',readability:'copy',entitydensity:'copy',qheadings:'copy',sections:'copy',liststables:'copy',wordcount:'copy',statdensity:'copy',h1:'copy',author:'copy',sourced:'copy',video:'copy',comparison:'copy',rankedlist:'copy',answerthird:'copy',h2answer:'copy',nearduplicate:'copy',
 meta:'meta',title:'meta',alt:'meta',citations:'meta',duplicate:'meta'};
const EBARS=e=>{const n=(e=='High')?3:(e=='Med')?2:1,c=n==1?'#3DD68C':n==2?'#F0B429':'#42D848',lab=n==1?'low':n==2?'medium':'high';
 let b='';for(let k=0;k<3;k++)b+=`<span class="eb" style="background:${k<n?c:'#2C231C'}"></span>`;
 return `<span class="ebs">${b}</span><span style="color:${c}">${lab}</span>`};
const ACT=()=>D.issues.filter(i=>i.pillar!='Info'&&(i.gain_overall>0||i.severity=='bad'));
const AFFPAGES=()=>D.pages.filter(p=>p.checks.some(c=>c.status=='bad'||c.status=='warn')).length;
const gpos=i=>i.gain_overall>0?i.gain_overall:0;
function tgl(id){const e=document.getElementById(id);if(e)e.style.display=e.style.display=='block'?'none':'block'}
function bull(u,c){return `<span class="b"><span class="dotb" style="background:${c}"></span><a href="${esc(u)}" target="_blank">${rel(u)}</a></span>`}
function urlList(id,i){return `<div id="${id}" class="apurls">${[...i.bad.map(u=>bull(u,'#E0533D')),...i.warn.map(u=>bull(u,'#F0B429'))].join('')||'<span class="qd">No affected pages.</span>'}</div>`}
function pdet(id){
 if(SITEIDS.has(id)){const sc=(D.site_checks||[]).find(c=>c.id==id)||{};const cl=sc.status=='good'?'ok':sc.status=='warn'?'wn':'er';const _u=sc.urls||[];const _l=_u.length?'<div class="egh" style="margin-top:8px">Pages ('+_u.length+')</div>'+_u.map(u=>'<span class="egp"><span class="dotb" style="background:#3DD68C"></span><a href="'+esc(u)+'" target="_blank">'+rel(u)+'</a></span>').join(''):'';return `<div id="pd_${id}" class="engdet"><span class="rst ${cl}">Site-wide check: ${sc.status||'n/a'}</span> <span class="qd">${esc(sc.detail||'')}</span>${_l}</div>`;}
 const ok=D.pages.filter(p=>p.status==200), dcx=s=>s=='good'?'#3DD68C':s=='warn'?'#F0B429':'#E0533D';
 const rr=ok.map(p=>[p,p.cs[id]]).filter(x=>x[1]&&x[1]!='na'&&x[1]!='info');
 if(!rr.length)return `<div id="pd_${id}" class="engdet"><span class="qd">No applicable pages for this check.</span></div>`;
 const fl=rr.filter(x=>x[1]!='good'),ps=rr.filter(x=>x[1]=='good');
 const ln=x=>`<span class="egp"><span class="dotb" style="background:${dcx(x[1])}"></span><a href="${esc(x[0].url)}" target="_blank">${rel(x[0].url)}</a></span>`;
 return `<div id="pd_${id}" class="engdet">${fl.length?'<div class="egh">Failing here ('+fl.length+')</div>'+fl.map(ln).join(''):''}${ps.length?'<div class="egh"'+(fl.length?' style="margin-top:10px"':'')+'>Passing ('+ps.length+')</div>'+ps.map(ln).join(''):''}</div>`}

function plan(){
 const overall=D.overall, act=ACT();
 if(!act.length)return `<div class="dashnote"><span class="dotb" style="background:var(--ok)"></span>Nothing to fix — every scored check passes across the crawl.</div>`;
 const tg=act.reduce((a,i)=>a+gpos(i),0), proj=(D.proj_all!=null?D.proj_all:Math.min(100,overall+tg));
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
     <div style="display:flex;justify-content:space-between;gap:12px;font-size:12px;color:var(--muted)"><span style="font-weight:600;letter-spacing:.08em">RANKED BY STANDALONE IMPACT</span><span>${edits} page edits across ${affp} pages</span></div>
     <div style="display:flex;height:14px;border-radius:7px;overflow:hidden;background:#221d1a">
       <div style="width:${Math.round(100*bG/TG)}%;background:#42D848"></div>
       <div style="width:${Math.round(100*wG/TG)}%;background:#74E67A"></div>
       <div style="width:${Math.round(100*pG/TG)}%;background:#5a4034"></div></div>
     <div style="display:flex;gap:22px;font-size:12px;color:#9d9691;flex-wrap:wrap">
       ${[['#42D848','Biggest movers',biggest.length,bG],['#74E67A','Worth doing',worth.length,wG],['#5a4034','Polish',polish.length,pG]].map(a=>`<span style="display:flex;align-items:center;gap:7px"><span style="width:8px;height:8px;border-radius:2px;background:${a[0]}"></span>${a[1]}, ${a[2]} fixes <b style="color:#fff">+${a[3]}</b></span>`).join('')}</div>
   </div></div>`;
 h+=`<div class="apintro">Each fix shows its <b>standalone</b> CITED Score gain if applied to every affected page. Fixes compound across a page, so applying them all reaches <b>${proj}/100</b>, higher than the standalone gains add up to on their own.</div>`;
 const tiers=[['#42D848','BIGGEST MOVERS',biggest,bG,'do these this month','apbox hot','big'],
              ['#74E67A','WORTH DOING',worth,wG,'structure for retrieval','apbox','mid'],
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
 const acc={1:['#140b06','#42D848'],2:['#74E67A','#42D84829'],3:['#b7afaa','#ffffff14']};
 let cum=D.overall,out='';
 [1,2,3].forEach(ph=>{const ids=(D.plan_phases&&D.plan_phases[ph])||[];if(!ids.length)return;
   const items=ids.map(id=>D.issues.find(y=>y.id==id)).filter(Boolean);
   const g=items.reduce((a,i)=>a+gpos(i),0);cum=Math.min(100,cum+g);
   out+=`<div class="rmph"><div style="display:flex;align-items:flex-start;gap:12px">
     <span class="rmnum" style="color:${acc[ph][0]};background:${acc[ph][1]}">0${ph}</span>
     <div style="flex:1"><div style="font-size:14px;font-weight:700">${info[ph][0]}</div><div class="qd">${info[ph][1]}</div></div>
     <div style="text-align:right"><div style="font-size:20px;font-weight:900;font-family:'Archivo',sans-serif;letter-spacing:-.02em">${cum}</div><div style="font-size:11px;color:var(--ok)">+${g}</div></div></div>`;
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
   const bc=p=='Findable'?'#F0B42933':'#ffffff14';
   h+=`<section class="aptier"><div class="aptierh"><span class="dotb" style="width:9px;height:9px;margin:0;background:${PDOT[p]}"></span><h3>${QL[p]}</h3><span class="meta">score ${D.pillars[p]} &middot; ${failing} check${failing>1?'s':''} failing &middot; +${avail} available${p==weakest?' &middot; weakest pillar':''}</span></div><div class="apbox" style="border-color:${bc}">`;
   gs.forEach(i=>h+=issueRow(i,P));
   h+=`</div></section>`});
 h+=`</div><div class="apside">`+worstPagesCard()+oneChangeCard(errs,iss)+`<div class="dashnote"><span class="dotb" style="background:var(--ok)"></span>${passed} checks passed on every page &mdash; not listed here.</div></div></div>`;
 return h}
function issueRow(i,P){
 const bad=i.severity=='bad', dc=bad?'#E0533D':'#F0B429';
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
 const cc=nb?['#E0533D','rgba(255,77,61,.14)']:fails.length?['#F0B429','rgba(242,181,60,.14)']:['#3DD68C','rgba(62,207,142,.14)'];
 const labels=[...fails.filter(c=>c.status=='bad'),...fails.filter(c=>c.status=='warn')].map(c=>esc(c.label)).join(', ');
 const pill=v=>`<span style="font-size:12px;text-align:center;color:${v?'#b7afaa':'#74E67A'}">${v}</span>`;
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
 const dcol=['#E0533D','#F0B429','#3DD68C','#2f9c6c'],dnum=['#74E67A','#f2c574','#7fdcae','#7fdcae'],dlab=['under 70','70–79','80–89','90+'];
 const wt={};ECOLS.forEach(e=>wt[e]=0);D.pages.forEach(p=>{let mn=1e9,me=null;ECOLS.forEach(e=>{if(p.engines[e]<mn){mn=p.engines[e];me=e}});if(me)wt[me]++});
 const ws=Object.entries(wt).filter(x=>x[1]>0).sort((a,b)=>b[1]-a[1]).slice(0,4),wmax=Math.max(...ws.map(x=>x[1]),1),wc=['#ff5b2e','#74E67A','#F0B429','#c98f2e'];
 if(!sortk)sortk='score';
 let h=`<div style="display:flex;flex-direction:column;gap:18px">`;
 // summary
 h+=`<div class="pgsum">
   <div><div class="apk">MEDIAN PAGE</div><div style="display:flex;align-items:baseline;gap:9px"><span style="font-size:40px;font-weight:900;line-height:1;font-family:'Archivo',sans-serif;letter-spacing:-.02em">${median}</span><span class="qd">quotable at 70</span></div></div>
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
     <span title="Known" style="text-align:center;color:#6f9dff">KN</span><span title="Findable" style="text-align:center;color:#F0B429">FI</span><span title="Trusted" style="text-align:center;color:#3DD68C">TR</span>
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
const SHORT={parity:'Schema in JS',answerfirst:'no opener',definitional:'no definition',readability:'hard to read',entitydensity:'few entities',sections:'walls of text',schema:'Article schema',wordcount:'thin content',freshness:'stale',qheadings:'H2s',faq:'no FAQ',liststables:'no tables',meta:'meta desc',title:'title',alt:'alt text',citations:'few sources',internal:'few links',statdensity:'few stats',canonical:'canonical',h1:'H1',robots:'bot blocked',sitemap:'no sitemap',reachability:'blocked',entity:'no entity',schemacomplete:'thin schema',author:'no author',sourced:'unsourced stats',video:'no video',comparison:'no comparison',noindex:'noindexed',speed:'slow response',schemavalidity:'invalid schema',duplicate:'dup title/meta',rankedlist:'no ranked list',answerthird:'answer buried',h2answer:'headings unanswered',orphans:'orphaned',brokenlinks:'broken links',nearduplicate:'near-duplicate',reviewschema:'no review schema'};
const EBOTS={'ChatGPT':['OAI-SearchBot','ChatGPT-User'],'Perplexity':['PerplexityBot','Perplexity-User'],'AI Overviews':['Googlebot'],'Gemini':['Googlebot','Google-Extended'],'Copilot':['Bingbot'],'Claude':['ClaudeBot','Claude-User']};
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
 const barC=pr=>pr>=90?'#3DD68C':pr>=40?'#F0B429':'#E0533D';
 const engDetail=id=>{
   if(SITEIDS.has(id)){const sc=(D.site_checks||[]).find(c=>c.id==id)||{};const cl=sc.status=='good'?'ok':sc.status=='warn'?'wn':'er';const _u=sc.urls||[];const _l=_u.length?'<div class="egh" style="margin-top:8px">Pages ('+_u.length+')</div>'+_u.map(u=>'<span class="egp"><span class="dotb" style="background:#3DD68C"></span><a href="'+esc(u)+'" target="_blank">'+rel(u)+'</a></span>').join(''):'';return `<div id="eg_${id}" class="engdet"><span class="rst ${cl}">Site-wide check: ${sc.status||'n/a'}</span> <span class="qd">${esc(sc.detail||'')}</span>${_l}</div>`;}
   const dcx=s=>s=='good'?'#3DD68C':s=='warn'?'#F0B429':'#E0533D';
   const rr=ok.map(p=>[p,p.cs[id]]).filter(x=>x[1]&&x[1]!='na'&&x[1]!='info');
   if(!rr.length)return `<div id="eg_${id}" class="engdet"><span class="qd">No applicable pages for this signal.</span></div>`;
   const fl=rr.filter(x=>x[1]!='good'),ps=rr.filter(x=>x[1]=='good');
   const ln=x=>`<span class="egp"><span class="dotb" style="background:${dcx(x[1])}"></span><a href="${esc(x[0].url)}" target="_blank">${rel(x[0].url)}</a></span>`;
   return `<div id="eg_${id}" class="engdet">${fl.length?'<div class="egh">Failing here ('+fl.length+')</div>'+fl.map(ln).join(''):''}${ps.length?'<div class="egh"'+(fl.length?' style="margin-top:10px"':'')+'>Passing ('+ps.length+')</div>'+ps.map(ln).join(''):''}</div>`;};
 const esig=(s,hi)=>{const dcol=s.w>=3?'#42D848':s.w==2?'#74E67A':'#8b8480';const lc=hi?(s.lift>=5?'#42D848':'#74E67A'):'#b7afaa';
   const bar=`<div style="display:flex;flex-direction:column;gap:5px"><div class="engbar"><i style="width:${Math.max(s.pr,2)}%;background:${barC(s.pr)}"></i></div><span style="font-size:11px;color:${s.pr==0?'#ff9c88':'#9d9691'}">${s.pr}% pass · ${s.good} of ${s.total} pages</span></div>`;
   const lift=`<span style="font-size:${hi?'20px':'16px'};font-weight:${hi?'800':'700'};color:${lc};text-align:right">${s.lift>0?'+'+s.lift:'—'}</span>`;
   const lab=hi?`<div style="display:flex;flex-direction:column;gap:4px;min-width:0"><span style="font-size:15px;font-weight:700">${esc(s.lab)} <span class="egcar">▾</span></span><span class="qd" style="line-height:1.5">${esc(s.ev)}</span></div>`
             :`<div style="display:flex;align-items:center;gap:12px;min-width:0"><span style="font-size:14px;font-weight:600;white-space:nowrap">${esc(s.lab)} <span class="egcar">▾</span></span><span class="qd" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.ev)}</span></div>`;
   return `<div class="engcols engrow" onclick="tgl('eg_${s.id}')" title="Show pages">${lab}${WD(s.w,dcol)}${bar}${lift}</div>${engDetail(s.id)}`};
 const estrong=s=>`<div><div class="engcols engrow" onclick="tgl('eg_${s.id}')" title="Show pages" style="padding:2px 0;border:0"><span style="font-size:13px;color:#c9c2bd">${esc(s.lab)} <span class="egcar">▾</span></span>${WD(s.w,'#3DD68C')}<div style="display:flex;align-items:center;gap:10px"><div class="engbar" style="height:7px;flex:1"><i style="width:${s.pr}%;background:#3DD68C"></i></div><span style="font-size:11px;color:${s.pr>=100?'#3DD68C':'#7fdcae'};width:74px;flex:none">${s.pr}% · ${s.good}/${s.total}</span></div><span style="text-align:right;color:#6f6864">—</span></div>${engDetail(s.id)}</div>`;
 const secH=(c,name,sub)=>`<div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:${c}"></span><h3>${name}</h3><span class="meta">${sub}</span></div>`;
 let h=`<div class="ap2"><div class="apmain">`;
 h+=`<div class="apsum" style="display:grid;grid-template-columns:auto 1fr;gap:34px;align-items:center;padding:26px 28px">
   <div style="display:flex;align-items:center;gap:22px">
     <div class="sring" style="--p:${score}"><i><span class="v">${score}</span><span class="o">OF 100</span></i></div>
     <div style="display:flex;flex-direction:column;gap:9px">
       <div class="htitle" style="font-size:20px;margin:0">${e} READINESS</div>
       <div style="display:flex;align-items:center;gap:8px"><span style="font-size:12px;font-weight:600;color:${gap>0?'#f2c574':'#3DD68C'};background:${gap>0?'rgba(242,181,60,.14)':'rgba(62,207,142,.14)'};border:1px solid ${gap>0?'rgba(242,181,60,.3)':'rgba(62,207,142,.3)'};padding:4px 9px;border-radius:999px">${gap>0?gap+' pts to quotable':'clears 70 · quotable'}</span><span class="qd">threshold ${TH}</span></div>
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
 if(high.length)h+=`<section class="aptier">${secH('#42D848','HIGH WEIGHT, LOW PASS RATE','where '+e+' is costing you the most — fix in this order')}<div class="apbox hot" style="padding:0 24px"><div class="engcols enghead"><span>SIGNAL</span><span>WEIGHT</span><span>SITE PASS RATE</span><span style="text-align:right">LIFT</span></div>${high.map(s=>esig(s,1)).join('')}</div></section>`;
 if(low.length)h+=`<section class="aptier">${secH('#F0B429','LOWER WEIGHT, WORTH TIDYING',low.length+' signal'+(low.length>1?'s':'')+' · +'+low.reduce((a,s)=>a+(s.lift>0?s.lift:0),0)+' between them')}<div class="apbox" style="padding:2px 24px 6px">${low.map(s=>esig(s,0)).join('')}</div></section>`;
 if(strong.length)h+=`<section class="aptier">${secH('#3DD68C','ALREADY STRONG',strong.length+' signal'+(strong.length>1?'s':'')+' · protect these when you edit')}<div class="apbox" style="padding:16px 24px;display:flex;flex-direction:column;gap:12px">${strong.map(estrong).join('')}</div></section>`;
 const worst=[...ok].sort((a,b)=>a.engines[e]-b.engines[e]),w8=worst.slice(0,8);
 h+=`<section class="aptier"><div class="aptierh" style="justify-content:space-between"><div style="display:flex;align-items:center;gap:10px"><h3>WORST PAGES FOR ${e}</h3><span class="meta">${pagesBelow} of ${P} below ${TH} · showing the ${w8.length} weakest</span></div><span onclick="go('Pages')" style="font-size:12px;font-weight:600;color:var(--grn);cursor:pointer">See all ${P} pages →</span></div>
   <div style="background:var(--panel2);border:1px solid var(--line);border-radius:14px;overflow:hidden">
   <div class="engwcols engwhead"><span style="text-align:center">${EABBR[e]||e}</span><span>URL</span><span>WHAT IT FAILS HERE</span><span style="text-align:center">OVERALL</span><span style="text-align:right">VS ${TH}</span></div>
   ${w8.map(p=>{const b=PBAND(p.engines[e]),wf=p.checks.filter(c=>(c.status=='bad'||c.status=='warn')&&ws[c.id]).map(c=>SHORT[c.id]||c.label).slice(0,4).join(', ')||'—',d=p.engines[e]-TH,dc=d>=0?'#8b8480':d<=-10?'#ff9c88':'#f2c574';
     return `<div class="engwcols engwrow"><span class="schip" style="background:${b[0]};color:${b[1]}">${p.engines[e]}</span><a href="${esc(p.url)}" target="_blank" style="font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${rel(p.url)}</a><span style="font-size:11px;color:#8b8480;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(wf)}</span><span style="font-size:13px;color:#b7afaa;text-align:center">${p.score}</span><span style="font-size:12px;font-weight:700;color:${dc};text-align:right">${d>=0?'+'+d:'−'+Math.abs(d)}</span></div>`}).join('')}</div></section>`;
 h+=`</div>`;
 // sidebar
 const eranked=Object.entries(D.engines).sort((a,b)=>b[1]-a[1]);
 const acr=eranked.map(x=>`<div style="display:flex;align-items:center;gap:10px"><span style="width:82px;color:${x[0]==e?'#fff':'#b7afaa'};font-weight:${x[0]==e?'700':'400'}">${x[0]}</span><span style="flex:1;height:8px;border-radius:4px;background:#221d1a;overflow:hidden"><span style="display:block;width:${x[1]}%;height:100%;background:${x[0]==e?'#42D848':'#3a3227'}"></span></span><span style="width:24px;text-align:right;color:${x[0]==e?'#fff':'#b7afaa'};font-weight:${x[0]==e?'700':'400'}">${x[1]}</span></div>`).join('');
 const bnote=e==best[0]?`Same pages, different weightings. ${e} is your strongest surface — protect it as you edit.`:`Same pages, different weightings. ${e} runs ${best[1]-score} point${best[1]-score==1?'':'s'} behind ${best[0]}, your strongest.`;
 const fixes=high.concat(low).filter(s=>s.lift>0).slice(0,3);
 const doHtml=fixes.length?fixes.map((s,i)=>{const iss=IM[s.id]||{},title=(iss.fix||s.lab).split(' - ')[0].split('. ')[0].trim(),cnt=iss.count||(s.total-s.good),nb=i==0?'color:#140b06;background:#42D848':'color:#74E67A;background:rgba(66,216,72,.2)';
   return `<div style="display:flex;align-items:flex-start;gap:12px"><span class="numbadge" style="${nb}">${i+1}</span><div style="display:flex;flex-direction:column;gap:3px"><span style="font-size:13px;font-weight:600">${esc(title)}</span><span class="qd" style="line-height:1.5">${cnt} page${cnt==1?'':'s'} · +${s.lift} ${e}</span></div></div>`}).join(''):`<div class="qd">No fixes needed — ${e} passes every weighted signal.</div>`;
 const reach=(D.site_checks.find(c=>c.id=='reachability')||{}).status||'good';
 const rT=reach=='good'?['#3DD68C','● allowed · '+P+'/'+P]:reach=='warn'?['#F0B429','● partial']:['#ff9c88','● blocked'];
 const parityBad=ok.filter(p=>p.cs.parity=='bad'||p.cs.parity=='warn').length;
 const bots=(EBOTS[e]||[]).map(b=>`<div style="display:flex;justify-content:space-between;font-size:12px"><span style="color:#b7afaa">${b}</span><span style="color:${rT[0]}">${rT[1]}</span></div>`).join('');
 const caNote=reach=='bad'?'Bots are blocked at the WAF — unblock them first.':parityBad?'Access is fine. The problem is what the crawler can read once it arrives.':'Access and rendering both look clean.';
 h+=`<div class="apside">
   <div class="card2"><div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px"><h3 style="margin:0">Across Your Engines</h3><span class="qd">site level</span></div><div style="display:flex;flex-direction:column;gap:10px;font-size:12px">${acr}</div><div class="qd" style="line-height:1.5;border-top:1px solid var(--line);padding-top:11px;margin-top:12px">${bnote}</div></div>
   <div class="card2 hot"><h3>Do this for ${e}</h3><div style="display:flex;flex-direction:column;gap:13px">${doHtml}</div>${fixes.length?`<button class="bigbtn" style="border-radius:6px;margin-top:6px" onclick="go('Action Plan')">Open the action plan</button>`:''}</div>
   <div class="card2"><h3>Crawler Access</h3><div style="display:flex;flex-direction:column;gap:11px">${bots}<div style="display:flex;justify-content:space-between;font-size:12px"><span style="color:#b7afaa">Server-rendered schema</span><span style="color:${parityBad?'#ff9c88':'#3DD68C'}">● ${parityBad?parityBad+' pages JS-only':'all '+P+' server-rendered'}</span></div></div><div class="qd" style="line-height:1.5;border-top:1px solid var(--line);padding-top:11px;margin-top:2px">${caNote}</div></div>
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
 var _tr=D.trend||[];
 if(_tr.length>=2){
   var _v=_tr.map(function(t){return t.overall;}),_mn=Math.min.apply(null,_v),_mx=Math.max.apply(null,_v),_rg=(_mx-_mn)||1;
   var _W=680,_H=90,_pd=10;
   var _xy=function(t,i){return [_pd+(i/(_tr.length-1))*(_W-2*_pd), _H-_pd-((t.overall-_mn)/_rg)*(_H-2*_pd)];};
   var _poly=_tr.map(function(t,i){var q=_xy(t,i);return q[0].toFixed(1)+','+q[1].toFixed(1);}).join(' ');
   var _sp='<svg viewBox="0 0 '+_W+' '+_H+'" style="width:100%;height:90px"><polyline points="'+_poly+'" fill="none" stroke="#42D848" stroke-width="2"/>';
   _tr.forEach(function(t,i){var q=_xy(t,i);_sp+='<circle cx="'+q[0].toFixed(1)+'" cy="'+q[1].toFixed(1)+'" r="2.6" fill="#42D848"><title>'+esc(t.date||'')+': '+t.overall+'</title></circle>';});
   _sp+='</svg>';
   var _d=_tr[_tr.length-1].overall-_tr[0].overall;
   h+='<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>SCORE OVER TIME</h3><span class="meta">'+_tr.length+' crawls &middot; '+(_d>=0?'+':'')+_d+' since first</span></div><div class="apbox" style="padding:16px 22px">'+_sp+'</div></section>';
 }
 var sm=D.sitemap||{};
 if(sm.has_sitemap){
   var smchip=function(arr,n,label,color){
     if(!n) return '<div style="margin:7px 0;color:var(--ok);font-size:12.5px">&#10003; '+label+': none</div>';
     var list=(arr||[]).slice(0,12).map(function(u){return '<span class="stck warn">'+esc(u)+'</span>';}).join('');
     return '<div style="margin:9px 0"><b style="color:'+color+';font-size:12.5px">'+n+' '+label+'</b><div class="stcks" style="margin-top:5px">'+list+(n>12?'<span class="stck">+'+(n-12)+' more</span>':'')+'</div></div>';};
   h+='<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>SITEMAP HEALTH</h3><span class="meta">'+sm.sitemap_urls+' in sitemap &middot; '+sm.crawled+' crawled</span></div><div class="apbox" style="padding:14px 22px">';
   h+=smchip(sm.missing_from_sitemap,sm.missing_n,'indexable page(s) MISSING from the sitemap - add them so AI &amp; engines discover them','#f2c574');
   h+=smchip(sm.orphan_no_internal_links,sm.orphan_n,'orphan page(s) with NO internal links - add links so crawlers can reach them','#f2c574');
   h+=smchip(sm.noindex_in_sitemap,sm.noindex_n,'noindexed page(s) listed IN the sitemap - remove them (a sitemap should list only indexable URLs)','#ff9c88');
   if(sm.not_crawled_n) h+='<div style="margin-top:11px;color:var(--muted);font-size:11px">'+sm.not_crawled_n+' sitemap URL(s) not reached in this crawl - raise the crawl limit to confirm whether these are genuine orphans or just uncrawled.</div>';
   h+='</div></section>';
 }
 const stpage=p=>{const b=PBAND(p.score);
   const bad=p.checks.filter(c=>c.status=='bad'),warn=p.checks.filter(c=>c.status=='warn');
   const sum=[bad.length?bad.length+' error'+(bad.length>1?'s':''):'',warn.length?warn.length+' warning'+(warn.length>1?'s':''):''].filter(Boolean).join(' · ')||'all checks pass';
   const chips=[...bad.map(c=>`<span class="stck bad">${esc(c.label)}</span>`),...warn.map(c=>`<span class="stck warn">${esc(c.label)}</span>`)].join('');
   return `<div class="stp"><span class="schip" style="background:${b[0]};color:${b[1]}">${p.score}</span><div style="flex:1;min-width:0"><div style="display:flex;gap:10px;align-items:baseline"><a href="${esc(p.url)}" target="_blank">${rel(p.url)}</a><span class="qd" style="flex:none;color:${bad.length?'#ff9c88':warn.length?'#f2c574':'var(--ok)'}">${sum}</span></div>${chips?`<div class="stcks">${chips}</div>`:''}</div></div>`};
 const rows=(list,lab)=>list.map((x,i)=>{const a=avg(x[1]),b=PBAND(a),id='st_'+lab+i;return `<div class="strow" onclick="tgl('${id}')" style="cursor:pointer"><span style="font-weight:600">${esc(x[0])} <span class="egcar">▾</span></span><span class="qd">${x[1].length} page${x[1].length>1?'s':''}</span><div class="stbar" title="avg score ${a}/100"><span class="stthr"></span><i style="width:${a}%;background:var(--grn)"></i></div><span class="schip" style="background:${b[0]};color:${b[1]}">${a}</span></div><div id="${id}" class="stdet">${[...x[1]].sort((p,q)=>p.score-q.score).map(stpage).join('')}</div>`}).join('');
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>BY TOP-LEVEL SECTION</h3><span class="meta">${secs.length} section${secs.length>1?'s':''}</span></div><div class="apbox" style="padding:6px 22px">${rows(secs.map(s=>['/'+s[0],s[1]]),'sec')}</div></section>`;
 const depths=Object.keys(byDepth).map(Number).sort((a,b)=>a-b),maxd=Math.max(...depths.map(d=>byDepth[d].length),1);
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#F0B429"></span><h3>BY CRAWL DEPTH</h3><span class="meta">clicks from the homepage</span></div><div class="apbox" style="padding:6px 22px">${rows(depths.map(d=>['Depth '+d,byDepth[d]]),'dep')}</div></section>`;
 var _lg=D.linkgraph||{nodes:[]};
 if(_lg.nodes&&_lg.nodes.length){
   h+='<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>SITE MAP</h3><span class="meta">rings = crawl depth &middot; colour = score &middot; red ring = orphan</span></div><div class="apbox" style="padding:16px 22px">'+sitemapViz()+'</div></section>';
 }
 h+=`</div>`;return h}
function sitemapViz(){
  var g=D.linkgraph||{nodes:[],edges:[]},nodes=g.nodes||[],edges=g.edges||[];
  if(!nodes.length) return '';
  var W=780,H=560,cx=W/2,cy=H/2,byd={};
  nodes.forEach(function(n,i){var d=n.d||0;(byd[d]=byd[d]||[]).push(i);});
  var depths=Object.keys(byd).map(Number).sort(function(a,b){return a-b;}),maxd=depths[depths.length-1]||1;
  var ring=(Math.min(W,H)/2-34)/(maxd+0.5),pos=[];
  depths.forEach(function(d){var arr=byd[d],r=d*ring;arr.forEach(function(idx,k){
    if(d===0){pos[idx]=[cx,cy];return;}
    var a=(k/arr.length)*Math.PI*2-Math.PI/2+d*0.5;pos[idx]=[cx+r*Math.cos(a),cy+r*Math.sin(a)];});});
  var band=function(s){return s>=85?'#42D848':s>=70?'#F0B429':'#E0533D';};
  var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;max-height:560px;background:#00000018;border-radius:12px">';
  edges.forEach(function(e){var a=pos[e[0]],b=pos[e[1]];if(a&&b)s+='<line x1="'+a[0].toFixed(1)+'" y1="'+a[1].toFixed(1)+'" x2="'+b[0].toFixed(1)+'" y2="'+b[1].toFixed(1)+'" stroke="#ffffff12" stroke-width="1"/>';});
  nodes.forEach(function(n,i){var p=pos[i];if(!p)return;var r=n.d===0?8:4.5;
    s+='<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="'+r+'" fill="'+band(n.s)+'" '+(n.o?'stroke="#E0533D" stroke-width="2.2"':'stroke="#0000002e" stroke-width="0.6"')+'><title>'+esc(n.p)+' - '+n.s+(n.o?' - orphan (no internal links)':'')+'</title></circle>';});
  s+='</svg><div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:11px;color:var(--muted)">'
    +'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#42D848;vertical-align:middle"></span> 85+</span>'
    +'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#F0B429;vertical-align:middle"></span> 70-84</span>'
    +'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#E0533D;vertical-align:middle"></span> under 70</span>'
    +'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:transparent;border:2px solid #E0533D;vertical-align:middle"></span> orphan = no internal links (AI can\'t reach it)</span>'
    +(g.capped?'<span>(first 250 pages)</span>':'')+'</div>';
  return s;
}
function speed(){
 const sc2=ms=>ms<=800?'#3DD68C':ms<=1800?'#F0B429':'#E0533D',sl=ms=>ms<=800?'Fast':ms<=1800?'OK':'Slow';
 const ps=[...D.pages].sort((a,b)=>b.fetch_ms-a.fetch_ms);
 const avgf=Math.round(ps.reduce((a,p)=>a+p.fetch_ms,0)/ps.length),avgr=Math.round(ps.reduce((a,p)=>a+p.render_ms,0)/ps.length);
 const fast=ps.filter(p=>p.fetch_ms<=800).length,okc=ps.filter(p=>p.fetch_ms>800&&p.fetch_ms<=1800).length,slow=ps.filter(p=>p.fetch_ms>1800).length;
 let h=`<div style="display:flex;flex-direction:column;gap:18px">`;
 h+=`<div class="statgrid">
   <div class="statcard"><div class="n" style="color:${sc2(avgf)}">${avgf}<span style="font-size:14px;color:var(--muted)"> ms</span></div><div class="l">Avg server response · <b style="color:${sc2(avgf)}">${sl(avgf)}</b></div></div>
   <div class="statcard"><div class="n" style="color:#3DD68C">${fast}</div><div class="l">Fast ≤ 0.8s</div></div>
   <div class="statcard"><div class="n" style="color:#F0B429">${okc}</div><div class="l">OK ≤ 1.8s</div></div>
   <div class="statcard"><div class="n" style="color:#E0533D">${slow}</div><div class="l">Slow &gt; 1.8s</div></div>
   <div class="statcard"><div class="n">${avgr}<span style="font-size:14px;color:var(--muted)"> ms</span></div><div class="l">Avg render (tool overhead)</div></div></div>`;
 h+=`<div class="qd" style="line-height:1.6;max-width:900px">Server response graded on Google's TTFB thresholds: Fast ≤ 0.8s, OK ≤ 1.8s, Slow &gt; 1.8s. Render time is the tool's headless-Chrome overhead, not your site's speed.</div>`;
 h+=`<div class="qd" style="line-height:1.6;max-width:900px;border-top:1px solid var(--line);padding-top:12px"><b style="color:var(--txt)">This is a scored check</b> (the <b>Fast server response</b> signal, Findable pillar): <b style="color:${sc2(avgf)}">${fast} of ${ps.length}</b> pages pass. It feeds the live-retrieval engines - <b>ChatGPT, Perplexity, Copilot, AI Overviews</b> - which abandon slow pages before they can cite them, so a Slow page is a citation risk, not just a UX one.</div>`;
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>SLOWEST PAGES</h3><span class="meta">by server response time</span></div>
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
function grokView(){const G=D.grok_advisory||{};
 const px=(G.proxy||[]).filter(e=>D.engines&&D.engines[e]!=null);
 const proxyAvg=px.length?Math.round(px.reduce((a,e)=>a+D.engines[e],0)/px.length):null;
 const proxyH=px.map(e=>{const v=D.engines[e];return `<div style="display:flex;align-items:center;gap:10px"><span style="width:90px;color:#b7afaa;font-size:12px">${e}</span><span style="flex:1;height:8px;border-radius:4px;background:#221d1a;overflow:hidden"><span style="display:block;width:${Math.max(2,v)}%;height:100%;background:${bcol(v)}"></span></span><span style="width:26px;text-align:right;color:#fff;font-weight:700;font-size:13px">${v}</span></div>`}).join('');
 const block=(c,t,sub,body)=>`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:${c}"></span><h3>${t}</h3><span class="meta">${sub}</span></div><div class="apbox" style="padding:16px 24px"><div style="font-size:14px;line-height:1.65;color:#c9c2bd">${esc(body)}</div></div></section>`;
 let h=`<div class="ap2"><div class="apmain">`;
 h+=`<div class="apsum" style="display:grid;grid-template-columns:auto 1fr;gap:34px;align-items:center;padding:26px 28px">
   <div style="display:flex;align-items:center;gap:22px">
     <div class="sring" style="--p:0;--c:#3a3227"><i><span class="v" style="font-size:15px;letter-spacing:.5px">N/A</span><span class="o">ADVISORY</span></i></div>
     <div style="display:flex;flex-direction:column;gap:9px">
       <div class="htitle" style="font-size:20px;margin:0">${esc(G.name||'Grok (xAI)')}</div>
       <div style="display:flex;align-items:center;gap:8px"><span style="font-size:12px;font-weight:600;color:#c9c2bd;background:rgba(139,132,128,.14);border:1px solid rgba(139,132,128,.3);padding:4px 9px;border-radius:999px">Advisory · not scored</span></div>
       <div class="qd" style="line-height:1.5;max-width:250px">A large surface (~117M users) that sends almost no referrals. Covered here through its web-side proxy, not a fabricated score.</div>
     </div>
   </div>
   <div style="border-left:1px solid var(--line);padding-left:32px;display:flex;flex-direction:column;gap:14px">
     <div class="apk">HOW THIS ENGINE DECIDES</div>
     <div style="font-size:14px;line-height:1.6;color:#c9c2bd;max-width:640px">${esc(G.how||'')}</div>
   </div></div>`;
 if(px.length)h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#3DD68C"></span><h3>GROK'S WEB SIDE, VIA YOUR EXISTING SCORES</h3><span class="meta">it reads the same open-web signals as these two engines</span></div>
   <div class="apbox" style="padding:18px 24px;display:flex;flex-direction:column;gap:12px">${proxyH}<div class="qd" style="line-height:1.55;border-top:1px solid var(--line);padding-top:12px">${px.join(' and ')} are your Grok web-side proxy${proxyAvg!=null?' (about '+proxyAvg+'/100 today)':''}. Improve those and Grok's open-web retrieval improves with them. ${esc(G.why||'')}</div></div></section>`;
 h+=block('#42D848','THE ONE GROK-SPECIFIC LEVER','off-page, so outside this audit',G.lever||'');
 h+=block('#F0B429','ACCURACY CAVEAT','least reliable attributions of any engine here',G.caveat||'');
 h+=block('#8b8480','WHAT WOULD MAKE IT A SCORED ENGINE','the honest trigger',G.trigger||'');
 h+=`</div><div class="apside">
   <div class="card2"><h3>Why advisory, not a 7th ring</h3><div class="qd" style="line-height:1.6">A scored engine has to be calibrated against real citation data. There is no Grok export to calibrate against, and its web weighting would just clone Perplexity's. A fabricated ring would cheapen the six that are earned.</div></div>
   <div class="card2"><h3>If you want Grok visibility</h3><div class="qd" style="line-height:1.6">The lever is X presence, not this site. Keep shipping the parity, entity and freshness work that already feeds Grok's open-web pool, and treat any Grok mention as a free by-product of your X footprint.</div></div>
   <div class="card2"><div style="display:flex;align-items:center;gap:8px"><span style="width:8px;height:8px;border-radius:50%;background:#3DD68C"></span><span style="font-size:13px;font-weight:700">Informational, like llms.txt</span></div><div class="qd" style="line-height:1.6;margin-top:8px">Shown for completeness and deliberately not counted in your CITED Score, exactly as the tool treats llms.txt.</div></div>
 </div></div>`;
 return h}
function agentView(){
 var a=D.agentready||{counts:{},total:0,pages_any:[],any_n:0};
 var c=a.counts||{}, tot=a.total||1;
 var SIG=[
   ['action','Action schema','potentialAction / OrderAction / ReserveAction / ContactAction - tells an agent what it can DO'],
   ['offer','Machine-readable Offer','Offer / offers / price - an agent can read what you sell'],
   ['price','Explicit price','a specific machine-readable price, not buried in prose'],
   ['avail','Availability','availability / stock status an agent can check before acting'],
   ['prodserv','Product / Service entity','a transactable Product or Service @type'],
   ['contact','Contact / booking point','a ContactPoint an agent can reach or book through']
 ];
 var sig=a.signals||{};
 var ln=function(u,dc){return '<span class="egp"><span class="dotb" style="background:'+dc+'"></span><a href="'+esc(u)+'" target="_blank">'+rel(u)+'</a></span>';};
 var bar=function(k,label,desc){var n=c[k]||0;var pct=Math.round(100*n/tot);var col=n?(pct>=50?'#3DD68C':'#F0B429'):'#ff9c88';
   var s=sig[k]||{has:[],missing_money:[]};var miss=(s.missing_money||[]).length;var hn=(s.has||[]).length;
   var det='<div id="ag_'+k+'" class="engdet">'
     +'<div class="egh">Present on ('+hn+')</div>'+(hn?(s.has||[]).map(function(u){return ln(u,'#3DD68C')}).join(''):'<span class="egp qd">none</span>')
     +'<div class="egh" style="margin-top:10px">Missing on '+miss+' commercial page'+(miss==1?'':'s')+' (should add)</div>'+(miss?(s.missing_money||[]).map(function(u){return ln(u,'#E0533D')}).join(''):'<span class="egp qd">none</span>')
     +'<div class="qd" style="column-span:all;margin-top:10px;line-height:1.5">Only commercial / transactable pages are flagged as missing - editorial and blog pages do not need to be actionable.</div></div>';
   return '<div class="apissue"><div class="aprow" onclick="tgl(\'ag_'+k+'\')" style="grid-template-columns:1fr;gap:5px;cursor:pointer;padding:13px 0">'
     +'<div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px"><span style="font-weight:700;font-size:14px">'+label+' <span class="egcar">▾</span></span><span style="color:'+col+';font-weight:700;font-size:13px;white-space:nowrap">'+n+' / '+tot+' pages</span></div>'
     +'<div style="height:7px;border-radius:4px;background:#221d1a;overflow:hidden;margin:1px 0"><span style="display:block;width:'+Math.max(2,pct)+'%;height:100%;background:'+col+'"></span></div>'
     +'<div class="qd" style="line-height:1.5">'+desc+'</div>'
     +'</div>'+det+'</div>';};
 var matrix=SIG.map(function(s){return bar(s[0],s[1],s[2])}).join('');
 var proto=a.protocols||{}; var pk=Object.keys(proto);
 var protoH=pk.length?pk.map(function(k){var p=proto[k];var col=p.found?'#3DD68C':'#8b8480';return '<div style="display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-top:1px solid var(--line)"><span style="min-width:0"><b style="font-size:13px">'+k+'</b> <span class="qd" style="font-size:11px">'+p.path+'</span></span><span style="color:'+col+';font-weight:700;font-size:12px;flex:none">'+(p.found?'✓ found':'not found')+'</span></div>';}).join(''):'<span class="qd">Not probed (benchmark run).</span>';
 var protoFound=pk.filter(function(k){return proto[k].found}).length;
 return `<div class="ap2"><div class="apmain">
   <div class="apsum" style="padding:24px 28px">
     <div class="apk">AGENT-READINESS &middot; ADVISORY (NOT SCORED YET)</div>
     <div style="font-size:14px;line-height:1.65;color:#c9c2bd;max-width:730px;margin-top:10px">The next shift is answers &rarr; <b>agents</b>: AI that browses, compares and <b>transacts</b> on the user's behalf. An agent doesn't just read your page, it needs to <b>act</b> - read a price, check availability, book, contact. Actions need machine-readable precision prose can't give. This tab reads whether AI can <b>act</b> on you, not just cite you. Forward-looking and directional, not part of the CITED Score yet.</div>
   </div>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>ACTIONABLE SIGNALS ON YOUR SITE</h3><span class="meta">${a.any_n||0} of ${tot} pages expose at least one &middot; ${a.money_n||0} commercial &middot; click a signal to see which pages</span></div><div class="apbox" style="padding:6px 22px 16px">${matrix}</div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>PROTOCOL &amp; DISCOVERY FILES</h3><span class="meta">${protoFound} of ${pk.length} present &middot; how an agent connects programmatically</span></div><div class="apbox" style="padding:6px 22px 14px">${protoH}<div class="qd" style="line-height:1.55;border-top:1px solid var(--line);padding-top:11px;margin-top:6px">These are emerging and nascent - most sites have none yet - so this is forward guidance, not a mark against you. The one to watch is the <b>MCP server card</b> (<code>/.well-known/mcp.json</code>): as agents standardise on MCP, it becomes how they discover your tools and actions.</div></div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#F0B429"></span><h3>ACTIONABLE, NOT DESCRIPTIVE, SCHEMA</h3><span class="meta">the distinction that matters</span></div><div class="apbox" style="padding:16px 22px"><div style="font-size:14px;line-height:1.65;color:#c9c2bd">Agentic does not make <b>all</b> schema matter - it splits it. <b>Descriptive</b> schema (Article, breadcrumbs, FAQ) stays a minor entity signal. <b>Actionable</b> schema (Offer + price + availability, potentialAction, ContactPoint) is the structured path an agent transacts through. Add the actionable subset to your commercial pages, and <b>server-render it</b> - an agent that doesn't run JS can't see JS-injected schema, exactly like today's crawlers.</div></div></section>
   </div>
   <div class="apside">
     <div class="card2"><h3>Do this first</h3><div class="qd" style="line-height:1.6">On product / service pages, add an <b>Offer</b> with price + availability, and a <b>potentialAction</b> for the primary action (buy / book / contact). That is the minimum an agent needs to act on you.</div></div>
     <div class="card2"><h3>Why it isn't scored</h3><div class="qd" style="line-height:1.6">Agentic search is emerging, not mainstream, and agent support for Action schema is still nascent (it could be "declared but unread" for a while). So this is a forward-looking advisory - the same honest treatment as Grok and llms.txt - and it graduates to a scored "Actionable" pillar as agents arrive.</div></div>
   </div></div>`;
}
function aicrawlerView(){
 var ac=D.aicrawler||{bots:[],has_robots:false};
 var bots=ac.bots||[];
 var reach=(D.site_checks||[]).find(function(c){return c.id=='reachability'})||{};
 var col={allowed:'#3DD68C',partial:'#F0B429',blocked:'#E0533D'};
 var isBlk=function(r){return r===0||r==401||r==403;};               // HARD firewall deny (429 = rate limit, handled separately)
 var isRL=function(r){return r==429;};                               // rate-limited: inconclusive, likely our own probe burst, NOT a block
 var fwBlocked=function(b){return b.reach!=null&&isBlk(b.reach);};    // robots may allow, but the firewall 403s
 var effCol=function(b){return (b.status=='blocked'||fwBlocked(b))?col.blocked:(b.status=='partial'?col.partial:col.allowed);};
 var reachLabel=function(b){
   if(b.reach==null) return '<span style="width:104px;flex:none"></span>';
   if(isRL(b.reach)) return '<span title="429 rate-limit after retry, likely our probe burst, not a block" style="color:#F0B429;font-weight:700;font-size:11px;flex:none;width:104px;text-align:right">rate-limited</span>';
   var blk=isBlk(b.reach);
   return '<span style="color:'+(blk?'#E0533D':'#3DD68C')+';font-weight:700;font-size:11px;flex:none;width:104px;text-align:right">'+(blk?('firewall '+(b.reach||'x')):'reachable')+'</span>';
 };
 var serving=bots.filter(function(b){return b.role=='serving'});
 var servingBlocked=serving.filter(function(b){return b.status=='blocked'||b.status=='partial'||fwBlocked(b);});
 var allowedN=bots.filter(function(b){return b.status=='allowed'&&!fwBlocked(b);}).length;
 var ops=[],seen={};
 bots.forEach(function(b){if(!seen[b.op]){seen[b.op]=[];ops.push(b.op);}seen[b.op].push(b);});
 var roleBadge=function(r){return r=='serving'
   ?'<span style="font-size:9px;font-weight:800;color:#74E67A;letter-spacing:.05em">CITATION</span>'
   :'<span style="font-size:9px;font-weight:800;color:#8b8480;letter-spacing:.05em">TRAINING</span>';};
 var grid=ops.map(function(op){
   return '<div style="margin-top:15px"><div class="egh" style="margin-bottom:2px">'+esc(op)+'</div>'+seen[op].map(function(b){
     return '<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)">'
       +'<span class="dotb" style="background:'+effCol(b)+'"></span>'
       +'<span style="font-weight:600;font-size:13px;width:140px;flex:none">'+esc(b.name)+'</span>'
       +'<span style="width:62px;flex:none">'+roleBadge(b.role)+'</span>'
       +'<span class="qd" style="flex:1;font-size:12px;min-width:0">'+esc(b.purpose)+'</span>'
       +reachLabel(b)
       +'<span style="color:'+col[b.status]+';font-weight:700;font-size:12px;text-transform:uppercase;flex:none;width:64px;text-align:right">'+b.status+'</span></div>';
   }).join('')+'</div>';
 }).join('');
 var ch=(D.diff&&D.diff.bot_changes)||[];
 var pill=function(txt,cc){return '<span style="text-transform:uppercase;font-weight:700;font-size:12px;letter-spacing:.03em;color:'+cc+'">'+esc(txt)+'</span>';};
 var chH=ch.length?`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#F0B429"></span><h3>ACCESS CHANGED SINCE LAST CRAWL</h3><span class="meta">since ${esc((D.diff&&D.diff.since)||'')}</span></div><div class="apbox" style="padding:10px 22px 14px">`+ch.map(function(c){var cc=col[c.now]||'#F0B429';return '<div style="display:flex;align-items:baseline;gap:9px;padding:6px 0;font-size:13px"><b style="min-width:150px">'+esc(c.bot)+'</b>'+pill(c.was,'#8b8480')+'<span style="color:#8b8480">&rarr;</span>'+pill(c.now,cc)+'</div>';}).join('')+`</div></section>`:'';
 var headline;
 if(!ac.has_robots)headline='<span style="color:#F0B429">No robots.txt found - every bot is allowed by default. Fine for citation, but you have no control lever.</span>';
 else if(servingBlocked.length)headline='<span style="color:#ff9c88"><b>'+servingBlocked.length+' citation bot'+(servingBlocked.length==1?'':'s')+' restricted</b> ('+servingBlocked.map(function(b){return esc(b.name)}).join(', ')+') - those engines cannot fully cite you.</span>';
 else headline='<span style="color:#3DD68C"><b>All citation bots allowed.</b> '+allowedN+' of '+bots.length+' AI bots allowed overall.</span>';
 var searchRisk=bots.filter(function(b){return (b.name=='Googlebot'||b.name=='Bingbot')&&fwBlocked(b);});
 var probeCaveat='<div class="qd" style="margin-top:6px;font-style:italic">Caveat: we probe from a generic client, not the bot&#39;s real IP, so a WAF that verifies bots by IP may be blocking our impersonation while the real bot gets through. Treat a firewall block as a strong flag, then confirm in robots.txt and your server logs.</div>';
 var reachNote=searchRisk.length
   ?'<div class="qd" style="margin-top:8px;color:#ff9c88"><b>Search crawler blocked.</b> '+searchRisk.map(function(b){return esc(b.name)+' ('+b.reach+')'}).join(', ')+' is firewall-blocked, a Google/Bing <b>indexing</b> risk, not just an AI one. A WAF "block AI training" rule can 403 the search crawlers too (Cloudflare formalises this on 15 Sep 2026).</div>'+probeCaveat
   :(reach.status=='bad'
     ?'<div class="qd" style="margin-top:8px;color:#ff9c88">Live WAF test: '+esc(reach.detail||'')+'. A bot that robots.txt "allows" can still be blocked at the firewall.</div>'+probeCaveat
     :'<div class="qd" style="margin-top:8px">Live WAF test (citation-serving bots): '+esc(reach.detail||'reachable')+'.</div>');
 var noidx=(D.pages||[]).filter(function(p){return p.cs&&p.cs.noindex=='bad'});
 var noidxNote=noidx.length
   ?'<div class="qd" style="margin-top:6px;color:#ff9c88">Page level: '+noidx.length+' of '+(D.pages||[]).length+' pages are noindexed - excluded from AI citation regardless of bot access (see the Indexable check).</div>'
   :'<div class="qd" style="margin-top:6px">Page level: all '+(D.pages||[]).length+' pages are indexable - no per-page noindex suppressing citation.</div>';
 return `<div class="ap2"><div class="apmain">
   <div class="apsum" style="padding:24px 28px">
     <div class="apk">AI-CRAWLER EXPOSURE &middot; ADVISORY</div>
     <div style="font-size:14px;line-height:1.65;color:#c9c2bd;max-width:740px;margin-top:10px">Crawler access is the on/off switch for AI citation, and it is becoming a battleground (Cloudflare AI-blocking, pay-per-crawl, page-level controls). A blocked <b>citation</b> bot means that engine literally cannot quote you; a blocked <b>training</b> bot is a legitimate content-protection choice that does not stop live-search citation. This matrix pairs your robots.txt rules with a <b>live firewall probe</b> of the citation-serving bots (the ones that fetch at answer time), because a robots.txt "allow" means nothing if the firewall 403s the bot.</div>
     <div style="margin-top:14px;font-size:14px;line-height:1.55">${headline}${reachNote}${noidxNote}</div>
   </div>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>AI-BOT ACCESS MATRIX</h3><span class="meta">${allowedN} of ${bots.length} allowed &middot; from robots.txt</span></div><div class="apbox" style="padding:2px 22px 16px">${grid}</div></section>
   ${(function(){var la=D.log_analysis;if(!la)return '';
     var names=Object.keys(la.bots||{});
     if(!names.length) return '<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#6E7A6F"></span><h3>REAL AI-BOT ACTIVITY (SERVER LOG)</h3><span class="meta">'+(la.parsed||0).toLocaleString()+' lines, no AI-bot hits</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.6">Parsed '+(la.parsed||0).toLocaleString()+' log lines and found no AI-bot requests. Either the engines are not fetching you yet, or the log window is too short.</div></div></section>';
     var rows=names.map(function(nm){var b=la.bots[nm];
       var stH=Object.keys(b.statuses).map(function(s){var bad=(s=='401'||s=='403'||s=='429');return '<span style="font-family:var(--mono);font-size:11px;color:'+(bad?'#E0533D':(s=='200'?'#3DD68C':'#8b8480'))+'">'+s+':'+b.statuses[s]+'</span>';}).join(' ');
       var badge=b.role=='serving'?'<span style="font-size:9px;font-weight:800;color:#74E67A;margin-left:5px">CITATION</span>':'<span style="font-size:9px;font-weight:800;color:#8b8480;margin-left:5px">TRAINING</span>';
       var ct=b.citation_time?'<span title="fetches at citation time = live citation activity" style="font-size:9px;font-weight:800;color:#42D848;margin-left:4px">LIVE</span>':'';
       return '<div style="padding:9px 0;border-top:1px solid var(--line);display:flex;align-items:center;gap:10px"><span style="font-weight:600;font-size:13px;width:200px;flex:none">'+esc(nm)+badge+ct+'</span><span style="font-family:var(--mono);font-size:13px;font-weight:600;width:66px;flex:none">'+(b.hits||0).toLocaleString()+'</span><span class="qd" style="font-size:12px;flex:1;min-width:0">'+stH+(b.blocked?' &middot; <span style="color:#E0533D">'+b.blocked+' blocked</span>':'')+' &middot; '+b.days_seen+'d</span></div>';
     }).join('');
     return '<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>REAL AI-BOT ACTIVITY (SERVER LOG)</h3><span class="meta">'+(la.parsed||0).toLocaleString()+' lines &middot; '+(la.total_bot_hits||0).toLocaleString()+' AI-bot hits</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.65;margin-bottom:12px">Ground truth, not the probe estimate: the real AI bots, from their real IPs, and what they fetched. A <b>CITATION</b> bot with 200s is actively crawling you; a <b>LIVE</b> bot (ChatGPT-User, Perplexity-User, Claude-User) fetching is a citation happening in real time. Blocks here are real firewall denials of the real bot, the answer the reachability probe can only estimate.</div><div style="display:flex;gap:10px;font-size:10px;font-weight:800;letter-spacing:.5px;color:var(--muted);text-transform:uppercase;padding-bottom:2px"><span style="width:200px">Bot</span><span style="width:66px">Hits</span><span>Status &middot; blocks &middot; days seen</span></div>'+rows+'</div></section>';
   })()}
   ${chH}
   </div>
   <div class="apside">
     <div class="card2"><h3>What to do</h3><div class="qd" style="line-height:1.6">Allow every <b>CITATION</b> bot (OAI-SearchBot, PerplexityBot, ClaudeBot, Googlebot, Bingbot) - blocking one removes you from that engine's answers. <b>TRAINING</b> bots (GPTBot, Google-Extended, CCBot, Applebot-Extended) are your call: block them to keep content out of model training and you can still be cited via the live-search bots.</div></div>
     <div class="card2"><h3>How this is read</h3><div class="qd" style="line-height:1.6">Two signals per bot: the <b>robots.txt</b> rule (allowed / partial / blocked, exact UA match else the <code>*</code> fallback), and a <b>live fetch</b> as that bot to catch a firewall 403 robots cannot show. A red "firewall" beside a green "allowed" is the mismatch that matters. We probe the <b>citation-serving</b> bots (ChatGPT-User, OAI-SearchBot, PerplexityBot, Googlebot, Bingbot, ClaudeBot), <b>not GPTBot</b>, which is a training bot that does not fetch to cite. And <b>Google-Extended</b> governs Gemini/Vertex training only, not AI Overviews serving (that uses Googlebot).</div></div>
   </div></div>`;
}
function infogainView(){
 var g=D.infogain||{pages:[],bands:{high:0,medium:0,low:0},total:0};
 var b=g.bands||{high:0,medium:0,low:0}; var tot=g.total||1;
 var pl=function(u){return (u||'').replace(/^https?:\/\/[^/]+/,'')||'/';};
 var bandcol={high:'#3DD68C',medium:'#F0B429',low:'#ff9c88'};
 var gchip=function(txt){return '<span style="font-size:11px;padding:1px 7px;border-radius:10px;margin-right:5px;background:rgba(62,207,142,.14);color:#3DD68C">'+txt+'</span>';};
 var td='padding:9px 12px;border-top:1px solid var(--line)';
 var th='padding:9px 12px;position:static;background:#141110';
 var rows=(g.pages||[]).map(function(p){var col=bandcol[p.band]||'#8b8480';var fc=(p.figures>=15?'#3DD68C':(p.figures>=6?'#F0B429':'#8b8480'));
   var sc='';
   if(p.firsthand)sc+=gchip('first-hand');
   if(p.proprietary)sc+=gchip('proprietary');
   if(p.datatable)sc+=gchip('data table');
   if(p.dup)sc+='<span style="font-size:11px;padding:1px 7px;border-radius:10px;background:rgba(255,156,136,.14);color:#ff9c88">near-dup</span>';
   if(!sc)sc='<span class="qd" style="font-size:12px">figures only</span>';
   return '<tr>'
    +'<td style="'+td+';font-weight:600">'+esc(pl(p.url))+'</td>'
    +'<td style="'+td+';text-align:center"><span style="color:'+col+';font-weight:700;text-transform:uppercase;font-size:12px">'+p.band+'</span></td>'
    +'<td style="'+td+';text-align:center;color:'+fc+'">'+p.figures+'</td>'
    +'<td style="'+td+'">'+sc+'</td></tr>';}).join('');
 var tile=function(lbl,n,col){return '<div style="flex:1;background:#17130f;border:1px solid var(--line);border-radius:10px;padding:14px 16px;text-align:center"><div style="font-size:26px;font-weight:800;color:'+col+'">'+n+'</div><div class="qd" style="font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-top:2px">'+lbl+'</div></div>';};
 return `<div class="ap2"><div class="apmain">
   <div class="apsum" style="padding:24px 28px">
     <div class="apk">INFORMATION GAIN &middot; ADVISORY (PROXY, NOT SCORED)</div>
     <div style="font-size:14px;line-height:1.65;color:#c9c2bd;max-width:740px;margin-top:10px">Original data is the one moat AI cannot route around - pages that state their own numbers, first-hand research and named frameworks get cited where derivative rehash does not (Indig: 15+ distinct figures = information-gain 62.1 vs 40.2 for &le;1). A local crawler <b>cannot prove true originality</b> (no web-corpus to diff against), so this is a labelled <b>proxy</b>: distinct-figure density, first-hand-research language, a named proprietary asset, and real data tables - minus near-duplication.</div>
     <div style="display:flex;gap:12px;margin-top:18px">${tile('original / high',b.high||0,'#3DD68C')}${tile('some / medium',b.medium||0,'#F0B429')}${tile('thin / low',b.low||0,'#ff9c88')}</div>
   </div>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>PAGES BY INFORMATION-GAIN PROXY</h3><span class="meta">${tot} pages &middot; ranked most original first</span></div>
     <div class="apbox" style="padding:0"><table style="width:100%;border-collapse:collapse"><thead><tr style="text-align:left"><th style="${th}">Page</th><th style="${th};text-align:center">Band</th><th style="${th};text-align:center">Figures</th><th style="${th}">Original-data signals</th></tr></thead><tbody>${rows||'<tr><td colspan="4" style="padding:14px">no pages</td></tr>'}</tbody></table></div></section>
   </div>
   <div class="apside">
     <div class="card2"><h3>Do this first</h3><div class="qd" style="line-height:1.6">Take your <b>thin / low</b> pages and add something only you can say: a first-hand result, an original statistic with its source, a named framework, or a data table. One genuinely original figure beats ten borrowed ones.</div></div>
     <div class="card2"><h3>Why it's a proxy</h3><div class="qd" style="line-height:1.6">True information gain needs a web-corpus comparison this local tool does not do. These signals <b>correlate</b> with original content but cannot confirm novelty - so it is an honest directional read, not part of the CITED Score. The underlying stat / citation / sourced checks already feed the Trusted pillar.</div></div>
   </div></div>`;
}
function offpageView(){
 var o=D.offpage||{declared:[],missing:[],sameas_count:0};
 var pill=function(t,ok){return '<span style="display:inline-block;font-size:12px;font-weight:600;padding:4px 11px;border-radius:999px;margin:3px 4px 3px 0;'+(ok?'color:#3DD68C;background:rgba(62,207,142,.12);border:1px solid rgba(62,207,142,.3)':'color:#ff9c88;background:rgba(255,77,61,.1);border:1px solid rgba(255,77,61,.28)')+'">'+t+'</span>';};
 var declared=(o.declared||[]).map(function(d){return pill(d+' ✓',true)}).join('')||'<span class="qd">None declared in your schema.</span>';
 var missing=(o.missing||[]).map(function(m){return pill(m,false)}).join('')||'<span class="qd">You declare all the high-value surfaces - now earn active, well-reviewed presence on each.</span>';
 var plays=[
   ['Claim your Trustpilot profile','Claiming a Trustpilot review profile lifted AI citation rate from 1% to 54% (Trustpilot / Seer study). Highest-ROI single move.'],
   ['Get reviews on G2 / Capterra','Software categories with ~10% more G2 reviews average ~2% more AI citations (Kevin Indig / G2).'],
   ['Build Reddit + LinkedIn presence','Shopify: 44.5K Reddit + 15.4K LinkedIn mentions behind 45K AI mentions; Reddit is cited in ~1 in 5 AI answers (Semrush AI Visibility Index 2026).'],
   ['Publish original research for digital PR','Houzz\'s trends study earned 180+ backlinks from 81 domains and was cited in 188+ AI prompts (Semrush).'],
   ['Get into "best of" roundups','~90% of third-party AI mentions come from listicles / comparison / review roundups, and being in the top 3 of that page matters most (AirOps).']
 ];
 var playsH=plays.map(function(p,i){return '<div style="display:flex;gap:12px;padding:12px 0;'+(i?'border-top:1px solid var(--line)':'')+'"><span style="flex:none;width:22px;height:22px;border-radius:50%;background:#42D848;color:#140b06;font-weight:800;font-size:12px;display:flex;align-items:center;justify-content:center">'+(i+1)+'</span><div><div style="font-weight:700;font-size:14px">'+p[0]+'</div><div class="qd" style="line-height:1.55;margin-top:2px">'+p[1]+'</div></div></div>';}).join('');
 return `<div class="ap2"><div class="apmain">
   <div class="apsum" style="padding:24px 28px">
     <div class="apk">OFF-PAGE PRESENCE &middot; ADVISORY (NOT SCORED)</div>
     <div style="font-size:14px;line-height:1.65;color:#c9c2bd;max-width:730px;margin-top:10px">CITED Score audits your pages, but AI citation is dominated by <b>off-page</b> signals this on-page crawl cannot measure. A brand's own site is cited in only <b>~16%</b> of AI responses; the other ~84% are third-party sources (Reddit, YouTube, review sites, roundups), and brand mentions correlate with citation far more than backlinks (0.664 vs 0.218). This tab is directional guidance, not a score.</div>
   </div>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#3DD68C"></span><h3>PROFILES YOU DECLARE</h3><span class="meta">from your schema sameAs (${o.sameas_count} links)</span></div><div class="apbox" style="padding:14px 20px">${declared}</div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>MENTION DIVERSITY</h3><span class="meta">${(o.declared||[]).length} distinct declared domain${(o.declared||[]).length===1?'':'s'} &middot; parametric-authority proxy</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.65">What an AI model already <b>knows</b> about you, before it retrieves anything, is <b>parametric authority</b>. Research shows that only becomes reliable when your name appears in <b>varied phrasing across many independent sources</b>, not from self-publishing (a fact seen in too few, too-similar sources can sit in a model at near-zero recall). You currently declare <b>${(o.declared||[]).length}</b> distinct third-party domain${(o.declared||[]).length===1?'':'s'} in your schema. Treat that as a floor, not a measurement: real mention diversity, how many independent sites describe you, needs a backlink / mention tool. The more independent domains describe you, and the more varied the wording, the more reliably AI names you by default. This is slow and third-party-built, years not campaigns.</div></div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>HIGH-VALUE SURFACES TO SECURE</h3><span class="meta">AI-cited surfaces not in your declared set</span></div><div class="apbox" style="padding:14px 20px">${missing}<div class="qd" style="margin-top:10px;line-height:1.5">"Declared" only means present in your schema - it does not confirm an active, well-reviewed profile. Verify each, because these are where AI looks.</div></div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>DO THIS OFF-PAGE (data-backed)</h3><span class="meta">ranked by evidence</span></div><div class="apbox" style="padding:4px 20px 14px">${playsH}</div></section>
   ${(function(){var q=D.query_coverage;if(!q)return '';
     var gaps=(q.gaps||[]).map(function(g){return '<div style="display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line)"><span style="font-size:13px">'+esc(g.query)+'</span><span class="qd" style="white-space:nowrap;font-family:var(--mono);font-size:11px">'+g.citations+' cit &middot; match '+Math.round((g.best_score||0)*100)+'%</span></div>';}).join('');
     var orph=(q.orphans||[]).map(function(o){return '<span style="font-size:12px;padding:3px 9px;border-radius:999px;background:rgba(139,132,128,.12);border:1px solid var(--line)">'+esc(o.path)+' <b>'+o.score+'</b></span>';}).join('');
     return '<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#42D848"></span><h3>CITATION-QUERY COVERAGE</h3><span class="meta">'+q.covered+'/'+q.n_queries+' cited queries have a page targeting them</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.6;margin-bottom:12px">These are the grounding queries actually driving your AI citations, from your Bing AI Performance export. A <b>gap</b> is a query AI cites heavily where no page of yours targets it, the clearest opportunity. An <b>orphan</b> is a well-scored page that targets no cited query, effort not aimed at demand. Matching is title, meta and URL term overlap, so treat it as directional.</div>'
       +(q.n_gaps?'<div style="font-size:11px;font-weight:800;letter-spacing:.5px;color:#f0c674;text-transform:uppercase;margin:6px 0 8px">Coverage gaps ('+q.n_gaps+')</div>'+gaps:'<div class="qd">Every high-citation query has a page targeting it. Strong.</div>')
       +(q.n_orphans?'<div style="font-size:11px;font-weight:800;letter-spacing:.5px;color:var(--muted);text-transform:uppercase;margin:16px 0 8px">Orphaned pages ('+q.n_orphans+'), well-scored but targeting no cited query</div><div style="display:flex;flex-wrap:wrap;gap:6px">'+orph+'</div>':'')
       +'</div></section>';
   })()}
   </div>
   <div class="apside">
     <div class="card2"><h3>Why this isn't scored</h3><div class="qd" style="line-height:1.6">Off-page presence and brand mentions live outside your site, so an on-page crawler cannot measure them without a backlink / mention API. Rather than fake a number, this tab shows what you declare and where to build - the same honest treatment as the Grok and llms.txt advisories.</div></div>
     <div class="card2"><h3>Measure it properly</h3><div class="qd" style="line-height:1.6">To track real off-page presence use a backlink tool (Ahrefs / Semrush referring domains) plus an AI-visibility tracker for brand-mention share. First move with the biggest evidence: claim your Trustpilot profile (1% &rarr; 54% citation lift).</div></div>
     <div class="card2"><h3>Grounding queries &amp; Citation Share</h3><div class="qd" style="line-height:1.6"><b>Grounding queries</b> are the retrieval searches an AI runs before it answers; <b>Citation Share</b> and <b>Share of Authority</b> are how often your domain is cited, and cited versus competitors. An on-page crawl cannot see these. <b>Microsoft Clarity's Topic Insights</b> (free, if Clarity is on your site) exposes all three - it groups AI citations by topic so you can find coverage gaps and compare grounding queries to your existing content.</div></div>
   </div></div>`;
}
function exportBroken(){var bl=D.broken_links||[];var rows=[['status','broken_url','source_count','source_pages']];bl.forEach(function(b){rows.push([b.status==null?'dead':b.status,b.url,b.sources.length,b.sources.join(' | ')]);});dl('cited-broken-links-'+(D.domain||'site')+'.csv',rows);}
function brokenView(){
 var bl=D.broken_links||[], dom=D.domain||'';
 var isInt=function(u){return u.indexOf('//'+dom)>=0||u.indexOf('//www.'+dom)>=0};
 if(!bl.length) return `<div class="wrap"><div class="sech">Broken outbound links</div><div class="panel"><div style="display:flex;align-items:center;gap:10px"><span style="width:9px;height:9px;border-radius:50%;background:#3DD68C"></span><b>No broken links found.</b></div><div class="qd" style="margin-top:8px">Checked every outbound content link across the crawl. Only genuinely dead targets count (404/410/5xx); 403/429 bot-blocks and timeouts are excluded to avoid false positives.</div></div></div>`;
 var internal=bl.filter(b=>isInt(b.url));
 var rows=bl.slice().sort((a,c)=>((isInt(a.url)?0:1)-(isInt(c.url)?0:1))||(c.sources.length-a.sources.length));
 var th=t=>`<th style="padding:10px;font-size:11px;color:#8b8480;font-weight:600;text-align:left;position:static;background:transparent">${t}</th>`;
 var body=rows.map(function(b){
   var ii=isInt(b.url);
   var srcs=b.sources.map(u=>`<a href="${esc(u)}" target="_blank" style="color:#b7afaa">${rel(u)}</a>`).join(', ');
   return `<tr style="border-top:1px solid var(--line)">
     <td style="padding:9px 10px;vertical-align:top"><span class="schip" style="background:rgba(255,77,61,.14);color:#ff9c88">${esc(String(b.status||'dead'))}</span></td>
     <td style="padding:9px 10px;vertical-align:top;max-width:430px">${ii?'<span style="font-size:10px;font-weight:700;color:#140b06;background:#42D848;padding:1px 6px;border-radius:999px;margin-right:6px">INTERNAL</span>':''}<a href="${esc(b.url)}" target="_blank" style="word-break:break-all;font-size:13px">${esc(b.url)}</a></td>
     <td style="padding:9px 10px;vertical-align:top;text-align:center;color:#b7afaa">${b.sources.length}</td>
     <td style="padding:9px 10px;vertical-align:top;font-size:12px">${srcs}</td>
   </tr>`;
 }).join('');
 return `<div class="wrap">
   <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:10px"><div class="sech" style="margin:0">Broken outbound links <span class="s">${bl.length} dead${internal.length?' &middot; '+internal.length+' internal':''} &middot; 404/410/5xx only, bot-blocks excluded</span></div><button onclick="exportBroken()" style="background:var(--grn);color:#140b06;border:0;border-radius:6px;padding:7px 14px;font-weight:700;font-size:12px;cursor:pointer">Export CSV</button></div>
   <div class="panel" style="overflow-x:auto;padding:0;margin-top:14px">
     <table style="width:100%;border-collapse:collapse">
       <thead><tr>${th('STATUS')}${th('BROKEN URL')}<th style="padding:10px;font-size:11px;color:#8b8480;font-weight:600;text-align:center;position:static;background:transparent">ON</th>${th('SOURCE PAGES')}</tr></thead>
       <tbody>${body}</tbody>
     </table>
   </div>
   <div class="qd" style="margin-top:10px">Internal broken links are top priority. 403/429 (bot-blocked) links and timeouts are deliberately excluded to avoid false positives. The full list is also in the CSV / JSON export.</div>
 </div>`;
}
function printReport(){const Q={Known:'Do they know you?',Findable:'Can they find your answer?',Trusted:'Do they trust you?'};
 document.getElementById('printroot').innerHTML=`<h1>${SCORELABEL} — ${esc(D.domain)}</h1><p>${D.pages_crawled} pages · ${esc(D.generated)} · Overall ${D.overall}/100</p>`+
  `<h2>Action plan</h2>`+D.issues.map((i,x)=>`<p><b>${x+1}. ${esc(i.label)}</b> [${i.pillar}, ${i.ch}, ${i.effort}] ${i.gain_overall>0?'(+'+i.gain_overall+' overall)':''}<br>${esc(i.fix)} — ${i.count} pages</p>`).join('')+
  `<h2>All pages</h2><table><thead><tr><th>Score</th><th>URL</th><th>Kn</th><th>Fi</th><th>Tr</th></tr></thead><tbody>`+
  D.pages.map(p=>`<tr><td>${p.score}</td><td>${rel(p.url)}</td><td>${p.pillars.Known}</td><td>${p.pillars.Findable}</td><td>${p.pillars.Trusted}</td></tr>`).join('')+`</tbody></table>`;
 window.print()}
tabsbar();render();
"""
    _wl=bool(d.get('client'))                                    # white-label mode when a client is set
    _mark="<svg viewBox='0 0 200 200' width='29' height='29' style='flex:none'><path d='M148.3,50.1 A72,72 0 1 0 158.7,127' fill='none' stroke='#EDF0EB' stroke-width='17' stroke-linecap='square'/><path d='M70,101 L92,123 L135.7,67.5' fill='none' stroke='#42D848' stroke-width='17' stroke-linecap='square'/></svg>"
    _hdr_logo=(f"<img src=\"{d['logo']}\" alt='' style='height:30px;flex:none;max-width:220px'>" if d.get('logo') else ("" if _wl else _mark))
    _hdr_wm=(f"<span class='wm'><span class='lw'>{H.escape(d.get('agency') or 'GoGoChimp')}</span></span>" if _wl else "<span class='wm'><span class='lw'>CITED</span><span class='ls'>Score</span></span>")
    doc=("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{'AI Search Audit' if _wl else 'CITED Score'}: {H.escape(d['domain'])}</title><link rel='icon' href=\"{FAVICON}\">"
         "<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
         "<link href='https://fonts.googleapis.com/css2?family=Archivo:wght@400..900&family=IBM+Plex+Mono:wght@400;500;600&display=swap' rel='stylesheet'>"
         f"<style>{css}</style></head><body>"
         f"<header><span class='logo'>{_hdr_logo}{_hdr_wm}</span>"
         f"<span class='m'><a href='{H.escape(d['origin'])}' target='_blank' style='color:var(--txt);font-weight:600'>{H.escape(d['domain'])}</a> &middot; {d['pages_crawled']} pages &middot; {d['generated']}</span>"
         "<span class='btns'><button onclick='printReport()'>Print / PDF</button><button onclick='exportPages()'>Export CSV</button></span></header>"
         + ((f"<div style='padding:14px 24px;background:linear-gradient(90deg,rgba(66,216,72,.10),transparent);border-bottom:1px solid var(--line);font-size:14px'><span style='color:#8b8480'>AI Search Audit prepared for</span> <b style='font-size:16px'>{H.escape(d.get('client') or '')}</b> <span style='color:#8b8480'>by {H.escape(d.get('agency') or 'GoGoChimp')}</span>" + (f"<div style='color:#c9c2bd;line-height:1.6;margin-top:8px;max-width:820px'>{H.escape(d.get('intro') or '')}</div>" if d.get('intro') else "") + "</div>") if d.get('client') else "")
       + "<div class='tabs' id='tabs'></div><div id='app'><div class='wrap' id='view'></div>"
       + ("<div class='foot'>This report <b>estimates citability</b> for AI search from on-page, structural and technical signals. It does <b>not</b> measure citations. llms.txt and Grok are shown for reference only and are not scored.</div></div>" if _wl
          else "<div class='foot'>The CITED Score <b>estimates citability</b> from on-page, structural and technical signals. It does <b>not</b> measure citations. For measured citations, calibrate the model against your Bing Webmaster Tools AI Performance export (<code>--calibrate citations.csv</code>). Every check carries a source (engine documentation, first-party citation data, or a CITED chapter). llms.txt and Grok are shown for reference only and are not scored (Ch5): llms.txt shows no citation correlation, and Grok has no citation export to calibrate against.</div></div>")
       + "<div id='printroot'></div>"
         f"<script>window.__DATA__={payload};</script><script>{js}</script></body></html>")
    with open(path,"w",encoding="utf-8") as f: f.write(doc)

def run_audit(url, out="report", max_pages=0, workers=WORKERS, progress=None, client=None, intro=None, links=True, site_type=None, queries=None, logs=None, agency=None, logo=None):
    """Crawl + score a whole site and write out.html/.json/.csv. progress(phase, done,
    total, msg) is called through the run so a UI can show live status. Returns the data."""
    if not url.startswith("http"): url="https://"+url
    _logo_uri=None
    if logo:                                          # white-label: embed the agency/client logo as a data URI
        try:
            import base64, mimetypes
            mt=mimetypes.guess_type(logo)[0] or "image/png"
            with open(logo,"rb") as _lf: _logo_uri=f"data:{mt};base64,"+base64.b64encode(_lf.read()).decode()
        except Exception: _logo_uri=None
    p=urllib.parse.urlparse(url); domain=p.netloc.replace("www.",""); origin=f"{p.scheme}://{p.netloc}"
    def emit(phase,done,total,msg):
        if progress: progress(phase,done,total,msg)
    emit("discover",0,0,f"Discovering URLs for {domain}...")
    def _reach(u):                                   # fail-fast: don't grind sitemap discovery on a dead/blocked site
        for _ in range(2):
            s,_,_,_=fetch_raw(u, timeout=8)
            if s is not None: return s
        return None
    _start_st=_reach(url); unreachable = _start_st is None or _start_st in (401,403)
    if unreachable:
        emit("discover",0,1,f"{domain} did not respond (timeout or bot-block); auditing only what we can reach")
        urls,sitemap_paths=[url],set()
    else:
        urls,sitemap_paths=all_urls(origin,domain,max_pages,url)
    total=len(urls); done=[0]
    emit("discover",0,total,f"{total} URLs to crawl")
    def work(u):
        r=process(u,domain); done[0]+=1
        emit("crawl",done[0],total,f"{r['status']} {u}")
        return r
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pages=list(ex.map(work,urls))
    emit("site",total,total,"Site-wide checks...")
    sitecx=site_checks(origin,domain) if not unreachable else []   # skip site-wide network probes on an unreachable site
    linkstatus={}
    if out and links and not unreachable:      # skip on an unreachable site (nothing to check + avoids more timeouts)
        emit("links",0,0,"Checking outbound links...")
        linkstatus=check_links(pages, progress=lambda d,t,m: emit("links",d,t,m))
    protocols=agent_protocols(origin) if (out and not unreachable) else {}    # agent protocol/discovery probe (advisory)
    aicrawler=ai_crawler_matrix(origin) if (out and not unreachable) else {}  # AI-bot access matrix (advisory monitor)
    data=build(domain,origin,pages,sitecx,sitemap_paths,linkstatus,client,intro,protocols,aicrawler,site_type_override=site_type,agency=agency,logo=_logo_uri)
    if sum(1 for pg in pages if pg.get("status")==200)==0:       # nothing crawlable -> flag it clearly, do not present a 0 as a citability score
        data["crawl_failed"]=True
        data["crawl_note"]=((f"{domain} did not respond (timeout or network-level bot protection)" if unreachable
                             else f"0 of {len(pages)} crawled page(s) returned 200; the site firewall is likely rate-limiting or blocking the crawler")
                            +". This is a reachability problem, not a citability score. Try again shortly, lower the worker count, or the site may hard-block bots.")
    if out:
        if not unreachable:                                    # skip the extra probes on a site we could not reach
            try: data["internal_search"]=audit_internal_search(pages,origin)   # citation-to-landing (b): honest probe of the site's own search
            except Exception: data["internal_search"]={"detected":False}
            if queries:                                        # citation-query coverage: which cited queries a page targets
                try: data["query_coverage"]=query_coverage(pages, load_queries(queries))
                except Exception: pass
        if logs:                                               # log analysis: real AI-bot behaviour from the server access log (independent of the crawl)
            try: data["log_analysis"]=analyze_logs(logs)
            except Exception: pass
        apply_diff(data,out); write_outputs(data,out)         # out=None -> crawl + score only, no files (used by benchmark)
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
    ap.add_argument("--client",help="white-label: client name shown in the report banner")
    ap.add_argument("--intro",help="white-label: optional intro line shown under the client banner")
    ap.add_argument("--agency",help="white-label: agency name delivering the report (replaces GoGoChimp)")
    ap.add_argument("--logo",help="white-label: path to an agency/client logo image to embed in the report header")
    ap.add_argument("--logs",help="server access log (Apache/Nginx combined) for real AI-bot log analysis")
    ap.add_argument("--no-links",action="store_true",help="skip the broken-link check (faster)")
    ap.add_argument("--queries",help="grounding-query CSV (Bing AI Performance 'AI Search Queries' export) for citation-query coverage")
    ap.add_argument("--monitor",help="server access log for the standing LOG MONITOR (per-day time-series of AI-bot activity)")
    ap.add_argument("--history",help="JSONL history file for --monitor to accumulate across uploads (persists per-day)")
    ap.add_argument("--check-draft",dest="check_draft",help="path to a draft HTML/text file: pre-publish citability check, prints JSON")
    a=ap.parse_args()
    if a.monitor:
        print(json.dumps(monitor_logs(a.monitor, a.history), indent=2, ensure_ascii=False)); return
    if a.check_draft:
        with open(a.check_draft,encoding="utf-8",errors="ignore") as _f: _txt=_f.read()
        print(json.dumps(check_draft(_txt, url="https://draft.local/"+os.path.basename(a.check_draft)), indent=2, ensure_ascii=False)); return
    if a.calibrate:
        calibrate(a.report or (a.out+".json"), a.calibrate); return
    if not a.url: ap.error("--url required (or use --calibrate with --report)")
    print(f"Chrome: {CHROME or 'NONE (raw only)'}")
    def prog(phase,done,total,msg):
        print(f"  [{done}/{total}] {msg}" if phase=="crawl" else msg, flush=True)
    data=run_audit(a.url,out=a.out,max_pages=a.max_pages,workers=a.workers,progress=prog,client=a.client,intro=a.intro,links=not a.no_links,queries=a.queries,logs=a.logs,agency=a.agency,logo=a.logo)
    print(f"\n=== CITED Score: {data['domain']} === {data['overall']}/100 | {data['pages_crawled']} pages")
    print("Pillars: "+" | ".join(f"{k} {v}" for k,v in data['pillars'].items()))
    print("Engines: "+" | ".join(f"{e} {v}" for e,v in data['engines'].items()))
    top=data['issues'][:3]
    print("Do first: "+" ; ".join(f"{i['label']} (+{i['gain_overall']})" for i in top))
    print(f"Report: {a.out}.html / .json / .csv")

if __name__=="__main__": main()
