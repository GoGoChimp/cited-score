"""CITED Score - local MCP server (stdio transport).

Exposes the crawl + citability + log-monitor tools to the client's OWN AI (Claude Desktop/Code, Cursor,
ChatGPT connectors). LOCAL and private: every tool runs on this machine and nothing leaves it. The
analysis tools take their data as INPUT (a file path, a paste, or another MCP's output) so CITED Score
composes with whatever the client already has connected - it is the crawl-and-reasoning brain, not a
data connector.

Run:  python mcp_server.py         (the host launches this; it speaks MCP over stdio)
Install: add to the host's MCP config, e.g. Claude Desktop claude_desktop_config.json:
  "cited-score": { "command": "python", "args": ["C:\\\\Users\\\\chris\\\\ai-seo-crawler\\\\mcp_server.py"] }
"""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
os.chdir(_HERE)                       # so aiseo_audit's relative resource reads work, as they do from the CLI

import json, datetime, urllib.parse
import aiseo_audit as A
import audit_store as _store
from mcp.server.fastmcp import FastMCP

_INSTRUCTIONS = (
    "CITED Score is a local, privacy-first AI-SEO auditor - 'Screaming Frog for AI search'. It measures how "
    "citable a site is by AI engines (ChatGPT, Perplexity, Google AI Overviews, Gemini, Copilot, Claude) and "
    "helps improve it. Reach for these tools when the user asks about AI-search visibility, getting cited by AI, "
    "whether a page/draft will be picked up by AI, AI-crawler activity, or the value/ROI of AI search.\n"
    "Tools: audit_site (score a whole site), check_draft (lint a DRAFT's citability BEFORE publishing), "
    "click_resilience (will AI still send a click or answer inline), analyze_log / monitor_log (real AI-bot "
    "activity from a server access log), citation_coverage + correlate (map pages to citation data, find gaps), "
    "estimate_ai_influence (honest ranged estimate of AI-influenced value). "
    "Everything runs locally; analysis tools take a file path or pasted data as input. If unsure what to offer, "
    "call cited_score_help for a plain-English menu, or use one of the guided prompts (audit_my_site, "
    "check_my_draft, estimate_ai_roi, find_citation_gaps, am_i_being_crawled, is_this_page_worth_the_click)."
)
mcp = FastMCP("cited-score", instructions=_INSTRUCTIONS)

