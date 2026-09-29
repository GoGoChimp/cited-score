"""Multi-tool MCP connector registry for the Rubric launcher's Connect grid.

Single source of truth for wiring the local Rubric MCP into each AI tool. It reuses the proven
backup-merge-write discipline from cli._cmd_mcp: back up before writing, merge into any existing
mcpServers block, never clobber another server, and never write a credential. It does NOT rename
the 'rubric' server key (high blast radius). ChatGPT is deliberately absent: its connectors expect
an internet-reachable server, not a local stdio MCP, so the grid shows it as a grey 'coming soon'
tile rather than pretending a local connection would work.

Tool kinds:
  json    - auto-write an mcpServers.rubric block to the tool's JSON config (Claude Desktop, Cursor).
  cli     - shell the tool's own registration command (Claude Code: `claude mcp add`).
  snippet - the tool needs a manual paste (Codex TOML, Cline VS Code settings); we return the exact
            block to add. These are the 'verify then ship' tools; snippet keeps us honest until then.
"""
import os, json, shutil, subprocess, time

SERVER_KEY = "rubric"


def _server_command():
    return shutil.which("rubric-mcp") or "rubric-mcp"


def _appdata():
    return os.environ.get("APPDATA") or os.path.expanduser("~")


def _claude_desktop_path():
    return os.path.join(_appdata(), "Claude", "claude_desktop_config.json")


def _cursor_path():
    return os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json")


def _cline_path():
    return os.path.join(_appdata(), "Code", "User", "globalStorage",
                        "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json")


def _codex_path():
    return os.path.join(os.path.expanduser("~"), ".codex", "config.toml")


TOOLS = {
    "claude-desktop": {"name": "Claude Desktop", "kind": "json", "path": _claude_desktop_path},
    "claude-code":    {"name": "Claude Code",    "kind": "cli",  "path": None},
    "cursor":         {"name": "Cursor",         "kind": "json", "path": _cursor_path},
    "codex":          {"name": "Codex CLI",      "kind": "snippet", "path": _codex_path},
    "cline":          {"name": "Cline",          "kind": "snippet", "path": _cline_path},
}


def config_path(tool_id):
    """The tool's config file path, or None for a tool that has no single JSON path (cli). Patched in tests."""
    t = TOOLS.get(tool_id)
    if not t or not t.get("path"):
        return None
    return t["path"]()


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _block():
    return {"command": _server_command(), "args": []}


def _install_json(path):
    existed = os.path.exists(path)
    cfg = _read_json(path)
    unparsed = existed and os.path.getsize(path) > 2 and not cfg   # a non-empty file that would not parse
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    backup = None
    if existed:
        # A corrupt config is REPLACED wholesale, so back it up to a UNIQUE name so a second click
        # never overwrites an earlier good backup. A normal merge keeps the stable .rubric-backup.
        backup = path + (".rubric-backup-" + time.strftime("%Y%m%d%H%M%S") if unparsed else ".rubric-backup")
        try:
            shutil.copy(path, backup)                              # never clobber a user file blind
        except Exception:
            backup = None
    cfg.setdefault("mcpServers", {})[SERVER_KEY] = _block()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if unparsed:
        where = os.path.basename(backup) if backup else "(the old content could not be backed up)"
        return {"ok": True, "replaced": True,
                "message": "The existing config could not be parsed and was replaced. Your previous "
                           "config was saved to " + where + ". Restart the app to load Rubric."}
    return {"ok": True, "message": "Connected. Restart the app to load Rubric."}


def _uninstall_json(path):
    cfg = _read_json(path)
    servers = cfg.get("mcpServers") or {}
    if SERVER_KEY in servers:
        try:
            shutil.copy(path, path + ".rubric-backup")
        except Exception:
            pass
        del servers[SERVER_KEY]
        cfg["mcpServers"] = servers
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return {"ok": True, "message": "Disconnected. Restart the app to unload Rubric."}
    return {"ok": True, "message": "Rubric was not connected."}


