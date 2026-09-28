"""The `rubric` console script (pipx). Wraps the existing engine without modifying it:
- rubric activate <key>   verify + store the Pro licence (the account API key)
- rubric status           show whether Pro is unlocked (key prefix only)
- rubric audit ...         gate on Pro, then hand the remaining flags to aiseo_audit.main()
The cloud worker and the existing exe do not use this file; the Pro gate lives only here."""
import sys, os, json, shutil
import licence
import watch_store

_MCP_KEY = "rubric"

def _claude_config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Claude", "claude_desktop_config.json")

def _mcp_read(path):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}

def _cmd_mcp(rest):
    """Register / check / remove the local Rubric MCP in Claude Desktop's config. Backs up before writing,
    merges into any existing mcpServers, and never touches another server or writes a credential."""
    sub = (rest[0] if rest else "status").lower()
    path = _claude_config_path()
    if sub == "status":
        reg = _MCP_KEY in (_mcp_read(path).get("mcpServers") or {})
        print(f"Local Rubric MCP is {'registered' if reg else 'not registered'} in Claude Desktop ({path}).")
        return 0
    if sub == "install":
        cmd = shutil.which("rubric-mcp") or "rubric-mcp"
        cfg = _mcp_read(path)
        try:
            existed = os.path.exists(path)
            unparsed = existed and os.path.getsize(path) > 2 and not cfg   # a non-empty file that would not parse
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if existed:
                try: shutil.copy(path, path + ".rubric-backup")   # never clobber a user file blind
                except Exception: pass
            cfg.setdefault("mcpServers", {})[_MCP_KEY] = {"command": cmd, "args": []}
            with open(path, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2)
            if unparsed:
                print(f"Note: the existing Claude Desktop config could not be parsed; it was backed up to {path}.rubric-backup and replaced.")
            print(f"Registered the local Rubric MCP ({cmd}). Restart Claude Desktop to load it.")
            return 0
        except Exception as e:
            print(f"Could not write the Claude Desktop config: {str(e)[:160]}")
            return 1
    if sub == "uninstall":
        cfg = _mcp_read(path); servers = cfg.get("mcpServers") or {}
        if _MCP_KEY in servers:
            try: shutil.copy(path, path + ".rubric-backup")
            except Exception: pass
            del servers[_MCP_KEY]; cfg["mcpServers"] = servers
            with open(path, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2)
            print("Removed the local Rubric MCP. Restart Claude Desktop to unload it.")
        else:
            print("The local Rubric MCP was not registered.")
        return 0
    print("Usage: rubric mcp [install | status | uninstall]")
    return 1

def _cmd_activate(key):
    ok, msg = licence.activate(key)
    print(msg)
    return 0 if ok else 1

def _cmd_status():
    d = licence.load()
    if not d or not d.get("key"):
        print("Rubric desktop is not activated. Run: rubric activate <your cs_live_ key>")
        return 0
    state = "Pro (unlocked)" if licence.is_pro() else "not currently Pro (verify online or renew)"
    print(f"Licence key {licence.key_prefix(d.get('key'))}: {state}.")
    return 0

def _cmd_audit(rest):
    licence.refresh()          # best-effort online re-verify (downgrades a cancelled/revoked account)
    licence.require_pro()      # exits(2) if not Pro
    import aiseo_audit
    sys.argv = ["aiseo_audit"] + rest
    aiseo_audit.main()
    return 0

ALERT_THRESHOLD = 2

def _score_of(url):
    """Crawl a watched site and return its overall score, or None if the crawl could not score it."""
    import aiseo_audit
    try:
        d = aiseo_audit.run_audit(url, out=None, links=False)
        return d.get("overall")
    except Exception:
        return None

def _notify(title, message):
    """Best-effort desktop notification. The alert log is the reliable channel; this never fails the run."""
    import subprocess
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Write-Output ('{title}: {message}')"], timeout=8, capture_output=True)
    except Exception:
        pass

