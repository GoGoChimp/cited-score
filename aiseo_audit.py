#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rubric - the companion auditor to the book CITED (Chris McCarron / GoGoChimp).
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
import argparse, json, os, re, subprocess, sys, csv, html as H, datetime, time, threading, tempfile, base64, hashlib, gzip, zlib
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Identifies this revision of the scoring (the set of checks and their weights). run_audit stamps it on every result as
# data["scoring_version"]; the worker records it on each crawl_page_checks row (so the proof loop only diffs like with
# like) and on site_proof (so a change here makes the scheduler recompute every stored proof). BUMP IT whenever a check is
# added, removed, renamed or re-weighted, or the scoring otherwise changes; leave it alone for copy, layout or other
# changes that cannot move a score.
SCORING_VERSION = "2026-10-01"

# ------------------------------------------------------------------ SSRF guard (online worker only)
# When CITED_SSRF_GUARD=1 (set by the public online worker), every fetch/render target must resolve to a
# PUBLIC IP and redirects are re-validated per hop. Default OFF, so the desktop app and local CLI - where a
# trusted operator audits their own sites, including intranet hosts - stay byte-for-byte unaffected.
import socket, ipaddress

def _ssrf_on():
    return os.environ.get("CITED_SSRF_GUARD", "") == "1"

def _ip_public(ip):
    try: a = ipaddress.ip_address(ip)
    except ValueError: return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified)

def ssrf_ok(url):
    if not _ssrf_on(): return True
    try: host = urllib.parse.urlparse(url).hostname
    except Exception: return False
    if not host: return False
    try: infos = socket.getaddrinfo(host, None)      # resolve A + AAAA
    except Exception: return False                   # unresolvable -> treat as unsafe under guard
    addrs = {i[4][0] for i in infos}
    return bool(addrs) and all(_ip_public(a) for a in addrs)

class _GuardRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not ssrf_ok(newurl):
            raise urllib.error.HTTPError(newurl, code, "blocked (redirect to non-public host)", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_GUARD_OPENER = urllib.request.build_opener(_GuardRedirect())

# ------------------------------------------------------------------ check catalog
# pillar: Known (Discovery, do I know you?) / Findable (Retrieval, can I find your
# answer?) / Trusted (Citation, do I trust you enough to name you?). ch = CITED chapter.
CHECK_META = {
 # KNOWN - entity recognition (ch4 Entities & Trust)
 "schema":     {"label":"Content-type schema (Article/Service/etc.)","pillar":"Known","ch":"Ch4","phase":1,"effort":"Med",
                "ev":"Schema classifies the page as an entity the engine can attribute (Ch4)."},
 "parity":     {"label":"Schema readable without JavaScript","pillar":"Known","ch":"Ch4","phase":1,"effort":"Low",
                "ev":"Non-JS AI crawlers never run your JavaScript, so JS-injected schema is invisible to them (Ch4)."},
 "readwindow": {"label":"Answer inside the no-JS read window","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"ChatGPT Deep Research reads linearised HTML with NO JavaScript, no clicks, in a ~5,700-char window; an answer that is JS-injected or buried under nav/hero past that window never gets read (Peec/Konitzny 200-session capture, 2026, Ch5)."},
 "canonical":  {"label":"Canonical tag present","pillar":"Known","ch":"Ch4","phase":1,"effort":"Low",
                "ev":"A self-referencing canonical stops duplicate-entity confusion (Ch4)."},
 "internal":   {"label":"Internal links in content (>=3)","pillar":"Known","ch":"Ch4","phase":2,"effort":"Med",
                "ev":"Internal links build the topical cluster engines read as authority (Ch4)."},
 "entity":     {"label":"Entity clarity (Organization/Person + sameAs)","pillar":"Known","ch":"Ch4","phase":1,"effort":"Med",
                "ev":"sameAs to Wikipedia/Wikidata/LinkedIn plus a stable @id let the engine resolve WHO you are AND mark you as the first-party 'official' source AI probes for: ChatGPT's fan-out runs site: probes on 64% of queries and prioritises official first-party sources (Nectiv 2026). Entity is ALSO one of the two fan-out types (with Comparison) that drove ~97% of all brand mentions across a 50,000-prompt study (Moz, 2026), so this is the sub-query where AI decides who you are. The core of AI visibility (entity recognition, Ch4)."},
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
 "snippetlead":{"label":"H1 lands in the AI snippet window","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Low",
                "ev":"ChatGPT's index snippets ~200 chars from the page's leading content and pulls the H1 83.6% of the time; if breadcrumbs, kickers or hero clutter push the H1 out of that window the snippet loses your topic (Resoneo 1,249-answer study 2026, Ch5)."},
 "answerfirst":{"label":"Answer-first opener (40-70 words)","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"The opening 40-60 words are the chunk the machine lifts; AIO answers run ~67 words median (Pew 2026, Ch5)."},
 "answerlead": {"label":"Sections open with a direct answer","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"Rerankers lift the passage that ANSWERS; a section that opens on an orphan pronoun ('it', 'this') or a slow wind-up hands the engine no self-contained lead. Named the single highest-leverage on-page edit (Peec reranker analysis, 2026, Ch5)."},
 "qheadings":  {"label":"Question / claim-shaped H2-H3s","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"Question headings match the retrieval query (Google AIO guidance, Ch5)."},
 "sections":   {"label":"Self-contained sections (no walls of text)","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"Retrieval reranks 40-180 word passages; a wall of text hands the engine no clean chunk (Firecrawl 2026, Ch5). Not a word-count target: each section just needs its own liftable answer."},
 "liststables":{"label":"Tables / lists present","pillar":"Findable","ch":"Ch5","phase":2,"effort":"Med",
                "ev":"80% of AI-cited pages use lists or structured elements (Profound 2026, Ch5)."},
 "video":      {"label":"Video / YouTube present","pillar":"Findable","ch":"Ch5","phase":2,"effort":"High",
                "ev":"AI Overviews leans on YouTube; an embedded video or VideoObject adds a citable modality (Ch5)."},
 "comparison": {"label":"Comparison / best-of content on the site","pillar":"Findable","ch":"Ch5","phase":2,"effort":"High",
                "ev":"Comparison and best-of pages are ~33% of AI citations, the format engines cite most, and Comparison is one of the two fan-out types (with Entity) that drove ~97% of all brand mentions across a 50,000-prompt study (Moz, 2026): the sub-query where AI decides who is best. Caveat (Ahrefs 2026, 34 lists / 9,886 answers): a SELF-published 'we're #1' list gets cited but the AI recommends a competitor listed inside it - the recommendation lift comes from THIRD-PARTY roundups, so treat your own comparisons as citation and impression assets, not recommendation drivers (Ch5)."},
 "faq":        {"label":"FAQPage / HowTo schema","pillar":"Findable","ch":"Ch5","phase":1,"effort":"Med",
                "ev":"FAQ schema hands the engine pre-chunked question-answer pairs (Ch5)."},
 "alt":        {"label":"Image alt coverage (>=90%)","pillar":"Findable","ch":"Ch5","phase":1,"effort":"Low",
                "ev":"Alt text lets engines read and reuse your images (Ch5)."},
 "robots":     {"label":"robots.txt allows AI search bots","pillar":"Findable","ch":"Ch7","phase":1,"effort":"Low",
                "ev":"A Disallow on GPTBot / PerplexityBot / Google-Extended / OAI-SearchBot blocks citation outright, and OAI-SearchBot is the bot that governs ChatGPT search visibility. Note robots.txt does NOT reliably stop ChatGPT-User: OpenAI states user-initiated fetches may ignore robots (2026), so the WAF/reachability test below is the real control there (Ch7)."},
 "sitemap":    {"label":"XML sitemap present","pillar":"Findable","ch":"Ch7","phase":1,"effort":"Low",
                "ev":"The sitemap is how engines discover every page (Ch7)."},
 "reachability":{"label":"AI crawlers not network-blocked","pillar":"Findable","ch":"Ch7","phase":1,"effort":"Low",
                "ev":"A Cloudflare/WAF 403 on the citation-serving bots (ChatGPT-User, OAI-SearchBot, Googlebot, Bingbot) silently blocks citation. This is the ONLY reliable control for ChatGPT-User: OpenAI states robots.txt may not apply to it because the fetch is user-initiated (2026), so a robots Disallow is theatre and the network/WAF response is what actually decides reachability. Blocking Google/Bing is a search-indexing risk too (live reachability test, Ch7)."},
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
                "ev":"Engines weight recency: ~90% of AI-bot crawl activity is on content under 3 years old (Seer 2025), and undated content loses to dated (Ch4)."},
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

# --- "What your SEO crawler can't see" (build spec D). Which checks are NOT in the DEFAULT output of a standard
# SEO crawler. Kept as an ordered id list so membership is a clean boolean and the count is DERIVED, never a
# hardcoded literal (add a check -> the stat updates itself). Verified Aug 2026 against the documented default
# output of the four named tools -- a claim that names its comparison set and dates it is checkable, not
# assertable (same discipline as the 491-site study). LEAD WITH THE LIST, NOT THE FRACTION (a ratio invites
# arithmetic; the list is unarguable). 'reachability' heads it: the one item with published research proving the
# category's user-agent-probe method gets it wrong, and the hardest to copy.
STD_AUDIT_TOOLS = "Screaming Frog, Sitebulb, Ahrefs Site Audit and Semrush Site Audit"
STD_AUDIT_CHECKED = "August 2026"
NOT_IN_STANDARD_AUDIT = ["reachability", "parity", "readwindow", "snippetlead", "answerfirst", "answerlead",
    "qheadings", "sections", "h2answer", "answerthird", "definitional", "liststables", "rankedlist",
    "comparison", "video", "statdensity", "sourced", "citations", "entitydensity"]
# Extracted by standard crawlers but NOT weighted for AI citation -- the honest nuance lives here, as a note
# rather than a third enum value. These stay flagged in-standard; the note is what belongs in the copy.
STD_AUDIT_NOTES = {
    "robots": "Standard crawlers parse robots.txt and Semrush now flags AI-user-agent disallows; the citation consequence of that allow/deny is the AI-SEO reading.",
    "entity": "sameAs is a schema property standard crawlers extract; weighting it for how an engine resolves who you are is ours (entity SEO is its own discipline).",
    "schemacomplete": "Validators already report missing recommended properties; 'rich enough to be cited' is our reading of the same extracted data.",
    "reviewschema": "Review / AggregateRating is a standard rich-result check; the 'AI recommends the well-reviewed' consequence is the AI-SEO layer.",
    "freshness": "Every crawler reads last-modified; 'engines weight recency' is the AI-SEO angle on it.",
    "readability": "Sitebulb reports readability already; 'cited text reads at about grade 16' is our angle on the same score.",
}

# --- Who does the work (build spec C). Every check carries an owner so a 36-item plan lands as three assignable
# lists, not a wall: dev (code / schema / templates / performance), content (copy, answers, structure, links),
# config (a settings / robots / sitemap / redirect job someone finishes this afternoon). No nulls, no
# default-by-omission -- every scored check is assigned deliberately. CHECK_OWNER_2 carries an optional secondary
# for the few fixes that genuinely span two desks (write the FAQ answers AND mark them up as schema).
CHECK_OWNER = {
    # dev -- code, JSON-LD, templates, server performance
    "schema":"dev","parity":"dev","entity":"dev","schemacomplete":"dev","schemavalidity":"dev","reviewschema":"dev",
    "faq":"dev","h1":"dev","speed":"dev",
    # config -- a settings / infra job, not code and not copy
    "canonical":"config","http":"config","robots":"config","sitemap":"config","reachability":"config","noindex":"config",
    # content -- copy, answers, structure, links, freshness
    "readwindow":"content","internal":"content","title":"content","meta":"content","snippetlead":"content",
    "answerfirst":"content","answerlead":"content","qheadings":"content","sections":"content","liststables":"content",
    "video":"content","comparison":"content","alt":"content","wordcount":"content","statdensity":"content",
    "citations":"content","author":"content","sourced":"content","freshness":"content","entitydensity":"content",
    "readability":"content","definitional":"content","duplicate":"content","rankedlist":"content","answerthird":"content",
    "h2answer":"content","orphans":"content","brokenlinks":"content","nearduplicate":"content",
}
CHECK_OWNER_2 = {  # secondary desk, only where the fix genuinely spans two
    "faq":"content","author":"dev","reviewschema":"content","entity":"content","snippetlead":"dev",
}
OWNER_LABEL = {"dev":"Developer","content":"Writer / content","config":"Config / settings"}
PILLARS = ["Known","Findable","Trusted"]
FIX = {
 "http":"Return 200 or 301 the URL to a live page.",
 "title":"Write a 15-65 char title leading with the topic, not the brand.",
 "meta":"Write a 50-160 char meta description that opens with the answer.",
 "h1":"Use exactly one H1 that states the page topic.",
 "snippetlead":"Put the H1 near the very top of the content, above breadcrumbs, category kickers, dates and hero clutter, so it lands in the ~300-char AI snippet lead.",
 "wordcount":"Add substantive, unique depth so the page has a real answer to lift.",
 "answerfirst":"Put a direct, self-contained 40-70 word answer in the first two sentences (any shape - definition, gerund or plain statement).",
 "answerlead":"Open each section with a one-sentence self-contained answer (name the subject, don't start with 'it'/'this'), then elaborate.",
 "qheadings":"Rephrase H2/H3s as the questions or claims users actually search.",
 "sections":"Break walls of text so each section carries its own liftable answer (no artificial chopping - just one idea per block).",
 "liststables":"Add an HTML comparison table or numbered list for the key facts.",
 "statdensity":"Add sourced numbers, roughly one verifiable fact per 80 words.",
 "citations":"Cite two or more external authorities with inline links.",
 "schema":"Add server-rendered content-type JSON-LD: Article/BlogPosting for posts, Service/SoftwareApplication/Book/etc. for other pages.",
 "faq":"Add 6-10 FAQPage Q&A pairs, each a self-contained 40-60 word answer.",
 "parity":"Server-render the JSON-LD so non-JS AI crawlers can read it (Page Settings head, not a JS embed).",
 "readwindow":"Put your core answer in plain server-rendered HTML near the top, above the nav/hero clutter, so it lands in the first ~5,700 chars a no-JS reader ingests.",
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
 "entity":"Add Organization + Person JSON-LD with a stable @id and sameAs to your Wikipedia/Wikidata/LinkedIn/Crunchbase profiles, so your page is the machine-readable first-party source for your brand.",
 "schemacomplete":"Fill the schema out: author, datePublished, dateModified, headline and image, not just @type and name.",
 "video":"Embed a relevant YouTube video or add VideoObject schema for the key explainer or how-to.",
 "author":"Add a named author (Person schema with name + sameAs) and a visible byline with credentials.",
 "sourced":"Cite an authoritative external source next to each statistic, as an inline link.",
 "comparison":"Publish at least one comparison or best-of page (X vs Y, best X for Y) with a structured table. To be RECOMMENDED (not just cited), earn placement in THIRD-PARTY roundups too - a self-authored 'we're #1' list gets cited but AI tends to name a competitor from within it.",
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
# Deeper, example-led fix guidance shown in the action plan (Wave 2). The "what do I actually do" layer
# on top of the one-line FIX. Only the highest-impact checks carry it; the rest fall back to FIX.
FIX_DEEP = {
 "answerfirst":"Open with a 40-70 word answer to the exact question the H1 implies, before any preamble (AIO answers run ~67 words median, so up to ~70 is fine). Engines lift the first self-contained passage that answers the query, so for 'What is AI CRO?' lead with 'AI CRO is...' in one tight paragraph, then expand below. Any answer shape counts here; the separate 'definitional opener' check is the one that specifically rewards an '[X] is ...' construction.",
 "answerlead":"Lead EVERY H2/H3 section with a single self-contained answer sentence, then elaborate below it. Name the subject explicitly instead of opening with 'it'/'this'/'that' - retrieval splits a page into passages at its headings, so a section that only makes sense after the ones above it, or that buries its answer under a wind-up, will not be lifted. Answer first, elaboration after, never instead of (Peec reranker analysis, 2026).",
 "qheadings":"Rewrite H2/H3s as the questions or claims people search, not labels. 'Pricing' becomes 'How much does X cost?'; 'Benefits' becomes 'Why use X?'. Each heading plus the 40-60 words under it should stand alone as a citable answer.",
 "snippetlead":"Move the H1's answer into the first ~300 characters. Engines read a short window first, so if the answer sits below a long intro or nav it is missed. Front-load the payload.",
 "sections":"Break walls of text into self-contained blocks of 40-120 words, each under its own question-shaped heading. Engines extract passages, not pages: a block that only makes sense after the three above it will not be quoted.",
 "liststables":"Turn comparisons, steps and specs into real table/list markup, not prose or images. 'X vs Y' and 'best N for Z' answers are pulled straight from tables; a paragraph of the same data is far less extractable.",
 "comparison":"Add at least one genuine comparison or best-of page ('X vs Y', 'best N for Z') with a structured table. Comparison content is roughly a third of AI citations; a site with none forfeits the single most-cited format. Ceiling to know (Ahrefs 2026): a SELF-published list that ranks you #1 still earns the citation, but AI frequently recommends a competitor listed inside it - the recommendation lift comes from THIRD-PARTY roundups and category ownership. Publish your own comparisons for the citation and impression; pursue earned placement in others' roundups for the recommendation.",
 "statdensity":"Raise specific, sourced numbers to about 1.5 per 100 words: dates, percentages, counts, benchmarks. 'Cut load time 42% in 30 days' is citable; 'much faster' is not. Numbers are what engines quote.",
 "citations":"Add at least two outbound links to authoritative primary sources (original research, standards, official docs). Citing sources is the biggest GEO visibility lever and signals a page engines can trust to quote.",
 "sourced":"Attribute every statistic to a named source inline ('according to Seer, 2025') and link it. An unsourced number reads as an assertion; a sourced one is a fact an engine will repeat with your page as the reference.",
 "entity":"Add Organization or Person schema with sameAs links to your Wikipedia/Wikidata/LinkedIn/Crunchbase profiles, server-rendered. This is how engines resolve who you are and mark you as the first-party source they probe with site: queries.",
 "schema":"Add the right content-type schema (Article, Product, Service, HowTo) as server-rendered JSON-LD so engines know what the page IS and retrieve it correctly. Match the type to the page; do not bolt Article onto everything.",
 "parity":"Server-render schema and key content into the initial HTML, not injected by JavaScript. ChatGPT, Perplexity and Claude bots do not run JS, so schema they cannot see cannot help you even when Google renders it fine.",
 "readwindow":"ChatGPT Deep Research reads linearised HTML with no JavaScript, never clicks tabs/accordions, and stops around 5,700 characters. So the answer must be in plain server-rendered HTML AND near the top: a JS-injected answer is invisible, and one buried under a big nav or hero (60+ nav links can eat two-thirds of the window) is never reached. Front-load a plain-text answer paragraph high in the source, and keep the critical content out of JS-only components.",
 "schemacomplete":"Fill the required and recommended properties, not just @type. A Product with no price/brand/review, or an Article with no author/datePublished, is treated as thin. Complete the fields engines actually read.",
 "faq":"Add FAQPage or HowTo schema around the real question-and-answer or step content already on the page. It hands engines pre-structured Q&A that maps onto how people ask, raising the odds your answer is the one lifted.",
 "freshness":"Show a visible 'Last updated' date and refresh the content behind it. Around 90% of AI-bot crawl activity is on content under three years old; undated or stale pages lose to dated ones on the same query.",
 "author":"Add a named author with a visible byline and Person schema (credentials, sameAs). E-E-A-T attribution is a trust signal engines weight when deciding whose page to name; an anonymous page is easier to leave uncited.",
 "internal":"Add at least three in-content links from related pages into this one, with descriptive anchors. Internal links build the topical cluster engines read as authority and are how they discover the page within your site.",
 "definitional":"Add a one-sentence 'X is a ...' definition of the core term in the opening. It gives engines a clean, quotable definition for 'what is X' queries and anchors the page's entity.",
 "wordcount":"Bring thin pages to at least ~300 substantive words that add information, not filler. Below that, engines treat the page as too thin to cite; depth (specifics, data, examples) earns the citation, not length alone.",
 "readability":"Simplify toward a grade-16 reading level or below: shorter sentences, plainer words, one idea per sentence. Clearer prose is easier for engines to parse and lift cleanly as an answer.",
}
# per-engine weights (which signals each engine actually weights). site ids allowed.
# Render-parity CORRECTED 2026-07-19 after deep research (book/research/schema-render-parity-...):
# GPTBot / PerplexityBot / ClaudeBot do NOT execute JavaScript (Vercel/MERJ, 500M+ fetches), so
# JS-injected schema is invisible to ChatGPT / Perplexity / Claude - parity is a hard visibility
# gate there (weight 3). Bing/Copilot renders but unreliably (weight 2). Google renders JS, so
# Gemini + AI Overviews DO eventually see JS-injected schema, so parity is removed for them (it is
# a reliability/speed issue there, not visibility). schema itself still weighted for Gemini.
ENGINE_WEIGHTS = {
 "ChatGPT":     {"wordcount":3,"statdensity":3,"citations":3,"parity":3,"entity":2,"entitydensity":2,"schemacomplete":2,"author":2,"sourced":2,"answerfirst":2,"sections":2,"schema":1,"definitional":1,"readability":1,"freshness":1,"qheadings":1,"noindex":2,"speed":2,"schemavalidity":1,"answerthird":1,"h2answer":1,"answerlead":2,"readwindow":2},
 "Perplexity":  {"freshness":3,"citations":3,"parity":3,"sourced":2,"entity":2,"entitydensity":2,"statdensity":2,"answerfirst":2,"liststables":2,"sections":2,"reachability":2,"definitional":1,"qheadings":1,"author":1,"comparison":1,"video":1,"noindex":2,"speed":2,"answerthird":1,"h2answer":1,"rankedlist":1,"orphans":1,"answerlead":2,"readwindow":2},
 "AI Overviews":{"answerfirst":3,"qheadings":3,"definitional":2,"sections":2,"schema":2,"faq":2,"liststables":2,"entity":2,"entitydensity":1,"readability":1,"schemacomplete":2,"video":2,"freshness":1,"canonical":1,"sitemap":1,"title":1,"meta":1,"comparison":1,"author":1,"noindex":3,"speed":1,"answerthird":2,"h2answer":1,"rankedlist":1,"schemavalidity":1,"duplicate":1,"orphans":1,"brokenlinks":1,"nearduplicate":1,"reviewschema":1,"answerlead":1},
 "Gemini":      {"schema":3,"entity":3,"entitydensity":2,"schemacomplete":2,"statdensity":2,"answerfirst":2,"canonical":1,"sitemap":1,"faq":1,"citations":1,"author":1,"noindex":3,"schemavalidity":2,"duplicate":1,"nearduplicate":1,"reviewschema":1},
 "Copilot":     {"schema":3,"sitemap":2,"reachability":2,"liststables":2,"statdensity":2,"answerfirst":2,"parity":2,"entity":2,"entitydensity":1,"schemacomplete":2,"faq":2,"freshness":1,"sourced":1,"comparison":1,"video":1,"noindex":3,"speed":1,"schemavalidity":1,"rankedlist":1,"orphans":1,"duplicate":1,"brokenlinks":1,"nearduplicate":1,"reviewschema":1,"readwindow":1},
 "Claude":      {"sections":3,"parity":3,"answerfirst":2,"definitional":1,"readability":1,"statdensity":2,"qheadings":2,"liststables":2,"entity":2,"entitydensity":1,"citations":1,"author":1,"sourced":1,"noindex":2,"h2answer":2,"answerthird":1,"answerlead":2,"readwindow":2},
}
for _e,_w in (("ChatGPT",2),("AI Overviews",1),("Perplexity",1),("Copilot",1)): ENGINE_WEIGHTS[_e]["snippetlead"]=_w
ENGINE_NOTE = {
 "ChatGPT":"Favours comprehensive, authoritative, source-cited content + strong entity grounding. Cites few sources per answer, so be THE definitive page.",
 "Perplexity":"Live-searches every query. Rewards freshness, extractable facts and external citations. Cites many sources, so breadth helps.",
 "AI Overviews":"Rank-coupled + query fan-out. Answer-first, question headings, schema and self-contained sections win.",
 "Gemini":"Google index + Knowledge Graph. Entity/schema clarity, sameAs and canonical carry visibility across.",
 "Copilot":"Bing-grounded and highly citation-friendly. Schema, sitemap/IndexNow, listicles and extractable facts win here.",
 "Claude":"Synthesises rather than quotes. Rewards clean logical chunking, factual density and clear structure.",
}
# The 10 AI fan-out types (Moz 50,000-prompt study, 2026): the kinds of sub-query an engine generates when it
# fans a topic out. Entity + Comparison drove ~97% of ALL brand mentions in that study, so they lead. Each type
# maps to the on-page checks that decide whether a site wins that sub-query (a heuristic overlay on the checks).
FANOUT_CHECKS = {
 "Entity":      ("entity", "schemacomplete", "schema", "canonical"),
 "Comparison":  ("comparison", "rankedlist", "liststables"),
 "Semantic":    ("definitional", "answerfirst", "answerlead", "sections"),
 "Factual":     ("statdensity", "sourced", "citations"),
 "Attribute":   ("schemacomplete", "wordcount", "liststables"),
 "Follow-up":   ("qheadings", "h2answer", "faq"),
 "Anticipate":  ("faq", "answerthird", "qheadings"),
 "Perspective": ("author", "video", "citations"),
 "Transact":    ("reviewschema", "schema", "faq"),
 "Tutorial":    ("liststables", "sections", "faq"),
}
FANOUT_KEY = ("Entity", "Comparison")   # the two fan-out types that drove ~97% of brand mentions
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

# Real-browser request headers. A User-Agent-only request is trivially fingerprinted as a bot, so many WAFs 403 it;
# a fuller header set clears header-based bot rules. It cannot spoof TLS/JA3, so a hard Cloudflare bot-fight site
# still blocks urllib - those pages fall back to the render path in process(). Applied ONLY to the default crawl UA,
# never to the bot-UA reachability probes (they must stay minimal so the WAF reading reflects the real bot).
_BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache", "Pragma": "no-cache",
}

# ------------------------------------------------------------------ authenticated crawl (desktop only)
# Credentials for a private/staging crawl are attached to the request ONLY when the URL is the audited host
# or a subdomain of it, so a redirect or external link can never carry them to a parent, a sibling, or a third
# party. run_audit sets _CRAWL_AUTH for the duration of a crawl; default None = today's behaviour (worker-safe).
_CRAWL_AUTH = None   # {"host","basic","cookie","headers"} or None

def _host_ok(target_host, url):
    try:
        h = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    t = (target_host or "").lower()
    # exact host or a subdomain of the audited host ONLY; never a parent or a sibling (no credential over-send)
    return bool(t) and (h == t or h.endswith("." + t))

def _auth_headers(auth, url):
    """Credentials for the target host only. Returns {} for any off-host URL so a redirect cannot leak them."""
    if not auth or not _host_ok(auth.get("host"), url):
        return {}
    h = {}
    if auth.get("basic"):
        h["Authorization"] = "Basic " + base64.b64encode(str(auth["basic"]).encode("utf-8")).decode("ascii")
    if auth.get("cookie"):
        h["Cookie"] = str(auth["cookie"])
    for k, v in (auth.get("headers") or {}).items():
        h[str(k)] = str(v)
    return h

class _AuthSafeRedirect(urllib.request.HTTPRedirectHandler):
    """Credentials must not follow a redirect to a host they were not gated to. Strips Authorization / Cookie /
    the extra auth header names from the redirected request whenever the new URL is NOT the audited host or a
    subdomain of it. Also honours the SSRF guard when it is on, so the authed opener is safe on both axes."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _ssrf_on() and not ssrf_ok(newurl):
            raise urllib.error.HTTPError(newurl, code, "blocked (redirect to non-public host)", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            auth = _CRAWL_AUTH
            if not (auth and _host_ok(auth.get("host"), newurl)):
                strip = {"authorization", "cookie"} | {str(k).lower() for k in (auth or {}).get("headers", {})}
                for k in [h for h in list(new.headers) if h.lower() in strip]:
                    del new.headers[k]
        return new

_AUTH_OPENER = urllib.request.build_opener(_AuthSafeRedirect())

def _decompress(raw, headers):
    ce = (headers.get("Content-Encoding") or "").lower()
    if "gzip" in ce:
        try: return gzip.decompress(raw)
        except Exception: return raw
    if "deflate" in ce:
        try: return zlib.decompress(raw)
        except Exception:
            try: return zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception: return raw
    return raw

def fetch_raw(url, ua=UA, timeout=25, capture=None, _retry=True, auth=None):
    t0 = time.time()
    if not ssrf_ok(url):
        return None, {}, "", int((time.time()-t0)*1000)
    try:
        eff_auth = auth if auth is not None else _CRAWL_AUTH
        hdrs = {"User-Agent": ua}
        if ua == UA:                                                   # browser headers + credentials only for the crawl UA
            hdrs.update(_BROWSER_HEADERS)
            hdrs.update(_auth_headers(eff_auth, url))                  # host-gated credentials (desktop)
        req = urllib.request.Request(url, headers=hdrs)
        if eff_auth and ua == UA:
            _open = _AUTH_OPENER.open                                  # strips credentials on any off-host redirect (C1)
        elif _ssrf_on():
            _open = _GUARD_OPENER.open
        else:
            _open = urllib.request.urlopen
        with _open(req, timeout=timeout) as r:
            data = _decompress(r.read(), r.headers); enc = r.headers.get_content_charset() or "utf-8"
            if capture is not None: capture["final_url"] = r.geturl()   # after redirects
            return r.status, dict(r.headers), data.decode(enc, "ignore"), int((time.time()-t0)*1000)
    except urllib.error.HTTPError as e:
        if _retry and e.code in (429, 503):                             # transient rate-limit / overload -> back off (honour Retry-After), retry once
            wait = 2.0
            try:
                ra = (e.headers or {}).get("Retry-After")
                if ra: wait = min(float(ra), 10.0)
            except Exception: pass
            time.sleep(wait); return fetch_raw(url, ua, timeout, capture, _retry=False, auth=auth)
        return e.code, dict(e.headers or {}), "", int((time.time()-t0)*1000)
    except Exception:
        return None, {}, "", int((time.time()-t0)*1000)

def render(url, timeout=35):
    if not CHROME: return None, 0
    if not ssrf_ok(url): return None, 0
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

def first_answer_offset(html):
    """Char offset (in the linearised full body text, document order) of the first >=40-word CONTENT
    paragraph, plus the total linearised length. Returns (-1, len) if there is no such paragraph. Models
    what a NO-JS reader reaches scanning the page top-down (ChatGPT Deep Research reads linearised HTML,
    no JavaScript, no clicks, ~5,700-char window - a buried answer never enters that window)."""
    if not html:
        return -1, 0
    soup = BeautifulSoup(html, "lxml")
    body = soup.body or soup
    full = body.get_text(" ", strip=True)
    root = main_content(BeautifulSoup(html, "lxml"))
    for p in root.find_all("p"):
        t = p.get_text(" ", strip=True)
        if len(words(t)) >= 40:
            return full.find(t[:60]), len(full)
    return -1, len(full)

def section_counts(root):
    """Words of PROSE between consecutive H2/H3 headings, in DOCUMENT order. NOT sibling order:
    CMS/Webflow wrap each heading+block in its own <div>, so the next heading is not a sibling of the
    previous one - the old sibling-walk never saw it and merged the whole page into one giant 'section',
    inflating the wall count. Walking descendants attributes every text node to the most recent heading
    regardless of nesting. Structured content (tables, lists, code, figures) is not a wall of prose, so
    it is excluded; a heading's own text is excluded too."""
    heads = root.find_all(["h2", "h3"])
    if not heads:
        return []
    hset = {id(h) for h in heads}
    _SKIP = ("h2", "h3", "table", "pre", "code", "li", "figure")
    counts = []
    cur = -1
    for node in root.descendants:
        if id(node) in hset:                    # a heading starts a new section
            counts.append(0); cur = len(counts) - 1; continue
        if cur < 0:                             # text before the first heading = intro, not a section
            continue
        if isinstance(node, str):
            t = node.strip()
            if not t:
                continue
            if node.find_parent(_SKIP):         # heading text / list / table / code = not prose
                continue
            counts[cur] += len(words(t))
    return counts

# ---- answer-lead: does each H2/H3 section OPEN with a self-contained direct answer? (Peec reranker 2026:
# "one direct-answer sentence in the first two lines of every section" = the highest-leverage on-page edit;
# plus passage self-containment - no orphan pronoun across a heading boundary, since chunkers split there).
_ANS_ORPHAN = re.compile(r"^(it|this|that|these|those|they|there|here|so|now|but|and|also|however|"
                         r"additionally|furthermore|moreover|in addition|in other words|for example|"
                         r"for instance|plus|yet|still)\b", re.I)
_ANS_DEFN = re.compile(r"^\W{0,3}[A-Z0-9][\w&/.\-' ]{0,50}?\s+(is|are|means|refers to|=|:)\s", re.I)
def section_leads(root):
    """First substantive block (<p>/<li>, >=5 words) after each H2/H3, in document order. Returns one entry
    per section (None if the section has no prose lead - e.g. a heading straight into a sub-heading)."""
    heads = root.find_all(["h2", "h3"])
    if not heads:
        return []
    hset = {id(h) for h in heads}
    leads = []; cur = -1; done = set()
    for node in root.descendants:
        if id(node) in hset:
            leads.append(None); cur = len(leads) - 1; continue
        if cur < 0 or cur in done:
            continue
        if getattr(node, "name", None) in ("p", "li"):
            txt = node.get_text(" ", strip=True)
            if len(words(txt)) >= 5:
                leads[cur] = txt; done.add(cur)
    return leads
def answer_lead_pct(root):
    """% of prose-led sections whose FIRST sentence is a self-contained answer (not an orphan-pronoun
    opener, not a question) - either substantive (>=6 words) or a concise definitional 'X is/=' lead."""
    leads = [l for l in section_leads(root) if l]
    if not leads:
        return None, 0, 0
    good = 0
    for lead in leads:
        first = re.split(r"(?<=[.!?])\s", lead)[0].strip()
        if (not _ANS_ORPHAN.match(lead.strip()) and not first.endswith("?")
                and (len(words(first)) >= 6 or bool(_ANS_DEFN.match(first)))):
            good += 1
    return round(100 * good / len(leads)), good, len(leads)

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

ARCHIVE_RE = re.compile(
    r"(?:^|/)tags?(?:/|$)"                                # /tag/x, /tags/x  (blog taxonomy; ecom uses collections/category)
    r"|(?:^|/)authors?(?:/|$)"                            # /author/x
    r"|/blog/(?:categor(?:y|ies)|tags?|authors?)(?:/|$)"  # taxonomy under /blog
    r"|/page/\d+(?:/|$)"                                 # pagination /page/2
    r"|/\d{4}/\d{2}(?:/|$)"                             # date archives /2024/05/
    r"|/(?:feed|rss)(?:/|$)", re.I)

# comparison / best-of page detector (path + title). Module-level so analyze() and build() share one pattern.
CMP_RE = re.compile(r"(vs\.?|versus|comparison|compare|best[- ]|top[- ]?\d+|alternativ|which[- ])", re.I)

def classify(path, types):
    if path in ("", "/"): return "home"
    segs = [s for s in path.strip("/").split("/") if s]
    if ARCHIVE_RE.search(path): return "archive"       # tag/category/author/date/pagination archives: index pages, not citation targets
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
    "home":     {"answerfirst","answerlead","qheadings","sections","faq","freshness","schema","schemacomplete","author","sourced","video","readability","entitydensity","definitional","answerthird","h2answer","reviewschema"},
    "listing":  {"answerfirst","answerlead","qheadings","sections","faq","freshness","schema","statdensity","citations","wordcount","schemacomplete","author","sourced","video","entity","readability","entitydensity","definitional","answerthird","h2answer","reviewschema"},
    "article":  {"reviewschema"},
    # person / bio page: it IS the E-E-A-T source, not article or Q&A content. It owes entity schema, title,
    # links and images; it does not owe an author byline, a definitional opener, answer-first, Q-headings,
    # sourced stats, FAQ, reviews or a freshness date.
    "profile":  {"answerfirst","answerlead","answerthird","qheadings","h2answer","sections","definitional","sourced","statdensity","faq","reviewschema","freshness","snippetlead","schemacomplete","video"},
    # e-commerce: a product/category page is not article/Q&A content, so the article-shaped checks do not apply
    # (a product page has no human author and no "X is a..." opener; it owes schema, entity, reviews, freshness).
    "product":  {"author","definitional","answerfirst","answerlead","answerthird","qheadings","h2answer","sourced","citations"},
    "category": {"author","definitional","answerfirst","answerlead","answerthird","qheadings","h2answer","sourced","citations","wordcount","statdensity"},
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

# ---- block-level citability: score each passage for how quotable it is (uniqueness finalized at rollup) ----
_BLK_REF=re.compile(r"^\W{0,3}(this|that|these|those|it|they|he|she|here|there|also|however|therefore|thus|moreover|furthermore|as (mentioned|noted|above|described)|in addition|for (example|instance)|as a result|and |but |so )",re.I)
_BLK_DEFN=re.compile(r"^\W{0,3}[A-Z][\w&/.\-' ]{1,60}?\s+(is|are|means|refers to)\b")
_BLK_NUM=re.compile(r"(?<![\w-])(\d[\d,\.]*\s?(%|percent|x|k|m|bn|billion|million|thousand)?|\$\d[\d,\.]*)",re.I)
_BLK_SRC=re.compile(r"\b(according to|source|study|survey|research|report|20\d\d|\bet al\b|per )\b",re.I)
_BLK_QH=re.compile(r"^(how|what|why|when|where|which|who|is|are|can|should|does|do|will)\b",re.I)
def blk_shingles(text,k=4):
    w=[x.lower() for x in words(text)]
    return set(tuple(w[i:i+k]) for i in range(max(0,len(w)-k+1))) if len(w)>=k else set()
def split_blocks(root):
    """Segment a main-content root into citability blocks: a heading (h2-h4) + the content under it
    (to the next heading), plus a lead block for content before the first heading. Trivial prose
    blocks (<12 words with no list) are dropped."""
    if root is None: return []
    heads=root.find_all(["h2","h3","h4"]); out=[]
    def after(h):
        parts=[]; hl=False
        for sib in h.find_next_siblings():
            nm=getattr(sib,"name",None)
            if nm in ("h1","h2","h3","h4"): break
            if nm in ("ul","ol","table"): hl=True
            t=sib.get_text(" ",strip=True) if hasattr(sib,"get_text") else ""
            if t: parts.append(t)
        return " ".join(parts),hl
    if heads:
        lp=[]
        for sib in heads[0].find_previous_siblings():
            if hasattr(sib,"get_text"):
                t=sib.get_text(" ",strip=True)
                if t: lp.append(t)
        lp.reverse(); lead=" ".join(lp)
        if len(words(lead))>=12: out.append({"heading":"","text":lead,"has_list":False})
    else:
        lead=root.get_text(" ",strip=True)
        if len(words(lead))>=12: out.append({"heading":"","text":lead,"has_list":bool(root.find(["ul","ol","table"]))})
    for h in heads:
        ht=h.get_text(" ",strip=True); bt,hl=after(h)
        if len(words(ht+" "+bt))>=12 or hl: out.append({"heading":ht,"text":bt,"has_list":hl})
    return out
def blk_base(b):
    """The four per-block signals that need no cross-page context (uniqueness is added in the rollup).
    Keeps the block's shingles (in-process set) for the rollup's site-wide dedup."""
    text=b["text"] or ""; nwords=len(words(text))
    first=re.split(r"(?<=[.!?])\s",text.strip())[0] if text.strip() else ""
    lead=" ".join(words(text)[:60]); lw=len(words(lead))
    qh=bool(b["heading"]) and (b["heading"].strip().endswith("?") or bool(_BLK_QH.match(b["heading"].strip())))
    answer=0
    if _BLK_DEFN.match(text): answer+=60
    if qh: answer+=25
    if 8<=len(words(first))<=45: answer+=15
    answer=min(100,answer)
    selfc=100
    if _BLK_REF.match(text): selfc-=55
    if nwords<25: selfc-=25
    if b["heading"]: selfc=min(100,selfc+10)
    selfc=max(0,selfc)
    structure=0
    if b["heading"]: structure+=45
    if b["has_list"]: structure+=35
    if 40<=lw<=120 or (b["has_list"] and nwords>=20): structure+=20
    if not b["heading"] and not b["has_list"] and nwords>=40: structure=max(structure,40)
    structure=min(100,structure)
    stats=min(100,len(_BLK_NUM.findall(text))*30+(25 if _BLK_SRC.search(text) else 0))
    return {"heading":(b["heading"] or "")[:80],"snippet":(text[:150]+("…" if len(text)>150 else "")),
            "answer":answer,"selfcontained":selfc,"structure":structure,"stats":stats,"_sh":blk_shingles(text)}
def finalize_blocks(pages):
    """Rollup: compute each block's uniqueness (shingles that appear in ONLY that block, site-wide),
    the final weighted score (self-contained 28 / answer 27 / stats 17 / structure 14 / uniqueness 14),
    keep top 3 + weakest 2 per page, and return the site-wide {top,weakest,avg}. Mutates pages:
    sets JSON-safe p['blocks'] and drops the transient p['_blocks']."""
    from collections import Counter
    allb=[(p,bb) for p in pages for bb in (p.get("_blocks") or [])]
    shcount=Counter()
    for _,bb in allb:
        for x in bb["_sh"]: shcount[x]+=1
    per={}; site=[]
    for p,bb in allb:
        sh=bb["_sh"]
        dup=sum(1 for x in sh if shcount[x]>1)
        uniq=round(100*(1-dup/len(sh))) if sh else 100
        score=round(0.27*bb["answer"]+0.28*bb["selfcontained"]+0.14*bb["structure"]+0.17*bb["stats"]+0.14*uniq)
        rec={"heading":bb["heading"],"snippet":bb["snippet"],"score":score,
             "sub":{"answer":bb["answer"],"selfcontained":bb["selfcontained"],"structure":bb["structure"],"stats":bb["stats"],"uniqueness":uniq},
             "path":(p.get("path") or p.get("url") or "")}
        per.setdefault(id(p),[]).append(rec); site.append(rec)
    for p in pages:
        recs=sorted(per.get(id(p),[]),key=lambda r:-r["score"])
        keep=recs[:3]+[r for r in recs[-2:] if r not in recs[:3]]
        p["blocks"]=keep; p.pop("_blocks",None)
    site.sort(key=lambda r:-r["score"])
    avg=round(sum(r["score"] for r in site)/len(site)) if site else 0
    return {"top":site[:5],"weakest":sorted(site,key=lambda r:r["score"])[:5],"avg":avg,"n":len(site)}

# --- Content-citability TEXT signals (SIGIR-2026 "What Gets Cited" + AirOps 2026: what an LLM prefers when
# choosing between two candidate passages). ADVISORY: surfaced in the report, NOT added to the weighted score.
_HEDGE_RE = re.compile(r"\b(may|might|could|possibly|perhaps|arguably|seems?|appears?|we believe|generally|typically|in some cases|often|sometimes|relatively|fairly|somewhat|likely|probably|tends? to|more or less|up to)\b", re.I)
_CLAIM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?\s?%|\b(?:#\s?1|no\.?\s?1|number one)\b|\b(?:best|leading|fastest|largest|smallest|cheapest|top|most|first|only|award-winning|world-class|industry-leading|unrivalled|unmatched|guaranteed)\b)", re.I)
# Superlative / ranking BRAGS specifically - the claims that read as unbacked marketing unless evidenced. A bare
# statistic is often the evidence itself, so it is NOT counted "naked"; only an unsupported superlative is.
_SUPER_RE = re.compile(r"\b(best|leading|fastest|largest|smallest|cheapest|#\s?1|no\.?\s?1|number one|top-rated|award-winning|world-class|industry-leading|unrivalled|unmatched|market-leading|the only)\b", re.I)
_ATTRIB_RE = re.compile(r"(according to|\bsource[sd]?\b|\bstud(y|ies)\b|\breports?\b|\bresearch\b|\bsurvey\b|\bper\b|\(20\d\d\)|\[\d+\])", re.I)
_GENERIC_H_RE = re.compile(r"^(introduction|overview|conclusion|summary|about( us)?|features|benefits|faqs?|frequently asked questions|more info(rmation)?|learn more|details|our services|services|welcome|get started|how it works|why choose us|the basics|final thoughts)$", re.I)
_ANAPHORA_RE = re.compile(r"^(this|it|they|these|those|that|he|she|there|as (mentioned|noted|described|discussed|above|shown)|such|here)\b", re.I)
_PROPER_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")

_CJK_RE = re.compile(r"[぀-ヿ㐀-鿿가-힯]")   # Hiragana / Katakana / CJK / Hangul
_INDIC_RE = re.compile(r"[ऀ-ൿ]")                          # Devanagari .. Malayalam
def _dominant_cpt(text):
    """Approx characters-per-token for the text's dominant script (lower CPT = more tokens for the same length).
    Latin ~4, CJK ~1.2, Indic ~1.0 - so a chunk sized fine in English can blow a retrieval token budget in JP/KR/ZH/Indic."""
    t=text or ""; n=len(t) or 1
    if len(_CJK_RE.findall(t))/n>0.2: return 1.2
    if len(_INDIC_RE.findall(t))/n>0.2: return 1.0
    return 4.0

def page_assets(root, img_count):
    """Structural 'more than prose an AI can regenerate' signal (deterministic proxy for originality; Shepard-400:
    AI-search winners host proprietary assets 92.9% vs 57.1%). A data table with real rows, a downloadable dataset,
    a chart/canvas, a compute form (range/number input), an embedded video, or a real image gallery."""
    tables=sum(1 for t in root.find_all("table") if len(t.find_all("tr"))>=3)
    dl=sum(1 for a in root.find_all("a",href=True) if re.search(r"\.(csv|xlsx?|json|zip)(\?|$)", a.get("href",""), re.I))
    charts=len(root.find_all("canvas"))+len(root.select('[class*="chart" i],[id*="chart" i]'))
    cform=bool(root.select('input[type="range"],input[type="number"]'))
    vid=bool(root.find_all("video")) or bool([i for i in root.find_all("iframe",src=True) if re.search(r"(youtube|vimeo|wistia|loom)", i.get("src",""), re.I)])
    return bool(tables or dl or charts or cform or vid or (img_count or 0)>=8)

# Decision-completeness via a LOCAL ollama model (no key, no cost, runs on the worker's own machine). The three
# hard slots are deterministic elsewhere; ollama reads the whole page and judges all six holistically. Optional:
# if ollama is not reachable the check is simply skipped (desktop/CLI users without it lose nothing).
_OLLAMA_URL = "http://localhost:11434"; _OLLAMA_OK = None
_DFACT_KEYS = ("what_is","who_for","cost","what_isnt","next_step","why_better")
def _ollama_available():
    global _OLLAMA_OK
    if _OLLAMA_OK is not None: return _OLLAMA_OK
    try:
        import urllib.request
        with urllib.request.urlopen(_OLLAMA_URL+"/api/tags", timeout=3) as r:
            _OLLAMA_OK = (getattr(r,"status",200)==200)
    except Exception:
        _OLLAMA_OK = False
    return _OLLAMA_OK

def decision_facts(text, model="llama3"):
    """Does this money page COMMIT the 6 buyer-decision facts an AI needs to recommend it? Judged by a local
    ollama model. Returns {key:bool} for the 6 keys, or None if ollama is unavailable / fails / returns junk."""
    if not text or not _ollama_available(): return None
    prompt=("You are auditing a product or service web page. For each fact answer strictly true or false: does the "
            "page CLEARLY state it? Return ONLY JSON with lowercase keys what_is, who_for, cost, what_isnt, next_step, why_better.\n"
            "what_is = what the product/service actually is; who_for = who it is for; cost = the price or pricing model; "
            "what_isnt = who it is NOT for or its limits; next_step = the next action (buy/book/contact/trial); "
            "why_better = why it beats alternatives.\n\nPAGE:\n"+text[:6000])
    try:
        import urllib.request
        body=json.dumps({"model":model,"prompt":prompt,"format":"json","stream":False,
                         "options":{"temperature":0,"num_predict":150}}).encode()
        req=urllib.request.Request(_OLLAMA_URL+"/api/generate",data=body,headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=60) as resp:
            out=json.loads(resp.read().decode())
        parsed=json.loads(out.get("response") or "{}") or {}
        low={str(k).lower():v for k,v in parsed.items()}
        return {k:bool(low.get(k)) for k in _DFACT_KEYS}
    except Exception:
        return None

_PRICE_RE = re.compile(r"[$£€]\s?\d|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|GBP|EUR|AUD|CAD)\b|\b\d[\d,]*\s?(?:per month|per year|/mo\b|/yr\b|a month)", re.I)
_NEGQUAL_RE = re.compile(r"\b(not (a )?(good )?(fit|suitable|right|ideal|for you)|isn'?t for|is not for|who (this|it) (is|isn'?t) (not )?for|not recommended for|not designed for|might not be|may not be right|if you'?re looking for .{0,40}, (this|we|our).{0,20}(not|isn'?t))\b", re.I)

def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text or "") if len(s.strip())>=20]

def content_signals(body, heading_texts, passages):
    """Per-page text-quality citability signals (advisory, plain text, no engine). Hedging near claims (SIGIR:
    confident >> hedged), naked claims (evidence-backed cited far more), generic headings (AirOps: heading match
    is the strongest on-page lever), unresolved-anaphora passages (AI Mode retrieves passage-level), and a rough
    over-breadth proxy (AirOps: mixed/over-broad pages cite worst)."""
    sents=_sentences(body)
    claims=[s for s in sents if _CLAIM_RE.search(s)]
    hedged=[s for s in claims if _HEDGE_RE.search(s)]
    naked=[s for s in sents if _SUPER_RE.search(s) and not _ATTRIB_RE.search(s)]
    generic=[h for h in heading_texts if _GENERIC_H_RE.match((h or "").strip().lower())]
    unresolved=0; checked=0
    for p in (passages or []):
        t=(p or "").strip()
        if len(words(t))<12: continue
        checked+=1
        if _ANAPHORA_RE.match(t) and not _PROPER_RE.search(" ".join(t.split()[:10])): unresolved+=1
    return {"hedge_ratio": round(len(hedged)/len(claims),3) if claims else 0.0,
            "hedged_claims": len(hedged), "claims": len(claims), "naked_claims": len(naked),
            "generic_headings": len(generic), "unresolved_passages": unresolved,
            "passages_checked": checked, "subtopics": len([h for h in heading_texts if (h or "").strip()])}

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]|$)")
_VAGUE_VAL_RE = re.compile(r"\b(affordable|cheap|budget-friendly|best|leading|top|premium|high[- ]quality|world[- ]class|cutting[- ]edge|state[- ]of[- ]the[- ]art|fast|quick|various|many|several|competitive|great|excellent|a range of|wide range|up to|starting (at|from)|and more)\b", re.I)

def schema_quality(jobjs, page_text=""):
    """Grade the schema VALUES, not presence (2026 controlled evidence: schema PRESENCE is inert for citation;
    what an AI can weigh is whether the declared facts are MACHINE-VERIFIABLE vs prose-vague). Advisory. Flags a
    present-but-ambiguous fact so it reads as a fix, not a verdict: a price without priceCurrency or non-numeric,
    a date not in ISO-8601, or a vague-worded name/description/offer value ("affordable", "leading", "up to").
    Also flags schema-to-page CONSISTENCY: a declared price or rating that appears NOWHERE on the visible page,
    i.e. structured data that is stale or contradicts what the buyer (and the engine) actually reads."""
    vague=[]
    _pc=re.sub(r"[,\s]","",(page_text or ""))                        # compacted page text, for digit-run matching
    _has_price=bool(_PRICE_RE.search(page_text or ""))
    _has_reviewword=bool(re.search(r"\b(reviews?|ratings?|stars?|out of 5|/\s?5|trustpilot|rated)\b",(page_text or "").lower()))
    def _digits(x):
        m=re.search(r"\d[\d,]*(?:\.\d+)?",str(x)); return m.group(0).replace(",","") if m else None
    def walk(n):
        if isinstance(n,dict):
            offers=n.get("offers")
            for off in ([offers] if isinstance(offers,dict) else (offers if isinstance(offers,list) else [])):
                if isinstance(off,dict):
                    pr=off.get("price")
                    if pr is not None and not off.get("priceCurrency"):
                        vague.append({"field":"price","value":str(pr)[:40],"issue":"no priceCurrency"})
                    if pr is not None and not re.match(r"^[\d.,]+$",str(pr).strip()):
                        vague.append({"field":"price","value":str(pr)[:40],"issue":"price not a plain number"})
                    # CONSISTENCY: a numeric schema price that appears nowhere in the visible page text (stale/mismatched)
                    if pr is not None and page_text and _has_price and re.match(r"^[\d.,]+$",str(pr).strip()):
                        _pn=_digits(pr)
                        if _pn and _pn not in _pc:
                            vague.append({"field":"price","value":str(pr)[:40],"issue":"not shown on the page - stale or mismatched schema"})
            # CONSISTENCY: aggregateRating declared but no visible rating / review anywhere on the page
            _ar=n.get("aggregateRating")
            if isinstance(_ar,dict) and _ar.get("ratingValue") is not None and page_text and not _has_reviewword:
                _rn=_digits(_ar.get("ratingValue"))
                if _rn and _rn not in _pc:
                    vague.append({"field":"aggregateRating","value":str(_ar.get("ratingValue"))[:40],"issue":"rating in schema, not shown on the page"})
            for df in ("datePublished","dateModified"):
                dv=n.get(df)
                if isinstance(dv,str) and dv and not _ISO_DATE_RE.match(dv):
                    vague.append({"field":df,"value":dv[:40],"issue":"not ISO-8601"})
            # a fact stated as a vague RANGE rather than a machine-readable value (e.g. price "up to 50%",
            # a QuantitativeValue with no unitText). Marketing adjectives in name/description are NOT flagged -
            # they are legitimate brand copy, not a fact-verifiability problem.
            for ff in ("value","minValue","maxValue"):
                fv=n.get(ff)
                if isinstance(fv,str) and _VAGUE_VAL_RE.search(fv):
                    vague.append({"field":ff,"value":fv[:50],"issue":"not a machine-readable value"})
            if n.get("@type")=="QuantitativeValue" and n.get("value") is not None and not n.get("unitText") and not n.get("unitCode"):
                vague.append({"field":"QuantitativeValue","value":str(n.get("value"))[:40],"issue":"no unitText/unitCode"})
            for v in n.values(): walk(v)
        elif isinstance(n,list):
            for v in n: walk(v)
    for o in (jobjs or []): walk(o)
    # de-dupe identical findings
    seen=set(); out=[]
    for f in vague:
        k=(f["field"],f["value"],f["issue"])
        if k in seen: continue
        seen.add(k); out.append(f)
    return {"vague":out[:25],"n_vague":len(out)}

_FOLLOWUP = {
    "Compare":  re.compile(r"\b(vs\.?|versus|alternatives?|compare|comparison|best .{0,30}(for|tools?|apps?|software|agenc)|top \d+)\b", re.I),
    "Validate": re.compile(r"\b(case[- ]stud(y|ies)|success stor|results|testimonial|reviews?|proof|client stor|portfolio)\b", re.I),
    "Constrain":re.compile(r"\b(how to choose|buyer'?s? guide|which .{0,30}(right|best|for you)|checklist|choosing|pricing guide|use cases?)\b", re.I),
    "Act":      re.compile(r"\b(calculator|estimate|get a quote|pricing|plans|book (a )?(demo|call)|get started|contact|quiz|free trial)\b", re.I),
    "Clarify":  re.compile(r"\b(what is|guide to|overview|introduction to|faqs?|glossary|resources|101|learn)\b", re.I),
}
def followup_coverage(pages):
    """SITE-LEVEL: does the site hold the assets a conversational fan-out needs across the 5 follow-up types
    (Clarify / Constrain / Compare / Validate / Act)? Deterministic presence/absence over the crawl (URL + title
    + question-headings), so low-noise. Maps to query fan-out (brands lose the Comparison + Entity fan-outs)."""
    hits={k:[] for k in _FOLLOWUP}
    for p in pages:
        blob=((p.get("url") or "")+" "+(p.get("title") or "")+" "+" ".join(p.get("q_headings") or [])).lower()
        for k,rx in _FOLLOWUP.items():
            if len(hits[k])<5 and rx.search(blob): hits[k].append(p.get("url") or p.get("path"))
    types={k:{"covered":bool(hits[k]),"examples":hits[k][:4]} for k in _FOLLOWUP}
    gaps=[k for k,v in types.items() if not v["covered"]]
    return {"types":types,"gaps":gaps,"covered":len(_FOLLOWUP)-len(gaps),"total":len(_FOLLOWUP)}

# Citation landscapes are NATIONAL, not global (Reddit/Wikipedia dominance is an English-market artefact; JP leans
# note/Ameblo/PR TIMES, KR on Naver, CN on Baidu Baike - non-English sweep 2026-09-20). Curated from that research.
_MARKET_MAP = {
  "jp":{"name":"Japan","platforms":[["Community","note / Ameblo / Hatena"],["PR wire","PR TIMES"],["Reviews","kakaku.com / @cosme"],["Knowledge","Wikipedia (JA)"]]},
  "kr":{"name":"South Korea","platforms":[["Community","Naver Blog / Cafe / Knowledge-iN"],["Blog","Tistory"],["Knowledge","Namuwiki / Wikipedia (KO)"]]},
  "zh":{"name":"China","platforms":[["Knowledge","Baidu Baike"],["Community","Zhihu / Xiaohongshu"],["Official","government / .edu sources"]]},
  "de":{"name":"Germany","platforms":[["Community","Reddit / gutefrage"],["Reviews","Trustpilot / ProvenExpert"],["Knowledge","Wikipedia (DE)"],["Video","YouTube"]]},
  "fr":{"name":"France","platforms":[["Community","Reddit / forums"],["Reviews","Trustpilot / Avis Verifies"],["Knowledge","Wikipedia (FR)"],["Video","YouTube"]]},
  "es":{"name":"the Spanish-language market","platforms":[["Community","Reddit / forums"],["Reviews","Trustpilot"],["Knowledge","Wikipedia (ES)"],["Video","YouTube"]]},
  "default":{"name":"the US / UK / English market","platforms":[["Community","Reddit"],["Knowledge","Wikipedia"],["Reviews","G2 / Trustpilot / Yelp (US)"],["Video","YouTube"],["Developer","GitHub / Stack Overflow (technical niches)"]]},
}
_CCTLD_MARKET={".jp":"jp",".kr":"kr",".cn":"zh",".de":"de",".at":"de",".fr":"fr",".es":"es",".mx":"es",".ar":"es"}
def market_platforms(domain, pages):
    """Infer the site's target MARKET (ccTLD first, then dominant html lang) and return the platforms AI actually
    cites THERE, grouped by source category. Advisory: a market-aware source checklist. Earn authentic presence
    (Google explicitly warns against seeding inauthentic mentions)."""
    mk=None; dl=(domain or "").lower()
    for tld,m in _CCTLD_MARKET.items():
        if dl.endswith(tld): mk=m; break
    if not mk:
        langs={}
        for p in pages:
            l=((p.get("metrics") or {}).get("lang") or "")[:2]
            if l: langs[l]=langs.get(l,0)+1
        top=max(langs,key=langs.get) if langs else "en"
        mk={"ja":"jp","ko":"kr","zh":"zh","de":"de","fr":"fr","es":"es"}.get(top,"default")
    info=_MARKET_MAP.get(mk,_MARKET_MAP["default"])
    return {"market":info["name"],"is_default":(mk=="default"),"platforms":info["platforms"]}

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
    # answer-first opener: the liftable 40-60 word answer at the top. Many CMSes place it in a dedicated
    # answer CAPSULE (aside/div marked answer-capsule / tl;dr / key-answer / doc-abstract) rather than a
    # plain <p> - and main_content() strips <aside>, so look for the capsule in the FULL soup first, then
    # fall back to the first content <p>. The capsule IS the intended opener when it sits above the body.
    fp=None; fptext=""
    _cap=soup.select_one('aside.answer-capsule, div.answer-capsule, [data-bind="answer-capsule"], '
                         '[class*="answer-capsule" i], [class*="tldr" i], [class*="tl-dr" i], '
                         '[class*="key-answer" i], [class*="key-takeaway" i], [role="doc-abstract"], '
                         '[role="doc-tip"], [aria-label*="answer" i]')
    if _cap:
        _ct=_cap.get_text(" ",strip=True); _cn=len(words(_ct))
        if _cn>=12: fp=_cn; fptext=_ct
    if fp is None:
        for p in root.find_all("p"):
            pt=p.get_text(" ",strip=True); n=len(words(pt))
            if n>=12: fp=n; fptext=pt; break
    C.append(chk("answerfirst","good" if fp and 40<=fp<=70 else ("warn" if fp and 30<=fp<=90 else "bad"),f"opening {fp or 0} words"))
    defn=bool(re.match(r"^\W{0,3}[A-Z][\w&/.\-' ]{1,60}?\s+(is|are|means|refers to)\b", fptext))
    C.append(chk("definitional","good" if defn else "warn","definitional opener" if defn else "opener is not a definition"))
    # snippet lead: how many characters of readable content precede the H1? Engines read a short lead
    # window (~300 chars) first. Measured on the chrome-stripped page BODY (NOT just <main>: Webflow-style
    # heroes put the H1 above <main>, which is still a valid lead position), in document order - a
    # deterministic reading. String-matching the H1 text gave an unstable result when the phrase recurred
    # earlier on the page. (H1 pulled 83.6%, Resoneo 2026)
    _sl_root=BeautifulSoup(rendered or raw or "","lxml")
    _sl_root=_sl_root.body or _sl_root
    # strip site nav/footer/aside but NOT <header>: an ARTICLE <header> holds the H1 (content), only the
    # site nav (its own <nav>) is chrome. Removing all <header> used to delete the legitimate post title.
    for _sl_sel in ("script","style","nav","footer","aside","noscript","svg"):
        for _sl_x in _sl_root.find_all(_sl_sel): _sl_x.decompose()
    _sl_h1el=_sl_root.find("h1")
    if not h1s:
        C.append(chk("snippetlead","na","no H1 to anchor the snippet"))
    elif not _sl_h1el:
        C.append(chk("snippetlead","warn","H1 is inside page chrome, not the content"))
    else:
        _sl_pre=0
        for _sl_n in _sl_root.descendants:
            if _sl_n is _sl_h1el: break
            if isinstance(_sl_n,str):
                _sl_txt=_sl_n.strip()
                if _sl_txt: _sl_pre+=len(_sl_txt)+1
        C.append(chk("snippetlead","good" if _sl_pre<=300 else ("warn" if _sl_pre<=600 else "bad"),
                     (f"H1 after {_sl_pre} chars of content" if _sl_pre else "H1 leads the content")))
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
    _qshaped=[t for t in (h.get_text(strip=True) for h in heads) if _qshape(t)]
    qh=len(_qshaped)
    qpct=round(100*qh/nh) if nh else 0
    _qhead=[t for t in _qshaped if t.endswith("?") or QSTART.match(t)][:8]   # question-form headings, for query proposals
    C.append(chk("qheadings","good" if qpct>=30 else ("warn" if qpct>=10 else "bad"),f"{qh}/{nh} ({qpct}%)"))
    # self-contained sections: a WALL is a single prose block too long to lift as one clean passage.
    # Engines chunk by PARAGRAPH, so several short <p> under one heading are already well-chunked - only
    # an oversized single block is a wall. (The old measure summed all prose BETWEEN headings, so a
    # well-paragraphed 600-word section read as one giant wall = big over-report on long-form posts.)
    # Fall back to heading-delimited section spans only when the page has no paragraph markup at all.
    secs=section_counts(root)   # heading-delimited section spans (also used by the h2answer check below)
    _pblocks=[len(words(_pb.get_text(" ",strip=True))) for _pb in root.find_all(["p","blockquote"])
              if not _pb.find_parent(("li","table","pre","code","figure"))]
    _pblocks=[b for b in _pblocks if b>0]
    if _pblocks: _units=_pblocks; _wthr=200; _ulbl="blocks"
    else: _units=secs; _wthr=220; _ulbl="sections"   # no <p> structure: use section spans
    walls=sum(1 for u in _units if u>_wthr)
    if not _units: sec_status="warn"; sec_detail="no readable sections"
    elif walls==0: sec_status="good"; sec_detail=f"{len(_units)} {_ulbl}, none over {_wthr}w"
    elif walls<=max(1,len(_units)//10): sec_status="warn"; sec_detail=f"{walls} wall(s) of text over {_wthr}w"
    else: sec_status="bad"; sec_detail=f"{walls} walls of text ({_ulbl} over {_wthr}w)"
    C.append(chk("sections",sec_status,sec_detail))
    # H2-to-answer pairing: is each heading followed by a liftable 40-180 word answer?
    _live=[s for s in secs if 40<=s<=180]
    if not secs: _hp=("warn","no H2/H3 sections")
    else:
        _hpct=round(100*len(_live)/len(secs))
        _hp=("good" if _hpct>=50 else ("warn" if _hpct>=25 else "bad"),f"{len(_live)}/{len(secs)} liftable answer sections ({_hpct}%)")
    C.append(chk("h2answer",_hp[0],_hp[1]))
    # answer-lead: do sections OPEN with a self-contained direct answer (not an orphan pronoun / slow wind-up)?
    _alp,_alg,_alt=answer_lead_pct(root)
    if _alp is None: C.append(chk("answerlead","na","no H2/H3 sections"))
    else: C.append(chk("answerlead","good" if _alp>=60 else ("warn" if _alp>=30 else "bad"),f"{_alg}/{_alt} sections lead with an answer ({_alp}%)"))
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
    # parity fails only when a CORE citability type (the entity + content schema an AI needs to identify and
    # cite the page) is JS-injected-only - non-JS crawlers (GPTBot, ClaudeBot, PerplexityBot) then cannot read
    # it. Decorative types (Breadcrumb, WebPage/WebSite, Review/AggregateRating, Speakable, ImageObject) being
    # JS-injected do NOT fail parity: the essential schema is still server-rendered and readable.
    _PARITY_CORE={"Organization","Person","LocalBusiness","Corporation","ProfessionalService",
                  "Article","BlogPosting","NewsArticle","TechArticle","Product","Service",
                  "SoftwareApplication","WebApplication","MobileApplication","FAQPage","HowTo",
                  "Recipe","Book","Course","Event","QAPage"}
    inj=sorted(set(types_r)-set(types_raw))
    inj_core=[t for t in inj if t in _PARITY_CORE]
    if inj_core: C.append(chk("parity","bad","core schema JS-injected only: "+", ".join(inj_core)))
    elif inj: C.append(chk("parity","good","core schema in raw HTML ("+", ".join(inj)+" injected via JS)"))
    else: C.append(chk("parity","good","in raw HTML"))
    # read-window: does the core answer sit within the ~5,700-char window a NO-JS reader ingests top-down
    # (ChatGPT Deep Research: linearised HTML, no JS, no clicks)? Measured on RAW (SSR) HTML; a JS-only
    # answer, or one buried past the window, is invisible to non-JS AI readers.
    _rw_off,_rw_len=first_answer_offset(raw)
    if _rw_off>=0:
        C.append(chk("readwindow","good" if _rw_off<=3000 else ("warn" if _rw_off<=5700 else "bad"),
                     f"first answer at char {_rw_off} (~5,700 no-JS window)"))
    elif rendered and first_answer_offset(rendered)[0]>=0:
        C.append(chk("readwindow","bad","answer is JS-injected - invisible to non-JS AI readers"))
    else:
        C.append(chk("readwindow","na","no prose answer block to place"))
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
    _upd=d.isoformat() if d else None
    _age=(datetime.date.today()-d).days if d else None
    if d:
        age=_age
        C.append(chk("freshness","good" if age<=365 else ("warn" if age<=730 else "bad"),f"{d.isoformat()} ({age}d)"))
        # decay-RISK band, SEPARATE from the calibrated freshness scoring above. Tighter thresholds keyed to the
        # AI-citation freshness evidence (76% of ChatGPT top-cited updated <30d; 70% <12mo): fresh<=90 / aging<=365 / stale>365.
        _decay="fresh" if age<=90 else ("aging" if age<=365 else "stale")
    elif ptype=="article":
        C.append(chk("freshness","bad","no date found"))
        _decay="undated"                                  # owes a date but has none: at decay risk, AI cannot tell it is current
    else:
        # evergreen / static page (contact, tool, service, bio): an absent date is by design, not staleness.
        # 'no date' only penalises content that OWES a date (articles). A dated non-article that is old is
        # still flagged above via the age branch.
        C.append(chk("freshness","na","evergreen page - no dated content"))
        _decay="na"                                       # evergreen: exempt from decay risk
    imgs=soup.find_all("img"); alts=sum(1 for i in imgs if (i.get("alt") or "").strip()); apct=round(100*alts/len(imgs)) if imgs else 100
    C.append(chk("alt","good" if apct>=90 else "warn",f"{alts}/{len(imgs)} ({apct}%)"))
    # 'internal' check = editorial in-content links (a content signal): count links inside <main>/<article>.
    il=sum(1 for a in root.find_all("a",href=True) if (a.get("href","").startswith("/") or domain in a.get("href","")))
    C.append(chk("internal","good" if il>=3 else "warn",f"{il} internal links"))
    # crawl link graph (reachability + click-depth): EVERY internal link a crawler actually follows - nav,
    # footer, collection lists, in-body - from the FULL rendered page, not just <main>. Using main-content
    # links alone dropped nav/footer/collection-list edges, inflating click-depth (a 2-click post read as 4+)
    # and marking nav/footer-linked pages as false orphans.
    il_targets=set()
    for a in soup.find_all("a",href=True):
        h=a.get("href","")
        if h.startswith("/") or domain in h:
            try: il_targets.add(urllib.parse.urlparse(urllib.parse.urljoin("https://"+domain,h)).path.rstrip("/") or "/")
            except Exception: pass
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
    # a Person / ProfilePage bio page IS the E-E-A-T source - it does not owe a separate "author". Detect it
    # by a Person node whose name matches the page H1 (so an article's byline Person is NOT mistaken for one).
    _pnames=[(n.get("name") or "").strip() for n in nodes if _t(n,{"Person"}) and n.get("name")]
    _h1l=(h1s[0].get_text(" ",strip=True).lower() if h1s else "")
    _is_profile=("ProfilePage" in tset) or (bool(_h1l) and any(pn and (pn.lower()==_h1l or (len(pn)>6 and pn.lower() in _h1l)) for pn in _pnames))
    byline=bool(soup.select_one('[rel="author"],[class*="author" i],[class*="byline" i]')) or bool(re.search(r"\bby\s+[A-Z][a-z]+\s+[A-Z][a-z]",body[:400]))
    if anames: C.append(chk("author","good","by "+anames[0][:40]))
    elif _is_profile: C.append(chk("author","good","author profile page"))
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
    _agr=None
    for _n in nodes:
        if not isinstance(_n,dict): continue
        _c=_n.get("aggregateRating"); _c=_c if isinstance(_c,dict) else (_n if ("AggregateRating" in (_n.get("@type") if isinstance(_n.get("@type"),list) else [_n.get("@type")])) else None)
        if isinstance(_c,dict) and (_c.get("ratingValue") is not None or _c.get("reviewCount") is not None or _c.get("ratingCount") is not None): _agr=_c; break
    _rv=(_agr or {}).get("ratingValue"); _rc=(_agr or {}).get("reviewCount") or (_agr or {}).get("ratingCount")
    if hasrev and (_rv is not None or _rc is not None):
        _revdet=(f"{_rv}★" if _rv is not None else "rated")+(f" from {_rc} reviews" if _rc is not None else "")
    else: _revdet="Review/AggregateRating present" if hasrev else "no Review/AggregateRating schema"
    C.append(chk("reviewschema","good" if hasrev else "warn",_revdet))
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
    # WebMCP (advisory): does the page DECLARE agent-callable tools in-page (navigator.modelContext / registerTool),
    # the emerging client-side complement to the /.well-known/mcp.json server card (Moz WebMCP how-to)?
    _ag_webmcp=False
    for _s in soup.find_all("script"):
        if "mcp" in (_s.get("type") or "").lower(): _ag_webmcp=True; break
        _sc=_s.string or _s.get_text() or ""
        if _sc and re.search(r"navigator\.modelContext|modelContext\.(?:registerTool|provideContext)|window\.mcp\b|\bregisterTool\s*\(|\bwebmcp\b", _sc, re.I): _ag_webmcp=True; break
    if not _ag_webmcp:
        for _l in soup.find_all("link"):
            _rel=_l.get("rel"); _rel=(" ".join(_rel) if isinstance(_rel,list) else str(_rel or "")).lower()
            if "mcp" in _rel or "modelcontext" in _rel: _ag_webmcp=True; break
    agent={"action":_ag_action,"offer":_ag_offer,"price":_ag_price,"avail":_ag_avail,"prodserv":_ag_prodserv,"contact":_ag_contact,"webmcp":_ag_webmcp}
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
    if _is_profile and ptype in ("page","article"): ptype="profile"   # bio/profile page: exempt the article-shaped checks
    na=NA_BY_TYPE.get(ptype,set())
    for c in C:
        if c["id"] in na: c["status"]="na"
    # self-published comparison page: on the audited (own) domain a 'best X' / 'X vs Y' page that features
    # the site's own brand earns the AI citation but tends NOT to win the recommendation (AI names a rival
    # listed inside it; Ahrefs 2026, 34 lists / 9,886 answers). Flag it so the comparison check nudges
    # toward third-party roundups for the recommendation lift.
    _cmp_brand=(domain.split(".")[0] or "").lower()
    self_comparison=bool(CMP_RE.search(path+" "+title)) and len(_cmp_brand)>=3 and _cmp_brand in body.lower()
    _htexts=[h.get_text(" ",strip=True) for h in root.find_all(["h2","h3","h4","h5","h6"])]
    _ptexts=[p.get_text(" ",strip=True) for p in root.find_all(["p","li"])]
    _csig=content_signals(body, _htexts, _ptexts)
    _sq=schema_quality(jsonld_objs(soup), body)
    _price_raw=bool(_PRICE_RE.search(raw or "")); _price_rend=bool(_PRICE_RE.search(rendered or raw or ""))
    _neg_qual=bool(_NEGQUAL_RE.search(body))
    _hl_raw=len(rawsoup.find_all("link", attrs={"hreflang":True})); _hl_rend=len(soup.find_all("link", attrs={"hreflang":True}))
    _orig=page_assets(root,len(imgs))
    _lang=((soup.html.get("lang") if soup.html else "") or "").strip().lower()
    _cpt=_dominant_cpt(body); _maxtok=0
    for _pt in _ptexts:
        _tt=len(_pt or "")/_cpt
        if _tt>_maxtok: _maxtok=_tt
    metrics={"words":wc,"headings":nh,"question_pct":qpct,"walls":walls,"stat_density":dens,
             "readability":fk,"entity_density":pnd,"definitional":defn,
             "ext_links":ext,"internal_links":il,"schema_types":sorted(tset),"images":len(imgs),"alt_pct":apct,
             "updated":_upd,"age_days":_age,"decay":_decay,
             "hedge_ratio":_csig["hedge_ratio"],"hedged_claims":_csig["hedged_claims"],"claims":_csig["claims"],
             "naked_claims":_csig["naked_claims"],"generic_headings":_csig["generic_headings"],
             "unresolved_passages":_csig["unresolved_passages"],"subtopics":_csig["subtopics"],
             "sch_vague":_sq["n_vague"],"price_raw":_price_raw,"price_rend":_price_rend,"neg_qual":_neg_qual,
             "hreflang_raw":_hl_raw,"hreflang_rend":_hl_rend,
             "orig_asset":_orig,"max_tok":round(_maxtok),"oversized":bool(_maxtok>220),"lang":_lang}
    return {"path":path,"type":ptype,"title":title,"meta":mdc,"checks":C,"metrics":metrics,"rendered":rendered is not None,"sch_vague_items":_sq["vague"],
            "_text":(body[:6000] if (ptype in ("product","service") or (isinstance(agent,dict) and (agent.get("offer") or agent.get("price") or agent.get("action")))) else None),
            "links":sorted(il_targets),"outlinks":sorted(outlinks),"q_headings":_qhead,"simhash":simhash,"chash":chash,"sameas":sameas,"agent":agent,"infogain":infogain,
            "_blocks":[blk_base(_b) for _b in split_blocks(root)],   # per-passage citability (uniqueness finalized in build)
            "self_comparison":self_comparison,"search_forms":find_search_forms(soup, url)}

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
    mode = "keywords" if (qs and max(q["citations"] for q in qs) <= 1) else "citations"
    return {"n_queries":len(qs),"covered":len(covered),"n_gaps":len(gaps),"gaps":gaps[:12],
            "n_orphans":len(orphans),"orphans":orphans[:12],"mode":mode}

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
    # THE IMPERSONATION PROBLEM: we probe from OUR IP, never the bot's real network, so a WAF that VERIFIES
    # bot identity (reverse-DNS / ASN, or the vendors' published crawler-IP allowlists) sees our "GPTBot" as
    # a FAKE and may 403 it while happily serving the REAL bot. So a 403 here is a genuine BLOCK only when
    # robots.txt ALSO disallows the bot (policy + enforcement agree = they meant it). If robots ALLOWS the
    # bot but we still get a persistent 403, it is UNVERIFIABLE -> warn, NEVER a score cap: a check that
    # can't tell "broken" from "correctly-configured" must not cap the score, and the better-run the site,
    # the more likely that false alarm. robots.txt is the discriminator. Two CONTROL probes (a plain browser
    # + an unknown bot, same IP) and the full evidence (UA, status, timestamp) are recorded so a +40 finding
    # is auditable; the server log (analyze_log) is the only authoritative confirmation.
    import datetime as _dtm
    _pstamp=_dtm.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _rgroups=_parse_robots(robots)
    def _rprobe(item):
        nm,ua=item; s,_,_,_=fetch_raw(origin+"/",ua=ua)
        if s==429: time.sleep(1.5); s,_,_,_=fetch_raw(origin+"/",ua=ua)   # 429 = rate limit, often our own burst -> retry once
        return (nm,s)
    with ThreadPoolExecutor(max_workers=3) as _ex:                        # gentle burst so we do not self-trigger the WAF
        reach=list(_ex.map(_rprobe, SERVING_UAS))
    _hardblk=lambda s: s in (401,403) or s is None   # a firewall deny (or timeout)
    _uam=dict(SERVING_UAS); _cleared=[]; _reach2=dict(reach)
    for _b,_s in reach:
        if _hardblk(_s):
            time.sleep(2.0)                                              # spaced single request clears a burst rate-limit
            _s2,_,_,_=fetch_raw(origin+"/",ua=_uam[_b]); _reach2[_b]=_s2
            if not _hardblk(_s2): _cleared.append(_b)
    reach=[(b,_reach2[b]) for b,_ in reach]
    _anyblk=any(_hardblk(s) for _,s in reach)
    # CONTROL probes only when there is a block to interpret: separates a bot-specific block from a whole-IP
    # / rate block, and tells verified-bot handling from a crude UA block.
    _ctrl_br=_ctrl_un=None
    if _anyblk:
        time.sleep(1.5); _ctrl_br=fetch_raw(origin+"/",ua=UA)[0]
        time.sleep(1.5); _ctrl_un=fetch_raw(origin+"/",ua="GoGoChimpProbe/1.0 (+https://gogochimp.com/cited-score; unrecognised bot)")[0]
    # discriminate each persistently-blocked serving bot by robots POLICY
    _real=[b for b,s in reach if _hardblk(s) and _bot_status(_rgroups,b)=="blocked"]
    _unver=[b for b,s in reach if _hardblk(s) and _bot_status(_rgroups,b)!="blocked"]
    rl=[b for b,s in reach if s==429]
    srch=[b for b in _real if b in ("Googlebot","Bingbot")]
    _verdict="bad" if _real else ("warn" if (_unver or rl) else "good")
    _ss=lambda s:(s if s is not None else "timeout")
    _ev=[{"ua":b,"sent":_uam[b],"status":_ss(s),"at":_pstamp} for b,s in reach]
    if _anyblk:
        _ev.append({"ua":"control: plain browser","sent":UA,"status":_ss(_ctrl_br),"at":_pstamp})
        _ev.append({"ua":"control: unknown bot","sent":"GoGoChimpProbe/1.0","status":_ss(_ctrl_un),"at":_pstamp})
    _det="probed "+_pstamp+" from our IP (not the bot's network): "+"; ".join(f"{b}={s if s is not None else 'x'}" for b,s in reach)
    if _anyblk: _det+=f" | controls browser={_ctrl_br if _ctrl_br is not None else 'x'} unknown-bot={_ctrl_un if _ctrl_un is not None else 'x'}"
    if _cleared: _det+=" | "+", ".join(_cleared)+" cleared on spaced re-probe (burst rate-limit, not a block)"
    if rl and not _real: _det+=" | "+", ".join(rl)+" rate-limited (429), likely our probe burst not a block"
    if _real: _det+=" | BLOCKED (robots.txt disallows AND WAF 403 agree): "+", ".join(_real)+(" incl. Google/Bing = a search-indexing risk too" if srch else "")
    if _unver: _det+=(" | INDETERMINATE (robots ALLOWS these, but our impersonated UA got 403): "+", ".join(_unver)
                      +" - this signature is IDENTICAL whether your edge verifies bot identity (and serves the real bot) or runs a block-AI-scrapers rule that rejects the real bot TOO. It cannot be told apart from outside our IP. Not scored either way; only your server logs resolve it (analyze_log).")
    _rc=chk("reachability",_verdict,_det); _rc["evidence"]=_ev; _rc["probe_at"]=_pstamp
    out.append(_rc)
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
                and not pl.endswith((".xml",".xml.gz",".md",".txt",".json",".yaml",".yml",".rss",".atom",".kml",".kmz",".csv",".gpx"))  # machine/agent/data files (agents.md, llms.txt, locations.kml, ...) are not scored pages
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
    # 3+4. link discovery, three phases:
    #   A. harvest the seeded page (and homepage) FIRST, so when a user points the crawl at a hub
    #      like /blog its own articles rank ahead of the bulk sitemap (fixes the "seed the hub" case);
    #   B. add the sitemap pages (the canonical set) after the seed's own children;
    #   C. BFS-expand breadth-first through those pages and every internal link they yield (including
    #      real-href pagination like /page/2/), within a bounded fetch budget, so a hub yields its
    #      articles even when they are absent from the sitemap and unlinked from the homepage.
    # For a sitemap-complete site the cap fills from A+B and phase C is a no-op (parity with the old
    # shallow behaviour); the BFS only does real work when the sitemap under-lists the site.
    want = cap if (cap and cap>0) else 5000
    fetch_budget = min(want,150) if (cap and cap>0) else 300   # light raw fetches (no render) spent on discovery
    ordered=[]; done=set(); harvested=set()
    def _add(u):
        k=u.rstrip("/")
        if k in done: return False
        done.add(k); ordered.append(u); return True
    def _harvest(pg):                                          # fetch a page once and add its internal links
        k=pg.rstrip("/")
        if k in harvested: return
        harvested.add(k)
        _,_,raw,_=fetch_raw(pg)
        if not raw: return
        for a in BeautifulSoup(raw,"lxml").find_all("a",href=True):
            u=urllib.parse.urljoin(pg,a["href"].strip()); u,_=urllib.parse.urldefrag(u)
            if same(u): _add(u)
    # A: seed page first (its children rank highest), then the homepage; both always harvested
    hubs=[]
    if start and same(start): hubs.append(start)
    if (origin+"/").rstrip("/") not in {h.rstrip("/") for h in hubs}: hubs.append(origin+"/")
    for pg in hubs: _add(pg)
    for pg in hubs: _harvest(pg)
    # B: the canonical sitemap set, after the seed's own children
    for _u in sm_urls: _add(_u)
    # C: BFS-expand remaining pages + discovered links (incl. pagination) within the budget
    frontier=list(ordered); qi=0
    while qi < len(frontier) and len(ordered) < want and len(harvested) < fetch_budget:
        pg=frontier[qi]; qi+=1
        before=len(ordered); _harvest(pg)
        for u in ordered[before:]: frontier.append(u)          # follow newly discovered links (pagination chains)
    if cap and cap>0: ordered=ordered[:cap]
    sitemap_paths={(urllib.parse.urlparse(u).path.rstrip("/") or "/") for u in sm_urls}
    return ordered, sitemap_paths

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
    rend=None; rms=0; rescued=False
    if st==200:
        rend,rms=render(url)
    elif CHROME and (st is None or st in (401,402,403,406,429)):
        # The plain fetch was WAF-blocked (urllib is easily fingerprinted). Chrome's real browser fingerprint often
        # gets through, so retry the page through the full rendering browser before giving up. If it loads, score
        # the page on the rendered DOM instead of failing it - the biggest single lever on the WAF-block failure
        # rate. raw is set to the render so render-parity is not falsely flagged "JS-only" (we never saw the true
        # raw); the page is marked rendered_only so the report can say it was reached only through the browser.
        rend,rms=render(url)
        if rend:
            st=200; raw=rend; rescued=True
    page=analyze(url,st,raw,rend,domain,hdrs,fms)
    page.update({"url":url,"status":st,"fetch_ms":fms,"render_ms":rms,
                 "depth":len([x for x in urllib.parse.urlparse(url).path.strip("/").split("/") if x]),
                 "server":hdrs.get("Server","")})
    if rescued: page["rendered_only"]=True
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
        # SSRF guard (online worker): outbound links come from the CRAWLED page, i.e. attacker-controllable. Never
        # probe a private/loopback/link-local target, and re-validate redirects per hop (same defence as the main
        # fetch at ssrf_ok/_GUARD_OPENER). A blocked link returns None, indistinguishable from unreachable, so the
        # broken-links section can't be used as an oracle into the host's network.
        if _ssrf_on() and not ssrf_ok(u): return u,None
        _open = _GUARD_OPENER.open if _ssrf_on() else urllib.request.urlopen
        for method in ("HEAD","GET"):
            try:
                req=urllib.request.Request(u,method=method,headers={"User-Agent":UA})
                with _open(req,timeout=8) as r: return u,r.status
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
    # ARD (Agentic Resource Discovery) catalog - Google Lighthouse 13.5 audits this under "Agent Discoverability".
    # Discover via a robots.txt Agentmap directive OR /.well-known/ai-catalog.json (spec v0.91: ard.json). The
    # link-tag / Link-header routes are per-page; this site-level probe checks the two cheap fixed locations.
    _ard_found=False; _ard_where=None
    try:
        for _ap in ("/.well-known/ai-catalog.json","/.well-known/ard.json"):
            st,_,body,_=fetch_raw(origin+_ap,timeout=8)
            if st==200 and body: _ard_found=True; _ard_where=_ap; break
        if not _ard_found:
            rst,_,rbody,_=fetch_raw(origin+"/robots.txt",timeout=8)
            if rst==200 and rbody and re.search(r"(?im)^\s*Agentmap\s*:", rbody):
                _ard_found=True; _ard_where="robots.txt Agentmap"
    except Exception: pass
    out["Agent resource catalog (ARD)"]={"found":_ard_found,"path":_ard_where or "/.well-known/ai-catalog.json"}
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
def _block_cause(h, s):
    """Attribute a blocked/failed bot fetch to a vendor from response headers, so a bare 403 becomes
    'Cloudflare' (which default-blocks AI crawlers on domains created after Jul 2025) instead of a mystery."""
    if s not in (401,403,429,503,0,None): return ""
    g=lambda k:str(h.get(k) or h.get(k.lower()) or h.get(k.title()) or "")
    srv=g("Server").lower()
    if "cloudflare" in srv or g("cf-ray") or g("cf-mitigated"): return "Cloudflare"
    if "sucuri" in srv or g("x-sucuri-id"): return "Sucuri"
    if "akamai" in srv or g("x-akamai-transformed") or g("x-akamai-request-id"): return "Akamai"
    if "squarespace" in srv: return "Squarespace"
    if "awselb" in srv or g("x-amz-cf-id"): return "AWS"
    if "fastly" in srv: return "Fastly"
    return srv.split("/")[0].title() if srv else ""

def ai_crawler_matrix(origin):
    """AI-bot access matrix: robots.txt rules for every bot, PLUS a live reachability probe of the
    citation-serving bots, so the tab can show robots-allowed-but-firewall-blocked. Per bot, `reach`
    is the HTTP status fetching the homepage as that bot (0 = unreachable/failed; null = not probed)."""
    st,_,robots,_=fetch_raw(origin+"/robots.txt")
    groups=_parse_robots(robots)
    _uamap=dict(SERVING_UAS)
    def _probe(nm):
        s,hd,_,_=fetch_raw(origin+"/",ua=_uamap[nm])
        if s in (401,403,429):                  # 401/403/429 from OUR concurrent burst is usually the WAF rate-limiting
            time.sleep(2.0); s,hd,_,_=fetch_raw(origin+"/",ua=_uamap[nm])   # the burst, not a real per-bot block -> one spaced re-probe
        return (nm, s if s is not None else 0, _block_cause(hd,s))
    reachmap={}; causemap={}
    with ThreadPoolExecutor(max_workers=3) as ex:   # gentler burst so we do not self-trigger the WAF rate limit
        for nm,s,cz in ex.map(_probe,[n for n,_ in SERVING_UAS]): reachmap[nm]=s; causemap[nm]=cz
    bots=[{"name":n,"ua":ua,"op":op,"role":role,"purpose":pur,
           "status":_bot_status(groups,ua),"reach":reachmap.get(n),"cause":causemap.get(n,"")} for n,ua,op,role,pur in AI_BOTS]
    return {"has_robots":bool(st==200 and robots),"bots":bots}

def common_crawl_presence(domain, timeout=10):
    """ADVISORY (not scored): how many pages of this domain are in the latest Common Crawl monthly index.
    Common Crawl feeds ~64% of LLM training sets (C4/RefinedWeb/FineWeb/RedPajama/Dolma), so absence = invisible
    to the training layer. Best-effort + fail-safe (short timeouts, try/except): never breaks an audit."""
    try:
        st,_,body,_=fetch_raw("https://index.commoncrawl.org/collinfo.json", timeout=timeout)
        if st!=200 or not body: return {"ok":False,"reason":"index list unavailable"}
        cols=json.loads(body)
        if not cols: return {"ok":False,"reason":"no crawls listed"}
        latest=cols[0]; api=latest.get("cdx-api") or ""
        if not api: return {"ok":False,"reason":"no cdx endpoint"}
        q=api+"?url="+urllib.parse.quote(domain)+"&matchType=domain&output=json&fl=url&limit=1000"
        st2,_,body2,_=fetch_raw(q, timeout=timeout)
        if st2==404: return {"ok":True,"crawl":latest.get("id",""),"crawl_name":latest.get("name",""),"captures":0,"capped":False}
        if st2!=200: return {"ok":False,"reason":"query failed ("+str(st2)+")","crawl":latest.get("id","")}
        paths=set(); n=0
        for line in body2.splitlines():
            line=line.strip()
            if not line: continue
            n+=1
            try: _u=json.loads(line).get("url","")
            except Exception: _u=""
            if _u:
                try: paths.add(urllib.parse.urlparse(_u).path.rstrip("/").lower() or "/")
                except Exception: pass
        return {"ok":True,"crawl":latest.get("id",""),"crawl_name":latest.get("name",""),"captures":n,"capped":n>=1000,"paths":sorted(paths)[:2000]}
    except Exception as e:
        return {"ok":False,"reason":str(e)[:80]}

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
    # Credential-scan / impersonation detection: real AI crawlers fetch CONTENT, never secrets. Requests to
    # credential/config paths (.env, .ssh, .mcp.json, cloud creds) are scanners; any carrying a trusted-bot
    # user-agent (CCBot/GPTBot/...) are SPOOFING it (UAs are forgeable). Flag them and prompt reverse-DNS checks.
    SENSITIVE = re.compile(r"(?:/\.(?:env|ssh|git|aws|npmrc|continue|vscode)|/\.env|/\.mcp\.json|id_rsa|/wp-config|firebase|service[-_]?account|/credentials|/config\.json|\.pem$)", re.I)
    TRUSTED_UA = re.compile("|".join(re.escape(b[1]) for b in AI_BOTS) + r"|CCBot|Common\s?Crawl", re.I)
    scan = [r for r in rows if SENSITIVE.search(r[1] or "")]
    spoof = [r for r in scan if TRUSTED_UA.search(r[0] or "")]
    cred = {}
    if scan:
        cred = {"credential_path_requests": len(scan), "spoofing_a_trusted_bot_ua": len(spoof),
                "top_paths": [[u, c] for u, c in Counter(r[1] for r in scan).most_common(10)],
                "note": ("Requests to secret/config paths (.env, .ssh, .mcp.json, cloud creds). Real AI crawlers "
                         "fetch content, never secrets, so these are credential scanners; any carrying a trusted-bot "
                         "user-agent (CCBot/GPTBot/etc.) are impersonating it. User-agents are forgeable - verify the "
                         "big claimed bots by reverse-DNS against their published IP ranges before trusting the traffic "
                         "as real AI-crawler activity.")}
    return {"parsed":len(rows),"total_bot_hits":sum(b["hits"] for b in bots.values()),
            "credential_scan":cred,
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
    return {"tool":"Rubric log monitor","window":{"first":dates[0] if dates else None,
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
    return {"tool":"Rubric check_draft","url":url,"page_type":page.get("type"),
            "words":page.get("metrics",{}).get("words"),"verdict":verdict,
            "passing_checks":passing,"bad":bad,"fix_count":len(fixes),"fixes":fixes,
            "note":"Lints the draft body's extractability; publish-wrapper checks (title, meta, schema, date, author) are excluded and handled at publish."}

def cited_gap(your_url, winner_url, query=None):
    """The 'you vs the cited winner' diff - the heart of the AI-citation fix loop. Given YOUR page and the
    page an engine actually CITED for a query (captured from the answer in the user's own browser), score
    both and return the on-page citability signals the winner has that you lack, ranked by how much engines
    weight them, each with the fix. The capture (which page was cited) is the browser's job; this explains
    WHY and HOW to close it - no engine API, so it can't run at a loss."""
    def _one(u):
        d = run_audit(u, out=None, max_pages=1, links=False)
        pages = d.get("pages") or []
        return d, (pages[0] if pages else {})
    dy, py = _one(your_url); dw, pw = _one(winner_url)
    smap = lambda pg: {c.get("id"): c.get("status") for c in (pg.get("checks") or []) if c.get("id")}
    sy, sw = smap(py), smap(pw)
    wsum = lambda cid: sum(ENGINE_WEIGHTS[e].get(cid, 0) for e in ENGINE_WEIGHTS)
    engs = lambda cid: [e for e in ENGINE_WEIGHTS if ENGINE_WEIGHTS[e].get(cid, 0) > 0]
    gaps = []
    for cid, ws in sw.items():
        if ws == "good" and sy.get(cid) in ("bad", "warn"):
            m = CHECK_META.get(cid, {})
            if m.get("phase") == 0: continue          # skip info-only (llms.txt etc.)
            gaps.append({"check": cid, "label": m.get("label", cid), "pillar": m.get("pillar"),
                         "your_status": sy.get(cid), "winner_status": "good", "weight": wsum(cid),
                         "engines": engs(cid), "fix": FIX.get(cid, ""), "fix_deep": FIX_DEEP.get(cid, "")})
    gaps.sort(key=lambda g: -g["weight"])
    ahead = [CHECK_META.get(cid, {}).get("label", cid) for cid, ss in sy.items()
             if ss == "good" and sw.get(cid) in ("bad", "warn") and CHECK_META.get(cid, {}).get("phase") != 0][:8]
    ys, wsq = py.get("score"), pw.get("score")
    if not gaps:
        verdict = ("You match or beat the cited page on the on-page signals we measure (%s vs %s). If they're "
                   "cited and you're not, the gap is likely off-page: domain authority, third-party mentions, "
                   "or query intent - not this page's structure." % (ys, wsq))
    else:
        verdict = ("The cited page scores %s to your %s and clears %d citability signal%s you don't. Close the "
                   "ranked gaps below, strongest-weighted first." % (wsq, ys, len(gaps), "" if len(gaps) == 1 else "s"))
    return {"tool": "Rubric cited_gap", "query": query,
            "you": {"url": your_url, "domain": dy.get("domain"), "score": ys},
            "winner": {"url": winner_url, "domain": dw.get("domain"), "score": wsq},
            "score_gap": (wsq or 0) - (ys or 0), "verdict": verdict, "gaps": gaps, "you_already_ahead": ahead,
            "note": "On-page diff of the two URLs (single-page fetch each). WHICH page got cited comes from the "
                    "browser; off-page factors (authority, third-party mentions) are not measured here."}

def cited_gap_bridge(your_url, competitor_url, query=None):
    """Surface B, FREE for logged-in members (board spec change 27 Aug). A deterministic diff of two PUBLIC pages,
    API-free -- so it is free-tier by our own rule (free = what a free crawl produced). Returns the FULL gap: the
    label, pillar, weight, engines and the FIX for each signal the competitor clears that you don't. The paid tier
    keeps only what a diff CANNOT answer -- which query they win, whether an engine actually cited them, and whether
    it is changing (source_of_truth / analyze_ai_answer / logs). Member report only; the anon teaser strips
    d['bridge']. Returns None on failure so the card is omitted rather than shown broken."""
    try:
        g = cited_gap(your_url, competitor_url, query=query)
    except Exception:
        return None
    _gaps = g.get("gaps") or []
    rows = [{"label": x.get("label"), "pillar": x.get("pillar"), "weight": x.get("weight"),
             "engines": x.get("engines"), "your_status": x.get("your_status"), "fix": x.get("fix")} for x in _gaps]
    return {"query": query,
            "you": {"domain": (g.get("you") or {}).get("domain"), "score": (g.get("you") or {}).get("score")},
            "competitor": {"domain": (g.get("winner") or {}).get("domain"), "score": (g.get("winner") or {}).get("score")},
            "score_gap": g.get("score_gap"), "gap_count": len(_gaps),
            "ahead_count": len(g.get("you_already_ahead") or []),
            "rows": rows}   # FULL gap -- label + fix per signal (free for members)

# ------------------------------------------------------------------ reasoning tools (compose over data)
def click_resilience(page):
    """Per-page CLICK-RESILIENCE band: will AI still send a click, or does it answer inline? Derived from
    signals Rubric already computes (page type, info-gain / original data, actionable schema, tables,
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
    return {"tool":"Rubric click_resilience","url":page.get("url"),"type":typ,"words":wc,
            "band":band,"signal_score":score,"reasons":reasons,"advice":advice}

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = {"fbclid", "gclid", "mc_eid", "mc_cid", "gclsrc", "_hsenc", "_hsmi", "igshid"}

def canon_url(u: str):
    """One canonical form for every URL join in the proof loop. Returns (key, display).
    key: scheme-insensitive, www stripped, host lowercased, trailing slash removed,
    tracking params stripped, path case and meaningful query preserved."""
    display = (u or "").strip()
    try:
        p = urllib.parse.urlsplit(display if "//" in display else "https://" + display)
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if p.port:
            host = f"{host}:{p.port}"
        path = p.path.rstrip("/") or ""
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not (k.lower() in _TRACKING_EXACT or k.lower().startswith(_TRACKING_PREFIXES))]
        q.sort()
        query = urllib.parse.urlencode(q)
        key = host + path + (("?" + query) if query else "")
    except Exception:
        key = display.lower().rstrip("/")
    return key, display

def canon_key(u: str) -> str:
    return canon_url(u)[0]

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
                    h=log_hits.setdefault(canon_key(u),{"ai":0,"cite":0}); h["ai"]+=1
                    if name in CITE_TIME: h["cite"]+=1
                    break
    rows=[]
    have_cites=bool(cites)
    for p in pages:
        if p.get("status")!=200: continue
        k=canon_key(p["url"]); lh=log_hits.get(k) or {}
        cval=cites.get(k)
        if cval is None and have_cites: cval=0   # a citation export lists every page that earned citations; absent => 0 reported (not "unknown")
        rows.append({"url":p["url"],"type":p.get("type"),"score":p.get("score"),
                     "citations":cval,"ai_fetches":lh.get("ai"),"citation_time_fetches":lh.get("cite")})
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
    return {"tool":"Rubric correlate","joined":len(rows),"have_citations":bool(cites),"have_logs":bool(logrows),
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
    return {"tool":"Rubric estimate_ai_influence","period_days":period_days,
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
        return {"tool":"Rubric actionable_readiness","score":None,"band":None,"money_pages":0,
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
    return {"tool":"Rubric actionable_readiness","score":score,"band":band,"money_pages":money,
            "agent_protocol_files":proto_any,"gaps":sorted(gaps,key=lambda g:-g["missing_on_money_pages"]),
            "note":"Advisory 'Actionable' pillar (can an AI agent transact here?) - NOT in the core score; the missing signals are what stops an agent completing a purchase/booking/contact. Watch agentic-commerce adoption before graduating it."}

# Platforms whose catalog can auto-feed the AI-shopping surfaces (ChatGPT Shopping / agentic checkout) without the
# merchant standing up a separate feed. Everything else needs a Merchant Center / product feed to be ingested.
AUTOFEED_PLATFORMS = {"Shopify"}

def _detect_platform(origin):
    """Best-effort ecommerce PLATFORM from the homepage (markers in the HTML + response headers). Local, one
    fetch, no keys. Returns (platform, evidence) or (None, None) if unreadable. Reliable signal (unlike a feed,
    which a crawl cannot see): platform decides whether the store's catalog can AUTO-feed AI shopping."""
    try:
        st, hdrs, raw, _ = fetch_raw(origin, timeout=10)
    except Exception:
        return (None, None)
    if st != 200 or not raw:
        return (None, None)
    low = raw.lower()
    try: hstr = " ".join("%s:%s" % (k, v) for k, v in dict(hdrs or {}).items()).lower()
    except Exception: hstr = str(hdrs or "").lower()
    # Match INFRASTRUCTURE markers (CDN hosts, plugin paths, framework classes), never a bare word that a page
    # could merely mention in its content (a CRO blog naming "WooCommerce" must not read as a WooCommerce store).
    if any(m in low for m in ("cdn.shopify.com", "cdn/shop/", "myshopify.com", "shopify.theme")) or "x-shopid" in hstr or "x-shopify" in hstr:
        return ("Shopify", "Shopify storefront markers")
    if "wp-content/plugins/woocommerce" in low or "/wc-ajax/" in low or "wc-block-" in low or 'class="woocommerce' in low:
        return ("WooCommerce", "WooCommerce install markers")
    if "cdn11.bigcommerce.com" in low or "/stencil/" in low:
        return ("BigCommerce", "BigCommerce markers")
    if "static.parastorage.com" in low or "wixstores" in low:
        return ("Wix", "Wix markers")
    if "static.squarespace.com" in low or "squarespace-cdn.com" in low:
        return ("Squarespace", "Squarespace markers")
    if "/static/version" in low or "mage-init" in low or "magento_" in low:
        return ("Magento", "Magento markers")
    return ("custom", None)

def commerce_readiness(platform_tuple, agentready):
    """AI-SHOPPING FEED advisory for an ecommerce-typed crawl. ChatGPT Shopping / agentic checkout increasingly
    answer from PRODUCT FEEDS, not open-web retrieval, so on-page citability is necessary-not-sufficient here.
    Routes on the platform: auto-feed platforms (Shopify) are eligible - verify enabled; other platforms need a
    Merchant Center / product feed or they are invisible to AI shopping regardless of score. HONEST LIMIT: a crawl
    sees the platform + on-page product data, NOT the actual Merchant Center feed - this is feed ELIGIBILITY, not
    a feed audit. `platform_tuple` is (name, evidence) detected BEFORE the crawl (a post-crawl re-fetch of a
    rate-limited store is unreliable), or None. Reuses the Agent-ready money-page data for the on-page half."""
    plat, ev = platform_tuple if platform_tuple else (None, None)
    return {"platform": plat, "evidence": ev, "autofeed": (plat in AUTOFEED_PLATFORMS),
            "money_pages": (agentready or {}).get("money_n") or 0}

# ------------------------------------------------------------------ AI-citation fix-loop tools (Wave 3)
# These compose over data the user's own Claude+Chrome CAPTURES (fan-out sub-queries, cited URLs) or over a
# GSC/GA CSV the user exports - no paid engine API, so the loop can't run at a loss.
def _ai_norm_url(u):
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u); u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")

def _ai_domain_of(u):
    return _ai_norm_url(u).split("/")[0]

_SOT_SURFACES = [("Reddit","reddit.com"),("Wikipedia","wikipedia.org"),("YouTube","youtube.com"),
  ("Quora","quora.com"),("LinkedIn","linkedin.com"),("Medium","medium.com"),("G2","g2.com"),
  ("Capterra","capterra.com"),("Trustpilot","trustpilot.com"),("GitHub","github.com"),
  ("Stack Overflow","stackoverflow.com"),("X / Twitter","twitter.com"),("Crunchbase","crunchbase.com"),
  ("Product Hunt","producthunt.com"),("Wikidata","wikidata.org"),("Forbes","forbes.com"),("TechCrunch","techcrunch.com")]

def source_of_truth(cited_urls, your_domain=""):
    """Classify the URLs an engine CITED (captured from the answer in the browser) into own-site vs the
    third-party sources engines trust - Reddit, Facebook Groups, Wikipedia, YouTube, G2, review sites, news. Shows whether the
    fix is 'improve your page' or 'get INTO the source' (brands are ~6.5x likelier to be cited via third
    parties than their own domain). Pass the cited URLs + your domain."""
    yd = _ai_domain_of(your_domain); buckets = {}; own = 0
    for u in (cited_urls or []):
        d = _ai_domain_of(u)
        if not d: continue
        if yd and (d == yd or d.endswith("." + yd)): own += 1; continue
        label = next((name for name, dom in _SOT_SURFACES if dom in d), d)
        buckets[label] = buckets.get(label, 0) + 1
    total = own + sum(buckets.values()) or 1
    ranked = sorted(buckets.items(), key=lambda x: -x[1]); top = ranked[0] if ranked else None
    if own and not ranked:
        rec = "Every cited source is your own site - your page IS the source of truth here. Keep it strong."
    elif top:
        rec = ("Most citations come from %s (%d of %d). The lever is often to get INTO that source (a strong %s "
               "presence/entry), not only to fix your own page." % (top[0], top[1], total, top[0]))
    else:
        rec = "No third-party sources cited."
    return {"tool": "Rubric source_of_truth", "your_domain": yd, "total_cited": total,
            "own_site": own, "own_site_pct": round(100 * own / total),
            "third_party": [{"source": n, "citations": k} for n, k in ranked], "recommendation": rec,
            "note": "Where the engine's trust sits. A low own_site_pct means the win is presence in the cited sources, not on-page fixes."}

def subquery_sov(fanout, your_domain, competitors=""):
    """Sub-query share-of-voice across the FAN-OUT. Given the sub-queries an engine expanded a prompt into
    and the sources it cited for each (captured from the answer in the browser), report the % of the fan-out
    citing YOU vs each competitor, plus the exact sub-queries where you're missing. The honest version of
    'share of AI voice' - coverage across the real fan-out, not one vanity number. fanout = list of
    {subquery, cited:[domains or urls]}."""
    yd = _ai_domain_of(your_domain)
    comp = [_ai_domain_of(c) for c in re.split(r"[,\n]", competitors or "") if c.strip()]
    rows = []
    for f in (fanout or []):
        if isinstance(f, dict):
            sq = f.get("subquery") or f.get("query") or ""
            cited = f.get("cited_domains") or f.get("cited") or f.get("cited_urls") or []
        else:
            sq, cited = str(f), []
        dset = {_ai_domain_of(c) for c in cited}
        rows.append({"subquery": sq, "you": yd in dset, "competitors": [c for c in comp if c in dset]})
    n = len(rows) or 1
    you_n = sum(1 for r in rows if r["you"])
    comp_cov = {c: sum(1 for r in rows if c in r["competitors"]) for c in comp}
    total_named = you_n + sum(comp_cov.values())
    sov = round(100 * you_n / total_named, 1) if total_named else 0.0
    board = sorted([{"domain": c, "cited_on": k, "coverage_pct": round(100 * k / n)} for c, k in comp_cov.items()],
                   key=lambda x: -x["cited_on"])
    return {"tool": "Rubric subquery_sov", "your_domain": yd, "fanout_size": len(rows),
            "you_cited_on": you_n, "your_coverage_pct": round(100 * you_n / n), "share_of_voice_pct": sov,
            "competitor_leaderboard": board, "missing_subqueries": [r["subquery"] for r in rows if not r["you"]][:20],
            "note": "Coverage across the captured fan-out. your_coverage_pct = share of sub-queries citing you; share_of_voice_pct = your citations vs competitors'. Target the missing_subqueries with pages/sections that answer them."}

def competitor_teardown(competitor_url, your_url):
    """Run the citability diff the OTHER way: find where a competitor is WEAK so you can take those
    sub-queries. Crawls both (single page each) and returns the competitor's weakest citability signals
    ranked by engine weight, split into 'you already beat them here' and 'open for both'."""
    def _one(u):
        d = run_audit(u, out=None, max_pages=1, links=False); pg = (d.get("pages") or [{}])[0]
        return d, {c.get("id"): c.get("status") for c in (pg.get("checks") or []) if c.get("id")}, pg.get("score")
    dc, sc, csc = _one(competitor_url); dy, sy, ysc = _one(your_url)
    wsum = lambda cid: sum(ENGINE_WEIGHTS[e].get(cid, 0) for e in ENGINE_WEIGHTS)
    weak = []
    for cid, st in sc.items():
        if st in ("bad", "warn"):
            m = CHECK_META.get(cid, {})
            if m.get("phase") == 0: continue
            weak.append({"check": cid, "label": m.get("label", cid), "pillar": m.get("pillar"),
                         "competitor_status": st, "your_status": sy.get(cid), "weight": wsum(cid),
                         "you_win": sy.get(cid) == "good", "fix": FIX.get(cid, "")})
    weak.sort(key=lambda g: -g["weight"])
    return {"tool": "Rubric competitor_teardown",
            "you": {"url": your_url, "domain": dy.get("domain"), "score": ysc},
            "competitor": {"url": competitor_url, "domain": dc.get("domain"), "score": csc},
            "you_already_beat_them": [w for w in weak if w["you_win"]][:15],
            "open_for_both": [w for w in weak if not w["you_win"]][:15],
            "note": "The competitor's weak citability signals, ranked by engine weight. 'you_already_beat_them' = press that advantage now; 'open_for_both' = fix on your page to leapfrog. On-page only; off-page authority not measured."}

def _load_perf_csv(path):
    """Tolerant loader for a Google Search Console 'Pages' export or a GA landing-page CSV (url, clicks,
    impressions), whatever the column casing."""
    rows = []
    if not path or not os.path.exists(path): return rows
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            low = {(k or "").strip().lower(): v for k, v in r.items()}
            url = (low.get("page") or low.get("top pages") or low.get("url") or low.get("landing page") or "").strip()
            if not url: continue
            def _num(*keys):
                for k in keys:
                    v = low.get(k)
                    if v not in (None, ""):
                        try: return float(str(v).replace(",", "").replace("%", ""))
                        except Exception: pass
                return 0.0
            rows.append({"url": url, "clicks": _num("clicks"), "impressions": _num("impressions")})
    return rows

def value_bridge(url, perf_csv, value_per_click=0.0, max_pages=0, progress=None):
    """The value bridge: join your citability gaps to real traffic and put a RANGED 'clicks at risk' number
    on being un-citable. Crawls the site, matches pages to a GSC 'Pages' export (or GA landing-page CSV: url,
    clicks, impressions), and for pages that pull traffic but score low on citability, estimates the clicks
    AI answers put at risk. Honest ranges, never attribution. perf_csv is a local file path; value_per_click
    (optional) converts clicks to money."""
    d = run_audit(url, out=None, max_pages=max_pages, links=False, progress=progress)
    scores = {_ai_norm_url(p.get("url")): p for p in (d.get("pages") or [])}
    perf = _load_perf_csv(perf_csv)
    at_risk = []; tot_clicks = 0.0; tot_risk = 0.0; matched = 0
    for pr in perf:
        pg = scores.get(_ai_norm_url(pr["url"]))
        if not pg: continue
        matched += 1
        s = pg.get("score") or 0; clk = pr["clicks"]; tot_clicks += clk
        weak = max(0.0, 1 - s / 100.0)                 # weakly citable = AI can answer around you
        risk = clk * 0.40 * weak                        # AIOs cut organic clicks ~40% where they appear
        if clk > 0 and s < 75:
            tot_risk += risk
            at_risk.append({"url": pr["url"], "score": s, "clicks": round(clk), "clicks_at_risk": round(risk, 1)})
    at_risk.sort(key=lambda x: -x["clicks_at_risk"])
    lo, hi = tot_risk * 0.5, tot_risk
    out = {"tool": "Rubric value_bridge", "domain": d.get("domain"), "pages_matched": matched,
           "total_clicks_in_data": round(tot_clicks),
           "clicks_at_risk_low": round(lo), "clicks_at_risk_high": round(hi), "top_at_risk_pages": at_risk[:15],
           "basis": "AI Overviews cut organic clicks ~40% where they appear (Agarwal & Sen field RCT 2026); risk scales with how weakly citable each page is (1 - Rubric/100). Cited pages earn ~35% higher CTR (Seer 2025) = the recovery upside.",
           "note": "A RANGED estimate, not attribution. Fix the low-scoring high-traffic pages first: that is where citability protects the most clicks."}
    if value_per_click and value_per_click > 0:
        out["value_at_risk_low"] = round(lo * value_per_click, 2)
        out["value_at_risk_high"] = round(hi * value_per_click, 2)
        out["value_per_click"] = value_per_click
    return out

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
    return {"tool":"Rubric scan_ai_answer","brand":brand,"brand_mentioned":brand_mentioned,
            "domain_cited":domain_cited,"competitors_named":named,"competitor_count":len(named),
            "brand_position":pos,"answer_chars":len(t),
            "note":"Deterministic presence check only. For FRAMING (sentiment/positioning/caveats) and WHO WINS, have the model read the answer - this tool does not judge quality."}

def scan_ai_panel(answers, brand, domain="", competitors=""):
    """Panel / POLLING layer of the AI ground-truth audit: aggregate the SAME question's answers from
    several engines (answers = list of {engine, text}) into one cross-engine view. Built for the reality
    that 91% of AI citations show on only ONE engine (Indig H1 2026), so a single-engine read misleads.
    Surfaces where the brand is NAMED vs merely CITED (the 'cited != recommended' gap), panel share of
    voice, and the competitor leaderboard. Mechanical only; the browser probing that GETS the answers
    stays assisted (no automation/keys in the tool). Framing / who-wins nuance stays with the model."""
    if isinstance(answers, dict):
        answers=[{"engine":k,"text":v} for k,v in answers.items()]
    dclean=(domain or "").lower().replace("https://","").replace("http://","").replace("www.","").rstrip("/")
    bl=(brand or "").lower()
    rows=[]
    for a in (answers or []):
        eng=((a.get("engine") if isinstance(a,dict) else None) or "?").strip() or "?"
        txt=str((a.get("text") if isinstance(a,dict) else a) or "")
        r=scan_ai_answer(txt, brand, domain, competitors)
        # NAMED = brand appears in PROSE with the domain stripped out first, so a brand whose name sits
        # inside its own domain (GoGoChimp / gogochimp.com) is not counted as 'named' merely because it is CITED.
        prose=txt.lower().replace(dclean," ") if dclean else txt.lower()
        named=bool(bl) and bl in prose
        pos=round(prose.find(bl)/max(1,len(prose)),3) if named else None
        rows.append({"engine":eng,"brand_named":named,"domain_cited":r["domain_cited"],
                     "brand_position":pos,"competitors_named":r["competitors_named"]})
    n=len(rows)
    comp=[c.strip() for c in re.split(r"[,\n]", competitors or "") if c.strip()]
    named_on=[r["engine"] for r in rows if r["brand_named"]]
    cited_on=[r["engine"] for r in rows if r["domain_cited"]]
    cited_not_named=[r["engine"] for r in rows if r["domain_cited"] and not r["brand_named"]]
    named_not_cited=[r["engine"] for r in rows if r["brand_named"] and not r["domain_cited"]]
    board={c:sum(1 for r in rows if any(c.lower()==x.lower() for x in r["competitors_named"])) for c in comp}
    leaderboard=sorted([{"name":c,"engines":k} for c,k in board.items()], key=lambda x:-x["engines"])
    ranking=sorted([{"name":brand,"engines":len(named_on)}]+leaderboard, key=lambda x:-x["engines"])
    brand_rank=next((i+1 for i,x in enumerate(ranking) if x["name"]==brand), None)
    positions=[r["brand_position"] for r in rows if r["brand_position"] is not None]
    avg_pos=round(sum(positions)/len(positions),3) if positions else None
    total_namings=len(named_on)+sum(board.values())
    sov=round(100*len(named_on)/total_namings,1) if total_namings else 0.0
    return {"tool":"Rubric scan_ai_panel","brand":brand,"engines_polled":n,
            "brand_named_on":len(named_on),"named_engines":named_on,
            "brand_cited_on":len(cited_on),"cited_engines":cited_on,
            "cited_not_recommended":cited_not_named,    # a SOURCE but the brand isn't named = the 'cited != recommended' gap
            "recommended_not_cited":named_not_cited,
            "share_of_voice_pct":sov,"brand_rank":brand_rank,"brand_position_avg":avg_pos,
            "competitor_leaderboard":leaderboard[:12],"per_engine":rows,
            "note":"Mechanical panel aggregation of pasted answers. Treat it as a POLL - run the same question across engines and watch NAMED-vs-merely-CITED + the competitor leaderboard. The browser probing that gets the answers stays assisted (no automation/keys). Sentiment/framing/who-wins nuance stays with the model."}

def _fixmoji(s):
    """Repair the common UTF-8-read-as-latin-1 mojibake (£ -> "Â£", en-dash -> "â€\"") in a short text
    field, so an extracted price/label displays cleanly. Guarded: only attempted when the tell-tale
    bytes are present, and never allowed to raise."""
    if isinstance(s,str) and ("Â" in s or "â€" in s or "Ã" in s):
        try: return s.encode("latin-1").decode("utf-8")
        except Exception: return s
    return s

def _ground_facts(domain):
    """Best-effort STRUCTURED facts about a brand from its OWN site: fetch a few key pages, read
    Organization/LocalBusiness/Product JSON-LD (expanding @graph) plus a little homepage/about text,
    and return a truth dict. Local, no keys. This is the ground truth the hallucination check diffs
    an AI answer against."""
    origin = domain if str(domain).startswith("http") else "https://"+str(domain)
    p=urllib.parse.urlparse(origin); base=f"{p.scheme}://{p.netloc}"
    cand=[base+"/", base+"/about", base+"/about-us", base+"/contact", base+"/pricing", base+"/company"]
    facts={"name":None,"founded":None,"locality":None,"region":None,"country":None,"phone":None,"prices":[],"sameas":[]}
    texts=[]
    for u in cand:
        try: st,_,raw,_=fetch_raw(u, timeout=10)
        except Exception: continue
        if st!=200 or not raw: continue
        soup=BeautifulSoup(raw,"lxml")
        for top in jsonld_objs(soup):
            stack=[top]
            while stack:
                node=stack.pop()
                if isinstance(node,list): stack.extend(node); continue
                if not isinstance(node,dict): continue
                if isinstance(node.get("@graph"),list): stack.extend(node["@graph"])
                tt=node.get("@type"); tset={str(x) for x in ([tt] if isinstance(tt,str) else tt if isinstance(tt,list) else [])}
                if tset & {"Organization","LocalBusiness","Corporation","ProfessionalService","NGO","OnlineBusiness"}:
                    if not facts["name"] and isinstance(node.get("name"),str): facts["name"]=node["name"].strip()
                    fd=node.get("foundingDate") or node.get("dateFounded")
                    if fd and not facts["founded"]:
                        m=re.search(r"(?:18|19|20)\d{2}", str(fd))
                        if m: facts["founded"]=m.group(0)
                    addr=node.get("address")
                    if isinstance(addr,dict):
                        facts["locality"]=facts["locality"] or (addr.get("addressLocality") if isinstance(addr.get("addressLocality"),str) else None)
                        facts["region"]=facts["region"] or (addr.get("addressRegion") if isinstance(addr.get("addressRegion"),str) else None)
                        facts["country"]=facts["country"] or (addr.get("addressCountry") if isinstance(addr.get("addressCountry"),str) else None)
                    if not facts["phone"] and isinstance(node.get("telephone"),str): facts["phone"]=node["telephone"].strip()
                    sa=node.get("sameAs")
                    if isinstance(sa,str): facts["sameas"].append(sa)
                    elif isinstance(sa,list): facts["sameas"].extend(x for x in sa if isinstance(x,str))
                    if isinstance(node.get("priceRange"),str): facts["prices"].append(node["priceRange"].strip())
                if tset & {"Product","Offer","AggregateOffer","Service"}:
                    off=node.get("offers") if isinstance(node.get("offers"),dict) else node
                    if isinstance(off,dict):
                        for pk in ("price","lowPrice","highPrice"):
                            if off.get(pk) not in (None,""): facts["prices"].append(str(off[pk]))
        if u in (base+"/", base+"/about", base+"/about-us"):
            texts.append(soup.get_text(" ", strip=True)[:6000])
    if not facts["founded"] and texts:
        m=re.search(r"(?:founded|established|est\.?|since|incorporated|started|launched)\s+(?:in\s+)?((?:18|19|20)\d{2})", " ".join(texts), re.I)
        if m: facts["founded"]=m.group(1)
    facts["sameas"]=sorted(set(facts["sameas"])); facts["prices"]=sorted(set(_fixmoji(x) for x in facts["prices"]))
    return facts

def check_ai_facts(answer_text, domain, brand=""):
    """Hallucination / entity-substitution check: DIFF the factual claims in a pasted AI answer against
    the brand's OWN site (Organization/Product schema + key pages). Flags where the AI states a founding
    year, location, or price that contradicts your site (a stale-training hallucination) or that your
    site never states (a gap the AI is filling in for you). Local, no keys. High-precision by design: it
    compares only STRUCTURED facts readable on both sides, so it under-reports rather than guesses. This
    is the local, crawl-only half of the ground-truth audit (the browser-driven verification stays a
    service); it makes 'the AI is wrong about you, and here is the truth next to it' a repeatable check."""
    t=answer_text or ""
    truth=_ground_facts(domain)
    F=[]
    ay=re.search(r"(?:founded|established|est\.?|since|incorporated|started|launched|began)\s+(?:in\s+)?((?:18|19|20)\d{2})", t, re.I)
    ay=ay.group(1) if ay else None
    if truth["founded"] and ay and ay!=truth["founded"]:
        F.append({"fact":"founding year","ai_says":ay,"your_site":truth["founded"],"verdict":"mismatch"})
    elif ay and not truth["founded"]:
        F.append({"fact":"founding year","ai_says":ay,"your_site":None,"verdict":"unverifiable"})
    if truth["locality"]:
        m=re.search(r"(?:based|located|headquartered|based out of|operating)\s+(?:in|out of|at)\s+([A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+){0,3})", t)
        if m:
            said=m.group(1).strip().rstrip(".")
            loc=truth["locality"]
            if loc.lower() not in said.lower() and said.lower() not in loc.lower():
                F.append({"fact":"location","ai_says":said,"your_site":loc,"verdict":"mismatch"})
    ap=re.findall(r"[$£€]\s?\d[\d,]*(?:\.\d+)?\s?(?:k|/mo|/month|per month|a month)?", t, re.I)
    ap=[x.strip() for x in ap if re.search(r"\d", x)]
    if truth["prices"] and ap:
        def _nums(s): return set(re.findall(r"\d[\d,]*", s.replace(",","")))
        site_nums=set().union(*[_nums(x) for x in truth["prices"]]) if truth["prices"] else set()
        ans_nums=set().union(*[_nums(x) for x in ap]) if ap else set()
        if site_nums and ans_nums and not (site_nums & ans_nums):
            F.append({"fact":"pricing","ai_says":", ".join(dict.fromkeys(ap))[:140],"your_site":", ".join(truth["prices"])[:140],"verdict":"check"})
    return {"tool":"Rubric check_ai_facts","brand":brand or truth.get("name"),"domain":domain,
            "ground_truth":truth,"findings":F,
            "mismatches":sum(1 for f in F if f["verdict"]=="mismatch"),
            "note":"Structured-fact diff only (founding year, location, price). A mismatch means the AI answer disagrees with your own site, usually a stale-training hallucination, sometimes a fact your site never states clearly (fix the source so the AI has something to read). It compares only facts readable on both sides, so it under-reports; no findings is not proof the answer is accurate. For sentiment and framing, have a model read the answer."}

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

def ia_analysis(pages, npf, deep_threshold=3):
    """Internal-linking / information-architecture analysis over the crawled internal-link graph.
    ADVISORY (not scored): reuses each page's `links` (its normalized internal-link targets) to surface
    click-depth from the homepage, pages buried too deep, pages UNREACHABLE via internal links, high-value
    pages that are under-linked, and 'equity sink' pages (lots of inbound, almost no outbound). All computed
    from data the crawl already holds - no extra fetch. The homepage-orphan case stays in the scored
    `orphans` check; this is the richer, fix-is-free IA view most auditors don't surface. Note: the score's
    stored `depth` is URL-PATH depth, whereas `clicks` here is real click-depth (BFS from home)."""
    from collections import deque
    n = len(pages)
    idxof = {npf(p.get("path")): i for i, p in enumerate(pages)}
    outdeg = [0] * n; indeg = [0] * n; adj = [[] for _ in range(n)]
    for i, p in enumerate(pages):
        seen = set()
        for t in (p.get("links") or []):
            j = idxof.get(t)
            if j is not None and j != i and j not in seen:
                seen.add(j); adj[i].append(j); outdeg[i] += 1; indeg[j] += 1
    home = [i for i, p in enumerate(pages) if npf(p.get("path")) in ("", "/")]
    depth = [None] * n; dq = deque(home)
    for h in home: depth[h] = 0
    while dq:
        i = dq.popleft()
        for j in adj[i]:
            if depth[j] is None: depth[j] = depth[i] + 1; dq.append(j)
    _P = lambda p: p.get("path") or "/"
    _root = lambda p: npf(p.get("path")) in ("", "/")
    home_found = bool(home)
    deep = sorted(({"path": _P(p), "clicks": depth[i], "score": p.get("score")}
                   for i, p in enumerate(pages) if depth[i] is not None and depth[i] > deep_threshold),
                  key=lambda x: -x["clicks"])
    unreachable = ([{"path": _P(p), "score": p.get("score"), "inbound": indeg[i]}
                    for i, p in enumerate(pages) if depth[i] is None and not _root(p)] if home_found else [])
    equity_sinks = sorted(({"path": _P(p), "inbound": indeg[i], "outbound": outdeg[i]}
                           for i, p in enumerate(pages) if indeg[i] >= 5 and outdeg[i] <= 1 and not _root(p)),
                          key=lambda x: -x["inbound"])
    underlinked = sorted(({"path": _P(p), "score": p.get("score"), "inbound": indeg[i]}
                          for i, p in enumerate(pages)
                          if (p.get("score") or 0) >= 75 and indeg[i] <= 2 and not _root(p)),
                         key=lambda x: (x["inbound"], -(x.get("score") or 0)))
    inbs = sorted(indeg)
    return {"n": n, "home_found": home_found,
            "median_inbound": (inbs[len(inbs) // 2] if inbs else 0),
            "avg_outbound": (round(sum(outdeg) / n, 1) if n else 0),
            "max_depth": max([d for d in depth if d is not None] or [0]),
            "deep_threshold": deep_threshold,
            "deep": deep[:60], "deep_n": len(deep),
            "unreachable": unreachable[:60], "unreachable_n": len(unreachable),
            "equity_sinks": equity_sinks[:60], "equity_sinks_n": len(equity_sinks),
            "underlinked": underlinked[:60], "underlinked_n": len(underlinked)}

def build(domain, origin, pages, sitecx, sitemap_paths=None, linkstatus=None, client=None, intro=None, protocols=None, aicrawler=None, site_type_override=None, agency=None, logo=None):
    ok200=[p for p in pages if p.get("status")==200]
    site_type=(site_type_override if site_type_override in SITE_PROFILE_LABEL else classify_site(pages)); prof=PROFILES.get(site_type)   # override or auto-detect; None profile = general / no-op
    site_type_source="override" if site_type_override in SITE_PROFILE_LABEL else "auto"
    def _np(x): return (x or "/").rstrip("/") or "/"
    # site-level: does the site publish comparison / best-of content (a top AI-cited format)? Self-published
    # comparisons that feature the own brand still earn citations but rarely the RECOMMENDATION (Ahrefs 2026),
    # so surface how many self-brand and point the fix at third-party roundups.
    _cmp=[p for p in pages if CMP_RE.search((p.get("path") or "")+" "+(p.get("title") or ""))]
    _selfcmp=[p for p in _cmp if p.get("self_comparison")]
    if not _cmp:
        _cc=chk("comparison","warn","no comparison / best-of content found")
    elif _selfcmp:
        _cc=chk("comparison","good",f"{len(_cmp)} comparison/best-of page(s), {len(_selfcmp)} self-branded (cited, not recommended)")
    else:
        _cc=chk("comparison","good",f"{len(_cmp)} comparison/best-of page(s)")
    _cc["urls"]=[p.get("url") for p in _cmp if p.get("url")]
    _cc["self_urls"]=[p.get("url") for p in _selfcmp if p.get("url")]
    sitecx=list(sitecx)+[_cc]
    scx={c["id"]:c["status"] for c in sitecx}
    # per-page injected checks (need the whole page set): orphan/missing-from-sitemap + duplicate title/meta
    _linked=set()
    for p in pages: _linked|=set(p.get("links") or [])
    # duplicate / near-duplicate only matter among INDEXABLE pages: a noindexed page is out of the index, so
    # it cannot clash with an indexable one. Exclude noindexed pages from the fingerprints so that once a
    # re-crawl sees the noindex the pair stops being flagged (and the noindexed page reports 'na', not a dupe).
    _idx=lambda p: next((c["status"] for c in p.get("checks",[]) if c["id"]=="noindex"),"good")=="good"
    _idxpages=[p for p in ok200 if _idx(p)]
    _titles=Counter(t for t in ((p.get("title") or "").strip().lower() for p in _idxpages) if t)
    _metas=Counter(m for m in ((p.get("meta") or "").strip().lower() for p in _idxpages) if m)
    # near-duplicate fingerprints: exact (md5) + near (simhash Hamming <= 3)
    _bychash=defaultdict(list)
    for p in _idxpages:
        if p.get("chash"): _bychash[p["chash"]].append(p.get("url"))
    _near=defaultdict(list); _simp=[p for p in _idxpages if p.get("simhash")]
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
        if not _idx(p): p["checks"].append(chk("duplicate","na","noindexed - out of the index"))
        elif _tt and _titles[_tt]>1: p["checks"].append(chk("duplicate","bad",f"title shared with {_titles[_tt]-1} other page(s)"))
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
        if not _idx(p): p["checks"].append(chk("nearduplicate","na","noindexed - out of the index"))
        elif p.get("status")==200 and _ex: p["checks"].append(chk("nearduplicate","bad",f"exact duplicate of {len(_ex)} page(s)"))
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
                "protocols":protocols or {},"signals":_agsignals,"money_n":len(_money),"money_urls":[p.get("url") for p in _money][:120],
                "webmcp_n":sum(1 for p in ok200 if (p.get("agent") or {}).get("webmcp")),
                "webmcp_urls":[p.get("url") for p in ok200 if (p.get("agent") or {}).get("webmcp")][:40]}
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
    _igfigs=sorted(x["figures"] for x in _igp)
    infogain={"pages":_igp,"bands":{b:sum(1 for x in _igp if x["band"]==b) for b in ("high","medium","low")},"total":len(ok200),
              "atbar":sum(1 for x in _igp if x["figures"]>=15),"median_figs":(_igfigs[len(_igfigs)//2] if _igfigs else 0)}
    # decay-RISK aggregate: per-page freshness age -> fresh/aging/stale/undated (evergreen non-articles exempt = 'na').
    # HONEST LABEL: this is decay-RISK (a leading indicator from staleness), NOT measured citation decay, and a missing
    # citation can also be a source conflict (Dias/MemToC), so copy says "at risk / worth refreshing", never "why you lost it".
    _dc=Counter(); _at_risk=[]
    for p in ok200:
        _m=p.get("metrics") or {}; _b=_m.get("decay")
        if not _b: continue
        _dc[_b]+=1
        if _b in ("stale","undated"):
            _at_risk.append({"url":p.get("url") or p.get("path"),"decay":_b,"age_days":_m.get("age_days"),"updated":_m.get("updated")})
    _at_risk.sort(key=lambda r:(r["decay"]!="undated", -((r.get("age_days") or 0))))   # undated first, then oldest stale
    decay={"fresh":_dc.get("fresh",0),"aging":_dc.get("aging",0),"stale":_dc.get("stale",0),
           "undated":_dc.get("undated",0),"na":_dc.get("na",0),
           "dated":_dc.get("fresh",0)+_dc.get("aging",0)+_dc.get("stale",0),"at_risk":_at_risk[:60]}
    # Content-citability text signals (advisory, NOT scored): aggregate the per-page hedging / naked-claim /
    # generic-heading / unresolved-passage / over-breadth signals into worst-offender lists (SIGIR + AirOps 2026).
    _cq_hedge=[]; _cq_naked=[]; _cq_generic=[]; _cq_unres=[]; _cq_broad=[]; _cq_schema=[]; _cq_pricejs=[]; _cq_over=[]
    _money_urls=set(p.get("url") for p in _money); _neg_money=0; _n_money_comm=0
    _content_pages=0; _orig_pages=0; _n_commercial=0; _n_informational=0
    for p in ok200:
        _m=p.get("metrics") or {}; _u=p.get("url") or p.get("path"); _ty=p.get("type")
        _iscomm=(_ty in ("product","service") or _u in _money_urls)
        if _iscomm:
            _n_money_comm+=1; _n_commercial+=1
            if _m.get("price_rend") and not _m.get("price_raw"):
                _cq_pricejs.append({"url":_u})   # price visible only after JS render -> invisible to non-rendering AI crawlers (SIGIR: price is a citation gatekeeper)
            if _m.get("neg_qual"): _neg_money+=1
        elif _ty in ("article","page","profile"): _n_informational+=1
        if _ty in ("article","page","profile","listing"):
            _content_pages+=1
            if _m.get("orig_asset"): _orig_pages+=1
        if _m.get("oversized"): _cq_over.append({"url":_u,"tokens":_m.get("max_tok")})
        if (_m.get("sch_vague") or 0)>=1:
            _cq_schema.append({"url":_u,"n":_m.get("sch_vague"),"items":(p.get("sch_vague_items") or [])[:5]})
        if (_m.get("claims") or 0)>=3 and (_m.get("hedge_ratio") or 0)>=0.4:
            _cq_hedge.append({"url":_u,"ratio":_m.get("hedge_ratio"),"hedged":_m.get("hedged_claims"),"claims":_m.get("claims")})
        if (_m.get("naked_claims") or 0)>=2:
            _cq_naked.append({"url":_u,"n":_m.get("naked_claims")})
        if (_m.get("generic_headings") or 0)>=1:
            _cq_generic.append({"url":_u,"n":_m.get("generic_headings")})
        if (_m.get("unresolved_passages") or 0)>=2:
            _cq_unres.append({"url":_u,"n":_m.get("unresolved_passages")})
        if p.get("type")=="article" and (_m.get("subtopics") or 0)>=12 and (_m.get("words") or 0)>=2000:
            _cq_broad.append({"url":_u,"sections":_m.get("subtopics"),"words":_m.get("words")})   # over-breadth only on ARTICLES (a long landing page with many sections is not a sprawling guide)
    _cq_hedge.sort(key=lambda r:-(r.get("ratio") or 0)); _cq_naked.sort(key=lambda r:-(r.get("n") or 0))
    _cq_generic.sort(key=lambda r:-(r.get("n") or 0)); _cq_unres.sort(key=lambda r:-(r.get("n") or 0))
    _cq_broad.sort(key=lambda r:-(r.get("sections") or 0)); _cq_schema.sort(key=lambda r:-(r.get("n") or 0)); _cq_over.sort(key=lambda r:-(r.get("tokens") or 0))
    content_quality={"hedge":_cq_hedge[:30],"naked":_cq_naked[:30],"generic":_cq_generic[:30],
                     "unresolved":_cq_unres[:30],"broad":_cq_broad[:20],"schema":_cq_schema[:30],"pricejs":_cq_pricejs[:30],"over":_cq_over[:20],
                     "n_hedge":len(_cq_hedge),"n_naked":len(_cq_naked),"n_generic":len(_cq_generic),
                     "n_unresolved":len(_cq_unres),"n_broad":len(_cq_broad),"n_schema":len(_cq_schema),
                     "n_pricejs":len(_cq_pricejs),"n_over":len(_cq_over),"money_pages":_n_money_comm,"neg_qual_money":_neg_money,
                     "content_pages":_content_pages,"orig_pages":_orig_pages,"n_commercial":_n_commercial,"n_informational":_n_informational,"pages":len(ok200)}
    followup=followup_coverage(ok200)
    mktplat=market_platforms(domain, ok200)
    # Entity anchor (Known pillar): a Wikidata QID in sameAs is a language-independent authority anchor most LLMs
    # hold in training; and hreflang served only after JS render is invisible to non-rendering AI crawlers.
    _sameas_all=set(); _hl_raw_tot=0; _hl_rend_tot=0
    for p in ok200:
        for s in (p.get("sameas") or []): _sameas_all.add(s)
        _mm=p.get("metrics") or {}; _hl_raw_tot+=(_mm.get("hreflang_raw") or 0); _hl_rend_tot+=(_mm.get("hreflang_rend") or 0)
    _wd=[s for s in _sameas_all if re.search(r"wikidata\.org/(wiki|entity)/q\d", (s or "").lower())]
    entity_anchor={"wikidata":bool(_wd),"wikidata_url":(_wd[0] if _wd else None),
                   "sameas_n":len(_sameas_all),"hreflang":bool(_hl_rend_tot),
                   "hreflang_jsonly":bool(_hl_rend_tot and not _hl_raw_tot)}
    # link graph for the Site-map visualisation: nodes = crawled pages (score/depth/orphan), edges = internal links.
    _idxof={_np(p.get("path")):i for i,p in enumerate(ok200)}
    _indeg=[0]*len(ok200); _lgedges=[]
    for i,p in enumerate(ok200):
        for t in (p.get("links") or []):
            j=_idxof.get(t)
            if j is not None and j!=i: _lgedges.append([i,j]); _indeg[j]+=1
    ia=ia_analysis(ok200,_np)   # internal-linking / IA advisory - MUST run before `links` is popped below
    _ulset=set(x.get("path") for x in ia.get("underlinked",[]))
    _lgnodes=[{"p":(p.get("path") or "/"),"s":p.get("score"),"d":p.get("depth") or 0,"ind":_indeg[i],
               "o":(_np(p.get("path")) not in _linked and _np(p.get("path")) not in ("","/")),
               "ul":((p.get("path") or "/") in _ulset)} for i,p in enumerate(ok200)]
    linkgraph={"nodes":_lgnodes[:250],"edges":[e for e in _lgedges if e[0]<250 and e[1]<250][:1400],"capped":len(ok200)>250}
    # Proposals for the report cards, computed HERE while per-page outlinks + question headings still exist (the
    # next loop strips them). propose_* crawl and query NOTHING -- they only read what the crawl already fetched.
    try: _proposed_competitor=propose_competitor(pages, origin, site_type)
    except Exception: _proposed_competitor={"mode":"benchmark","source":"benchmark"}
    try: _proposed_queries=propose_queries(pages, domain, site_type, SITE_PROFILE_LABEL.get(site_type,site_type))
    except Exception: _proposed_queries=[]
    for p in pages:
        for _k in ("outlinks","q_headings","_broken","simhash","chash","links","sameas","agent","infogain"): p.pop(_k,None)
    _ARCH=lambda p:(p.get("type") or "")=="archive"      # tag/taxonomy archives: detected but excluded from the citability score
    archives=[p for p in pages if _ARCH(p)]
    content=[p for p in pages if not _ARCH(p)]
    ok=[p for p in ok200 if not _ARCH(p)]                # 200-OK content pages feed the score + action plan
    def avg(f): return round(sum(f(p) for p in ok)/len(ok)) if ok else 0
    overall=avg(lambda p:p["score"])
    pill={pl:avg(lambda p:p["pillars"][pl]) for pl in PILLARS}
    eng={e:avg(lambda p:p["engines"][e]) for e in ENGINE_WEIGHTS}
    # access gate: if AI bots are blocked (robots) or WAF-blocked (reachability), the whole site is
    # uncitable regardless of page quality -> cap the headline so a block visibly tanks the score.
    _scx2={c["id"]:c["status"] for c in sitecx}
    access_blocked = _scx2.get("robots")=="bad" or _scx2.get("reachability")=="bad"
    _ov_unc=overall; _eng_unc=dict(eng)          # UNLOCKED baseline (pre access-gate cap) the action plan projects against
    if access_blocked:
        overall=min(overall,25); pill["Findable"]=min(pill.get("Findable",0),25)
        eng={e:min(v,25) for e,v in eng.items()}
    # ---- issues aggregated (+ pillar/chapter/evidence/effort) ----
    agg=defaultdict(lambda:{"warn":[],"bad":[]})
    for p in content:
        for c in p["checks"]:
            if c["status"] in ("warn","bad"): agg[c["id"]]["bad" if c["status"]=="bad" else "warn"].append(p["url"])
    for c in sitecx:
        if c["status"] in ("warn","bad"): agg[c["id"]]["bad" if c["status"]=="bad" else "warn"].append(origin)
    issues=[]
    for cid,d in agg.items():
        m=CHECK_META[cid]; sev="bad" if d["bad"] else "warn"
        issues.append({"id":cid,"label":m["label"],"pillar":m["pillar"],"ch":m["ch"],"ev":m["ev"],
                       "effort":m["effort"],"phase":m["phase"],"fix":FIX.get(cid,""),"fix_deep":FIX_DEEP.get(cid,""),
                       "owner":CHECK_OWNER.get(cid,"content"),"owner2":CHECK_OWNER_2.get(cid),
                       "severity":sev,"bad":d["bad"],"warn":d["warn"],"count":len(d["bad"])+len(d["warn"])})
    # ---- ACTION PLAN: simulate fixing each issue, measure projected gain ----
    base=overall; base_eng=eng                              # capped headline = "today"
    # gains project against the UNLOCKED baseline: the action plan is a SEQUENCE and item 1 (fixing the
    # robots/WAF block) lifts the gate, so every later fix is scored as if the site is already unblocked.
    # The gate fix itself shows the unlock jump (today -> unlocked). Without this a blocked site showed the
    # gate fix +50 and all the others +0, which reads as "fix robots and nothing else matters".
    gbase = _ov_unc if access_blocked else overall
    gbase_eng = _eng_unc if access_blocked else eng
    for it in issues:
        cid=it["id"]; affected=set(it["bad"])|set(it["warn"])
        site_fix = cid in SITE_IDS
        sim=[]
        for p in ok:
            st=dict(p["cs"])
            if site_fix: st[cid]="good"
            elif p["url"] in affected: st[cid]="good"
            o,_,en2=page_scores(st, prof); sim.append((o,en2))
        if access_blocked and cid in ("robots","reachability"):
            it["gain_overall"]=gbase-base                   # unlock jump: today -> unlocked baseline
            eg={e:gbase_eng[e]-base_eng[e] for e in ENGINE_WEIGHTS}
        else:
            _so=round(sum(s[0] for s in sim)/len(sim)) if sim else gbase
            _se={e:(round(sum(s[1][e] for s in sim)/len(sim)) if sim else gbase_eng[e]) for e in ENGINE_WEIGHTS}
            it["gain_overall"]=_so-gbase                    # real per-fix increment vs the (unlocked) baseline
            eg={e:_se[e]-gbase_eng[e] for e in ENGINE_WEIGHTS}
        it["gain_engines"]=eg
        te=max(eg.items(),key=lambda x:x[1]) if eg else ("",0)
        it["top_engine"]=te[0]; it["top_engine_gain"]=te[1]
    # realistic "if you clear the plan" projection. The all-checks-good re-score is ~100 for ANY site - a
    # vanity ceiling no real site reaches (even a heavily-optimised reference site tops out ~89), and a
    # knowledgeable reader discounts a "-> 100" claim. So drive the headline from the summed per-fix ENGINE
    # gains (consistent with the per-engine bars shown right below it) and cap it at a believable ceiling,
    # never below today's score.
    # projection = the "if you clear the plan" score: re-score every page with all its flagged checks good.
    # A page with every check good scores 100, so proj_all tops out at 100 - a perfect site scores 100 and
    # the projection never exceeds it. (All-good implies the robots block is lifted too, so a blocked site's
    # projection is the true unblocked ceiling; the gate fix's own gain in the plan reflects that unlock.)
    # Projection = today + the summed per-fix gains: the honest, believable number, and the SAME value the
    # per-engine bars, the effort panel and the roadmap all sum to. (The all-checks-good re-score tops out at a
    # vanity ~100 no real site reaches, so it is never the headline - the note above always intended this.)
    proj_all = min(100, overall + sum(max(0, it.get("gain_overall", 0)) for it in issues))
    # Rank by score movement PER HOUR OF WORK (which the report copy already promises) = gain / effort-hours,
    # then severity, then raw gain, then pages. This surfaces a low-effort high-impact fix (e.g. the H1-snippet
    # window) ahead of a bigger-but-slower one, and it is the SAME order the roadmap uses below, so the action
    # plan and the roadmap can never disagree.
    _EFFORT_HRS = {"Low": 1, "Med": 2, "High": 3}
    issues.sort(key=lambda x:(-(max(0,x["gain_overall"])/_EFFORT_HRS.get(x.get("effort"),2)), x["severity"]!="bad", -x["gain_overall"], -x["count"]))
    # Roadmap phases = that same priority ranking split into thirds (Days 0-30 / 30-60 / 60-90). Carries EVERY
    # actionable fix (gain>0 or an error), so clearing the whole plan reaches the projected score above, and the
    # timeline can never contradict the action-plan order because it IS the action-plan order.
    _act = [it for it in issues if it["gain_overall"]>0 or it["severity"]=="bad"]
    plan_phases={1:[],2:[],3:[]}
    _n = len(_act) or 1
    for _idx, it in enumerate(_act):
        plan_phases[1 if _idx < _n/3.0 else (2 if _idx < 2*_n/3.0 else 3)].append(it["id"])
    # Fan-out readiness: for each of the 10 fan-out types, how many supporting checks the site wins vs fails.
    # Entity + Comparison lead (they drove ~97% of brand mentions). "won" = the check is not a failing issue.
    _failing = {it["id"] for it in issues}
    fanout = []
    for _t, _cids in FANOUT_CHECKS.items():
        _c = [c for c in _cids if c in CHECK_META]
        _gaps = [c for c in _c if c in _failing]
        fanout.append({"type": _t, "won": len(_c) - len(_gaps), "total": len(_c),
                       "key": _t in FANOUT_KEY, "gaps": [CHECK_META[c]["label"] for c in _gaps][:3]})
    fanout.sort(key=lambda f: (not f["key"], f["won"] / (f["total"] or 1)))
    tot=Counter()
    for p in content:
        for c in p["checks"]:
            if c["status"] not in ("na","info"): tot[c["status"]]+=1
    _cb = finalize_blocks(content)   # per-passage citability rollup: sets each page's 'blocks' + returns site top/weakest
    return {"tool":"Rubric","citability_blocks":_cb,"domain":domain,"origin":origin,
            "generated":datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "date":datetime.date.today().isoformat(),
            "pages_crawled":len(content),"archive_n":len(archives),
            "archive_pages":[{"url":p.get("url"),"score":p.get("score")} for p in archives][:200],"overall":overall,"pillars":pill,"engines":eng,
            "engine_note":ENGINE_NOTE,"engine_weights":ENGINE_WEIGHTS,"check_meta":CHECK_META,
            "crawler_blindspots":{"tools":STD_AUDIT_TOOLS,"checked":STD_AUDIT_CHECKED,
                "cant_see":[{"label":CHECK_META[_c]["label"]} for _c in NOT_IN_STANDARD_AUDIT if _c in CHECK_META],
                "not_weighted":[{"label":CHECK_META[_c]["label"],"note":_n} for _c,_n in STD_AUDIT_NOTES.items() if _c in CHECK_META],
                "total_scored":sum(1 for _c in CHECK_META if CHECK_META[_c].get("phase")!=0)},
            "grok_advisory":GROK_ADVISORY,
            "totals":dict(tot),"issues":issues,"site_checks":sitecx,"broken_links":broken_links,"sitemap":sitemap,"linkgraph":linkgraph,"ia":ia,"offpage":offpage,"agentready":agentready,"infogain":infogain,"fanout":fanout,"decay":decay,"content_quality":content_quality,"followup":followup,"entity_anchor":entity_anchor,"market_platforms":mktplat,"aicrawler":aicrawler or {},"proj_all":proj_all,
            "client":client,"intro":intro,"agency":(agency or "GoGoChimp"),"logo":logo,"access_blocked":access_blocked,
            "types":dict(Counter(p["type"] for p in content)),
            "site_type":site_type,"site_type_label":SITE_PROFILE_LABEL.get(site_type,site_type),"site_type_source":site_type_source,
            "proposed_competitor":_proposed_competitor,"proposed_queries":_proposed_queries,
            "profile":(prof or {}),"profile_up":sorted([c for c,m in (prof or {}).items() if m>1]),
            "profile_down":sorted([c for c,m in (prof or {}).items() if m<1]),
            "redirect_home":[{"url":p["url"],"to":p.get("redirect_home")} for p in content if p.get("redirect_home")],
            "plan_phases":plan_phases,"pages":content}

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
    """Parse 'url,citations' rows (Bing WMT AI Performance export) -> {canon_key(url): float}.
    Rows whose URLs collapse to the same canonical key (scheme, www, trailing slash, tracking params)
    are summed, not overwritten."""
    cites={}
    for row in csv.reader(text.splitlines()):
        if len(row)<2: continue
        try: val=float(str(row[1]).replace(",","").strip())
        except ValueError: continue
        k=canon_key(row[0]); cites[k]=cites.get(k,0.0)+val
    return cites

def calibrate_data(d, cites):
    """Spearman-correlate the report's scores vs real per-URL citations. Returns a dict for UI/CLI."""
    rows=[p for p in d["pages"] if canon_key(p["url"]) in cites]
    if len(rows)<8:
        return {"error":f"Only {len(rows)} of {len(d['pages'])} crawled pages matched the citations file (need at least 8 for a stable correlation).","matched":len(rows)}
    y=[cites[canon_key(p["url"])] for p in rows]
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
    print(f"\n=== Rubric calibration vs {r['matched']} pages with real citations ===")
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
    from cited_logo_data import CITED_LOGO_DATAURI, CITED_LOGO_DARK, CITED_FAVICON  # ring lockup (light), print variant (black text), favicon symbol
except Exception:
    CITED_LOGO_DATAURI = CITED_LOGO_DARK = CITED_FAVICON = ""
try:
    from cited_fonts import FONT_FACE_CSS                # embedded Archivo + IBM Plex Mono (no network / Google-Fonts / CSP dependency)
except Exception:
    FONT_FACE_CSS = ""
# Rubric mark: red "R" on a warm-dark rounded square (matches the web app favicon and the header lockup).
FAVICON = "data:image/svg+xml;base64," + base64.b64encode(
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='18' fill='#db0632'/><path fill-rule='evenodd' d='M0 0 H61.8 A35 35 0 0 1 74.8 67.5 L96.6 100 H68.6 Z M30.5 23 H55.3 A12.5 12.5 0 0 1 55.3 48 H30.5 Z' fill='#ffffff' transform='translate(21 19) scale(0.6)'/></svg>").decode()

def mcp_audit(d):
    """Reshape a completed audit's data dict into the compact JSON the hosted MCP serves (get_audit /
    get_action_plan / get_affected_pages / get_page). Read-only reshape - the deterministic scores were already
    computed centrally by the engine (the host model never scores). The worker uploads this per crawl so the MCP
    can read decision-ready payloads without the heavy local JSON."""
    def _ek(k): return k.lower().replace(" ", "_")           # "AI Overviews" -> "ai_overviews"
    P = d.get("pillars") or {}; E = d.get("engines") or {}; T = d.get("totals") or {}
    issues = d.get("issues") or []; pages = d.get("pages") or []; cm = d.get("check_meta") or {}
    by = {}
    for p in pages:
        t = p.get("type") or "page"; b = by.setdefault(t, [0, 0]); b[0] += 1
        if (p.get("score") or 0) >= 70: b[1] += 1
    pages_by_type = [{"type": t, "count": n, "pct_quotable": round(100 * q / n) if n else 0}
                     for t, (n, q) in sorted(by.items(), key=lambda x: -x[1][0])]
    bots = {b.get("name"): b.get("status") for b in ((d.get("aicrawler") or {}).get("bots") or [])}
    weakest = min(("Known", "Findable", "Trusted"), key=lambda k: P.get(k, 100)) if P else None
    ptype = {p.get("url"): (p.get("type") or "page") for p in pages}
    pchecks = {p.get("url"): {c.get("id"): (c.get("detail") or "") for c in (p.get("checks") or [])} for p in pages}
    def fx(i, rank):
        cid = i.get("id"); urls = list(i.get("bad", [])) + list(i.get("warn", []))
        n = i.get("count"); n = len(urls) if n is None else n
        ge = {_ek(k): v for k, v in (i.get("gain_engines") or {}).items() if v}
        return {"rank": rank, "check_id": cid, "title": i.get("label"), "pillar": i.get("pillar"),
                "severity": i.get("severity"), "effort": i.get("effort"), "pages_affected": n,
                "owner": i.get("owner"), "owner_secondary": i.get("owner2"),
                "gain_overall": i.get("gain_overall"), "gain_by_engine": ge, "instruction": i.get("fix") or "",
                "source_ref": i.get("ch"), "template_level": bool(n and n >= 3),
                "affected": [{"url": u, "page_type": ptype.get(u, "page"),
                              "current_value": pchecks.get(u, {}).get(cid, ""),
                              "expected": (cm.get(cid) or {}).get("ev") or ""} for u in urls[:100]]}
    proj = min(100, (d.get("overall") or 0) + sum((i.get("gain_overall") or 0) for i in issues))
    def pg(p):
        return {"url": p.get("url"), "page_type": p.get("type") or "page", "score": p.get("score"),
                "engines": {_ek(k): v for k, v in (p.get("engines") or {}).items()},
                "checks": [{"check_id": c.get("id"), "title": c.get("label"), "status": c.get("status"),
                            "detail": c.get("detail") or ""}
                           for c in (p.get("checks") or []) if c.get("status") not in ("na", "info")]}
    return {
        "domain": d.get("domain"), "pages_crawled": d.get("pages_crawled"), "created_at": d.get("date"),
        "score": d.get("overall"), "quotable_threshold": 70,
        "pillars": {"known": P.get("Known"), "findable": P.get("Findable"), "trusted": P.get("Trusted"),
                    "weakest": (_ek(weakest) if weakest else None)},
        "engines": {"chatgpt": E.get("ChatGPT"), "perplexity": E.get("Perplexity"), "ai_overviews": E.get("AI Overviews"),
                    "gemini": E.get("Gemini"), "copilot": E.get("Copilot"), "claude": E.get("Claude")},
        "counts": {"errors": T.get("bad"), "warnings": T.get("warn"), "passed": T.get("good"),
                   "checks_total": (T.get("bad") or 0) + (T.get("warn") or 0) + (T.get("good") or 0)},
        "pages_by_type": pages_by_type,
        "reachability": {"gptbot": bots.get("GPTBot"), "perplexitybot": bots.get("PerplexityBot"),
                         "google_extended": bots.get("Google-Extended"),
                         "notes": "A blocked bot means that engine cannot fetch or cite the site."},
        "website_type": {"detected": d.get("site_type_label") or d.get("site_type"),
                         "weighted_up": d.get("profile_up") or [], "weighted_down": d.get("profile_down") or []},
        "total_fixes": len(issues), "projected_score_if_all_applied": proj,
        "action_plan": [fx(i, n + 1) for n, i in enumerate(issues)],
        "pages": [pg(p) for p in pages],
        "caveats": ["current_value is the page-level finding text; expected is the check's guidance, not a literal target string.",
                    "template_level is inferred from pages_affected >= 3, not declared by the check."],
    }


# Compact "Proof loop" panel for the audit report (secondary surface; the full render is the web Proof view).
# `proof` is a site_proof.summary dict plus the row's own "tier" (the summary does not repeat it) and, optionally,
# "computed_at". It is the LAST-KNOWN proof, computed before this crawl (the report is written during the crawl, the
# proof is recomputed after it), so the panel says so and always points at the live Proof view. The honesty labels are
# read FROM the summary; the fallback wording is the same text the engine stamps (proof_loop.LABELS) and only covers a
# summary somehow missing them, so a label can never silently go missing. No per-site numbers are shown here: the
# figures live in the (Pro-gated) Proof view, never in the report page.
_PROOF_LABEL_FALLBACK = {"source": "Bing/Copilot data only", "causation": "correlation, not proof of cause"}
_PROOF_HEADLINES = {
    "empty": "No citation data yet. Upload your Bing AI Performance export to measure what your fixes change.",
    "cold_start": "Your Bing citations are matched to your crawl. Fix a page that is cited, then re-crawl, to start your proof.",
    "waiting": "Fixes detected, waiting for enough data to measure them.",
}
_PROOF_HEADLINE_DEFAULT = "See what your fixes changed in your Bing/Copilot citations."
_PROOF_TIER_B_LABEL = "overall change since your first audit"   # spec section 9 wording; the summary's tier_b.label wins when present

def _proof_headline(proof):
    """The one plain line per tier. Two tiers need the summary, so they cannot overclaim:
    tier_b carries the spec's exact label (summary tier_b.label, never a paraphrase) and is never a per-fix claim;
    tier_a only says "measured against pages you did not touch" when an event positively shows a SUFFICIENT untouched
    control (compare.untouched present and not flagged insufficient). A missing control or one the summary itself
    calls insufficient gets the plain dated before-and-after line, so the report never asserts a fair control it lacks."""
    tier = proof.get("tier")
    if tier == "tier_b":
        tb = proof.get("tier_b") if isinstance(proof.get("tier_b"), dict) else {}
        label = str(tb.get("label") or _PROOF_TIER_B_LABEL).strip().rstrip(".")
        return f"Whole site: {label}. This is not the effect of any single fix."
    if tier == "tier_a":
        events = proof.get("events") if isinstance(proof.get("events"), list) else []
        def _cmp(ev): return (ev.get("compare") if isinstance(ev, dict) else None) or {}
        def _ctl(ev): return _cmp(ev).get("untouched") if isinstance(_cmp(ev).get("untouched"), dict) else None
        shown = [ev for ev in events if _cmp(ev).get("shown")]
        controlled = bool(shown) and all(_ctl(ev) is not None and not _ctl(ev).get("insufficient") for ev in shown)
        return ("Your fixes have a dated before-and-after result, measured against pages you did not touch." if controlled
                else "Your fixes have a dated before-and-after result.")
    return _PROOF_HEADLINES.get(tier, _PROOF_HEADLINE_DEFAULT)

def _proof_panel_html(proof, proof_url=None):
    """Server-rendered compact proof panel, or "" when there is no proof (the report is then byte-for-byte unchanged)."""
    if not proof or not isinstance(proof, dict): return ""
    e = H.escape
    labels = proof.get("labels") if isinstance(proof.get("labels"), dict) else {}
    chips = [labels.get("source") or _PROOF_LABEL_FALLBACK["source"],
             labels.get("causation") or _PROOF_LABEL_FALLBACK["causation"]]
    headline = _proof_headline(proof)
    w = proof.get("waiting") if isinstance(proof.get("waiting"), dict) else {}
    def _n(v):
        try: return max(0, int(v or 0))
        except (TypeError, ValueError): return 0
    prov, awaiting = _n(w.get("provisional_count")), _n(w.get("awaiting_period"))
    wait = []
    if prov: wait.append(f"{prov} {'fix' if prov == 1 else 'fixes'} detected, awaiting the confirming crawl.")
    if awaiting: wait.append(f"{awaiting} confirmed {'fix' if awaiting == 1 else 'fixes'} cannot be measured yet.")
    when = ""
    try: when = datetime.date.fromisoformat(str(proof.get("computed_at") or "")[:10]).strftime("%d %b %Y").lstrip("0")
    except ValueError: pass
    MM = "font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81"
    link = ""
    if isinstance(proof_url, str) and proof_url.startswith(("https://", "http://", "/")):
        link = (f"<a href=\"{e(proof_url, quote=True)}\" style='align-self:flex-start;font-size:13px;font-weight:800;"
                f"color:#f2f0e4;border:1px solid #2a2a24;padding:7px 12px'>See the full proof &rarr;</a>")
    return (
        "<div class='wrap' style='padding-top:0'><section class='proof-panel' aria-label='Proof loop' "
        "style='background:#191914;border:1px solid #2a2a24;padding:18px 22px;display:flex;flex-direction:column;gap:10px'>"
        "<div style='display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap'>"
        "<div style='font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#f2f0e4'>Proof loop</div>"
        + (f"<div style=\"{MM}\">Last updated {e(when)}. Open the full proof for the current state.</div>" if when else "")
        + "</div>"
        f"<div style='font-size:15px;font-weight:700;line-height:1.45;color:#f2f0e4;max-width:880px'>{e(headline)}</div>"
        + (f"<div style='font-size:13px;line-height:1.5;color:#a8a495'>{e(' '.join(wait))}</div>" if wait else "")
        + "<div style='display:flex;flex-wrap:wrap;gap:8px;font-family:\"IBM Plex Mono\",ui-monospace,monospace;font-size:12.5px;font-weight:600'>"
        + "".join(f"<span style='color:#a8a495;border:1px solid #2a2a24;padding:5px 10px'>{e(str(c))}</span>" for c in chips)
        + "</div>" + link + "</section></div>")

def write_html(d, path, anon=False):
    if anon:
        # The teaser is retired. Anonymous users land in the FULL 25-page report - the real product, every
        # tab, all pages, honesty footer. Registration sells SCALE (crawl up to 500) and TOOLS (MCP, export,
        # saved reports), never the verdict. We only add flags the report uses to show the sticky "more pages"
        # bar, the greyed locked rows below the 25, and to gate exports behind sign-up.
        d = dict(d)
        d["_anon"] = True
        _sm = d.get("sitemap") or {}
        _crawled = d.get("pages_crawled") or len(d.get("pages") or [])
        _total = d.get("total_discovered") or _crawled
        # "N more pages" = the pages we KNOW exist but didn't crawl. On a shallow 25-page anon crawl, crawl-time
        # link discovery only sees the frontier of those 25 pages (tiny), so the SITEMAP is the authoritative
        # site-size signal -- take whichever knows more, else a big site reads as "2 more pages".
        _sm_uncrawled = int(_sm.get("not_crawled_n") or len(_sm.get("in_sitemap_not_crawled") or []))
        d["_more_pages"] = max(0, _total - _crawled, _sm_uncrawled)
        d["_locked_pages"] = list(_sm.get("in_sitemap_not_crawled") or [])[:40]  # real page paths, greyed (not padlocks)
        d["diff"] = None    # anon users are first-time: clean "First crawl" state, not a "- since <date>" delta
    # The proof summary is rendered server-side into the compact panel only; it is NOT embedded in the page payload
    # (the full numbers live in the Pro-gated Proof view). Absent keys leave the payload byte-for-byte unchanged.
    # No panel on an anonymous report (no proof exists for anon) or a white-labelled / de-branded one (the agency's
    # client cannot open the owner-only Rubric /proof link, and it would break the de-brand): same fields the report's
    # own white-label banner and de-brand logic key off (d["client"], d["_debrand"]).
    _proof_panel="" if (anon or d.get("client") or d.get("_debrand")) else _proof_panel_html(d.get("proof"), d.get("proof_url"))
    payload=json.dumps({k:v for k,v in d.items() if k not in ("proof","proof_url")},ensure_ascii=False).replace("</","<\\/")
    css=r"""
:root{--bg:#14140f;--panel:#191914;--panel2:#1e1e18;--line:#2a2a24;--line2:#242420;--muted:#a8a495;--dim:#6b6b65;--txt:#f2f0e4;--white:#FFFFFF;
 --grn:#db0632;--grn2:#ef1a48;--deep:#db0632;--g1:#db0632;--g2:#db0632;--amber:#ff4d6d;--red:#db0632;--chip:#db0632;--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 'Archivo',-apple-system,Segoe UI,Arial,sans-serif}
a{color:var(--txt);text-decoration:none}a:hover{color:var(--grn)}
header{padding:14px 28px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:18px;flex-wrap:wrap;max-width:1616px;margin:0 auto}
.logo{display:inline-flex;align-items:center;gap:11px}
.logo .wm{display:inline-flex;align-items:baseline;gap:8px}
.logo .lw{font-family:'Archivo',sans-serif;font-weight:900;font-size:22px;letter-spacing:-.02em;color:var(--txt);line-height:1}
.logo .ls{font-family:var(--mono);font-size:12px;font-weight:400;letter-spacing:.24em;color:var(--muted);text-transform:uppercase}
header .m{color:var(--muted);font-size:14px}
.btns{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.navbtn{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:0;padding:7px 12px;font-size:13px;display:inline-flex;align-items:center;cursor:pointer}
.navbtn:hover{border-color:var(--grn);color:var(--grn)}
button{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:0;padding:7px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--grn);color:var(--grn2)}
.tabs{display:flex;flex-wrap:wrap;gap:4px;padding:0 28px;border-bottom:1px solid var(--line);background:var(--bg);position:sticky;top:0;z-index:5;max-width:1616px;margin:0 auto}
.tab{padding:14px 12px;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;font-size:14px;white-space:nowrap;display:flex;align-items:center;gap:7px}
.tab:hover{color:var(--txt)}.tab.on{color:#fff;border-bottom-color:var(--grn);font-weight:600}
.tabsep{width:1px;height:18px;background:var(--line);align-self:center;margin:0 8px}
.tabcount{font-family:var(--mono);font-size:12px;background:#262620;color:var(--grn);padding:2px 5px;border-radius:3px}
.tabcount.err{color:#ff4d6d}
.tabdd{position:relative;display:flex}
.ddmenu{display:none;position:absolute;top:100%;left:0;background:#191914;border:1px solid #2a2a24;border-radius:0;padding:6px;z-index:30;min-width:170px}
.ddi{padding:8px 12px;color:var(--muted);cursor:pointer;font-size:14px;border-radius:0;display:flex;align-items:center}
.ddi:hover{background:#242420;color:#fff}.ddi.on{color:#fff;background:#1e1e18}
.wrap{padding:28px;max-width:1616px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;margin-bottom:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:16px}
.card .n{font-size:30px;font-weight:800}.card .l{color:var(--muted);font-size:13px;margin-top:2px}
.pillcards{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:20px}
.pill3{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:16px}
.pill3 .q{color:var(--muted);font-size:13px;margin-top:2px}
.ring{--p:0;width:74px;height:74px;flex:0 0 74px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:18px;
 background:conic-gradient(var(--c) calc(var(--p)*1%),#262620 0)}.ring i{width:58px;height:58px;border-radius:50%;background:var(--panel);display:grid;place-items:center;font-style:normal}
.engs{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.eng{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:16px;display:flex;gap:14px;align-items:center;cursor:pointer}
.eng .b{font-weight:800}.eng .d{color:var(--muted);font-size:13px;margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:42px;background:#14140f}
tr:hover td{background:#242420}.sc{font-weight:800;border-radius:0;padding:2px 8px;color:#14140f;display:inline-block;min-width:30px;text-align:center}
.badge{font-size:12px;padding:1px 6px;border-radius:20px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.badge.Known{border-color:#a8a49555;color:#a8a495}.badge.Findable{border-color:#f2f0e455;color:var(--grn2)}.badge.Trusted{border-color:#ff4d6d55;color:var(--amber)}
.dot{font-weight:800}.dot.good{color:var(--grn)}.dot.warn{color:var(--amber)}.dot.bad{color:var(--red)}
.issue{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:14px 16px;margin-bottom:10px}
.issue h4{margin:0 0 4px;font-size:15px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.issue .ev{color:var(--muted);font-size:13px;margin:6px 0}.issue .fix{color:#f2f0e4;margin-top:4px}
.issue .urls{margin-top:8px;font-size:13px;color:var(--muted);max-height:160px;overflow:auto;display:none}
.issue.open .urls{display:block}
.sev{font-size:12.5px;padding:2px 8px;border-radius:20px}.sev.bad{background:#1e1e18;color:var(--red)}.sev.warn{background:#1e1e18;color:var(--amber)}
.gain{font-size:12.5px;padding:2px 8px;border-radius:20px;background:#242420;color:var(--grn2);font-weight:800}
.rank{background:var(--grn);color:#fff;font-weight:800;width:24px;height:24px;border-radius:50%;display:inline-grid;place-items:center;font-size:13px}
.phase{background:var(--panel2);border:1px solid var(--line);border-radius:0;padding:6px 14px 14px;margin-bottom:16px}
.phase h3{color:var(--grn2);margin:10px 0}
.muted{color:var(--muted)}.hide{display:none}h3{margin:18px 0 10px;font-size:15px}
.bar{height:8px;background:#262620;border-radius:0;overflow:hidden;min-width:120px}.bar i{display:block;height:100%}
input.search{background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:0;padding:7px 10px;font-size:14px;width:260px;margin-bottom:12px}
.foot{color:var(--muted);font-size:13px;padding:20px 28px 40px;border-top:1px solid var(--line);max-width:1616px;margin:0 auto}
.diffline{background:var(--panel2);border:1px solid var(--line);border-radius:0;padding:10px 14px;margin-bottom:16px;font-size:14px}
:root{--display:'Archivo',sans-serif;--ok:#f2f0e4;--warn2:#8b8b81;--err2:#db0632}
.ov{display:grid;grid-template-columns:1fr 336px;gap:22px;align-items:start}
@media(max-width:1080px){.ov{grid-template-columns:1fr}}
.ov2{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:640px){.ov2{grid-template-columns:1fr}}
.sech{font-family:var(--display);text-transform:uppercase;letter-spacing:.6px;font-size:17px;color:var(--txt);font-weight:400;margin:26px 0 12px;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.sech .s{font-family:'Archivo',sans-serif;text-transform:none;letter-spacing:0;font-size:13px;color:var(--muted);font-weight:400}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:20px}
.hero{display:flex;gap:24px;align-items:center;flex-wrap:wrap}
.sring{--p:0;--c1:var(--g1);--c2:var(--g2);width:150px;height:150px;flex:0 0 150px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--c1),var(--c2) calc(var(--p)*1%),#262620 0)}
.sring i{width:120px;height:120px;border-radius:50%;background:var(--panel);display:flex;flex-direction:column;align-items:center;justify-content:center;font-style:normal;gap:2px}
.sring .v{font-family:var(--display);font-size:52px;line-height:.85;color:var(--txt)}
.sring .o{font-size:12px;letter-spacing:1.5px;color:var(--muted)}
.htitle{font-family:var(--display);text-transform:uppercase;font-size:22px;letter-spacing:.5px}
.hsub{color:var(--muted);font-size:14px;margin:4px 0 10px;max-width:290px}
.hdelta{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:3px 12px;font-size:13px;font-weight:600}
.hc{flex:1;min-width:250px;border-left:1px solid var(--line);padding-left:24px}
.hclabel{font-family:var(--display);text-transform:uppercase;font-size:14px;letter-spacing:.6px;color:var(--muted)}
.chbar{display:flex;height:12px;border-radius:999px;overflow:hidden;gap:2px;margin:10px 0 14px}
.chstat{display:flex;gap:26px}
.chstat .n{font-family:var(--display);font-size:26px;line-height:1}
.chstat .l{font-size:12.5px;color:var(--muted)}
.trow{display:grid;grid-template-columns:170px 1fr 62px;gap:16px;align-items:center;padding:13px 0;border-bottom:1px solid var(--line)}
.trow:last-child{border-bottom:0}
.trow.eng{grid-template-columns:170px 1fr 100px 62px}
.trow .q{font-weight:800}.trow .qd,.qd{color:var(--muted);font-size:13px}
.tbar{position:relative;height:12px;background:#262620;border-radius:999px}
.tbar i{position:absolute;left:0;top:0;height:100%;border-radius:999px}
.tbar .thr{position:absolute;top:-4px;height:20px;width:2px;background:var(--muted);opacity:.55}
.tval{text-align:right;font-family:var(--display);font-size:26px;line-height:1;white-space:nowrap}
.tnote{color:var(--err2);font-size:13px;font-weight:600;text-align:right}
.d{font-size:13px;font-weight:700}.d.up{color:var(--ok)}.d.dn{color:var(--err2)}.d.z{color:var(--muted)}
.df{border:1px solid var(--line);border-radius:0;padding:18px}
.dfh{display:flex;justify-content:space-between;align-items:baseline}
.dfh .t{font-family:var(--display);text-transform:uppercase;font-size:15px;letter-spacing:.5px}
.dfrow{border-top:1px solid var(--line);padding:16px 0}.dfrow:first-of-type{border-top:0;padding-top:12px}
.dfrow:hover{background:#ffffff06}
.dfnum{width:22px;height:22px;border-radius:50%;border:1px solid var(--grn);color:var(--grn);font-size:13px;font-weight:800;display:inline-grid;place-items:center;margin-right:6px}
.dfgain{font-family:var(--display);font-size:22px;color:var(--grn);text-align:right;line-height:1}.dfgl{font-size:11px;color:var(--muted);letter-spacing:.5px}
.chip2{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:0;padding:2px 8px;font-size:12.5px;font-weight:600;margin:0 6px 4px 0}
.listbtn{border:1px solid var(--line);color:var(--txt);border-radius:0;padding:5px 12px;font-size:13px;font-weight:700;background:none;cursor:pointer}
.dffull{display:block;text-align:center;background:var(--grn);color:#fff;border-radius:0;padding:11px;font-weight:800;margin-top:14px;cursor:pointer;text-decoration:none}
.lg{border:1px dashed var(--line);border-radius:0;padding:18px;margin-top:16px}
.lgrow{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-top:1px solid var(--line)}.lgrow:first-of-type{border-top:0}
.note{border:1px dashed var(--line);border-radius:0;padding:12px 16px;margin-top:16px;color:var(--muted);font-size:13px;display:flex;align-items:center;gap:8px}
.note::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--ok);flex:0 0 8px}
.bl{display:flex;align-items:center;gap:12px;padding:6px 0}
.bl .lab{width:64px;color:var(--muted);font-size:14px}
.bl .bar2{height:8px;background:#262620;border-radius:999px;flex:1;position:relative}
.bl .bar2 i{position:absolute;left:0;top:0;height:100%;border-radius:999px;background:var(--grn)}
.bl .num{font-family:var(--display);font-size:16px;width:28px;text-align:right}
.rch{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-top:1px solid var(--line);font-size:14px}.rch:first-of-type{border-top:0}
.rst{font-size:14px}.rst.ok{color:var(--ok)}.rst.wn{color:var(--warn2)}.rst.er{color:var(--err2)}.rst::before{content:'\25CF '}
.sech,.htitle,.hclabel,.sring .v,.tval,.chstat .n,.dfgain,.bl .num,.dfh .t{font-family:'Archivo',sans-serif;font-weight:800;letter-spacing:0}
/* ===== Action Plan + Issues (mockup redesign) ===== */
.ap2{display:grid;grid-template-columns:1fr 380px;gap:24px;align-items:start}
.apmain{display:flex;flex-direction:column;gap:20px;min-width:0}
.apside{display:flex;flex-direction:column;gap:20px}
.apsum,.issum{background:#191914;border:1px solid var(--line);border-radius:0;padding:22px 26px}
.apk{font-family:var(--mono);font-size:12.5px;font-weight:400;letter-spacing:.16em;color:var(--muted);text-transform:uppercase}
.apbn{font-family:'Archivo',sans-serif;font-size:46px;font-weight:900;line-height:1;letter-spacing:-.02em}
.apintro{font-size:14px;line-height:1.6;color:#8b8b81;max-width:900px}
.aptier{display:flex;flex-direction:column;gap:12px}
.aptierh{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.aptierh h3{margin:0;font-size:16px;letter-spacing:.06em;font-family:'Archivo',sans-serif;font-weight:800}
.aptierh .sq{width:9px;height:9px;border-radius:2px;flex:none}
.aptierh .meta{font-size:13px;color:var(--muted)}
.apbox{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:2px 22px 6px}
.apbox.hot{border-color:#f2f0e447}
.apissue{border-bottom:1px solid #ffffff10}
.apissue:last-child{border-bottom:0}
.aprow{display:grid;gap:18px;align-items:center;padding:15px 0}
.aprow:hover{background:#ffffff06}
.eb{width:5px;height:11px;border-radius:1px;display:inline-block}
.ebs{display:inline-flex;gap:2px;margin-right:6px;vertical-align:middle}
.apchip{display:inline-block;font-size:12.5px;color:#a8a495;background:#ffffff10;padding:2px 7px;border-radius:0;margin:0 6px 2px 0}
.apgain{font-family:'Archivo',sans-serif;font-weight:900;letter-spacing:-.02em;line-height:1}
.pbtn{font:inherit;font-size:13px;font-weight:600;color:var(--red);background:transparent;border:1px solid #db063266;border-radius:0;padding:5px 10px;cursor:pointer}
.pbtn:hover{background:#db06321f}
.pbtn.g{color:#a8a495;border-color:#ffffff28}.pbtn.g:hover{color:#fff;border-color:#ffffff5c}
.apurls{display:none;font-size:13px;padding:2px 0 12px;columns:2;column-gap:24px}
.apurls a{color:var(--muted)}.apurls a:hover{color:var(--txt)}
.apurls .b{display:block;padding:2px 0;break-inside:avoid;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dotb{width:7px;height:7px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle;flex:none}
.rmc{background:var(--panel);border:1px solid var(--line);border-radius:0;overflow:hidden}
.rmch{padding:16px 20px 12px;border-bottom:1px solid #ffffff12}
.rmch h3{margin:0;font-size:15px;letter-spacing:.06em;font-family:'Archivo',sans-serif;font-weight:800}
.rmph{padding:16px 20px;border-bottom:1px solid #ffffff12}
.rmph:last-child{border-bottom:0}
.rmnum{font-size:13px;font-weight:800;padding:3px 8px;border-radius:4px;flex:none}
.card2{background:var(--panel);border:1px solid var(--line);border-radius:0;padding:18px 20px}
.card2 h3{margin:0 0 10px;font-size:15px;letter-spacing:.06em;font-family:'Archivo',sans-serif;font-weight:800}
.rowsb{display:flex;align-items:center;justify-content:space-between;font-size:13px;padding:4px 0}
.bigbtn{display:block;width:100%;text-align:center;font:inherit;font-size:14px;font-weight:800;color:#fff;background:var(--red);border:none;border-radius:0;padding:12px;cursor:pointer}
.bigbtn:hover{background:var(--grn2);color:#fff}
.dashnote{display:flex;align-items:center;gap:10px;padding:14px 18px;border:1px dashed #ffffff24;border-radius:0;font-size:12.5px;color:var(--muted);line-height:1.5}
.issum{display:flex;align-items:center;gap:30px;flex-wrap:wrap}
.issum .big{font-size:44px;font-weight:900;line-height:1;font-family:'Archivo',sans-serif;letter-spacing:-.02em}
.istat{display:flex;flex-direction:column;gap:5px;justify-content:center}
.inumwrap{display:flex;align-items:center;gap:8px;min-height:34px}
.inum{font-family:'Archivo',sans-serif;font-size:34px;font-weight:900;line-height:1;letter-spacing:-.02em}
.isub{font-size:13px;color:var(--muted);line-height:1.35}
.vr{width:1px;align-self:stretch;min-height:44px;background:#ffffff14}
.fpill{font:inherit;font-size:13px;font-weight:600;color:#a8a495;background:transparent;border:1px solid #ffffff28;border-radius:0;padding:7px 14px;cursor:pointer}
.fpill:hover{color:#fff}
.fpill.on{color:#fff;background:var(--red);border-color:var(--red)}
.wpage{display:flex;align-items:center;gap:14px;padding:13px 20px;border-bottom:1px solid #ffffff10;cursor:pointer;color:inherit}
.wpage:hover{background:#ffffff08}
.wpage:last-child{border-bottom:0}
.wpage .s{font-size:20px;font-weight:800;width:34px;font-family:'Archivo',sans-serif}
@media(max-width:1080px){.ap2{grid-template-columns:1fr}.apside{position:static}.apurls{columns:1}}
/* ===== Pages (mockup redesign) ===== */
.pgsum{background:#191914;border:1px solid var(--line);border-radius:0;padding:20px 26px;display:flex;align-items:center;gap:30px;flex-wrap:wrap}
.pgdist{display:flex;align-items:flex-end;gap:8px;height:76px}
.pgdist .col{flex:1;display:flex;flex-direction:column;justify-content:flex-end;gap:5px}
.pgdist .trk{height:44px;display:flex;align-items:flex-end}
.pgdist .trk i{width:100%;border-radius:4px 4px 0 0;display:block}
.pgscroll{overflow-x:auto}
.pgtbl{background:var(--panel2);border:1px solid var(--line);border-radius:0;overflow:hidden;min-width:1060px}
.pgcols{display:grid;grid-template-columns:58px 1fr 40px 40px 40px 50px 50px 50px 50px 50px 50px 56px 220px;gap:9px;align-items:center}
.pghead{padding:12px 20px;background:#191914;border-bottom:1px solid var(--line);font-size:12.5px;font-weight:700;letter-spacing:.06em;color:var(--muted)}
.pghead span[data-s]{cursor:pointer}.pghead span[data-s]:hover{color:#fff}
.pgrow{padding:11px 20px;border-bottom:1px solid #ffffff0d}
.pgrow:last-child{border-bottom:0}
.pgrow a{font-size:14px;font-weight:600;color:var(--txt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pgrow a:hover{color:var(--grn)}
.tybadge{font-size:12px;color:#8b8b81;border:1px solid #ffffff1f;border-radius:3px;padding:1px 5px;flex:none}
.schip{font-size:15px;font-weight:800;text-align:center;padding:5px 0;border-radius:0;font-family:'Archivo',sans-serif}
.ecell{font-size:13px;font-weight:600;text-align:center;padding:4px 0;border-radius:4px}
.pgpill{font:inherit;font-size:13px;font-weight:600;color:#a8a495;background:transparent;border:1px solid #ffffff28;border-radius:999px;padding:7px 13px;cursor:pointer}
.pgpill:hover{color:#fff}
.pgpill.on{color:#fff;background:var(--grn);border-color:var(--grn)}
.pgpill.bel.on{color:#ff4d6d;background:transparent;border-color:#ff4d6d}
.pgleg{display:flex;align-items:center;gap:22px;padding:13px 20px;background:#14140f;border-top:1px solid var(--line);font-size:12.5px;color:#8b8b81;flex-wrap:wrap}
.pgsearch{display:flex;align-items:center;gap:9px;background:var(--panel2);border:1px solid #ffffff24;border-radius:0;padding:0 14px;width:280px}
.pgsearch input{flex:1;background:transparent;border:none;outline:none;font:inherit;font-size:14px;color:#fff;padding:10px 0}
.pgsearch input::placeholder{color:#8b8b81}
/* ===== Engines + Site structure + Response times ===== */
.engcols{display:grid;grid-template-columns:1fr 92px 230px 84px;gap:18px;align-items:center}
.enghead{padding:12px 0;border-bottom:1px solid var(--line);font-size:12.5px;font-weight:700;letter-spacing:.06em;color:var(--muted)}
.engrow{padding:15px 0;border-bottom:1px solid #ffffff0f;cursor:pointer}
.engrow:last-child{border-bottom:0}
.engrow:hover{background:#ffffff06}
.egcar{font-size:11px;color:var(--muted);vertical-align:middle}
.engdet{display:none;font-size:13px;padding:2px 0 14px;columns:2;column-gap:26px}
.engdet .egp{display:block;padding:2px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;break-inside:avoid}
.engdet .egp a{color:var(--muted)}.engdet .egp a:hover{color:var(--txt)}
.egh{font-size:12px;letter-spacing:.05em;color:var(--muted);text-transform:uppercase;margin:2px 0 4px;column-span:all}
.engbar{height:9px;border-radius:0;background:#262620;overflow:hidden}
.engbar i{display:block;height:100%;border-radius:0}
.wdots{font-size:14px;letter-spacing:.12em;white-space:nowrap}
.engwcols{display:grid;grid-template-columns:70px 1fr 220px 66px 74px;gap:16px;align-items:center}
.engwrow{padding:12px 20px;border-bottom:1px solid #ffffff0d}
.engwrow:last-child{border-bottom:0}
.engwhead{padding:12px 20px;background:#191914;border-bottom:1px solid var(--line);font-size:12.5px;font-weight:700;letter-spacing:.06em;color:var(--muted)}
.card2.hot{border-color:#f2f0e44d}
.numbadge{width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-size:14px;font-weight:800;flex:none}
.stbar{position:relative;height:8px;border-radius:0;background:#262620;overflow:hidden;flex:1}
.stbar i{display:block;height:100%;border-radius:0}
.stthr{position:absolute;left:70%;top:0;height:100%;width:2px;background:var(--muted);opacity:.45;z-index:1}
.strow:hover{background:#ffffff06}
.stdet{display:none;padding:2px 0 12px}
.stp{display:flex;align-items:flex-start;gap:14px;padding:8px 0;font-size:14px;border-top:1px solid #ffffff08}
.stp:first-child{border-top:0}
.stp .schip{width:40px;flex:none;font-size:13px;padding:3px 0}
.stp a{color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stp a:hover{color:var(--txt)}.stp .qd{font-size:13px}
.stcks{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.stck{font-size:12px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);white-space:nowrap}
.stck.bad{color:#ff4d6d;border-color:#ff4d6d44}.stck.warn{color:#8b8b81;border-color:#8b8b8144}
.strow{display:grid;grid-template-columns:170px 70px 1fr 52px;gap:16px;align-items:center;padding:12px 0;border-bottom:1px solid #ffffff0f}
.strow:last-child{border-bottom:0}
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}
.statcard{background:var(--panel2);border:1px solid var(--line);border-radius:0;padding:16px 18px}
.statcard .n{font-family:'Archivo',sans-serif;font-size:30px;font-weight:900;line-height:1;letter-spacing:-.02em}
.statcard .l{font-size:12.5px;color:var(--muted);margin-top:6px}
@media(max-width:1080px){.engcols{grid-template-columns:1fr 80px 160px 60px}.engwcols{grid-template-columns:60px 1fr 140px 56px 64px}}
#printroot{display:none}
/* ---- full story report (Print / PDF): a light, editorial client deliverable ---- */
.crep{background:#fff;color:#161b18;font-family:'Archivo',-apple-system,Segoe UI,Arial,sans-serif;max-width:880px;margin:0 auto;padding:0 46px 70px;line-height:1.55;font-size:14px;
 --ink:#161b18;--muted:#63706a;--hair:#e6eae4;--paper2:#f6f8f4;--grn:#1c7f29;--grnbg:#e7f4e6;--amb:#a06a12;--ambbg:#f8efd8;--red:#b23a2b;--redbg:#f7e5e1;--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace}
.crep *{box-sizing:border-box}
.crep h1,.crep h2,.crep h3,.crep h4,.crep p,.crep ul{margin:0}
.crep .cr-cover{padding:38px 0 30px;border-bottom:2px solid var(--ink)}
.crep .cr-mast{display:flex;align-items:center;gap:12px;margin-bottom:34px}
.crep .cr-mast .wm{font-weight:900;font-size:19px;letter-spacing:-.02em}
.crep .cr-mast .ws{font-family:var(--mono);font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--muted);margin-left:-3px}
.crep .cr-mast .meta{margin-left:auto;text-align:right;font-family:var(--mono);font-size:12.5px;color:var(--muted);line-height:1.7}
.crep .cr-title{font-size:15px;font-family:var(--mono);letter-spacing:.02em;color:var(--muted);text-transform:uppercase}
.crep .cr-dom{font-size:34px;font-weight:900;letter-spacing:-.03em;line-height:1.05;margin:4px 0 22px}
.crep .cr-hero{display:flex;align-items:center;gap:26px}
.crep .cr-bigscore{font-size:78px;font-weight:900;letter-spacing:-.04em;line-height:.9}
.crep .cr-bigscore small{font-size:22px;color:var(--muted);font-weight:800}
.crep .cr-verdict{flex:1}
.crep .cr-vlead{font-size:16px;line-height:1.5;margin-top:12px;max-width:46ch}
.crep .cr-sec{padding-top:40px}
.crep .cr-sechead{display:flex;align-items:baseline;gap:14px;border-bottom:1px solid var(--ink);padding-bottom:10px;margin-bottom:22px}
.crep .cr-num{font-family:var(--mono);font-size:13px;font-weight:600;color:var(--grn)}
.crep .cr-sechead h2{font-size:23px;font-weight:900;letter-spacing:-.025em}
.crep .cr-sechead .cr-sub{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--muted)}
.crep .cr-lead{font-size:15.5px;line-height:1.6;max-width:64ch;color:#25302a}
.crep .cr-lead b{color:var(--ink)}
.crep .cr-bars{display:flex;flex-direction:column;gap:11px;margin-top:20px}
.crep .cr-bar{display:grid;grid-template-columns:150px 1fr 60px;align-items:center;gap:14px}
.crep .cr-bar .bl{font-weight:700;font-size:13.5px}
.crep .cr-bar .bl small{display:block;font-weight:400;color:var(--muted);font-size:11px}
.crep .cr-bar .bt{height:9px;background:var(--paper2);border-radius:0;overflow:hidden}
.crep .cr-bar .bt i{display:block;height:100%;border-radius:0}
.crep .cr-bar .bv{font-family:var(--mono);font-weight:600;text-align:right;font-size:14px}
.crep .cr-chips{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:24px}
.crep .cr-chip{border:1px solid var(--hair);border-radius:11px;padding:13px 15px;background:var(--paper2)}
.crep .cr-chip .n{font-size:25px;font-weight:900;letter-spacing:-.02em}
.crep .cr-chip .l{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-top:3px}
.crep .cr-grp{font-family:var(--mono);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:22px 0 10px;display:flex;align-items:center;gap:8px}
.crep .cr-grp::after{content:"";flex:1;height:1px;background:var(--hair)}
.crep .cr-item{display:grid;grid-template-columns:20px 1fr auto;gap:12px;padding:12px 0;border-bottom:1px solid var(--hair);break-inside:avoid}
.crep .cr-item:last-child{border-bottom:0}
.crep .cr-ic{font-family:var(--mono);font-weight:700;font-size:13px;line-height:1.5}
.crep .cr-ic.g{color:var(--grn)}.crep .cr-ic.a{color:var(--amb)}.crep .cr-ic.r{color:var(--red)}
.crep .cr-it .t{font-weight:700;font-size:14px}
.crep .cr-it .why{color:var(--muted);font-size:12.5px;margin-top:3px;line-height:1.45}
.crep .cr-it .fix{font-size:13px;margin-top:5px;color:#25302a}
.crep .cr-tag{font-family:var(--mono);font-size:10px;font-weight:600;padding:2px 7px;border-radius:0;white-space:nowrap}
.crep .cr-tag.g{background:var(--grnbg);color:var(--grn)}.crep .cr-tag.a{background:var(--ambbg);color:var(--amb)}.crep .cr-tag.r{background:var(--redbg);color:var(--red)}
.crep .cr-meta-r{text-align:right;font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap;line-height:1.6}
.crep .cr-meta-r b{color:var(--grn);font-size:13px}
.crep .cr-road{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:22px}
.crep .cr-phase{border:1px solid var(--hair);border-top:3px solid var(--grn);border-radius:0;padding:15px;break-inside:avoid}
.crep .cr-phase .ph{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.crep .cr-phase h4{font-size:15px;font-weight:800;margin:2px 0 9px}
.crep .cr-phase ul{list-style:none;padding:0;font-size:12.5px}
.crep .cr-phase li{padding:4px 0 4px 15px;position:relative;color:#25302a}
.crep .cr-phase li::before{content:"";position:absolute;left:0;top:10px;width:5px;height:5px;border-radius:50%;background:var(--grn)}
.crep .cr-proj{display:flex;align-items:center;gap:22px;margin-top:20px;background:var(--grnbg);border:1px solid #cbe6c8;border-radius:0;padding:22px 26px;break-inside:avoid}
.crep .cr-proj .now,.crep .cr-proj .then{text-align:center}
.crep .cr-proj .lbl{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.crep .cr-proj .v{font-size:52px;font-weight:900;letter-spacing:-.03em;line-height:1}
.crep .cr-proj .arrow{font-size:30px;color:var(--grn);font-weight:700}
.crep .cr-proj .then .v{color:var(--grn)}
.crep .cr-proj .say{flex:1;font-size:14px;line-height:1.55}
.crep .cr-proj .say b{color:var(--grn)}
.crep table{width:100%;border-collapse:collapse;font-size:12px;margin-top:14px}
.crep thead th{text-align:left;font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:600;border-bottom:1.5px solid var(--ink);padding:7px 8px}
.crep tbody td{padding:6px 8px;border-bottom:1px solid var(--hair)}
.crep tbody td.sc{font-family:var(--mono);font-weight:600}
.crep .cr-foot{margin-top:38px;padding-top:16px;border-top:1px solid var(--hair);font-family:var(--mono);font-size:10.5px;color:var(--muted);line-height:1.7}
@media print{
 @page{margin:13mm 11mm}
 body{background:#fff!important;color:#161b18}
 header,.tabs,#app{display:none!important}
 #printroot{display:block}
 .crep{padding:0!important;max-width:none!important}
 .crep .cr-item,.crep .cr-phase,.crep .cr-proj,.crep .cr-chip,.crep .cr-bar,.crep tr{break-inside:avoid}
 .crep .cr-sec{break-inside:auto}
}
"""
    js=r"""
const D=window.__DATA__;
const WL=!!D.client, DB=!!D._debrand, SCORELABEL=(WL||DB)?'AI Search Score':'Rubric';   // white-label OR Pro de-brand: drop the Rubric name
const col=s=>s>=70?'#f2f0e4':s>=50?'#ff4d6d':'#ff4d6d';
const bcol=v=>v>=70?'#f2f0e4':v>=50?'#ff4d6d':'#ff4d6d';
const dlt=(now,was)=>{if(was==null)return'';const d=now-was,c=d>0?'up':d<0?'dn':'z',s=(d>0?'+':'')+d;return ` <span class="d ${c}">${s}</span>`};
const dot=s=>`<span class="dot ${s}">●</span>`;
const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const rel=u=>esc(u.replace(D.origin,'')||'/');
const ring=v=>`<div class="ring" style="--p:${v};--c:${col(v)}"><i>${v}</i></div>`;
const scb=v=>`<span class="sc" style="background:${col(v)}">${v}</span>`;
const bd=p=>`<span class="badge ${p}">${p}</span>`;
const ECOLS=['ChatGPT','Perplexity','AI Overviews','Gemini','Copilot','Claude'];
const PRIMARY=['Overview','Action Plan','Issues','Pages','Agent-ready'];
const ENGTABS=['ChatGPT','Perplexity','AI Overviews','Gemini','Copilot','Claude','Grok'];
const TECHTABS=['Off-page','Info gain','Site structure','Response times','Broken links','AI crawlers','Common Crawl'];
const TABS=[...PRIMARY,...ENGTABS,...TECHTABS];
let cur='Overview',sortk='score',sortd=1,pageFilter='';
function tabcount(t){if(t=='Action Plan')return (typeof ACT=='function'?ACT().length:null);if(t=='Issues')return (D.issues||[]).filter(i=>i.pillar!='Info').length;return null;}
function tabsbar(){
 const bd=(t)=>{const c=tabcount(t);return (c!=null)?`<span class="tabcount ${t=='Issues'?'err':''}">${c}</span>`:'';};
 const item=(t)=>`<div class="tab ${t==cur?'on':''}" onclick="go('${t}')">${t}${bd(t)}</div>`;
 const dd=(label,items)=>{const active=items.indexOf(cur)>=0;return `<div class="tabdd"><div class="tab ${active?'on':''}" onclick="tglDD(event,'${label}')">${label} <span style="color:#8b8b81;font-size:12.5px">&#9662;</span></div><div class="ddmenu" id="dd_${label}">${items.map(t=>`<div class="ddi ${t==cur?'on':''}" onclick="go('${t}')">${t}${t=='Grok'?'<span style="color:#8b8b81;margin-left:6px">adv</span>':''}</div>`).join('')}</div></div>`;};
 document.getElementById('tabs').innerHTML=PRIMARY.map(item).join('')+'<div class="tabsep"></div>'+dd('Engines',ENGTABS)+dd('Technical',TECHTABS);
}
function tglDD(e,label){e.stopPropagation();const m=document.getElementById('dd_'+label);const open=m.style.display=='block';document.querySelectorAll('.ddmenu').forEach(x=>x.style.display='none');m.style.display=open?'none':'block';}
function go(t){cur=t;pageFilter='';document.querySelectorAll('.ddmenu').forEach(x=>x.style.display='none');tabsbar();render();updExp()}
function csToggleMore(el){var m=document.getElementById('csMore');if(!m)return;var o=m.style.display=='none';m.style.display=o?'block':'none';el.innerHTML=o?'Hide extra analyses &#9652;':'Show '+m.children.length+' more analyses &#9662;';}
function csCopyAllow(el){var p=document.getElementById('csAllowPre');if(!p)return;try{navigator.clipboard.writeText(p.textContent);el.textContent='Copied';setTimeout(function(){el.textContent='Copy';},1800);}catch(e){}}
document.addEventListener('click',function(){document.querySelectorAll('.ddmenu').forEach(x=>x.style.display='none');});
// ---- context-aware CSV export: the header button exports the CURRENT tab's data + labels itself for it ----
var EXPORTS={'Pages':{label:'Export pages',fn:function(){exportPages()}},
 'Action Plan':{label:'Export action plan',fn:function(){exportPlan()}},
 'Broken links':{label:'Export broken links',fn:function(){exportBroken()}},
 'Site structure':{label:'Export sitemap issues',fn:function(){exportSitemap()}}};
function expInfo(){return EXPORTS[cur]||{label:'Export pages',fn:function(){exportPages()}};}
function exportCurrent(){exportAll();}
function exportAll(){exportPages();setTimeout(exportPlan,350);if((D.broken_links||[]).length)setTimeout(exportBroken,700);var s=D.sitemap||{};if(s.has_sitemap)setTimeout(exportSitemap,1050);}
function updExp(){var b=document.getElementById('expbtn');if(b)b.textContent='Export all (CSV)';}
function exportSitemap(){var s=D.sitemap||{},rows=[['issue','path']];
 (s.missing_from_sitemap||[]).forEach(function(u){rows.push(['missing from sitemap',u]);});
 (s.orphan_no_internal_links||[]).forEach(function(u){rows.push(['orphan - no internal links',u]);});
 (s.noindex_in_sitemap||[]).forEach(function(u){rows.push(['noindexed in sitemap',u]);});
 if(rows.length===1)rows.push(['no sitemap issues found','']);
 dl(_fn('sitemap-issues.csv'),rows);}
function render(){const w=document.getElementById('view');
 if(cur=='Overview')return w.innerHTML=ovwR();
 if(cur=='Action Plan')return w.innerHTML=planR();
 if(cur=='Issues')return w.innerHTML=issuesView();
 if(cur=='Pages')return w.innerHTML=pagesView(null);
 if(cur=='Site structure')return w.innerHTML=structure();
 if(cur=='Response times')return w.innerHTML=speed();
 if(cur=='Broken links')return w.innerHTML=brokenView();
 if(cur=='Off-page')return w.innerHTML=offpageView();
 if(cur=='Agent-ready')return w.innerHTML=agentView();
 if(cur=='Info gain')return w.innerHTML=infogainView();
 if(cur=='AI crawlers')return w.innerHTML=aicrawlerView();
 if(cur=='Common Crawl')return w.innerHTML=commoncrawlView();
 if(cur=='Grok')return w.innerHTML=grokView();
 return w.innerHTML=engine(cur);}

function diffCard(){if(!D.diff)return '';const x=D.diff;const s=(x.overall_d>0?'+':'')+x.overall_d;
 let mv='';if(x.improved.length)mv+=' Most improved: '+x.improved.slice(0,3).map(m=>rel(m.url)+' '+m.was+'→'+m.now).join(', ')+'.';
 if(x.declined.length)mv+=' Declined: '+x.declined.slice(0,3).map(m=>rel(m.url)+' '+m.was+'→'+m.now).join(', ')+'.';
 return `<div class="diffline"><b>Since ${esc(x.since)}:</b> overall ${x.overall_was} → ${D.overall} (${s}).${mv}</div>`}

function jumpIssue(id){var d=document.getElementById('pd_'+id);if(d){if(d.style.display!='block')tgl('pd_'+id);(d.previousElementSibling||d).scrollIntoView({block:'center'});}}
function passRateBars(){
  var meta=D.check_meta||{},pages=D.pages||[];if(!pages.length)return '';
  var stat={};
  pages.forEach(function(p){var cs=p.cs||{};for(var id in cs){var m=meta[id];if(!m||m.pillar=='Info')continue;var s=cs[id];if(s=='na')continue;stat[id]=stat[id]||{g:0,f:0};if(s=='good')stat[id].g++;else if(s=='warn'||s=='bad')stat[id].f++;}});
  var rows=Object.keys(stat).map(function(id){var st=stat[id];var t=st.g+st.f;return {id:id,label:(meta[id]||{}).label||id,pass:t?Math.round(100*st.g/t):100,fail:st.f};});
  if(!rows.length)return '';
  rows.sort(function(a,b){return a.pass-b.pass||b.fail-a.fail;});
  var col=function(r){return r.pass<70?'#db0632':'#f2f0e4';};
  var fail=rows.filter(function(r){return r.pass<100;}),passAll=rows.length-fail.length;
  if(!fail.length)return '';
  var bars=fail.map(function(r){return `<div style="display:flex;align-items:center;gap:12px;padding:6px 0;cursor:pointer" onclick="jumpIssue('${r.id}')"><div style="width:182px;font-size:13.5px;color:#a8a495;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.label)}</div><div style="flex:1;height:14px;background:#262620;border-radius:4px;overflow:hidden"><div style="width:${r.pass}%;height:100%;background:${col(r)}"></div></div><div style="width:96px;text-align:right;font-family:var(--mono);font-size:13px"><span style="color:${r.pass<70?'#ff4d6d':'#f2f0e4'}">${r.pass}%</span> <span style="color:#8b8b81">${r.fail} fail</span></div></div>`;}).join('');
  if(passAll>0)bars+=`<div style="padding:11px 0 2px;font-size:13px;color:#8b8b81"><span style="color:#f2f0e4">&#10003;</span> ${passAll} more check${passAll>1?'s':''} pass on every page</div>`;
  return `<section class="aptier" style="margin-bottom:22px"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>PASS RATE BY CHECK</h3><span class="meta">signals failing on some pages &middot; weakest first &middot; click a bar to jump to it</span></div><div class="apbox" style="padding:12px 22px">${bars}</div></section>`;
}
function radarSVG(){
  var E=ECOLS,n=E.length,cx=140,cy=140,R=100,MIN=60,MX=100;
  var ang=function(i){return (i/n)*Math.PI*2-Math.PI/2;};
  var pt=function(i,rr){return [cx+rr*Math.cos(ang(i)),cy+rr*Math.sin(ang(i))];};
  var rv=function(v){return R*(Math.max(MIN,Math.min(MX,v))-MIN)/(MX-MIN);};
  var s='<svg width="100%" viewBox="-40 0 360 285" style="max-width:380px;display:block;margin:0 auto" role="img"><title>Citability across six engines</title>';
  [100,70].forEach(function(gv){var q=[];for(var i=0;i<n;i++){var p=pt(i,rv(gv));q.push(p[0].toFixed(1)+','+p[1].toFixed(1));}var thr=gv===70;s+='<polygon points="'+q.join(' ')+'" fill="none" stroke="'+(thr?'#8b8b81':'#2a2a24')+'" stroke-width="'+(thr?'1.5':'1')+'"'+(thr?' stroke-dasharray="4 4"':'')+'/>';});
  for(var i=0;i<n;i++){var p=pt(i,R);s+='<line x1="'+cx+'" y1="'+cy+'" x2="'+p[0].toFixed(1)+'" y2="'+p[1].toFixed(1)+'" stroke="#2a2a24" stroke-width="1"/>';}
  var dp=[];for(var i=0;i<n;i++){var p=pt(i,rv(D.engines[E[i]]));dp.push(p[0].toFixed(1)+','+p[1].toFixed(1));}
  s+='<polygon points="'+dp.join(' ')+'" fill="rgba(242,240,228,0.13)" stroke="#f2f0e4" stroke-width="2" stroke-linejoin="round"/>';
  for(var i=0;i<n;i++){var v=D.engines[E[i]],p=pt(i,rv(v)),lp=pt(i,R+16),ax=Math.cos(ang(i)),an=(ax>0.3?'start':ax<-0.3?'end':'middle'),bl=v<70;
    s+='<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="4.5" fill="'+(bl?'#db0632':'#f2f0e4')+'"><title>'+esc(E[i])+' '+v+'</title></circle>';
    s+='<text x="'+lp[0].toFixed(1)+'" y="'+lp[1].toFixed(1)+'" text-anchor="'+an+'" style="font-size:12.5px;font-weight:800;fill:#a8a495;font-family:Archivo,sans-serif" dominant-baseline="middle">'+esc(E[i])+'</text>';}
  return s+'</svg>';
}
function ovwR(){
 const anon=!!D._anon;
 const ov=Math.max(0,Math.min(100,D.overall||0));
 const P=D.pillars||{};
 const pill=["Known","Findable","Trusted"];
 const weakP=pill.slice().sort((a,c)=>P[a]-P[c])[0];
 const g=(D.totals&&D.totals.good)||0,w=(D.totals&&D.totals.warn)||0,b=(D.totals&&D.totals.bad)||0,tc=g+w+b,pass=tc?Math.round(100*g/tc):0;
 const gp=tc?(100*g/tc):0,wp=tc?(100*w/tc):0,bp=tc?(100*b/tc):0;
 const df=D.diff||{},od=df.overall_d;
 const LBL="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#a8a495";
 const HLBL="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#f2f0e4";
 const MM="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81";
 const SECT="display:flex;flex-direction:column;gap:22px;padding:38px 0 42px;border-bottom:1px solid #2a2a24";
 const BAR=(val,thr)=>{const cl=val>=thr;return `<div style="flex:1;position:relative;height:8px;min-width:0;background:#262620"><div style="position:absolute;left:0;top:0;width:${Math.max(2,Math.min(100,val))}%;height:8px;background:${cl?'#f2f0e4':'#db0632'}"></div><div style="position:absolute;left:${thr}%;top:-5px;width:2px;height:18px;background:${cl?'#14140f':'#f2f0e4'}"></div></div>`;};
 const BARm=(val,med)=>{const cl=val>=med;return `<div style="flex:1;position:relative;height:8px;min-width:0;background:#262620"><div style="position:absolute;left:0;top:0;width:${Math.max(2,Math.min(100,val))}%;height:8px;background:${cl?'#f2f0e4':'#db0632'}"></div><div style="position:absolute;left:${Math.max(2,Math.min(100,med))}%;top:-5px;width:2px;height:18px;background:${cl?'#14140f':'#f2f0e4'}"></div></div>`;};
 const engBelow=ECOLS.filter(e=>D.engines[e]<70).length,vs=ov-70;
 const scoreSent=`${Math.abs(vs)} point${Math.abs(vs)==1?'':'s'} ${vs>=0?'above':'below'} the standard.${engBelow?` ${engBelow} of six engine${engBelow>1?'s':''} still answer${engBelow>1?'':'s'} without naming you.`:' All six engines can name you.'}`;
 const yourRubric=`<div style="display:flex;flex-direction:column;gap:18px;padding:34px 40px 36px 0"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px"><div style="${LBL}">Your ${SCORELABEL}</div><div style="${MM}">${od==null?'first crawl · trend shows next run':'since '+esc(df.since||'last crawl')}</div></div><div style="display:flex;align-items:flex-start;justify-content:space-between;gap:30px"><div style="font-size:84px;font-weight:800;letter-spacing:-5px;line-height:.78;color:#f2f0e4">${ov}</div><div style="font-size:14px;font-weight:400;line-height:1.5;color:#a8a495;text-align:right;max-width:250px">${scoreSent}</div></div><div style="position:relative;height:8px;background:#262620;margin-top:6px"><div style="position:absolute;left:0;top:0;width:${ov}%;height:8px;background:${ov>=70?'#f2f0e4':'#db0632'}"></div><div style="position:absolute;left:70%;top:-5px;width:2px;height:18px;background:#f2f0e4"></div></div><div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;${MM}"><div>0</div><div>standard 70</div><div>100</div></div></div>`;
 const qrow=(l,v)=>`<div style="display:flex;align-items:center;gap:18px"><div style="font-size:14px;font-weight:800;width:72px;flex:none">${l}</div>${BAR(v,70)}<div style="font-size:19px;font-weight:800;letter-spacing:-.7px;width:32px;text-align:right;flex:none">${v}</div></div>`;
 const threeQ=`<div style="display:flex;flex-direction:column;gap:18px;padding:34px 0 36px 40px;border-left:1px solid #2a2a24"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px"><div style="${LBL}">The three questions</div><div style="${MM}">scale 0&#8211;100</div></div><div style="display:flex;flex-direction:column;gap:14px">${pill.map(p=>qrow(p,P[p]||0)).join('')}</div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495">${weakP} is the drag. Every bar shares one scale and one tick, so the shortfall is read by length, not by hue.</div></div>`;
 const health=`<div style="display:flex;flex-direction:column;gap:18px;padding:30px 40px 34px 0"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px"><div style="${LBL}">Check health</div><div style="${MM}">${tc.toLocaleString()} checks &middot; ${pass}% passing</div></div><div style="display:flex;height:8px;background:#262620"><div style="width:${gp.toFixed(1)}%;background:#f2f0e4"></div><div style="width:${wp.toFixed(1)}%;background:#55534a"></div><div style="width:${bp.toFixed(1)}%;background:#db0632"></div></div><div style="display:grid;grid-template-columns:repeat(3,1fr);gap:20px"><div style="display:flex;flex-direction:column;gap:5px"><div style="font-size:26px;font-weight:800;letter-spacing:-1.2px;line-height:.85">${b}</div><div style="${MM}">errors &middot; blocking citation</div></div><div style="display:flex;flex-direction:column;gap:5px"><div style="font-size:26px;font-weight:800;letter-spacing:-1.2px;line-height:.85">${w}</div><div style="${MM}">warnings &middot; weakening</div></div><div style="display:flex;flex-direction:column;gap:5px"><div style="font-size:26px;font-weight:800;letter-spacing:-1.2px;line-height:.85">${g}</div><div style="${MM}">passed</div></div></div></div>`;
 const why=`<div style="display:flex;flex-direction:column;gap:14px;padding:30px 0 34px 40px;border-left:1px solid #2a2a24"><div style="${LBL}">Why it matters</div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495">AI Overviews cut organic clicks <span style="color:#f2f0e4;font-weight:800">~40%</span> where they appear (Agarwal &amp; Sen field RCT, 2026); pages cited in the AI Overview earn <span style="color:#f2f0e4;font-weight:800">~35% higher CTR</span> (Seer, 2025). This score is your odds of being the cited page.</div></div>`;
 var LB=D.benchmark;var BM=(LB&&LB.overall!=null)?{overall:LB.overall,Known:LB.Known,Findable:LB.Findable,Trusted:LB.Trusted}:{overall:76,Known:82,Findable:74,Trusted:72};var live=(LB&&LB.overall!=null);
 var pc=D.proposed_competitor||{};if(!window._csCompProp){window._csCompProp=1;csTrack('competitor_proposed',{source:pc.source||'benchmark',outlinks_seen:pc.outlinks_seen,cmp_pages:pc.cmp_pages});}
 const gaprow=(l,you,med)=>`<div style="display:flex;align-items:center;gap:18px"><div style="font-size:14px;font-weight:800;width:84px;flex:none">${l}</div>${BARm(you,med)}<div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:600;width:76px;text-align:right;flex:none"><span style="color:#f2f0e4">${you}</span><span style="color:#8b8b81"> / ${med}</span></div></div>`;
 var prop=(pc.mode==='competitor')?pc:null;
 const gapForm=cgForm(prop?(prop.domain||''):'','competitor.com','See the gap',prop?('Comparing you against <b style="color:#f2f0e4">'+esc(prop.domain||'')+'</b> &mdash; hit See the gap, or edit it to compare someone else.'):'See exactly where a specific competitor&#39;s page beats yours &mdash; every signal they clear that you don&#39;t, with the fix.',prop?'csClearedOnce()':'');
 const bridgeSec=D.bridge?(function(){var b=D.bridge,you=b.you||{},comp=b.competitor||{},n=b.gap_count||0,pd={Known:'#a8a495',Findable:'#ff4d6d',Trusted:'#f2f0e4'};var rows=(b.rows||[]).slice(0,n).map(function(r){var pc=pd[r.pillar]||'#ff4d6d';return `<div style="padding:14px 0;border-top:1px solid #2a2a24"><div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><span style="width:8px;height:8px;background:${pc};flex:none;position:relative;top:3px"></span><span style="font-size:14px;font-weight:800;color:#f2f0e4">${esc(r.label||'')}</span><span style="${MM};margin-left:auto">${(r.pillar||'').toLowerCase()} &middot; wt ${r.weight||0}</span></div>${r.fix?`<div style="font-size:13px;font-weight:500;line-height:1.55;color:#b6b2a4;margin:6px 0 0 18px">${esc(r.fix)}</div>`:''}</div>`;}).join('');var head=`<div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">The cited gap</div><div style="${MM}">homepage vs ${esc(comp.domain||'a competitor')}</div></div>`;if(!n||(b.score_gap||0)<=0){return `<section style="${SECT}">${head}<div style="font-size:20px;font-weight:800;letter-spacing:-.4px;color:#f2f0e4;margin:2px 0 8px">You&#39;re ${((b.score_gap||0)<0)?'ahead of':'level with'} ${esc(comp.domain||'them')}</div><div style="font-size:14px;font-weight:500;line-height:1.55;color:#a8a495;max-width:880px">Page-for-page it is <b style="color:#f2f0e4">${you.score}</b> to ${comp.score} <span style="color:#8b8b81">(your site is ${ov} overall)</span>.${n?` They still clear ${n} signal${n===1?'':'s'} you don&#39;t, below.`:` Nothing separates you on the signals we measure; if they get cited and you are not, the gap is off-page.`}</div>${n?`<div style="display:flex;flex-direction:column">${rows}</div>`:''}${cgForm('','another competitor.com','See the gap','Line up the next one, or compare someone else.')}</section>`;}return `<section style="${SECT}">${head}<div style="display:flex;align-items:center;gap:22px;flex-wrap:wrap;margin:2px 0 6px"><div><div style="${MM}">${esc(you.domain||'you')}</div><div style="font-size:34px;font-weight:800;letter-spacing:-1.4px;line-height:1;color:#f2f0e4">${you.score}</div></div><div style="${MM};font-size:15px">vs</div><div><div style="${MM}">${esc(comp.domain||'competitor')}</div><div style="font-size:34px;font-weight:800;letter-spacing:-1.4px;line-height:1;color:#f2f0e4">${comp.score}</div></div><div style="flex:1"></div><div style="text-align:right"><div style="font-size:34px;font-weight:800;letter-spacing:-1.4px;line-height:1;color:#ff4d6d">+${b.score_gap}</div><div style="${MM}">their lead</div></div></div><div style="font-size:14px;font-weight:500;line-height:1.55;color:#a8a495;max-width:880px"><b style="color:#f2f0e4">${esc(comp.domain||'They')}</b>&#39;s page out-scores your homepage and clears <b style="color:#f2f0e4">${n}</b> citability signal${n===1?'':'s'} you don&#39;t${b.query?` for <span style="color:#f2f0e4">${esc(b.query)}</span>`:''}. Here is each, with the fix:</div><div style="display:flex;flex-direction:column">${rows}</div>${cgForm(comp.domain||'','another competitor.com','See the gap','That is the on-page gap in full.')}</section>`;})():'';
 const gap=D.bridge?bridgeSec:`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">The cited gap</div><div style="${MM}">${live?('vs '+LB.n+' '+esc(LB.segment||'')+' sites'):'vs 491 sites'}</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">You against the median of ${live?('<b style="color:#f2f0e4">'+LB.n+'</b> '+esc(LB.segment||'')):'<b style="color:#f2f0e4">491</b> SEO and marketing'} sites we have crawled. The tick is the median: where your bar falls short of it is where an engine has a reason to quote someone else.</div><div style="display:flex;flex-direction:column;gap:14px">${gaprow('Overall',ov,BM.overall)}${gaprow('Known',P.Known||0,BM.Known)}${gaprow('Findable',P.Findable||0,BM.Findable)}${gaprow('Trusted',P.Trusted||0,BM.Trusted)}</div>${gapForm}</section>`;
 const cbd=D.crawler_blindspots;
 const blind=(!cbd||!(cbd.cant_see||[]).length)?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">What your SEO crawler can't see</div><div style="${MM}">${(cbd.cant_see||[]).length} checks a standard audit skips</div></div><div style="font-size:15px;font-weight:400;line-height:1.55;color:#a8a495;max-width:940px">No standard SEO crawler checks whether a WAF is blocking <span style="color:#f2f0e4;font-weight:800">ClaudeBot</span>, whether your schema survives <span style="color:#f2f0e4;font-weight:800">without JavaScript</span>, whether your answer lands inside the <span style="color:#f2f0e4;font-weight:800">5,700-character</span> no-JS read window, or how many <span style="color:#f2f0e4;font-weight:800">verifiable data points per 1,000 words</span> your page carries.</div><div style="display:flex;flex-wrap:wrap;gap:8px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600">${(cbd.cant_see||[]).map(x=>`<div style="color:#a8a495;border:1px solid #2a2a24;padding:7px 11px">${esc(x.label)}</div>`).join('')}</div><div style="font-size:12.5px;font-weight:400;line-height:1.5;color:#a8a495;max-width:940px">Not present in the default output of ${esc(cbd.tools||'')} (checked ${esc(cbd.checked||'')}). ${(cbd.not_weighted||[]).length} more they extract but don't weight for AI &mdash; entity clarity, schema completeness, review, robots, freshness and readability.</div></section>`;
 const radar=`<div style="display:flex;flex-direction:column;gap:22px;padding:38px 40px 42px 0"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px"><div style="${HLBL}">Citability shape</div><div style="${MM}">across six engines</div></div><div style="display:flex;align-items:center;justify-content:center;padding:4px 0">${radarSVG()}</div><div style="display:flex;align-items:center;justify-content:center;gap:24px;${MM}"><div style="display:flex;align-items:center;gap:7px"><div style="width:14px;height:8px;background:rgba(242,240,228,.35);border:1px solid #f2f0e4"></div><span>your score</span></div><div style="display:flex;align-items:center;gap:7px"><div style="width:14px;height:0;border-top:1.5px dashed #8b8b81"></div><span>70 threshold</span></div><div style="display:flex;align-items:center;gap:7px"><div style="width:9px;height:9px;border-radius:50%;background:#db0632"></div><span>below it</span></div></div></div>`;
 const iss=(D.issues||[]);const top=iss.slice(0,3);
 const dcard=(i)=>{const eg=Object.entries(i.gain_engines||{}).filter(([e,v])=>v>0).sort((a,c)=>c[1]-a[1]).slice(0,3);return `<div style="display:flex;flex-direction:column;gap:11px;padding:20px 0;border-bottom:1px solid #2a2a24"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px"><div style="font-size:14px;font-weight:800;letter-spacing:-.3px;min-width:0">${esc(i.label)}</div><div style="font-size:22px;font-weight:800;letter-spacing:-1px;line-height:.9;flex:none">+${i.gain_overall||0}</div></div><div style="${MM}">${(i.pillar||'').toLowerCase()} &middot; ${(i.effort||'').toLowerCase()} effort &middot; ${i.count} page${i.count>1?'s':''}</div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495">${esc(i.ev||'')}</div><div style="display:flex;flex-wrap:wrap;gap:6px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:600">${eg.map(([e,v])=>`<div style="color:#a8a495;border:1px solid #2a2a24;padding:5px 8px">${e} +${v}</div>`).join('')}</div></div>`;};
 const proj=Math.min(100,ov+top.reduce((a,i)=>a+(i.gain_overall||0),0));
 const doFirst=`<div style="display:flex;flex-direction:column;padding:38px 0 42px 40px;border-left:1px solid #2a2a24"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px"><div style="${HLBL}">Do these first</div><div style="${MM}">${Math.min(3,top.length)} of ${iss.length}</div></div><div style="font-size:13px;font-weight:400;color:#a8a495;padding:8px 0 4px">Ranked by score movement per hour of work.</div>${top.map(dcard).join('')}<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding:20px 0 0"><div style="display:flex;flex-direction:column;gap:4px"><div style="font-size:14px;font-weight:800">Top three done</div><div style="${MM}">+${proj-ov} from about a day of work</div></div><div style="display:flex;align-items:baseline;gap:8px;flex:none"><div style="font-size:30px;font-weight:800;letter-spacing:-1.4px;line-height:.85">${proj}</div><div style="${MM};color:#a8a495">clears</div></div></div><div onclick="go('Action Plan')" style="align-self:flex-start;margin-top:16px;font-size:12px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#a8a495;cursor:pointer">Full plan &rarr;</div></div>`;
 const _ew=ECOLS.slice().sort((a,c)=>D.engines[a]-D.engines[c]),worst=_ew[0],best=_ew[_ew.length-1],allClear=_ew.every(e=>D.engines[e]>=70);
 const erow=(e)=>{const v=D.engines[e],to=Math.max(0,70-v),sub=(e==worst)?'weakest':(e==best)?'strongest':'';return `<div style="display:flex;align-items:center;gap:18px"><div style="width:100px;flex:none"><div style="font-size:14px;font-weight:800">${e}</div>${sub?`<div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81">${sub}</div>`:''}</div>${BAR(v,70)}<div style="font-size:17px;font-weight:800;width:30px;text-align:right;flex:none">${v}</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${to?'#8b8b81':'#a8a495'};width:66px;text-align:right;flex:none">${to?to+' to 70':'clears'}</div></div>`;};
 const engSec=`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="display:flex;align-items:baseline;gap:16px"><div style="${HLBL}">Readiness by engine</div><div style="${MM}">worst first</div></div><div style="font-size:13px;font-weight:800;color:#a8a495">${allClear?'All six clear 70':worst+' is the weakest'}</div></div><div style="display:flex;flex-direction:column;gap:14px">${_ew.map(erow).join('')}</div><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px;${MM}"><div>grok &middot; not scored &mdash; reads the same web signals as Perplexity and ChatGPT, so those cover it</div><div onclick="go('Grok')" style="color:#a8a495;cursor:pointer">Advisory &rarr;</div></div></section>`;
 const ts={};(D.pages||[]).forEach(p=>{const t=p.type||'page';(ts[t]=ts[t]||{n:0,q:0});ts[t].n++;if((p.score||0)>=70)ts[t].q++;});
 const tent=Object.entries(ts).sort((a,c)=>c[1].n-a[1].n),tmax=Math.max.apply(0,tent.map(t=>t[1].n).concat(1));
 const trow=(k,o)=>{const pct=o.n?Math.round(100*o.q/o.n):0,cl=pct>=50;return `<div style="display:flex;align-items:center;gap:16px"><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:600;color:#a8a495;width:62px;flex:none">${esc(k)}</div><div style="flex:1;height:8px;min-width:0;background:#262620"><div style="width:${Math.round(100*o.n/tmax)}%;height:8px;background:${cl?'#f2f0e4':'#db0632'}"></div></div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:600;color:#8b8b81;width:30px;text-align:right;flex:none">${o.n}</div><div style="font-size:14px;font-weight:800;width:44px;text-align:right;flex:none">${pct}%</div></div>`;};
 const P2=(D.pages||[]).length,parityBad=(D.pages||[]).filter(p=>p.cs&&p.cs.parity=='bad').length,reach=((D.site_checks||[]).find(s=>s.id=='reachability')||{}).status||'good';
 const rrow2=(name,txt,red)=>`<div style="display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 0;border-top:1px solid #2a2a24"><div style="font-size:14px;font-weight:800">${name}</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${red?'#ff4d6d':'#a8a495'}">${txt}</div></div>`;
 const pagesSec=`<section style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);border-bottom:1px solid #2a2a24"><div style="display:flex;flex-direction:column;gap:20px;padding:34px 40px 38px 0"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px"><div style="${HLBL}">Pages by type</div><div style="${MM}">% quotable</div></div><div style="display:flex;flex-direction:column;gap:14px">${tent.map(([k,o])=>trow(k,o)).join('')}</div></div><div style="display:flex;flex-direction:column;gap:20px;padding:34px 0 38px 40px;border-left:1px solid #2a2a24"><div style="${HLBL}">Crawler reachability</div><div style="display:flex;flex-direction:column">${rrow2('GPTBot',reach=='good'?P2+'/'+P2+' allowed':reach=='warn'?'partial':'blocked',reach=='bad')}${rrow2('PerplexityBot',reach=='good'?P2+'/'+P2+' allowed':reach=='warn'?'partial':'blocked',reach=='bad')}${rrow2('Google-Extended',reach=='bad'?'blocked':'partial',true)}${rrow2('Server-rendered schema',parityBad?parityBad+' pages JS-only':'all '+P2+' server-rendered',!!parityBad)}</div></div></section>`;
 const foD=(D.fanout||[]);
 const forow=(f)=>{const pct=Math.round(100*f.won/(f.total||1)),has=f.won>0;return `<div style="display:flex;align-items:center;gap:18px"><div style="font-size:14px;font-weight:${f.key?800:500};color:${f.key?'#f2f0e4':'#a8a495'};width:94px;flex:none">${f.type}</div><div style="flex:1;height:7px;min-width:0;background:#262620">${has?`<div style="width:${pct}%;height:7px;background:#db0632"></div>`:''}</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${has?'#a8a495':'#8b8b81'};width:34px;text-align:right;flex:none">${f.won}/${f.total}</div></div>`;};
 const foSec=!foD.length?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Fan-out readiness</div><div style="${MM}">entity + comparison = most mentions</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">The sub-query types an engine generates when it fans a topic out. Winning <span style="color:#f2f0e4;font-weight:800">Entity</span> and <span style="color:#f2f0e4;font-weight:800">Comparison</span> is where AI decides who you are and who is best (Moz 50K-fan-out study).</div><div style="display:flex;flex-direction:column;gap:12px">${foD.map(forow).join('')}</div></section>`;
 const cbD=D.citability_blocks;
 const cbFix={answer:'lead with a direct answer',selfcontained:'opens mid-thought &mdash; name the subject in the first line',structure:'no heading &mdash; add a question-shaped H2',stats:'no data &mdash; add a sourced statistic',uniqueness:'near-duplicate of other pages &mdash; make it distinct'};
 const cbRow=(b,weak)=>{const sc=b.score||0,red=sc<70,sub=b.sub||{},ks=Object.keys(sub);const lo=ks.length?ks.slice().sort((a,c)=>sub[a]-sub[c])[0]:'';const head=b.heading?esc(b.heading):'(lead paragraph)';return `<div style="padding:15px 0;border-top:1px solid #2a2a24"><div style="display:flex;align-items:baseline;gap:12px"><div style="font-size:22px;font-weight:800;letter-spacing:-1px;line-height:.9;color:${red?'#ff4d6d':'#f2f0e4'};width:34px;flex:none">${sc}</div><div style="font-size:14px;font-weight:800;letter-spacing:-.3px;min-width:0;color:#f2f0e4">${head}</div></div><div style="font-size:13px;font-weight:400;line-height:1.55;color:#b6b2a4;margin:7px 0 0 46px">${esc(b.snippet||'')}</div><div style="${MM};margin:7px 0 0 46px">${esc(rel(b.path||''))}${weak&&lo&&cbFix[lo]?` &middot; <span style="color:#ff4d6d">${cbFix[lo]}</span>`:''}</div></div>`;};
 const cbSec=(!cbD||!((cbD.top||[]).length))?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Quotable passages</div><div style="${MM}">block avg ${cbD.avg} &middot; ${cbD.n} scored</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">AI lifts <span style="color:#f2f0e4;font-weight:800">passages</span>, not whole pages. These are the individual blocks on your site an engine is most and least likely to quote &mdash; scored on whether each answers directly, stands alone, carries data, and is distinct from your other pages.</div><div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:40px">
   <div><div style="${LBL}">Most quotable</div>${(cbD.top||[]).slice(0,5).map(b=>cbRow(b,false)).join('')}</div>
   <div><div style="${LBL}">Least quotable &mdash; fix these</div>${(cbD.weakest||[]).slice(0,5).map(b=>cbRow(b,true)).join('')}</div>
 </div></section>`;
 const pqW=(D.proposed_queries&&D.proposed_queries.queries)||[];
 if(!window._csQProp&&pqW.length){window._csQProp=1;csTrack('queries_proposed',{count:pqW.length,headings_seen:(D.proposed_queries||{}).headings_seen,from_headings:(D.proposed_queries||{}).from_headings});}
 const qSec=(!pqW.length)?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Queries to check</div><div style="${MM}">pre-ticked &middot; edit any</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">The sub-queries we would check your citability for, taken from your own pages. Untick or edit any, then copy them to run in ChatGPT or Perplexity.</div><div style="display:flex;flex-direction:column">${pqW.map((q,i)=>`<div style="display:flex;align-items:center;gap:16px;padding:11px 0;border-top:1px solid #2a2a24"><input type="checkbox" checked id="pq${i}" onchange="csQEdit()" style="width:15px;height:15px;accent-color:#ff4d6d;flex:none"><input type="text" id="pqt${i}" value="${esc(q.q)}" oninput="csQEdit()" style="flex:1;min-width:0;background:transparent;border:none;color:#f2f0e4;font-size:14px;font-weight:800;font-family:inherit;padding:2px"><span style="${MM};flex:none">${esc(q.type)}</span></div>`).join('')}</div><button onclick="csQCopy(this)" style="align-self:flex-start;font-size:12px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#14140f;background:#f2f0e4;border:0;padding:12px 18px;cursor:pointer;font-family:inherit">Copy for AI</button></section>`;
 const biz=`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">In business terms</div><div style="${MM}">index &middot; outcome</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">What each number here means for revenue and demand, not GEO jargon &mdash; buyers want measurable outcomes, not acronyms (Fractl, 2026).</div><div style="display:flex;flex-direction:column">${[['Your Rubric','Your odds of being the source AI names when a buyer asks about your category. The mention and the click go to whoever gets quoted.'],['The 70 line','Below it, engines answer without you. The buyer never sees your name, so there is nothing to click and no brand lift.'],['The three pillars','Can AI find you, use your answer, and trust you enough to name you. All three have to hold before you are cited.'],['The action plan','Ranked by score points per hour of work, so effort maps to more answers where you are the cited source, not busywork.'],['Off-page &amp; reviews','About 84% of AI citations are third-party. Citation is demand you capture without paying per click, unlike ads.']].map(r=>`<div style="display:grid;grid-template-columns:190px minmax(0,1fr);gap:24px;padding:16px 0;border-top:1px solid #2a2a24"><div style="font-size:14px;font-weight:800">${r[0]}</div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495">${r[1]}</div></div>`).join('')}</div></section>`;
 const stW=((D.profile_up&&D.profile_up.length)||(D.profile_down&&D.profile_down.length));
 const stL=esc(D.site_type_label||D.site_type||'general-purpose');
 const cm=(cid)=>esc((((D.check_meta||{})[cid])||{}).label||cid);
 const stSec=`<section style="display:flex;flex-direction:column;gap:22px;padding:38px 0 42px"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Website-type profile</div><div style="${MM}">detected: ${stL}</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">This site was ${D.site_type_source=='override'?'set by you as':'detected as'} ${stL}, so the checks that decide AI citation for this page type are weighted higher and the less relevant ones lower. Same 0&#8211;100 scale; every page's applicable checks unchanged &mdash; only the emphasis shifts.</div>${stW?`<div style="display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:40px"><div style="display:flex;flex-direction:column;gap:14px"><div style="${LBL}">Weighted up</div><div style="display:flex;flex-wrap:wrap;gap:8px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600">${(D.profile_up||[]).map(c=>`<div style="color:#f2f0e4;border:1px solid #55534a;padding:7px 11px">${cm(c)}</div>`).join('')||'<span style="color:#8b8b81">none</span>'}</div></div><div style="display:flex;flex-direction:column;gap:14px"><div style="${LBL}">Weighted down</div><div style="display:flex;flex-wrap:wrap;gap:8px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600">${(D.profile_down||[]).map(c=>`<div style="color:#8b8b81;border:1px solid #2a2a24;padding:7px 11px">${cm(c)}</div>`).join('')||'<span style="color:#8b8b81">none</span>'}</div></div></div>`:''}</section>`;
 const foot=`<footer style="font-size:12.5px;font-weight:400;line-height:1.6;color:#a8a495;padding:30px 0 0;border-top:1px solid #2a2a24;max-width:1100px"><span style="color:#a8a495;font-weight:800">Why citability matters:</span> AI Overviews cut organic clicks ~40% where they appear (Agrawal &amp; Sen field RCT, 2026), and pages cited in the AI Overview earn ~35% higher CTR (Seer, 2025) &mdash; so this score is your odds of being the cited page. Rubric <span style="color:#a8a495">estimates</span> citability from on-page, structural and technical signals. It does not measure citations. Every check carries a source; Grok is shown for reference only and is not scored. Scanned ${esc(D.generated||D.date||'')} &middot; crawl ran locally, no page data left this machine.</footer>`;
 const dc=D.decay||{};const atRisk=(dc.at_risk||[]);const nRisk=(dc.stale||0)+(dc.undated||0);
 const decaySec=(!(dc.dated||0)&&!(dc.undated||0))?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Decay risk</div><div style="${MM}">freshness &middot; ${dc.dated||0} dated</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">In a logged 2026 experiment, <span style="color:#f2f0e4;font-weight:800">about half of AI citations stopped within 30 days</span>. Freshness is one of the strongest citation signals, so these are the pages most likely to age out of AI answers. ${nRisk?`<span style="color:#f2f0e4;font-weight:800">${nRisk}</span> of your pages ${nRisk===1?'is':'are'} at risk.`:'None of your pages look stale yet.'}</div><div style="display:flex;flex-wrap:wrap;gap:8px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600"><div style="color:#f2f0e4;border:1px solid #2a2a24;padding:7px 11px">${dc.fresh||0} fresh &lt; 90d</div><div style="color:#a8a495;border:1px solid #2a2a24;padding:7px 11px">${dc.aging||0} aging</div><div style="color:#ff4d6d;border:1px solid #2a2a24;padding:7px 11px">${dc.stale||0} stale &gt; 12mo</div><div style="color:#ff4d6d;border:1px solid #2a2a24;padding:7px 11px">${dc.undated||0} undated</div></div>${atRisk.length?`<div style="display:flex;flex-direction:column">${atRisk.slice(0,12).map(r=>`<div style="display:flex;align-items:baseline;gap:12px;padding:11px 0;border-top:1px solid #2a2a24"><span style="width:8px;height:8px;background:${r.decay=='undated'?'#ff4d6d':'#db0632'};flex:none;position:relative;top:3px"></span><a href="${esc(r.url)}" target="_blank" style="font-size:14px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(rel(r.url))}</a><span style="${MM};margin-left:auto;flex:none">${r.decay=='undated'?'no date':((r.age_days||0)+'d old')}</span></div>`).join('')}${atRisk.length>12?`<div style="${MM};padding:11px 0 0;color:#8b8b81">+ ${atRisk.length-12} more</div>`:''}</div>`:''}<div style="font-size:13px;font-weight:400;line-height:1.5;color:#8b8b81;max-width:880px">Fix: refresh the content and show a visible last-updated date, then re-crawl to confirm it moved. This flags decay <span style="color:#a8a495">risk</span> from staleness, not measured citation loss &mdash; a missing citation can also be a source conflict, so treat these as worth refreshing, not proof they were dropped.</div></section>`;
 const cq=D.content_quality||{};
 const cqTot=(cq.n_hedge||0)+(cq.n_naked||0)+(cq.n_generic||0)+(cq.n_unresolved||0)+(cq.n_broad||0)+(cq.n_schema||0)+(cq.n_pricejs||0)+(cq.n_over||0)+((cq.content_pages||0)&&(cq.orig_pages||0)<(cq.content_pages||0)?1:0);
 const cqList=(arr,lbl,col)=>((arr||[]).length?`<div style="display:flex;flex-direction:column">${arr.slice(0,8).map(r=>`<div style="display:flex;align-items:baseline;gap:12px;padding:10px 0;border-top:1px solid #2a2a24"><span style="width:8px;height:8px;background:${col};flex:none;position:relative;top:3px"></span><a href="${esc(r.url)}" target="_blank" style="font-size:14px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(rel(r.url))}</a><span style="${MM};margin-left:auto;flex:none">${lbl(r)}</span></div>`).join('')}</div>`:'');
 const contentSec=(!cqTot)?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Content citability signals</div><div style="${MM}">text quality &middot; advisory</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">In a 2026 controlled test of what an AI picks between two candidate passages, <span style="color:#f2f0e4;font-weight:800">confident, evidence-backed, specific</span> text wins and hedged / unsupported / generic text loses. These flags are advisory (NOT in your score).</div><div style="display:flex;flex-wrap:wrap;gap:8px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600">${[['hedged claims',cq.n_hedge],['unbacked superlatives',cq.n_naked],['generic headings',cq.n_generic],['unresolved passages',cq.n_unresolved],['over-broad articles',cq.n_broad],['stale or mismatched schema',cq.n_schema],['price hidden until JS',cq.n_pricejs],['oversized passages (exp.)',cq.n_over]].map(x=>`<div style="color:${x[1]?'#ff4d6d':'#a8a495'};border:1px solid #2a2a24;padding:7px 11px">${x[1]||0} ${x[0]}</div>`).join('')}</div>${((cq.content_pages||0)&&(cq.orig_pages||0)<(cq.content_pages||0))?`<div style="font-size:13px;font-weight:500;line-height:1.55;color:#a8a495;max-width:880px;margin-top:4px"><b style="color:#f2f0e4">${cq.orig_pages||0} of ${cq.content_pages} content pages</b> carry an original asset (a data table, tool, chart, dataset or video); the rest are prose an AI can regenerate without citing you. AI-search winners host something reproducible-proof, so add an original element to your strongest pages.</div>`:''}${(cq.n_commercial||0||cq.n_informational||0)?`<div style="font-size:13px;font-weight:400;line-height:1.5;color:#8b8b81;max-width:880px">Citation gating differs by intent: your <b style="color:#a8a495">${cq.n_commercial||0}</b> commercial pages still need classic top-10 ranking to be cited (structure alone will not do it), while your <b style="color:#a8a495">${cq.n_informational||0}</b> informational pages can be cited off page one on answer-first structure.</div>`:''}${(cq.pricejs||[]).length?`<div style="font-size:13px;font-weight:500;line-height:1.55;color:#a8a495;max-width:880px;margin-top:4px"><b style="color:#ff4d6d">Price is injected by JavaScript on ${cq.n_pricejs} commercial page${cq.n_pricejs===1?'':'s'}</b>, so the non-rendering AI crawlers (GPTBot, ClaudeBot, PerplexityBot) never see it - and an explicit price is one of the strongest AI-citation gatekeepers. Server-render the price into the raw HTML.</div>`:''}${cqList(cq.pricejs,r=>'price JS-only','#db0632')}${cqList(cq.hedge,r=>Math.round((r.ratio||0)*100)+'% of claims hedged','#db0632')}${cqList(cq.naked,r=>(r.n||0)+' unbacked','#db0632')}${cqList(cq.generic,r=>(r.n||0)+' generic heading'+((r.n||0)===1?'':'s'),'#ff4d6d')}${(cq.schema||[]).length?`<div style="display:flex;flex-direction:column">${cq.schema.slice(0,8).map(r=>`<div style="padding:10px 0;border-top:1px solid #2a2a24"><div style="display:flex;align-items:baseline;gap:12px"><span style="width:8px;height:8px;background:#e0a83a;flex:none;position:relative;top:3px"></span><a href="${esc(r.url)}" target="_blank" style="font-size:14px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(rel(r.url))}</a><span style="${MM};margin-left:auto;flex:none">${r.n} vague schema value${r.n===1?'':'s'}</span></div><div style="${MM};margin:5px 0 0 20px">${(r.items||[]).slice(0,3).map(i=>esc(i.field+': "'+i.value+'" ('+i.issue+')')).join(' &middot; ')}</div></div>`).join('')}</div>`:''}${(cq.money_pages||0)?`<div style="font-size:13px;font-weight:400;line-height:1.55;color:#8b8b81;max-width:880px"><b style="color:#a8a495">${cq.neg_qual_money||0} of your ${cq.money_pages} commercial page${cq.money_pages===1?'':'s'}</b> state who they are NOT for. Qualifying out the wrong buyer is oddly powerful for AI recommendation because almost no competitor publishes it - worth adding where it is missing.</div>`:''}<div style="font-size:13px;font-weight:400;line-height:1.5;color:#8b8b81;max-width:880px">Heuristic text signals, so treat as prompts to tighten, not verdicts. Fixes: replace "may / generally / up to" with the specific figure on money-page claims; put a source next to a superlative; rename "Introduction / Overview / About us" headings to the specific question they answer; open each passage with its named subject, not "this / it"; and make schema values MACHINE-VERIFIABLE (a numeric price + currency, an ISO date, a specific figure not "affordable" / "leading"). The 2026 evidence is that AI weighs the specificity of your facts, not the presence of the tag.</div></section>`;
 const fu=D.followup||{types:{},gaps:[]};
 const FUD={Clarify:'a hub / overview / FAQ that explains the category',Constrain:'a "how to choose" / buyer guide / use-cases page',Compare:'a comparison / alternatives / vs page',Validate:'a case study / results / testimonials page',Act:'a pricing / quote / booking / clear next-step page'};
 const followSec=(!(fu.total))?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Conversation coverage</div><div style="${MM}">${fu.covered||0} of ${fu.total||5} follow-up types</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">AI search answers a question, then fans out into follow-ups (clarify, constrain, compare, validate, act). To stay the cited source across the whole conversation, your site needs an asset for each. You are missing ${(fu.gaps||[]).length?`<span style="color:#ff4d6d;font-weight:800">${fu.gaps.length}</span>`:`<span style="color:#f2f0e4;font-weight:800">none</span>`}.</div><div style="display:flex;flex-wrap:wrap;gap:8px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600">${Object.keys(fu.types||{}).map(k=>{var c=(fu.types[k]||{}).covered;return `<div style="color:${c?'#f2f0e4':'#ff4d6d'};border:1px solid ${c?'#2a2a24':'rgba(255,77,109,.4)'};padding:7px 11px">${c?'&#10003;':'&#10007;'} ${k}</div>`;}).join('')}</div>${(fu.gaps||[]).length?`<div style="display:flex;flex-direction:column">${fu.gaps.map(g=>`<div style="display:flex;align-items:baseline;gap:12px;padding:11px 0;border-top:1px solid #2a2a24"><span style="width:8px;height:8px;background:#ff4d6d;flex:none;position:relative;top:3px"></span><span style="font-size:14px;font-weight:800;color:#f2f0e4;flex:none;width:88px">${g}</span><span style="font-size:13.5px;color:#a8a495;line-height:1.5">Add ${FUD[g]||'an asset for this follow-up'}.</span></div>`).join('')}</div>`:''}<div style="font-size:13px;font-weight:400;line-height:1.5;color:#8b8b81;max-width:880px">Detected from page URLs, titles and headings across the crawl, so it reads presence not quality. The Comparison fan-out is where brands most often lose the AI recommendation, so a missing Compare asset is the costliest gap.</div></section>`;
 const ea=D.entity_anchor||{};
 const entShow=ea.wikidata||ea.hreflang_jsonly||((ea.sameas_n||0)>=3&&!ea.wikidata);
 const entSec=(!entShow)?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Entity anchor</div><div style="${MM}">Known &middot; cross-language authority</div></div>${ea.wikidata?`<div style="font-size:14px;font-weight:500;line-height:1.55;color:#a8a495;max-width:880px"><b style="color:#f2f0e4">Anchored to a Wikidata entity.</b> A Wikidata QID sits in most LLMs' training data and identifies you the same way in every language market. Keep the sameAs pointing at it.</div>`:`<div style="font-size:14px;font-weight:500;line-height:1.55;color:#a8a495;max-width:880px">Your schema declares <b style="color:#f2f0e4">${ea.sameas_n||0}</b> sameAs profiles but <b style="color:#ff4d6d">none is a Wikidata entity</b>. A Wikidata QID is a language-independent authority anchor most LLMs hold in training; adding one and pointing sameAs at it is the strongest single entity signal for a site already doing entity work.</div>`}${ea.hreflang_jsonly?`<div style="font-size:13.5px;font-weight:500;line-height:1.55;color:#a8a495;max-width:880px;margin-top:6px"><b style="color:#ff4d6d">Your hreflang tags are injected by JavaScript</b>, so the non-rendering AI crawlers never see your language versions. Server-render the hreflang cluster into the raw HTML.</div>`:''}</section>`;
 const mp=D.market_platforms||{};
 const mktSec=(!mp.market)?'':(mp.is_default?`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">AI source landscape</div><div style="${MM}">${esc(mp.market)}</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">For ${esc(mp.market)}, AI answers pull most from <b style="color:#f2f0e4">${(mp.platforms||[]).map(p=>p[1]).join(', ')}</b>. Citation is off-page as much as on-page, so earn authentic presence there (Google warns that seeding inauthentic mentions does not help).</div></section>`:`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">AI source landscape</div><div style="${MM}">market: ${esc(mp.market)}</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">Citation landscapes are NATIONAL, not global. Your site targets <b style="color:#f2f0e4">${esc(mp.market)}</b>, where AI pulls from different platforms than the English-market Reddit / Wikipedia default. These are where to earn authentic presence:</div><div style="display:flex;flex-direction:column">${(mp.platforms||[]).map(p=>`<div style="display:flex;align-items:baseline;gap:12px;padding:10px 0;border-top:1px solid #2a2a24"><span style="font-size:13px;font-weight:800;color:#f2f0e4;flex:none;width:110px">${esc(p[0])}</span><span style="font-size:14px;color:#a8a495">${esc(p[1])}</span></div>`).join('')}</div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#8b8b81;max-width:880px">Inferred from your ccTLD and page language. A crawl names the platforms that matter in your market but cannot verify your presence on them (that is off-site), so treat this as the map, then earn genuine presence.</div></section>`);
 const dcp=D.decision_completeness;
 const DFL={what_is:'what it is',who_for:'who it is for',cost:'the price',what_isnt:'who it is not for',next_step:'the next step',why_better:'why it beats alternatives'};
 const decSec=(!dcp||!dcp.checked)?'':`<section style="${SECT}"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="${HLBL}">Money-page decision completeness</div><div style="${MM}">${dcp.checked} page${dcp.checked===1?'':'s'} &middot; ${esc(dcp.model||'local model')}</div></div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;max-width:880px">An AI recommends a product only when the page commits the facts a buyer needs. A local model read your top ${dcp.checked} commercial page${dcp.checked===1?'':'s'} and checked six.</div><div style="display:flex;flex-wrap:wrap;gap:8px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600">${Object.keys(DFL).map(k=>{var got=(dcp.agg||{})[k]||0;var full=got>=dcp.checked;return `<div style="color:${full?'#f2f0e4':'#ff4d6d'};border:1px solid ${full?'#2a2a24':'rgba(255,77,109,.4)'};padding:7px 11px">${got}/${dcp.checked} ${DFL[k]}</div>`;}).join('')}</div>${(dcp.rows||[]).filter(r=>(r.missing||[]).length).length?`<div style="display:flex;flex-direction:column">${dcp.rows.filter(r=>(r.missing||[]).length).slice(0,8).map(r=>`<div style="padding:11px 0;border-top:1px solid #2a2a24"><div style="display:flex;align-items:baseline;gap:12px"><span style="width:8px;height:8px;background:#ff4d6d;flex:none;position:relative;top:3px"></span><a href="${esc(r.url)}" target="_blank" style="font-size:14px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(rel(r.url))}</a></div><div style="${MM};margin:5px 0 0 20px">missing: ${(r.missing||[]).map(m=>DFL[m]||m).join(', ')}</div></div>`).join('')}</div>`:''}<div style="font-size:13px;font-weight:400;line-height:1.5;color:#8b8b81;max-width:880px">Judged by a local model on your own machine, no data left it. Advisory, not in the score. Commit every missing fact on the page itself so an AI never has to guess or leave you out.</div></section>`;
 const banners=`${D.crawl_failed?`<div style="background:rgba(219,6,50,.14);border:1px solid rgba(219,6,50,.5);padding:16px 18px;margin-bottom:20px;color:#ff4d6d;font-size:14px"><b>Couldn't crawl this site.</b> ${esc(D.crawl_note||'')}</div>`:''}`;
 const _more=[decaySec,contentSec,followSec,entSec,mktSec,decSec,foSec,biz,stSec].filter(Boolean);
 const moreSec=_more.length?`<div onclick="csToggleMore(this)" style="padding:24px 0;text-align:center;border-bottom:1px solid #2a2a24;cursor:pointer;font-size:12px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#a8a495">Show ${_more.length} more analyses &#9662;</div><div id="csMore" style="display:none">${_more.join('')}</div>`:'';
 return `${banners}<div style="display:flex;flex-direction:column">
   <section style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);border-bottom:1px solid #2a2a24">${yourRubric}${threeQ}</section>
   <section style="display:grid;grid-template-columns:minmax(0,1.55fr) minmax(300px,0.9fr);border-bottom:1px solid #2a2a24">${radar}${doFirst}</section>
   <section style="display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);border-bottom:1px solid #2a2a24">${health}${why}</section>
   ${gap}
   ${blind}
   ${engSec}
   ${pagesSec}
   ${cbSec}
   ${qSec}
   ${moreSec}
 </div>`;
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
 const ol=(o)=>({dev:'Dev',content:'Content',config:'Config'})[o]||'Content';const oc=({dev:'#a8a495',content:'#f2f0e4',config:'#a8a495'})[i.owner]||'#f2f0e4';
 return `<div class="issue" onclick="this.classList.toggle('open')" style="cursor:pointer"><h4><span class="rank">${x+1}</span> ${esc(i.label)} ${bd(i.pillar)} <span class="badge">${i.ch}</span> <span class="badge">${i.effort} effort</span> <span class="badge" style="border-color:${oc}66;color:${oc}">${ol(i.owner)}${i.owner2?' + '+ol(i.owner2):''}</span>
   ${i.gain_overall>0?`<span class="gain">+${i.gain_overall} overall</span>`:''}${eg.map(([e,v])=>`<span class="gain">+${v} ${e}</span>`).join('')}</h4>
   <div class="ev">${esc(i.ev)}</div>
   <div class="fix"><b>Fix (${i.count} page${i.count>1?'s':''}):</b> ${esc(i.fix)} <span class="muted">- click to list pages</span></div>
   <div class="urls">${[...i.bad.map(u=>'● '+rel(u)),...i.warn.map(u=>'○ '+rel(u))].join('<br>')}</div></div>`}
// ---- shared helpers for Action Plan + Issues ----
const PDOT={Known:'#a8a495',Findable:'#ff4d6d',Trusted:'#f2f0e4'};
const TTYPE={schema:'template',parity:'template',faq:'template',canonical:'template',robots:'template',sitemap:'template',reachability:'template',freshness:'template',internal:'template',http:'template',entity:'template',schemacomplete:'template',noindex:'template',speed:'template',schemavalidity:'template',orphans:'template',brokenlinks:'template',reviewschema:'template',
 answerfirst:'copy',definitional:'copy',readability:'copy',entitydensity:'copy',qheadings:'copy',sections:'copy',liststables:'copy',wordcount:'copy',statdensity:'copy',h1:'copy',author:'copy',sourced:'copy',video:'copy',comparison:'copy',rankedlist:'copy',answerthird:'copy',h2answer:'copy',nearduplicate:'copy',
 meta:'meta',title:'meta',alt:'meta',citations:'meta',duplicate:'meta'};
const EBARS=e=>{const n=(e=='High')?3:(e=='Med')?2:1,c=n==1?'#f2f0e4':n==2?'#ff4d6d':'#f2f0e4',lab=n==1?'low':n==2?'medium':'high';
 let b='';for(let k=0;k<3;k++)b+=`<span class="eb" style="background:${k<n?c:'#262620'}"></span>`;
 return `<span class="ebs">${b}</span><span style="color:${c}">${lab}</span>`};
const ACT=()=>D.issues.filter(i=>i.pillar!='Info');   // Action Plan covers every distinct failing check, tiered by score impact
const AFFPAGES=()=>D.pages.filter(p=>p.checks.some(c=>c.status=='bad'||c.status=='warn')).length;
const gpos=i=>i.gain_overall>0?i.gain_overall:0;
function tgl(id){const e=document.getElementById(id);if(e)e.style.display=e.style.display=='block'?'none':'block'}
function _cgErr(btn,msg){var s=document.getElementById('cgstatus');if(s){s.textContent=msg||'Something went wrong.';s.style.color='#db0632';}if(btn){btn.disabled=false;btn.textContent='Compare';}}
// ---- proposal telemetry ("propose don't ask"): instrument the PROPOSAL, not just the outcome ----
function csJob(){return (location.pathname.match(/\/report\/([^\/?#]+)/)||[])[1]||(D&&D._job_id)||'';}
function csTrack(ev,props){try{fetch('/api/track',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({event:ev,job_id:csJob(),props:props||{}})}).catch(function(){});}catch(e){}}
function csNorm(u){return (u||'').replace(/^https?:\/\//,'').replace(/^www\./,'').replace(/\/.*$/,'').toLowerCase();}
function csClearedOnce(){if(window._csCleared)return;window._csCleared=1;var pc=(D.proposed_competitor||{});csTrack('competitor_cleared',{source:pc.source});}
function csQEdit(){if(window._csQEdited)return;window._csQEdited=1;csTrack('queries_edited',{});}
function csQCopy(btn){var orig=((D.proposed_queries||{}).queries||[]);var picked=[];var changed=0;
  for(var i=0;i<orig.length;i++){var cb=document.getElementById('pq'+i),tx=document.getElementById('pqt'+i);if(!cb||!tx)continue;var v=(tx.value||'').trim();if(cb.checked&&v){picked.push(v);}if(v!==((orig[i].q||'').trim()))changed++;}
  csTrack('queries_copied',{count:picked.length,kept:picked.length,changed:changed});   // DEMAND: took the queries to their own model -- the cheapest test of the MCP/agent-native thesis
  if(changed===0)csTrack('queries_accepted',{count:picked.length});                       // ACCURACY: kept our guess unchanged
  // Copy a self-contained PROMPT, not bare queries -- a pasted list tells the AI nothing about the task. This
  // frames it as the citability check so the paste returns something useful (and the user has a reason to return).
  var dom=(D.domain||'my website');
  var text='Answer each question below the way you normally would. After each answer, list the specific websites and brands you cited or recommended. I am checking whether '+dom+' gets cited -- tell me which questions surface it and which do not.\\n\\n'+picked.map(function(q,i){return (i+1)+'. '+q;}).join('\\n');
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(function(){btn.textContent='Copied '+picked.length;setTimeout(function(){btn.textContent='Copy for AI';},2200);},function(){window.prompt('Copy these queries',text);});}
  else{window.prompt('Copy these queries',text);}}
function citedGapSubmit(inputId,btn){
  var el=document.getElementById(inputId);if(!el)return;
  var url=(el.value||'').trim();if(!url){el.focus();return;}
  var job=(location.pathname.match(/\/report\/([^\/?#]+)/)||[])[1]||(D&&D._job_id)||'';
  if(!job){_cgErr(btn,'Open this from your report to compare.');return;}
  var _pc=(D.proposed_competitor||{});var _prop=(_pc.mode==='competitor')?(_pc.domain||''):'';
  if(_prop){csTrack(csNorm(url)===csNorm(_prop)?'competitor_accepted':'competitor_changed',{source:_pc.source});}
  if(btn){btn.disabled=true;btn.textContent='Comparing…';}
  var s=document.getElementById('cgstatus');if(s){s.textContent='Comparing you to '+url.replace(/^https?:\/\//,'').replace(/\/.*$/,'')+'…';s.style.color='#f2f0e4';}
  fetch('/api/cited-gap',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({audit_id:job,competitor:url})})
    .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
    .then(function(res){
      if(!res.ok||!res.j.job_id){throw new Error(res.j.error||'Could not start the comparison.');}
      var jid=res.j.job_id,tries=0;
      (function poll(){tries++;
        fetch('/api/cited-gap?job='+encodeURIComponent(jid)).then(function(r){return r.json();}).then(function(p){
          if(p.status==='done'&&p.bridge){D.bridge=p.bridge;if(typeof cur!=='undefined'&&cur==='Overview')render();return;}
          if(p.status==='failed'){_cgErr(btn,p.error||'Could not compare that URL.');return;}
          if(tries>40){_cgErr(btn,'That took too long. Try again.');return;}
          setTimeout(poll,1500);
        }).catch(function(){_cgErr(btn,'Lost the connection. Try again.');});
      })();
    })
    .catch(function(e){_cgErr(btn,(e&&e.message)||'Could not read that URL.');});
}
function cgForm(val,ph,cta,lead,oninput){return `<div style="border-top:1px solid #2a2a24;padding-top:14px;margin-top:2px">${lead?`<div style="font-size:13.5px;color:#8b8b81;line-height:1.55;margin-bottom:10px">${lead}</div>`:''}<form onsubmit="event.preventDefault();citedGapSubmit('cginput',this.querySelector('button'));" style="display:flex;gap:8px;flex-wrap:wrap"><input id="cginput" type="text" value="${esc(val||'')}" placeholder="${ph}" autocomplete="off" ${oninput?'oninput="'+oninput+'"':''} style="flex:1;min-width:170px;height:40px;background:#0f0f0b;border:1px solid #2a2a24;color:#f2f0e4;border-radius:0;padding:0 13px;font-size:14px;font-family:inherit"><button type="submit" style="height:40px;background:#191914;color:#f2f0e4;border:1px solid #2a2a24;border-radius:0;font-family:inherit;font-size:14px;padding:0 16px;cursor:pointer">${esc(cta)}</button></form><div id="cgstatus" style="font-size:13px;margin-top:7px;min-height:14px"></div></div>`;}
function foldcard(id,cnt,label,desc,color,items){
  if(!cnt) return `<div class="apissue"><div class="aprow" style="grid-template-columns:1fr auto;gap:16px;cursor:default"><div style="display:flex;align-items:center;gap:10px"><span class="dotb" style="background:var(--ok)"></span><span style="font-size:14px;font-weight:600;color:var(--muted)">${label}</span></div><span class="qd" style="color:var(--ok);white-space:nowrap;font-weight:600">&#10003; none</span></div></div>`;
  var body=(items||[]).map(function(x){var href=(x.path&&x.path.indexOf('http')===0)?x.path:((D.origin||'')+x.path);return `<span class="b"><span class="dotb" style="background:${color}"></span><a href="${esc(href)}" target="_blank">${esc(x.path)}</a>${x.metric?' <span class="qd" style="font-family:var(--mono);font-size:12px;color:#8b8b81">'+x.metric+'</span>':''}</span>`;}).join('');
  return `<div class="apissue"><div class="aprow" style="grid-template-columns:1fr auto;gap:16px;cursor:pointer" onclick="tgl('${id}')"><div style="display:flex;flex-direction:column;gap:5px;min-width:0"><div style="display:flex;align-items:center;gap:10px"><span class="dotb" style="background:${color}"></span><span style="font-size:14px;font-weight:600">${label} <span class="egcar">&#9662;</span></span></div><div class="qd" style="line-height:1.5;padding-left:17px">${desc}</div></div><span style="font-size:14px;font-weight:700;color:${color};white-space:nowrap;text-align:right">${cnt} page${cnt>1?'s':''}</span></div><div id="${id}" class="apurls" style="padding-left:17px">${body}</div></div>`;}
function bull(u,c){return `<span class="b"><span class="dotb" style="background:${c}"></span><a href="${esc(u)}" target="_blank">${rel(u)}</a></span>`}
function urlList(id,i){return `<div id="${id}" class="apurls">${[...i.bad.map(u=>bull(u,'#db0632')),...i.warn.map(u=>bull(u,'#8b8b81'))].join('')||'<span class="qd">No affected pages.</span>'}</div>`}
function pdet(id){
 if(SITEIDS.has(id)){const sc=(D.site_checks||[]).find(c=>c.id==id)||{};const cl=sc.status=='good'?'ok':sc.status=='warn'?'wn':'er';const _u=sc.urls||[];const _l=_u.length?'<div class="egh" style="margin-top:8px">Pages ('+_u.length+')</div>'+_u.map(u=>'<span class="egp"><span class="dotb" style="background:#f2f0e4"></span><a href="'+esc(u)+'" target="_blank">'+rel(u)+'</a></span>').join(''):'';return `<div id="pd_${id}" class="engdet"><span class="rst ${cl}">Site-wide check: ${sc.status||'n/a'}</span> <span class="qd">${esc(sc.detail||'')}</span>${_l}</div>`;}
 const ok=D.pages.filter(p=>p.status==200), dcx=s=>s=='good'?'#f2f0e4':s=='warn'?'#8b8b81':'#db0632';
 const rr=ok.map(p=>[p,p.cs[id]]).filter(x=>x[1]&&x[1]!='na'&&x[1]!='info');
 if(!rr.length)return `<div id="pd_${id}" class="engdet"><span class="qd">No applicable pages for this check.</span></div>`;
 const fl=rr.filter(x=>x[1]!='good'),ps=rr.filter(x=>x[1]=='good');
 const ln=x=>`<span class="egp"><span class="dotb" style="background:${dcx(x[1])}"></span><a href="${esc(x[0].url)}" target="_blank">${rel(x[0].url)}</a></span>`;
 return `<div id="pd_${id}" class="engdet">${fl.length?'<div class="egh">Failing here ('+fl.length+')</div>'+fl.map(ln).join(''):''}${ps.length?'<div class="egh"'+(fl.length?' style="margin-top:10px"':'')+'>Passing ('+ps.length+')</div>'+ps.map(ln).join(''):''}</div>`}

function owChip(i){var MN="font-family:'IBM Plex Mono',monospace";var oc=({dev:'#a8a495',content:'#f2f0e4',config:'#a8a495'})[i.owner]||'#f2f0e4';var ol=function(o){return ({dev:'Dev',content:'Content',config:'Config'})[o]||'Content';};return '<span style="'+MN+';font-size:11px;color:'+oc+';border:1px solid '+oc+'44;border-radius:3px;padding:1px 5px;white-space:nowrap;flex:none">'+ol(i.owner)+(i.owner2?' + '+ol(i.owner2):'')+'</span>';}
function plan(){
 const overall=D.overall, act=ACT();
 if(!act.length)return `<div style="padding:40px;text-align:center;color:#8b8b81">Nothing to fix — every scored check passes across the crawl.</div>`;
 const tg=act.reduce((a,i)=>a+gpos(i),0), proj=(D.proj_all!=null?D.proj_all:Math.min(100,overall+tg));
 const totalG=Math.max(0,proj-overall);
 const affS=new Set();act.forEach(i=>{i.bad.forEach(u=>affS.add(u));i.warn.forEach(u=>affS.add(u))});
 const edits=act.reduce((a,i)=>a+i.count,0), affp=Math.min(D.pages_crawled||affS.size,affS.size);
 const instN=((D.totals||{}).warn||0)+((D.totals||{}).bad||0), P=D.pages_crawled;
 const biggest=act.filter(i=>gpos(i)>=2);
 const worth=act.filter(i=>gpos(i)==1);
 const nostand=act.filter(i=>gpos(i)==0);
 const gsum=a=>a.reduce((s,i)=>s+gpos(i),0);
 const bG=gsum(biggest),wG=gsum(worth),standalone=Math.min(totalG,bG+wG),compound=Math.max(0,totalG-standalone);
 const ordered=[...biggest,...worth,...nostand]; window._ordered=ordered; window._rk={}; ordered.forEach((i,x)=>window._rk[i.id]=x+1);
 const MN="font-family:'IBM Plex Mono',monospace";
 const denom=Math.max(totalG,bG+wG)||1;
 const bar=`<div style="display:flex;height:10px;border-radius:0;overflow:hidden;gap:2px"><div style="width:${(100*bG/denom).toFixed(1)}%;background:#f2f0e4"></div><div style="width:${(100*wG/denom).toFixed(1)}%;background:#f2f0e4;opacity:0.45"></div><div style="flex:1;background:#262620"></div></div>`;
 const header=`<div style="background:#191914;border:1px solid #2a2a24;border-radius:0;padding:24px;display:flex;gap:30px;flex-wrap:wrap;align-items:center">
   <div style="display:flex;align-items:center;gap:20px">
     <div style="display:flex;flex-direction:column;gap:6px"><div style="${MN};font-size:12px;letter-spacing:0.16em;color:#8b8b81">TODAY</div><div style="font-size:44px;line-height:0.85;font-variation-settings:'wght' 600;color:#8b8b81">${overall}</div></div>
     <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="#8b8b81" stroke-width="2"><path d="M4 12h15"></path><path d="M14 6l6 6-6 6"></path></svg>
     <div style="display:flex;flex-direction:column;gap:6px"><div style="${MN};font-size:12px;letter-spacing:0.16em;color:#8b8b81">ALL ${act.length} APPLIED</div><div style="display:flex;align-items:baseline;gap:8px"><div style="font-size:44px;line-height:0.85;font-variation-settings:'wght' 700;color:#FFFFFF">${proj}</div><div style="${MN};font-size:14px;color:#8b8b81">/100</div><div style="${MN};font-size:14px;color:#f2f0e4">+${totalG}</div></div></div>
   </div>
   <div style="width:1px;align-self:stretch;background:#2a2a24"></div>
   <div style="flex:1;min-width:280px;display:flex;flex-direction:column;gap:8px">
     <div style="${MN};font-size:12px;letter-spacing:0.16em;color:#8b8b81">THE PLAN</div>
     <div style="font-size:14px;line-height:1.6;color:#8b8b81"><span style="color:#f2f0e4">${act.length} fixes</span> clear <span style="color:#f2f0e4">${instN} failing checks</span> across ${affp} of ${P} pages · ${edits} page-edits. Ranked by standalone impact below.</div>
   </div>
 </div>`;
 const infonote=`<div style="display:flex;align-items:flex-start;gap:11px;padding:14px 18px;background:#191914;border:1px solid #2a2a24;border-radius:0"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="#f2f0e4" stroke-width="2" style="flex:0 0 15px;margin-top:1px"><circle cx="12" cy="12" r="9"></circle><path d="M12 8v.01M12 11v5"></path></svg><div style="font-size:13.5px;line-height:1.6;color:#8b8b81">Each gain below is <span style="color:#f2f0e4">standalone</span> — what that one fix is worth on its own if applied to every affected page. Fixes overlap on a page, so they do <span style="color:#f2f0e4">not simply add up</span>: applying the whole plan lifts the score to <span style="color:#f2f0e4">${proj}/100</span>, which is why that is less than the individual gains summed.</div></div>`;
 const ebar=(eff)=>{const e=(eff||"").toLowerCase(); const spec=(e=="low"||e=="template")?[1,"#f2f0e4"]:e=="high"?[3,"#db0632"]:[2,"#ff4d6d"]; let bars="";for(let k=0;k<3;k++)bars+=`<div style="width:4px;height:9px;background:${k<spec[0]?spec[1]:'#2a2a24'}"></div>`; const lbl=e=="low"?"low":e=="high"?"high":"med"; return `<div style="display:flex;align-items:center;gap:6px"><div style="display:flex;gap:2px">${bars}</div><div style="${MN};font-size:12.5px;color:#8b8b81">${lbl}</div></div>`;};
 const bigRow=(i,x)=>{const eg=Object.entries(i.gain_engines||{}).filter(a=>a[1]>0).sort((a,c)=>c[1]-a[1]).slice(0,3);
   const _ow=function(o){return ({dev:'dev',content:'content',config:'config'})[o]||'content';};
   return `<div style="display:grid;grid-template-columns:30px minmax(0,1fr) 56px;gap:20px;padding:28px 22px;border-top:1px solid #242420;align-items:start;cursor:pointer" onclick="tgl('pd_${i.id}')"><div style="${MN};font-size:13px;font-weight:700;color:#ff4d6d">${x<10?'0'+x:x}</div><div style="display:flex;flex-direction:column;gap:11px;min-width:0"><div style="${MN};font-size:11px;font-weight:600;color:#96968c">${(i.pillar||'').toLowerCase()} &middot; ${(i.effort||'').toLowerCase()} &middot; ${i.count} page${i.count==1?'':'s'}</div><div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><div style="font-size:17px;font-weight:800;letter-spacing:-.5px;line-height:1.25;color:#f2f0e4">${esc(i.label)}</div><div style="${MN};font-size:11px;font-weight:600;color:#96968c">${esc(i.ch||'')}${i.owner?' &middot; '+_ow(i.owner)+(i.owner2?' + '+_ow(i.owner2):''):''}</div></div><div style="font-size:14px;font-weight:500;line-height:1.6;color:#b6b2a4">${esc(i.ev)}</div>${(i.fix_deep||i.fix)?`<div style="display:flex;flex-direction:column;gap:7px;padding:15px 18px;background:#1c1c16;border-left:3px solid #db0632"><div style="${MN};font-size:10px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;color:#ff4d6d">How to fix</div><div style="font-size:14px;font-weight:600;line-height:1.6;color:#f2f0e4">${esc(i.fix_deep||i.fix)}</div></div>`:''}${eg.length?`<div style="display:flex;flex-wrap:wrap;gap:8px;${MN};font-size:11px;font-weight:600">${eg.map(a=>`<div style="color:#b6b2a4;border:1px solid #35352d;padding:6px 10px">${a[0]} +${a[1]}</div>`).join('')}</div>`:''}<div style="margin-top:2px">${pdet(i.id)}</div></div><div style="font-size:26px;font-weight:800;letter-spacing:-1px;text-align:right;color:#db0632">+${i.gain_overall}</div></div>`;};
 const bigTable=biggest.length?`<div style="background:#191914;border:1px solid #2a2a24;border-radius:0;overflow:hidden"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:20px 22px 16px"><div style="display:flex;align-items:baseline;gap:16px;flex-wrap:wrap"><div style="display:flex;align-items:center;gap:9px"><div style="width:7px;height:7px;background:#db0632;flex:none"></div><div style="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#f2f0e4">Biggest movers</div></div><div style="${MN};font-size:11px;font-weight:600;color:#96968c">${biggest.length} fix${biggest.length==1?'':'es'} &middot; biggest impact</div></div><div style="font-size:11px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;color:#ff4d6d">Do these this month</div></div><div style="display:flex;align-items:baseline;justify-content:space-between;gap:14px;padding:10px 22px;background:#14140f;border-top:1px solid #242420;${MN};font-size:11px;font-weight:600;letter-spacing:.05em;color:#96968c"><div>fix &middot; question &middot; effort &middot; pages</div><div>gain</div></div>${biggest.map((i,x)=>bigRow(i,x+1)).join("")}</div>`:"";
 const wRow=(i,x)=>`<div style="border-top:1px solid #242420;cursor:pointer" onclick="tgl('pd_${i.id}')"><div style="display:flex;align-items:center;gap:14px;padding:12px 22px"><div style="width:22px;${MN};font-size:13px;color:#8b8b81">${x}</div><div style="flex:1;min-width:0;font-size:14px;color:#FFFFFF">${esc(i.label)}</div>${owChip(i)}<div style="width:78px;${MN};font-size:12.5px;color:#8b8b81">${(i.pillar||"").toUpperCase()}</div><div style="width:74px;${MN};font-size:12.5px;color:#8b8b81">${(i.effort||"").toLowerCase()}</div><div style="width:62px;text-align:right;${MN};font-size:13px;color:#f2f0e4">${i.count}</div><div style="width:48px;text-align:right;font-size:17px;font-variation-settings:'wght' 700;color:${i.gain_overall>0?'#f2f0e4':'#6b6b65'}">${i.gain_overall>0?'+'+i.gain_overall:'&lt;+1'}</div></div>${(i.fix_deep||i.fix)?`<div style="padding:0 22px 8px;font-size:13px;line-height:1.5;color:#8b8b81"><span style="${MN};font-size:11px;letter-spacing:0.1em;color:#f2f0e4">FIX</span> ${esc(i.fix_deep||i.fix)}</div>`:""}<div style="padding:0 22px 4px">${pdet(i.id)}</div></div>`;
 const wStart=biggest.length+1;
 const nsStart=biggest.length+worth.length+1;
 const wTable=worth.length?`<div style="background:#191914;border:1px solid #2a2a24;border-radius:0;overflow:hidden"><div style="display:flex;align-items:center;gap:12px;padding:18px 22px 14px"><div style="width:9px;height:9px;border-radius:2px;background:#f2f0e4;opacity:0.45"></div><div style="font-size:15px;font-variation-settings:'wght' 700;color:#FFFFFF">Worth doing</div><div style="${MN};font-size:13px;color:#8b8b81">${worth.length} fixes · about +1 each</div><div style="flex:1"></div><div style="font-size:13.5px;color:#8b8b81">Structure for retrieval</div></div>${worth.map((i,x)=>wRow(i,wStart+x)).join("")}</div>`:"";
 const nsTable=nostand.length?`<div style="background:#191914;border:1px solid #2a2a24;border-radius:0;overflow:hidden"><div style="display:flex;align-items:center;gap:12px;padding:18px 22px 14px"><div style="width:9px;height:9px;border-radius:2px;background:#2a2a24;border:1px solid #3A3A3A"></div><div style="font-size:15px;font-variation-settings:'wght' 700;color:#FFFFFF">Under +1 on their own</div><div style="${MN};font-size:13px;color:#8b8b81">${nostand.length} fixes · &lt;+1 each</div><div style="flex:1"></div><div style="font-size:13.5px;color:#8b8b81">Worth doing after the above, they compound</div></div>${nostand.map((i,x)=>wRow(i,nsStart+x)).join("")}</div>`:"";
 return `<div style="display:flex;gap:22px;flex-wrap:wrap;align-items:flex-start"><div style="flex:1;min-width:600px;display:flex;flex-direction:column;gap:22px">${header}${infonote}${bigTable}${wTable}${nsTable}</div><div style="flex:0 0 372px;min-width:0;display:flex;flex-direction:column;gap:14px">${roadmapCard()}${effortCard(act,edits)}<div onclick="exportDevPlan()" style="background:#f2f0e4;color:#14140f;font-size:14px;font-variation-settings:'wght' 600;padding:13px;border-radius:0;text-align:center;cursor:pointer">Export dev checklist (Markdown)</div><div onclick="exportPlan()" style="background:transparent;color:#f2f0e4;border:1px solid #2a2a24;font-size:13.5px;padding:11px;border-radius:0;text-align:center;cursor:pointer">Export data (CSV)</div><div style="display:flex;align-items:center;gap:9px;padding:12px 18px;border:1px solid #2a2a24;border-radius:0"><div style="width:7px;height:7px;border-radius:50%;background:#f2f0e4"></div><div style="font-size:13px;color:#8b8b81">Crawl ran locally. No page data left this machine.</div></div></div></div>`;
}
function roadmapCard(){
 const MN="font-family:'IBM Plex Mono',monospace";
 const info={1:["PHASE 01 · DAYS 0–30","Foundation","Biggest score movement per hour, do these first"],2:["PHASE 02 · DAYS 30–60","Structure","The next tier of the ranked plan"],3:["PHASE 03 · DAYS 60–90","Polish","Lower-yield or heavier lifts, then re-crawl and compare"]};
 let cum=D.overall,out="";
 [1,2,3].forEach(ph=>{const ids=(D.plan_phases&&D.plan_phases[ph])||[];if(!ids.length)return;
   const items=ids.map(id=>D.issues.find(y=>y.id==id)).filter(Boolean);
   const g=items.reduce((a,i)=>a+gpos(i),0);cum=Math.min(100,cum+g);
   const border=ph<3?"border-bottom:1px solid #242420":"";
   out+=`<div style="padding:22px 24px;display:flex;flex-direction:column;gap:14px;${border}"><div style="display:flex;align-items:flex-start;gap:12px"><div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:4px"><div style="${MN};font-size:12px;letter-spacing:0.14em;color:${ph==1?'#f2f0e4':'#8b8b81'}">${info[ph][0]}</div><div style="font-size:14.5px;font-variation-settings:'wght' 600;color:#FFFFFF">${info[ph][1]}</div><div style="font-size:13px;color:#8b8b81">${info[ph][2]}</div></div><div style="display:flex;flex-direction:column;align-items:flex-end"><div style="font-size:26px;line-height:0.9;font-variation-settings:'wght' 700;color:#FFFFFF">${cum}</div><div style="${MN};font-size:12.5px;color:#f2f0e4">+${g}</div></div></div><div style="display:flex;gap:5px;flex-wrap:wrap">${items.slice(0,6).map(i=>`<div style="${MN};font-size:12px;background:#242420;color:#8b8b81;padding:3px 6px;border-radius:3px">${esc(i.label.split("(")[0].split(",")[0].trim())}</div>`).join("")}</div></div>`;
 });
 return `<div style="background:#191914;border:1px solid #2a2a24;border-radius:0;overflow:hidden"><div style="padding:20px 22px 16px;display:flex;flex-direction:column;gap:5px;border-bottom:1px solid #2a2a24"><div style="font-size:16px;font-variation-settings:'wght' 700;color:#FFFFFF;letter-spacing:-0.01em">90-day roadmap</div><div style="font-size:13.5px;color:#8b8b81">Sequenced so each phase makes the next cheaper.</div></div>${out}</div>`;
}
function effortCard(act,edits){
 const MN="font-family:'IBM Plex Mono',monospace";
 const bk={template:["Template-level fixes",0,0],copy:["Per-page copy work",0,0],meta:["One-off metadata edits",0,0]};
 act.forEach(i=>{const t=TTYPE[i.id]||"copy";bk[t][1]++;bk[t][2]+=gpos(i)});
 const tplN=bk.template[1], tplEdits=act.filter(i=>TTYPE[i.id]=="template").reduce((a,i)=>a+i.count,0);
 const rows=Object.keys(bk).filter(k=>bk[k][1]).map(k=>`<div style="display:flex;align-items:center;gap:12px"><div style="flex:1;min-width:0;font-size:13.5px;color:#f2f0e4">${bk[k][0]}</div><div style="${MN};font-size:13px;color:#8b8b81">${bk[k][1]} fixes</div><div style="width:34px;text-align:right;${MN};font-size:13.5px;color:#f2f0e4">+${bk[k][2]}</div></div>`).join("");
 const note=tplN?`<div style="display:flex;align-items:flex-start;gap:9px;padding:11px 13px;background:#1e1e18;border-radius:0"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="#f2f0e4" stroke-width="2.2" style="flex:0 0 14px;margin-top:1px"><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"></path></svg><div style="font-size:13px;line-height:1.5;color:#a8a495">Start with the ${tplN} template fixes — they touch <span style="color:#FFFFFF">${tplEdits} of ${edits}</span> page edits in one change.</div></div>`:"";
 return `<div style="background:#191914;border:1px solid #2a2a24;border-radius:0;padding:20px 22px;display:flex;flex-direction:column;gap:14px"><div style="font-size:14px;font-variation-settings:'wght' 700;color:#FFFFFF;letter-spacing:-0.01em">Effort at a glance</div>${rows}${note}</div>`;
}
function planR(){
 const overall=D.overall, act=ACT();
 if(!act.length)return `<div style="padding:60px 44px;text-align:center;color:#a8a495">Nothing to fix — every scored check passes across the crawl.</div>`;
 const tg=act.reduce((a,i)=>a+gpos(i),0), proj=(D.proj_all!=null?D.proj_all:Math.min(100,overall+tg));
 const totalG=Math.max(0,proj-overall);
 const affS=new Set();act.forEach(i=>{i.bad.forEach(u=>affS.add(u));i.warn.forEach(u=>affS.add(u))});
 const edits=act.reduce((a,i)=>a+i.count,0), affp=Math.min(D.pages_crawled||affS.size,affS.size);
 const instN=((D.totals||{}).warn||0)+((D.totals||{}).bad||0), P=D.pages_crawled;
 const biggest=act.filter(i=>gpos(i)>=2), worth=act.filter(i=>gpos(i)==1), nostand=act.filter(i=>gpos(i)==0);
 const ordered=[...biggest,...worth,...nostand]; window._ordered=ordered; window._rk={}; ordered.forEach((i,x)=>window._rk[i.id]=x+1);
 const MM="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81";
 const HL="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#f2f0e4";
 const sq='<div style="width:7px;height:7px;background:#db0632;flex:none"></div>';
 const header=`<section style="display:flex;flex-direction:column;gap:20px;padding:34px 0 32px;border-bottom:1px solid #2a2a24"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap"><div style="display:flex;align-items:center;gap:9px">${sq}<div style="${HL}">Action plan</div></div><div style="${MM}">${act.length} fixes &middot; ${affp} pages</div></div><div style="display:flex;align-items:flex-end;gap:26px;flex-wrap:wrap"><div style="display:flex;flex-direction:column;gap:6px"><div style="${MM}">today</div><div style="font-size:56px;font-weight:800;letter-spacing:-3px;line-height:.78;color:#f2f0e4">${overall}</div></div><div style="width:26px;height:3px;background:#55534a;margin-bottom:14px"></div><div style="display:flex;flex-direction:column;gap:6px"><div style="${MM}">all ${act.length} applied</div><div style="font-size:56px;font-weight:800;letter-spacing:-3px;line-height:.78;color:#f2f0e4">${proj}</div></div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:14px;font-weight:800;color:#ff4d6d;margin-bottom:8px">+${totalG}</div><div style="flex:1;min-width:240px;font-size:14px;font-weight:400;line-height:1.55;color:#a8a495;margin-bottom:4px">${act.length} fixes clear <span style="color:#f2f0e4;font-weight:800">${instN} failing checks</span> across ${affp} of ${P} pages &middot; ${edits} page edits. Ranked by standalone impact below.</div></div><div style="position:relative;height:8px;background:#262620"><div style="position:absolute;left:0;top:0;width:${overall}%;height:8px;background:#db0632"></div><div style="position:absolute;left:${overall}%;top:0;width:${Math.max(0,proj-overall)}%;height:8px;background:#55534a"></div><div style="position:absolute;left:70%;top:-5px;width:2px;height:18px;background:#f2f0e4"></div></div><div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;${MM}"><div>0</div><div>standard 70</div><div>100</div></div></section>`;
 const readit=`<section style="display:flex;align-items:baseline;gap:22px;padding:18px 0;border-bottom:1px solid #2a2a24"><div style="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#ff4d6d;flex:none">Read it as</div><div style="font-size:13px;font-weight:400;line-height:1.55;color:#a8a495">Each gain below is standalone &mdash; what that one fix is worth on its own if applied to every affected page. Fixes overlap on a page, so they <span style="color:#f2f0e4;font-weight:800">do not simply add up</span>: applying the whole plan lifts the score to ${proj}/100, which is why that is less than the individual gains summed.</div></section>`;
 const row=(i,x)=>{const eg=Object.entries(i.gain_engines||{}).filter(a=>a[1]>0).sort((a,c)=>c[1]-a[1]);
   const chips=eg.length?`<div style="display:flex;flex-wrap:wrap;gap:6px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:600">${eg.map(a=>`<div style="color:#a8a495;border:1px solid #2a2a24;padding:5px 8px">${a[0]} +${a[1]}</div>`).join('')}</div>`:'';
   const howto=(i.fix_deep||i.fix)?`<div style="display:flex;flex-direction:column;gap:6px;padding:11px 14px;border-left:2px solid #db0632"><div style="${MM};flex:none">how to fix</div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#f2f0e4">${esc(i.fix_deep||i.fix)}</div></div>`:'';
   const gv=i.gain_overall>0?('+'+i.gain_overall):'&lt;+1';
   return `<div onclick="tgl('pd_${i.id}')" style="display:grid;grid-template-columns:22px minmax(0,1fr) 48px;gap:14px;padding:20px 0;border-bottom:1px solid #2a2a24;align-items:start;cursor:pointer"><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:#ff4d6d">${x<10?'0'+x:x}</div><div style="display:flex;flex-direction:column;gap:11px;min-width:0"><div style="${MM}">${(i.pillar||'').toLowerCase()} &middot; ${(i.effort||'').toLowerCase()} &middot; ${i.count}</div><div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><div style="font-size:14px;font-weight:800;letter-spacing:-.3px">${esc(i.label)}</div><div style="${MM}">${esc(i.ch||'')}${i.owner?' &middot; '+esc(i.owner)+(i.owner2?' + '+esc(i.owner2):''):''}</div></div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495">${esc(i.ev||'')}</div>${howto}${chips}<div>${pdet(i.id)}</div></div><div style="font-size:20px;font-weight:800;letter-spacing:-.8px;text-align:right;color:#ff4d6d">${gv}</div></div>`;};
 const sect=(title,note,rightnote,items,start)=>items.length?`<section style="display:flex;flex-direction:column;padding:30px 0 0"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap;padding-bottom:18px"><div style="display:flex;align-items:baseline;gap:16px"><div style="display:flex;align-items:center;gap:9px">${sq}<div style="${HL}">${title}</div></div><div style="${MM}">${note}</div></div><div style="font-size:12px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;color:#ff4d6d">${rightnote}</div></div><div style="display:flex;align-items:baseline;justify-content:space-between;gap:14px;padding:0 0 10px;${MM};border-bottom:1px solid #2a2a24"><div>fix &middot; question &middot; effort &middot; pages</div><div>gain</div></div>${items.map((i,k)=>row(i,start+k)).join('')}</section>`:'';
 const wStart=biggest.length+1,nStart=biggest.length+worth.length+1;
 const left=`${header}${readit}${sect('Biggest movers',biggest.length+' fixes &middot; biggest impact','Do these this month',biggest,1)}${sect('Structure for retrieval',worth.length+' fixes &middot; about +1 each','Worth doing',worth,wStart)}${sect('They compose',nostand.length+' fixes &middot; under +1 each','After the above',nostand,nStart)}`;
 const exportBox=`<div style="display:flex;flex-direction:column;gap:10px;padding:22px 0 0 24px;border-top:1px solid #2a2a24"><div onclick="exportDevPlan()" style="font-size:12.5px;font-weight:800;letter-spacing:1.8px;text-transform:uppercase;color:#f2f0e4;background:#db0632;padding:14px 16px;text-align:center;cursor:pointer">Export dev checklist</div><div onclick="exportPlan()" style="font-size:12.5px;font-weight:800;letter-spacing:1.8px;text-transform:uppercase;color:#a8a495;border:1px solid #2a2a24;padding:13px 16px;text-align:center;cursor:pointer">Export data (CSV)</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:600;line-height:1.55;color:#8b8b81;padding-top:8px">Crawl ran locally. No page data left this machine.</div></div>`;
 return `<div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(232px,320px);gap:0"><div style="display:flex;flex-direction:column;min-width:0;padding-right:32px">${left}</div><aside style="display:flex;flex-direction:column;min-width:0;align-self:start;padding:34px 0 24px 0;border-left:1px solid #2a2a24">${roadmapR()}${effortR(act,edits)}${exportBox}</aside></div>`;
}
function roadmapR(){
 const MM="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81";
 const info={1:["phase 01 &middot; days 0&#8211;30","Foundation","Biggest score movement per hour, do these first."],2:["phase 02 &middot; days 30&#8211;60","Structure","The next tier of the ranked plan."],3:["phase 03 &middot; days 60&#8211;90","Polish","Lower-yield or heavier lifts, then re-crawl and compare."]};
 let cum=D.overall,out="";
 [1,2,3].forEach(ph=>{const ids=(D.plan_phases&&D.plan_phases[ph])||[];if(!ids.length)return;
   const items=ids.map(id=>D.issues.find(y=>y.id==id)).filter(Boolean);
   const g=items.reduce((a,i)=>a+gpos(i),0);cum=Math.min(100,cum+g);
   out+=`<div style="display:flex;flex-direction:column;gap:12px;padding:20px 0 20px 24px;border-top:1px solid #2a2a24"><div style="display:flex;align-items:flex-start;justify-content:space-between;gap:14px"><div style="display:flex;flex-direction:column;gap:5px;min-width:0"><div style="${MM}">${info[ph][0]}</div><div style="font-size:15px;font-weight:800;letter-spacing:-.4px">${info[ph][1]}</div></div><div style="display:flex;flex-direction:column;align-items:flex-end;flex:none"><div style="font-size:26px;font-weight:800;letter-spacing:-1.2px;line-height:.85">${cum}</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:800;color:#ff4d6d">+${g}</div></div></div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495">${info[ph][2]}</div><div style="display:flex;flex-wrap:wrap;gap:6px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:600">${items.slice(0,6).map(i=>`<div style="color:#a8a495;border:1px solid #2a2a24;padding:5px 8px">${esc(i.label.split("(")[0].split(",")[0].trim())}</div>`).join('')}</div></div>`;
 });
 if(!out)return '';
 return `<div style="display:flex;flex-direction:column;gap:8px;padding:0 0 20px 24px"><div style="display:flex;align-items:center;gap:9px"><div style="width:7px;height:7px;background:#db0632;flex:none"></div><div style="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#f2f0e4">90-day roadmap</div></div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495">Sequenced so each phase makes the next cheaper.</div></div>${out}`;
}
function effortR(act,edits){
 const bk={template:["Template-level fixes",0,0],copy:["Per-page copy work",0,0],meta:["One-off metadata edits",0,0]};
 act.forEach(i=>{const t=TTYPE[i.id]||"copy";bk[t][1]++;bk[t][2]+=gpos(i)});
 const tplN=bk.template[1], tplEdits=act.filter(i=>TTYPE[i.id]=="template").reduce((a,i)=>a+i.count,0);
 const rows=Object.keys(bk).filter(k=>bk[k][1]).map(k=>`<div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px"><div style="font-size:13px;font-weight:800">${bk[k][0]}</div><div style="display:flex;align-items:baseline;gap:12px;flex:none"><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81">${bk[k][1]} fixes</div><div style="font-size:14px;font-weight:800;color:#ff4d6d">+${bk[k][2]}</div></div></div>`).join("");
 const note=tplN?`<div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495;padding-top:4px;border-top:1px solid #2a2a24">Start with the ${tplN} template fixes &mdash; they touch <span style="color:#ff4d6d;font-weight:800">${tplEdits} of ${edits}</span> page edits in one change.</div>`:"";
 return `<div style="display:flex;flex-direction:column;gap:14px;padding:22px 0 22px 24px;border-top:1px solid #2a2a24"><div style="display:flex;align-items:center;gap:9px"><div style="width:7px;height:7px;background:#db0632;flex:none"></div><div style="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#f2f0e4">Effort at a glance</div></div><div style="display:flex;flex-direction:column;gap:10px">${rows}</div>${note}</div>`;
}
function exportPlan(){const rows=[['rank','fix','severity','pillar','chapter','effort','pages_affected','gain_overall',...ECOLS.map(e=>'gain_'+e.split(' ')[0]),'fix_instruction','why_it_matters','affected_pages']];
 (window._ordered||D.issues).forEach((i,x)=>rows.push([x+1,i.label,i.severity,i.pillar,i.ch,i.effort,i.count,i.gain_overall,...ECOLS.map(e=>(i.gain_engines||{})[e]||0),i.fix||'',i.ev||'',[...(i.bad||[]),...(i.warn||[])].join(' | ')]));
 dl(_fn('action-plan.csv'),rows)}

function ifilt(v){window._ifilter=v;render()}
function issuesView(){
 const iss=D.issues.filter(i=>i.pillar!='Info');
 const instN=((D.totals||{}).warn||0)+((D.totals||{}).bad||0);
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
   ${istat('CHECKS FAILING',`<span class="inum">${iss.length}</span>`,`of ${totalChecks} checks &middot; ${instN} instances`)}
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
 h+=passRateBars();
 const QL={Known:'KNOWN &mdash; DO THEY KNOW YOU?',Findable:'FINDABLE &mdash; CAN THEY FIND YOUR ANSWER?',Trusted:'TRUSTED &mdash; DO THEY TRUST YOU?'};
 ['Known','Findable','Trusted'].forEach(p=>{
   const gs=iss.filter(i=>i.pillar==p&&show(i));if(!gs.length)return;
   const avail=iss.filter(i=>i.pillar==p).reduce((a,i)=>a+gpos(i),0);
   const failing=iss.filter(i=>i.pillar==p).length;
   const bc=D.pillars[p]<70?'#db063233':'#ffffff14';   // tier tint by the pillar's own rating
   h+=`<section class="aptier"><div class="aptierh"><span class="dotb" style="width:9px;height:9px;margin:0;background:${D.pillars[p]>=70?'#f2f0e4':'#db0632'}"></span><h3>${QL[p]}</h3><span class="meta">score ${D.pillars[p]} &middot; ${failing} check${failing>1?'s':''} failing &middot; +${avail} available${p==weakest?' &middot; weakest pillar':''}</span></div><div class="apbox" style="border-color:${bc}">`;
   gs.forEach(i=>h+=issueRow(i,P));
   h+=`</div></section>`});
 h+=`</div><div class="apside">`+worstPagesCard()+oneChangeCard(errs,iss)+`<div class="dashnote"><span class="dotb" style="background:var(--ok)"></span>${passed} checks passed on every page &mdash; not listed here.</div></div></div>`;
 return h}
function issueRow(i,P){
 const bad=i.severity=='bad';
 const w=Math.min(100,Math.round(100*i.count/(P||1)));
 const dc=w>30?'#db0632':'#f2f0e4';   // colour by how widely the check fails (rating), not a flat severity grey
 const gv=i.gain_overall>0?('+'+i.gain_overall):'—', gc=i.gain_overall>0?'var(--ok)':'var(--muted)';
 let body;
 if(bad){body=`<div style="display:flex;flex-direction:column;gap:5px;min-width:0">
   <div style="display:flex;align-items:center;gap:10px"><span class="dotb" style="background:${dc}"></span><span style="font-size:15px;font-weight:700">${esc(i.label)} <span class="egcar">▾</span></span><span style="font-size:12.5px;color:#8b8b81">${i.ch}</span></div>
   <div class="qd" style="line-height:1.5;padding-left:17px">${esc(i.ev)}</div>
   <div style="font-size:13px;color:#a8a495;line-height:1.5;padding-left:17px"><span style="color:#fff;font-weight:600">Fix</span> ${esc(i.fix)}</div></div>`}
 else{body=`<div style="display:flex;flex-direction:column;gap:4px;min-width:0">
   <div style="display:flex;align-items:center;gap:10px"><span class="dotb" style="background:${dc}"></span><span style="font-size:14px;font-weight:600">${esc(i.label)} <span class="egcar">▾</span></span><span style="font-size:12.5px;color:#8b8b81">${i.ch}</span></div>
   <div class="qd" style="line-height:1.5;padding-left:17px"><span style="font-weight:600">Fix</span> ${esc(i.fix||i.ev)}</div></div>`}
 return `<div class="apissue"><div class="aprow" style="grid-template-columns:1fr 210px 66px;gap:20px;cursor:pointer" onclick="tgl('pd_${i.id}')">
   ${body}
   <div style="display:flex;flex-direction:column;gap:6px"><div style="height:8px;border-radius:4px;background:#262620;overflow:hidden"><div style="width:${w}%;height:100%;background:${dc}"></div></div><div style="font-size:12.5px;color:#8b8b81">${i.count} of ${P} pages affected</div></div>
   <span style="font-size:14px;font-weight:700;color:${gc};text-align:right">${gv}</span>
 </div>${pdet(i.id)}</div>`}
function worstPagesCard(){
 const rows=D.pages.map(p=>({p,f:p.checks.filter(c=>c.status=='bad'||c.status=='warn').length,e:p.checks.filter(c=>c.status=='bad').length})).filter(x=>x.f>0).sort((a,b)=>b.f-a.f||a.p.score-b.p.score).slice(0,5);
 if(!rows.length)return '';
 const out=rows.map(x=>`<div class="wpage" onclick="go('Pages')"><span class="s" style="color:${bcol(x.p.score)}">${x.p.score}</span><span style="flex:1;min-width:0;display:flex;flex-direction:column;gap:2px"><span style="font-size:14px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${rel(x.p.url)}</span><span class="qd">${x.f} checks failing &middot; ${x.e} error${x.e==1?'':'s'}</span></span></div>`).join('');
 return `<div class="rmc"><div class="rmch" style="display:flex;justify-content:space-between;align-items:baseline"><h3>Worst Pages</h3><span class="qd">by failing checks</span></div>${out}</div>`}
function oneChangeCard(errs,iss){
 const tplErr=errs.filter(i=>TTYPE[i.id]=='template');
 const totInst=iss.reduce((a,i)=>a+i.count,0);
 const tplInst=iss.filter(i=>TTYPE[i.id]=='template').reduce((a,i)=>a+i.count,0);
 const top=iss.filter(i=>TTYPE[i.id]=='template').sort((a,b)=>b.count-a.count).slice(0,3);
 const lead=tplErr.length?`${tplErr.length} of the ${errs.length} error${errs.length==1?'':'s'} ${tplErr.length==1?'is':'are'} template-level. Fixing the page template clears ${tplInst} of the ${totInst} affected page instances without touching copy.`:`Most fixes here are per-page copy work &mdash; work through the action plan in priority order.`;
 const rows=top.map(i=>`<div class="rowsb"><span style="color:#a8a495">${esc(i.label.split('(')[0].trim())}</span><span style="color:#fff;font-weight:600">${i.count} page${i.count>1?'s':''}</span></div>`).join('');
 return `<div class="card2"><h3>One Change, Most Pages</h3><div class="qd" style="line-height:1.6">${lead}</div>${rows}<button class="bigbtn" style="border-radius:0;margin-top:2px" onclick="go('Action Plan')">Open the action plan</button></div>`}
function sc(k){if(sortk==k)sortd*=-1;else{sortk=k;sortd=1}render()}
const PBAND=v=>v>=70?['rgba(242,240,228,.16)','#f2f0e4']:['rgba(219,6,50,.16)','#db0632'];
const EABBR={'ChatGPT':'GPT','Perplexity':'PPLX','AI Overviews':'AIO','Gemini':'GEM','Copilot':'CPLT','Claude':'CLDE'};
function pgVal(p,k){if(k=='url')return p.path||p.url;if(k=='Known'||k=='Findable'||k=='Trusted')return p.pillars[k];if(ECOLS.indexOf(k)>=0)return p.engines[k];if(k=='fetch_ms')return p.fetch_ms;return p.score}
function pgFiltered(){const q=(window._pq||'').toLowerCase(),ty=window._ptype||'all',bel=window._pbelow;
 let ps=D.pages.filter(p=>(ty=='all'||p.type==ty)&&(!bel||p.score<70)&&(!q||p.url.toLowerCase().includes(q)));
 const k=sortk||'score';ps.sort((a,b)=>{let x=pgVal(a,k),y=pgVal(b,k);if(typeof x=='string')return(x>y?1:x<y?-1:0)*sortd;return(x-y)*sortd});
 return ps}
const PGCOLS="display:grid;grid-template-columns:44px minmax(220px,1fr) 34px 34px 34px 38px 38px 38px 38px 38px 38px 42px 40px minmax(150px,220px);gap:10px;align-items:center";
const _CBFIX={answer:'lead with a direct answer',selfcontained:'names no subject up front, opens mid-thought',structure:'no heading to anchor it',stats:'no data or sourced number',uniqueness:'near-duplicate of other pages'};
function _blkLo(sub){const ks=Object.keys(sub||{});return ks.length?ks.slice().sort((a,c)=>sub[a]-sub[c])[0]:''}
function pgBlockRow(b){const sc=b.score||0,red=sc<70,lo=_blkLo(b.sub),head=b.heading?esc(b.heading):'(lead paragraph)';const MMx="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81";return `<div style="padding:12px 0;border-top:1px solid #2a2a24"><div style="display:flex;align-items:baseline;gap:11px"><div style="font-size:18px;font-weight:800;letter-spacing:-.6px;line-height:.9;color:${red?'#ff4d6d':'#f2f0e4'};width:28px;flex:none">${sc}</div><div style="font-size:13.5px;font-weight:800;color:#f2f0e4;min-width:0">${head}</div>${red&&lo&&_CBFIX[lo]?`<div style="${MMx};color:#ff4d6d;margin-left:auto;flex:none">${_CBFIX[lo]}</div>`:''}</div><div style="font-size:12.5px;font-weight:400;line-height:1.5;color:#b6b2a4;margin:5px 0 0 39px">${esc(b.snippet||'')}</div></div>`}
function pgDetail(p){const bs=(p.blocks||[]).slice().sort((a,b)=>(b.score||0)-(a.score||0));if(!bs.length)return '';const HLm="font-size:11px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#a8a495";return `<div class="pgdet" style="display:none;padding:14px 0 20px 54px;background:#191914"><div style="${HLm};margin-bottom:2px">Quotable passages &middot; ${bs.length} on this page</div>${bs.map(pgBlockRow).join('')}</div>`}
function pageRow(p){
 const _hasB=(p.blocks||[]).length;
 const fails=p.checks.filter(c=>c.status=='bad'||c.status=='warn');
 const labels=[...fails.filter(c=>c.status=='bad'),...fails.filter(c=>c.status=='warn')].map(c=>esc(c.label)).join(', ');
 const MMc="font-family:'IBM Plex Mono',ui-monospace,monospace;font-weight:600;text-align:right";
 const pl=v=>`<div style="${MMc};font-size:12.5px;color:${v<70?'#ff4d6d':'#a8a495'}">${v}</div>`;
 const ec=v=>`<div style="${MMc};font-size:13px;color:${v<70?'#ff4d6d':'#f2f0e4'}">${v}</div>`;
 return `<div><div style="${PGCOLS};padding:13px 0;border-bottom:1px solid #2a2a24${_hasB?';cursor:pointer':''}"${_hasB?' onclick="pgTog(this)"':''}>
  <div style="font-size:17px;font-weight:800;letter-spacing:-.6px;color:${p.score<70?'#ff4d6d':'#f2f0e4'}">${p.score}</div>
  <div style="display:flex;align-items:baseline;gap:9px;min-width:0"><a href="${esc(p.url)}" target="_blank" onclick="event.stopPropagation()" style="font-size:14px;font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#f2f0e4">${rel(p.url)}</a><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:${_hasB?'#a8a495':'#8b8b81'};flex:none">${esc(p.type)}${_hasB?' &middot; '+_hasB+'&#9656;':''}</div></div>
  ${pl(p.pillars.Known)}${pl(p.pillars.Findable)}${pl(p.pillars.Trusted)}
  ${ECOLS.map(e=>ec(p.engines[e])).join('')}
  <div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:#8b8b81;text-align:right">${(p.fetch_ms/1000).toFixed(1)}s</div>
  <div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${fails.length?'#ff4d6d':'#8b8b81'};text-align:right">${fails.length}</div>
  <div style="font-size:12.5px;font-weight:400;color:#a8a495;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${labels||'all checks pass'}</div>
 </div>${pgDetail(p)}</div>`}
function pgTog(el){var d=el.parentNode.querySelector('.pgdet');if(d)d.style.display=d.style.display=='none'?'block':'none'}
function pgPillsHTML(){const ty=window._ptype||'all',bel=window._pbelow;
 const order=['page','article','listing','product','home'];const types=Object.keys(D.types).sort((a,b)=>{let i=order.indexOf(a),j=order.indexOf(b);return(i<0?9:i)-(j<0?9:j)});
 const chip=(on,red,label,onc)=>`<div onclick="${onc}" style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;cursor:pointer;padding:${red||!on?'6px 10px':'7px 11px'};${red?'color:#ff4d6d;border:1px solid #db0632':on?'color:#14140f;background:#f2f0e4':'color:#a8a495;border:1px solid #2a2a24'}">${label}</div>`;
 let h=chip(ty=='all',false,'all '+D.pages.length,"window._ptype='all';pgRefresh()");
 h+=types.map(t=>chip(ty==t,false,esc(t)+' '+D.types[t],"window._ptype='"+t+"';pgRefresh()")).join('');
 h+=chip(bel,true,'below 70 only',"window._pbelow=!window._pbelow;pgRefresh()");
 return h}
function pgRefresh(){const ps=pgFiltered();
 const rb=document.getElementById('pgrows');if(rb)rb.innerHTML=ps.map(pageRow).join('')||'<div class="pgrow" style="color:var(--muted);grid-template-columns:1fr">No pages match.</div>';
 const c=document.getElementById('pgcount');if(c)c.textContent=ps.length+(ps.length==1?' page':' pages');
 const pl=document.getElementById('pgpills');if(pl)pl.innerHTML=pgPillsHTML()}
function pgsort(k){if(sortk==k)sortd*=-1;else{sortk=k;sortd=(k=='url')?1:1}pgRefresh();
 const hd=document.getElementById('pghead');if(hd)hd.querySelectorAll('[data-s]').forEach(s=>{s.innerHTML=s.dataset.lab+(s.dataset.s==k?(sortd>0?' ↑':' ↓'):'')})}
function pagesView(){
 const scores=D.pages.map(p=>p.score).sort((a,b)=>a-b),n=scores.length;
 const median=n?(n%2?scores[(n-1)/2]:Math.round((scores[n/2-1]+scores[n/2])/2)):0;
 const dist=[0,0,0,0,0];D.pages.forEach(p=>{const s=p.score;dist[s<60?0:s<70?1:s<80?2:s<90?3:4]++});
 const clear=D.pages.filter(p=>p.score>=70).length,maxd=Math.max(...dist,1);
 const dcol=['#db0632','#db0632','#f2f0e4','#f2f0e4','#f2f0e4'],dnum=['#ff4d6d','#ff4d6d','#a8a495','#a8a495','#a8a495'],dlab=['&lt;60','60–69','70–79','80–89','90+'];
 const wt={};ECOLS.forEach(e=>wt[e]=0);D.pages.forEach(p=>{let mn=1e9,me=null;ECOLS.forEach(e=>{if(p.engines[e]<mn){mn=p.engines[e];me=e}});if(me)wt[me]++});
 const ws=Object.entries(wt).filter(x=>x[1]>0).sort((a,b)=>b[1]-a[1]).slice(0,4),wmax=Math.max(...ws.map(x=>x[1]),1),wc=['#db0632','#ff4d6d','#a8a495','#8b8b81'];
 if(!sortk)sortk='score';
 const MM="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81";
 const HL="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#f2f0e4";
 const HLm="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#a8a495";
 const shortSent=median<70?`${70-median} point${70-median==1?'':'s'} short, so the typical page on this site is answered around rather than quoted.`:`${median-70} above the 70 line, so the typical page here is already quotable.`;
 const bars=dist.map((d,i)=>{const cl=i>=2;return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:7px"><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${d===0?'#8b8b81':cl?'#a8a495':'#ff4d6d'}">${d}</div><div style="width:100%;height:${Math.max(2,Math.round(80*d/maxd))}px;background:${d===0?'#2a2a24':cl?'#f2f0e4':'#db0632'}"></div><div style="${MM}">${dlab[i]}</div></div>`;}).join('');
 const wrow=(x,i)=>`<div style="display:flex;align-items:center;gap:12px"><div style="font-size:13px;font-weight:800;width:86px;flex:none">${x[0]}</div><div style="flex:1;height:7px;min-width:0;background:#262620"><div style="width:${Math.round(100*x[1]/wmax)}%;height:7px;background:${i===0?'#db0632':'#55534a'}"></div></div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${i===0?'#ff4d6d':'#a8a495'};width:20px;text-align:right;flex:none">${x[1]}</div></div>`;
 let h=`<div style="display:flex;flex-direction:column">
   <section style="display:grid;grid-template-columns:minmax(0,0.6fr) minmax(300px,1.6fr) minmax(220px,0.9fr);border-bottom:1px solid #2a2a24">
     <div style="display:flex;flex-direction:column;gap:12px;padding:30px 32px 34px 0"><div style="display:flex;align-items:center;gap:9px"><div style="width:7px;height:7px;background:#db0632;flex:none"></div><div style="${HL}">Median page</div></div><div style="display:flex;align-items:baseline;gap:12px"><div style="font-size:52px;font-weight:800;letter-spacing:-2.8px;line-height:.8;color:#f2f0e4">${median}</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:#8b8b81">quotable at 70</div></div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495">${shortSent}</div></div>
     <div style="display:flex;flex-direction:column;gap:18px;padding:30px 32px 34px;border-left:1px solid #2a2a24"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px"><div style="${HLm}">Score distribution</div><div style="${MM}"><span style="color:#f2f0e4">${clear} of ${D.pages.length}</span> pages clear 70</div></div><div style="display:flex;align-items:flex-end;gap:10px;height:96px">${bars}</div></div>
     <div style="display:flex;flex-direction:column;gap:16px;padding:30px 0 34px 32px;border-left:1px solid #2a2a24"><div style="${HLm}">Weakest engine per page</div><div style="display:flex;flex-direction:column;gap:11px">${ws.map(wrow).join('')}</div></div>
   </section>
   <section style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:20px 0;border-bottom:1px solid #2a2a24"><input type="text" name="rubric-url-filter" autocomplete="off" spellcheck="false" data-1p-ignore="true" data-lpignore="true" readonly onfocus="this.removeAttribute('readonly')" placeholder="filter by URL" oninput="window._pq=this.value;pgRefresh()" value="${esc(window._pq||'')}" style="flex:1;min-width:200px;background:transparent;border:0;border-bottom:1px solid #55534a;outline:none;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:14px;font-weight:600;color:#f2f0e4;padding:9px 0"><div id="pgpills" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">${pgPillsHTML()}</div></section>`;
 const HCELL=(k,lab,al)=>`<div data-s="${k}" data-lab="${lab}" onclick="pgsort('${k}')" style="text-align:${al||'left'};cursor:pointer">${lab}${sortk==k?(sortd>0?' &#8593;':' &#8595;'):''}</div>`;
 h+=`<section style="display:flex;flex-direction:column;overflow-x:auto"><div style="min-width:1080px;display:flex;flex-direction:column">
   <div id="pghead" style="${PGCOLS};padding:16px 0 10px;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81;border-bottom:1px solid #2a2a24">${HCELL('score','score')}${HCELL('url','url')}${HCELL('Known','kn','right')}${HCELL('Findable','fi','right')}${HCELL('Trusted','tr','right')}${ECOLS.map(e=>HCELL(e,(EABBR[e]||e).toLowerCase(),'right')).join('')}${HCELL('fetch_ms','load','right')}<div style="text-align:right">fail</div><div>failing checks</div></div>
   <div id="pgrows">${pgFiltered().map(pageRow).join('')}</div>
   ${D._anon&&(D._locked_pages||[]).length?`<div id="pglocked">${(D._locked_pages).map(function(u){return `<div style="${PGCOLS};padding:13px 0;border-bottom:1px solid #2a2a24;opacity:.4"><div style="text-align:center;color:#6b6b65;font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px">&#8226;</div><div style="font-size:14px;color:#a8a495;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(u)}</div></div>`;}).join('')}</div><div onclick="stickyCTA()" style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:16px 0;border-bottom:1px solid #2a2a24;cursor:pointer"><div style="font-size:14px;color:#a8a495"><b style="color:#f2f0e4">${(D._more_pages||(D._locked_pages).length).toLocaleString()} more page${(D._more_pages||(D._locked_pages).length)==1?'':'s'}</b> discovered on your site, not yet crawled.</div><div style="font-size:13px;font-weight:800;letter-spacing:.4px;text-transform:uppercase;color:#f2f0e4">Create a free account to crawl them &rarr;</div></div>`:''}
 </div></section>
   <section style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap;padding:18px 0 0;border-top:1px solid #2a2a24"><div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap;${MM}"><div style="display:flex;align-items:center;gap:7px"><div style="width:9px;height:9px;background:#db0632"></div><span>below 70 &middot; not quotable</span></div><div style="display:flex;align-items:center;gap:7px"><div style="width:9px;height:9px;background:#f2f0e4"></div><span>70+ &middot; clears the standard</span></div></div><div style="${MM}">kn / fi / tr are the three pillars &middot; click a column header to sort</div></section>`;
 h+=`</div>`;
 return h}
const SITEIDS=new Set(['robots','llms','sitemap','reachability','comparison']);
const SHORT={parity:'Schema in JS',answerfirst:'no opener',definitional:'no definition',readability:'hard to read',entitydensity:'few entities',sections:'walls of text',schema:'Article schema',wordcount:'thin content',freshness:'stale',qheadings:'H2s',faq:'no FAQ',liststables:'no tables',meta:'meta desc',title:'title',alt:'alt text',citations:'few sources',internal:'few links',statdensity:'few stats',canonical:'canonical',h1:'H1',robots:'bot blocked',sitemap:'no sitemap',reachability:'blocked',entity:'no entity',schemacomplete:'thin schema',author:'no author',sourced:'unsourced stats',video:'no video',comparison:'no comparison',noindex:'noindexed',speed:'slow response',schemavalidity:'invalid schema',duplicate:'dup title/meta',rankedlist:'no ranked list',answerthird:'answer buried',h2answer:'headings unanswered',orphans:'orphaned',brokenlinks:'broken links',nearduplicate:'near-duplicate',reviewschema:'no review schema',snippetlead:'H1 buried'};
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
 const MM="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:11px;font-weight:600;color:#8b8b81";
 const HL="font-size:12px;font-weight:800;letter-spacing:2.4px;text-transform:uppercase;color:#f2f0e4";
 const sq='<div style="width:7px;height:7px;background:#db0632;flex:none"></div>';
 const WD=(w)=>`<div style="display:flex;gap:3px;flex:none">${'<div style=\"width:5px;height:5px;background:#f2f0e4\"></div>'.repeat(w)}${'<div style=\"width:5px;height:5px;background:#2a2a24\"></div>'.repeat(Math.max(0,3-w))}</div>`;
 const engDetail=id=>{
   const dcx=s=>s=='good'?'#f2f0e4':s=='warn'?'#8b8b81':'#db0632';
   if(SITEIDS.has(id)){const sc=(D.site_checks||[]).find(c=>c.id==id)||{};const cl=sc.status=='good'?'ok':sc.status=='warn'?'wn':'er';const _u=sc.urls||[];const _l=_u.length?'<div class="egh" style="margin-top:8px">Pages ('+_u.length+')</div>'+_u.map(u=>'<span class="egp"><span class="dotb" style="background:#f2f0e4"></span><a href="'+esc(u)+'" target="_blank">'+rel(u)+'</a></span>').join(''):'';return `<div id="eg_${id}" class="engdet"><span class="rst ${cl}">Site-wide check: ${sc.status||'n/a'}</span> <span class="qd">${esc(sc.detail||'')}</span>${_l}</div>`;}
   const rr=ok.map(p=>[p,p.cs[id]]).filter(x=>x[1]&&x[1]!='na'&&x[1]!='info');
   if(!rr.length)return `<div id="eg_${id}" class="engdet"><span class="qd">No applicable pages for this signal.</span></div>`;
   const fl=rr.filter(x=>x[1]!='good'),ps=rr.filter(x=>x[1]=='good');
   const ln=x=>`<span class="egp"><span class="dotb" style="background:${dcx(x[1])}"></span><a href="${esc(x[0].url)}" target="_blank">${rel(x[0].url)}</a></span>`;
   return `<div id="eg_${id}" class="engdet">${fl.length?'<div class="egh">Failing here ('+fl.length+')</div>'+fl.map(ln).join(''):''}${ps.length?'<div class="egh"'+(fl.length?' style="margin-top:10px"':'')+'>Passing ('+ps.length+')</div>'+ps.map(ln).join(''):''}</div>`;};
 const srow=(s,strong)=>{
   const bar=`<div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding-top:3px">${WD(s.w)}<div style="flex:1;min-width:120px;max-width:260px"><div style="height:6px;background:#262620"><div style="width:${strong?100:Math.max(2,s.pr)}%;height:6px;background:${strong?'#f2f0e4':'#db0632'}"></div></div></div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:600;color:${s.pr>=90?'#a8a495':'#ff4d6d'}">${s.pr}% pass &middot; ${s.good} of ${s.total} pages</div></div>`;
   const lift=`<div style="font-size:20px;font-weight:800;letter-spacing:-.8px;text-align:right;color:${strong?'#8b8b81':'#db0632'}">${s.lift>0?'+'+s.lift:'&#8212;'}</div>`;
   return `<div onclick="tgl('eg_${s.id}')" style="display:grid;grid-template-columns:minmax(0,1fr) 46px;gap:16px;align-items:start;padding:18px 0;border-bottom:1px solid #2a2a24;cursor:pointer"><div style="display:flex;flex-direction:column;gap:9px;min-width:0"><div style="font-size:14px;font-weight:800;letter-spacing:-.3px">${esc(s.lab)}</div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495">${esc(s.ev)}</div>${bar}</div>${lift}</div>${engDetail(s.id)}`;};
 const secHdr=(name,right)=>`<div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap;padding-bottom:6px"><div style="display:flex;align-items:center;gap:9px">${sq}<div style="${HL}">${name}</div></div><div style="font-size:12px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;color:#ff4d6d">${right}</div></div><div style="display:flex;align-items:baseline;justify-content:space-between;gap:14px;padding:10px 0;${MM};border-bottom:1px solid #2a2a24"><div>signal &middot; weight &middot; site pass rate</div><div>lift</div></div>`;
 const secHdr2=(name,right)=>`<div style="display:flex;align-items:baseline;justify-content:space-between;gap:20px;flex-wrap:wrap;padding-bottom:6px"><div style="display:flex;align-items:center;gap:9px">${sq}<div style="${HL}">${name}</div></div><div style="${MM}">${right}</div></div>`;
 const relhint=gap>0?`${gap} point${gap==1?'':'s'} to quotable &middot; threshold ${TH}`:`clears the ${TH} threshold`;
 let h=`<div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(232px,320px);gap:0"><div style="display:flex;flex-direction:column;min-width:0;padding-right:32px">
   <section style="display:flex;flex-direction:column;gap:22px;padding:32px 0 30px;border-bottom:1px solid #2a2a24">
     <div style="display:flex;align-items:center;gap:9px">${sq}<div style="${HL}">${e} readiness</div></div>
     <div style="display:flex;align-items:flex-end;gap:28px;flex-wrap:wrap"><div style="display:flex;align-items:baseline;gap:12px"><div style="font-size:76px;font-weight:800;letter-spacing:-4.4px;line-height:.78;color:#f2f0e4">${score}</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:#8b8b81">of 100</div></div><div style="display:flex;flex-direction:column;gap:8px;flex:1;min-width:240px;margin-bottom:6px"><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#ff4d6d">${relhint}</div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495">Your ${ORD[rank]||'lower-ranked'} engine. ${hint}</div></div></div>
     <div style="position:relative;height:8px;background:#262620"><div style="position:absolute;left:0;top:0;width:${Math.max(2,Math.min(100,score))}%;height:8px;background:${score<70?'#db0632':'#f2f0e4'}"></div><div style="position:absolute;left:70%;top:-5px;width:2px;height:18px;background:#f2f0e4"></div></div>
     <div style="display:flex;flex-direction:column;gap:10px;padding:16px 18px;border-left:2px solid #db0632"><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#ff4d6d">How this engine decides</div><div style="font-size:14px;font-weight:400;line-height:1.55;color:#a8a495">${esc(D.engine_note[e])}</div></div>
     <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:20px;padding-top:4px"><div style="display:flex;flex-direction:column;gap:5px"><div style="font-size:26px;font-weight:800;letter-spacing:-1.2px;line-height:.85">${belowFull}</div><div style="${MM}">of ${sig.length} signals below full pass</div></div><div style="display:flex;flex-direction:column;gap:5px"><div style="font-size:26px;font-weight:800;letter-spacing:-1.2px;line-height:.85;color:#ff4d6d">+${totLift}</div><div style="${MM}">points available</div></div><div style="display:flex;flex-direction:column;gap:5px"><div style="font-size:26px;font-weight:800;letter-spacing:-1.2px;line-height:.85">${pagesBelow}</div><div style="${MM}">pages below the threshold</div></div></div>
   </section>`;
 if(high.length)h+=`<section style="display:flex;flex-direction:column;padding:30px 0 0">${secHdr('High weight, low pass rate','Fix in this order')}${high.map(s=>srow(s,0)).join('')}</section>`;
 if(low.length)h+=`<section style="display:flex;flex-direction:column;padding:30px 0 0">${secHdr2('Lower weight, worth tidying',low.length+' signal'+(low.length>1?'s':'')+' &middot; +'+low.reduce((a,s)=>a+(s.lift>0?s.lift:0),0)+' between them')}${low.map(s=>srow(s,0)).join('')}</section>`;
 if(strong.length)h+=`<section style="display:flex;flex-direction:column;padding:30px 0 0">${secHdr2('Already strong',strong.length+' signal'+(strong.length>1?'s':'')+' &middot; protect these when you edit')}${strong.map(s=>srow(s,1)).join('')}</section>`;
 const worst=[...ok].sort((a,b)=>a.engines[e]-b.engines[e]),w8=worst.slice(0,8);
 const WPC="display:grid;grid-template-columns:44px minmax(200px,1fr) minmax(160px,1.2fr) 44px 46px;gap:12px;align-items:center";
 h+=`<section style="display:flex;flex-direction:column;padding:30px 0 0">${secHdr2('Worst pages for '+e,pagesBelow+' of '+P+' below '+TH+' &middot; showing the '+w8.length+' weakest')}
   <div style="${WPC};padding:10px 0;${MM};border-bottom:1px solid #2a2a24"><div>${(EABBR[e]||e).toLowerCase()}</div><div>url</div><div>what it fails here</div><div style="text-align:right">all</div><div style="text-align:right">vs ${TH}</div></div>
   ${w8.map(p=>{const v=p.engines[e],wf=p.checks.filter(c=>(c.status=='bad'||c.status=='warn')&&ws[c.id]).map(c=>SHORT[c.id]||c.label).slice(0,4).join(', ')||'&#8212;',d=v-TH;return `<div style="${WPC};padding:13px 0;border-bottom:1px solid #2a2a24"><div style="font-size:16px;font-weight:800;color:${v<70?'#ff4d6d':'#f2f0e4'}">${v}</div><a href="${esc(p.url)}" target="_blank" style="font-size:14px;font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#f2f0e4">${rel(p.url)}</a><div style="font-size:12.5px;font-weight:400;color:#a8a495;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(wf)}</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:600;color:#8b8b81;text-align:right">${p.score}</div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:13px;font-weight:600;color:${d>=0?'#a8a495':'#ff4d6d'};text-align:right">${d>=0?'+'+d:'&#8722;'+Math.abs(d)}</div></div>`;}).join('')}
   <div onclick="go('Pages')" style="align-self:flex-start;margin-top:14px;font-size:12px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#a8a495;cursor:pointer">See all ${P} pages &rarr;</div></section>
   `;
 h+=`</div>`;
 // sidebar
 const eranked=Object.entries(D.engines).sort((a,b)=>b[1]-a[1]);
 const acr=eranked.map(x=>{const cur=x[0]==e;const bl=x[1]<70;return `<div style="display:flex;align-items:center;gap:12px"><div style="font-size:13px;font-weight:${cur?800:500};color:${cur?'#f2f0e4':'#a8a495'};width:82px;flex:none">${x[0]}</div><div style="flex:1;height:7px;min-width:0;background:#262620"><div style="width:${x[1]}%;height:7px;background:${bl?'#db0632':'#55534a'}"></div></div><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${bl?'#ff4d6d':'#a8a495'};width:20px;text-align:right;flex:none">${x[1]}</div></div>`;}).join('');
 const bnote=e==best[0]?`Same pages, different weightings. ${e} is your strongest surface — protect it as you edit.`:`Same pages, different weightings. ${e} runs ${best[1]-score} point${best[1]-score==1?'':'s'} behind ${best[0]}, your strongest.`;
 const fixes=high.concat(low).filter(s=>s.lift>0).slice(0,3);
 const doHtml=fixes.length?fixes.map((s,i)=>{const iss=IM[s.id]||{},title=(iss.fix||s.lab),cnt=iss.count||(s.total-s.good);return `<div style="display:flex;gap:12px"><div style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:#ff4d6d;flex:none">0${i+1}</div><div style="display:flex;flex-direction:column;gap:5px;min-width:0"><div style="font-size:13px;font-weight:400;line-height:1.5;color:#f2f0e4">${esc(title)}</div><div style="${MM}">${cnt} page${cnt==1?'':'s'} &middot; +${s.lift} ${e}</div></div></div>`;}).join(''):`<div style="font-size:13px;color:#a8a495">No fixes needed — ${e} passes every weighted signal.</div>`;
 const reach=(D.site_checks.find(c=>c.id=='reachability')||{}).status||'good';
 const rTxt=reach=='good'?'allowed &middot; '+P+'/'+P:reach=='warn'?'partial':'blocked',rRed=reach=='bad';
 const parityBad=ok.filter(p=>p.cs.parity=='bad'||p.cs.parity=='warn').length;
 const bots=(EBOTS[e]||[]).map(b=>`<div style="display:flex;justify-content:space-between;gap:12px;font-size:13px"><span style="color:#a8a495">${b}</span><span style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${rRed?'#ff4d6d':'#a8a495'}">${rTxt}</span></div>`).join('');
 const caNote=reach=='bad'?'Bots are blocked at the WAF — unblock them first.':parityBad?'Access is fine. The problem is what the crawler can read once it arrives.':'Access and rendering both look clean.';
 h+=`<aside style="display:flex;flex-direction:column;min-width:0;align-self:start;padding:32px 0 24px 0;border-left:1px solid #2a2a24">
   <div style="display:flex;flex-direction:column;gap:16px;padding:0 0 22px 24px"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px"><div style="${HL}">Across your engines</div><div style="${MM}">site level</div></div><div style="display:flex;flex-direction:column;gap:10px">${acr}</div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495">${bnote}</div></div>
   <div style="display:flex;flex-direction:column;gap:16px;padding:22px 0 22px 24px;border-top:1px solid #2a2a24"><div style="${HL}">Do this for ${e}</div><div style="display:flex;flex-direction:column;gap:16px">${doHtml}</div>${fixes.length?`<div onclick="go('Action Plan')" style="font-size:12.5px;font-weight:800;letter-spacing:.16em;text-transform:uppercase;color:#fff;background:#db0632;padding:14px 16px;text-align:center;cursor:pointer">Open the action plan</div>`:''}</div>
   <div style="display:flex;flex-direction:column;gap:14px;padding:22px 0 0 24px;border-top:1px solid #2a2a24"><div style="${HL}">Crawler access</div><div style="display:flex;flex-direction:column;gap:11px">${bots}<div style="display:flex;justify-content:space-between;gap:12px;font-size:13px"><span style="color:#a8a495">Server-rendered schema</span><span style="font-family:'IBM Plex Mono',ui-monospace,monospace;font-size:12.5px;font-weight:600;color:${parityBad?'#ff4d6d':'#a8a495'}">${parityBad?parityBad+' pages JS-only':'all '+P+' server-rendered'}</span></div></div><div style="font-size:13px;font-weight:400;line-height:1.5;color:#a8a495;border-top:1px solid #2a2a24;padding-top:11px">${caNote}</div></div>
 </aside></div>`;
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
   var _sp='<svg viewBox="0 0 '+_W+' '+_H+'" style="width:100%;height:90px"><polyline points="'+_poly+'" fill="none" stroke="#f2f0e4" stroke-width="2"/>';
   _tr.forEach(function(t,i){var q=_xy(t,i);_sp+='<circle cx="'+q[0].toFixed(1)+'" cy="'+q[1].toFixed(1)+'" r="2.6" fill="#f2f0e4"><title>'+esc(t.date||'')+': '+t.overall+'</title></circle>';});
   _sp+='</svg>';
   var _d=_tr[_tr.length-1].overall-_tr[0].overall;
   h+='<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>SCORE OVER TIME</h3><span class="meta">'+_tr.length+' crawls &middot; '+(_d>=0?'+':'')+_d+' since first</span></div><div class="apbox" style="padding:16px 22px">'+_sp+'</div></section>';
 }
 var sm=D.sitemap||{};
 if(sm.has_sitemap){
   h+='<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>SITEMAP HEALTH</h3><span class="meta">'+sm.sitemap_urls+' in sitemap &middot; '+sm.crawled+' crawled</span></div><div class="apbox">';
   h+=foldcard('sm_miss',sm.missing_n,'Missing from the sitemap','Reachable via internal links, so Google and Bing find them, but absent from the XML sitemap. The AI crawlers (GPTBot, ClaudeBot, PerplexityBot) lean on the sitemap, so these pages risk being invisible to ChatGPT, Perplexity and Claude even while Google still indexes them. Add them to the sitemap.','#ff4d6d',(sm.missing_from_sitemap||[]).map(function(u){return {path:u};}));
   h+=foldcard('sm_orph',sm.orphan_n,'Orphan pages (no internal links)','Nothing links to these, so a crawler may never reach them. Add internal links.','#ff4d6d',(sm.orphan_no_internal_links||[]).map(function(u){return {path:u};}));
   h+=foldcard('sm_nox',sm.noindex_n,'Noindexed pages in the sitemap','A sitemap should list only indexable URLs. Remove these.','#db0632',(sm.noindex_in_sitemap||[]).map(function(u){return {path:u};}));
   if(sm.not_crawled_n) h+='<div style="margin-top:11px;color:var(--muted);font-size:12.5px">'+sm.not_crawled_n+' sitemap URL(s) not reached in this crawl - raise the crawl limit to confirm whether these are genuine orphans or just uncrawled.</div>';
   h+='</div></section>';
 }
 var _ia=D.ia||{};
 if(_ia.n){
   h+='<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#ff4d6d"></span><h3>INTERNAL LINKING</h3><span class="meta">median '+_ia.median_inbound+' inbound &middot; avg '+_ia.avg_outbound+' outbound &middot; deepest '+_ia.max_depth+' clicks from home</span></div><div class="apbox">';
   h+='<div class="qd" style="line-height:1.5;padding:10px 0 2px">Computed from your own internal-link graph. Every fix here is free: add a link.</div>';
   h+=foldcard('ia_ul',_ia.underlinked_n,'High-value pages barely linked','Score 75+ with 2 or fewer internal links. Link these from stronger pages so authority flows to them.','#ff4d6d',(_ia.underlinked||[]).map(function(x){return {path:x.path,metric:'score '+x.score+' &middot; '+x.inbound+' in'};}));
   h+=foldcard('ia_deep',_ia.deep_n,'Pages buried too deep','More than '+_ia.deep_threshold+' clicks from the homepage. Shorten the click path so crawlers and AI reach them sooner.','#ff4d6d',(_ia.deep||[]).map(function(x){return {path:x.path,metric:x.clicks+' clicks'};}));
   if(_ia.home_found) h+=foldcard('ia_unr',_ia.unreachable_n,'Unreachable from the homepage','No internal click path exists at all, so a crawler may never reach them.','#db0632',(_ia.unreachable||[]).map(function(x){return {path:x.path,metric:x.inbound+' in'};}));
   h+=foldcard('ia_sink',_ia.equity_sinks_n,'Equity sinks','Lots of inbound links but almost none out, so authority pools and stops. Add contextual links out.','#ff4d6d',(_ia.equity_sinks||[]).map(function(x){return {path:x.path,metric:x.inbound+' in / '+x.outbound+' out'};}));
   h+='</div></section>';
 }
 const stpage=p=>{const b=PBAND(p.score);
   const bad=p.checks.filter(c=>c.status=='bad'),warn=p.checks.filter(c=>c.status=='warn');
   const sum=[bad.length?bad.length+' error'+(bad.length>1?'s':''):'',warn.length?warn.length+' warning'+(warn.length>1?'s':''):''].filter(Boolean).join(' · ')||'all checks pass';
   const chips=[...bad.map(c=>`<span class="stck bad">${esc(c.label)}</span>`),...warn.map(c=>`<span class="stck warn">${esc(c.label)}</span>`)].join('');
   return `<div class="stp"><span class="schip" style="background:${b[0]};color:${b[1]}">${p.score}</span><div style="flex:1;min-width:0"><div style="display:flex;gap:10px;align-items:baseline"><a href="${esc(p.url)}" target="_blank">${rel(p.url)}</a><span class="qd" style="flex:none;color:${bad.length?'#ff4d6d':warn.length?'#ff4d6d':'var(--ok)'}">${sum}</span></div>${chips?`<div class="stcks">${chips}</div>`:''}</div></div>`};
 const rows=(list,lab)=>list.map((x,i)=>{const a=avg(x[1]),b=PBAND(a),id='st_'+lab+i;return `<div class="strow" onclick="tgl('${id}')" style="cursor:pointer"><span style="font-weight:600">${esc(x[0])} <span class="egcar">▾</span></span><span class="qd">${x[1].length} page${x[1].length>1?'s':''}</span><div class="stbar" title="avg score ${a}/100"><span class="stthr"></span><i style="width:${a}%;background:${a>=70?'#f2f0e4':'#db0632'}"></i></div><span class="schip" style="background:${b[0]};color:${b[1]}">${a}</span></div><div id="${id}" class="stdet">${[...x[1]].sort((p,q)=>p.score-q.score).map(stpage).join('')}</div>`}).join('');
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>BY TOP-LEVEL SECTION</h3><span class="meta">${secs.length} section${secs.length>1?'s':''}</span></div><div class="apbox" style="padding:6px 22px">${rows(secs.map(s=>['/'+s[0],s[1]]),'sec')}</div></section>`;
 const depths=Object.keys(byDepth).map(Number).sort((a,b)=>a-b),maxd=Math.max(...depths.map(d=>byDepth[d].length),1);
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#ff4d6d"></span><h3>BY CRAWL DEPTH</h3><span class="meta">clicks from the homepage</span></div><div class="apbox" style="padding:6px 22px">${rows(depths.map(d=>['Depth '+d,byDepth[d]]),'dep')}</div></section>`;
 var _lg=D.linkgraph||{nodes:[]};
 if(_lg.nodes&&_lg.nodes.length){
   h+='<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>SITE MAP</h3><span class="meta">node size = inbound links &middot; colour = score &middot; red ring = under-linked / orphan</span></div><div class="apbox" style="padding:16px 22px">'+sitemapViz()+'</div></section>';
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
  var band=function(s){return s>=70?'#f2f0e4':'#db0632';};
  var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;max-height:560px;background:#00000018;border-radius:0">';
  edges.forEach(function(e){var a=pos[e[0]],b=pos[e[1]];if(a&&b)s+='<line x1="'+a[0].toFixed(1)+'" y1="'+a[1].toFixed(1)+'" x2="'+b[0].toFixed(1)+'" y2="'+b[1].toFixed(1)+'" stroke="#ffffff12" stroke-width="1"/>';});
  nodes.forEach(function(n,i){var p=pos[i];if(!p)return;var r=Math.max(4,Math.min(20,4+1.7*Math.sqrt(n.ind||0)));
    var ring=n.ul?'stroke="#ff4d6d" stroke-width="2.6"':(n.o?'stroke="#db0632" stroke-width="1.5"':'stroke="#0000002e" stroke-width="0.6"');
    var tip=esc(n.p)+' - score '+n.s+' - '+(n.ind||0)+' inbound'+(n.ul?' - under-linked high-value':(n.o?' - orphan':''));
    s+='<circle cx="'+p[0].toFixed(1)+'" cy="'+p[1].toFixed(1)+'" r="'+r.toFixed(1)+'" fill="'+band(n.s)+'" '+ring+'><title>'+tip+'</title></circle>';});
  s+='</svg><div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:12.5px;color:var(--muted)">'
    +'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#f2f0e4;vertical-align:middle"></span> 85+</span>'
    +'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#f2f0e4;vertical-align:middle"></span> 70-84</span>'
    +'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:#db0632;vertical-align:middle"></span> under 70</span>'
    +'<span><span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:transparent;border:2px solid #db0632;vertical-align:middle"></span> under-linked high-value or orphan (add internal links)</span>'
    +(g.capped?'<span>(first 250 pages)</span>':'')+'</div>';
  return s;
}
function speed(){
 const sc2=ms=>ms<=800?'#f2f0e4':ms<=1800?'#8b8b81':'#db0632',sl=ms=>ms<=800?'Fast':ms<=1800?'OK':'Slow';
 const ps=[...D.pages].sort((a,b)=>b.fetch_ms-a.fetch_ms);
 const avgf=Math.round(ps.reduce((a,p)=>a+p.fetch_ms,0)/ps.length),avgr=Math.round(ps.reduce((a,p)=>a+p.render_ms,0)/ps.length);
 const fast=ps.filter(p=>p.fetch_ms<=800).length,okc=ps.filter(p=>p.fetch_ms>800&&p.fetch_ms<=1800).length,slow=ps.filter(p=>p.fetch_ms>1800).length;
 let h=`<div style="display:flex;flex-direction:column;gap:18px">`;
 h+=`<div class="statgrid">
   <div class="statcard"><div class="n" style="color:${sc2(avgf)}">${avgf}<span style="font-size:14px;color:var(--muted)"> ms</span></div><div class="l">Avg server response · <b style="color:${sc2(avgf)}">${sl(avgf)}</b></div></div>
   <div class="statcard"><div class="n" style="color:#f2f0e4">${fast}</div><div class="l">Fast ≤ 0.8s</div></div>
   <div class="statcard"><div class="n" style="color:#8b8b81">${okc}</div><div class="l">OK ≤ 1.8s</div></div>
   <div class="statcard"><div class="n" style="color:#ff4d6d">${slow}</div><div class="l">Slow &gt; 1.8s</div></div>
   <div class="statcard"><div class="n">${avgr}<span style="font-size:14px;color:var(--muted)"> ms</span></div><div class="l">Avg render (tool overhead)</div></div></div>`;
 h+=`<div class="qd" style="line-height:1.6;max-width:900px">Server response graded on Google's TTFB thresholds: Fast ≤ 0.8s, OK ≤ 1.8s, Slow &gt; 1.8s. Render time is the tool's headless-Chrome overhead, not your site's speed.</div>`;
 h+=`<div class="qd" style="line-height:1.6;max-width:900px;border-top:1px solid var(--line);padding-top:12px"><b style="color:var(--txt)">This is a scored check</b> (the <b>Fast server response</b> signal, Findable pillar): <b style="color:${sc2(avgf)}">${fast} of ${ps.length}</b> pages pass. It feeds the live-retrieval engines - <b>ChatGPT, Perplexity, Copilot, AI Overviews</b> - which abandon slow pages before they can cite them, so a Slow page is a citation risk, not just a UX one.</div>`;
 h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>SLOWEST PAGES</h3><span class="meta">by server response time</span></div>
   <div style="background:var(--panel2);border:1px solid var(--line);border-radius:0;overflow:hidden">
   <div style="display:grid;grid-template-columns:140px 1fr 90px;gap:16px;padding:12px 20px;background:#191914;border-bottom:1px solid var(--line);font-size:12.5px;font-weight:700;letter-spacing:.06em;color:var(--muted)"><span>SERVER RESPONSE</span><span>URL</span><span style="text-align:right">RENDER MS</span></div>
   ${ps.slice(0,40).map(p=>`<div style="display:grid;grid-template-columns:140px 1fr 90px;gap:16px;align-items:center;padding:11px 20px;border-bottom:1px solid #ffffff0d"><span><span style="font-size:12.5px;font-weight:700;color:${sc2(p.fetch_ms)};background:${sc2(p.fetch_ms)}22;padding:3px 8px;border-radius:4px">${p.fetch_ms} ms</span> <span class="qd">${sl(p.fetch_ms)}</span></span><a href="${esc(p.url)}" target="_blank" style="font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${rel(p.url)}</a><span class="qd" style="text-align:right">${p.render_ms}</span></div>`).join('')}
   </div></section></div>`;
 return h}
function dl(name,rows){const csv=rows.map(r=>r.map(c=>`"${String(c==null?'':c).replace(/"/g,'""')}"`).join(',')).join('\n');
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download=name;a.click()}
function dlText(name,text,mime){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type:(mime||'text/plain')+';charset=utf-8'}));a.download=name;a.click();}
// every export filename carries the CRAWL date (D.date), so a re-audit downloads a DISTINCT file and an
// old snapshot can never be mistaken for the current one (they used to collide as "...(1).csv").
function _fn(suffix){return 'cited-score-'+(D.domain||'site')+'-'+(D.date||'undated')+'-'+suffix;}
// Developer handoff: the ranked plan as a Markdown checklist - the FIX instruction plus the exact pages to
// change, so a developer (or a coding agent) can act on it without opening the report. Differs from the CSV,
// which is score data; this is the do-this list.
function exportDevPlan(){
 const ord=(window._ordered||D.issues||[]), overall=D.overall, act=(typeof ACT==='function'?ACT():(D.issues||[]));
 const tg=act.reduce((a,i)=>a+(typeof gpos==='function'?gpos(i):(i.gain_overall||0)),0);
 const proj=(D.proj_all!=null?D.proj_all:Math.min(100,overall+tg));
 const affS=new Set();act.forEach(i=>{(i.bad||[]).forEach(u=>affS.add(u));(i.warn||[]).forEach(u=>affS.add(u))});
 const edits=act.reduce((a,i)=>a+(i.count||0),0);
 const _brand=WL?(D.agency||''):(DB?'':'Rubric');   // white-label -> agency; Pro de-brand -> none; free -> Rubric
 let md='# '+(_brand?_brand+' action plan':'AI search action plan')+' — '+D.domain+'\n\n';
 md+='Score today: '+overall+'/100 → projected '+proj+'/100 (+'+Math.max(0,proj-overall)+') with all fixes applied.\n';
 md+='Generated '+(D.generated||D.date||'')+' · '+ord.length+' fixes · '+edits+' page edits across '+affS.size+' pages.\n';
 md+='Ordered by score impact. Each gain is standalone; fixes compound on a page.\n\n---\n\n';
 ord.forEach((i,x)=>{
   md+='## '+(x+1)+'. '+i.label+'\n';
   md+='- Impact: +'+(i.gain_overall||0)+' overall · '+(i.pillar||'')+' · '+(i.effort||'')+' effort · '+(i.count||0)+' page'+((i.count||0)===1?'':'s')+(i.ch?(' · '+i.ch):'')+'\n';
   const eg=Object.entries(i.gain_engines||{}).filter(a=>a[1]>0).sort((a,c)=>c[1]-a[1]);
   if(eg.length)md+='- Engines helped: '+eg.map(a=>a[0]+' +'+a[1]).join(', ')+'\n';
   md+='- Fix: '+(i.fix||i.ev||'')+'\n';
   const urls=[...(i.bad||[]),...(i.warn||[])];
   if(urls.length){md+='- Pages to change:\n';urls.forEach(u=>md+='  - [ ] '+u+'\n');}
   md+='\n';
 });
 if(_brand)md+='---\nGenerated by '+_brand+(_brand==='Rubric'?' · cited.gogochimp.com':'')+'\n';
 dlText(_fn('dev-checklist.md'), md, 'text/markdown');
}
function exportPages(){var cids=Object.keys(D.check_meta||{}).filter(id=>(D.check_meta[id]||{}).pillar!='Info');
 const rows=[['url','type','depth','score','Known','Findable','Trusted',...ECOLS,'words','ms','errors','warnings',...cids]];
 D.pages.forEach(p=>{const st=p.cs||{};rows.push([p.url,p.type,p.depth,p.score,p.pillars.Known,p.pillars.Findable,p.pillars.Trusted,...ECOLS.map(e=>p.engines[e]),(p.metrics&&p.metrics.words)||'',p.fetch_ms+p.render_ms,p.checks.filter(c=>c.status=='bad').map(c=>c.label).join('; '),p.checks.filter(c=>c.status=='warn').map(c=>c.label).join('; '),...cids.map(id=>st[id]||'')])});
 dl(_fn('pages.csv'),rows)}
function grokView(){const G=D.grok_advisory||{};
 const px=(G.proxy||[]).filter(e=>D.engines&&D.engines[e]!=null);
 const proxyAvg=px.length?Math.round(px.reduce((a,e)=>a+D.engines[e],0)/px.length):null;
 const proxyH=px.map(e=>{const v=D.engines[e];return `<div style="display:flex;align-items:center;gap:10px"><span style="width:90px;color:#a8a495;font-size:13px">${e}</span><span style="flex:1;height:8px;border-radius:4px;background:#262620;overflow:hidden"><span style="display:block;width:${Math.max(2,v)}%;height:100%;background:${bcol(v)}"></span></span><span style="width:26px;text-align:right;color:#fff;font-weight:700;font-size:14px">${v}</span></div>`}).join('');
 const block=(c,t,sub,body)=>`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:${c}"></span><h3>${t}</h3><span class="meta">${sub}</span></div><div class="apbox" style="padding:16px 24px"><div style="font-size:14px;line-height:1.65;color:#a8a495">${esc(body)}</div></div></section>`;
 let h=`<div class="ap2"><div class="apmain">`;
 h+=`<div class="apsum" style="display:grid;grid-template-columns:auto 1fr;gap:34px;align-items:center;padding:26px 28px">
   <div style="display:flex;align-items:center;gap:22px">
     <div class="sring" style="--p:0;--c1:#2a2a24;--c2:#2a2a24"><i><span class="v" style="font-size:15px;letter-spacing:.5px">N/A</span><span class="o">ADVISORY</span></i></div>
     <div style="display:flex;flex-direction:column;gap:9px">
       <div class="htitle" style="font-size:20px;margin:0">${esc(G.name||'Grok (xAI)')}</div>
       <div style="display:flex;align-items:center;gap:8px"><span style="font-size:13px;font-weight:600;color:#a8a495;background:rgba(139,132,128,.14);border:1px solid rgba(139,132,128,.3);padding:4px 9px;border-radius:999px">Advisory · not scored</span></div>
       <div class="qd" style="line-height:1.5;max-width:250px">A large surface (~117M users) that sends almost no referrals. Covered here through its web-side proxy, not a fabricated score.</div>
     </div>
   </div>
   <div style="border-left:1px solid var(--line);padding-left:32px;display:flex;flex-direction:column;gap:14px">
     <div class="apk">HOW THIS ENGINE DECIDES</div>
     <div style="font-size:14px;line-height:1.6;color:#a8a495;max-width:640px">${esc(G.how||'')}</div>
   </div></div>`;
 if(px.length)h+=`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>GROK'S WEB SIDE, VIA YOUR EXISTING SCORES</h3><span class="meta">it reads the same open-web signals as these two engines</span></div>
   <div class="apbox" style="padding:18px 24px;display:flex;flex-direction:column;gap:12px">${proxyH}<div class="qd" style="line-height:1.55;border-top:1px solid var(--line);padding-top:12px">${px.join(' and ')} are your Grok web-side proxy${proxyAvg!=null?' (about '+proxyAvg+'/100 today)':''}. Improve those and Grok's open-web retrieval improves with them. ${esc(G.why||'')}</div></div></section>`;
 h+=block('#f2f0e4','THE ONE GROK-SPECIFIC LEVER','off-page, so outside this audit',G.lever||'');
 h+=block('#ff4d6d','ACCURACY CAVEAT','least reliable attributions of any engine here',G.caveat||'');
 h+=block('#8b8b81','WHAT WOULD MAKE IT A SCORED ENGINE','the honest trigger',G.trigger||'');
 h+=`</div><div class="apside">
   <div class="card2"><h3>Why advisory, not a 7th ring</h3><div class="qd" style="line-height:1.6">A scored engine has to be calibrated against real citation data. There is no Grok export to calibrate against, and its web weighting would just clone Perplexity's. A fabricated ring would cheapen the six that are earned.</div></div>
   <div class="card2"><h3>If you want Grok visibility</h3><div class="qd" style="line-height:1.6">The lever is X presence, not this site. Keep shipping the parity, entity and freshness work that already feeds Grok's open-web pool, and treat any Grok mention as a free by-product of your X footprint.</div></div>
   <div class="card2"><div style="display:flex;align-items:center;gap:8px"><span style="width:8px;height:8px;border-radius:50%;background:#f2f0e4"></span><span style="font-size:14px;font-weight:700">Informational, like llms.txt</span></div><div class="qd" style="line-height:1.6;margin-top:8px">Shown for completeness and deliberately not counted in your Rubric, exactly as the tool treats llms.txt.</div></div>
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
 var bar=function(k,label,desc){var n=c[k]||0;var pct=Math.round(100*n/tot);var col=n?(pct>=50?'#f2f0e4':'#ff4d6d'):'#ff4d6d';var barcol=n?(pct>=50?'#f2f0e4':'#db0632'):'#db0632';
   var s=sig[k]||{has:[],missing_money:[]};var miss=(s.missing_money||[]).length;var hn=(s.has||[]).length;
   var det='<div id="ag_'+k+'" class="engdet">'
     +'<div class="egh">Present on ('+hn+')</div>'+(hn?(s.has||[]).map(function(u){return ln(u,'#f2f0e4')}).join(''):'<span class="egp qd">none</span>')
     +'<div class="egh" style="margin-top:10px">Missing on '+miss+' commercial page'+(miss==1?'':'s')+' (should add)</div>'+(miss?(s.missing_money||[]).map(function(u){return ln(u,'#db0632')}).join(''):'<span class="egp qd">none</span>')
     +'<div class="qd" style="column-span:all;margin-top:10px;line-height:1.5">Only commercial / transactable pages are flagged as missing - editorial and blog pages do not need to be actionable.</div></div>';
   return '<div class="apissue"><div class="aprow" onclick="tgl(\'ag_'+k+'\')" style="grid-template-columns:1fr;gap:5px;cursor:pointer;padding:13px 0">'
     +'<div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px"><span style="font-weight:700;font-size:14px">'+label+' <span class="egcar">▾</span></span><span style="color:'+col+';font-weight:700;font-size:14px;white-space:nowrap">'+n+' / '+tot+' pages</span></div>'
     +'<div style="height:7px;border-radius:4px;background:#262620;overflow:hidden;margin:1px 0"><span style="display:block;width:'+Math.max(2,pct)+'%;height:100%;background:'+barcol+'"></span></div>'
     +'<div class="qd" style="line-height:1.5">'+desc+'</div>'
     +'</div>'+det+'</div>';};
 var matrix=SIG.map(function(s){return bar(s[0],s[1],s[2])}).join('');
 var proto=a.protocols||{}; var pk=Object.keys(proto);
 var protoH=pk.length?pk.map(function(k){var p=proto[k];var col=p.found?'#f2f0e4':'#8b8b81';return '<div style="display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-top:1px solid var(--line)"><span style="min-width:0"><b style="font-size:14px">'+k+'</b> <span class="qd" style="font-size:12.5px">'+p.path+'</span></span><span style="color:'+col+';font-weight:700;font-size:13px;flex:none">'+(p.found?'✓ found':'not found')+'</span></div>';}).join(''):'<span class="qd">Not probed (benchmark run).</span>';
 var protoFound=pk.filter(function(k){return proto[k].found}).length;
 var wmn=a.webmcp_n||0; var wmcol=wmn?'#f2f0e4':'#8b8b81';
 var wmRow='<div style="display:flex;justify-content:space-between;gap:10px;padding:9px 0;border-top:1px solid var(--line)"><span style="min-width:0"><b style="font-size:14px">In-page WebMCP</b> <span class="qd" style="font-size:12.5px">page declares agent-callable tools (navigator.modelContext / registerTool)</span></span><span style="color:'+wmcol+';font-weight:700;font-size:13px;flex:none">'+(wmn?('✓ '+wmn+' page'+(wmn==1?'':'s')):'not found')+'</span></div>';
 var cm=D.commerce;
 var shopSec=cm?(function(){var plat=cm.platform,af=cm.autofeed;var platTxt=plat?(plat==='custom'?'custom platform':plat):'platform not detected';var metaTxt=af?'catalog can auto-feed':(plat?'needs a product feed':'platform not detected');var route=af?`You are on <b style="color:#f2f0e4">Shopify</b>, whose Catalog can feed the AI shopping surfaces automatically, so confirm your Shop / catalog is enabled and every product carries a machine-readable price and availability.`:(plat?`You are on <b style="color:#f2f0e4">${esc(plat==='custom'?'a custom platform':plat)}</b>, which does not automatically feed AI shopping, so your store is invisible to ChatGPT Shopping unless you publish a product feed - a Google Merchant Center feed is the widest-reach route.`:`We could not read your store platform on this run, so cannot confirm whether it auto-feeds AI shopping. Either way, publishing a product feed (a Google Merchant Center feed reaches the most surfaces) is how a store gets into AI shopping.`);return `<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#ff4d6d"></span><h3>AI SHOPPING &middot; FEED READINESS</h3><span class="meta">${esc(platTxt)} &middot; ${metaTxt}</span></div><div class="apbox" style="padding:16px 22px"><div style="font-size:14px;line-height:1.65;color:#a8a495;max-width:760px">ChatGPT Shopping and Google&#39;s agentic checkout increasingly answer from <b>product feeds</b>, not open-web retrieval, so your citability score is <b style="color:#f2f0e4">necessary but not sufficient</b> for AI shopping: a store can score well and still be invisible if it is not in a feed the shopping agents ingest. ${route}</div><div class="qd" style="line-height:1.55;margin-top:12px;max-width:760px">A crawl sees your platform and on-page product data, not your actual Merchant Center feed, so treat this as feed <b>eligibility</b>, not a feed audit. For the on-page half an agent reads (Offer, price, availability), see the actionable signals below.</div></div></section>`;})():'';
 return `<div class="ap2"><div class="apmain">
   <div class="apsum" style="padding:24px 28px">
     <div class="apk">AGENT-READINESS &middot; ADVISORY (NOT SCORED YET)</div>
     <div style="font-size:14px;line-height:1.65;color:#a8a495;max-width:730px;margin-top:10px">The next shift is answers &rarr; <b>agents</b>: AI that browses, compares and <b>transacts</b> on the user's behalf. An agent doesn't just read your page, it needs to <b>act</b> - read a price, check availability, book, contact. Actions need machine-readable precision prose can't give. This tab reads whether AI can <b>act</b> on you, not just cite you. Forward-looking and directional, not part of the Rubric yet.</div>
   </div>
   ${shopSec}
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>ACTIONABLE SIGNALS ON YOUR SITE</h3><span class="meta">${a.any_n||0} of ${tot} pages expose at least one &middot; ${a.money_n||0} commercial &middot; click a signal to see which pages</span></div><div class="apbox" style="padding:6px 22px 16px">${matrix}</div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>PROTOCOL &amp; DISCOVERY FILES</h3><span class="meta">${protoFound} of ${pk.length} present &middot; how an agent connects programmatically</span></div><div class="apbox" style="padding:6px 22px 14px">${protoH}${wmRow}<div class="qd" style="line-height:1.55;border-top:1px solid var(--line);padding-top:11px;margin-top:6px">These are emerging and nascent - most sites have none yet - so this is forward guidance, not a mark against you. Two to watch: the <b>MCP server card</b> (<code>/.well-known/mcp.json</code>), how an agent discovers your tools server-side, and <b>in-page WebMCP</b> (<code>navigator.modelContext</code>), where the page itself hands a browsing agent callable tools. As agents standardise on MCP, these become how they act on you rather than just read you.</div></div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#ff4d6d"></span><h3>ACTIONABLE, NOT DESCRIPTIVE, SCHEMA</h3><span class="meta">the distinction that matters</span></div><div class="apbox" style="padding:16px 22px"><div style="font-size:14px;line-height:1.65;color:#a8a495">Agentic does not make <b>all</b> schema matter - it splits it. <b>Descriptive</b> schema (Article, breadcrumbs, FAQ) stays a minor entity signal. <b>Actionable</b> schema (Offer + price + availability, potentialAction, ContactPoint) is the structured path an agent transacts through. Add the actionable subset to your commercial pages, and <b>server-render it</b> - an agent that doesn't run JS can't see JS-injected schema, exactly like today's crawlers.</div></div></section>
   </div>
   <div class="apside">
     <div class="card2"><h3>Do this first</h3><div class="qd" style="line-height:1.6">On product / service pages, add an <b>Offer</b> with price + availability, and a <b>potentialAction</b> for the primary action (buy / book / contact). That is the minimum an agent needs to act on you.</div></div>
     <div class="card2"><h3>Why it isn't scored</h3><div class="qd" style="line-height:1.6">Agentic search is emerging, not mainstream, and agent support for Action schema is still nascent (it could be "declared but unread" for a while). So this is a forward-looking advisory - the same honest treatment as Grok and llms.txt - and it graduates to a scored "Actionable" pillar as agents arrive.</div></div>
   </div></div>`;
}
function commoncrawlView(){
 var cc=D.commoncrawl||{ok:false};
 var MNc="";
 var body='', lists='';
 if(!cc.ok){
   body='<div class="qd" style="line-height:1.65;max-width:720px">Could not reach Common Crawl&#39;s index on this run ('+esc(cc.reason||'no response')+'), so presence is unavailable here. It is an external lookup and never affects your Rubric.</div>';
 }else{
   var pin=cc.pages_in||[], pout=cc.pages_out||[], tot=pin.length+pout.length;
   var n=tot||cc.captures||0, col=pin.length>0?'#f2f0e4':'#db0632';
   var big=(tot? (''+pin.length) : (cc.capped?'1k+':(''+(cc.captures||0))));
   var sub=tot?('OF YOUR '+tot+'<br>CRAWLED PAGES'):('PAGES IN<br>'+esc(cc.crawl_name||cc.crawl||'LATEST CRAWL'));
   var lead=(pin.length>0)
     ?(''+pin.length+' of the '+tot+' pages we crawled are in the '+esc(cc.crawl_name||'latest')+' Common Crawl. Those reach the open-web dataset that seeds most model training - it does not prove any single model trained on them (every lab filters the crawl), but the ones below marked red are invisible to the training layer.')
     :('None of the pages we crawled were found in the latest monthly crawl. Usual causes: a robots.txt / WAF block on CCBot (see the AI crawlers tab), a brand-new or thinly-linked domain, or JavaScript-only content - CCBot does not run JS.');
   body='<div style="display:flex;align-items:baseline;gap:14px;margin:6px 0 16px"><div style="font-size:46px;font-variation-settings:&#39;wght&#39; 700;line-height:1;color:'+col+'">'+big+'</div><div style="'+MNc+';font-size:12px;color:#8b8b81;letter-spacing:.12em;line-height:1.5">'+sub+'</div></div><div class="qd" style="line-height:1.65;max-width:720px">'+lead+(cc.capped?' <span style="color:#ff4d6d">(Common Crawl returned the 1,000-capture cap, so a red page may be a capture beyond that limit rather than truly absent.)</span>':'')+'</div>';
   var mkList=function(title,arr,c){ if(!arr||!arr.length) return '';
     return '<div class="egh" style="margin-top:18px">'+title+' ('+arr.length+')</div><div style="columns:2;column-gap:26px;margin-top:4px">'+arr.map(function(u){return '<span class="egp" style="display:block;break-inside:avoid;padding:2px 0"><span class="dotb" style="background:'+c+'"></span><a href="'+esc(u)+'" target="_blank">'+rel(u)+'</a></span>';}).join('')+'</div>'; };
   lists=mkList('In Common Crawl',pin,'#f2f0e4')+mkList('Not in Common Crawl',pout,'#db0632');
 }
 return '<div class="ap2"><div class="apmain"><div class="apsum" style="padding:24px 28px"><div class="apk">COMMON CRAWL PRESENCE &middot; ADVISORY (NOT SCORED)</div><div style="font-size:14px;line-height:1.65;color:#a8a495;max-width:740px;margin-top:10px">Common Crawl is the open-web snapshot that feeds ~64% of LLM training sets (C4, RefinedWeb, FineWeb, RedPajama, Dolma). Whether your pages are in it is a rough proxy for whether the <b>training</b> layer can know you exist - distinct from live-search citation, which the engine tabs measure.</div><div style="margin-top:14px">'+body+'</div>'+(lists?'<div style="padding:2px 28px 18px">'+lists+'</div>':'')+'</div><div class="apside"><div class="card2"><h3>Why it isn&#39;t scored</h3><div class="qd" style="line-height:1.6">Presence does not prove any model trained on you - every training set filters the crawl, and labs stopped disclosing their mixes around 2023. So it is an honest directional signal, shown like llms.txt and Grok, not part of the Rubric.</div></div><div class="card2"><h3>If pages are missing</h3><div class="qd" style="line-height:1.6">Check CCBot is not blocked in robots.txt or at the WAF (AI crawlers tab), server-render key pages (CCBot runs no JS), and earn a few inbound links so the crawler discovers them.</div></div></div></div>';
}
function aicrawlerView(){
 var ac=D.aicrawler||{bots:[],has_robots:false};
 var bots=ac.bots||[];
 var reach=(D.site_checks||[]).find(function(c){return c.id=='reachability'})||{};
 var col={allowed:'#f2f0e4',partial:'#ff4d6d',blocked:'#db0632'};
 var isBlk=function(r){return r===0||r==401||r==403;};               // HARD firewall deny (429 = rate limit, handled separately)
 var isRL=function(r){return r==429;};                               // rate-limited: inconclusive, likely our own probe burst, NOT a block
 var fwBlocked=function(b){return b.reach!=null&&isBlk(b.reach);};    // robots may allow, but the firewall 403s
 var effCol=function(b){return (b.status=='blocked'||fwBlocked(b))?col.blocked:(b.status=='partial'?col.partial:col.allowed);};
 var reachLabel=function(b){
   if(b.reach==null) return '<span style="width:104px;flex:none"></span>';
   if(isRL(b.reach)) return '<span title="429 rate-limit after retry, likely our probe burst, not a block" style="color:#ff4d6d;font-weight:700;font-size:12.5px;flex:none;width:104px;text-align:right">rate-limited</span>';
   var blk=isBlk(b.reach);
   return '<span style="color:'+(blk?'#db0632':'#f2f0e4')+';font-weight:700;font-size:12.5px;flex:none;width:150px;text-align:right">'+(blk?('firewall '+(b.reach||'x')+(b.cause?' &middot; '+esc(b.cause):'')):'reachable')+'</span>';
 };
 var serving=bots.filter(function(b){return b.role=='serving'});
 var servingBlocked=serving.filter(function(b){return b.status=='blocked'||b.status=='partial'||fwBlocked(b);});
 var allowedN=bots.filter(function(b){return b.status=='allowed'&&!fwBlocked(b);}).length;
 var ops=[],seen={};
 bots.forEach(function(b){if(!seen[b.op]){seen[b.op]=[];ops.push(b.op);}seen[b.op].push(b);});
 var roleBadge=function(r){return r=='serving'
   ?'<span style="font-size:11px;font-weight:800;color:#f2f0e4;letter-spacing:.05em">CITATION</span>'
   :'<span style="font-size:11px;font-weight:800;color:#8b8b81;letter-spacing:.05em">TRAINING</span>';};
 var grid=ops.map(function(op){
   return '<div style="margin-top:15px"><div class="egh" style="margin-bottom:2px">'+esc(op)+'</div>'+seen[op].map(function(b){
     return '<div style="display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid var(--line)">'
       +'<span class="dotb" style="background:'+effCol(b)+'"></span>'
       +'<span style="font-weight:600;font-size:14px;width:140px;flex:none">'+esc(b.name)+'</span>'
       +'<span style="width:62px;flex:none">'+roleBadge(b.role)+'</span>'
       +'<span class="qd" style="flex:1;font-size:13px;min-width:0">'+esc(b.purpose)+'</span>'
       +reachLabel(b)
       +'<span style="color:'+col[b.status]+';font-weight:700;font-size:13px;text-transform:uppercase;flex:none;width:64px;text-align:right">'+b.status+'</span></div>';
   }).join('')+'</div>';
 }).join('');
 var ch=(D.diff&&D.diff.bot_changes)||[];
 var pill=function(txt,cc){return '<span style="text-transform:uppercase;font-weight:700;font-size:13px;letter-spacing:.03em;color:'+cc+'">'+esc(txt)+'</span>';};
 var chH=ch.length?`<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#ff4d6d"></span><h3>ACCESS CHANGED SINCE LAST CRAWL</h3><span class="meta">since ${esc((D.diff&&D.diff.since)||'')}</span></div><div class="apbox" style="padding:10px 22px 14px">`+ch.map(function(c){var cc=col[c.now]||'#ff4d6d';return '<div style="display:flex;align-items:baseline;gap:9px;padding:6px 0;font-size:14px"><b style="min-width:150px">'+esc(c.bot)+'</b>'+pill(c.was,'#8b8b81')+'<span style="color:#8b8b81">&rarr;</span>'+pill(c.now,cc)+'</div>';}).join('')+`</div></section>`:'';
 var headline;
 if(!ac.has_robots)headline='<span style="color:#ff4d6d">No robots.txt found - every bot is allowed by default. Fine for citation, but you have no control lever.</span>';
 else if(servingBlocked.length)headline='<span style="color:#ff4d6d"><b>'+servingBlocked.length+' citation bot'+(servingBlocked.length==1?'':'s')+' restricted</b> ('+servingBlocked.map(function(b){return esc(b.name)}).join(', ')+') - those engines cannot fully cite you.</span>';
 else headline='<span style="color:#f2f0e4"><b>All citation bots allowed.</b> '+allowedN+' of '+bots.length+' AI bots allowed overall.</span>';
 var searchRisk=bots.filter(function(b){return (b.name=='Googlebot'||b.name=='Bingbot')&&fwBlocked(b);});
 var cfBlk=serving.filter(function(b){return fwBlocked(b)&&b.cause=='Cloudflare';});
 var cfNote=cfBlk.length?'<div class="qd" style="margin-top:6px;color:#ff4d6d"><b>Cloudflare is the blocker.</b> '+cfBlk.map(function(b){return esc(b.name)}).join(', ')+' hit a Cloudflare 403 - most often its default "Block AI Scrapers and Crawlers" rule (on by default for domains created after Jul 2025). Turn that off or add a verified-bot allowlist so the citation bots get through.</div>':'';
 var probeCaveat='<div class="qd" style="margin-top:6px;font-style:italic">Caveat: we probe from a generic client, not the bot&#39;s real IP, so a WAF that verifies bots by IP may be blocking our impersonation while the real bot gets through. Treat a firewall block as a strong flag, then confirm in robots.txt and your server logs.</div>';
 var reachNote=searchRisk.length
   ?'<div class="qd" style="margin-top:8px;color:#ff4d6d"><b>Search crawler blocked.</b> '+searchRisk.map(function(b){return esc(b.name)+' ('+b.reach+')'}).join(', ')+' is firewall-blocked, a Google/Bing <b>indexing</b> risk, not just an AI one. A WAF "block AI training" rule can 403 the search crawlers too (Cloudflare formalises this on 15 Sep 2026).</div>'+probeCaveat
   :(reach.status=='bad'
     ?'<div class="qd" style="margin-top:8px;color:#ff4d6d">Live WAF test: '+esc(reach.detail||'')+'. A bot that robots.txt "allows" can still be blocked at the firewall.</div>'+probeCaveat
     :'<div class="qd" style="margin-top:8px">Live WAF test (citation-serving bots): '+esc(reach.detail||'reachable')+'.</div>');
 var noidx=(D.pages||[]).filter(function(p){return p.cs&&p.cs.noindex=='bad'});
 var noidxNote=noidx.length
   ?'<div class="qd" style="margin-top:6px;color:#ff4d6d">Page level: '+noidx.length+' of '+(D.pages||[]).length+' pages are noindexed - excluded from AI citation regardless of bot access (see the Indexable check).</div>'
   :'<div class="qd" style="margin-top:6px">Page level: all '+(D.pages||[]).length+' pages are indexable - no per-page noindex suppressing citation.</div>';
 var _allowBots=[];serving.forEach(function(b){if(_allowBots.indexOf(b.name)<0)_allowBots.push(b.name);});['Googlebot','Bingbot'].forEach(function(n){if(_allowBots.indexOf(n)<0)_allowBots.push(n);});
 var _allowText=_allowBots.join(String.fromCharCode(10));
 var allowGuide=(servingBlocked.length||searchRisk.length)?'<div style="margin-top:12px;border:1px solid #2a2a24;background:#0c0c09"><div style="display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 14px;border-bottom:1px solid #2a2a24"><span class="qd" style="font-size:12px">Allow these citation + search bots at your WAF / Cloudflare (a verified-bot allow rule, or a user-agent allowlist)</span><span onclick="csCopyAllow(this)" style="font-size:11px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:#a8a495;cursor:pointer;flex:none">Copy</span></div><pre id="csAllowPre" style="margin:0;padding:12px 14px;font-family:var(--mono);font-size:13px;line-height:1.6;color:#d8d5c8;overflow-x:auto">'+esc(_allowText)+'</pre></div>':'';
 return `<div class="ap2"><div class="apmain">
   <div class="apsum" style="padding:24px 28px">
     <div class="apk">AI-CRAWLER EXPOSURE &middot; ADVISORY</div>
     <div style="font-size:14px;line-height:1.65;color:#a8a495;max-width:740px;margin-top:10px">Crawler access is the on/off switch for AI citation, and it is becoming a battleground (Cloudflare AI-blocking, pay-per-crawl, page-level controls). A blocked <b>citation</b> bot means that engine literally cannot quote you; a blocked <b>training</b> bot is a legitimate content-protection choice that does not stop live-search citation. This matrix pairs your robots.txt rules with a <b>live firewall probe</b> of the citation-serving bots (the ones that fetch at answer time), because a robots.txt "allow" means nothing if the firewall 403s the bot.</div>
     <div style="margin-top:14px;font-size:14px;line-height:1.55">${headline}${reachNote}${cfNote}${allowGuide}${noidxNote}</div>
   </div>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>AI-BOT ACCESS MATRIX</h3><span class="meta">${allowedN} of ${bots.length} allowed &middot; from robots.txt</span></div><div class="apbox" style="padding:2px 22px 16px">${grid}</div></section>
   ${(function(){var la=D.log_analysis;if(!la)return '';
     var names=Object.keys(la.bots||{});
     if(!names.length) return '<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#8b8b81"></span><h3>REAL AI-BOT ACTIVITY (SERVER LOG)</h3><span class="meta">'+(la.parsed||0).toLocaleString()+' lines, no AI-bot hits</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.6">Parsed '+(la.parsed||0).toLocaleString()+' log lines and found no AI-bot requests. Either the engines are not fetching you yet, or the log window is too short.</div></div></section>';
     var rows=names.map(function(nm){var b=la.bots[nm];
       var stH=Object.keys(b.statuses).map(function(s){var bad=(s=='401'||s=='403'||s=='429');return '<span style="font-family:var(--mono);font-size:12.5px;color:'+(bad?'#db0632':(s=='200'?'#f2f0e4':'#8b8b81'))+'">'+s+':'+b.statuses[s]+'</span>';}).join(' ');
       var badge=b.role=='serving'?'<span style="font-size:11px;font-weight:800;color:#f2f0e4;margin-left:5px">CITATION</span>':'<span style="font-size:11px;font-weight:800;color:#8b8b81;margin-left:5px">TRAINING</span>';
       var ct=b.citation_time?'<span title="fetches at citation time = live citation activity" style="font-size:11px;font-weight:800;color:#f2f0e4;margin-left:4px">LIVE</span>':'';
       return '<div style="padding:9px 0;border-top:1px solid var(--line);display:flex;align-items:center;gap:10px"><span style="font-weight:600;font-size:14px;width:200px;flex:none">'+esc(nm)+badge+ct+'</span><span style="font-family:var(--mono);font-size:14px;font-weight:600;width:66px;flex:none">'+(b.hits||0).toLocaleString()+'</span><span class="qd" style="font-size:13px;flex:1;min-width:0">'+stH+(b.blocked?' &middot; <span style="color:#ff4d6d">'+b.blocked+' blocked</span>':'')+' &middot; '+b.days_seen+'d</span></div>';
     }).join('');
     return '<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>REAL AI-BOT ACTIVITY (SERVER LOG)</h3><span class="meta">'+(la.parsed||0).toLocaleString()+' lines &middot; '+(la.total_bot_hits||0).toLocaleString()+' AI-bot hits</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.65;margin-bottom:12px">Ground truth, not the probe estimate: the real AI bots, from their real IPs, and what they fetched. A <b>CITATION</b> bot with 200s is actively crawling you; a <b>LIVE</b> bot (ChatGPT-User, Perplexity-User, Claude-User) fetching is a citation happening in real time. Blocks here are real firewall denials of the real bot, the answer the reachability probe can only estimate.</div><div style="display:flex;gap:10px;font-size:12px;font-weight:800;letter-spacing:.5px;color:var(--muted);text-transform:uppercase;padding-bottom:2px"><span style="width:200px">Bot</span><span style="width:66px">Hits</span><span>Status &middot; blocks &middot; days seen</span></div>'+rows+'</div></section>';
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
 var bandcol={high:'#f2f0e4',medium:'#8b8b81',low:'#ff4d6d'};
 var gchip=function(txt){return '<span style="font-size:12.5px;padding:1px 7px;border-radius:0;margin-right:5px;background:rgba(242,240,228,.14);color:#f2f0e4">'+txt+'</span>';};
 var td='padding:9px 12px;border-top:1px solid var(--line)';
 var th='padding:9px 12px;position:static;background:#191914';
 var rows=(g.pages||[]).map(function(p){var col=bandcol[p.band]||'#8b8b81';var fc=(p.figures>=15?'#f2f0e4':(p.figures>=6?'#a8a495':'#8b8b81'));
   var sc='';
   if(p.firsthand)sc+=gchip('first-hand');
   if(p.proprietary)sc+=gchip('proprietary');
   if(p.datatable)sc+=gchip('data table');
   if(p.dup)sc+='<span style="font-size:12.5px;padding:1px 7px;border-radius:0;background:rgba(255,156,136,.14);color:#ff4d6d">near-dup</span>';
   if(!sc)sc='<span class="qd" style="font-size:13px">figures only</span>';
   return '<tr>'
    +'<td style="'+td+';font-weight:600">'+esc(pl(p.url))+'</td>'
    +'<td style="'+td+';text-align:center"><span style="color:'+col+';font-weight:700;text-transform:uppercase;font-size:13px">'+p.band+'</span></td>'
    +'<td style="'+td+';text-align:center;color:'+fc+'">'+p.figures+'</td>'
    +'<td style="'+td+'">'+sc+'</td></tr>';}).join('');
 var tile=function(lbl,n,col){return '<div style="flex:1;background:#191914;border:1px solid var(--line);border-radius:0;padding:14px 16px;text-align:center"><div style="font-size:26px;font-weight:800;color:'+col+'">'+n+'</div><div class="qd" style="font-size:12.5px;text-transform:uppercase;letter-spacing:.05em;margin-top:2px">'+lbl+'</div></div>';};
 return `<div class="ap2"><div class="apmain">
   <div class="apsum" style="padding:24px 28px">
     <div class="apk">INFORMATION GAIN &middot; ADVISORY (PROXY, NOT SCORED)</div>
     <div style="font-size:14px;line-height:1.65;color:#a8a495;max-width:740px;margin-top:10px">Original data is the one moat AI cannot route around - pages that state their own numbers, first-hand research and named frameworks get cited where derivative rehash does not (Indig: 15+ distinct figures = information-gain 62.1 vs 40.2 for &le;1). A local crawler <b>cannot prove true originality</b> (no web-corpus to diff against), so this is a labelled <b>proxy</b>: distinct-figure density, first-hand-research language, a named proprietary asset, and real data tables - minus near-duplication.</div>
     <div style="display:flex;gap:12px;margin-top:18px">${tile('original / high',b.high||0,'#f2f0e4')}${tile('some / medium',b.medium||0,'#8b8b81')}${tile('thin / low',b.low||0,'#ff4d6d')}</div>
     ${(function(){var at=g.atbar||0,med=g.median_figs||0;var bc=at?'#f2f0e4':'#ff4d6d';var mc=med>=15?'#f2f0e4':(med>=6?'#8b8b81':'#ff4d6d');
       var verdict=med>=15?'Your typical page already clears the bar - hold it as you publish.':(at?'Some pages clear it; lift your thin / low pages to the same 15+ bar.':'No page yet clears the bar - adding sourced, specific numbers is the fastest originality lift you have.');
       return '<div style="margin-top:14px;padding:13px 18px;border:1px solid var(--line);border-radius:0;background:#191914;font-size:14px;line-height:1.65;color:#a8a495"><b style="color:'+bc+'">'+at+' of '+tot+' page'+(tot===1?'':'s')+'</b> carry the <b>15+ distinct data points</b> that top-3-ranked pages average (On-Page.ai, 150 pages: 15+ figures &rarr; information-gain 62.1 vs 40.2 for &le;1). Your median page has <b style="color:'+mc+'">'+med+' data point'+(med===1?'':'s')+'</b>. '+verdict+'</div>';})()}
   </div>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>PAGES BY INFORMATION-GAIN PROXY</h3><span class="meta">${tot} pages &middot; ranked most original first</span></div>
     <div class="apbox" style="padding:0"><table style="width:100%;border-collapse:collapse"><thead><tr style="text-align:left"><th style="${th}">Page</th><th style="${th};text-align:center">Band</th><th style="${th};text-align:center">Figures</th><th style="${th}">Original-data signals</th></tr></thead><tbody>${rows||'<tr><td colspan="4" style="padding:14px">no pages</td></tr>'}</tbody></table></div></section>
   </div>
   <div class="apside">
     <div class="card2"><h3>Do this first</h3><div class="qd" style="line-height:1.6">Take your <b>thin / low</b> pages and add something only you can say: a first-hand result, an original statistic with its source, a named framework, or a data table. One genuinely original figure beats ten borrowed ones.</div></div>
     <div class="card2"><h3>Why it's a proxy</h3><div class="qd" style="line-height:1.6">True information gain needs a web-corpus comparison this local tool does not do. These signals <b>correlate</b> with original content but cannot confirm novelty - so it is an honest directional read, not part of the Rubric. The underlying stat / citation / sourced checks already feed the Trusted pillar.</div></div>
   </div></div>`;
}
function offpageView(){
 var o=D.offpage||{declared:[],missing:[],sameas_count:0};
 var pill=function(t,ok){return '<span style="display:inline-block;font-size:13px;font-weight:600;padding:4px 11px;border-radius:999px;margin:3px 4px 3px 0;'+(ok?'color:#f2f0e4;background:rgba(242,240,228,.12);border:1px solid rgba(242,240,228,.3)':'color:#ff4d6d;background:rgba(255,77,109,.1);border:1px solid rgba(255,77,109,.28)')+'">'+t+'</span>';};
 var declared=(o.declared||[]).map(function(d){return pill(d+' ✓',true)}).join('')||'<span class="qd">None declared in your schema.</span>';
 var missing=(o.missing||[]).map(function(m){return pill(m,false)}).join('')||'<span class="qd">You declare all the high-value surfaces - now earn active, well-reviewed presence on each.</span>';
 var plays=[
   ['Claim your Trustpilot profile','Claiming a Trustpilot review profile lifted AI citation rate from 1% to 54% (Trustpilot / Seer study). Highest-ROI single move.'],
   ['Get reviews on G2 / Capterra','Software categories with ~10% more G2 reviews average ~2% more AI citations (Kevin Indig / G2).'],
   ['Build Reddit + LinkedIn presence','Shopify: 44.5K Reddit + 15.4K LinkedIn mentions behind 45K AI mentions; Reddit is cited in ~1 in 5 AI answers (Semrush AI Visibility Index 2026).'],
   ['Publish original research for digital PR','Houzz\'s trends study earned 180+ backlinks from 81 domains and was cited in 188+ AI prompts (Semrush).'],
   ['Get into "best of" roundups','~90% of third-party AI mentions come from listicles / comparison / review roundups, and being in the top 3 of that page matters most (AirOps).']
 ];
 var playsH=plays.map(function(p,i){return '<div style="display:flex;gap:12px;padding:12px 0;'+(i?'border-top:1px solid var(--line)':'')+'"><span style="flex:none;width:22px;height:22px;border-radius:50%;background:#f2f0e4;color:#14140f;font-weight:800;font-size:13px;display:flex;align-items:center;justify-content:center">'+(i+1)+'</span><div><div style="font-weight:700;font-size:14px">'+p[0]+'</div><div class="qd" style="line-height:1.55;margin-top:2px">'+p[1]+'</div></div></div>';}).join('');
 return `<div class="ap2"><div class="apmain">
   <div class="apsum" style="padding:24px 28px">
     <div class="apk">OFF-PAGE PRESENCE &middot; ADVISORY (NOT SCORED)</div>
     <div style="font-size:14px;line-height:1.65;color:#a8a495;max-width:730px;margin-top:10px">Rubric audits your pages, but AI citation is dominated by <b>off-page</b> signals this on-page crawl cannot measure. A brand's own site is cited in only <b>~16%</b> of AI responses; the other ~84% are third-party sources (Reddit, Facebook Groups, YouTube, review sites, roundups), and brand mentions correlate with citation far more than backlinks (0.664 vs 0.218). This tab is directional guidance, not a score.</div>
   </div>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>PROFILES YOU DECLARE</h3><span class="meta">from your schema sameAs (${o.sameas_count} links)</span></div><div class="apbox" style="padding:14px 20px">${declared}</div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>MENTION DIVERSITY</h3><span class="meta">${(o.declared||[]).length} distinct declared domain${(o.declared||[]).length===1?'':'s'} &middot; parametric-authority proxy</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.65">What an AI model already <b>knows</b> about you, before it retrieves anything, is <b>parametric authority</b>. Research shows that only becomes reliable when your name appears in <b>varied phrasing across many independent sources</b>, not from self-publishing (a fact seen in too few, too-similar sources can sit in a model at near-zero recall). You currently declare <b>${(o.declared||[]).length}</b> distinct third-party domain${(o.declared||[]).length===1?'':'s'} in your schema. Treat that as a floor, not a measurement: real mention diversity, how many independent sites describe you, needs a backlink / mention tool. The more independent domains describe you, and the more varied the wording, the more reliably AI names you by default. This is slow and third-party-built, years not campaigns.</div></div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>HIGH-VALUE SURFACES TO SECURE</h3><span class="meta">AI-cited surfaces not in your declared set</span></div><div class="apbox" style="padding:14px 20px">${missing}<div class="qd" style="margin-top:10px;line-height:1.5">"Declared" only means present in your schema - it does not confirm an active, well-reviewed profile. Verify each, because these are where AI looks.</div></div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>DO THIS OFF-PAGE (data-backed)</h3><span class="meta">ranked by evidence</span></div><div class="apbox" style="padding:4px 20px 14px">${playsH}</div></section>
   <section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>REVIEWS &amp; AI RECOMMENDATION</h3><span class="meta">recency + volume signal, plus 2026 compliance</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.65">AI assistants disproportionately recommend <b>well-reviewed</b> businesses, and for ecommerce only <b>~2.8%</b> of AI citations are brand-owned - the rest are review roundups and third-party "best of" pages (Shero 2026). Review <b>recency and volume</b> are the signal: a steady stream of recent, specific reviews on the surfaces AI reads (Google, Trustpilot, G2, Capterra, industry roundups) moves AI recommendation more than most on-page changes. Expose your real ratings in <b>Review / AggregateRating schema</b> so engines can read them machine-readably.</div><div style="margin-top:12px;padding:12px 15px;border:1px solid rgba(219,6,50,.35);border-radius:9px;background:rgba(219,6,50,.06);font-size:14px;line-height:1.6;color:#ff4d6d"><b>Compliance flag (Google, Apr 2026):</b> Google now bans <b>staff review quotas</b> and <b>soliciting reviews by an individual staff member&#39;s name</b>. If your review-gathering does either, change it - violations risk review removal and profile action, which strips the exact signal AI leans on. Ask for reviews of the <b>business</b>, never on a quota, never by a named employee.</div></div></section>
   ${(function(){var q=D.query_coverage;if(!q)return '';
     var km=q.mode==='keywords';
     var gaps=(q.gaps||[]).map(function(g){return '<div style="display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line)"><span style="font-size:14px">'+esc(g.query)+'</span><span class="qd" style="white-space:nowrap;font-family:var(--mono);font-size:12.5px">'+(km?'':g.citations+' &middot; ')+'match '+Math.round((g.best_score||0)*100)+'%</span></div>';}).join('');
     var orph=(q.orphans||[]).map(function(o){return '<span style="font-size:13px;padding:3px 9px;border-radius:999px;background:rgba(139,132,128,.12);border:1px solid var(--line)">'+esc(o.path)+' <b>'+o.score+'</b></span>';}).join('');
     return '<section class="aptier"><div class="aptierh"><span class="sq" style="width:9px;height:9px;border-radius:2px;background:#f2f0e4"></span><h3>'+(km?'KEYWORD COVERAGE':'QUERY COVERAGE')+'</h3><span class="meta">'+q.covered+'/'+q.n_queries+(km?' of your keywords have a page targeting them':' of your queries have a page targeting them')+'</span></div><div class="apbox" style="padding:14px 20px"><div class="qd" style="line-height:1.6;margin-bottom:12px">'+(km?'These are the keywords you flagged as important. A <b>gap</b> is a keyword no page of yours targets, the clearest opportunity to build. An <b>orphan</b> is a well-scored page that targets none of your keywords, effort not aimed at your priorities. Matching is title, meta and URL term overlap, so treat it as directional. Paste a Bing AI Performance or Search Console export (query and a count per line) to weight these by real demand.':'These are the queries you provided, ranked by the count you pasted (AI citations or search clicks). A <b>gap</b> is a high-count query no page of yours targets, the clearest opportunity. An <b>orphan</b> is a well-scored page that targets none of your queries, effort not aimed at demand. Matching is title, meta and URL term overlap, so treat it as directional.')+'</div>'
       +(q.n_gaps?'<div style="font-size:12.5px;font-weight:800;letter-spacing:.5px;color:#ff4d6d;text-transform:uppercase;margin:6px 0 8px">Coverage gaps ('+q.n_gaps+')</div>'+gaps:'<div class="qd">'+(km?'Every keyword has a page targeting it. Strong.':'Every query has a page targeting it. Strong.')+'</div>')
       +(q.n_orphans?'<div style="font-size:12.5px;font-weight:800;letter-spacing:.5px;color:var(--muted);text-transform:uppercase;margin:16px 0 8px">Orphaned pages ('+q.n_orphans+'), well-scored but targeting '+(km?'none of your keywords':'none of your queries')+'</div><div style="display:flex;flex-wrap:wrap;gap:6px">'+orph+'</div>':'')
       +'</div></section>';
   })()}
   </div>
   <div class="apside">
     <div class="card2"><h3>Why this isn't scored</h3><div class="qd" style="line-height:1.6">Off-page presence and brand mentions live outside your site, so an on-page crawler cannot measure them without a backlink / mention API. Rather than fake a number, this tab shows what you declare and where to build - the same honest treatment as the Grok and llms.txt advisories.</div></div>
     <div class="card2"><h3>Measure it properly</h3><div class="qd" style="line-height:1.6">To track real off-page presence use a backlink tool (Ahrefs / Semrush referring domains) plus an AI-visibility tracker for brand-mention share. First move with the biggest evidence: claim your Trustpilot profile (1% &rarr; 54% citation lift).</div></div>
     <div class="card2"><h3>Grounding queries &amp; Citation Share</h3><div class="qd" style="line-height:1.6"><b>Grounding queries</b> are the retrieval searches an AI runs before it answers; <b>Citation Share</b> and <b>Share of Authority</b> are how often your domain is cited, and cited versus competitors. An on-page crawl cannot see these. <b>Microsoft Clarity's Topic Insights</b> (free, if Clarity is on your site) exposes all three - it groups AI citations by topic so you can find coverage gaps and compare grounding queries to your existing content.</div></div>
   </div></div>`;
}
function exportBroken(){var bl=D.broken_links||[];var rows=[['status','broken_url','source_count','source_pages']];bl.forEach(function(b){rows.push([b.status==null?'dead':b.status,b.url,b.sources.length,b.sources.join(' | ')]);});dl(_fn('broken-links.csv'),rows);}
function brokenView(){
 var bl=D.broken_links||[], dom=D.domain||'';
 var isInt=function(u){return u.indexOf('//'+dom)>=0||u.indexOf('//www.'+dom)>=0};
 if(!bl.length) return `<div class="wrap"><div class="sech">Broken outbound links</div><div class="panel"><div style="display:flex;align-items:center;gap:10px"><span style="width:9px;height:9px;border-radius:50%;background:#f2f0e4"></span><b>No broken links found.</b></div><div class="qd" style="margin-top:8px">Checked every outbound content link across the crawl. Only genuinely dead targets count (404/410/5xx); 403/429 bot-blocks and timeouts are excluded to avoid false positives.</div></div></div>`;
 var internal=bl.filter(b=>isInt(b.url));
 var rows=bl.slice().sort((a,c)=>((isInt(a.url)?0:1)-(isInt(c.url)?0:1))||(c.sources.length-a.sources.length));
 var th=t=>`<th style="padding:10px;font-size:12.5px;color:#8b8b81;font-weight:600;text-align:left;position:static;background:transparent">${t}</th>`;
 var body=rows.map(function(b){
   var ii=isInt(b.url);
   var srcs=b.sources.map(u=>`<a href="${esc(u)}" target="_blank" style="color:#a8a495">${rel(u)}</a>`).join(', ');
   return `<tr style="border-top:1px solid var(--line)">
     <td style="padding:9px 10px;vertical-align:top"><span class="schip" style="background:rgba(255,77,109,.14);color:#ff4d6d">${esc(String(b.status||'dead'))}</span></td>
     <td style="padding:9px 10px;vertical-align:top;max-width:430px">${ii?'<span style="font-size:12px;font-weight:700;color:#14140f;background:#f2f0e4;padding:1px 6px;border-radius:999px;margin-right:6px">INTERNAL</span>':''}<a href="${esc(b.url)}" target="_blank" style="word-break:break-all;font-size:14px">${esc(b.url)}</a></td>
     <td style="padding:9px 10px;vertical-align:top;text-align:center;color:#a8a495">${b.sources.length}</td>
     <td style="padding:9px 10px;vertical-align:top;font-size:13px">${srcs}</td>
   </tr>`;
 }).join('');
 return `<div class="wrap">
   <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:10px"><div class="sech" style="margin:0">Broken outbound links <span class="s">${bl.length} dead${internal.length?' &middot; '+internal.length+' internal':''} &middot; 404/410/5xx only, bot-blocks excluded</span></div><button onclick="exportBroken()" style="background:var(--grn);color:#fff;border:0;border-radius:0;padding:7px 14px;font-weight:700;font-size:13px;cursor:pointer">Export CSV</button></div>
   <div class="panel" style="overflow-x:auto;padding:0;margin-top:14px">
     <table style="width:100%;border-collapse:collapse">
       <thead><tr>${th('STATUS')}${th('BROKEN URL')}<th style="padding:10px;font-size:12.5px;color:#8b8b81;font-weight:600;text-align:center;position:static;background:transparent">ON</th>${th('SOURCE PAGES')}</tr></thead>
       <tbody>${body}</tbody>
     </table>
   </div>
   <div class="qd" style="margin-top:10px">Internal broken links are top priority. 403/429 (bot-blocked) links and timeouts are deliberately excluded to avoid false positives. The full list is also in the CSV / JSON export.</div>
 </div>`;
}
function _band(s){return s>=85?['Strong','g']:s>=70?['Quotable','g']:s>=55?['At risk','a']:['Weak','r'];}
function _pm(p){return {Known:'Do the engines know you exist?',Findable:'Can they find your answer?',Trusted:'Do they trust you enough to cite you?'}[p]||'';}
function _crb(v){return v>=75?'#1c7f29':v>=50?'#a06a12':'#b23a2b';}
function esc2(s){return (s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function rel2(u){return esc2((u||'').replace(D.origin,'')||'/');}
function _rollup(){var CR={};(D.pages||[]).forEach(function(p){(p.checks||[]).forEach(function(c){var r=CR[c.id]||(CR[c.id]={id:c.id,label:c.label,pillar:c.pillar,ch:c.ch,ev:c.ev,good:0,warn:0,bad:0,na:0});r[c.status]=(r[c.status]||0)+1;});});return CR;}

function printReport(){
 var d=D,wl=!!d.client,db=!!d._debrand,brand=(wl||db)?(d.agency||'AI Search'):'Rubric';
 var band=_band(d.overall),CR=_rollup();
 var engs=['ChatGPT','Perplexity','AI Overviews','Gemini','Copilot','Claude'].slice().sort(function(a,b){return d.engines[a]-d.engines[b];});
 var issues=(d.issues||[]).slice();
 var strengths=Object.keys(CR).map(function(k){return CR[k];}).filter(function(r){var app=r.good+r.warn+r.bad;return app>0&&r.good>=app*0.85&&r.good>=3;}).sort(function(a,b){return b.good-a.good;});
 var nbad=issues.filter(function(i){return i.severity=='bad';}).length, nwarn=issues.filter(function(i){return i.severity=='warn';}).length;
 var H='<div class="crep">';

 // COVER
 var mark="<svg viewBox='0 0 200 200' width='30' height='30' style='flex:none'><circle cx='100' cy='100' r='86' fill='none' stroke='#1c7f29' stroke-width='16'/></svg>";
 var LG=window.__LOGO_DARK__||'';                     // black-text ring lockup for the printed (white) page
 var brandmark=(wl||db)?('<span><span class="wm">'+esc2(d.agency||'')+'</span></span>'):(LG?'<img src="'+LG+'" alt="Rubric" style="height:30px;flex:none">':(mark+'<span><span class="wm">Rubric</span></span>'));
 H+='<div class="cr-cover"><div class="cr-mast">'+brandmark
   +'<span class="meta">'+esc2(d.generated)+'<br>'+d.pages_crawled+' pages analysed</span></div>';
 H+='<div class="cr-title">'+(wl?'AI Search Audit':'AI Search Citability Report')+'</div><div class="cr-dom">'+esc2(d.domain)+'</div>';
 H+='<div class="cr-hero"><div class="cr-bigscore">'+d.overall+'<small>/100</small></div><div class="cr-verdict">'
   +'<span class="cr-vchip cr-tag '+band[1]+'">'+band[0]+' &middot; '+(d.overall>=70?'quotable':'below the 70 line')+'</span>'
   +'<div class="cr-vlead">This is how citable '+esc2(d.domain)+' is to AI engines today &mdash; and exactly what would move it higher.</div></div></div></div>';

 // 01 OVERVIEW
 var weak=engs[0], strongPill=['Known','Findable','Trusted'].sort(function(a,b){return d.pillars[b]-d.pillars[a];})[0];
 var lead='<b>'+esc2(d.domain)+' scores '+d.overall+'/100</b> for AI citability across six engines, weighted for how each one selects sources. '
   +(d.overall>=70?'The foundation is solid &mdash; ':'It sits below the 70-point line engines treat as quotable &mdash; ')
   +'its strongest footing is <b>'+strongPill+'</b> ('+d.pillars[strongPill]+') and its lowest engine readiness is <b>'+weak+'</b> ('+d.engines[weak]+'). '
   +'We found <b>'+nbad+' blocking issues</b> and <b>'+nwarn+' weakening ones</b> across '+d.pages_crawled+' pages, against <b>'+strengths.length+' checks the site already passes</b>. Fixing the items in section 4 projects the score to <b>'+d.proj_all+'/100</b>.';
 H+='<div class="cr-sec"><div class="cr-sechead"><span class="cr-num">01</span><h2>Overview</h2><span class="cr-sub">the headline</span></div>';
 H+='<p class="cr-lead">'+lead+'</p>';
 H+='<div class="cr-grp">The three questions engines ask</div><div class="cr-bars">';
 ['Known','Findable','Trusted'].forEach(function(p){var v=d.pillars[p];H+='<div class="cr-bar"><div class="bl">'+p+'<small>'+_pm(p)+'</small></div><div class="bt"><i style="width:'+v+'%;background:'+_crb(v)+'"></i></div><div class="bv">'+v+'</div></div>';});
 H+='</div>';
 H+='<div class="cr-grp">Readiness by engine &middot; weakest first</div><div class="cr-bars">';
 engs.forEach(function(e){var v=d.engines[e];H+='<div class="cr-bar"><div class="bl">'+e+'</div><div class="bt"><i style="width:'+v+'%;background:'+_crb(v)+'"></i></div><div class="bv">'+v+'</div></div>';});
 H+='</div>';
 H+='<div class="cr-chips">'
   +'<div class="cr-chip"><div class="n">'+d.pages_crawled+'</div><div class="l">Pages analysed</div></div>'
   +'<div class="cr-chip"><div class="n" style="color:var(--red)">'+nbad+'</div><div class="l">Blocking issues</div></div>'
   +'<div class="cr-chip"><div class="n" style="color:var(--amb)">'+nwarn+'</div><div class="l">Weakening issues</div></div>'
   +'<div class="cr-chip"><div class="n" style="color:var(--grn)">'+strengths.length+'</div><div class="l">Checks passed</div></div></div></div>';

 // 02 WHAT'S WORKING
 H+='<div class="cr-sec"><div class="cr-sechead"><span class="cr-num">02</span><h2>What&rsquo;s already working</h2><span class="cr-sub">the foundation</span></div>';
 H+='<p class="cr-lead">Before the fixes, here is what the site does well &mdash; the signals AI engines reward that are already in place. Protect these.</p>';
 ['Known','Findable','Trusted'].forEach(function(p){var ss=strengths.filter(function(s){return s.pillar==p;});if(!ss.length)return;
   H+='<div class="cr-grp">'+p+'</div>';
   ss.forEach(function(s){var app=s.good+s.warn+s.bad;H+='<div class="cr-item"><div class="cr-ic g">&#10003;</div><div class="cr-it"><div class="t">'+esc2(s.label)+'</div><div class="why">'+esc2(s.ev||'')+'</div></div><div class="cr-meta-r">'+s.good+'/'+app+' pages<br>'+esc2(s.ch||'')+'</div></div>';});
 });
 H+='</div>';

 // 03 WHAT'S WRONG
 H+='<div class="cr-sec"><div class="cr-sechead"><span class="cr-num">03</span><h2>What&rsquo;s holding you back</h2><span class="cr-sub">'+issues.length+' findings</span></div>';
 H+='<p class="cr-lead">Every issue the crawl surfaced, grouped by the question it affects. <b>Blocking</b> items stop citation outright; <b>weakening</b> items reduce it. The page counts show how widespread each is.</p>';
 ['Known','Findable','Trusted'].forEach(function(p){var is=issues.filter(function(i){return i.pillar==p;}).sort(function(a,b){return (a.severity=='bad'?0:1)-(b.severity=='bad'?0:1)||b.count-a.count;});if(!is.length)return;
   H+='<div class="cr-grp">'+p+' &middot; '+_pm(p)+'</div>';
   is.forEach(function(i){var sv=i.severity=='bad'?'r':'a';H+='<div class="cr-item"><div class="cr-ic '+sv+'">'+(i.severity=='bad'?'&#10007;':'!')+'</div><div class="cr-it"><div class="t">'+esc2(i.label)+' <span class="cr-tag '+sv+'">'+(i.severity=='bad'?'blocking':'weakening')+'</span></div><div class="why">'+esc2(i.ev||'')+'</div></div><div class="cr-meta-r">'+i.count+' pages<br>'+esc2(i.ch||'')+'</div></div>';});
 });
 // site-level findings
 var sm=d.sitemap||{},sf=[];
 if(sm.orphan_n)sf.push([sm.orphan_n+' orphan pages','No internal links point to them, so crawlers may never reach them.']);
 if(sm.missing_n)sf.push([sm.missing_n+' pages missing from the sitemap','Indexable pages absent from the XML sitemap are harder to discover.']);
 if(sm.noindex_n)sf.push([sm.noindex_n+' noindexed pages in the sitemap','A sitemap should list only indexable URLs.']);
 if((d.broken_links||[]).length)sf.push([(d.broken_links.length)+' broken outbound link(s)','Dead links signal neglect and break the trust chain.']);
 if(d.agentready&&d.agentready.money_n)sf.push([(d.agentready.money_n-(d.agentready.any_n||0))+' money pages not agent-ready','Missing offer / price / action signals stop an AI agent transacting.']);
 if((d.redirect_home||[]).length)sf.push([(d.redirect_home.length)+' deep pages redirect to home','Any AI citation of those URLs is wasted.']);
 if(sf.length){H+='<div class="cr-grp">Site-level findings</div>';sf.forEach(function(x){H+='<div class="cr-item"><div class="cr-ic a">!</div><div class="cr-it"><div class="t">'+esc2(x[0])+'</div><div class="why">'+esc2(x[1])+'</div></div><div class="cr-meta-r">structural</div></div>';});}
 H+='</div>';

 // 04 WHAT TO FIX
 var ordered=issues.slice().sort(function(a,b){return (b.gain_overall||0)-(a.gain_overall||0)||(a.severity=='bad'?0:1)-(b.severity=='bad'?0:1)||b.count-a.count;});
 H+='<div class="cr-sec"><div class="cr-sechead"><span class="cr-num">04</span><h2>What to fix, in order</h2><span class="cr-sub">prioritised by impact per effort</span></div>';
 H+='<p class="cr-lead">The same findings as an ordered plan &mdash; highest score movement per hour of work first. Each shows the concrete fix, the effort, and the projected point gain.</p>';
 ordered.forEach(function(i,x){var sv=i.severity=='bad'?'r':'a';H+='<div class="cr-item"><div class="cr-ic '+sv+'">'+(x+1)+'</div><div class="cr-it"><div class="t">'+esc2(i.label)+'</div><div class="fix">'+esc2(i.fix||'')+'</div></div><div class="cr-meta-r">'+(i.gain_overall>0?'<b>+'+i.gain_overall+' overall</b><br>':'<b>&lt;+1 overall</b><br>')+(i.top_engine&&i.top_engine_gain>0?i.top_engine+' +'+i.top_engine_gain+'<br>':'')+i.effort+' &middot; '+i.count+' pages</div></div>';});
 // roadmap
 var lbl={}; issues.forEach(function(i){lbl[i.id]=i.label;});
 var ph=d.plan_phases||{},pt={'1':['Days 0&ndash;30','Foundation'],'2':['Days 30&ndash;60','Structure'],'3':['Days 60&ndash;90','Polish']};
 H+='<div class="cr-grp">The 90-day roadmap</div><div class="cr-road">';
 ['1','2','3'].forEach(function(k){var ids=ph[k]||[];H+='<div class="cr-phase"><div class="ph">'+pt[k][0]+'</div><h4>'+pt[k][1]+'</h4><ul>'+ids.map(function(id){return '<li>'+esc2(lbl[id]||id)+'</li>';}).join('')+'</ul></div>';});
 H+='</div></div>';

 // 05 EXPECTED RESULT
 var projEng={};engs.forEach(function(e){var g=0;issues.forEach(function(i){g+=(i.gain_engines&&i.gain_engines[e])||0;});projEng[e]=Math.min(100,d.engines[e]+g);});
 H+='<div class="cr-sec"><div class="cr-sechead"><span class="cr-num">05</span><h2>The result you can expect</h2><span class="cr-sub">after the plan</span></div>';
 H+='<div class="cr-proj"><div class="now"><div class="lbl">Today</div><div class="v">'+d.overall+'</div></div><div class="arrow">&rarr;</div><div class="then"><div class="lbl">All fixes applied</div><div class="v">'+d.proj_all+'</div></div>'
   +'<div class="say">Clearing the plan lifts '+esc2(d.domain)+' from <b>'+d.overall+'</b> to a projected <b>'+d.proj_all+'</b>/100 &mdash; clearing the 70-point line engines treat as quotable on most pages, and lifting the weakest engines most.</div></div>';
 H+='<div class="cr-grp">Projected readiness by engine</div><div class="cr-bars">';
 engs.forEach(function(e){var v=d.engines[e],pv=projEng[e];H+='<div class="cr-bar"><div class="bl">'+e+'</div><div class="bt"><i style="width:'+pv+'%;background:var(--grn);opacity:.35"></i><i style="width:'+v+'%;background:'+_crb(v)+';margin-top:-9px"></i></div><div class="bv">'+v+'&rarr;'+pv+'</div></div>';});
 H+='</div>';

 // APPENDIX
 var pgs=(d.pages||[]).slice().sort(function(a,b){return a.score-b.score;});
 H+='<div class="cr-sec" style="break-before:page"><div class="cr-sechead"><span class="cr-num">A</span><h2>Every page</h2><span class="cr-sub">'+pgs.length+' URLs, weakest first</span></div>';
 H+='<table><thead><tr><th>Score</th><th>Page</th><th>Known</th><th>Findable</th><th>Trusted</th><th>Type</th></tr></thead><tbody>';
 pgs.forEach(function(p){H+='<tr><td class="sc" style="color:'+_crb(p.score)+'">'+p.score+'</td><td>'+rel2(p.url)+'</td><td class="sc">'+p.pillars.Known+'</td><td class="sc">'+p.pillars.Findable+'</td><td class="sc">'+p.pillars.Trusted+'</td><td style="color:var(--muted)">'+esc2(p.type||'')+'</td></tr>';});
 H+='</tbody></table></div>';
 H+='<div class="cr-foot">Prepared with '+esc2(brand)+(wl?'':' &middot; the AI-search auditor')+'. Scores estimate citability from on-page signals; they do not measure citations directly. Projections assume the listed fixes are applied cleanly. Full per-page data is in the CSV/JSON export.</div>';

 H+='</div>';document.getElementById('printroot').innerHTML=H;window.print();
};
tabsbar();render();updExp();
"""
    if anon:
        # Anon behaviour, appended after the report's own script: gate every tab and export to the CTA,
        # and define the modal handlers. Non-anon reports never see any of this.
        js += ("\nfunction showCTA(){var m=document.getElementById('ctaModal');if(m)m.style.display='flex';var e=document.getElementById('ctaEmail');if(e)e.focus();}"
               "\nfunction ctaSubmit(btn){var box=btn.closest('#ctaModal')||btn.parentElement;var em=box.querySelector('input[type=email]'),pw=box.querySelector('input[type=password]');var email=((em&&em.value)||'').trim(),pass=((pw&&pw.value)||'');if(!email||email.indexOf('@')<1){if(em)em.focus();return;}if(pass.length<8){if(pw)pw.focus();return;}var orig=btn.textContent;btn.textContent='Creating your account\\u2026';btn.disabled=true;var jid=location.pathname.split('/').filter(Boolean).pop();fetch('/api/anon-upgrade',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify({email:email,password:pass,job_id:jid})}).then(function(r){return r.json();}).then(function(j){if(j&&j.ok){location.reload();}else{btn.textContent=orig;btn.disabled=false;alert((j&&j.error)||'Could not create your account. Try again.');}}).catch(function(){btn.textContent=orig;btn.disabled=false;alert('Network error. Try again.');});}"
               "\nwindow.printReport=function(){showCTA();};window.exportCurrent=function(){showCTA();};window.exportDevPlan=function(){showCTA();};window.exportPlan=function(){showCTA();};"
               "\nfunction stickyCTA(){try{csTrack('sticky_cta_click',{more:(window.__DATA__&&window.__DATA__._more_pages)||0});}catch(e){}showCTA();}")
    _anon_modal = ""
    if anon:
        _mdom = H.escape(d.get('domain') or 'your site')
        _mmore = d.get('_more_pages') or 0                           # real pages discovered beyond the 25
        _mlogo = ("<span aria-label='Rubric' style='display:inline-flex;align-items:baseline;gap:0;margin-bottom:26px;position:relative'>"
                  "<span style='position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap'>R</span>"
                  "<svg aria-hidden='true' viewBox='0 0 97 100' style='width:22px;height:23px;display:block;flex:none'><path fill-rule='evenodd' d='M0 0 H61.8 A35 35 0 0 1 74.8 67.5 L96.6 100 H68.6 Z M30.5 23 H55.3 A12.5 12.5 0 0 1 55.3 48 H30.5 Z' fill='#db0632'/></svg>"
                  "<span style='font-family:Archivo,sans-serif;font-weight:800;font-size:26px;letter-spacing:-1.4px;line-height:.7;margin-left:-1px;color:#f2f0e4'>ubric</span></span>")
        _mben = [
            ("<rect x='2' y='3' width='20' height='7' rx='1.5'/><rect x='2' y='14' width='20' height='7' rx='1.5'/><line x1='6' y1='6.5' x2='6.01' y2='6.5'/><line x1='6' y1='17.5' x2='6.01' y2='17.5'/>", "<span style='color:#fff'>MCP access</span> &mdash; your own AI reads your score and fixes your site"),
            ("<circle cx='11' cy='11' r='8'/><line x1='21' y1='21' x2='16.65' y2='16.65'/>", ("Crawl up to <span style='color:#fff'>500 pages</span>, not just these 25" + (f" &mdash; <span style='color:#fff'>{_mmore} more</span> waiting" if _mmore>0 else ""))),
            ("<path d='M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4'/><polyline points='7 10 12 15 17 10'/><line x1='12' y1='15' x2='12' y2='3'/>", "<span style='color:#fff'>Export</span> to PDF and CSV, and keep every report"),
            ("<path d='M3 3v18h18'/><path d='M18.7 8l-5.1 5.2-2.8-2.7L7 14'/>", "<span style='color:#fff'>Track your score</span> over time as you fix"),
        ]
        _mrows = "".join(
            "<div style='display:flex;gap:13px;align-items:flex-start'>"
            "<div style='flex:none;width:34px;height:34px;background:rgba(242,240,228,.10);display:flex;align-items:center;justify-content:center'>"
            f"<svg width='17' height='17' viewBox='0 0 24 24' fill='none' stroke='#f2f0e4' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'>{_ic}</svg></div>"
            f"<div style='font-size:14px;color:#a8a495;line-height:1.45;padding-top:6px'>{_tx}</div></div>"
            for _ic, _tx in _mben)
        _anon_modal = (
            "<div id='ctaModal' style='display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:250;align-items:center;justify-content:center;padding:24px' onclick=\"if(event.target.id=='ctaModal')this.style.display='none'\">"
            "<div style='background:#191914;border:1px solid #2a2a24;max-width:780px;width:100%;display:flex;flex-wrap:wrap;overflow:hidden;position:relative;font-family:Archivo,sans-serif;box-shadow:0 40px 110px -35px #000'>"
            "<div onclick=\"document.getElementById('ctaModal').style.display='none'\" style='position:absolute;top:15px;right:18px;color:#8b8b81;font-size:22px;line-height:1;cursor:pointer;z-index:3'>&times;</div>"
            "<div style='flex:1;min-width:310px;padding:40px 36px'>"
            f"{_mlogo}<div style='font-size:24px;font-weight:800;color:#FFFFFF;line-height:1.18;letter-spacing:-.02em;margin-bottom:7px'>Create your <span style='color:#f2f0e4'>free account</span></div>"
            f"<div style='font-size:14px;color:#8b8b81;margin-bottom:28px'>Keep crawling {_mdom} &mdash; free, no card.</div>"
            "<div style='font-size:12px;color:#8b8b81;margin-bottom:7px'>Email</div>"
            "<input id='ctaEmail' type='email' placeholder='you@company.com' style='width:100%;height:48px;background:#fff;border:1px solid #d0cec4;color:#14140f;border-radius:0;padding:0 15px;font-size:15px;font-family:inherit;margin-bottom:15px'>"
            "<div style='font-size:12px;color:#8b8b81;margin-bottom:7px'>Password</div>"
            "<input id='ctaPass' type='password' placeholder='Create a password (8+ characters)' style='width:100%;height:48px;background:#fff;border:1px solid #d0cec4;color:#14140f;border-radius:0;padding:0 15px;font-size:15px;font-family:inherit;margin-bottom:22px'>"
            "<button id='ctaBtn' onclick='ctaSubmit(this)' style='width:100%;height:50px;background:#db0632;color:#fff;border:0;border-radius:0;font-family:inherit;font-weight:800;font-size:15px;letter-spacing:.2px;cursor:pointer'>Create free account</button>"
            "<div style='font-size:11.5px;color:#6b6b65;line-height:1.5;margin-top:14px'>No card, no spam. Your report stays saved to your account.</div>"
            "<div style='font-size:13px;color:#8b8b81;margin-top:16px'>Already have an account? <a href='/login' style='color:#f2f0e4'>Log in</a></div>"
            "</div>"
            "<div style='flex:0 0 300px;min-width:260px;background:#1e1e18;border-left:1px solid #262620;padding:40px 34px;display:flex;flex-direction:column;gap:22px;justify-content:center'>"
            f"{_mrows}"
            "</div></div></div>")
    _wl=bool(d.get('client'))                                    # white-label mode when a client is set
    _debrand=bool(d.get('_debrand'))                             # Pro de-brand: drop Rubric marks even without a client name
    _cited=("<span style='display:inline-flex;align-items:baseline;gap:0;position:relative' aria-label='Rubric'>"
            "<span style='position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap'>R</span>"
            "<svg aria-hidden='true' viewBox='0 0 97 100' style='width:16px;height:17px;display:block;flex:none'><path fill-rule='evenodd' d='M0 0 H61.8 A35 35 0 0 1 74.8 67.5 L96.6 100 H68.6 Z M30.5 23 H55.3 A12.5 12.5 0 0 1 55.3 48 H30.5 Z' fill='#db0632'/></svg>"
            "<span style='font-family:Archivo,sans-serif;font-weight:800;font-size:23px;letter-spacing:-1.3px;line-height:.695;margin-left:-1px;color:var(--txt)'>ubric</span></span>")
    # default report = Rubric lockup (mark+wordmark already in the PNG); white-label keeps the client/agency treatment.
    _hdr_logo=(f"<img src=\"{d['logo']}\" alt='' style='height:30px;flex:none;max-width:220px'>" if d.get('logo') else ("" if (_wl or _debrand) else _cited))
    _hdr_wm=(f"<span class='wm'><span class='lw'>{H.escape(d.get('agency') or 'GoGoChimp')}</span></span>" if _wl else "")
    _nav=d.get('nav') or {}                                     # online chrome: new-crawl + logout links (relative to the web app origin)
    _reico="<svg viewBox='0 0 24 24' width='13' height='13' fill='none' stroke='#db0632' stroke-width='2.4' style='margin-right:6px;vertical-align:-2px'><path d='M20 12a8 8 0 1 1-2.3-5.6'/><path d='M20 4v4h-4'/></svg>"
    _navbtns=((f"<a class='navbtn' href=\"{H.escape(_nav.get('rerun'))}\">{_reico}Re-run crawl</a>" if _nav.get('rerun') else "")
             +(f"<a class='navbtn' href=\"{H.escape(_nav.get('new_crawl'))}\">New crawl</a>" if _nav.get('new_crawl') else "")
             +(f"<a class='navbtn' href=\"{H.escape(_nav.get('logout'))}\">Log out</a>" if _nav.get('logout') else ""))
    if anon:
        # Anonymous view: no account yet, so "Log out" is wrong. Exports stay visible and clickable (offer-first);
        # clicking pops the register CTA via printReport/exportCurrent rather than downloading.
        _btns_html = ("<span class='btns'>"
                      "<a class='navbtn' href='/login' style='color:#f2f0e4'>Log in</a>"
                      "<button onclick='printReport()' title='Create a free account to export'>Full report (PDF)</button>"
                      "<button id='expbtn' onclick='exportCurrent()' title='Create a free account to export'>Export all (CSV)</button>"
                      "</span>")
    else:
        _btns_html = (f"<span class='btns'>{_navbtns}<button onclick='printReport()'>Full report (PDF)</button>"
                      f"<button id='expbtn' onclick='exportCurrent()'>Export all (CSV)</button></span>")
    _sticky = ""
    if anon:
        _more = d.get('_more_pages') or 0
        # Dynamic CTA keyed to the qualifying-card answers: aim the pitch at the intent they told us.
        _ans = d.get('answers') or {}
        _own = (_ans.get('ownership') or '').lower()
        _traf = (_ans.get('traffic') or '').strip()
        if 'client' in _own:                                   # agency auditing a client -> the deliverable pitch
            _cta_lead = "Auditing a client&rsquo;s site?"
            _cta_act = "<b style='color:#fff'>white-label</b> this report as your own and crawl up to <b style='color:#fff'>500 pages</b>."
        elif _traf and _traf.lower() != 'no':                  # unsure / worried AI is eating traffic -> the evidence pitch
            _cta_lead = "Want proof of what AI is costing you?"
            _cta_act = "measure your <b style='color:#fff'>clicks at risk</b> in your own numbers, and crawl your whole site."
        else:                                                  # everyone else -> the scale pitch
            _cta_lead = (f"We found <b style='color:#fff'>{_more:,} more page{'' if _more==1 else 's'}</b> on your site"
                         if _more > 0 else "Crawl your whole site, not just these 25")
            _cta_act = "crawl up to <b style='color:#fff'>500 pages</b> and get <b style='color:#fff'>MCP access</b>."
        _sticky = ("<div id='stickybar' style='position:fixed;left:0;right:0;bottom:0;z-index:150;background:#191914;border-top:1px solid #2a2a24;padding:13px 24px;display:flex;align-items:center;gap:18px;flex-wrap:wrap;box-shadow:0 -20px 44px -28px #000'>"
                   f"<div style='flex:1;min-width:220px;font-size:14px;color:#a8a495;line-height:1.4'>{_cta_lead} &mdash; <span style='color:#f2f0e4'>create a free account</span> to {_cta_act}</div>"
                   "<button onclick='stickyCTA()' style='flex:none;height:44px;background:#db0632;color:#fff;border:0;font-family:inherit;font-weight:800;font-size:14px;letter-spacing:.2px;padding:0 22px;cursor:pointer'>Create free account</button></div>"
                   "<div style='height:78px'></div>")
    _scored = d.get('pages_crawled') or 0
    _fetched = d.get('pages_fetched') or _scored
    # Be precise: the crawler fetched N pages, but some (non-HTML, machine files like agents.md/llms.txt, redirects)
    # aren't scored. Say "23 scored of 25 crawled" rather than a bare "23 pages" that conflicts with the crawl counter.
    _pagecount = (f"{_scored} scored of {_fetched} crawled" if _fetched > _scored
                  else f"{_scored} page{'' if _scored==1 else 's'}")
    doc=("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>{'AI Search Audit' if (_wl or _debrand) else 'Rubric'}: {H.escape(d['domain'])}</title><link rel='icon' href=\"{FAVICON}\">"
         f"<style>{FONT_FACE_CSS}</style>"
         f"<style>{css}</style></head><body>"
         f"<header><span class='logo'>{_hdr_logo}{_hdr_wm}</span>"
         f"<span class='m'><a href='{H.escape(d['origin'])}' target='_blank' style='color:var(--txt);font-weight:600'>{H.escape(d['domain'])}</a> &middot; {_pagecount} &middot; {d['generated']}</span>"
         f"{_btns_html}</header>"
         + ((f"<div style='padding:14px 24px;background:rgba(242,240,228,.06);border-bottom:1px solid var(--line);font-size:14px'><span style='color:#8b8b81'>AI Search Audit prepared for</span> <b style='font-size:16px'>{H.escape(d.get('client') or '')}</b> <span style='color:#8b8b81'>by {H.escape(d.get('agency') or 'GoGoChimp')}</span>" + (f"<div style='color:#a8a495;line-height:1.6;margin-top:8px;max-width:820px'>{H.escape(d.get('intro') or '')}</div>" if d.get('intro') else "") + "</div>") if d.get('client') else "")
       + "<div class='tabs' id='tabs'></div><div id='app'><div class='wrap' id='view'></div>" + _proof_panel
       + ("<div class='foot'><b>Why citability matters:</b> AI Overviews cut organic clicks ~40% where they appear (Agarwal &amp; Sen field RCT, 2026), and pages cited in the AI Overview earn ~35% higher CTR (Seer, 2025). This report <b>estimates citability</b> for AI search from on-page, structural and technical signals. It does <b>not</b> measure citations. llms.txt and Grok are shown for reference only and are not scored.</div></div>" if _wl
          else "<div class='foot'><b>Why citability matters:</b> AI Overviews cut organic clicks ~40% where they appear (Agarwal &amp; Sen field RCT, 2026), and pages cited in the AI Overview earn ~35% higher CTR (Seer, 2025) - so this score is your odds of being the cited page. Rubric <b>estimates citability</b> from on-page, structural and technical signals. It does <b>not</b> measure citations. For measured citations, calibrate the model against your Bing Webmaster Tools AI Performance export (<code>--calibrate citations.csv</code>). Every check carries a source (engine documentation, first-party citation data, or a CITED chapter). llms.txt and Grok are shown for reference only and are not scored (Ch5): llms.txt shows no citation correlation, and Grok has no citation export to calibrate against.</div></div>")
       + "<div id='printroot'></div>" + _anon_modal + _sticky
       + f"<script>window.__DATA__={payload};window.__LOGO_DARK__={json.dumps(CITED_LOGO_DARK)};</script><script>{js}</script></body></html>")
    with open(path,"w",encoding="utf-8") as f: f.write(doc)

def write_benchmark_html(rows, path, nav=None):
    """Self-contained 'you vs up to 4' comparison page (rows[0] is the user's own site). Same dark
    theme and embedded fonts as the main report, no external requests, so it serves standalone from
    Storage. rows come from benchmark(): {domain,overall,pillars{Known/Findable/Trusted},engines{6},pages,error}."""
    import html as _H
    try: from cited_fonts import FONT_FACE_CSS as _FF
    except Exception: _FF=""
    try: from cited_logo_data import CITED_LOGO_DATAURI as _LOGO
    except Exception: _LOGO=""
    def _col(s): s=s or 0; return "var(--grn)" if s>=75 else "var(--amber)" if s>=50 else "var(--red)"
    PILL=["Known","Findable","Trusted"]; ENG=["ChatGPT","Perplexity","AI Overviews","Gemini","Copilot","Claude"]
    you=rows[0] if rows else {"domain":"your site","overall":None}
    you_dom=you.get("domain") or "your site"
    ranked=sorted([r for r in rows if r.get("overall") is not None], key=lambda r:-r["overall"])
    rankmap={id(r):i+1 for i,r in enumerate(ranked)}
    you_rank=rankmap.get(id(you))
    if you_rank==1 and len(ranked)>1:
        head="You lead this set - the most citable site of the "+str(len(ranked))+" scored."
    elif you_rank:
        leader=ranked[0]; gap=(leader.get("overall") or 0)-(you.get("overall") or 0)
        head=("You rank #%d of %d. The gap to the leader (%s) is %d point%s - that is how much more citable the top site looks to AI engines right now."
              % (you_rank, len(ranked), _H.escape(leader.get("domain","")), gap, "" if gap==1 else "s"))
    else:
        head="Your site could not be reached, so it is unscored in this comparison."
    disp=sorted(rows, key=lambda r:(r.get("overall") is None, -(r.get("overall") or 0)))
    def _mainrow(r):
        you_c=" you" if r is you else ""; ov=r.get("overall")
        rk=rankmap.get(id(r)); rk="" if rk is None else str(rk)
        dom=_H.escape(r.get("domain","")) + (" <span class=tag>you</span>" if r is you else "")
        if ov is None:
            return ("<tr class='row%s'><td class=rk>%s</td><td class=dom>%s</td>"
                    "<td class=sc><span style='color:var(--muted)'>-</span></td>"
                    "<td colspan=3 class=miss>couldn't reach this site</td><td class=pg>-</td></tr>"
                    % (you_c, rk, dom))
        pills="".join("<td>%s</td>" % ((r.get("pillars") or {}).get(p,"-")) for p in PILL)
        return ("<tr class='row%s'><td class=rk>%s</td><td class=dom>%s</td>"
                "<td class=sc><span style='color:%s;font-weight:700'>%d</span></td>%s<td class=pg>%s</td></tr>"
                % (you_c, rk, dom, _col(ov), ov, pills, r.get("pages","-")))
    def _engrow(r):
        you_c=" you" if r is you else ""; ov=r.get("overall")
        dom=_H.escape(r.get("domain","")) + (" <span class=tag>you</span>" if r is you else "")
        if ov is None:
            return "<tr class='row%s'><td class=dom>%s</td><td colspan=%d class=miss>-</td></tr>" % (you_c, dom, len(ENG))
        cells="".join("<td class=eng style='color:%s'>%s</td>" % (_col((r.get("engines") or {}).get(e)), (r.get("engines") or {}).get(e,"-")) for e in ENG)
        return "<tr class='row%s'><td class=dom>%s</td>%s</tr>" % (you_c, dom, cells)
    nav=nav or {}
    navbtns=""
    if nav.get("rerun"): navbtns+="<a class=navbtn href=\"%s\">Re-run</a>" % _H.escape(nav["rerun"])
    if nav.get("new_crawl"): navbtns+="<a class=navbtn href=\"%s\">New crawl</a>" % _H.escape(nav["new_crawl"])
    if nav.get("logout"): navbtns+="<a class=navbtn href=\"%s\">Log out</a>" % _H.escape(nav["logout"])
    logo=("<img src=\"%s\" alt='Rubric'>" % _LOGO) if _LOGO else "<b style='font-family:var(--mono);letter-spacing:.1em'>CITEDSCORE</b>"
    css=(_FF+"\n:root{--bg:#14140f;--panel:#191914;--line:#2a2a24;--muted:#8b8b81;--txt:#f2f0e4;--grn:#f2f0e4;--amber:#ff4d6d;--red:#db0632;"
         "--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--sans:'Archivo',-apple-system,Segoe UI,Arial,sans-serif}"
         "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}"
         "a{color:var(--txt);text-decoration:none}a:hover{color:var(--grn)}"
         "header{display:flex;align-items:center;gap:12px;padding:16px 26px;border-bottom:1px solid var(--line)}header img{height:26px;max-width:210px}"
         "header .nav{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}"
         ".navbtn{font-family:var(--mono);font-size:11px;letter-spacing:.05em;color:var(--muted);border:1px solid var(--line);border-radius:100px;padding:6px 12px}"
         ".navbtn:hover{color:var(--grn);border-color:var(--grn)}"
         ".wrap{max-width:1000px;margin:0 auto;padding:30px 26px 80px}"
         ".kick{font-family:var(--mono);font-size:11px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}"
         "h1{font-weight:400;font-size:clamp(26px,4vw,40px);letter-spacing:-.02em;margin:8px 0 8px}"
         ".head{font-size:15.5px;margin:18px 0 20px;padding:15px 18px;border:1px solid var(--line);border-radius:0;background:var(--panel)}"
         "h3{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin:34px 0 4px}"
         ".tblwrap{overflow-x:auto}table{width:100%;border-collapse:collapse;margin-top:8px;min-width:560px}"
         "th,td{text-align:center;padding:12px 10px;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}"
         "th{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--muted);text-transform:uppercase;font-weight:500}"
         "th.dom,td.dom{text-align:left}td.rk{font-family:var(--mono);color:var(--muted);width:34px}"
         "td.dom{font-size:14.5px}td.sc{font-family:var(--mono);font-size:22px}td.pg{font-family:var(--mono);font-size:12px;color:var(--muted)}"
         "td.eng{font-family:var(--mono);font-size:14px}td.miss{color:var(--muted);font-size:12px;font-family:var(--mono)}"
         ".tag{font-family:var(--mono);font-size:9px;letter-spacing:.08em;color:#fff;background:var(--grn);border-radius:100px;padding:2px 6px}"
         "tr.you td{background:rgba(242,240,228,.06)}tr.you td.dom{box-shadow:inset 3px 0 0 var(--grn)}"
         ".note{color:var(--muted);font-size:12px;margin-top:16px;max-width:70ch}")
    doc=("<!doctype html><html lang=en><head><meta charset=utf-8>"
         "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
         "<title>Competitor benchmark - "+_H.escape(you_dom)+"</title><style>"+css+"</style></head><body>"
         "<header>"+logo+"<span class=nav>"+navbtns+"</span></header>"
         "<div class=wrap><div class=kick>Competitor benchmark</div>"
         "<h1>"+_H.escape(you_dom)+" vs the field</h1>"
         "<div class=head>"+head+"</div>"
         "<h3>Rubric &amp; pillars</h3><div class=tblwrap><table><thead><tr>"
         "<th class=rk>#</th><th class=dom>Site</th><th>Rubric</th><th>Known</th><th>Findable</th><th>Trusted</th><th>Pages</th>"
         "</tr></thead><tbody>"+"".join(_mainrow(r) for r in disp)+"</tbody></table></div>"
         "<h3>AI-engine readiness</h3><div class=tblwrap><table><thead><tr>"
         "<th class=dom>Site</th>"+"".join("<th>"+e+"</th>" for e in ENG)+"</tr></thead><tbody>"
         +"".join(_engrow(r) for r in disp)+"</tbody></table></div>"
         "<div class=note>Each site is crawled to a capped sample of pages for a fast comparison, so these scores are directional. "
         "Known / Findable / Trusted are the three questions an engine asks: do I know you exist, can I find your answer, do I trust you enough to name you. "
         "Run a full single-site audit for the complete page-by-page report and action plan.</div></div></body></html>")
    with open(path,"w",encoding="utf-8") as f: f.write(doc)

def write_draft_html(res, path, nav=None, query=None):
    """Render a check_draft() result as a standalone pre-publish citability report (Wave 2). res is the
    dict from check_draft: {verdict, passing_checks, bad, fix_count, fixes[{check,status,detail,why}], ...}."""
    import html as _H
    try: from cited_fonts import FONT_FACE_CSS as _FF
    except Exception: _FF=""
    try: from cited_logo_data import CITED_LOGO_DATAURI as _LOGO
    except Exception: _LOGO=""
    verdict=(res.get("verdict") or "").strip()
    vl=verdict.lower()
    vcol="var(--grn)" if "ready" in vl else "var(--red)" if "weak" in vl else "var(--amber)"
    passing=res.get("passing_checks",0) or 0; bad=res.get("bad",0) or 0
    fixes=res.get("fixes",[]) or []; words=res.get("words")
    STB={"bad":("var(--red)","must fix"),"warn":("var(--amber)","improve")}
    def _fx(f):
        col,lbl=STB.get(f.get("status"),("var(--muted)",f.get("status","")))
        d="<div class=fxd>"+_H.escape(f.get("detail",""))+"</div>" if f.get("detail") else ""
        w="<div class=fxw>"+_H.escape(f.get("why",""))+"</div>" if f.get("why") else ""
        return ("<div class=fx><div class=fxh><span class=fxs style='color:%s'>%s</span>"
                "<span class=fxc>%s</span></div>%s%s</div>" % (col,lbl,_H.escape(f.get("check","")),d,w))
    fixrows="".join(_fx(f) for f in fixes) or "<div class=allclear>Every draft-body check passes. This reads as citable-ready.</div>"
    nav=nav or {}; navbtns=""
    if nav.get("new_crawl"): navbtns+="<a class=navbtn href=\"%s\">New check</a>" % _H.escape(nav["new_crawl"])
    if nav.get("logout"): navbtns+="<a class=navbtn href=\"%s\">Log out</a>" % _H.escape(nav["logout"])
    logo=("<img src=\"%s\" alt='Rubric'>" % _LOGO) if _LOGO else "<b style='font-family:var(--mono);letter-spacing:.1em'>CITEDSCORE</b>"
    qline=("<div class=q>Target query: <b>"+_H.escape(query)+"</b></div>") if query else ""
    css=(_FF+"\n:root{--bg:#14140f;--panel:#191914;--panel2:#111;--line:#2a2a24;--muted:#8b8b81;--txt:#f2f0e4;--grn:#f2f0e4;--amber:#ff4d6d;--red:#db0632;"
         "--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--sans:'Archivo',-apple-system,Segoe UI,Arial,sans-serif}"
         "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;line-height:1.55}"
         "a{color:var(--txt);text-decoration:none}a:hover{color:var(--grn)}"
         "header{display:flex;align-items:center;gap:12px;padding:16px 26px;border-bottom:1px solid var(--line)}header img{height:26px;max-width:210px}"
         "header .nav{margin-left:auto;display:flex;gap:8px}.navbtn{font-family:var(--mono);font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:100px;padding:6px 12px}.navbtn:hover{color:var(--grn);border-color:var(--grn)}"
         ".wrap{max-width:820px;margin:0 auto;padding:30px 26px 80px}.kick{font-family:var(--mono);font-size:11px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}"
         ".verdict{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin:10px 0 4px}.verdict h1{font-weight:400;font-size:clamp(24px,4vw,34px);letter-spacing:-.02em;margin:0;color:var(--vc)}"
         ".stat{font-family:var(--mono);font-size:12px;color:var(--muted)}.stat b{color:var(--txt)}"
         ".q{font-size:13px;color:var(--muted);margin:8px 0 22px}.q b{color:var(--txt)}"
         "h3{font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin:26px 0 6px}"
         ".fx{border:1px solid var(--line);border-radius:0;padding:14px 16px;margin-bottom:10px;background:var(--panel)}"
         ".fxh{display:flex;align-items:center;gap:10px;margin-bottom:6px}.fxs{font-family:var(--mono);font-size:10px;letter-spacing:.06em;text-transform:uppercase;border:1px solid currentColor;border-radius:100px;padding:2px 8px}"
         ".fxc{font-size:14.5px;color:var(--txt)}.fxd{font-size:13px;color:var(--txt);line-height:1.5}.fxw{font-size:12.5px;color:var(--muted);line-height:1.5;margin-top:4px}"
         ".allclear{border:1px solid var(--line);border-left:2px solid var(--grn);border-radius:0;padding:16px;color:var(--grn);background:var(--panel)}"
         ".note{color:var(--muted);font-size:13px;margin-top:20px;max-width:70ch}")
    css=css.replace("--vc",vcol)   # verdict colour token for h1
    doc=("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
         "<title>Draft pre-flight - Rubric</title><style>"+css+"</style></head><body>"
         "<header>"+logo+"<span class=nav>"+navbtns+"</span></header>"
         "<div class=wrap><div class=kick>Draft pre-flight</div>"
         "<div class=verdict><h1>"+_H.escape(verdict or "checked")+"</h1></div>"
         "<div class=stat><b>"+str(passing)+"</b> checks passing &nbsp;·&nbsp; <b>"+str(len(fixes))+"</b> to address"
         +((" &nbsp;·&nbsp; <b>"+str(words)+"</b> words") if words else "")+"</div>"
         +qline+
         "<h3>Fix before publishing</h3>"+fixrows+
         "<div class=note>This lints the draft body's extractability - answer-first structure, question-shaped headings, self-contained sections, stats, sources, internal links. "
         "Publish-wrapper checks (title, meta, schema, date, author, canonical) are set by your CMS at publish time and are excluded here. "
         "Fix these, publish, then run a full site audit to score the live page.</div></div></body></html>")
    with open(path,"w",encoding="utf-8") as f: f.write(doc)

def render_loop_report(findings, path, nav=None):
    """Feedback bridge: render the AI-citation fix-loop findings Claude assembled (query, who's cited, share
    of voice, source-of-truth split, the you-vs-winner gap, the fix) into a standalone Rubric-branded
    report. Every section is OPTIONAL - renders whatever `findings` contains. The 'Claude feeds Rubric'
    half of the symbiosis; capture stays in the browser, no engine API."""
    import html as _H
    try: from cited_fonts import FONT_FACE_CSS as _FF
    except Exception: _FF=""
    try: from cited_logo_data import CITED_LOGO_DATAURI as _LOGO
    except Exception: _LOGO=""
    f = findings or {}
    esc = lambda x: _H.escape(str(x if x is not None else ""))
    query = f.get("query") or "AI answer"
    yd = re.sub(r"^www\.", "", re.sub(r"^https?://", "", (f.get("your_domain") or "").lower())).rstrip("/")
    named, cited = f.get("named"), f.get("cited")
    verdict = f.get("verdict")
    if not verdict:
        verdict = ("Cited and recommended" if (named and cited) else "Cited as a source, not named as the pick"
                   if cited else "Named but not cited as a source" if named else "Not cited for this query")
    vl = verdict.lower()
    vcol = ("var(--grn)" if ("recommend" in vl or ("cited" in vl and "not cited" not in vl))
            else "var(--red)" if "not cited" in vl else "var(--amber)")
    sec = []
    cs = f.get("cited_sources") or []
    if cs:
        rows = ""
        for u in cs[:20]:
            mine = bool(yd and yd in str(u).lower())
            rows += "<li%s>%s%s</li>" % ((" class=mine" if mine else ""), esc(u), (" <span class=tag>you</span>" if mine else ""))
        sec.append("<h3>Who the engine cited</h3><ul class=cslist>" + rows + "</ul>")
    sov = f.get("subquery_sov") or {}
    if sov:
        board = "".join("<tr><td class=dom>%s</td><td>%s</td><td>%s%%</td></tr>" % (esc(b.get("domain")), esc(b.get("cited_on")), esc(b.get("coverage_pct"))) for b in (sov.get("competitor_leaderboard") or [])[:8])
        miss = "".join("<li>%s</li>" % esc(m) for m in (sov.get("missing_subqueries") or [])[:12])
        sec.append("<h3>Share of the fan-out</h3><div class=stat><b>%s%%</b> of sub-queries cite you &nbsp;&middot;&nbsp; <b>%s%%</b> share of voice vs competitors</div>%s%s"
                   % (esc(sov.get("your_coverage_pct")), esc(sov.get("share_of_voice_pct")),
                      ("<table class=t><thead><tr><th class=dom>Competitor</th><th>Cited on</th><th>Coverage</th></tr></thead><tbody>" + board + "</tbody></table>" if board else ""),
                      ("<div class=sub>Sub-queries you're missing:</div><ul class=miss>" + miss + "</ul>" if miss else "")))
    sot = f.get("source_of_truth") or {}
    if sot:
        tp = "".join("<tr><td class=dom>%s</td><td>%s</td></tr>" % (esc(t.get("source")), esc(t.get("citations"))) for t in (sot.get("third_party") or [])[:10])
        sec.append("<h3>Where the trust sits</h3><div class=stat><b>%s%%</b> of citations are your own site</div>%s%s"
                   % (esc(sot.get("own_site_pct")),
                      ("<table class=t><thead><tr><th class=dom>Third-party source</th><th>Citations</th></tr></thead><tbody>" + tp + "</tbody></table>" if tp else ""),
                      ("<div class=rec>" + esc(sot.get("recommendation")) + "</div>" if sot.get("recommendation") else "")))
    gap = f.get("gap") or {}
    if gap:
        you = gap.get("you") or {}; win = gap.get("winner") or {}; grows = ""
        for g in (gap.get("gaps") or [])[:12]:
            engs = ", ".join((g.get("engines") or [])[:3])
            grows += ("<div class=g><div class=gh><span class=gw>+%s wt</span><span class=gl>%s</span></div><div class=gf>%s</div>%s</div>"
                      % (esc(g.get("weight")), esc(g.get("label")), esc(g.get("fix_deep") or g.get("fix")),
                         ("<div class=ge>" + esc(engs) + "</div>" if engs else "")))
        sec.append("<h3>Why you lost: you vs the cited winner</h3><div class=stat>You <b>%s</b> &nbsp;vs&nbsp; %s <b>%s</b> &nbsp;&middot;&nbsp; gap <b>%s</b></div>%s"
                   % (esc(you.get("score")), esc(win.get("domain") or "the cited page"), esc(win.get("score")), esc(gap.get("score_gap")), grows))
    fix = f.get("fix")
    if fix:
        body = ("<ol class=fixlist>" + "".join("<li>%s</li>" % esc(x) for x in fix) + "</ol>") if isinstance(fix, (list, tuple)) else ("<div class=fixbody>" + esc(fix) + "</div>")
        sec.append("<h3>The fix</h3>" + body)
    nav = nav or {}; navbtns = ""
    if nav.get("new_crawl"): navbtns += "<a class=navbtn href=\"%s\">New</a>" % esc(nav["new_crawl"])
    if nav.get("logout"): navbtns += "<a class=navbtn href=\"%s\">Log out</a>" % esc(nav["logout"])
    logo = ("<img src=\"%s\" alt='Rubric'>" % _LOGO) if _LOGO else "<b style='font-family:var(--mono);letter-spacing:.1em'>CITEDSCORE</b>"
    css = (_FF + "\n:root{--bg:#14140f;--panel:#191914;--panel2:#111;--line:#2a2a24;--muted:#8b8b81;--dim:#6b6b65;--txt:#f2f0e4;--grn:#f2f0e4;--amber:#ff4d6d;--red:#db0632;"
           "--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--sans:'Archivo',-apple-system,Segoe UI,Arial,sans-serif}"
           "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;line-height:1.55}"
           "a{color:var(--txt);text-decoration:none}a:hover{color:var(--grn)}"
           "header{display:flex;align-items:center;gap:12px;padding:16px 26px;border-bottom:1px solid var(--line)}header img{height:26px;max-width:210px}"
           "header .nav{margin-left:auto;display:flex;gap:8px}.navbtn{font-family:var(--mono);font-size:12.5px;color:var(--muted);border:1px solid var(--line);border-radius:100px;padding:6px 12px}.navbtn:hover{color:var(--grn);border-color:var(--grn)}"
           ".wrap{max-width:820px;margin:0 auto;padding:30px 26px 80px}.kick{font-family:var(--mono);font-size:12.5px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}"
           "h1{font-weight:400;font-size:clamp(22px,3.5vw,30px);letter-spacing:-.02em;margin:8px 0 6px}"
           ".verdict{font-size:17px;font-weight:600;color:var(--vc);margin:0 0 4px}.dom{color:var(--muted);font-size:14px}"
           "h3{font-family:var(--mono);font-size:12.5px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin:30px 0 8px}"
           ".stat{font-family:var(--mono);font-size:13.5px;color:var(--muted);margin-bottom:8px}.stat b{color:var(--txt)}"
           "ul.cslist{list-style:none;padding:0;margin:0}ul.cslist li{padding:8px 0;border-bottom:1px solid var(--line);font-size:14px;word-break:break-all}ul.cslist li.mine{color:var(--grn)}"
           ".tag{font-family:var(--mono);font-size:11px;letter-spacing:.08em;color:#fff;background:var(--grn);border-radius:100px;padding:2px 6px}"
           "table.t{width:100%;border-collapse:collapse;margin:6px 0}table.t th,table.t td{text-align:right;padding:8px 10px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:13.5px}table.t th{color:var(--muted);font-weight:400;font-size:12px;letter-spacing:.06em;text-transform:uppercase}table.t .dom{text-align:left;font-family:var(--sans);font-size:14px}"
           "ul.miss{margin:6px 0;padding-left:18px;color:var(--txt);font-size:14px}ul.miss li{padding:2px 0}.sub{font-size:13px;color:var(--muted);margin-top:8px}"
           ".rec{border-left:2px solid var(--grn);background:var(--panel);border-radius:0 8px 8px 0;padding:11px 14px;font-size:14px;color:var(--txt);margin-top:8px}"
           ".g{border:1px solid var(--line);border-radius:0;padding:12px 14px;margin-bottom:9px;background:var(--panel)}.gh{display:flex;align-items:baseline;gap:10px;margin-bottom:5px}.gw{font-family:var(--mono);font-size:12px;color:var(--grn);flex:none}.gl{font-size:14px;color:var(--txt);font-weight:600}.gf{font-size:13.5px;color:var(--muted);line-height:1.5}.ge{font-family:var(--mono);font-size:12px;color:var(--dim);margin-top:4px}"
           ".fixbody{border:1px solid var(--line);border-left:2px solid var(--grn);border-radius:0 8px 8px 0;padding:14px 16px;background:var(--panel);font-size:14px;line-height:1.6;white-space:pre-wrap}ol.fixlist{padding-left:20px;font-size:14px;line-height:1.6}ol.fixlist li{padding:4px 0}"
           ".note{color:var(--muted);font-size:13px;margin-top:26px;max-width:72ch}")
    css = css.replace("--vc", vcol)
    doc = ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
           "<title>AI citation fix loop - " + esc(query) + "</title><style>" + css + "</style></head><body>"
           "<header>" + logo + "<span class=nav>" + navbtns + "</span></header>"
           "<div class=wrap><div class=kick>AI Citation Fix Loop</div><h1>" + esc(query) + "</h1>"
           "<div class=verdict>" + esc(verdict) + "</div>" + ("<div class=dom>" + esc(yd) + "</div>" if yd else "")
           + "".join(sec) +
           "<div class=note>Findings captured from the real AI answer in the browser (no engine API). AI answers are non-deterministic, so treat single-capture findings as directional and re-run a money query a few times for anything asserted as stable. The you-vs-winner gap is on-page only; off-page authority is not measured here.</div>"
           "</div></body></html>")
    with open(path, "w", encoding="utf-8") as ff:
        ff.write(doc)

def page_compare(your_url, competitor_url, progress=None):
    """Web feature (no capture / no MCP): crawl your page + a competitor page once each and return BOTH
    views - the signals they have that you lack (close these to catch up), and where they're weak so you can
    leapfrog. Composes cited_gap + competitor_teardown over one pair of single-page crawls."""
    def _one(u):
        d = run_audit(u, out=None, max_pages=1, links=False, progress=progress); pg = (d.get("pages") or [{}])[0]
        return d, {c.get("id"): c.get("status") for c in (pg.get("checks") or []) if c.get("id")}, pg.get("score")
    dy, sy, ys = _one(your_url); dc, sc, cscore = _one(competitor_url)
    wsum = lambda cid: sum(ENGINE_WEIGHTS[e].get(cid, 0) for e in ENGINE_WEIGHTS)
    engs = lambda cid: [e for e in ENGINE_WEIGHTS if ENGINE_WEIGHTS[e].get(cid, 0) > 0]
    def _row(cid, yourst, theirst):
        m = CHECK_META.get(cid, {})
        return {"check": cid, "label": m.get("label", cid), "pillar": m.get("pillar"), "weight": wsum(cid),
                "your_status": yourst, "their_status": theirst, "engines": engs(cid),
                "fix": FIX_DEEP.get(cid) or FIX.get(cid, "")}
    your_gaps, their_weak = [], []
    for cid in set(list(sy) + list(sc)):
        if CHECK_META.get(cid, {}).get("phase") == 0: continue
        yourst, theirst = sy.get(cid), sc.get(cid)
        if theirst == "good" and yourst in ("bad", "warn"):
            your_gaps.append(_row(cid, yourst, theirst))
        if theirst in ("bad", "warn"):
            r = _row(cid, yourst, theirst); r["you_win"] = (yourst == "good"); their_weak.append(r)
    your_gaps.sort(key=lambda g: -g["weight"]); their_weak.sort(key=lambda g: -g["weight"])
    return {"you": {"url": your_url, "domain": dy.get("domain"), "score": ys},
            "competitor": {"url": competitor_url, "domain": dc.get("domain"), "score": cscore},
            "score_gap": (cscore or 0) - (ys or 0), "your_gaps": your_gaps, "their_weak": their_weak}

def write_compare_html(data, path, nav=None):
    """Render page_compare() as a standalone Rubric-branded 'you vs a competitor page' report."""
    import html as _H
    try: from cited_fonts import FONT_FACE_CSS as _FF
    except Exception: _FF=""
    try: from cited_logo_data import CITED_LOGO_DATAURI as _LOGO
    except Exception: _LOGO=""
    esc = lambda x: _H.escape(str(x if x is not None else ""))
    you = data.get("you") or {}; comp = data.get("competitor") or {}
    def _card(g, show_win=False):
        eng = ", ".join((g.get("engines") or [])[:3])
        win = " <span class=wintag>you already win</span>" if (show_win and g.get("you_win")) else ""
        st = "" if not g.get("your_status") else "<span class=yst>you: %s</span>" % esc(g.get("your_status"))
        return ("<div class=g><div class=gh><span class=gw>+%s wt</span><span class=gl>%s</span>%s%s</div>"
                "<div class=gf>%s</div>%s</div>"
                % (esc(g.get("weight")), esc(g.get("label")), win, st, esc(g.get("fix")),
                   ("<div class=ge>" + esc(eng) + "</div>" if eng else "")))
    gaps = "".join(_card(g) for g in (data.get("your_gaps") or [])[:15]) or "<div class=allclear>You match or beat their page on every signal we measure.</div>"
    weak = data.get("their_weak") or []
    wins = [w for w in weak if w.get("you_win")]; open_both = [w for w in weak if not w.get("you_win")]
    weakhtml = ""
    if wins: weakhtml += "<div class=sub>You already beat them here - press it:</div>" + "".join(_card(w, True) for w in wins[:10])
    if open_both: weakhtml += "<div class=sub>Open for both - fix on your page to leapfrog:</div>" + "".join(_card(w) for w in open_both[:10])
    if not weak: weakhtml = "<div class=allclear>Their page is strong across the board.</div>"
    nav = nav or {}; navbtns = ""
    if nav.get("new_crawl"): navbtns += "<a class=navbtn href=\"%s\">New</a>" % esc(nav["new_crawl"])
    if nav.get("logout"): navbtns += "<a class=navbtn href=\"%s\">Log out</a>" % esc(nav["logout"])
    logo = ("<img src=\"%s\" alt='Rubric'>" % _LOGO) if _LOGO else "<b style='font-family:var(--mono);letter-spacing:.1em'>CITEDSCORE</b>"
    css = (_FF + "\n:root{--bg:#14140f;--panel:#191914;--line:#2a2a24;--muted:#8b8b81;--dim:#6b6b65;--txt:#f2f0e4;--grn:#f2f0e4;--amber:#ff4d6d;--red:#db0632;"
           "--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--sans:'Archivo',-apple-system,Segoe UI,Arial,sans-serif}"
           "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;line-height:1.55}"
           "a{color:var(--txt);text-decoration:none}a:hover{color:var(--grn)}"
           "header{display:flex;align-items:center;gap:12px;padding:16px 26px;border-bottom:1px solid var(--line)}header img{height:26px;max-width:210px}"
           "header .nav{margin-left:auto;display:flex;gap:8px}.navbtn{font-family:var(--mono);font-size:12.5px;color:var(--muted);border:1px solid var(--line);border-radius:100px;padding:6px 12px}.navbtn:hover{color:var(--grn);border-color:var(--grn)}"
           ".wrap{max-width:820px;margin:0 auto;padding:30px 26px 80px}.kick{font-family:var(--mono);font-size:12.5px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}"
           "h1{font-weight:400;font-size:clamp(22px,3.5vw,30px);letter-spacing:-.02em;margin:8px 0 6px}"
           ".stat{font-family:var(--mono);font-size:14px;color:var(--muted);margin:2px 0 6px}.stat b{color:var(--txt)}"
           "h3{font-family:var(--mono);font-size:12.5px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin:30px 0 8px}"
           ".sub{font-size:13px;color:var(--muted);margin:14px 0 8px}"
           ".g{border:1px solid var(--line);border-radius:0;padding:12px 14px;margin-bottom:9px;background:var(--panel)}.gh{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap;margin-bottom:5px}.gw{font-family:var(--mono);font-size:12px;color:var(--grn);flex:none}.gl{font-size:14px;color:var(--txt);font-weight:600}.gf{font-size:13.5px;color:var(--muted);line-height:1.5}.ge{font-family:var(--mono);font-size:12px;color:var(--dim);margin-top:4px}"
           ".yst{font-family:var(--mono);font-size:12px;color:var(--amber)}.wintag{font-family:var(--mono);font-size:11px;letter-spacing:.06em;color:#fff;background:var(--grn);border-radius:100px;padding:2px 7px}"
           ".allclear{border:1px solid var(--line);border-left:2px solid var(--grn);border-radius:0 8px 8px 0;padding:14px;color:var(--grn);background:var(--panel)}"
           ".note{color:var(--muted);font-size:13px;margin-top:24px;max-width:72ch}")
    doc = ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
           "<title>Page vs competitor - Rubric</title><style>" + css + "</style></head><body>"
           "<header>" + logo + "<span class=nav>" + navbtns + "</span></header>"
           "<div class=wrap><div class=kick>Page vs competitor</div>"
           "<h1>" + esc(you.get("domain") or "your page") + " vs " + esc(comp.get("domain") or "competitor") + "</h1>"
           "<div class=stat>You <b>" + esc(you.get("score")) + "</b> &nbsp;vs&nbsp; them <b>" + esc(comp.get("score")) + "</b> &nbsp;&middot;&nbsp; gap <b>" + esc(data.get("score_gap")) + "</b></div>"
           "<h3>What they have that you don't</h3>" + gaps +
           "<h3>Where they're weak</h3>" + weakhtml +
           "<div class=note>Single-page crawl of each URL. On-page citability signals only; off-page authority (domain strength, third-party mentions) is not measured here. Weighting is the sum of engine weights for each signal.</div>"
           "</div></body></html>")
    with open(path, "w", encoding="utf-8") as ff:
        ff.write(doc)

def write_value_html(vb, path, nav=None):
    """Render value_bridge() as a standalone Rubric-branded 'clicks at risk' report."""
    import html as _H
    try: from cited_fonts import FONT_FACE_CSS as _FF
    except Exception: _FF=""
    try: from cited_logo_data import CITED_LOGO_DATAURI as _LOGO
    except Exception: _LOGO=""
    esc = lambda x: _H.escape(str(x if x is not None else ""))
    lo, hi = vb.get("clicks_at_risk_low", 0), vb.get("clicks_at_risk_high", 0)
    vlo, vhi = vb.get("value_at_risk_low"), vb.get("value_at_risk_high")
    money = (" &nbsp;&middot;&nbsp; <b>%s to %s</b> in value" % (esc(vlo), esc(vhi))) if vlo is not None else ""
    rows = "".join("<tr><td class=u>%s</td><td>%s</td><td>%s</td><td class=r>%s</td></tr>"
                   % (esc(p.get("url")), esc(p.get("score")), esc(p.get("clicks")), esc(p.get("clicks_at_risk")))
                   for p in (vb.get("top_at_risk_pages") or [])[:20])
    if not rows:
        rows = "<tr><td colspan=4 class=miss>No matched pages scored below 75 - your traffic pages read as citable.</td></tr>"
    nav = nav or {}; navbtns = ""
    if nav.get("new_crawl"): navbtns += "<a class=navbtn href=\"%s\">New</a>" % esc(nav["new_crawl"])
    if nav.get("logout"): navbtns += "<a class=navbtn href=\"%s\">Log out</a>" % esc(nav["logout"])
    logo = ("<img src=\"%s\" alt='Rubric'>" % _LOGO) if _LOGO else "<b style='font-family:var(--mono);letter-spacing:.1em'>CITEDSCORE</b>"
    css = (_FF + "\n:root{--bg:#14140f;--panel:#191914;--line:#2a2a24;--muted:#8b8b81;--dim:#6b6b65;--txt:#f2f0e4;--grn:#f2f0e4;--amber:#ff4d6d;--red:#db0632;"
           "--mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;--sans:'Archivo',-apple-system,Segoe UI,Arial,sans-serif}"
           "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--sans);font-size:14px;line-height:1.55}"
           "a{color:var(--txt);text-decoration:none}a:hover{color:var(--grn)}"
           "header{display:flex;align-items:center;gap:12px;padding:16px 26px;border-bottom:1px solid var(--line)}header img{height:26px;max-width:210px}"
           "header .nav{margin-left:auto;display:flex;gap:8px}.navbtn{font-family:var(--mono);font-size:12.5px;color:var(--muted);border:1px solid var(--line);border-radius:100px;padding:6px 12px}.navbtn:hover{color:var(--grn);border-color:var(--grn)}"
           ".wrap{max-width:860px;margin:0 auto;padding:30px 26px 80px}.kick{font-family:var(--mono);font-size:12.5px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}"
           "h1{font-weight:400;font-size:clamp(24px,4vw,36px);letter-spacing:-.02em;margin:8px 0 6px}"
           ".big{font-size:15px;color:var(--txt);margin:6px 0 4px}.big b{color:var(--amber);font-variation-settings:'wght' 700}"
           ".stat{font-family:var(--mono);font-size:13px;color:var(--muted);margin-bottom:4px}.stat b{color:var(--txt)}"
           "h3{font-family:var(--mono);font-size:12.5px;letter-spacing:.1em;color:var(--muted);text-transform:uppercase;margin:28px 0 6px}"
           ".tblwrap{overflow-x:auto}table{width:100%;border-collapse:collapse;margin-top:6px;min-width:520px}"
           "th,td{text-align:right;padding:10px 10px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:13.5px;font-variant-numeric:tabular-nums}"
           "th{color:var(--muted);font-weight:400;font-size:12px;letter-spacing:.06em;text-transform:uppercase}"
           "td.u,th.u{text-align:left;font-family:var(--sans);word-break:break-all}td.r{color:var(--amber);font-weight:600}td.miss{text-align:left;color:var(--muted);font-family:var(--sans)}"
           ".note{color:var(--muted);font-size:13px;margin-top:22px;max-width:74ch}")
    doc = ("<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content=\"width=device-width,initial-scale=1\">"
           "<title>Clicks at risk - " + esc(vb.get("domain")) + "</title><style>" + css + "</style></head><body>"
           "<header>" + logo + "<span class=nav>" + navbtns + "</span></header>"
           "<div class=wrap><div class=kick>Clicks at risk from AI answers</div>"
           "<h1>" + esc(vb.get("domain") or "your site") + "</h1>"
           "<div class=big><b>" + esc(lo) + " to " + esc(hi) + "</b> clicks at risk" + money + "</div>"
           "<div class=stat>" + esc(vb.get("pages_matched")) + " pages matched &nbsp;&middot;&nbsp; " + esc(vb.get("total_clicks_in_data")) + " clicks in your data</div>"
           "<h3>Top at-risk pages (traffic + weak citability)</h3>"
           "<div class=tblwrap><table><thead><tr><th class=u>Page</th><th>Score</th><th>Clicks</th><th>At risk</th></tr></thead><tbody>"
           + rows + "</tbody></table></div>"
           "<div class=note>" + esc(vb.get("basis")) + " A ranged estimate, never attribution. Fix the low-scoring high-traffic pages first: that is where citability protects the most clicks.</div>"
           "</div></body></html>")
    with open(path, "w", encoding="utf-8") as ff:
        ff.write(doc)

# ---- deterministic competitor proposal (spec: "never show a blank where you could show a proposal") ----
# Priority order, all deterministic (no LLM, no API):
#   1. The site names them  -- /vs//alternatives//compare pages + the external domains linked in that context.
#      Highest confidence: they wrote it, and it is already in the HTML we fetched.
#   2. Citation corpus       -- NOT YET WIRED: no competitor-citation data is captured (offpage is sameAs presence only).
#   3. Nearest-neighbour     -- NOT YET WIRED: the benchmark is aggregate medians; there is no per-domain index to match.
#   4. Benchmark fallback    -- the 491-site medians (already shipped, rendered by the gap card). Never empty.
# Computed at crawl time because per-page outlinks are dropped before the report is stored. Crawls NOTHING here:
# the caller crawls the proposed competitor only when the user accepts it (guard: no pre-crawl, one worker).
_CMP_PATH = re.compile(r'(?:^|[/_-])(vs|versus|alternatives?|compare|comparison|competitors?)(?:[/_-]|$)', re.I)
_COMP_SKIP = ("google.", "facebook.", "fb.com", "twitter.", "x.com", "youtube.", "youtu.be", "linkedin.",
    "instagram.", "gstatic.", "googleapis.", "googletagmanager.", "doubleclick.", "cloudflare.", "gravatar.",
    "w3.org", "schema.org", "wordpress.", "apple.", "microsoft.", "bing.", "pinterest.", "tiktok.", "fonts.",
    "ampproject.", "jsdelivr.", "unpkg.", "gmpg.org", "paypal.", "stripe.", "cloudfront.", "wp.com", "github.",
    "play.google.", "apps.apple.", "t.me", "whatsapp.", "reddit.", "medium.com", "vimeo.", "g2.com", "capterra.",
    "trustpilot.", "getty", "shutterstock")

def _host_root(u):
    try: h = (urllib.parse.urlparse(u).hostname or "").lower()
    except Exception: return ""
    return h[4:] if h.startswith("www.") else h

def propose_competitor(pages, origin, site_type=None):
    """Deterministic competitor proposal from the crawl itself. Returns
       {"mode":"competitor","domain","url","source","label"} or {"mode":"benchmark"}.
       Never crawls -- the caller crawls only when the user accepts the proposal."""
    self_host = _host_root(origin)
    cmp_ext = {}                                            # external domain -> times linked from a comparison page
    outlinks_seen = 0                                       # readable external outlinks -- EVIDENCE the primary ran
    cmp_pages = 0                                           # comparison-context pages found
    for p in (pages or []):
        path = (p.get("url") or p.get("path") or "")
        is_cmp = bool(_CMP_PATH.search(path))
        if is_cmp: cmp_pages += 1
        for o in (p.get("outlinks") or (p.get("metrics") or {}).get("outlinks") or []):
            h = _host_root(o)
            if not h or "." not in h: continue
            outlinks_seen += 1                              # ANY absolute outlink read (incl. internal) = evidence the
                                                            # extractor saw the field. Counting external-only would
                                                            # false-alarm on internal-only sites, which links to itself.
            if self_host and (h == self_host or h.endswith("." + self_host)): continue
            if is_cmp and not any(s in h for s in _COMP_SKIP):
                cmp_ext[h] = cmp_ext.get(h, 0) + 1          # a rival named on a comparison page (infra/social filtered)
    # Self-verifying fallback: benchmark + outlinks_seen==0 on a real site is a BROKEN extractor, not a site without
    # rivals. Without this the two are indistinguishable, which is exactly what nearly mispriced the index decision.
    meta = {"outlinks_seen": outlinks_seen, "cmp_pages": cmp_pages}
    if cmp_ext:
        dom = max(cmp_ext, key=cmp_ext.get)
        return {"mode": "competitor", "domain": dom, "url": "https://" + dom + "/",
                "source": "comparison_page", "label": "you name them on your comparison page", **meta}
    # Sources 2/3 not yet wired; benchmark is never empty. source is stamped even here so competitor_proposed prices
    # the index off a week of live hit-rate (comparison_page vs benchmark now; corpus/neighbour later).
    return {"mode": "benchmark", "source": "benchmark", **meta}

# ---- deterministic query proposal (100% reach: every site has headings, titles and a detected type) ----
# Five queries the report pre-ticks on the fan-out panel, so the query box is never blank and the fan-out work has
# something to run on. Sources, deterministic: the site's own question headings > service/product titles > brand +
# category templates. Always includes at least one Entity and one Comparison query -- the two fan-out types that
# drove ~97% of brand mentions (Moz 2026). Nothing is queried here; the report analyses them when the user runs it.
def _qtype(q):
    ql = (q or "").lower()
    if any(w in ql for w in (" vs ", " vs.", "versus", "alternativ", "compare", "comparison", " or ", "best ", "top ")): return "Comparison"
    if ql.startswith(("what is", "what are", "who is", "who are", "is ")) or "review" in ql or "pricing" in ql or "cost" in ql: return "Entity"
    return "Informational"

def _brand_of(domain, pages):
    # Prefer the homepage <title>'s lead segment ("MyOwnConference: Webinars" -> "MyOwnConference"); fall back to the
    # registrable root. The user can edit it, so a slightly-off brand token is corrected, never a blank.
    for p in (pages or []):
        path = (p.get("path") or "")
        if (p.get("type") == "home") or (path.strip("/") == ""):
            t = (p.get("title") or "").strip()
            if t:
                brand = re.split(r"[|\-–—:·]", t)[0].strip()
                if 2 <= len(brand) <= 40: return brand
    root = (domain or "").replace("www.", "").split(".")[0]
    return root or "your brand"

def propose_queries(pages, domain, site_type=None, site_type_label=None):
    """Deterministic five-query proposal from the crawl. Returns [{"q","source","type"}], always with >=1 Entity
       and >=1 Comparison. 100% reach. Nothing is crawled or queried here."""
    brand = _brand_of(domain, pages)
    cat = (site_type_label or site_type or "").strip()
    out = []; seen = set()
    def add(q, source, typ):
        q = (q or "").strip(); k = q.lower()
        if q and k not in seen and len(out) < 5:
            seen.add(k); out.append({"q": q, "source": source, "type": typ})
    # 1) the site's own question headings -- highest signal, they wrote them. Prefer short, clean questions; cap 3.
    heads = []
    for p in (pages or []):
        heads += (p.get("q_headings") or [])
    for h in sorted(set(heads), key=lambda s: (len(s), s))[:3]:
        add(h, "heading", _qtype(h))
    # 2) brand + category templates. The Entity + Comparison anchors are added FIRST so they always make the 5
    #    (headings are capped at 3), guaranteeing coverage of the two fan-out types that matter.
    add("what is " + brand, "template", "Entity")
    add(brand + " alternatives", "template", "Comparison")
    if cat and cat.lower() not in ("general", "other", "unknown"): add("best " + cat, "template", "Comparison")
    add(brand + " reviews", "template", "Entity")
    add(brand + " pricing", "template", "Entity")
    add("how does " + brand + " work", "template", "Informational")   # extra fallbacks so a headingless, generic-type
    add(brand + " features", "template", "Entity")                     # site still reaches 5 (never fewer than promised)
    # Self-verifying: headings_seen==0 with an all-template result on a real content site flags a broken heading
    # capture, not a site with no questions. from_headings shows how much of the 5 came from the site vs templates.
    return {"queries": out[:5], "headings_seen": len(heads),
            "from_headings": sum(1 for q in out if q["source"] == "heading")}

def run_audit(*args, **kwargs):
    """Public crawl entry: scope the crawl credential to this call (reset is guaranteed even on an exception),
    then delegate to _run_audit_impl. auth (desktop only) = {"host","basic","cookie","headers"}, host-gated."""
    global _CRAWL_AUTH
    _CRAWL_AUTH = kwargs.get("auth")
    try:
        return _run_audit_impl(*args, **kwargs)
    finally:
        _CRAWL_AUTH = None

def _run_audit_impl(url, out="report", max_pages=0, workers=WORKERS, progress=None, client=None, intro=None, links=True, site_type=None, queries=None, logs=None, agency=None, logo=None, nav=None, max_seconds=0, benchmark_fn=None, debrand=False, auth=None, proof=None, proof_url=None):
    """Crawl + score a whole site and write out.html/.json/.csv. progress(phase, done,
    total, msg) is called through the run so a UI can show live status. Returns the data.
    max_seconds>0 caps wall-clock crawl time: at the deadline it stops gracefully and scores
    the pages already crawled (data['partial']=True), instead of failing. 0 = no cap (default)."""
    if not url.startswith("http"): url="https://"+url
    _logo_uri=None
    if logo:                                          # white-label: embed the agency/client logo as a data URI
        try:
            import base64, mimetypes
            mt=mimetypes.guess_type(logo)[0] or "image/png"
            with open(logo,"rb") as _lf: _logo_uri=f"data:{mt};base64,"+base64.b64encode(_lf.read()).decode()
        except Exception: _logo_uri=None
    p=urllib.parse.urlparse(url); domain=p.netloc.replace("www.",""); origin=f"{p.scheme}://{p.netloc}"
    _plat_early=_detect_platform(origin) if out else None   # detect ecommerce PLATFORM before the crawl throttles the site (a post-crawl re-fetch of a rate-limited store is unreliable); used only if the site types as ecommerce
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
    partial=False
    if max_seconds and max_seconds>0 and total>1:
        # Deadline-aware crawl: collect pages as they finish and stop at the wall, keeping what we have.
        _deadline=time.time()+max_seconds
        pages=[]; ex=ThreadPoolExecutor(max_workers=workers)
        futs=[ex.submit(work,u) for u in urls]
        try:
            for fut in as_completed(futs):
                try: pages.append(fut.result())
                except Exception: pass
                if time.time()>_deadline:
                    partial=True; break
        finally:
            for f in futs:
                if not f.done(): f.cancel()
            ex.shutdown(wait=False, cancel_futures=True)
    else:
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
    data["_debrand"]=bool(debrand)   # Pro de-brand: suppress Rubric marks in the report + exports (set from the job by the worker)
    data["scoring_version"]=SCORING_VERSION   # proof loop: which scoring revision produced this result (worker stamps it on crawl_page_checks)
    data["partial"]=bool(partial)
    data["total_discovered"]=total    # URLs discovered at crawl start; anon uses (total - crawled) for the "N more pages" bar
    data["pages_fetched"]=len(urls)   # pages the crawler actually fetched; pages_crawled is the SCORED subset (some are non-HTML / machine files)
    if partial:
        _mins=max(1,round(max_seconds/60))
        data["partial_note"]=(f"We audited the first {data.get('pages_crawled',len(pages))} of {total} discovered pages, "
                              f"then stopped at the {_mins}-minute crawl limit. Full-site coverage is coming.")
    data["commoncrawl"]=common_crawl_presence(domain)   # advisory: training-corpus presence (best-effort, never fatal)
    _cc=data["commoncrawl"]
    if _cc.get("ok") and _cc.get("paths") is not None:   # split the crawled pages into in / not-in Common Crawl
        _ccset=set(_cc.pop("paths")); _pin=[]; _pout=[]
        for _pg in data.get("pages",[]):
            try: _pp=urllib.parse.urlparse(_pg["url"]).path.rstrip("/").lower() or "/"
            except Exception: _pp=_pg.get("url","")
            (_pin if _pp in _ccset else _pout).append(_pg["url"])
        _cc["pages_in"]=_pin; _cc["pages_out"]=_pout
    if sum(1 for pg in pages if pg.get("status")==200)==0:       # nothing crawlable -> flag it clearly, do not present a 0 as a citability score
        data["crawl_failed"]=True
        data["crawl_note"]=((f"{domain} did not respond (timeout or network-level bot protection)" if unreachable
                             else f"0 of {len(pages)} crawled page(s) returned 200; the site firewall is likely rate-limiting or blocking the crawler")
                            +". This is a reachability problem, not a citability score. Try again shortly, lower the worker count, or the site may hard-block bots.")
    if out:
        if not unreachable:                                    # skip the extra probes on a site we could not reach
            try: data["internal_search"]=audit_internal_search(pages,origin)   # citation-to-landing (b): honest probe of the site's own search
            except Exception: data["internal_search"]={"detected":False}
            if data.get("site_type")=="ecommerce":             # AI-shopping feed advisory: on-page citability is necessary-not-sufficient for AI shopping
                try: data["commerce"]=commerce_readiness(_plat_early, data.get("agentready"))
                except Exception: pass
            # Money-page decision-completeness via a LOCAL ollama model (no key/cost). Bounded: top 5 commercial
            # pages only, and skipped entirely if ollama is not reachable, so it never blocks a normal crawl.
            try:
                if _ollama_available():
                    _dc_pages=[p for p in pages if p.get("_text")][:5]
                    _dc_rows=[]
                    for _dp in _dc_pages:
                        _fr=decision_facts(_dp.get("_text"))
                        if _fr: _dc_rows.append({"url":_dp.get("url") or _dp.get("path"),"facts":_fr,
                                                 "missing":[k for k in _DFACT_KEYS if not _fr.get(k)]})
                    if _dc_rows:
                        _agg={k:sum(1 for x in _dc_rows if x["facts"].get(k)) for k in _DFACT_KEYS}
                        data["decision_completeness"]={"rows":_dc_rows,"checked":len(_dc_rows),"agg":_agg,"model":"llama3 (local ollama)"}
            except Exception: pass
            if queries:                                        # citation-query coverage: which cited queries a page targets
                try: data["query_coverage"]=query_coverage(pages, load_queries(queries))
                except Exception: pass
        if logs:                                               # log analysis: real AI-bot behaviour from the server access log (independent of the crawl)
            try: data["log_analysis"]=analyze_logs(logs)
            except Exception: pass
        if nav: data["nav"]=nav                                # online chrome: {new_crawl, logout} URLs rendered in the report header
        if benchmark_fn:                                        # living segmented median: the worker records this crawl
            try: data["benchmark"]=benchmark_fn(data)          # in the corpus + returns the median for its site_type
            except Exception: pass                             # (None -> the report falls back to the static 491 median)
        for _p in pages: _p.pop("_text",None)                 # drop the transient page text (only needed for the ollama decision-facts call) before writing
        apply_diff(data,out)
        if proof: data["proof"]=proof                        # proof loop: the LAST-KNOWN site_proof (summary + tier) the caller loaded, computed before this
        if proof and proof_url: data["proof_url"]=proof_url  # crawl. write_html renders the compact panel only when present and links to the live Proof view.
        write_outputs(data,out)                               # out=None -> crawl + score only, no files (used by benchmark)
    emit("done",total,total,f"{domain}: {data['overall']}/100, {data['pages_crawled']} pages")
    return data

def benchmark(urls, max_pages=25, workers=WORKERS, progress=None):
    """Crawl each site (capped for speed) and return side-by-side Rubric / pillars / engines."""
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

def _build_auth(a):
    """Assemble the crawl auth from CLI flags. host is derived from --url so credentials are locked to it.
    Returns None when no auth flag is present. Exits with a clear message on malformed input. The credential
    values are never printed or logged; they live only in the request headers and the user's own --auth-file."""
    parts = {}
    if getattr(a, "auth_file", None):
        try:
            with open(a.auth_file, encoding="utf-8") as f: parts.update(json.load(f))
        except Exception as e:
            sys.exit(f"Could not read --auth-file: {str(e)[:120]}")
    if getattr(a, "basic", None):
        if ":" not in a.basic: sys.exit("--basic must be user:password (missing ':').")
        parts["basic"] = a.basic
    if getattr(a, "cookie", None): parts["cookie"] = a.cookie
    hdrs = parts.get("headers") or {}
    for i, item in enumerate(getattr(a, "auth_header", None) or []):
        if ":" not in item: sys.exit(f"--auth-header #{i+1} must be 'Name: value' (missing ':'); the value is not shown.")
        k, v = item.split(":", 1); hdrs[k.strip()] = v.strip()
    if hdrs: parts["headers"] = hdrs
    if not (parts.get("basic") or parts.get("cookie") or parts.get("headers")):
        return None
    parts["host"] = (urllib.parse.urlparse(a.url).hostname or "").lower()
    return parts

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
    ap.add_argument("--basic",help="HTTP Basic Auth user:password for a private/staging crawl (prefer --auth-file for secrets)")
    ap.add_argument("--cookie",help="Cookie header value for an authenticated crawl (e.g. a session cookie copied from your logged-in browser)")
    ap.add_argument("--auth-header",action="append",dest="auth_header",help="extra request header 'Name: value' for the crawl; repeatable")
    ap.add_argument("--auth-file",dest="auth_file",help="path to a local JSON file with {basic, cookie, headers}, kept off the command line")
    ap.add_argument("--queries",help="grounding-query CSV (Bing AI Performance 'AI Search Queries' export) for citation-query coverage")
    ap.add_argument("--monitor",help="server access log for the standing LOG MONITOR (per-day time-series of AI-bot activity)")
    ap.add_argument("--history",help="JSONL history file for --monitor to accumulate across uploads (persists per-day)")
    ap.add_argument("--check-draft",dest="check_draft",help="path to a draft HTML/text file: pre-publish citability check, prints JSON")
    ap.add_argument("--check-answer",dest="check_answer",help="an AI answer about your brand (a file path or inline text): fact-checks its claims against your own site, prints JSON. Use with --url for the domain.")
    a=ap.parse_args()
    if a.monitor:
        print(json.dumps(monitor_logs(a.monitor, a.history), indent=2, ensure_ascii=False)); return
    if a.check_draft:
        with open(a.check_draft,encoding="utf-8",errors="ignore") as _f: _txt=_f.read()
        print(json.dumps(check_draft(_txt, url="https://draft.local/"+os.path.basename(a.check_draft)), indent=2, ensure_ascii=False)); return
    if a.check_answer:
        if not a.url: ap.error("--check-answer requires --url (the site to fact-check the answer against)")
        _ans=open(a.check_answer,encoding="utf-8",errors="ignore").read() if os.path.exists(a.check_answer) else a.check_answer
        print(json.dumps(check_ai_facts(_ans, a.url), indent=2, ensure_ascii=False)); return
    if a.calibrate:
        calibrate(a.report or (a.out+".json"), a.calibrate); return
    if not a.url: ap.error("--url required (or use --calibrate with --report)")
    print(f"Chrome: {CHROME or 'NONE (raw only)'}")
    def prog(phase,done,total,msg):
        print(f"  [{done}/{total}] {msg}" if phase=="crawl" else msg, flush=True)
    data=run_audit(a.url,out=a.out,max_pages=a.max_pages,workers=a.workers,progress=prog,client=a.client,intro=a.intro,links=not a.no_links,queries=a.queries,logs=a.logs,agency=a.agency,logo=a.logo,auth=_build_auth(a))
    print(f"\n=== Rubric: {data['domain']} === {data['overall']}/100 | {data['pages_crawled']} pages")
    print("Pillars: "+" | ".join(f"{k} {v}" for k,v in data['pillars'].items()))
    print("Engines: "+" | ".join(f"{e} {v}" for e,v in data['engines'].items()))
    top=data['issues'][:3]
    print("Do first: "+" ; ".join(f"{i['label']} (+{i['gain_overall']})" for i in top))
    print(f"Report: {a.out}.html / .json / .csv")

if __name__=="__main__": main()
