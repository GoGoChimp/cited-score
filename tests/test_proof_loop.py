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


def test_confirm_transitions_confirms_held_fix_cancels_flipback():
    provisional = [
        {"id": "1", "url": "example.com/p", "kind": "fixed", "checks": ["a"]},
        {"id": "2", "url": "example.com/q", "kind": "fixed", "checks": ["b"]},
    ]
    cur_fails = {"example.com/p": [], "example.com/q": ["b"]}  # p held, q flipped back
    out = proof_loop.confirm_transitions(provisional, cur_fails, common_checks={"a", "b"})
    assert out["confirm"] == ["1"] and out["cancel"] == ["2"]


def test_confirm_transitions_regressed_confirms_when_still_failing():
    provisional = [
        {"id": "3", "url": "example.com/p", "kind": "regressed", "checks": ["a"]},
        {"id": "4", "url": "example.com/q", "kind": "regressed", "checks": ["b"]},
    ]
    cur_fails = {"example.com/p": ["a"], "example.com/q": []}  # p still failing, q recovered
    out = proof_loop.confirm_transitions(provisional, cur_fails, common_checks={"a", "b"})
    assert out["confirm"] == ["3"] and out["cancel"] == ["4"]


def test_cluster_merges_same_page_within_window():
    events = [
        {"url": "example.com/p", "checks": ["a"], "detected_at": "2026-09-01T00:00:00Z"},
        {"url": "example.com/p", "checks": ["b"], "detected_at": "2026-09-10T00:00:00Z"},  # within 28d
        {"url": "example.com/p", "checks": ["c"], "detected_at": "2026-11-01T00:00:00Z"},  # new cluster
    ]
    out = proof_loop.cluster_fixes(events)
    assert len(out) == 2
    assert out[0]["detected_at"] == "2026-09-01T00:00:00Z"
    assert sorted(out[0]["checks"]) == ["a", "b"]


def _period(s, e, counts):
    return {"period_start": s, "period_end": e, "counts": counts}


def test_tier_a_per_day_rate_and_control():
    fixed = [f"example.com/p{i}" for i in range(8)]
    unt = [f"example.com/u{i}" for i in range(8)]
    before = _period("2026-08-01", "2026-08-28", {u: 5 for u in fixed + unt})   # 28 days
    after = _period("2026-09-01", "2026-09-28", {**{u: 15 for u in fixed}, **{u: 5 for u in unt}})
    out = proof_loop.tier_a_compare(before, after, fixed, unt)
    assert out["shown"] is True and out["caveat"] is None
    assert out["fixed"]["before_total"] == 40 and out["fixed"]["after_total"] == 120
    assert round(out["fixed"]["after_per_day"], 2) == round(120 / 28, 2)
    assert out["untouched"]["after_total"] == 40      # control stayed flat


def test_tier_a_hidden_when_period_too_short():
    fixed = [f"example.com/p{i}" for i in range(8)]
    before = _period("2026-08-01", "2026-08-07", {u: 50 for u in fixed})  # 7 days
    after = _period("2026-09-01", "2026-09-28", {u: 99 for u in fixed})
    out = proof_loop.tier_a_compare(before, after, fixed, [])
    assert out["shown"] is False and "14" in (out["reason"] or "")


def test_tier_a_hidden_when_citations_below_floor_in_a_period():
    fixed = [f"example.com/p{i}" for i in range(8)]
    before = _period("2026-08-01", "2026-08-28", {u: 0 for u in fixed})     # 0 citations before
    after = _period("2026-09-01", "2026-09-28", {u: 10 for u in fixed})
    out = proof_loop.tier_a_compare(before, after, fixed, [])
    assert out["shown"] is False and "citation" in (out["reason"] or "").lower()


def test_tier_a_caveat_on_uneven_periods():
    fixed = [f"example.com/p{i}" for i in range(8)]
    before = _period("2026-08-01", "2026-08-15", {u: 50 for u in fixed})    # 14 days
    after = _period("2026-09-01", "2026-10-15", {u: 300 for u in fixed})    # 44 days (> 2x)
    out = proof_loop.tier_a_compare(before, after, fixed, [])
    assert out["caveat"] and "care" in out["caveat"]


