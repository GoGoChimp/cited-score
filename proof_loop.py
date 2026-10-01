"""Rubric proof loop (engine side): Bing export parsing and the tunable defaults
shared by the later proof-loop stages."""
import csv
import datetime
import io
import re

from aiseo_audit import canon_key

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