_USAGE = os.path.join(_HERE, "mcp_usage.json")
def _telemetry(tool):
    """LOCAL, disclosed usage counter (per-tool call count + first/last used). Stays on THIS machine -
    nothing is sent. An opt-in, consent-gated upload to gauge adoption is a documented follow-up, not wired."""
    try:
        data = {}
        if os.path.exists(_USAGE):
            with open(_USAGE, encoding="utf-8") as f:
                data = json.load(f)
        now = datetime.datetime.now().isoformat(timespec="seconds")
        rec = data.get(tool) or {"count": 0, "first": now, "last": now}
        rec["count"] += 1
        rec["last"] = now
        data[tool] = rec
        with open(_USAGE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


@mcp.tool()
def audit_site(url: str, max_pages: int = 25) -> dict:
    """Crawl a site and return its CITED Score (AI-search citability). Use when the user wants to know how
    citable a site or page is by AI engines, or where to improve it.

    Returns the overall score, the Known / Findable / Trusted pillars, per-engine scores
    (ChatGPT, Perplexity, AI Overviews, Gemini, Copilot, Claude), the site type, the top prioritised
    fixes ("do first"), and the weakest crawled pages. max_pages caps the crawl (0 = whole site)."""
    _telemetry("audit_site")
    d = A.run_audit(url, out=None, max_pages=max_pages, links=False)
    audit_id = _store.save(A.mcp_audit(d), url)   # persist for the reader tools (get_audit / get_action_plan / ...)
    pages = sorted((d.get("pages") or []), key=lambda p: (p.get("score") or 0))
    return {
        "audit_id": audit_id,
        "domain": d.get("domain"),
        "overall": d.get("overall"),
        "pillars": d.get("pillars"),
        "engines": d.get("engines"),
        "site_type": d.get("site_type_label") or d.get("site_type"),
        "pages_crawled": d.get("pages_crawled"),
        "do_first": [{"fix": i.get("label"), "gain": i.get("gain_overall")}
                     for i in (d.get("issues") or [])[:6]],
        "weakest_pages": [{"url": p.get("url"), "type": p.get("type"), "score": p.get("score")}
                          for p in pages if p.get("status") == 200][:8],
        "note": d.get("crawl_note"),
        "next": "get_action_plan(audit_id) for the ranked fixes, or run the rubric fix loop.",
    }


# ---- local MCP reader tools: mirror the hosted MCP (route.ts) over the local audit_store, so the rubric
#      fix-loop skill runs entirely locally. audit_site is the (synchronous) crawl; these read what it stored.
def _ek(k): return k.lower().replace(" ", "_")
def _domain_of(u):
    try: return (urllib.parse.urlparse(u).hostname or u).replace("www.", "")
    except Exception: return u

@mcp.tool()
def list_audits(limit: int = 10, domain: str = "") -> dict:
    """List recent local audits with score and the change vs the previous crawl of that domain."""
    _telemetry("list_audits")
    return {"audits": _store.list_recent(limit=min(int(limit or 10), 25), domain=(domain or None))}

@mcp.tool()
def get_audit(audit_id: str) -> dict:
    """The full scorecard for one stored audit: score, the Known/Findable/Trusted pillars, per-engine
    readiness, error/warning counts, page types, crawler reachability, and site type. Read the score against
    the quotable line (70), not as a bare number."""
    _telemetry("get_audit")
    a = _store.load(audit_id)
    if not a: return {"error": "Audit not found. Run audit_site(url), or see list_audits."}
    out = {k: a.get(k) for k in ("domain", "pages_crawled", "created_at", "score", "quotable_threshold",
           "pillars", "engines", "counts", "pages_by_type", "reachability", "website_type", "caveats")}
    out["next"] = "get_action_plan(audit_id) for the ranked fixes."
    return out

@mcp.tool()
def get_action_plan(audit_id: str, engine: str = "", tier: str = "", owner: str = "", limit: int = 10) -> dict:
    """The ranked fixes for an audit. Optionally re-rank for a target engine (chatgpt/perplexity/ai_overviews/
    gemini/copilot/claude), filter by tier (biggest_movers/worth_doing/marginal), or by owner (dev/content/
    config). Prefer template_level fixes (they move the most pages per edit); apply ONE at a time."""
    _telemetry("get_action_plan")
    a = _store.load(audit_id)
    if not a: return {"error": "Audit not found."}
    fixes = [dict(f) for f in (a.get("action_plan") or [])]
    if engine: fixes.sort(key=lambda f: -((f.get("gain_by_engine") or {}).get(engine, 0)))
    if tier == "biggest_movers": fixes = [f for f in fixes if (f.get("gain_overall") or 0) >= 2]
    elif tier == "worth_doing": fixes = [f for f in fixes if (f.get("gain_overall") or 0) == 1]
    elif tier == "marginal": fixes = [f for f in fixes if (f.get("gain_overall") or 0) <= 0]
    if owner: fixes = [f for f in fixes if owner in (f.get("owner"), f.get("owner_secondary"))]
    lim = min(int(limit or 10), 25)
    out = [{k: f.get(k) for k in ("rank", "check_id", "title", "pillar", "severity", "effort", "owner",
            "owner_secondary", "pages_affected", "gain_overall", "gain_by_engine", "instruction",
            "source_ref", "template_level")} for f in fixes[:lim]]
    if engine:
        for i, f in enumerate(out): f["rank"] = i + 1
    return {"total_fixes": a.get("total_fixes"), "returned": len(out),
            "projected_score_if_all_applied": a.get("projected_score_if_all_applied"),
            "ranked_for_engine": engine or "overall", "fixes": out}

@mcp.tool()
def get_affected_pages(audit_id: str, check_id: str, limit: int = 50) -> dict:
    """The pages a specific check fails on, with the current finding and the check's guidance. Paginated."""
    _telemetry("get_affected_pages")
    a = _store.load(audit_id)
    if not a: return {"error": "Audit not found."}
    fix = next((f for f in (a.get("action_plan") or []) if f.get("check_id") == check_id), None)
    if not fix: return {"error": "No fix with that check_id in this audit."}
    allp = fix.get("affected") or []; lim = min(int(limit or 50), 100)
    return {"check_id": check_id, "title": fix.get("title"), "total": len(allp),
            "returned": min(len(allp), lim), "pages": allp[:lim]}

@mcp.tool()
def get_page(audit_id: str, url: str) -> dict:
    """One page's score, per-engine readiness, and per-check results."""
    _telemetry("get_page")
    a = _store.load(audit_id)
    if not a: return {"error": "Audit not found."}
    for p in (a.get("pages") or []):
        pu = p.get("url") or ""
        if pu == url or (_domain_of(pu) + urllib.parse.urlparse(pu).path) == url:
            return p
    return {"error": "Page not found in this audit.", "hint": "Use the exact url from get_affected_pages."}

@mcp.tool()
def compare_audits(audit_id_a: str, audit_id_b: str) -> dict:
    """Diff two audits (a = newer, b = older): score, pillar and engine deltas, checks fixed, checks regressed.
    Always report the regressions, not only the improvements. The retention hook after a fix + re-crawl."""
    _telemetry("compare_audits")
    a = _store.load(audit_id_a); b = _store.load(audit_id_b)
    if not a or not b: return {"error": "One or both audits not found."}
    pd = {k: ((a.get("pillars") or {}).get(k) or 0) - ((b.get("pillars") or {}).get(k) or 0) for k in ("known", "findable", "trusted")}
    ed = {k: ((a.get("engines") or {}).get(k) or 0) - ((b.get("engines") or {}).get(k) or 0) for k in (a.get("engines") or {})}
    A_ids = {f.get("check_id"): f for f in (a.get("action_plan") or [])}
    B_ids = {f.get("check_id"): f for f in (b.get("action_plan") or [])}
    fixed = [{"check_id": k, "title": B_ids[k].get("title"), "pages_resolved": B_ids[k].get("pages_affected")} for k in B_ids if k not in A_ids]
    regressed = [{"check_id": k, "title": A_ids[k].get("title"), "pages_affected": A_ids[k].get("pages_affected")} for k in A_ids if k not in B_ids]
    return {"newer": a.get("created_at"), "older": b.get("created_at"),
            "score_delta": (a.get("score") or 0) - (b.get("score") or 0),
            "pillar_deltas": pd, "engine_deltas": ed, "fixed": fixed[:20], "regressed": regressed[:20],
            "unchanged_count": len([k for k in A_ids if k in B_ids])}


@mcp.tool()
def check_draft(content: str, url: str = "https://draft.local/page") -> dict:
    """Pre-publish citability lint. Score a DRAFT the way a crawled page is scored, and return the
    extractability checks + the specific fixes, so the draft can be corrected BEFORE it ships (instead of
    waiting weeks for citation lag to reveal whether AI picks it up). Call this while writing or editing
    content. Pass HTML for the full check; plain text still returns the prose-level checks.

    Returns a verdict, the passing-check count, and each fix (which check failed, the detail, and why it
    matters). The killer signal is usually structural: tables/lists present, self-contained sections
    (no walls of text), answer-first opener, and entity density."""
    _telemetry("check_draft")
    return A.check_draft(content, url=url)


@mcp.tool()
def cited_gap(your_url: str, winner_url: str, query: str = "") -> dict:
    """The "you vs the cited winner" diff - the core of the AI-citation fix loop. Given YOUR page and the
    page an AI engine actually CITED for a query (which you capture from the engine's answer in your own
    browser - no API needed), this scores both and returns the on-page citability signals the winner has
    that you lack, ranked by how much engines weight them, each with the concrete fix. Use it once you've
    seen who got cited and want to know exactly why, and what to change to close it. query is optional
    context. Off-page factors (domain authority, third-party mentions) are not measured here."""
    _telemetry("cited_gap")
    return A.cited_gap(your_url, winner_url, query=query or None)


@mcp.tool()
def source_of_truth(cited_urls: list[str], your_domain: str = "") -> dict:
    """Classify the URLs an AI engine cited (captured from the answer in your browser) into your own site vs
    the third-party sources engines trust - Reddit, Wikipedia, YouTube, G2, review sites, news. Tells you
    whether to improve your page or GET INTO the source (brands are ~6.5x likelier to be cited via third
    parties than their own domain). Pass the list of cited URLs and your domain."""
    _telemetry("source_of_truth")
    return A.source_of_truth(cited_urls, your_domain=your_domain)


@mcp.tool()
def subquery_sov(fanout: list, your_domain: str, competitors: str = "") -> dict:
    """Sub-query share-of-voice across the fan-out - the honest 'share of AI voice'. Given the sub-queries an
    engine expanded a prompt into and the sources it cited for each (captured from the answer in your
    browser), returns the % of the fan-out citing YOU vs each competitor plus the exact sub-queries you're
    missing. fanout is a list of {subquery, cited:[domains or urls]}; competitors is comma-separated."""
    _telemetry("subquery_sov")
    return A.subquery_sov(fanout, your_domain, competitors=competitors)


@mcp.tool()
def competitor_teardown(competitor_url: str, your_url: str) -> dict:
    """Run the citability diff the OTHER way: find where a competitor is WEAK so you can take those
    sub-queries. Crawls both pages and returns the competitor's weakest citability signals ranked by engine
    weight, split into 'you already beat them' and 'open for both'. On-page only; off-page authority not measured."""
    _telemetry("competitor_teardown")
    return A.competitor_teardown(competitor_url, your_url)


@mcp.tool()
def value_bridge(url: str, perf_csv: str, value_per_click: float = 0.0) -> dict:
    """Put a RANGED 'clicks at risk' number on being un-citable by joining your citability gaps to real
    traffic. Crawls the site and matches pages to a Google Search Console 'Pages' export (or GA landing-page
    CSV: url, clicks, impressions - a local file path the user provides), then estimates the clicks AI
    answers put at risk on pages that pull traffic but score low on citability. value_per_click (optional)
    converts to money. Honest ranges, never attribution."""
    _telemetry("value_bridge")
    return A.value_bridge(url, perf_csv, value_per_click=value_per_click)


@mcp.tool()
def loop_report(findings: dict, out_path: str = "cited-loop-report.html") -> dict:
    """Feedback bridge - render the AI-citation fix-loop findings you assembled into a branded CitedScore
    report (the 'Claude feeds CitedScore' step of the loop). Pass `findings` as a dict with any of: query,
    your_domain, named/cited (booleans), cited_sources (list of URLs), source_of_truth (the source_of_truth
    tool's result), subquery_sov (the subquery_sov tool's result), gap (the cited_gap tool's result), fix
    (your recommended fix - a string or a list of steps). Every section is optional; it renders whatever you
    pass. Writes a self-contained HTML report to out_path and returns its path."""
    _telemetry("loop_report")
    A.render_loop_report(findings, out_path)
    return {"tool": "CitedScore loop_report", "report_path": out_path,
            "note": "Open the HTML to see the branded fix-loop report. Feed it findings from capture + the cited_gap / source_of_truth / subquery_sov tools plus your recommended fix."}


@mcp.tool()
def analyze_log(log_path: str) -> dict:
    """Point-in-time AI-bot analysis of a server access log (Apache/Nginx combined). Ground truth the
    reachability probe can only estimate: which AI bots actually fetched, how often, with what status,
    and where. Citation-time bots (ChatGPT-User, Perplexity-User, Claude-User, OAI-SearchBot) fetching =
    live citation activity. Pass a local log file path (a file the user already has - Cloudflare Logpush
    or origin logs); nothing is fetched or sent."""
    _telemetry("analyze_log")
    return A.analyze_logs(log_path)


@mcp.tool()
def monitor_log(log_path: str, history_path: str = "") -> dict:
    """Standing log MONITOR - the time-series view. Buckets AI-bot hits by DAY from the log's own
    timestamps and reports per-bot crawl-frequency trend, citation-time fetch rate over time, real block
    detection (401/403/429), and new-bot arrivals. If history_path is given, merges with prior uploads
    into a persistent per-day file so successive logs accumulate (overlapping re-uploads never
    double-count). Use for "is AI crawling me more or less over time / who's blocked / any new bots".
    All parsed locally."""
    _telemetry("monitor_log")
    return A.monitor_logs(log_path, history_path or None)


@mcp.tool()
def citation_coverage(url: str, queries_csv: str, max_pages: int = 0) -> dict:
    """Which cited queries each page targets. Crawls the site and maps it against a grounding-query CSV
    (e.g. a Bing AI Performance 'AI Search Queries' export the user provides), so the user can see which
    citation-driving queries have a page and which are uncovered gaps. queries_csv is a local file path.
    max_pages defaults to 0 (whole site): gap analysis needs full coverage, or a page that DOES target a
    query but sits deep in the sitemap gets dropped by a low cap and reported as a FALSE gap. Pass a
    number only to cap a large site for speed."""
    _telemetry("citation_coverage")
    d = A.run_audit(url, out=None, max_pages=max_pages, links=False)
    return A.query_coverage(d.get("pages") or [], A.load_queries(queries_csv))


@mcp.tool()
def click_resilience(url: str) -> dict:
    """Will AI still send a click for this page, or answer it inline? Returns a per-page resilience band
    (high / medium / low) with reasons and advice. HIGH = AI cites you but the user still needs to visit
    (original data, tools, transactional pages) - protect and expand. LOW = AI answers it fully (a
    standalone definition / thin explainer) - low click value, don't over-invest. Directional proxy from
    signals already computed (page type, info-gain, actionable schema, tables)."""
    _telemetry("click_resilience")
    dom = urllib.parse.urlparse(url).netloc.replace("www.", "") or "site"
    return A.click_resilience(A.process(url, dom))


@mcp.tool()
def correlate(url: str, citations_csv: str = "", log_path: str = "", max_pages: int = 0) -> dict:
    """Deterministically JOIN a crawl with citation counts and server-log AI-bot activity, per URL - so the
    analysis is reliable instead of hoping the model stitches three sources together. citations_csv is a
    local 'url,citations' file (Bing WMT export); log_path is a local access log. Returns joined rows +
    plain-language insights (cited vs uncited score gap, well-scored pages earning zero citations, pages AI
    fetched but did not cite). max_pages defaults to 0 (whole site): a cap that misses cited pages makes them
    look uncited and buries the real gaps. Pass a number only to cap a large site for speed."""
    _telemetry("correlate")
    d = A.run_audit(url, out=None, max_pages=max_pages, links=False)
    cites = {}
    if citations_csv:
        with open(citations_csv, encoding="utf-8", errors="ignore") as f:
            cites = A.parse_cites(f.read())
    logrows = A.load_access_log(log_path) if log_path else None
    return A.correlate_data(d.get("pages") or [], cites, logrows)


@mcp.tool()
def estimate_ai_influence(ai_sessions: int = 0, total_citations: int = 0, ai_revenue: float = 0.0,
                          avg_deal_value: float = 0.0, close_rate: float = 0.0, conversion_rate: float = 0.0,
                          self_reported_ai: int = 0, period_days: int = 30) -> dict:
    """Honest, RANGED estimate of AI-INFLUENCED value (never 'attribution'). Pass numbers the client's AI
    can pull from GA4 (AI-Assistant channel: ai_sessions, ai_revenue), citations (total_citations from
    Bing/Clarity), and an optional self-report anchor (self_reported_ai = leads who said 'AI recommended
    us', with avg_deal_value + close_rate). For lead/services businesses supply conversion_rate +
    avg_deal_value [+ close_rate] instead of ai_revenue. Returns a measured floor + low/mid/high range with
    explicit assumptions and loud caveats. The signal is weak by nature; this assembles it honestly and
    never fakes precision."""
    _telemetry("estimate_ai_influence")
    return A.estimate_ai_influence(ai_sessions, total_citations, ai_revenue, avg_deal_value,
                                   close_rate, conversion_rate, self_reported_ai, period_days)


@mcp.tool()
def cited_score_help() -> dict:
    """What CITED Score can do, in plain English. Call this when the user asks what cited-score (or this
    server / these tools) can do, or how to use it. Returns a menu of capabilities with example asks."""
    _telemetry("cited_score_help")
    return {
        "what_it_is": "CITED Score - a local, privacy-first AI-SEO auditor. It scores how citable your site is "
                      "by AI engines and helps you improve it. Nothing leaves your machine.",
        "capabilities": [
            {"tool": "audit_site", "does": "Scores a whole site for AI citability + top fixes.",
             "ask": "Audit gogochimp.com with cited-score."},
            {"tool": "check_draft", "does": "Lints a DRAFT before you publish - will AI cite it? what to fix?",
             "ask": "Check whether this article will get cited by AI: <paste>."},
            {"tool": "fact_check_answer", "does": "Diffs an AI answer about your brand against your own site - catches a wrong founding year, location or price the AI is stating about you.",
             "ask": "Fact-check what ChatGPT said about my brand: <paste the answer> - my site is gogochimp.com."},
            {"tool": "click_resilience", "does": "Will AI still send a click for this page, or answer it inline?",
             "ask": "Is my pricing page worth the click for AI? https://..."},
            {"tool": "monitor_log / analyze_log", "does": "Real AI-bot activity from your server log - who crawls you, citation-time fetches, blocks, trend.",
             "ask": "Monitor my access log at C:\\logs\\access.log."},
            {"tool": "citation_coverage / correlate", "does": "Maps your pages to real citation data - which queries you cover, which well-scored pages earn zero citations.",
             "ask": "Find my citation gaps: correlate gogochimp.com with my Bing CSV."},
            {"tool": "estimate_ai_influence", "does": "Honest, ranged estimate of AI-influenced value (never fake precision).",
             "ask": "Estimate my AI ROI - I'll give you my GA4 and citation numbers."},
        ],
        "tip": "You can also pick a guided prompt from the menu (audit_my_site, check_my_draft, estimate_ai_roi, "
               "find_citation_gaps, am_i_being_crawled, is_this_page_worth_the_click).",
    }


# --- Guided prompts: these surface as clickable actions in the host UI (Claude Desktop '+' menu /
# --- Claude Code slash commands like /mcp__cited-score__audit_my_site) so users discover what to ask.
@mcp.prompt()
def audit_my_site(url: str) -> str:
    """Audit a site's AI-search citability and get the priority fixes."""
    return (f"Use the cited-score audit_site tool on {url}. Summarise the overall score, the Known / Findable / "
            "Trusted pillars, the per-engine scores, and the top fixes to do first - in plain English.")


@mcp.prompt()
def check_my_draft(draft: str) -> str:
    """Check whether a draft will get cited by AI, before you publish it."""
    return ("Use the cited-score check_draft tool on the content below, then give me the fixes in priority order "
            "(structure first - tables/lists, answer-first opener, self-contained sections, entity density):\n\n" + draft)


@mcp.prompt()
def estimate_ai_roi() -> str:
    """Estimate the AI-influenced value of your traffic (honest, with ranges)."""
    return ("Help me estimate my AI-influenced value with the cited-score estimate_ai_influence tool. First ask me "
            "for the numbers it needs - GA4 AI-Assistant channel sessions and revenue, total AI citations, and if I'm "
            "a lead/services business my site conversion rate, average deal value and close rate, plus how many leads "
            "said an AI recommended us. Then return the measured floor, the low/mid/high range, and the caveats.")


@mcp.prompt()
def fix_loop(url: str, query: str) -> str:
    """Run the full AI-citation fix loop for a money query: capture the real answer, diff you vs the cited winner, hand back the fix."""
    return (
        f"Run the AI-citation fix loop for {url} on the query \"{query}\". You are the brain; CitedScore is the body. "
        "No paid APIs - everything is captured from the browser.\n\n"
        f"1. CAPTURE: open ChatGPT (and Perplexity if you can) in the browser, ask \"{query}\", and read the answer. "
        "Record which sources it CITED (the URLs), whether this brand is NAMED vs merely cited, and the fan-out: the "
        "sub-queries it expanded into (visible in the network 'conversation' payload) and which sources were cited for each.\n"
        "2. MAP: call source_of_truth on the cited URLs (with your_domain) to see whether the win is on-page or getting "
        "INTO a source (Reddit / Wikipedia / G2 / YouTube). Call subquery_sov with the fan-out for coverage, share of "
        "voice, and the sub-queries you're missing.\n"
        f"3. DIFF: for the page the engine actually cited (the winner), call cited_gap(your_url={url}, "
        f"winner_url=<the cited url>, query=\"{query}\") to get the exact on-page signals the winner has that you lack, "
        "ranked, each with the fix.\n"
        "4. JUDGE (your read, not a tool): is the brand the PICK, a neutral mention, or a footnote? Is anything the "
        "engine says about it false - if so, flag it and the source feeding it. From the source-of-truth split, decide "
        "fix-the-page vs get-into-the-source, and if the latter, draft the outreach target and angle.\n"
        "5. FIX: hand back the ranked fixes and a paste-ready rewrite of the weakest section, then call check_draft on "
        "the rewrite to confirm it passes. Be honest about confidence: AI answers are non-deterministic, so note when a "
        "finding came from a single capture and re-run the query a couple of times for anything you assert as stable.")


@mcp.prompt()
def find_citation_gaps(url: str, citations_csv: str) -> str:
    """Find pages that score well but earn zero AI citations - your opportunities."""
    return (f"Use the cited-score correlate tool on {url} with my citations file at {citations_csv}. Show me the "
            "pages that score well (>=75) but earn zero citations - my clearest citation opportunities - and the "
            "cited-vs-uncited score gap.")


@mcp.prompt()
def am_i_being_crawled(log_path: str) -> str:
    """See which AI bots crawl you over time, from a server log."""
    return (f"Use the cited-score monitor_log tool on my access log at {log_path}. Tell me which AI bots are crawling "
            "me, the citation-time fetch trend (ChatGPT-User / Perplexity-User / Claude-User), whether any are blocked, "
            "and any new bots.")


@mcp.prompt()
def is_this_page_worth_the_click(url: str) -> str:
    """Will AI still send a click for a page, or just answer it inline?"""
    return (f"Use the cited-score click_resilience tool on {url}. Tell me whether AI will still send clicks or answer "
            "it inline, why, and what to do about it.")


@mcp.tool()
def agent_readiness(url: str, max_pages: int = 25) -> dict:
    """Can an AI AGENT transact on this site? Crawls it and returns a scored 'Actionable' readiness band
    (can an agent complete a purchase / booking / contact on the money pages) plus the specific missing
    signals (offer, price, availability, action schema, contact) that would stop an agent. Advisory - not
    part of the core CITED Score. N/A for sites with no transactable pages."""
    _telemetry("agent_readiness")
    d = A.run_audit(url, out=None, max_pages=max_pages, links=False)
    return A.actionable_readiness(d)


@mcp.tool()
def analyze_ai_answer(answer_text: str, brand: str, domain: str = "", competitors: str = "") -> dict:
    """The mechanical layer of the AI ground-truth audit. Paste a consumer-AI answer (from ChatGPT /
    Perplexity / Gemini / Copilot) and this returns the deterministic presence checks: is the BRAND named,
    is its DOMAIN cited, which COMPETITORS (comma/newline list) are named, and how early the brand appears.
    Use after probing an engine so a service run is repeatable. The FRAMING / who-wins judgement stays with
    you or the model reading the answer - this tool does not score quality."""
    _telemetry("analyze_ai_answer")
    return A.scan_ai_answer(answer_text, brand, domain, competitors)


@mcp.tool()
def analyze_ai_panel(answers: list, brand: str, domain: str = "", competitors: str = "") -> dict:
    """Aggregate several engines' answers to the SAME question into one cross-engine PANEL - the 'polling'
    view of AI visibility. 91% of AI citations show on only ONE engine (Indig H1 2026), so a single-engine
    read misleads; the panel is the honest unit. answers = a list of {engine, text} (paste each consumer-AI
    answer - ChatGPT / Perplexity / Gemini / AI Overviews / Copilot / Claude). Returns where the brand is
    NAMED vs merely CITED (the 'cited != recommended' gap the market now optimises for), panel share of
    voice, the competitor leaderboard, and brand rank. Mechanical/deterministic; the browser probing that
    GETS the answers stays assisted (no automation or keys in the tool). Use analyze_ai_answer for a single
    answer, this to compare across engines or over time."""
    _telemetry("analyze_ai_panel")
    return A.scan_ai_panel(answers, brand, domain, competitors)


@mcp.tool()
def fact_check_answer(answer_text: str, domain: str, brand: str = "") -> dict:
    """Hallucination / entity-substitution check. Paste what an AI said about a brand and this DIFFS its
    factual claims against the brand's OWN site (Organization / Product schema + key pages): it flags a
    founding year, location, or price the AI states that CONTRADICTS your site (a stale-training
    hallucination) or that your site never states (a gap the AI is filling in for you). Local, no keys.
    High-precision by design - it compares only structured facts readable on both sides, so it
    under-reports rather than guesses, and no findings is not proof the answer is accurate. Pairs with
    analyze_ai_answer, which checks whether you are NAMED, not whether the claims are TRUE."""
    _telemetry("fact_check_answer")
    return A.check_ai_facts(answer_text, domain, brand)


if __name__ == "__main__":
    mcp.run()
