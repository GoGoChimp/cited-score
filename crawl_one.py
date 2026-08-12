import sys, aiseo_audit as A
url = sys.argv[1]
if not url.startswith("http"): url = "https://" + url
try:
    d = A.run_audit(url, out=None, max_pages=50)
    st = "blocked" if d.get("access_blocked") else "ok"
    p = d["pillars"]
    print(f"OK\t{d['overall']}\t{p['Known']}\t{p['Findable']}\t{p['Trusted']}\t{d['pages_crawled']}\t{st}")
except Exception as e:
    print(f"ERR\t{type(e).__name__}: {str(e)[:90]}")
