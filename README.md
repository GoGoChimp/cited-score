# CITED Score

The free companion auditor to the book **CITED** (Chris McCarron / GoGoChimp). A "Screaming Frog for AEO / GEO / AI-SEO": it crawls the **entire site**, renders every page with headless Chrome, and scores how **citable and extractable** each page is for AI search. It reports one honest headline number and, more importantly, **ends in an action plan**.

> **The book:** [gogochimp.com/cited](https://www.gogochimp.com/cited) &nbsp;·&nbsp; **This tool:** [gogochimp.com/cited-score](https://www.gogochimp.com/cited-score) &nbsp;·&nbsp; Built by [Chris McCarron](https://www.gogochimp.com) at [GoGoChimp](https://www.gogochimp.com), the AI-SEO / AI-CRO agency.

Scores are given three ways:
- **Overall** CITED Score (0-100).
- **By pillar** - the three questions an engine asks (CITED ch3): **Known** (do I know you?), **Findable** (can I find your answer?), **Trusted** (do I trust you enough to name you?).
- **Per engine** - ChatGPT, Perplexity, Google AI Overviews, Gemini, Copilot, Claude, since each weights different signals.

No API keys, no LLM. Uses your installed Chrome or Edge to render (essential: curl/requests miss JS-injected schema).

## Download (Windows)
Grab the latest `CITED-Score.exe` from the [Releases page](https://github.com/GoGoChimp/cited-score/releases/latest). Double-click it; no Python needed. It still needs Google Chrome or Microsoft Edge installed (used to render pages) and warns if neither is found. The app checks for newer releases on launch and shows an update banner when one is available.

**First run:** the app is not code-signed yet, so Windows Defender SmartScreen may show "Windows protected your PC". Click **More info -> Run anyway**. This is expected for any unsigned app; the full source is in this repo, so you can inspect it or build the exe yourself.

## Run from source
Requirements: Python 3.10+, Google Chrome or Microsoft Edge, then `pip install -r requirements.txt`.

App (no command line):
```
python app.py
```
Opens a local web app at http://127.0.0.1:5000. Enter any website, click Run, watch the live crawl, open the report. Reports are saved per domain under `reports/` and listed for revisiting; re-running a site shows the since-last-crawl diff.

Command line:
```
python aiseo_audit.py --url https://www.example.com --out report
```
- `--max-pages 0` (default) crawls the **entire site**; a number caps it.
- `--workers 6` renders N pages in parallel (isolated Chrome profile each).

Outputs `report.html` (branded, tabbed), `report.json`, `report.csv`. Re-running appends to `report-history.jsonl` and the next report shows a **since-last-crawl diff**.

## The report (tabs)
- **Overview** - CITED Score, three pillar rings, six engine rings, "do these first", totals, page types, and the diff vs the last crawl.
- **Action Plan** - every fix **ranked by projected score gain** (the tool re-scores the whole site with each fix simulated), each showing the +overall and per-engine gain, pillar, chapter, effort, and the affected-page count. Ends in a **30/60/90-day roadmap**.
- **Issues** - grouped by pillar; each carries a why (evidence line), a fix, and the expandable affected-URL list.
- **Pages** - sortable, **searchable** table: score, pillar scores, per-engine columns, response time, errors. CSV export.
- **Per engine** (6 tabs) - that engine's readiness, the signals it weights (with site pass rate), and its worst pages.
- **Site structure** - by section and crawl depth. **Response times** - server + render, slowest pages.
- **Print / PDF** and **Export CSV** buttons for client delivery.

## Ruleset obeys CITED, not GEO folklore
A data-led author's tool must not prescribe what his own book calls a myth:
- **llms.txt is informational, not scored** - SE Ranking found no correlation with citations (ch5). Shown for reference only.
- **Sections are judged "self-contained, no walls of text"**, not a word-count band - one liftable answer per block, never "chunk artificially" (ch5).
- **Answer capsule target is 40-60 words** (ch5).
- **Every check carries an evidence line** naming its source (engine documentation, first-party citation data, or a CITED chapter), and the footer states the honest ceiling: **the score estimates citability; it does not measure citations.**

## Calibrate against real citations (optional)
```
python aiseo_audit.py --calibrate citations.csv --report report.json
```
`citations.csv` is `url,citations` - export per-URL citation counts from **Bing Webmaster Tools > AI Performance**. The tool correlates (Spearman) each pillar, engine and check against real citations, and flags which signals to up-weight. (This repo does not ship a citations file; bring your own.)

## Desktop packaging
`desktop.py` wraps the local server in a native window (pywebview / Edge WebView2 on Windows); `app.py` also runs standalone in a browser.
```
pip install pyinstaller pywebview
pyinstaller CITED-Score.spec
```
Ships `dist/CITED-Score.exe`. Attach it to a GitHub Release tagged with the app version (see `APP_VERSION` in `app.py`) so the in-app update check can find it. Mac builds must be done on a Mac.

## Brand
Orange-on-black to match the CITED cover (`#0d0b0a` field, brand orange `#ff4d00`, `#f2ede9` type, red `[1]` citation chip). The report is built to be screenshotted into LinkedIn posts and client emails. The **CITED** wordmark is set in Impact; section and display headers in Figtree ExtraBold.

## Notes
- The ruleset encodes CITED's playbook (answer-first, self-contained passages, evidence density, entity/schema clarity, AI-crawler access), sourced.
- Rendering uses isolated Chrome profiles so it never clashes with a running Chrome.
