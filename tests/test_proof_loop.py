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