def test_tier_a_floor_applies_to_after_period_and_control_flags_insufficient():
    fixed = [f"example.com/p{i}" for i in range(8)]
    # after-period fixed citations (8 x 1 = 8) below the floor even though before is healthy
    before = _period("2026-08-01", "2026-08-28", {u: 50 for u in fixed})
    after = _period("2026-09-01", "2026-09-28", {u: 1 for u in fixed})
    out = proof_loop.tier_a_compare(before, after, fixed, [])
    assert out["shown"] is False and "citation" in (out["reason"] or "").lower()
    # healthy fixed cohort but an empty control -> shown, control flagged insufficient
    after_ok = _period("2026-09-01", "2026-09-28", {u: 15 for u in fixed})
    out = proof_loop.tier_a_compare(before, after_ok, fixed, [])
    assert out["shown"] is True and out["untouched"]["insufficient"] is True


def test_tier_a_hidden_when_fixed_cohort_below_min_pages():
    fixed = [f"example.com/p{i}" for i in range(7)]            # 7 < COHORT_MIN_PAGES
    before = _period("2026-08-01", "2026-08-28", {u: 20 for u in fixed})
    after = _period("2026-09-01", "2026-09-28", {u: 20 for u in fixed})
    out = proof_loop.tier_a_compare(before, after, fixed, [])
    assert out["shown"] is False
    assert "not enough fixed pages" in (out["reason"] or "")


def test_tier_a_caveat_on_midpoint_gap_with_equal_length_periods():
    fixed = [f"example.com/p{i}" for i in range(8)]
    before = _period("2026-01-01", "2026-01-28", {u: 10 for u in fixed})   # 28 days
    after = _period("2026-06-01", "2026-06-28", {u: 10 for u in fixed})    # 28 days, midpoints ~151 days apart
    out = proof_loop.tier_a_compare(before, after, fixed, [])
    assert out["fixed"]["days_before"] == out["fixed"]["days_after"]       # length-ratio branch not in play
    assert out["shown"] is True
    assert out["caveat"] and "care" in out["caveat"]


def test_tier_a_untouched_control_below_period_floor_is_insufficient():
    fixed = [f"example.com/p{i}" for i in range(8)]
    unt = [f"example.com/u{i}" for i in range(8)]
    before = _period("2026-08-01", "2026-08-28", {**{u: 10 for u in fixed}, **{u: 5 for u in unt}})
    after = _period("2026-09-01", "2026-09-28", {**{u: 10 for u in fixed}, **{u: 3 for u in unt}})  # control after = 24 < 30
    out = proof_loop.tier_a_compare(before, after, fixed, unt)
    assert out["shown"] is True
    assert out["untouched"]["insufficient"] is True
    assert out["untouched"]["before_total"] == 40 and out["untouched"]["after_total"] == 24


def test_tier_a_hidden_when_after_period_too_short():
    fixed = [f"example.com/p{i}" for i in range(8)]
    before = _period("2026-08-01", "2026-08-28", {u: 50 for u in fixed})   # 28 days, fine
    after = _period("2026-09-01", "2026-09-07", {u: 50 for u in fixed})    # 7 days, too short
    out = proof_loop.tier_a_compare(before, after, fixed, [])
    assert out["shown"] is False and "14" in (out["reason"] or "")


def test_tier_a_duplicate_fixed_urls_do_not_defeat_cohort_gate():
    one_page = ["https://example.com/p0"] * 8                  # one real page repeated
    key = canon_key(one_page[0])
    before = _period("2026-08-01", "2026-08-28", {key: 500})
    after = _period("2026-09-01", "2026-09-28", {key: 500})
    out = proof_loop.tier_a_compare(before, after, [key] * 8, [])
    assert out["shown"] is False
    assert "not enough fixed pages" in (out["reason"] or "")


# --- compute_site_proof (tier selection) ---

