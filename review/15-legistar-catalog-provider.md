# review/15 — Legistar Calendar Provider (historical Granicus coverage)

**Maturity: L2→L3 · breakout of [`review/11`](11-technical-design-roadmap.md) Phase R · last updated
2026-06-13**

> A new `legistar` provider that scrapes `Calendar.aspx` year-by-year and maps Granicus clip IDs to
> normalized `Episode` objects. It solves the RSS 100-item view-cap for cities where all meeting bodies
> share a single Granicus view — Pflugerville TX is the immediate migration target. Media download,
> deeplinks, and chapter indices continue to use Granicus infrastructure; only the episode-discovery
> index moves to Legistar.

---

## Why now (after Phase H)

Granicus RSS is hard-capped at 100 items per view. Pflugerville TX has a single Granicus view
(`view_id=1`) used by all ten bodies. Once 100 items fill the view, older meetings disappear from the
RSS permanently — the backlog is effectively invisible.

Unlike Fort Worth (17 views, can add a new view_id) or CivicPlus cities (no cap), Pflugerville has no
additional Granicus views to merge. The Granicus operator hasn't created extras.

**Legistar as an index.** Pflugerville publishes its complete meeting calendar at
`pflugerville.legistar.com/Calendar.aspx`. Each row links to the same Granicus clip the RSS would
carry, but the calendar has unlimited history, is paginated by year, and contains the `clip_id` needed
to construct every URL the pipeline already knows how to use. The provider scrapes the calendar as an
index; Granicus remains the media host.

**0 materialized audio files.** Pflugerville has 226 episode records in state but no hosted audio.
Switching `provider: legistar` assigns a new `source_key`, orphaning the old Granicus records. Because
nothing is materialized, this is a clean break: all 226 old records vanish from state; the new provider
re-discovers the full history (1,000+ episodes) on first run. No audio files are orphaned.

**Pflugerville first; other cities are future scope.** Fort Worth has `fortworthgov.legistar.com` with
City Council only (10+ bodies live on Granicus, not all in Legistar). Denton County has
`dentoncounty.legistar.com` but no video links. Neither is in scope for the initial migration.

**Not Phase F #31.** The existing Phase F item "#31 Legistar provider (rich agendas/votes/rosters)"
is about the **InSite REST API** — structured meeting packets, vote tallies, and roster data used for
Phase F pre-meeting foresight and Phase R #3/#8 cards. That is a separate adapter and a separate
milestone. This provider is about **calendar scraping for Granicus video coverage only**; the two can
coexist (a city could have both the InSite API adapter and a calendar-based episode index), but they
are independent.

---

## How Legistar Calendar.aspx works

`Calendar.aspx` is an ASP.NET WebForms page served by the Granicus/Accela Legistar platform.

### Year filtering

Append `?Mode=YYYY` to scope the calendar to a single year:
```
https://pflugerville.legistar.com/Calendar.aspx?Mode=2024
```
Without a year param the page defaults to the current calendar year.

### Row structure

Each meeting row in the Telerik RadGrid table contains:
- **Body name** — plain text in the first `<td>` of each data row; matches the value of `body:` in
  the source config (e.g. `"City Council"`, `"Library Board"`).
- **Meeting date** — typically MM/DD/YYYY text in a `<td>`.
- **Agenda link** — an `<a>` tag pointing to the Legistar agenda viewer (optional; row present even
  without video).
- **Video link** — an `<a>` with an `onclick` attribute of the form:
  `onclick="radopen('...', 'VideoWidth'); return false;"` where the URL fragment contains
  `ID1=<clip_id>`. Rows without video (agenda-only meetings, cancelled meetings) have no such link —
  skip them.

### Granicus clip_id extraction

The onclick attribute reliably embeds `ID1=<clip_id>` as a query parameter. Example:
```html
<a ... onclick="radopen('https://pflugerville.granicus.com/MediaPlayer.php?view_id=1&amp;clip_id=1234&amp;meta_id=5678', 'VideoWidth'); return false;">Video</a>
```

Extracted: `clip_id = 1234`, `view_id = 1` (also verifiable against the source config).