def _watch_install(every):
    """Register a Windows scheduled task that runs `rubric watch run` on the cadence. Best-effort; prints guidance."""
    import subprocess
    rubric = shutil.which("rubric") or "rubric"
    sc = "WEEKLY" if every == "weekly" else "DAILY"
    try:
        subprocess.run(["schtasks", "/Create", "/F", "/SC", sc, "/TN", "RubricWatch",
                        "/TR", f'"{rubric}" watch run', "/ST", "09:00"], check=True, capture_output=True, timeout=15)
        print(f"Scheduled 'rubric watch run' {every} at 09:00 (task RubricWatch). Change it in Task Scheduler.")
        return 0
    except Exception as e:
        print(f"Could not create the scheduled task automatically ({str(e)[:100]}). "
              f"Create one that runs: {rubric} watch run")
        return 1

def _cmd_watch(rest):
    import datetime
    sub = (rest[0] if rest else "list").lower()
    args = rest[1:]
    def _opt(name, default):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default
    if sub == "add":
        if not args: print("Usage: rubric watch add <url> [--every daily|weekly]"); return 1
        every = _opt("--every", "weekly"); watch_store.add(args[0], every); print(f"Watching {args[0]} ({every})."); return 0
    if sub == "remove":
        if not args: print("Usage: rubric watch remove <url>"); return 1
        print("Removed." if watch_store.remove(args[0]) else "Not watched."); return 0
    if sub == "list":
        ws = watch_store.list_watches()
        if not ws: print("No watched sites. Add one: rubric watch add <url>"); return 0
        for w in ws:
            ls = w.get("last_score"); print(f"{w['url']}  [{w.get('cadence')}]  last score: {ls if ls is not None else '-'}")
        return 0
    if sub == "alerts":
        al = watch_store.list_alerts()
        if not al: print("No score-change alerts."); return 0
        for a in al: print(f"{a.get('at')}  {a.get('url')}  {a.get('old')} -> {a.get('new')} ({(a.get('new') or 0)-(a.get('old') or 0):+d})")
        return 0
    if sub == "install":
        return _watch_install(_opt("--every", "weekly"))
    if sub == "run":
        licence.require_pro()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for w in watch_store.list_watches():
            url = w["url"]; new = _score_of(url)
            if new is None:
                print(f"{url}: crawl did not score (skipped)"); continue
            old = w.get("last_score")
            if old is not None and abs(new - old) >= ALERT_THRESHOLD:
                watch_store.record_alert({"url": url, "old": old, "new": new, "at": now})
                print(f"ALERT {url}: {old} -> {new} ({new-old:+d})")
                try: _notify("Rubric score change", f"{url}: {old} -> {new}")
                except Exception: pass
            else:
                print(f"{url}: {new}" + (" (baseline)" if old is None else " (no significant change)"))
            watch_store.update_score(url, new, now)
        return 0
    print("Usage: rubric watch [add <url> [--every daily|weekly] | list | remove <url> | run | alerts | install]")
    return 1

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("Usage: rubric [activate <key> | status | audit --url <url> ... | mcp install | watch add <url>]")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "activate":
        if not rest:
            print("Usage: rubric activate <your cs_live_ key>")
            return 1
        return _cmd_activate(rest[0])
    if cmd == "status":
        return _cmd_status()
    if cmd == "audit":
        return _cmd_audit(rest)
    if cmd == "mcp":
        return _cmd_mcp(rest)
    if cmd == "watch":
        return _cmd_watch(rest)
    safe_cmd = licence.key_prefix(cmd) if cmd.startswith("cs_live_") else cmd
    print(f"Unknown command: {safe_cmd}. Try: activate, status, audit, mcp, watch.")
    return 1

def mcp_main():
    """The `rubric-mcp` console script: gate on Pro, then run the local MCP over stdio unchanged."""
    licence.refresh()
    licence.require_pro()
    import mcp_server
    mcp_server.mcp.run()

if __name__ == "__main__":
    raise SystemExit(main())
