"""Rubric proof loop (engine side): Bing export parsing and the tunable defaults
shared by the later proof-loop stages."""
import csv
import datetime
import io
import re

from aiseo_audit import canon_key, correlate_data

# Tunable defaults, defined once (Global Constraints).
CLUSTER_WINDOW_DAYS = 28
MIN_PERIOD_DAYS = 14
COHORT_MIN_PAGES = 8
PERIOD_CITATION_FLOOR = 30
CONFIRM_CRAWLS = 1
CAVEAT_LENGTH_RATIO = 2.0
CAVEAT_MIDPOINT_GAP_DAYS = 90

_URL_HEADERS = ("page", "url", "address", "landing page", "page url")
_CITE_HEADERS = ("citations", "citation", "citation count", "count", "ai citations")


def _find(header, candidates):
    low = {h.lower().strip(): h for h in header if h}
    for c in candidates:
        if c in low:
            return low[c]
    return None


def parse_bing_export(text: str) -> dict:
    """Parse a Bing AI Performance per-URL export as downloaded (utf-8-sig, extra
    columns tolerated). Sums duplicate URLs after canonicalisation."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = reader.fieldnames or []
    url_col = _find(header, _URL_HEADERS)
    cite_col = _find(header, _CITE_HEADERS)
    if not url_col or not cite_col:
        raise ValueError("Could not find a URL column and a citations column in the export.")
    counts, rows, urls = {}, 0, 0
    for r in reader:
        u = (r.get(url_col) or "").strip()
        if not u:
            continue
        urls += 1
        try:
            c = float(str(r.get(cite_col) or 0).replace(",", "").strip() or 0)
        except ValueError:
            c = 0.0
        key = canon_key(u)
        counts[key] = counts.get(key, 0.0) + c
        rows += 1
    return {"counts": counts, "rows": rows, "urls_seen": urls}


def parse_period(text: str, filename: str = ""):
    """Return (start, end) dates if the export embeds them, else (None, None).
    Looks for two ISO (yyyy-mm-dd) dates in the first lines of the text or in the
    filename."""
    blob = (filename or "") + "\n" + "\n".join(text.splitlines()[:3])
    iso = re.findall(r"(\d{4}-\d{2}-\d{2})", blob)
    if len(iso) >= 2:
        try:
            return datetime.date.fromisoformat(iso[0]), datetime.date.fromisoformat(iso[1])
        except ValueError:
            return None, None
    return None, None


def diff_crawls(prev: dict, cur: dict) -> dict:
    """Per-page fix (fail->pass) and regression (pass->fail) detection between two
    crawl_page_checks stamps, like-with-like: only checks evaluated in BOTH crawls
    and only pages present in BOTH crawls count."""
    common_checks = set(prev.get("checks_evaluated") or []) & set(cur.get("checks_evaluated") or [])
    pf_prev, pf_cur = prev.get("page_fails") or {}, cur.get("page_fails") or {}
    common_urls = set(pf_prev) & set(pf_cur)
    fixes, regressions = [], []
    for u in sorted(common_urls):
        was = set(pf_prev[u]) & common_checks
        now = set(pf_cur[u]) & common_checks
        fixed = sorted(was - now)        # failing before, passing now
        regressed = sorted(now - was)    # passing before, failing now
        base = {"url": u, "prev_crawl_at": prev.get("crawled_at"), "detected_at": cur.get("crawled_at")}
        if fixed:
            fixes.append({**base, "checks": fixed})
        if regressed:
            regressions.append({**base, "checks": regressed})
    return {"fixes": fixes, "regressions": regressions}


def _dt(s):
    return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def confirm_transitions(provisional, cur_page_fails, common_checks):
    """Settle provisional fixed/regressed events against the current crawl.
    A fixed event confirms when none of its checks are failing now, cancels when
    any is failing again. A regressed event confirms while its checks are still
    all failing, cancels otherwise."""
    confirm, cancel = [], []
    for e in provisional:
        now_failing = set(cur_page_fails.get(e["url"], [])) & set(common_checks)
        checks = set(e["checks"])
        if e["kind"] == "fixed":
            held = checks.isdisjoint(now_failing)       # none failing again -> held
        else:  # regressed: "held" means still failing
            held = checks.issubset(now_failing)
        (confirm if held else cancel).append(e["id"])
    return {"confirm": confirm, "cancel": cancel}


def cluster_fixes(events, window_days=CLUSTER_WINDOW_DAYS):
    """Merge same-url events detected within window_days of the cluster's first
    event into one: earliest detected_at, union of checks."""
    by_url = {}
    for e in events:
        by_url.setdefault(e["url"], []).append(e)
    out = []
    for url, evs in by_url.items():
        evs = sorted(evs, key=lambda x: _dt(x["detected_at"]))
        cur = None
        for e in evs:
            if cur and (_dt(e["detected_at"]) - _dt(cur["detected_at"])).days <= window_days:
                cur["checks"] = sorted(set(cur["checks"]) | set(e["checks"]))
            else:
                cur = {"url": url, "checks": sorted(set(e["checks"])),
                       "detected_at": e["detected_at"], "prev_crawl_at": e.get("prev_crawl_at")}
                out.append(cur)
    return out


def _days(p):
    return (datetime.date.fromisoformat(p["period_end"]) - datetime.date.fromisoformat(p["period_start"])).days + 1


def _midpoint(p):
    s = datetime.date.fromisoformat(p["period_start"])
    e = datetime.date.fromisoformat(p["period_end"])
    return s + (e - s) / 2


def _cohort(period, urls):
    urls = list(dict.fromkeys(urls))     # de-duplicate: a repeated page must not inflate n_pages or totals
    counts = period["counts"]
    present = [u for u in urls if u in counts]
    total = sum(counts.get(u, 0) for u in urls)
    days = _days(period)
    return {"n_pages": len(present), "total": total, "days": days,
            "per_day": (total / days) if days else 0.0}


def tier_a_compare(before, after, fixed_urls, untouched_urls):
    """Compare citations-per-day over a before-period vs an after-period for the
    fixed cohort, with an untouched control over the identical periods. Hidden
    (shown=False + reason) when a period is too short, the fixed cohort is too
    small, or either period's fixed citations are below the floor."""
    db, da = _days(before), _days(after)
    # Calendar dates of both periods, so a consumer can show the ranges even when hidden.
    periods = {"before_period": {"start": before["period_start"], "end": before["period_end"]},
               "after_period": {"start": after["period_start"], "end": after["period_end"]}}
    if db < MIN_PERIOD_DAYS or da < MIN_PERIOD_DAYS:
        return {"shown": False, "reason": f"a period is shorter than the {MIN_PERIOD_DAYS}-day minimum", "caveat": None, **periods}
    fb = _cohort(before, fixed_urls)
    fa = _cohort(after, fixed_urls)
    if min(fb["n_pages"], fa["n_pages"]) < COHORT_MIN_PAGES:
        return {"shown": False, "reason": "not enough fixed pages with citation data yet", "caveat": None, **periods}
    if fb["total"] < PERIOD_CITATION_FLOOR or fa["total"] < PERIOD_CITATION_FLOOR:
        return {"shown": False, "reason": "not enough citations yet on these pages", "caveat": None, **periods}
    ub = _cohort(before, untouched_urls)
    ua = _cohort(after, untouched_urls)
    untouched = {"before_total": ub["total"], "after_total": ua["total"],
                 "before_per_day": ub["per_day"], "after_per_day": ua["per_day"],
                 "days_before": db, "days_after": da, "n_pages": min(ub["n_pages"], ua["n_pages"])}
    untouched["insufficient"] = (untouched["n_pages"] < COHORT_MIN_PAGES
                                 or ub["total"] < PERIOD_CITATION_FLOOR or ua["total"] < PERIOD_CITATION_FLOOR)
    ratio = max(db, da) / max(1, min(db, da))
    gap = abs((_midpoint(after) - _midpoint(before)).days)
    caveat = ("periods differ in length; compare with care."
              if (ratio > CAVEAT_LENGTH_RATIO or gap > CAVEAT_MIDPOINT_GAP_DAYS) else None)
    return {"shown": True, "reason": None, "caveat": caveat,
            "fixed": {"before_total": fb["total"], "after_total": fa["total"],
                      "before_per_day": fb["per_day"], "after_per_day": fa["per_day"],
                      "days_before": db, "days_after": da, "n_pages": min(fb["n_pages"], fa["n_pages"])},
            "untouched": untouched, **periods}


