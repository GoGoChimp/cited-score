# Rubric desktop (Pro)

Rubric desktop crawls sites the web app cannot reach (private, staging, pre-launch, localhost and intranet) locally, with no page cap and nothing leaving your machine. It is a Pro feature.

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

## Authenticated and private crawls

Rubric runs locally, so it can already crawl private, staging, pre-launch, localhost and intranet URLs that the web app cannot reach, with no page cap.

For a site behind a login, supply credentials locally. They are sent only to the site you are auditing (the audited host or a subdomain of it, never a parent, a sibling, or a third party on a redirect) and are never written to a log or a report.

```bash
# HTTP Basic Auth (common on staging)
rubric audit --url https://staging.example.com --basic user:password

# A session cookie copied from your logged-in browser
rubric audit --url https://app.internal.example.com --cookie "session=abc123; role=admin"

# Preferred for secrets: a local JSON file, kept off your command line and shell history
echo '{ "cookie": "session=abc123" }' > auth.json
rubric audit --url https://app.internal.example.com --auth-file auth.json
```

`--basic` and `--cookie` appear in your shell history, so prefer `--auth-file` for anything sensitive. Automated form login is not yet supported; copy a session cookie from your browser instead.

## The local MCP (your analyst in Claude)

The local MCP is the flagship desktop surface: it runs entirely on your machine, so unlike the hosted read-only MCP it has no cloud, no queue, no page cap and needs no API key, and it can both crawl AND act.

Register it in one command, then restart Claude Desktop:

```bash
rubric mcp install
rubric mcp status      # confirm it registered
```

Then talk to Claude in plain language, or run the guided `rubric` skill. What it can do locally:

- **Crawl and score any site** (`audit_site`), including private, staging and localhost, with no page cap.
- **Run the fix loop**: audit, fix the highest-leverage thing in your codebase, re-crawl, report the honest delta.
- **Check a draft before you publish** (`check_draft`), find the citation gap (`cited_gap`), compare a competitor (`competitor_teardown`), and put a value range on the gaps (`value_bridge`).
- **AI ground-truth capture** (`analyze_ai_answer`, `analyze_ai_panel`, `fact_check_answer`): you paste one AI answer from your own browser and it reports what the AI said this time (whether you are named, who the competitors are, any hallucinated facts). It is user-driven, one answer at a time; it does not automate a browser, store a login, or treat a single capture as a measurement.
- **Self-calibrate** (`correlate`) against a Bing Webmaster Tools or GSC CSV, so the score is tuned to your real citation data.

Nothing leaves your machine.
