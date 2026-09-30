# Rubric — build status + feature backlog

*2026-09-30. Ranking = impact on AI citation × differentiation vs competitors × usefulness (does the owner act on it and come back). Rubric principles applied throughout: crawl-derivable is the auditor's moat (advisory/ground-truth items are labelled as such, never faked as measurements); every feature must tell the owner something they don't already know and would act on; the north star is the **second crawl** (a user returning to re-crawl). Research anchors live in `gogochimp-content/handover/seo-knowledge-base.md`.*

---

## Board review (2026-09-30) — status, so settled debates don't restart

The board's read is correct: most of the 22 below already exist per the 26-27 Sep roadmap; this list re-ranked shipped work and skipped the actual top build (crawl-failure reduction). Status:

- **SHIPPED / live:** #2 (follow-up coverage map), #4 (statdensity + structural-asset coverage), #5 (render-parity), #6 (passage signals: sections/answerfirst/snippetlead/answerlead), #7 (AI-bot robots + `analyze_log`), #9 (freshness/decay), #10 (`click_resilience`), #11 (off-page presence advisory), #13 (per-engine weighting — and the Gemini-parity concern is verified **correct**, see below), #15 (market-platform advisory, partial), #16 (AI-shopping feed advisory), #18 (schema-value grader), #22 (video check).
- **QUEUED / in progress:** #3 → the schema-to-page fact-consistency check (the next small build) + the Entity-Attribute Matrix (held as the Pro flagship); #8 `cited_gap` (shipped; board asked it be resurfaced); #12/#20 (calibration + second-crawl delta, parts shipped).
- **PARKED (needs a call):** #17 local, #21 AI-answer panel (tool exists, not a product surface), #19.
- **GENUINELY NEW:** #1 agent-readiness ACTIONS-layer — real and cheap to crawl, but fails "would an owner act today?" (agent buying is early), so **park as a small advisory spike, not a Tier 1 build** (matches CANDIDATE D on the roadmap).