### Pagination

Each year page is paginated when the body has more than the server's page size (empirically 100 rows
per page). The page header reads:
```
Records 1 - 100 of 347
```

Pagination is powered by **Telerik RadGrid** on top of ASP.NET WebForms. All page-navigation is
stateful: the next-page POST must carry the current `__VIEWSTATE` token (≥ 300 KB of base64-encoded
ASP.NET view state), plus `__VIEWSTATEGENERATOR` and `__EVENTVALIDATION`. The `__EVENTTARGET` field
selects the next-page control:

```
__EVENTTARGET = ctl00$ContentPlaceHolder1$gridCalendar$ctl00$ctl02$ctl00$ctl04
```

`ctl04` is the "next page" button in the default Legistar pager strip; `ctl03` is the current-page
indicator (which also accepts a page-number POST but is harder to construct). Each POST response
contains updated `__VIEWSTATE` for the following page. Fetching is complete when the "next page"
button element is absent or disabled in the response HTML.

---

## GUID and Episode construction

### GUID — MediaPlayer URL

Each episode's `guid` is the Granicus `MediaPlayer.php` URL for that clip:

```
{granicus_base}/MediaPlayer.php?view_id={view_id}&clip_id={clip_id}
```

Example: `https://pflugerville.granicus.com/MediaPlayer.php?view_id=1&clip_id=1234`

This is **identical** to the URL Granicus RSS puts in `<link>` for the same clip, so:
- If a city ever has both a Granicus RSS feed and a Legistar feed configured, episode deduplication
  via `guid` naturally prevents duplicates.
- If the operator migrates from Granicus RSS to Legistar, replaying the same guids against
  `records.merge_persisted` correctly updates rather than duplicates records (provided `source_key`
  is stable after migration).
- The MediaPlayer URL is stable (clip IDs do not rotate) and is the canonical public reference for
  the clip.

### video_url — DownloadFile endpoint

```
{granicus_base}/DownloadFile.php?clip_id={clip_id}
```

`GranicusProvider.resolve_media_url` already knows how to follow this redirect to the signed CDN
URL with backoff (rate-limit retry for 403). `LegistarProvider` delegates to the same logic.

### links dict

Same as `GranicusProvider._episode_links`:
- `canonical_video` → `MediaPlayer.php?view_id={view_id}&clip_id={clip_id}`
- `agenda` → `AgendaViewer.php?view_id={view_id}&clip_id={clip_id}` (synth, no extra HTTP)

### Date and title

The meeting date is scraped from the `<td>` adjacent to the body name. Legistar records do not
always carry a meeting title; when absent, default to `"{body} Meeting"` (consistent with the
current Granicus RSS title for Pflugerville meetings, which is often "City Council" with no extra
description).

### Description

Use the body's canonical description from the feed YAML's `podcast_description`, or the empty
string. Legistar calendar rows carry no per-meeting description.

---

## Source config schema

```yaml
provider: legistar
source:
  calendar_url: https://pflugerville.legistar.com/Calendar.aspx
  granicus_base: https://pflugerville.granicus.com
  view_id: 1                         # Granicus view_id for this city's archive
  body: "City Council"               # Legistar body name to filter; omit for all-meetings feed
  backfill_since: "2014-01-01"       # earliest year to fetch; provider iterates Year..today
```

Required keys: `calendar_url`, `granicus_base`, `view_id`, `backfill_since`.  
Optional: `body` — when absent, all rows (all bodies) are returned. The all-meetings feed
(`pflugerville-tx.yml`) omits `body`.

`backfill_since` is a YYYY-MM-DD date string. The provider iterates
`range(backfill_since.year, current_year + 1)` and fetches `calendar_url?Mode=YYYY` for each.

### SSRF note

`calendar_url` is operator-supplied (YAML), not user-facing. Still, `LegistarProvider.validate()`
must confirm `calendar_url` is a well-formed HTTPS URL before any fetch. The provider must **not**
follow any redirect that changes the host. `granicus_base` is used only to construct URLs; it
must also be `https://`.

---

## Provider protocol mapping

