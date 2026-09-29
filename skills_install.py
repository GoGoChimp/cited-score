"""Install the bundled Rubric skills into an AI tool's skills directory, so a connected Claude gets
the whole pack (the fix loop plus the focused workflows), not just the MCP. Default target is
~/.claude/skills (Claude Code personal skills). Nothing here reaches the network."""
import os
import skills_data


def default_dir():
    return os.path.join(os.path.expanduser("~"), ".claude", "skills")


def install(target=None):
    """Write each bundled skill to <target>/<name>/SKILL.md. Returns {ok, dir, installed:[names]}."""
    d = target or default_dir()
    written = []
    for name, content in skills_data.SKILLS.items():
        sd = os.path.join(d, name)
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(content)
        written.append(name)
    return {"ok": True, "dir": d, "installed": sorted(written)}


def installed(target=None):
    """Names of the bundled skills that are present in the target dir."""
    d = target or default_dir()
    return sorted(n for n in skills_data.SKILLS if os.path.exists(os.path.join(d, n, "SKILL.md")))


def bundled_count():
    return len(skills_data.SKILLS)
