# review/15 — Legistar Calendar Provider (historical Granicus coverage)

**Maturity: L2→L3 · breakout of [`review/11`](11-technical-design-roadmap.md) Phase R · last updated
2026-06-15**

> A new `legistar` provider that scrapes `Calendar.aspx` year-by-year and maps Granicus clip IDs to
> normalized `Episode` objects. It solves or reduces the RSS 100-item view-cap for cities where the
> Granicus RSS views hide older meetings — Pflugerville TX remains the first migration target, and
> Arlington TX plus Fort Worth TX are now explicit follow-on feed-health targets. Media download,
> deeplinks, and chapter indices continue to use Granicus infrastructure; only the episode-discovery
> index moves to Legistar.

---

## Why now (after Phase H)

Granicus RSS is hard-capped at 100 items per view. Pflugerville TX has a single Granicus view
(`view_id=1`) used by all ten bodies. Once 100 items fill the view, older meetings disappear from the
RSS permanently — the backlog is effectively invisible.

Unlike Denton County, which exposes year-specific Granicus archive RSS views, Pflugerville has no
additional Granicus views to merge. Arlington and Fort Worth do expose some alternate Granicus views,
but those useful views are also at the 100-item cap; they improve coverage but do not eliminate the
underlying backlog limit.

**Legistar as an index.** Pflugerville publishes its complete meeting calendar at
`pflugerville.legistar.com/Calendar.aspx`. Each row links to the same Granicus clip the RSS would
carry, but the calendar has unlimited history, is paginated by year, and contains the `clip_id` needed
to construct every URL the pipeline already knows how to use. The provider scrapes the calendar as an
index; Granicus remains the media host.

**0 materialized audio files.** Pflugerville has 226 episode records in state but no hosted audio.
Switching `provider: legistar` assigns a new `source_key`, orphaning the old Granicus records. Because
nothing is materialized, this is a clean break: all 226 old records vanish from state; the new provider
re-discovers the full history (1,000+ episodes) on first run. No audio files are orphaned.

**Feed-health target inventory (2026-06-15 triage).** The provider action should try to close or
reduce these active `view-cap` warnings, not only Pflugerville:

| City / issue set | Current Granicus problem | Legistar scope |
|---|---|---|
| Pflugerville TX (#93-#95, #97-#104) | Single `view_id=1` for every configured feed; only the newest 100 mixed-body meetings are visible. | Migrate all Pflugerville feeds to `pflugerville.legistar.com` if body-name verification passes. |
| Arlington TX (#23-#25) | Shared `view_id=2` is capped; body-specific `view_id=9` (Council) and `view_id=10` (Planning and Zoning) add older meetings but are also capped. | Verify the Arlington Legistar calendar host and video links. If usable, migrate Council, Planning and Zoning, and the all-meetings feed to Legistar; otherwise at least switch the Granicus feeds to body-specific views and leave a documented residual cap. |
| Fort Worth TX (#90, #192, #193) | Current configured archive views help, but City Council / Worksession still hit capped RSS windows; `view_id=7` adds some Public Comment rows not in the current config. | Use `fortworthgov.legistar.com` for Council, Worksession, and Public Comment coverage if the video links map back to Granicus clips. This does **not** solve the board/commission view-cap issues (#189-#191, #194-#205), because those bodies are not exposed in the Legistar calendar today. |

Denton County is deliberately not part of this Legistar migration: live triage found many
year-specific Granicus archive views and the official page already describes searchable archives from
1998 to present. Denton needs either those archive RSS views wired in or a Granicus archive-page
scraper, not this calendar provider.

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

Extracted: `clip_id = 1234`, `view_id = 1`. The provider should prefer the row's parsed `view_id`
when present and use the source config's `view_id` only as a fallback. This keeps Pflugerville simple
while allowing Arlington and Fort Worth rows to point at whichever Granicus view the Legistar calendar
uses for that clip.

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
  view_id: 1                         # fallback Granicus view_id when a row omits it
  body: "City Council"               # Legistar body name to filter; omit for all-meetings feed
  backfill_since: "2014-01-01"       # earliest year to fetch; provider iterates Year..today
```

Required keys: `calendar_url`, `granicus_base`, `backfill_since`.
Optional: `body` — when absent, all rows (all bodies) are returned. The all-meetings feed
(`pflugerville-tx.yml`) omits `body`. Optional: `view_id` — fallback only; use the `view_id` parsed
from each video link whenever the row supplies one. Pflugerville should keep `view_id: 1`; Arlington
and Fort Worth may still set a fallback, but correctness should not depend on one fixed view per
source.

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
| `validate(source)` | check `calendar_url`, `granicus_base`, `backfill_since` present + `https://` schemes; if `view_id` is present, ensure it is an integer/string integer fallback |
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

4. **clip_id and view_id from video link onclick**:
   ```python
   re.search(r'ID1=(\d+)', row_html)
   re.search(r'view_id=(\d+)', row_html)
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

## Migration plan

### Step 1 — Verify body names against live Legistar

Before updating YAML, fetch each target `Calendar.aspx` and list the distinct body names present in
the first few years. Confirm exact strings and video-link presence before migrating any feed: a body
name mismatch means zero episodes, and a calendar row without a Granicus video link is useful for
future agenda work but not for this episode provider.

Pflugerville:

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

Arlington:

| Config feed | Expected Legistar body name / check |
|---|---|
| `arlington-tx-council` | Verify Council body name and that rows expose Granicus video links. |
| `arlington-tx-planning-and-zoning-commission` | Verify Planning and Zoning body name and video links. |
| `arlington-tx` | Verify all-meetings mode returns both Council and Planning and Zoning rows without duplicates. |

Fort Worth:

| Config feed | Expected Legistar body name / check |
|---|---|
| `fort-worth-tx-city-council` | Verify `CITY COUNCIL` rows expose Granicus video links. |
| `fort-worth-tx-city-council-worksession` | Verify `CITY COUNCIL WORKSESSION` rows expose Granicus video links. |
| Optional follow-up feed | `CITY COUNCIL PUBLIC COMMENT` exists in Legistar; decide whether to add a feed or fold into the all-meetings feed. |

Do **not** migrate Fort Worth board/commission feeds under this provider unless live verification
shows those bodies in Legistar with video links. The current evidence says they remain Granicus-only.

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

If Arlington verification passes, migrate these feeds similarly:

```yaml
provider: legistar
source:
  calendar_url: https://arlingtontx.legistar.com/Calendar.aspx
  granicus_base: https://arlingtontx.granicus.com
  view_id: 9                         # Council fallback; P&Z fallback is 10
  body: "Council"                    # confirmed exact Legistar body name
  backfill_since: "2020-01-01"       # adjust to earliest year with video links
```

Use `view_id: 10` as the fallback for `arlington-tx-planning-and-zoning-commission`; omit `body` for
`arlington-tx` after confirming all-meetings mode is not too broad.

If Fort Worth verification passes, migrate only the Council/Worksession feeds first:

```yaml
provider: legistar
source:
  calendar_url: https://fortworthgov.legistar.com/Calendar.aspx
  granicus_base: https://fortworthgov.granicus.com
  body: "CITY COUNCIL"               # or "CITY COUNCIL WORKSESSION"
  backfill_since: "2019-01-01"
```

Fort Worth should rely on per-row `view_id` parsing from the Legistar video link; keep any configured
`view_id` as a fallback only. The Fort Worth all-meetings feed should stay Granicus until the provider
scope explicitly models mixed Legistar + Granicus source composition, because most configured
Fort Worth board/commission bodies are not in Legistar.

### Step 3 — source_key change and orphan handling

The new `source_key = SHA1[:12]("legistar|{json.dumps(source, sort_keys=True)}")` is different from
the old Granicus key. The old `state/sources/<old_key>/episodes.json` records are not deleted —
they are simply not read by the new provider key.

Pflugerville has **0 hosted audio files**, so no B2 objects are orphaned. Before migrating Arlington
or Fort Worth feeds, run the same hosted-audio inventory check and document the result in the PR. If
they already have hosted audio, preserve stable episode GUIDs by confirming overlap against the
Granicus feed before switching the YAML; otherwise the migration can orphan hosted objects and cause
avoidable re-encodes.

The old state records will be cleaned up by the normal B2 GC sweep once the old source key no
longer appears in any feed config. Until that sweep runs, the orphaned records are inert.

### Step 4 — First run

The new provider fetches all years from 2014 → present, yielding 1,000+ episodes. The pipeline
processes these as new, running the normal projection/skip logic. Feed and audio manifests rebuild
from scratch. The feed-health audit should find no view-cap warnings (Legistar has no view concept).

For Arlington and Fort Worth, use the verified `backfill_since` year for each city. The first run
should confirm that each migrated feed returns more episodes than the corresponding capped Granicus
RSS window and that overlapping GUIDs match.

### Step 5 — Audit follow-up

After provider rollout and YAML migrations, run feed-health and classify residual warnings:

- Pflugerville `view-cap` issues (#93-#95, #97-#104) should clear because Legistar has no view-cap
  concept.
- Arlington `view-cap` issues (#23-#25) should clear if the Legistar calendar is usable for Council,
  Planning and Zoning, and all-meetings feeds. If not, the fallback body-specific Granicus views
  should be documented as a partial improvement with residual cap risk.
- Fort Worth Council/Worksession warnings (#192, #193) should improve or clear if Legistar video
  links are usable. Fort Worth aggregate (#90) may remain while most board/commission feeds stay
  Granicus; do not mark #189-#191/#194-#205 as solved by this work unless live verification proves
  those bodies are exposed in Legistar with video links.

---

## Files

| File | Change |
|---|---|
| `citypods/providers/legistar.py` | **New.** `LegistarProvider` class + `_iter_year_pages` + `_parse_calendar_page` + `_extract_aspnet_fields` helpers |
| `citypods/providers/__init__.py` | Import `LegistarProvider`, call `register(LegistarProvider())` |
| `citypods/providers/granicus.py` | Expose `_resolve_download_url` as a module-level function (already has the backoff logic inside `resolve_media_url`; extract so `legistar.py` can import it without coupling to `GranicusProvider` instance) |
| `config/feeds/pflugerville-tx.yml` | Switch to `provider: legistar` |
| `config/feeds/pflugerville-tx-*.yml` (10 files) | Switch to `provider: legistar`; confirm `body:` strings after Step 1 |
| `config/feeds/arlington-tx.yml` | Switch to `provider: legistar` if Arlington calendar verification passes; otherwise document partial Granicus body-view fallback |
| `config/feeds/arlington-tx-council.yml` | Switch to `provider: legistar` after verifying Council body name + Granicus video links |
| `config/feeds/arlington-tx-planning-and-zoning-commission.yml` | Switch to `provider: legistar` after verifying Planning and Zoning body name + Granicus video links |
| `config/feeds/fort-worth-tx-city-council.yml` | Switch to `provider: legistar` after verifying `CITY COUNCIL` video links and GUID overlap |
| `config/feeds/fort-worth-tx-city-council-worksession.yml` | Switch to `provider: legistar` after verifying `CITY COUNCIL WORKSESSION` video links and GUID overlap |
| `config/feeds/fort-worth-tx.yml` | Leave Granicus in the first pass unless mixed Legistar+Granicus source composition is explicitly implemented |
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
| `test_row_view_id_preferred` | row fixture with `view_id=10` and config fallback `view_id=9` | episode GUID/canonical link uses row `view_id=10` |
| `test_config_view_id_fallback` | row fixture with `ID1` but no `view_id` | episode GUID/canonical link uses config fallback `view_id` |

### Live tests (opt-in, `pytest -m live`)

- `test_live_fetch_current_year` — fetch current-year calendar for Pflugerville City Council;
  assert ≥ 1 episode with a valid MediaPlayer URL.
- `test_live_pagination` — fetch a year known to exceed 100 meetings; assert > 100 episodes returned.
- `test_live_arlington_candidate` — if Arlington's Legistar calendar host is usable, fetch current-year
  Council/P&Z rows and assert video links include Granicus clip IDs; otherwise record the host as
  not usable in the implementation PR.
- `test_live_fort_worth_council` — fetch Fort Worth current-year Council/Worksession rows and assert
  at least one video link maps to `fortworthgov.granicus.com`.

---

## Acceptance criteria

- [ ] `citypods doctor pflugerville-tx-city-council` (after YAML migration) returns ≥ 200 episodes
  and no errors.
- [ ] `citypods doctor pflugerville-tx-library-board` returns ≥ 20 episodes going back before the
  current RSS 100-item window.
- [ ] `python scripts/audit_feeds.py --dry-run --city pflugerville-tx-city-council` emits zero
  `view-cap` warnings for the Pflugerville feeds.
- [ ] Arlington verification is recorded: either `citypods doctor arlington-tx-council` and
  `citypods doctor arlington-tx-planning-and-zoning-commission` return more than the capped Granicus
  RSS window with no `view-cap` warning, or the PR documents why Arlington Legistar is not usable and
  applies the body-specific Granicus fallback instead.
- [ ] Fort Worth verification is recorded: `citypods doctor fort-worth-tx-city-council` and
  `citypods doctor fort-worth-tx-city-council-worksession` improve/clear the capped RSS window, or
  the PR documents why Legistar video links are not usable. Board/commission view-cap issues are
  explicitly left Granicus unless verified otherwise.
- [ ] All Legistar unit tests pass (`pytest tests/test_legistar.py`).
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
