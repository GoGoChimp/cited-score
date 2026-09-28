import base64
import aiseo_audit as A

AUTH = {"host": "staging.example.com", "basic": "u:p", "cookie": "sid=abc", "headers": {"X-Bypass": "1"}}

def test_auth_headers_on_target_host():
    h = A._auth_headers(AUTH, "https://staging.example.com/pricing")
    assert h["Authorization"] == "Basic " + base64.b64encode(b"u:p").decode()
    assert h["Cookie"] == "sid=abc"
    assert h["X-Bypass"] == "1"

def test_auth_headers_follow_subdomain():
    h = A._auth_headers(AUTH, "https://app.staging.example.com/x")
    assert "Authorization" in h  # subdomain of the audited host

def test_auth_not_sent_off_host():
    assert A._auth_headers(AUTH, "https://evil.example.net/") == {}
    assert A._auth_headers(AUTH, "https://accounts.google.com/") == {}
    assert A._auth_headers(AUTH, "https://example.com/") == {}  # parent host, not the audited host

def test_auth_none_is_empty():
    assert A._auth_headers(None, "https://staging.example.com/") == {}

def test_no_auth_headers_unchanged(monkeypatch):
    seen = {}
    class R:
        status = 200; headers = {}
        def read(self): return b"<html>ok</html>"
        def geturl(self): return "https://x.test/"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(A.urllib.request, "urlopen", lambda req, timeout=0: seen.setdefault("headers", dict(req.headers)) or R())
    monkeypatch.setattr(A, "_ssrf_on", lambda: False)
    A.fetch_raw("https://x.test/")
    assert "Authorization" not in seen["headers"] and "Cookie" not in seen["headers"]

def test_fetch_raw_attaches_auth_for_target(monkeypatch):
    seen = {}
    class R:
        status = 200; headers = {}
        def read(self): return b"<html>ok</html>"
        def geturl(self): return "https://staging.example.com/"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(A.urllib.request, "urlopen", lambda req, timeout=0: seen.setdefault("h", dict(req.headers)) or R())
    monkeypatch.setattr(A, "_ssrf_on", lambda: False)
    A.fetch_raw("https://staging.example.com/", auth=AUTH)
    keys = {k.lower() for k in seen["h"]}
    assert "authorization" in keys and "cookie" in keys