def test_cold_start_when_snapshot_but_no_confirmed_fix():
    ctx = {
        "crawls": [{"crawled_at": "2026-09-01T00:00:00Z", "scoring_version": "v1",
                    "page_fails": {"example.com/a": []}, "checks_evaluated": ["x"],
                    "pages": [{"url": "https://example.com/a", "score": 80}]}],
        "snapshots": [{"period_start": "2026-09-01", "period_end": "2026-09-28",
                       "counts": {"example.com/a": 12}}],
        "confirmed_fixes": [], "confirmed_regressions": [], "provisional_fixes": [],
    }
    out = proof_loop.compute_site_proof(ctx)
    assert out["tier"] == "cold_start"
    assert out["summary"]["labels"]["source"] == "Bing/Copilot data only"
    assert out["summary"]["match_rate"]["matched"] == 1


def test_waiting_block_when_provisional_fix_pending():
    ctx = {"crawls": [{"crawled_at": "2026-09-08T00:00:00Z", "scoring_version": "v1",
                       "page_fails": {}, "checks_evaluated": ["x"], "pages": []}],
           "snapshots": [{"period_start": "2026-09-01", "period_end": "2026-09-28", "counts": {}}],
           "confirmed_fixes": [], "confirmed_regressions": [],
           "provisional_fixes": [{"url": "example.com/a", "detected_at": "2026-09-08T00:00:00Z"}]}
    out = proof_loop.compute_site_proof(ctx)
    assert out["summary"]["waiting"]["provisional_count"] == 1


def test_tier_a_when_confirmed_fix_has_before_and_after_periods():
    fixed = [f"example.com/p{i}" for i in range(8)]
    ctx = {
        "crawls": [
            {"crawled_at": "2026-08-30T00:00:00Z", "scoring_version": "v1", "page_fails": {}, "checks_evaluated": ["x"], "pages": []},
            {"crawled_at": "2026-09-05T00:00:00Z", "scoring_version": "v1", "page_fails": {}, "checks_evaluated": ["x"], "pages": []},
        ],
        "snapshots": [
            {"period_start": "2026-08-01", "period_end": "2026-08-28", "counts": {u: 5 for u in fixed}},
            {"period_start": "2026-09-10", "period_end": "2026-10-07", "counts": {u: 15 for u in fixed}},
        ],
        "confirmed_fixes": [{"url": u, "checks": ["x"], "prev_crawl_at": "2026-08-30T00:00:00Z",
                             "detected_at": "2026-09-05T00:00:00Z"} for u in fixed],
        "confirmed_regressions": [], "provisional_fixes": [],
    }
    out = proof_loop.compute_site_proof(ctx)
    assert out["tier"] == "tier_a"
    assert out["summary"]["events"][0]["compare"]["shown"] is True


def test_empty_tier_when_no_snapshot():
    out = proof_loop.compute_site_proof({"crawls": [], "snapshots": [], "confirmed_fixes": [],
                                         "confirmed_regressions": [], "provisional_fixes": []})
    assert out["tier"] == "empty"
    assert out["summary"]["source"] == "bing"
    assert out["summary"]["match_rate"] == {"uploaded": 0, "matched": 0}


def test_cold_start_joins_latest_crawl_pages_that_carry_no_status():
    # ctx pages are {url, score} only; the cold-start join must still see them.
    ctx = {"crawls": [{"crawled_at": "2026-09-01T00:00:00Z", "scoring_version": "v1",
                       "page_fails": {}, "checks_evaluated": ["x"],
                       "pages": [{"url": "https://example.com/a", "score": 80},
                                 {"url": "https://example.com/b", "score": 60}]}],
           "snapshots": [{"period_start": "2026-09-01", "period_end": "2026-09-28",
                          "counts": {"example.com/a": 12}}],
           "confirmed_fixes": [], "confirmed_regressions": [], "provisional_fixes": []}
    out = proof_loop.compute_site_proof(ctx)
    assert out["tier"] == "cold_start"
    assert out["summary"]["cold_start"]["joined"] == 2
    assert out["summary"]["period"] == {"start": "2026-09-01", "end": "2026-09-28"}