**Corrected build order (board's, adopted):**
1. **Crawl-failure reduction (~21%, the real top; not on my list).** Reduction core already built (browser headers + render-fallback, currys 403→200); open = measure the current rate + ship the weekly failure-rate ops metric.
2. **Gemini weighting — RESOLVED** (verified correct in `aiseo_audit.py:296-305`; drop from the queue).
3. Advisory hierarchy — built 2026-09-27; revisit only if the added cards crowd the report.
4. Schema-to-page fact-consistency check, then resurface `cited_gap`.
5. Entity-Attribute Matrix as the Pro flagship.

**Stats to source before any reach copy (board's flag):** "44% of citations from the first 30%" = Indig 2026 (already sourced in the engine — OK); "comparison ≈ 33% of citations" and "Trustpilot 1%→54%" = need a named, dated primary first (Trustpilot looks like a single case). Keep the latter two out of the product/articles until sourced.

---

## Shipped this session (2026-09-30)

- **Desktop = Ollama-style tray launcher** (branch `rubric-desktop-m1`): tray menu (Open / Open recent reports / Licence settings / Quit, left-click opens), the Ollama-style Connect grid (one-click MCP install into Claude Desktop/Code/Cursor; Codex/Cline snippets; ChatGPT greyed), native app windows (Edge `--app`), real Rubric-R icon. 93 engine tests green.
- **9-skill pack** (`cited-score-web/skill/`): `rubric` fix loop (reconciled to the local desktop MCP) + `rubric-competitor / -value / -draft / -logs / -calibrate / -subqueries / -answer / -schema`, each wrapping real local-MCP tools. Bundled into the desktop (`skills_data.py` + `rubric skills install` → `~/.claude/skills`) and installed.
- **In-app activation** — the tray opens without a Pro gate and prompts for the `cs_live_` key in a window (`/activate-pro`, no terminal). Persistent data moved to `~/.rubric` so a frozen onefile keeps its licence/reports.
- **Standalone `Rubric.exe`** (PyInstaller onefile, 35 MB, no Python) published to the public **GoGoChimp/rubric-desktop** repo (release v0.1.0; engine source stays private in `cited-score`). Stable URL: `https://github.com/GoGoChimp/rubric-desktop/releases/latest/download/Rubric.exe`.
- **Web (deployed):** entitlement fix (valid key unlocks desktop), `allow_promotion_codes` on, **`1138`** first-year-free coupon (card-on), **Download for Windows** button on the billing page (env `DESKTOP_DOWNLOAD_URL`). Billing confirmed **live** (`BILLING_ENABLED=true`). Sitemap confirmed complete + live.
- **Left for Chris:** set `DESKTOP_DOWNLOAD_URL` + redeploy; recreate `1138` in live Stripe + one live checkout; Microsoft Store (the SmartScreen fix); merge the branch.

---

## Feature backlog — 22 ranked

Type key: **[Audit]** crawl-derivable check (the moat) · **[Skill]** workflow over the MCP · **[Advisory]** off-page/ground-truth, labelled as guidance not measurement.

### Tier 1 — build first (high impact, high differentiation, crawl-derivable, timely)

1. **Agent-readiness / ACTIONS-layer audit** — [Audit] Can an AI agent *act* on the page, not just read it: labelled forms/inputs/links/buttons, machine-readable form feedback, WebMCP tool presence, and the WCAG floor (WebAIM 2026: 51% unlabeled inputs, 46% empty links, 31% unnamed buttons). Net-new category, near-zero competition, and timely — Shopify just made WebMCP transactional. The single most differentiated thing Rubric can own.
2. **Fan-out / facet-coverage audit** — [Audit] Does the content cover the sub-questions AI fans a topic into (definition / comparison / best-for / pricing / alternatives / how-to / entity)? Flags the missing facets — brands lose the Comparison and Entity fan-outs. Directly maps to being included across an AI answer, and it's a build list, not a vanity score.
3. **Entity clarity / source-of-truth audit** — [Audit] Are key entity facts consistent across the site, is there `sameAs`/Wikidata/`knowsAbout`, are name variants disambiguated, and is the "denominator" context (scale, history, proof) present and machine-readable? Entity clarity is the core of AI visibility; most tools check schema syntax, not entity consistency + the denominator (AnswerShare moved a rec rate 23.8%→100% with exactly this).
4. **Information-gain / proprietary-data audit** — [Audit] Unique-figure density (~1.5 per 100 words; 15+ figures ≈ info-gain 62.1 vs 40.2 for ≤1), front-loading (44.2% of citations come from the first 30% of a page), original-research signals. Original data is the most defensible AI-citation asset (Indig).
5. **Render-parity deepening (HTML vs rendered-DOM diff)** — [Audit] Show exactly what a no-JS AI crawler sees vs the rendered page; flag JS-only content and JS-injected schema that's invisible to GPTBot/ClaudeBot/PerplexityBot; recommend server/edge delivery. Rubric's founding thesis, made concrete and visual.
6. **Passage/block-level citability score** — [Audit] Score each passage for quotability: answer-first (40-70 words), self-contained blocks, list usage (~80% of ChatGPT-cited pages use lists), sequential heading hierarchy (87% single-H1; 2.8× citation). AI extracts passages, not pages. Partly built — promote to a first-class per-passage report.

### Tier 2 — strong, build next

7. **AI-bot crawlability audit** — [Audit] robots.txt check for GPTBot / ChatGPT-User / ClaudeBot / anthropic-ai / PerplexityBot / Google-Extended / Bingbot / CCBot, plus which AI bots actually fetch which pages (server log). A blocked bot = zero citation on that engine; owners block them by accident constantly.
8. **Comparison / vs-page coverage audit** — [Audit + Advisory] Do you have vs / alternative / "best-X" roundup pages for your category (comparison ≈ 33% of AI citations; 80% of mentions sit in the first three list positions)? Detect what exists, flag the gaps by competitor/query.
9. **Freshness / decay audit** — [Audit] Last-updated visibility, decay risk, recrawl window (~130-140 days), topicality (>half of citations are <12 months old, peak ~7 days post-publish). Strong second-crawl driver — the natural reason to come back and re-audit after a refresh.
10. **Click-resilience per page** — [Audit] Will AI answer this inline (zero-click) or send a click, and what earns the click even when the answer is extracted? (AWR: an AIO thirds the click, 29.05%→10.04%.) The `click_resilience` tool exists — make it a per-page report.
11. **Third-party / review-platform presence** — [Advisory] Wikipedia / Reddit / YouTube / Trustpilot / G2 / Capterra footprint (claiming a Trustpilot profile lifted AI citation 1%→54%; brands are 6.5× more cited via third parties). High impact, but off-page — surfaced as labelled guidance, not a crawl score.
12. **Calibration + confidence** — [Skill] Tune the score to the user's real Bing WMT / GSC AI-citation data with confidence ranges (rankings are noisy; citation ≠ mention; use median position). `correlate` exists — add the confidence layer and the measurement discipline.
13. **Cross-engine citability profile** — [Audit] Weight the score correctly per engine — ChatGPT/Perplexity/Claude (retrieval + parametric shortlist) vs Google AIO (ranking-correlated) — and surface per-engine gaps. Fixes the backwards Gemini weighting; genuinely differentiated framing.
14. **"Denominator / full-context" completeness** — [Audit] Are scale/history/proof/entity facts present and machine-readable so AI has the whole picture, not just the complaints? (The AnswerShare fix.) Can ship as a sub-check of #3.

### Tier 3 — worthwhile (niche, advisory, or roadmap)

15. **International / localised AI-readiness** — [Advisory] hreflang, per-market platform presence (ccTLD → market → the platforms AI cites *there*), non-English lower-competition wedge. Ties to the international push.
16. **Ecommerce feed-readiness** — [Advisory] Merchant Center + OpenAI merchant feed (refreshed as often as every 15 min) + ChatGPT Shopping membership. For ecom clients; feed membership, not page citability, is the shopping surface.
17. **Local AI-readiness** — [Advisory] GBP completeness, review rating/volume (ChatGPT recommends ~4.3★ avg), "Ask Maps" attributes, NAP consistency. For local businesses.
18. **Schema retrieval-value audit** — [Audit] Attribute-rich vs generic schema (helps 61.7 vs 41.6), schema render-parity, which types still earn rich results — framed as retrieval-help, not parametric memory. Deepens the existing schema check.
19. **LLM-aware content-audit / pruning skill** — [Skill] The 7-step Remove/Combine/Update/Keep pass by info-gain + funnel stage + business metric, across a site's audit history.
20. **Second-crawl delta / decay watch (first-class)** — [Skill] Track score + citations over time, alert on regression/decay, surface the delta report. Directly serves the north star; `watch` exists — make the delta report first-class.
21. **AI-answer ground-truth panel (expand)** — [Skill] Expand `analyze_ai_answer`/`_panel` into a saved, user-triggered cross-engine panel ("what the AIs said this time"), framed as samples, never a measurement.
22. **Video / YouTube citability** — [Audit] YouTube is the #1 AIO-cited domain and the transcript is the citable unit; check embedded/linked video for transcript, captions, chapters, entity-rich description.

*(Roadmap extra: a command-centre / stats skill that builds a dashboard from local audit history — the tray spec's v2.)*
