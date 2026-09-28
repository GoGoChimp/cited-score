import base64, json
import pytest
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

def _args(**kw):
    class NS: pass
    a = NS()
    for k in ("url", "basic", "cookie", "auth_header", "auth_file"): setattr(a, k, kw.get(k))
    return a

def test_build_auth_none_when_no_flags():
    assert A._build_auth(_args(url="https://staging.example.com")) is None

def test_build_auth_from_flags():
    au = A._build_auth(_args(url="https://staging.example.com/x", basic="u:p", cookie="sid=1", auth_header=["X-A: 1"]))
    assert au["host"] == "staging.example.com" and au["basic"] == "u:p" and au["cookie"] == "sid=1"
    assert au["headers"]["X-A"] == "1"

def test_build_auth_malformed_basic_exits():
    with pytest.raises(SystemExit):
        A._build_auth(_args(url="https://staging.example.com", basic="nocolon"))

def test_build_auth_file(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"cookie": "sid=filecookie"}), encoding="utf-8")
    au = A._build_auth(_args(url="https://staging.example.com", auth_file=str(p)))
    assert au["cookie"] == "sid=filecookie" and au["host"] == "staging.example.com"

def test_credentials_absent_from_report(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_ssrf_on", lambda: False)
    class R:
        status = 200; headers = {}
        def read(self): return b"<html><head><title>Staging</title></head><body><h1>Hi</h1><p>content here for the page.</p></body></html>"
        def geturl(self): return "https://staging.example.com/"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(A.urllib.request, "urlopen", lambda req, timeout=0: R())
    out = str(tmp_path / "rep")
    A.run_audit("https://staging.example.com/", out=out, max_pages=1, links=False,
                auth={"host": "staging.example.com", "basic": "secretuser:secretpass", "cookie": "sid=TOPSECRET"})
    for ext in (".html", ".json", ".csv"):
        try: body = open(out + ext, encoding="utf-8").read()
        except FileNotFoundError: continue
        assert "secretpass" not in body and "TOPSECRET" not in body