def test_confirmed_fix_without_a_complete_period_counts_as_awaiting_and_falls_to_tier_b():
    ctx = {
        "crawls": [
            {"crawled_at": "2026-08-30T00:00:00Z", "scoring_version": "v1", "page_fails": {}, "checks_evaluated": ["x", "y"], "pages": []},
            {"crawled_at": "2026-09-05T00:00:00Z", "scoring_version": "v2", "page_fails": {}, "checks_evaluated": ["x"], "pages": []},
        ],
        # two snapshots (so tier B has something to compare) but none starts after the fix yet
        "snapshots": [{"period_start": "2026-07-01", "period_end": "2026-07-28", "counts": {"example.com/a": 4}},
                      {"period_start": "2026-08-01", "period_end": "2026-08-28", "counts": {"example.com/a": 9}}],
        "confirmed_fixes": [{"url": "example.com/a", "checks": ["x"], "prev_crawl_at": "2026-08-30T00:00:00Z",
                             "detected_at": "2026-09-05T00:00:00Z"}],
        "confirmed_regressions": [{"url": "example.com/b", "checks": ["x"], "detected_at": "2026-09-05T00:00:00Z"}],
        "provisional_fixes": [],
    }
    out = proof_loop.compute_site_proof(ctx)
    assert out["tier"] == "tier_b"
    assert out["summary"]["waiting"]["awaiting_period"] == 1
    assert out["summary"]["tier_b"]["version_changed"] is True
    assert out["summary"]["tier_b"]["checks_compared"] == ["x"]
    assert out["summary"]["regressions"] == ctx["confirmed_regressions"]


def test_tier_a_regression_url_is_excluded_from_the_untouched_control():
    fixed = [f"example.com/p{i}" for i in range(8)]
    pages = [{"url": f"https://example.com/{n}", "score": 50} for n in [f"p{i}" for i in range(8)] + ["reg", "calm"]]
    crawl = {"scoring_version": "v1", "page_fails": {}, "checks_evaluated": ["x"], "pages": pages}
    ctx = {
        "crawls": [{**crawl, "crawled_at": "2026-08-30T00:00:00Z"}, {**crawl, "crawled_at": "2026-09-05T00:00:00Z"}],
        "snapshots": [
            {"period_start": "2026-08-01", "period_end": "2026-08-28", "counts": {**{u: 5 for u in fixed}, "example.com/reg": 40, "example.com/calm": 40}},
            {"period_start": "2026-09-10", "period_end": "2026-10-07", "counts": {**{u: 15 for u in fixed}, "example.com/reg": 40, "example.com/calm": 40}},
        ],
        "confirmed_fixes": [{"url": u, "checks": ["x"], "prev_crawl_at": "2026-08-30T00:00:00Z",
                             "detected_at": "2026-09-05T00:00:00Z"} for u in fixed],
        "confirmed_regressions": [{"url": "example.com/reg", "checks": ["x"], "detected_at": "2026-09-05T00:00:00Z"}],
        "provisional_fixes": [],
    }
    out = proof_loop.compute_site_proof(ctx)
    assert out["tier"] == "tier_a"
    control = out["summary"]["events"][0]["compare"]["untouched"]
    assert control["before_total"] == 40 and control["after_total"] == 40   # "calm" only, "reg" excluded


def test_two_crawls_but_one_snapshot_is_cold_start_not_tier_b():
    # tier B compares first vs latest snapshot; with a single snapshot that is a
    # self-comparison, so it must fall through to cold_start.
    ctx = {
        "crawls": [
            {"crawled_at": "2026-08-30T00:00:00Z", "scoring_version": "v1", "page_fails": {}, "checks_evaluated": ["x"],
             "pages": [{"url": "https://example.com/a", "score": 70}]},
            {"crawled_at": "2026-09-05T00:00:00Z", "scoring_version": "v1", "page_fails": {}, "checks_evaluated": ["x"],
             "pages": [{"url": "https://example.com/a", "score": 80}]},
        ],
        "snapshots": [{"period_start": "2026-09-01", "period_end": "2026-09-28", "counts": {"example.com/a": 12}}],
        "confirmed_fixes": [], "confirmed_regressions": [], "provisional_fixes": [],
    }
    out = proof_loop.compute_site_proof(ctx)
    assert out["tier"] == "cold_start"
    assert "tier_b" not in out["summary"]
    assert out["summary"]["cold_start"]["joined"] == 1
