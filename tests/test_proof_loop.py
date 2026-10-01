# Unit tests for the Rubric proof loop (engine side).
from aiseo_audit import canon_url, canon_key, parse_cites, correlate_data, calibrate_data

def test_canon_key_normalises_scheme_www_slash_host_case():
    assert canon_key("https://www.Example.com/Foo/") == canon_key("http://example.com/Foo")
    # path case preserved (servers may be case-sensitive)
    assert canon_key("https://example.com/Foo") != canon_key("https://example.com/foo")

def test_canon_key_strips_tracking_but_keeps_meaningful_query():
    assert canon_key("https://example.com/p?utm_source=x&id=9") == canon_key("https://example.com/p?id=9")
    assert canon_key("https://example.com/p?id=9") != canon_key("https://example.com/p?id=8")

def test_canon_url_returns_key_and_display():
    key, display = canon_url("  https://www.example.com/a/  ")
    assert display == "https://www.example.com/a/"
    assert key == canon_key("https://example.com/a")

def test_canon_key_bare_root():
    assert canon_key("https://example.com/") == canon_key("https://example.com")

def test_parse_cites_sums_rows_that_collapse_to_one_key():
    text = (
        "https://example.com/a,5\n"
        "http://www.example.com/a/,7\n"
        "https://example.com/a?utm_source=x,3\n"
    )
    cites = parse_cites(text)
    assert cites == {canon_key("https://example.com/a"): 15.0}

def test_parse_cites_keys_are_canonical():
    cites = parse_cites("http://www.example.com/b/,4\n")
    assert list(cites) == [canon_key("https://example.com/b")]
    assert cites[canon_key("https://example.com/b")] == 4.0

def test_correlate_data_joins_across_scheme_www_and_trailing_slash():
    cites = parse_cites("https://example.com/p,9\n")
    pages = [{"url": "http://www.example.com/p/", "status": 200, "type": "blog", "score": 80}]
    out = correlate_data(pages, cites)
    assert out["joined"] == 1
    assert out["top_cited"][0]["url"] == "http://www.example.com/p/"
    assert out["top_cited"][0]["citations"] == 9.0

def test_calibrate_data_matches_pages_across_scheme_www_and_trailing_slash():
    cites = parse_cites("https://example.com/q,2\n")
    d = {"pages": [{"url": "http://www.example.com/q/", "score": 70},
                   {"url": "https://example.com/other", "score": 60}]}
    out = calibrate_data(d, cites)
    # fewer than 8 matches returns the error dict, which still reports how many pages joined
    assert out["matched"] == 1


# --- Task 4: Bing per-URL export parser -------------------------------------
import pathlib

import pytest

import proof_loop

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def test_parse_bing_export_header_aware_and_sums_duplicates():
    csv_text = (
        "﻿Page,Clicks,Citations,Citation Share\n"
        "https://www.example.com/a/,10,5,2%\n"
        "https://example.com/a,3,2,1%\n"   # same page after canon -> sums to 7
        "https://example.com/b,0,4,1%\n"
    )
    out = proof_loop.parse_bing_export(csv_text)
    assert out["counts"][canon_key("https://example.com/a")] == 7
    assert out["counts"][canon_key("https://example.com/b")] == 4
    assert out["rows"] == 3

def test_parse_bing_export_tolerates_url_and_count_header_variants():
    csv_text = "URL,citations\nhttps://example.com/x,9\n"
    out = proof_loop.parse_bing_export(csv_text)
    assert out["counts"][canon_key("https://example.com/x")] == 9

def test_parse_bing_export_missing_citations_column_raises_clearly():
    with pytest.raises(ValueError) as e:
        proof_loop.parse_bing_export("Page,Clicks\nhttps://example.com/a,1\n")
    assert "citation" in str(e.value).lower()

def test_parse_bing_export_real_fixture_end_to_end():
    # Real per-URL Bing AI Performance export: quoted "Page","Citations" header + utf-8 BOM.
    text = (FIXTURES / "bing_ai_page_stats.csv").read_text(encoding="utf-8-sig")
    out = proof_loop.parse_bing_export(text)
    assert len(out["counts"]) >= 40
    top = canon_key("https://www.gogochimp.com/blog/best-ab-testing-tools-2026")
    assert out["counts"][top] == 13972.0

def test_parse_period_reads_iso_range_from_filename_or_preamble_else_none():
    import datetime
    d = datetime.date
    assert proof_loop.parse_period("Page,Citations\n", "ai_2026-08-01_2026-08-28.csv") == (d(2026, 8, 1), d(2026, 8, 28))
    assert proof_loop.parse_period("Period: 2026-09-01 to 2026-09-28\nPage,Citations\n") == (d(2026, 9, 1), d(2026, 9, 28))
    assert proof_loop.parse_period("Page,Citations\nhttps://example.com/a,1\n", "export.csv") == (None, None)


# --- Task 5: diff_crawls (fixes and regressions, like-with-like) -------------
def _stamp(ver, evaluated, fails, at):
    return {"scoring_version": ver, "checks_evaluated": evaluated, "page_fails": fails, "crawled_at": at}

def test_diff_detects_fix_and_regression_like_with_like():
    prev = _stamp("v1", ["a", "b", "c"], {"example.com/p": ["a", "b"], "example.com/q": []}, "2026-09-01T00:00:00Z")
    cur  = _stamp("v1", ["a", "b", "c"], {"example.com/p": ["b"],      "example.com/q": ["a"]}, "2026-09-08T00:00:00Z")
    out = proof_loop.diff_crawls(prev, cur)
    fixes = {f["url"]: f for f in out["fixes"]}
    regs = {r["url"]: r for r in out["regressions"]}
    assert fixes["example.com/p"]["checks"] == ["a"]          # a: fail->pass
    assert regs["example.com/q"]["checks"] == ["a"]           # a: pass->fail
    assert fixes["example.com/p"]["prev_crawl_at"] == "2026-09-01T00:00:00Z"
    assert fixes["example.com/p"]["detected_at"] == "2026-09-08T00:00:00Z"

def test_diff_ignores_checks_not_in_both_versions():
    prev = _stamp("v1", ["a"], {"example.com/p": ["a"]}, "2026-09-01T00:00:00Z")
    cur  = _stamp("v2", ["a", "d"], {"example.com/p": ["d"]}, "2026-09-08T00:00:00Z")
    out = proof_loop.diff_crawls(prev, cur)
    # a went fail->pass and counts; d is new this version -> never a fix
    assert out["fixes"][0]["checks"] == ["a"]

def test_diff_ignores_pages_not_in_both_crawls():
    prev = _stamp("v1", ["a"], {"example.com/p": ["a"]}, "2026-09-01T00:00:00Z")
    cur  = _stamp("v1", ["a"], {"example.com/other": []}, "2026-09-08T00:00:00Z")
    out = proof_loop.diff_crawls(prev, cur)
    assert out["fixes"] == [] and out["regressions"] == []
