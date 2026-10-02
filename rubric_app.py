"""Frozen-app entry point (PyInstaller target). Dispatches on how the exe was launched, so one signed
binary is the tray launcher AND the thing an AI tool, the Connect grid, and a scheduled task point at:

  Rubric.exe                 launch the system-tray launcher (default; prompts for a key in-app if not Pro)
  Rubric.exe --mcp           run the local MCP server over stdio (what Claude Desktop / Cursor spawn); no window
  Rubric.exe --crawl <url>   run ONE headless audit to the reports folder, then exit (a scheduled re-crawl)
  Rubric.exe --watch-run     run all scheduled watches once (score-change alerts), then exit

No terminal and no Python install needed once frozen. The --mcp / --crawl / --watch-run paths gate on Pro
(fail-safe to a clear stderr message, never the key); the tray opens either way so a non-technical user can
unlock it in-app. Kept import-light at module scope so a test can import this file without launching anything."""
import sys


def _run_mcp():
    """`--mcp` -> serve the local MCP over stdio and block until the host disconnects. No GUI.
    Needs the `mcp` SDK bundled in Rubric.spec (collect_all('mcp') + the 'mcp_server' hidden import)."""
    import licence
    licence.refresh()          # downgrade a cancelled/revoked account before serving
    licence.require_pro()      # exits(2) with a stderr hint if not Pro; never prints the key
    try:
        import mcp_server
    except Exception as e:      # mcp SDK not bundled / import error - fail loudly on stderr, not silently
        sys.stderr.write(f"Rubric MCP server unavailable: {e}\n")
        raise SystemExit(1)
    mcp_server.mcp.run()        # stdio JSON-RPC loop


def _run_crawl():
    """`--crawl <url>` -> one headless audit to the per-user reports dir, then exit (a scheduled re-crawl,
    so the Score-over-time trend fills in). What app.schedule_crawl's task launches on the frozen exe."""
    import licence
    licence.refresh()
    licence.require_pro()
    argv = sys.argv
    url = argv[argv.index("--crawl") + 1] if "--crawl" in argv and argv.index("--crawl") + 1 < len(argv) else None
    if not url:
        sys.stderr.write("usage: Rubric.exe --crawl <url>\n")
        raise SystemExit(2)
    import os, urllib.parse
    import aiseo_audit as A
    from app import REPORTS, safe
    dom = urllib.parse.urlparse(url if url.startswith("http") else "https://" + url).netloc.replace("www.", "") or "site"
    out = os.path.join(REPORTS, safe(dom))
    A.run_audit(url, out=out)
    print("done:", out + ".html", flush=True)


def _run_watch():
    """`--watch-run` -> run every scheduled watch once (the Watches scheduler points here when frozen)."""
    import cli
    return cli._cmd_watch(["run"]) or 0


def main():
    argv = sys.argv[1:]
    if "--mcp" in argv:
        _run_mcp()
        return 0
    if "--crawl" in argv:
        _run_crawl()
        return 0
    if "--watch-run" in argv:
        return _run_watch()
    import licence, tray
    licence.refresh()          # best-effort online re-verify; the tray opens either way
    return tray.run() or 0


if __name__ == "__main__":
    raise SystemExit(main())