| Method | Implementation |
|---|---|
| `name` | `"legistar"` |
| `capabilities` | `frozenset({"deeplink"})` — same as Granicus; MediaPlayer deeplinks work |
| `validate(source)` | check `calendar_url`, `granicus_base`, `view_id`, `backfill_since` present + `https://` schemes |
| `detect_change(source)` | return `None` — no cheap probe (Legistar pages carry no ETag/Last-Modified); caller always fetches |
| `fetch_episodes(source)` | year-by-year scrape with pagination; returns `list[Episode]` |
| `resolve_media_url(episode, source)` | delegate to `granicus._resolve_download_url(episode.video_url)` — identical DownloadFile.php backoff logic |
| `video_deeplink(ref, t_seconds)` | delegate to `GranicusProvider.video_deeplink` — appends `&starttime=N` to a MediaPlayer URL |
| `fetch_chapters(episode, source)` | delegate to `GranicusProvider.fetch_chapters` — same JSON.php endpoint, same clip_id |
| `fetch_view_counts(source)` | return `[]` — no view-cap concept for a scraping-based provider; audit never fires view-cap warnings for Legistar feeds |

---

## HTML parsing

No new dependency. Use `re` (stdlib) for the narrow patterns the provider needs:

1. **Hidden ASP.NET fields** (per page load, for POST construction):
   ```python
   re.search(r'id="__VIEWSTATE"\s+value="([^"]*)"', html)
   re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"', html)
   re.search(r'id="__EVENTVALIDATION"\s+value="([^"]*)"', html)
   ```

2. **Row boundaries** — split on the Telerik row ID pattern:
   ```python
   re.split(r'id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00__\d+"', html)
   ```
   Each segment between splits is one row's HTML.

3. **Body name** — first `<td>` text content in a row:
   ```python
   re.search(r'<td[^>]*>\s*([^<]+?)\s*</td>', row_html)
   ```

4. **clip_id from video link onclick**:
   ```python
   re.search(r'ID1=(\d+)', row_html)
   ```

5. **Meeting date** — second `<td>` after the body name; parse with `datetime.strptime`.

6. **Total record count** (for pagination):
   ```python
   re.search(r'Records\s+\d+\s+-\s+\d+\s+of\s+(\d+)', html)
   ```

7. **Next-page button absent/disabled** (pagination termination):
   ```python
   re.search(r'ctl00_ContentPlaceHolder1_gridCalendar_ctl00_ctl02_ctl00_ctl04[^>]*disabled', html)
   ```
   Or: button element completely absent.

This approach is brittle against markup changes but Legistar's grid HTML has been stable for years.
Add a `ProviderError` with a clear message if any required pattern fails to match so failures are
surfaced quickly rather than silently producing empty results.

---

## fetch_episodes implementation sketch

```python
def fetch_episodes(self, source: dict) -> list[Episode]:
    since_year = datetime.fromisoformat(source["backfill_since"]).year
    current_year = datetime.now().year
    episodes: list[Episode] = []
    seen_guids: set[str] = set()
    with make_session() as session:
        for year in range(since_year, current_year + 1):
            for html in _iter_year_pages(session, source["calendar_url"], year):
                for ep in _parse_calendar_page(html, source):
                    if ep.guid not in seen_guids:
                        seen_guids.add(ep.guid)
                        episodes.append(ep)
    return episodes
```

`_iter_year_pages(session, url, year)` is a generator that yields raw HTML strings, one per page:
- First: `session.get(url, params={"Mode": year})`
- Subsequent: `session.post(url, data={...})` with the `__EVENTTARGET` / `__VIEWSTATE` form fields
- Stops when total = collected or next-page button absent

`_parse_calendar_page(html, source)` extracts rows for the configured body, returns `list[Episode]`.

**Performance.** Fetching all years from `backfill_since` through the present issues one GET + ~K
POSTs per year. For 10+ years at ~200 meetings/year (2 pages each), this is ~20–30 HTTP calls —
fast on the Actions runner. The result set is deduplicated by `guid` across years.

---

## Migration plan for Pflugerville

