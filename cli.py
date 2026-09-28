"""The `rubric` console script (pipx). Wraps the existing engine without modifying it:
- rubric activate <key>   verify + store the Pro licence (the account API key)
- rubric status           show whether Pro is unlocked (key prefix only)
- rubric audit ...         gate on Pro, then hand the remaining flags to aiseo_audit.main()
The cloud worker and the existing exe do not use this file; the Pro gate lives only here."""
import sys
import licence

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
    print(f"Licence key {licence.key_prefix(d.get('key'))} — {state}.")
    return 0

def _cmd_audit(rest):
    licence.refresh()          # best-effort online re-verify (downgrades a cancelled/revoked account)
    licence.require_pro()      # exits(2) if not Pro
    import aiseo_audit
    sys.argv = ["aiseo_audit"] + rest
    aiseo_audit.main()
    return 0

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("Usage: rubric [activate <key> | status | audit --url <url> ...]")
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
    safe_cmd = licence.key_prefix(cmd) if cmd.startswith("cs_live_") else cmd
    print(f"Unknown command: {safe_cmd}. Try: activate, status, audit.")
    return 1

def mcp_main():
    """The `rubric-mcp` console script: gate on Pro, then run the local MCP over stdio unchanged."""
    licence.refresh()
    licence.require_pro()
    import mcp_server
    mcp_server.mcp.run()

if __name__ == "__main__":
    raise SystemExit(main())
