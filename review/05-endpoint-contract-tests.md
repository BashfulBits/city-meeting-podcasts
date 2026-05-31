# External Endpoint Inventory & Contract-Test Plan

Every provider integration is a scrape or undocumented API. When a city's platform changes its
HTML/JSON, the parser silently returns fewer/zero items and we find out via the *feed-health audit*
(a symptom, downstream) instead of a *contract test* that names the exact broken pattern. This doc
inventories every external endpoint and proposes a layered test/monitor strategy.

## 1. Endpoint inventory (what the code hits and the shape it depends on)

| Provider | Endpoint (pattern) | Method | Shape we depend on | Code |
|---|---|---|---|---|
| Granicus | `ViewPublisherRSS.php?view_id=N` | GET/HEAD | RSS `<item>`: enclosure/`media:content` url, `<link>` MediaPlayer, `pubDate`, `itunes:duration` | `granicus.parse_feed` |
| Granicus | `JSON.php?clip_id=N` | GET | nested list of `{time,type,text:"Agenda:…",title}` | `granicus.parse_index_json` |
| Granicus | `AgendaViewer.php?view_id&clip_id` | (synthesized link) | 302 → agenda doc | `granicus._episode_links` |
| Granicus | `DownloadFile.php?view_id&clip_id` | (enclosure) | progressive MP4 | enclosure |
| Swagit | `…/views/…` or list page | GET | table rows `<a href="/videos/ID">Body</a> … <td nowrap>Date</td>` | `swagit.parse_list` (`ROW_RE`) |
| Swagit | `/videos/ID` | GET | `<a class="playerControl" data-ts data-end-ts data-title href="/play/ID/ts">` + `/videos/ID/transcript` link | `swagit.parse_chapters` / `fetch_chapters` |
| Swagit | `/videos/ID/download` | GET (no-redirect) | 302 → presigned MP4 | `swagit.resolve_media_url` |
| CivicPlus | `RSSFeed.aspx?ModID=92&CID=…` | GET/HEAD | RSS `<item>` with `<link>` watch page (`?VID=`) | `civicplus.parse_civicmedia_feed` |
| CivicPlus | watch page `?VID=` | GET | contains `tikiliveapi.com/embed?…videoId=` | `civicplus._find_embed_url` (`EMBED_RE`) |
| CivicPlus | `tikiliveapi.com/embed?…` | GET | contains `…​.m3u8` | `civicplus._find_hls_url` (`M3U8_RE`) |
| CivicClerk | `/v1/Events?$filter=hasMedia…` | GET | OData JSON: `value[]` with `mediaSourcePathMp4` (abs), `publishedFiles[]`, `startDateTime` | `civicclerk.parse_events` |
| CivicClerk | `/v1/EventsMedia/{id}` | GET | `eventBookmarks[]{markerTimeStart,markerTitle}`, `transcriptionUrl`/`closedCaptionUrl` | `civicclerk.parse_bookmarks` |
| CivicClerk | `/v1/Meetings/GetMeetingFileStream(fileId=…)` | (synthesized link) | PDF stream | `civicclerk._file_stream_url` |
| Scripts | Wikipedia/Commons `w/api.php` | GET | image/color extraction | `fetch_seals` (offline tool) |
| Storage | B2/R2 S3 API | head/put/get/list/delete | object ops | `storage/s3.py` |

## 2. The problem with the current tests

The offline fixtures + snapshot tests are excellent for **parser regression** — they prove
`parse_*` still works on the *recorded* bytes. But:
- They can't detect when the **live** site changes shape (the recorded fixture is frozen).
- Several **newly added** endpoints have no recorded fixture at all: Granicus `JSON.php`, Swagit
  `/videos/ID` chapter page, CivicClerk `EventsMedia`. Their parsers (`parse_index_json`,
  `parse_chapters`, `parse_bookmarks`) are tested only against hand-written synthetic HTML/JSON in
  this session's PRs — good, but not against a real recorded sample.

## 3. Proposed three-layer strategy

### Layer 1 — Record real fixtures for the new endpoints (offline regression)
Extend `scripts/refresh_fixtures.py` to also capture, for one representative city per provider:
- Granicus `JSON.php?clip_id=…` → `tests/fixtures/granicus/<slug>.index.json`
- Swagit `/videos/<id>` → `tests/fixtures/swagit/<slug>.video.html`
- CivicClerk `/v1/EventsMedia/<id>` → `tests/fixtures/civicclerk/<slug>.media.json`
Then add offline parser tests that run `parse_index_json` / `parse_chapters` / `parse_bookmarks`
on the **real recorded** bytes and assert non-empty, well-formed output. This catches "we changed
the parser and broke it" with real data.

### Layer 2 — Live contract tests (`@pytest.mark.live`, opt-in, NOT in default CI)
A `tests/live/test_contracts.py` marked `live` that hits **one** representative live URL per
endpoint and asserts the *minimal shape* we depend on — e.g.:
- Granicus: `ViewPublisherRSS` returns ≥1 item with an enclosure; `JSON.php` returns ≥1
  `Agenda:`-prefixed entry with `time`+`title`.
- Swagit: list page yields ≥1 `(id, body, date)` row; `/videos/<id>` yields ≥1 `playerControl`
  with `data-ts`; `/download` 302s.
- CivicPlus: RSS yields a watch link; the watch page contains a TikiLive embed; the embed contains
  an `.m3u8`.
- CivicClerk: `Events` returns `value[]` with an absolute `mediaSourcePathMp4`; `EventsMedia/{id}`
  returns `eventBookmarks`.

Run via `pytest -m live` (a `pyproject` marker, `addopts = -m "not live"` so default runs skip it).
A dedicated **weekly** workflow (`contracts.yml`, `workflow_dispatch` + cron) runs `-m live` and, on
failure, files/updates a GitHub issue per broken endpoint (reuse the `audit_feeds` reconcile logic).
This is the early-warning system: it tells you *which URL pattern broke* before feeds degrade.

### Layer 3 — A `scripts/check_endpoints.py` monitor (operational)
A standalone script that, for each provider, runs the Layer-2 assertions against the **actual cities
in `cities/`** (not just one sample) and prints a per-endpoint PASS/FAIL matrix. Wire it into
`contracts.yml`. Distinct from the feed-health audit (which checks *output* — empty/stale feeds);
this checks *inputs* — the upstream contracts. Together: audit catches "feed went bad", contracts
catch "provider changed their API".

## 4. Why a separate suite (not in PR CI)

Live tests are flaky by nature (network, rate limits, gov sites with maintenance windows) and would
make PR CI nondeterministic. Keeping them `-m live` + scheduled gives the signal without the flake.
The offline Layer-1 tests *do* run in PR CI (deterministic, from fixtures).

## 5. Concrete deliverables (phase 3 branch `review/endpoint-tests`)

1. `pyproject.toml`: register `live` marker + `addopts = ["-m", "not live"]`.
2. `tests/live/test_contracts.py` — the marked live assertions (Layer 2).
3. `tests/test_scripts_import.py` — imports every `scripts/*.py` (catches B1-class breakage).
4. Extend `refresh_fixtures.py` to record the three new endpoint fixtures (Layer 1), and add offline
   parser tests against them once recorded.
5. `scripts/check_endpoints.py` + `.github/workflows/contracts.yml` (weekly + dispatch, files issues).

I'll implement 1–3 and 5 directly (they don't need network at build time). For 4, I'll add the
recording code + the offline tests *guarded to skip if the fixture isn't present yet*, and record the
fixtures live during this run if the network is reachable, so they're committed too.
