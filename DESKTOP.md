# Rubric desktop (Pro)

Rubric desktop crawls sites the web app cannot reach — private, staging, pre-launch, localhost and intranet — locally, with no page cap and nothing leaving your machine. It is a Pro feature.

## Install (developer install)

```bash
pipx install git+https://github.com/GoGoChimp/rubric-desktop
# or, from a local clone:
pipx install .
```

This puts two commands on your PATH: `rubric` (crawl) and `rubric-mcp` (the local MCP for Claude).

## Unlock with your account

Your licence key is your Rubric account API key. Create or copy it at https://cited.gogochimp.com/mcp (shown once), then:

```bash
rubric activate cs_live_your_key_here
rubric status
```

Activation checks your Pro status online once, then works offline for 14 days.

## Use

```bash
rubric audit --url https://staging.internal.example.com   # any flag aiseo_audit accepts
```

Never paste your key anywhere public; Rubric only ever sends it in an Authorization header to the licence check.
