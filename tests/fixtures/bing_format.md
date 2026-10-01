# Bing Webmaster Tools, AI Performance exports (Task 1 finding, 2026-10-01)

Captured from real gogochimp.com exports Chris downloaded on 2026-09-28 / 2026-10-01.
Bing WMT's AI Performance section offers three distinct exports:

## 1. AI Performance Overview Stats (daily trend, NOT per-URL)
- Fixture: `bing_ai_overview_daily.csv`
- Columns: `Date, Citations, Cited Pages`
- One row per day. `Citations` is that day's total; `Cited Pages` is a COUNT of pages cited that day, not the pages themselves.
- Use: site-level daily context only. It cannot drive the per-page join (no URLs).

## 2. AI Search Queries Report (query level)
- Fixture: `bing_ai_search_queries.csv`
- Columns: `Grounding Query, Intent, Topic, Citations, Citation Share`
- One row per grounding query. Parsed today by `load_queries` in `aiseo_audit.py`.
- Use: query coverage / gap analysis. Not the proof-loop join (maps to pages only fuzzily).

## 3. AI Page Stats Report (PER-URL, the proof-loop join source)
- Fixture: `bing_ai_page_stats.csv`
- Columns: `Page, Citations`
- One row per page. `Page` is the full absolute URL; `Citations` is the total for the export's period.
- THIS is what the proof loop ingests (Task 4 parser, Task 11 ingest). The join key is the page URL via `canon_key`.

## Decisions for the build
- The proof loop's per-URL source is export #3 (AI Page Stats). Task 4's parser targets its real headers: URL column = **`Page`**, citations column = **`Citations`**. Both already appear in the plan's tolerant header lists (`_URL_HEADERS` includes `page`; `_CITE_HEADERS` includes `citations`), so the real format matches without change.
- **No date range is embedded in export #3** (the filename carries an export date like `_10_1_2026` but not the covered range). So the reporting PERIOD must be captured at upload time, confirmed by the user (Task 12's 7/30/90 pre-fill + confirm). This matches the plan's whole-uploaded-periods model: each AI Page Stats export is one period's per-URL totals.
- Citations are large absolute totals (top page ~13,972 for the exported period), so the per-period citation floor (~30) is comfortably clear for real sites; keep it tunable for small sites.
- Period alignment for tier A still uses whole uploaded periods compared as per-day rate (the export gives a period total, not daily per-URL counts).
