# review/15 — Cross-Provider Agenda & History Network (was: Legistar Calendar Provider)

**Maturity: L3 · re-scoped and matured 2026-07-16 · breakout of
[`review/11`](11-technical-design-roadmap.md) Phase R · ROADMAP R11 (numbered out of table-position on
purpose, per the no-renumbering convention — sequenced third, right after R10, above R3) · issues not
yet cut, batch review pending**

> **Re-scoped 2026-07-16 (maintainer decision):** this item grows from "Legistar calendar scraping for
> historical Granicus video coverage only" into a general **cross-provider agenda & history network**
> with three goals: (1) ingest URLs for **non-PDF (HTML/portal) agendas**, (2) ingest URLs for **PDF
> agendas**, (3) **extend meeting history** for feeds with limited RSS/API windows — generalized beyond
> Legistar/Granicus to the other sibling-vendor relationships research turned up (§0). **R3 (agenda text
> extraction) now depends on this item** and narrows to "extract text from whatever URL this item
> already found" — R11 owns URL *discovery*, R3 owns text *extraction*. The original Legistar/Granicus
> design (proven, already L3) is preserved below as **Part A**; everything else is new.

---

## §0. Three goals, and the relationships that make them possible

1. **Ingest URLs for non-PDF (HTML/portal) agendas** — structured agenda-portal pages (Legistar
   AgendaViewer, OneMeeting/PrimeGov portal, CivicClerk portal) are richer than a bare PDF: item-level
   metadata, sometimes even video-sync points (see the Waco example below).
2. **Ingest URLs for PDF agendas** — the original R3 scope, generalized: discover the actual document
   link, wherever it lives, for R3 to extract text from.
3. **Extend meeting histories for feeds with limited history** — generalizes Part A's existing
   Legistar-for-Granicus mechanism (below) to the other sibling relationships, where viable.

### §0.1 The corporate relationships — candidates, not a fixed mapping (corrected 2026-07-16)