def _install_cli(tool_id):
    if not shutil.which("claude"):
        return {"ok": False, "message": "Claude Code CLI not found on PATH. Install it, then run: "
                f"claude mcp add {SERVER_KEY} -- {_server_command()}"}
    claude = shutil.which("claude") or "claude"
    try:
        subprocess.run([claude, "mcp", "add", SERVER_KEY, "--", _server_command()],
                       check=True, capture_output=True, timeout=20)
        return {"ok": True, "message": "Connected to Claude Code. Restart it to load Rubric."}
    except Exception as e:
        return {"ok": False, "message": f"Could not add to Claude Code: {str(e)[:160]}"}


def _uninstall_cli(tool_id):
    if not shutil.which("claude"):
        return {"ok": True, "message": f"Run: claude mcp remove {SERVER_KEY}"}
    claude = shutil.which("claude") or "claude"
    try:
        subprocess.run([claude, "mcp", "remove", SERVER_KEY], check=True, capture_output=True, timeout=20)
        return {"ok": True, "message": "Disconnected from Claude Code."}
    except Exception as e:
        return {"ok": False, "message": f"Could not remove from Claude Code: {str(e)[:160]}"}


def snippet(tool_id):
    """The exact config block to paste for a manual (snippet) tool."""
    if tool_id == "codex":
        return ("[mcp_servers.rubric]\n"
                f'command = "{_server_command()}"\n'
                "args = []\n")
    if tool_id == "cline":
        return json.dumps({SERVER_KEY: {"command": _server_command(), "args": [],
                                        "disabled": False, "autoApprove": []}}, indent=2)
    return ""


def install(tool_id):
    t = TOOLS.get(tool_id)
    if not t:
        return {"ok": False, "message": f"Unknown tool: {tool_id}"}
    kind = t["kind"]
    if kind == "json":
        p = config_path(tool_id)
        if not p:
            return {"ok": False, "message": "No config path for this tool."}
        try:
            return _install_json(p)
        except Exception as e:
            return {"ok": False, "message": f"Could not write the config: {str(e)[:160]}"}
    if kind == "cli":
        return _install_cli(tool_id)
    if kind == "snippet":
        return {"ok": True, "manual": True, "snippet": snippet(tool_id),
                "message": "Add this block to the tool's config, then restart it."}
    return {"ok": False, "message": "Unsupported tool kind."}


def uninstall(tool_id):
    t = TOOLS.get(tool_id)
    if not t:
        return {"ok": False, "message": f"Unknown tool: {tool_id}"}
    kind = t["kind"]
    if kind == "json":
        p = config_path(tool_id)
        if not p:
            return {"ok": False, "message": "No config path for this tool."}
        try:
            return _uninstall_json(p)
        except Exception as e:
            return {"ok": False, "message": f"Could not write the config: {str(e)[:160]}"}
    if kind == "cli":
        return _uninstall_cli(tool_id)
    if kind == "snippet":
        return {"ok": True, "manual": True, "message": f"Remove the [mcp_servers.{SERVER_KEY}] block from the tool's config."}
    return {"ok": False, "message": "Unsupported tool kind."}


def status(tool_id):
    """Honest per-tool connection state. json: read the config for the rubric key. cli: unknown
    (we do not shell out on every grid load). snippet: manual. unknown tool: unknown."""
    t = TOOLS.get(tool_id)
    if not t:
        return "unknown"
    kind = t["kind"]
    if kind == "snippet":
        return "manual"
    if kind == "cli":
        return "unknown"
    p = config_path(tool_id)
    if not p or not os.path.exists(p):
        return "not_connected"
    cfg = _read_json(p)
    return "connected" if SERVER_KEY in (cfg.get("mcpServers") or {}) else "not_connected"


def list_tools():
    return [{"id": tid, "name": t["name"], "kind": t["kind"], "status": status(tid)}
            for tid, t in TOOLS.items()]
