# Unit tests for the Rubric proof loop (engine side).
from aiseo_audit import canon_url, canon_key

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