**Correction, same day as the original draft: the relationships below name the *most likely* sibling
per video provider, not a guaranteed or exclusive pairing.** A city's agenda-system vendor is an
*independent procurement decision* from its video-system vendor — nothing about corporate ownership
compels a city to buy the "matching" sibling product, or to buy any of them at all. **Confirmed with a
real city already in this catalog**: Denton, TX (`denton-tx`, all 9 feeds configured `provider:
swagit`) is understood to expose its agendas through **Legistar/Granicus** — Granicus's own product,
not Rock Solid's OneMeeting — despite Swagit being its video source. So the "expected" Swagit→OneMeeting
pairing (§0.1's original framing) does not hold universally, and the design below must not assume it
does.

| Video provider we ingest today | Owns it | Most-likely candidate sibling(s) | Portal/API pattern | Confirmed |
|---|---|---|---|---|
| **Granicus** | Granicus (since 2011, via Daystar Systems acquisition) | **Legistar** | `{org}.legistar.com/Calendar.aspx` | Long-standing, deeply integrated — Part A already works |
| **Swagit** | Granicus, via **Rock Solid Technologies** (acquired Oct 2022) | **OneMeeting** (formerly PrimeGov) *or* **Legistar** — both are viable, a given Swagit city could have either, neither, or (in principle) both | `portal-{org}.primegov.com` / `{org}.legistar.com/Calendar.aspx` | OneMeeting: real production example (Waco, below). Legistar: **Denton, TX, in this catalog** |
| **CivicPlus** | CivicPlus (since 2017, via BoardSync acquisition, rebranded CivicClerk) | **CivicClerk** | `{tenant}.api.civicclerk.com` (OData JSON API — this project's existing `civicclerk.py` already speaks it) | Same parent company; product bundling, not a technical integration |

**Design consequence: discovery must probe every known candidate system for a city, not assume the
"corporate sibling" and stop there.** §B.2 below revises the verification methodology accordingly —
Part A's own Legistar mechanism is the thing to try *first* for a Swagit city too, not just OneMeeting,
precisely because Denton already proves that pairing exists in this exact catalog.

**Concrete proof OneMeeting↔Swagit is also real, not just a theoretical candidate:** the City of Waco
moved to "a web-based OneMeeting agenda portal and uses Swagit for live streaming," where OneMeeting's
agenda page has "on-screen 'play' links that open the Swagit player" and **item-level jump points into
the Swagit video." Both this and the Denton/Legistar case are real — the point isn't that one is right
and the other wrong, it's that **either can be true for any given Swagit city**, so the design can't
hard-code one assumption.

### §0.2 The joining key already exists — this doesn't need new fuzzy-matching

The hardest-looking part of this design — matching a sibling agenda source's rows to the *right*
episode from a different, primary provider — turns out to already have a clean answer in this codebase.
`Episode.uid` is computed by `_uid(author, body, date, seq)` (`citypods/records.py:229-238`) as
`SHA1(author | body_key(canonical_body(body)) | date | sequence-within-that-(body,date)-bucket)` — this
is **provider-independent by construction**: it depends only on the meeting's own facts (who, what
body, what date, which same-day occurrence), never on any provider-specific field like `guid`. If an
auxiliary source can independently compute `(canonical_body, date)` for its own rows — using the exact
same `canonical_body`/`body_key` normalization already in `citypods/bodies.py:48-78` — it derives the
**same uid** the primary provider's matching episode already has. No new string-similarity/fuzzy-match
library is needed (confirmed: none exists in this codebase today); the join key is already the identity
key.

**The one real risk this doesn't eliminate:** if a body has more than one meeting on the same date, the
`seq` (same-day sequence number) has to agree between the primary provider and the auxiliary source, or
the uids diverge even though `(body, date)` matches. Sequencing by start-time-of-day (when both sources
expose it) is the mitigation; this needs explicit handling, not an assumption that it always lines up.

### §0.3 Which mechanism solves which goal — a deliberate scoping split

Two complementary mechanisms, not one unified system — this is a deliberate risk-reduction choice, not
a shortcut:

| Mechanism | Solves | Status | Risk profile |
|---|---|---|---|
| **A. Full-replacement** (`city.provider` switches entirely to the sibling, e.g. `legistar`) | **Goal 3** (extended history) | Proven — Part A already does this for Legistar/Granicus | Low — the sibling *becomes* the episode-discovery source of truth, no cross-source reconciliation needed |
| **B. Auxiliary attachment** (a new, optional second source enriches the primary's already-discovered episodes) | **Goals 1 + 2** (agenda URLs) | New — Part B | Higher — needs the new uid-join reconciliation (§0.2), but **deliberately never creates new episodes**, only enriches existing ones |

**Why not make auxiliary mode also solve goal 3** (i.e., auto-promote an aux-only-discovered meeting
into a full episode)? Because that requires the record-store to accept episodes whose identity/state
isn't owned by the primary provider's own fetch — a genuinely new and riskier form of cross-source
episode *creation*, not just enrichment. Full-replacement mode already solves goal 3 safely, proven in
production for Legistar/Granicus; there's no need to take on that risk a second way when one is already
working.

---

## Part A — Legistar, full-replacement mode (existing, proven — solves Goal 3 for Granicus/Legistar)

**Everything in Part A is the original design, unchanged, still fully valid.** It's Mechanism A (§0.3):
`city.provider` switches entirely to `legistar`, which becomes the episode-discovery source of truth
while delegating media resolution back to Granicus. **Also usable in Mechanism B (auxiliary) for a
Granicus-primary city that doesn't want a full migration** — the same `AgendaViewer.php`/`Calendar.aspx`
scraping this section describes can run as an auxiliary source instead (see Part B), attaching agenda
links without switching `provider:`.

### Why now (after Phase H)

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

**Re-verified live 2026-07-16 — the inventory above is confirmed still current, and the migration
described in this Part appears designed but never actually executed.** The specific issue numbers cited
above are now closed, but not because the underlying problem was fixed — H4's feed-health reporting
consolidated from one issue per `(slug, check)` into one issue per check over the same period
([`review/11`](11-technical-design-roadmap.md) §4 H4 row), so the old per-feed issues closed as a
reporting-format change, not a resolution. Checked GitHub directly: the current consolidated view-cap
issue ([#776](https://github.com/BashfulBits/city-meeting-podcasts/issues/776), open since 2026-07-01,
last updated 2026-07-12 — 11 days after this Part's original design, and still 15 days before this
re-scoping pass) reports the identical set — **the same 33 Granicus feeds, unchanged**: Arlington ×3
(`arlington-tx`, `-council`, `-planning-and-zoning-commission`), Denton County ×1 (still present in the
audit's findings despite this Part's own decision that Legistar isn't the right fix for it — the audit
doesn't know that), Fort Worth ×18, Pflugerville ×11. (Predecessor issues
[#315](https://github.com/BashfulBits/city-meeting-podcasts/issues/315) and
[#400](https://github.com/BashfulBits/city-meeting-podcasts/issues/400), both closed, carry the full
per-feed breakdown before the reporting format compressed further — cross-referenced here since #776's
own body now only shows a truncated example, not the full list.) **No new cities have appeared** —
Part A's original city-level scope (Pflugerville, Arlington, Fort Worth) is still the complete,
currently-accurate target set. **The practical implication:** this Part's migration plan doesn't need
new design work — it needs *execution*. It's the one piece of R11 that's ready to ship today with zero
further design, unlike Parts B/C/D below.

**Not Phase F #31.** The existing Phase F item "#31 Legistar provider (rich agendas/votes/rosters)"
is about the **InSite REST API** — structured meeting packets, vote tallies, and roster data used for
Phase F pre-meeting foresight and Phase R #3/#8 cards. That is a separate adapter and a separate
milestone. This provider is about **calendar scraping for Granicus video coverage only**; the two can
coexist (a city could have both the InSite API adapter and a calendar-based episode index), but they
are independent.

---

### How Legistar Calendar.aspx works

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

### GUID and Episode construction

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

### Source config schema

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

### Provider protocol mapping

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

### HTML parsing

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

### fetch_episodes implementation sketch

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

### Migration plan

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

### Files

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

### Test plan

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

### Acceptance criteria (Part A)

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

---

## Part B — Auxiliary agenda-source attachment (new mechanism — solves Goals 1 + 2)

**Mechanism B from §0.3.** A city keeps its primary video provider unchanged and gains a second,
optional source that enriches already-discovered episodes with agenda URLs — never creates new
episodes (that's Mechanism A's job, §0.3).

### §B.1 Architecture

- **`MeetingProvider` Protocol is reused as-is for auxiliary sources** — confirmed it has exactly 7
  members (`citypods/providers/base.py:33-99`): `name`, `capabilities`, `validate`, `detect_change`,
  `fetch_episodes`, `resolve_media_url`, `video_deeplink`. `fetch_chapters`/`fetch_view_counts` are
  optional duck-typed extensions (always `getattr`-guarded by callers), not part of the Protocol. An
  auxiliary-only source implements all 7 for Protocol conformance, but the video-specific ones
  (`resolve_media_url`, `video_deeplink`) are harmless no-ops — nothing calls them for an auxiliary
  source, since it's never the one materializing media. `detect_change` can trivially `return None`
  (confirmed dead code in practice — zero call sites outside a docstring mention — but still required to
  satisfy the `runtime_checkable` Protocol's method-presence check). **No new Protocol needed** — this
  reuses existing plumbing rather than inventing a parallel one.
- **New optional `City` fields**: `aux_provider: str | None`, `aux_source: dict | None`
  (`citypods/models.py`, alongside `provider`/`source` at lines 197-198). Loaded and validated the same
  way as the primary (`get_provider(...).validate(...)`) in `citypods/config.py`'s `_build_city`
  (currently lines 85-178) — but optional, absent for every city today, so no config migration needed
  for cities that don't use this.
- **Insertion point: `SourcePipeline.fetch_merge`** (`citypods/run.py:189-205`), the exact and only
  place `provider.fetch_episodes(source)` is called in the live pipeline (line 194). After the existing
  `assign_uids(city, episodes)` call (line 200) — which is what makes the join key (§0.2) available —
  add: if `city.aux_provider` is set, call `aux_provider.fetch_episodes(city.aux_source)`, compute the
  same uid for each returned row (via the same `canonical_body`/`body_key`/date/seq logic), and reconcile
  against `episodes` by uid match.
- **No new record store / `source_key`.** `source_key(city)` (`citypods/records.py:158-163`) hashes only
  the primary `provider`+`source` — deliberately unchanged. The auxiliary fetch is ephemeral per run
  (enrich-in-place), not persisted as its own source of truth. This keeps the primary provider as the
  sole identity/state owner, consistent with Mechanism B never creating episodes.
- **New reconciliation function** — `citypods/records.py` (or a new small module) — this doesn't fit
  `merge_persisted` (which only hydrates a provider's *own* prior state, confirmed) or `merge_records`/
  `merge_seed_episodes` (same-source dedup, confirmed) — needs new code:
  ```python
  def attach_auxiliary_agenda_links(
      episodes: list[Episode], aux_episodes: list[Episode]
  ) -> None:
      """Enrich `episodes` in place with agenda links from `aux_episodes`, matched by uid.
      Never adds or removes episodes -- unmatched aux rows are dropped, not promoted."""
  ```
- **New link keys** — `links["agenda"]` already exists (PDF/PDF-redirect target). Add
  `links["agenda_portal"]` for the structured HTML/portal page (Legistar AgendaViewer, OneMeeting
  portal, CivicClerk portal) — distinct from the raw PDF, matching Goal 1 vs Goal 2. **Naming note**:
  `citypods/feeds.py:31`'s `LINK_LABELS` already has an unused `"documents": "Meeting documents"` entry
  — different namespace (display label vs. link dict key) so no actual collision, but worth reusing that
  label for whichever new key ends up user-facing, rather than inventing a second "documents" concept.
- **`feed_content_hash`** (`citypods/records.py:328-358`) already includes `sorted((e.links or {}).items())`
  (line 346) — a new agenda link populating means this hash naturally changes and triggers a re-render;
  no separate wiring needed, this already works.

### §B.2 Discovery methodology — probe every known candidate, per city, before configuring

**Corrected 2026-07-16, direct consequence of the Denton finding (§0.1):** `city.aux_provider` is a
single, explicit, human-verified config value at runtime (kept simple deliberately — no live
auto-probing on every scheduled run, matching Part A's own "verify before committing to YAML" discipline
rather than a runtime guessing game). But the **verification step that decides what to put in that
config value must try every known candidate system for that city, not just the "expected" corporate
sibling** — Denton (Swagit-primary) is confirmed to use Legistar, not OneMeeting, which is exactly the
pairing §0.1's original draft would have skipped checking.

Per-city discovery checklist (run once, before writing `aux_provider`/`aux_source` into a feed's YAML,
mirroring Part A's existing "Step 1 — Verify body names against live Legistar" discipline, generalized):

1. Try `{slug}.legistar.com/Calendar.aspx` — does it resolve, and does it list real meetings for this
   city's bodies? (Part A's mechanism already handles this fully once found.)
2. Try `portal-{slug}.primegov.com` — does it resolve, and does it carry agenda content for this city?
   (Part C's mechanism, once built.)
3. Try `{slug}.api.civicclerk.com` (or the tenant name if it differs from the city slug) — does it
   resolve, and does it return real event data? (Part D's mechanism, once built.)
4. If more than one candidate resolves for the same city, prefer whichever has better coverage
   (checked the same way Part A's migration table already compares "more episodes than the capped
   window" per candidate) — there's no a priori reason to prefer one system over another once multiple
   are confirmed viable for the same city.
5. If none resolve, the city stays on chapter-title-only agenda proxying (today's status quo) — not a
   regression, just no improvement available yet.

**Denton, TX is the first concrete target for this checklist** — a real Swagit-primary city in this
catalog with a plausible Legistar pairing, using a mechanism (Part A's) that's already fully built and
proven, unlike the OneMeeting case which still needs live HTML verification (Part C). This should be the
first city this feature is tried against, both because it's low-implementation-risk (no new adapter
code needed, just the new auxiliary-mode wiring from §B.1) and because it directly tests whether the
"probe every candidate" methodology finds real coverage the original single-sibling assumption would
have missed entirely.

### §B.3 Tests

- A fixture primary-provider episode list + a fixture aux-provider episode list sharing a `(body, date)`
  pair reconcile to the same uid and the primary episode gains `links["agenda"]`/`links["agenda_portal"]`.
- An aux row with no matching primary uid is dropped, not promoted to a new episode (the core Mechanism-B
  invariant — needs an explicit test, not just an implicit pass).
- Same-body-same-date multiple-meetings fixture: sequencing agreement (or documented disagreement)
  between primary and aux sources is exercised explicitly, not left untested.
- `City.aux_provider`/`aux_source` absent (today's default for every city) — `fetch_merge` behaves
  identically to before this change (regression guard).

---

## Part C — OneMeeting provider (new — one of two known candidate siblings for Swagit, not the only one)

**Corrected 2026-07-16: this is *a* candidate for Swagit cities, not *the* one.** §0.1's Denton finding
means a Swagit city might use OneMeeting, might use Legistar (already fully covered by Part A/B with no
new code), might use neither. Build this because OneMeeting↔Swagit is confirmed real in at least one
production case (Waco), not because it's assumed to be every Swagit city's answer — the discovery
checklist (§B.2) is what actually decides which mechanism applies to which city, this Part just makes
OneMeeting one of the checklist's viable options once built.

**Confirmed facts (2026-07-16 research):** public portal pattern `portal-{org}.primegov.com` (OneMeeting
is the current name for what was PrimeGov before Rock Solid's rebrand). Real production integration
exists today (the Waco example, §0.1) — OneMeeting pages link directly to the Swagit player, including
item-level jump points, which is structurally the same "embedded foreign-provider reference" pattern
Legistar already uses for Granicus clip IDs (Part A, "Granicus clip_id extraction").

### §C.1 Design — template lifted from Part A's Legistar mechanism, not invented fresh

Part A's clip_id-extraction mechanism generalizes cleanly: (a) identify the interactive/embedded-widget
attribute carrying the foreign provider's reference (for Legistar: `onclick="radopen('&lt;url&gt;', ...)"`;
for OneMeeting: **unconfirmed without live HTML** — likely a `data-*` attribute, an `onclick` player-open
call, or an `&lt;iframe src=...&gt;`, needs live verification before implementation, exactly like Part A's
own "verify body names against live Legistar" step), (b) extract the Swagit reference via a narrow regex
against that one attribute, not full HTML parsing, (c) skip rows where the pattern is absent (agenda-only
or cancelled meetings), (d) reconstruct the reference into Swagit's own canonical URL scheme and delegate
media resolution to the existing `SwagitProvider` logic — never re-derive it.

**Two usage modes, both viable, decided per-city:**
- **Full-replacement** (Mechanism A) — for a Swagit city with a genuinely limited RSS/API history window
  and a verified-viable OneMeeting calendar with embedded Swagit references: `city.provider` switches to
  `onemeeting`, mirroring Part A's Legistar migration exactly (same source_key-change/orphan-handling
  concerns, same verification-before-YAML-migration discipline).
- **Auxiliary** (Mechanism B, Part B) — for a Swagit city that just wants agenda URLs attached without a
  full migration: `city.aux_provider = "onemeeting"`.

### §C.2 Module / file plan

- `citypods/providers/onemeeting.py` — new. Structurally mirrors `citypods/providers/legistar.py`:
  `fetch_episodes` (portal scrape + pagination, mechanism TBD pending live HTML verification),
  `resolve_media_url`/`video_deeplink`/`fetch_chapters` delegating to `SwagitProvider`'s existing logic
  (same "extract module-level helper, delegate" refactor pattern Part A already used for
  `granicus._resolve_download_url`).
- `citypods/providers/__init__.py` — register `OneMeetingProvider`.
- **Live verification required before any implementation commitment** (matching Part A's own
  discipline): fetch a real `portal-{org}.primegov.com` page for a Swagit city already in this catalog,
  confirm the portal exists, confirm it embeds a Swagit-resolvable reference, and document the actual
  HTML pattern — none of this can be finalized from research alone the way Part A's Legistar mechanics
  could be (Legistar's markup was already documented via live inspection when Part A was written).

### §C.3 Risks

- **Unverified HTML structure** — everything in §C.1's extraction mechanism is a template by analogy to
  Legistar, not a confirmed pattern. This is the single biggest open risk in the whole document; treat
  Part C as L2 (designed approach, needs verification), not L3 (dev-ready), until a real OneMeeting page
  for a real catalog city is inspected.
- **Not every Swagit city has OneMeeting** — Rock Solid's OneMeeting is one option among several agenda
  systems a Swagit customer might use (or none at all); this generalizes Legistar's own experience (not
  every Granicus city had a usable Legistar calendar either — Part A's own migration table documents
  partial coverage, e.g. Fort Worth's board/commission feeds staying Granicus-only).

---

## Part D — CivicClerk auxiliary agenda index (new — for CivicPlus-video cities)

**Confirmed facts (2026-07-16 research):** CivicClerk's real API is `https://{tenant}.api.civicclerk.com`
(an OData JSON API — **not** the public HTML portal `{siteid}.portal.civicclerk.com` humans browse),
already spoken by this project's existing `citypods/providers/civicclerk.py`. Relevant endpoints:
`GET /v1/Events` (episode list), `GET /v1/Meetings/GetMeetingFileStream(fileId={id},plainText=false)`
(the actual PDF-serving endpoint for agenda/packet/minutes/transcript files, via `_file_stream_url`,
`civicclerk.py:70-72`), and `_published_links`/`_FILE_TYPE_LINKS` (`civicclerk.py:62-86`) — the pure,
already-written function that maps an event's `publishedFiles` to agenda/packet/minutes/transcript URLs.

### §D.1 The real blocker found in this codebase, not hypothetical

`CivicClerkProvider.fetch_episodes`/`parse_events` (`civicclerk.py:89-129`) **unconditionally drops any
event without `hasMedia=true` and a valid absolute-HTTPS `mediaSourcePathMp4`** — before
`_published_links` ever runs. For a CivicPlus-video city, CivicClerk is never the video source, so most
or all of its own events would have `hasMedia=false` from CivicClerk's perspective (the video lives on
CivicPlus, not CivicClerk) — meaning `fetch_episodes` as it exists today **cannot** be reused directly
for auxiliary agenda-only lookups. It would filter out exactly the rows this feature needs.

### §D.2 Design — a new function, not a new provider

Because `_published_links`/`_file_stream_url`/`_FILE_TYPE_LINKS` are already pure functions taking
`(event: dict, api_base: str)`, the fix is narrow: a new sibling function in the same file, not a new
adapter class:

```python
def fetch_agenda_index(source: dict) -> list[AgendaRecord]:
    """Like fetch_episodes, but without the hasMedia/mediaSourcePathMp4 gate -- returns every
    event's agenda/packet/minutes links regardless of whether CivicClerk itself hosts video."""
```
`AgendaRecord` — a new, deliberately lighter dataclass (`{body, date, links}`), not a repurposed
`Episode` — avoids carrying `Episode`'s video-specific fields (`audio_url`, `media_kind`, etc.) into a
context where they're meaningless and could mislead a future reader into treating an agenda-only record
as a playable one.

**Scope: Goals 1 + 2 only, auxiliary mode only (Mechanism B), never Goal 3.** Unlike Legistar/OneMeeting,
CivicClerk doesn't embed a resolvable reference back to CivicPlus's own video hosting — the two products
are independently operated, not one delegating media resolution to the other. So there's no equivalent
of Part A/C's "extract the foreign clip reference, delegate to the primary provider" mechanism here —
CivicClerk can only ever *enrich* an episode CivicPlus already discovered, never help discover one
CivicPlus is missing. This is a real, structural difference from Legistar/OneMeeting, not an
implementation gap to close later.

### §D.3 Module / file plan

- `citypods/providers/civicclerk.py` — add `fetch_agenda_index(source)` + a new `AgendaRecord` dataclass
  (likely `citypods/models.py`, alongside `Episode`). Reuses `_published_links`/`_file_stream_url`/
  `_FILE_TYPE_LINKS` unchanged.
- Wired as an auxiliary source (Part B) for CivicPlus-primary cities: `city.aux_provider = "civicclerk"`.
  **Note this needs its own capability surface** — `aux_provider`'s `fetch_episodes` contract (Part B)
  expects `list[Episode]`, but CivicClerk's auxiliary path naturally returns `list[AgendaRecord]`
  instead. Either (a) `AgendaRecord` needs to satisfy a narrower subset of what Part B's reconciliation
  function actually reads (just enough to compute a uid and carry `links`), or (b) Part B's
  `attach_auxiliary_agenda_links` should be typed to accept either shape. Flagged as an open
  implementation decision, not resolved by this design pass — the CivicClerk case is what surfaces it,
  but the resolution affects Part B's own interface.

### §D.4 Risks

- **CivicClerk coverage for a given CivicPlus city is not guaranteed** — same-parent-company doesn't
  mean every CivicPlus customer also bought CivicClerk. Needs a per-city discovery/verification step
  (does `{tenant}.api.civicclerk.com` resolve and return real data for this city?) before assuming
  coverage, matching Part A/C's own "verify before committing" discipline.
- **The `AgendaRecord` vs. `Episode` shape mismatch (§D.3)** touches Part B's own interface — resolve
  this before implementing Part D in isolation, or Part B's reconciliation function will need a second
  revision immediately after.

---

## §E. Consolidated sequencing, migration, and acceptance (Parts B–D)

**Sequencing:** R11 precedes R3 (agenda text extraction) — R3 narrows to text extraction once this item
supplies URLs. Within R11, in order:

1. **Part A's migration execution, first — it needs zero further design.** Re-verified live 2026-07-16
   (above): the same 33 Granicus feeds (Pflugerville, Arlington, Fort Worth) have been sitting capped
   for a month since this Part was originally designed. This is pure execution against an already-L3
   plan, not new work — it should not wait on Parts B/C/D at all.
2. **Part B (the auxiliary-attachment mechanism) next**, since it's the shared dependency every other
   new Part needs to be usable in auxiliary mode.
3. **Denton (Swagit-primary, Legistar-as-auxiliary) is the first *new-mechanism* target once Part B
   lands** — zero new adapter code (Part A's Legistar mechanism already exists), only the new
   auxiliary-mode wiring, making it the lowest-risk, highest-confidence proof that the discovery
   methodology (§B.2) actually works, ahead of both Part C and Part D.
4. **Part D (CivicClerk, also reuses existing adapter code) follows next.**
5. **Part C (OneMeeting) is gated on live HTML verification and should follow all of the above, not
   lead** — it's still the least-certain of the three sibling systems.

**Migration:** no backfill required — this is additive URL/link enrichment on already-existing episodes,
governed by the same `feed_content_hash` re-render trigger every other link-affecting change already
uses (Part B, §B.1).

**Acceptance (Parts B–D, beyond Part A's own criteria above):** a Granicus-primary city with an
auxiliary Legistar/OneMeeting source gains `links["agenda"]`/`links["agenda_portal"]` on matched
episodes without any change to which provider supplies its video; a CivicPlus-primary city with an
auxiliary CivicClerk source gains agenda links the same way; no auxiliary source ever creates a new
episode; `City` configs without `aux_provider` set behave identically to today (regression guard); R3's
own design (next) can assume agenda URLs are present for the large majority of the current feed index
before beginning its own text-extraction work.

## Proposed GitHub issues (not filed — batch review pending)

1. **Part A migration execution** — re-verified live 2026-07-16: the same 33 Granicus feeds
   (Pflugerville, Arlington, Fort Worth — [#776](https://github.com/BashfulBits/city-meeting-podcasts/issues/776))
   have been capped for a month against an already-L3 plan. Pure execution, no new design, should not
   wait on anything else in this list.
2. Part B: `City.aux_provider`/`aux_source` config + `fetch_merge` insertion point + the new
   `attach_auxiliary_agenda_links` reconciliation function. Everything below depends on it.
3. **Denton verification** (§B.2): run the discovery checklist against `denton-tx`, confirm Legistar
   coverage, wire it as Denton's `aux_provider` — zero new adapter code, the first real proof the
   auxiliary mechanism works end to end.
4. Part D: `civicclerk.py` `fetch_agenda_index` + `AgendaRecord` dataclass (also reuses existing adapter
   code, no live-HTML-verification blocker).
5. Part B: resolve the `AgendaRecord`-vs-`Episode` interface question (§D.3) that Part D's design
   surfaced, before Part D ships.
6. Part C: live verification of a real OneMeeting portal for a real catalog city, before any
   implementation commitment — sequenced last, least certain of the three.
7. Part C: `onemeeting.py` provider (full-replacement + auxiliary modes), gated on issue 6's findings.
