"""The `rubric` console script (pipx). Wraps the existing engine without modifying it:
- rubric activate <key>   verify + store the Pro licence (the account API key)
- rubric status           show whether Pro is unlocked (key prefix only)
- rubric audit ...         gate on Pro, then hand the remaining flags to aiseo_audit.main()
The cloud worker and the existing exe do not use this file; the Pro gate lives only here."""
import sys, os, json, shutil
import licence

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
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                try: shutil.copy(path, path + ".rubric-backup")   # never clobber a user file blind
                except Exception: pass
            cfg.setdefault("mcpServers", {})[_MCP_KEY] = {"command": cmd, "args": []}
            with open(path, "w", encoding="utf-8") as f: json.dump(cfg, f, indent=2)
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

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("Usage: rubric [activate <key> | status | audit --url <url> ... | mcp install]")
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
    safe_cmd = licence.key_prefix(cmd) if cmd.startswith("cs_live_") else cmd
    print(f"Unknown command: {safe_cmd}. Try: activate, status, audit, mcp.")
    return 1

def mcp_main():
    """The `rubric-mcp` console script: gate on Pro, then run the local MCP over stdio unchanged."""
    licence.refresh()
    licence.require_pro()
    import mcp_server
    mcp_server.mcp.run()

if __name__ == "__main__":
    raise SystemExit(main())