# The honesty strings every proof summary carries (Global Constraints).
LABELS = {"source": "Bing/Copilot data only",
          "causation": "correlation, not proof of cause",
          "dated": True}


def _period_before(snaps, when_iso):
    """Newest snapshot whose whole period ended on or before when_iso."""
    d = _dt(when_iso).date()
    cands = [s for s in snaps if datetime.date.fromisoformat(s["period_end"]) <= d]
    return max(cands, key=lambda s: s["period_end"], default=None)


def _period_after(snaps, when_iso):
    """Earliest snapshot whose whole period started on or after when_iso."""
    d = _dt(when_iso).date()
    cands = [s for s in snaps if datetime.date.fromisoformat(s["period_start"]) >= d]
    return min(cands, key=lambda s: s["period_start"], default=None)


def _match_rate(snaps, crawls):
    """How many uploaded Bing URLs matched a page in the latest crawl."""
    if not snaps or not crawls:
        return {"uploaded": 0, "matched": 0}
    uploaded = set().union(*[set(s["counts"]) for s in snaps])
    crawled = {canon_key(p["url"]) for p in (crawls[-1].get("pages") or [])}
    return {"uploaded": len(uploaded), "matched": len(uploaded & crawled)}


def compute_site_proof(ctx):
    """Pick the render tier and build the summary for one site.
    Tier order: empty (no snapshot) -> tier_a (a confirmed fix with a whole uploaded
    period on each side and a showable comparison) -> tier_b (>=2 crawls and >=2
    snapshots: first vs latest, version-change flagged) -> cold_start (latest crawl joined to the newest
    snapshot). Confirmed fixes whose comparison is not showable yet are counted in
    waiting.awaiting_period so the render can still emit the waiting telemetry."""
    snaps = ctx.get("snapshots") or []
    crawls = ctx.get("crawls") or []
    base = {"labels": dict(LABELS), "source": "bing",
            "match_rate": _match_rate(snaps, crawls),
            "waiting": {"provisional_count": len(ctx.get("provisional_fixes") or []), "awaiting_period": 0},
            "regressions": ctx.get("confirmed_regressions") or []}
    base["events"] = []        # tier-A events; set below once computed, so every tier carries the key
    if not snaps:
        return {"tier": "empty", "summary": base}
    # Tier A: a confirmed fix with a whole period before prev_crawl_at and after detected_at.
    confirmed = ctx.get("confirmed_fixes") or []
    fixed_urls = sorted({f["url"] for f in confirmed})
    events = []
    if fixed_urls:
        f0 = confirmed[0]
        before = _period_before(snaps, f0["prev_crawl_at"])
        after = _period_after(snaps, f0["detected_at"])
        if before and after:
            touched = set(fixed_urls) | {r["url"] for r in base["regressions"]}
            crawled_urls = {canon_key(p["url"]) for p in (crawls[-1].get("pages") or [])} if crawls else set()
            # untouched: fetched in both crawls, no fix/regression
            first_urls = {canon_key(p["url"]) for p in (crawls[0].get("pages") or [])} if crawls else set()
            untouched = sorted((crawled_urls & first_urls) - touched)
            cmp = tier_a_compare(before, after, fixed_urls, untouched)
            events.append({"fix_date": f0["detected_at"], "pages": len(fixed_urls),
                           "checks": sorted({c for f in confirmed for c in f["checks"]}),
                           "compare": cmp})
            base["events"] = events     # carried in every tier, so a hidden comparison's reason still reaches the summary
            if cmp["shown"]:
                return {"tier": "tier_a", "summary": base}
        # Confirmed fixes exist but tier A is not showable yet (period incomplete or
        # below a floor): count them as awaiting.
        base["waiting"]["awaiting_period"] = len(fixed_urls)
    # Tier B: first vs latest crawl, same scoring version or labelled. Needs two
    # snapshots too: with one, first vs latest is the snapshot against itself (a
    # vacuous delta), so fall through to cold_start.
    if len(crawls) >= 2 and len(snaps) >= 2:
        first, latest = crawls[0], crawls[-1]
        version_changed = first.get("scoring_version") != latest.get("scoring_version")
        common = set(first.get("checks_evaluated") or []) & set(latest.get("checks_evaluated") or [])
        s_first, s_last = snaps[0], snaps[-1]
        tb = {"label": "overall change since your first audit",
              "version_changed": version_changed,
              "checks_compared": sorted(common),
              "citations_first": sum(s_first["counts"].values()),
              "citations_latest": sum(s_last["counts"].values()),
              "period_first": {"start": s_first["period_start"], "end": s_first["period_end"]},
              "period_latest": {"start": s_last["period_start"], "end": s_last["period_end"]}}
        return {"tier": "tier_b", "summary": {**base, "tier_b": tb}}
    # Cold start: correlate the latest crawl with the newest snapshot. ctx pages are
    # {url, score} stamps of fetched pages, so treat a missing status as 200 (the
    # join skips non-200 pages).
    latest_snap = snaps[-1]
    pages = [{**p, "status": p.get("status", 200)} for p in (crawls[-1].get("pages") or [])] if crawls else []
    corr = correlate_data(pages, cites=latest_snap["counts"]) if crawls else {}
    return {"tier": "cold_start", "summary": {**base, "cold_start": corr,
            "period": {"start": latest_snap["period_start"], "end": latest_snap["period_end"]}}}