### Step 1 — Verify body names against live Legistar

Before updating YAML, fetch `pflugerville.legistar.com/Calendar.aspx` and list the distinct body
names present in the first few years. Confirm:

| Config `body:` value | Expected Legistar body name |
|---|---|
| `"City Council"` | verify exact string |
| `"City Council Worksession"` | may be `"City Council Work Session"` or `"City Council - Work Session"` |
| `"City Council Special Meeting"` | may be `"City Council - Special Meeting"` |
| `"Planning and Zoning Commission"` | verify exact string |
| `"Library Board"` | verify exact string |
| `"Parks & Rec"` | likely `"Parks and Recreation"` or similar |
| `"Capital Improvement Advisory Committee"` | verify |
| `"Capital Improvement Bond Committee"` | may appear only in certain years |
| `"Charter Review Commission"` | may appear only in certain years (review cycles) |
| `"Equity Advisory Board"` | verify; may be newer |

Any mismatch between the Legistar body name and the config `body:` string means zero episodes for
that feed. Body name verification **must happen before migration**.

### Step 2 — Update YAML files

All ten body-filtered Pflugerville feeds change from:
```yaml
provider: granicus
source:
  feed_url: https://pflugerville.granicus.com/ViewPublisherRSS.php?view_id=1&mode=vpodcast
  body: "City Council"
```
to:
```yaml
provider: legistar
source:
  calendar_url: https://pflugerville.legistar.com/Calendar.aspx
  granicus_base: https://pflugerville.granicus.com
  view_id: 1
  body: "City Council"          # confirmed against live Legistar in Step 1
  backfill_since: "2014-01-01"  # earliest year with Granicus video on Legistar
```

The all-meetings feed (`pflugerville-tx.yml`, no `body:`) changes similarly but omits `body:`:
```yaml
provider: legistar
source:
  calendar_url: https://pflugerville.legistar.com/Calendar.aspx
  granicus_base: https://pflugerville.granicus.com
  view_id: 1
  backfill_since: "2014-01-01"
```

### Step 3 — source_key change and orphan handling

The new `source_key = SHA1[:12]("legistar|{json.dumps(source, sort_keys=True)}")` is different from
the old Granicus key. The old `state/sources/<old_key>/episodes.json` records are not deleted —
they are simply not read by the new provider key. Since Pflugerville has **0 hosted audio files**,
no B2 objects are orphaned.

The old state records will be cleaned up by the normal B2 GC sweep once the old source key no
longer appears in any feed config. Until that sweep runs, the orphaned records are inert.

### Step 4 — First run

The new provider fetches all years from 2014 → present, yielding 1,000+ episodes. The pipeline
processes these as new, running the normal projection/skip logic. Feed and audio manifests rebuild
from scratch. The feed-health audit should find no view-cap warnings (Legistar has no view concept).

### Step 5 — Fort Worth expansion (future scope)

Fort Worth has City Council only on Legistar (`fortworthgov.legistar.com`). Its existing 17 Granicus
feeds already have `view_id=5` through view_id=12 for different bodies. Only City Council would
migrate; all other bodies stay on Granicus. Fort Worth's City Council has far more meetings per year
than Pflugerville — page-2 pagination is critical. This migration should happen after the provider
is proven stable on Pflugerville.

---

## Files

| File | Change |
|---|---|
| `citypods/providers/legistar.py` | **New.** `LegistarProvider` class + `_iter_year_pages` + `_parse_calendar_page` + `_extract_aspnet_fields` helpers |
| `citypods/providers/__init__.py` | Import `LegistarProvider`, call `register(LegistarProvider())` |
| `citypods/providers/granicus.py` | Expose `_resolve_download_url` as a module-level function (already has the backoff logic inside `resolve_media_url`; extract so `legistar.py` can import it without coupling to `GranicusProvider` instance) |
| `config/feeds/pflugerville-tx.yml` | Switch to `provider: legistar` |
| `config/feeds/pflugerville-tx-*.yml` (10 files) | Switch to `provider: legistar`; confirm `body:` strings after Step 1 |
| `tests/test_legistar.py` | Unit tests (see §Test plan) |
| `tests/fixtures/legistar_calendar_2024_p1.html` | Single-page fixture (< 100 rows, no pagination needed) |
| `tests/fixtures/legistar_calendar_2023_p1.html` | Multi-page fixture, page 1 of 2 |
| `tests/fixtures/legistar_calendar_2023_p2.html` | Multi-page fixture, page 2 of 2 |

### granicus.py refactor note

The DownloadFile.php backoff logic is currently embedded in
`GranicusProvider.resolve_media_url` (lines 135–160 of `granicus.py`). Extract it to a
module-level helper:

```python
def _resolve_download_url(url: str) -> str:
    """Follow DownloadFile.php to signed CDN URL with 403-backoff."""
    ...  # existing logic
```

`GranicusProvider.resolve_media_url` delegates to it. `LegistarProvider.resolve_media_url` imports
and calls it. This is a pure refactor — no behavior change.

---

## Test plan

### Unit tests (offline, fixture-based — default test run)

| Test | Fixture | Assertion |
|---|---|---|
| `test_parse_single_page` | `legistar_calendar_2024_p1.html` | returns N episodes with correct guids, dates, body |
| `test_body_filter` | same | with `body="Library Board"` returns only Library Board rows |
| `test_all_meetings_no_body` | same | without `body` returns all rows with video links |
| `test_skip_row_no_video` | row without onclick | episode not created |
| `test_pagination_state_extraction` | `legistar_calendar_2023_p1.html` | `__VIEWSTATE` extracted correctly |
| `test_pagination_terminates` | `legistar_calendar_2023_p2.html` (last page) | `_iter_year_pages` stops after page 2 |
| `test_guid_dedup` | same guid appears in two fixtures | episode list deduplicated |
| `test_validate_missing_key` | — | `ValueError` on missing `calendar_url` |
| `test_validate_http_rejected` | — | `ValueError` on `http://` scheme |
| `test_fetch_view_counts_empty` | — | `provider.fetch_view_counts(source) == []` |
| `test_detect_change_none` | — | `provider.detect_change(source) is None` |
| `test_resolve_media_url_delegates` | monkeypatch `granicus._resolve_download_url` | called with `DownloadFile.php?clip_id=N` |
| `test_video_deeplink` | — | `provider.video_deeplink("...MediaPlayer.php?...", 90) == "...&starttime=90"` |

### Live tests (opt-in, `pytest -m live`)

- `test_live_fetch_current_year` — fetch current-year calendar for Pflugerville City Council;
  assert ≥ 1 episode with a valid MediaPlayer URL.
- `test_live_pagination` — fetch a year known to exceed 100 meetings; assert > 100 episodes returned.

---

## Acceptance criteria

- [ ] `citypods doctor pflugerville-tx-city-council` (after YAML migration) returns ≥ 200 episodes
  and no errors.
- [ ] `citypods doctor pflugerville-tx-library-board` returns ≥ 20 episodes going back before the
  current RSS 100-item window.
- [ ] `python scripts/audit_feeds.py --dry-run --city pflugerville-tx-city-council` emits zero
  `view-cap` warnings for the Pflugerville feeds.
- [ ] All 12 unit tests pass (`pytest tests/test_legistar.py`).
- [ ] `fetch_view_counts` returns `[]`; no `view-cap` GitHub issues are filed for Legistar feeds.
- [ ] Episode guids produced by the Legistar provider for clips that also appear in the Granicus RSS
  are identical (verified by running both and diffing guids for the overlapping window).
- [ ] `resolve_media_url` on a Pflugerville episode returns an `https://archive-video.granicus.com/`
  signed URL (verified live with `--city pflugerville-tx-city-council`).
- [ ] The `legistar` provider appears in `citypods providers` output.
- [ ] The new `source_key` for the Pflugerville all-meetings feed differs from the old Granicus key
  (confirmed by running `records.source_key` before and after the YAML change).
- [ ] Old Pflugerville Granicus state files (under old `source_key`) are not deleted by the migration
  itself; they are left for the B2 GC sweep.
