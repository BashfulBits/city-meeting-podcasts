# review/13 — Per-Meeting Pages & Static Transcript Search (Phase R)

**Maturity: Part A **L3** (matured 2026-07-13) · Part B **L2→L3** (pending its own maturation pass) ·
breakout of [`review/11`](11-technical-design-roadmap.md) Phase R · last updated 2026-07-13**

> Two tightly-coupled initiatives: per-meeting permalink pages (#46/GH#157, **ROADMAP R1**) and static
> client-side transcript search (#6, **ROADMAP R2**). Pages are the **product hinge** from "podcast
> feeds" to "civic research tool"; search rides on the page content. Build pages first; search second.
> Issues are **not yet cut** for Part A — matured to L3 depth pending a maintainer review pass across the
> whole Phase R sequence before filing.

## Why now (after Phase H)

Today the only per-meeting artifact is a feed `<item>`. A real HTML page per meeting — player +
transcript + chapters/agenda + official + source-time links — is the biggest SEO/sharing win, the
natural home for transcript display, and the surface every later phase (highlights, alerts, AI audio)
links into. It also makes meetings **independently crawlable**, which demotes **directory-metadata**
index sharding (#42). Transcript-search partitioning remains part of the R2 launch design below.

Prerequisite that is now satisfied: transcripts exist (ASR #1 shipped), chapters/agenda links exist, the
timeline EDL can produce **source-time deep-links** (`video_deeplink` capability, review/08), and the
render/enrich split means pages render cheaply from known state.

---

## Part A — Per-meeting permalink pages (#46/GH#157)

**Maturity: L3 · matured 2026-07-13, grounded against current `main` (not just the prior L2 sketch) —
see the exploration citations inline below. ROADMAP R1.**

### Design (chosen approach)

A new render output, `render_meeting_page(city, ep, base_url, *, site_config=None) ->
docs/<slug>/<uid>/index.html`, written in `_process_city` **alongside the chapter sidecars**
(`_write_chapter_sidecars`, `citypods/run.py:1903-1918`), governed by a **new per-episode render hash**
(§ Data model deltas) — this is presented as "mirroring the chapter-sidecar pattern" in the prior sketch,
which is true at the file-io level but **not** at the invalidation level, and that difference matters
enough to call out now rather than discover during implementation (§ Pipeline changes). Pages project
the **append-only meeting archive**, not merely the current feed window: confirmed-empty/missing/invalid
recordings are intentionally absent from podcast feeds but must retain their research/posterity page.
Pruning therefore removes a page only when the durable meeting record is explicitly removed under an
archive policy, never because it fell outside `max_episodes` or lacks an enclosure. Gated by a config
flag (`meeting_pages: true`, default on) so forks can opt out. The page URL is stable (keyed on the
stable `uid`) and is linked from playable feed `<item>` entries, city/archive views, and search results.

**Page contents** (all already in the record or derivable):
- **Audio player when playable** (the hosted M4A or the source video audio track via range) — reuse
  the city-page inline `<audio>` component pattern from `render_city_page` (`citypods/site.py:80-116`).
  For suspected/confirmed empty, missing, or invalid media (`ep.media_availability.is_withheld()`,
  `citypods/availability.py:97-146`), render no player and show a clear availability notice with reason,
  last-check date, and recovery/operator-review state; never imply that the meeting itself did not occur.
- **Synced transcript** — render the served-time VTT/JSON; clicking a line seeks the player; the player
  position highlights the line. **Forward-compatible with speaker attribution (reserved for R5, not
  built here)** — see § Data model deltas #3.
- **Chapters / agenda items** — the served-time chapters; **each entry is clickable to seek the player to
  that chapter's timestamp** (`chapters[i].start`, already on the record — no new data, this is a
  rendering/JS requirement), the same in-page sync behavior the transcript gets, not just an outbound
  link. The agenda-packet link (where available) is a **separate, distinct affordance** next to the
  seek control, since it navigates away from the page while seeking stays on it — same in-page/
  out-of-page split the "Official links" and "Source-time deep-links" bullets below already draw.
  **Exact visual treatment (icon vs. clickable timestamp vs. clickable title) is TBD**, but the
  seek-to-timestamp wiring itself is required in this design, not deferred — see § Tests/Acceptance.
- **Official links** — agenda / minutes / canonical video (`ep.links`, label map
  `citypods/feeds.py:25-34`, resolved via `episode_resource_links(ep)`, `citypods/feeds.py:73-78`), plus
  the city's `meetings_url`/`city_website` (#51, shipped). The canonical city watch page remains visible
  for an unavailable recording as provenance/manual confirmation, while expiring signed media URLs do
  not.
- **Source-time deep-links** — "watch this moment on the city's archive." Exact call chain (confirmed
  new integration — see § Risks): `timeline.served_to_source(ep.timeline, t) -> (source_id, source_t)`
  (`citypods/timeline.py:223-243`) → look up the matching `SourceMedia` in `ep.sources` by `.id` →
  `provider.video_deeplink(source.ref, source_t)` (Protocol `citypods/providers/base.py:88-99`, gated by
  `"deeplink" in provider.capabilities`; implemented for Granicus/Swagit/CivicPlus/CivicClerk). This is
  exactly the pattern `citypods/clips.py:18-23` already documents for clip extraction — reuse it, don't
  reinvent it.
- **Shareable deep-links** — `#t=<seconds>` fragment that deep-links a player position on **our own
  meeting page**. **Our page URL is always the primary shared link, not the provider's** — the point is
  to drive traffic/adoption to the site, not hand it to the source portal. The "copy quote + link"
  affordance on transcript selections copies text of the shape `"<quote>" — <city> <body>, <date> at
  <timestamp> — <our page url>#t=<seconds> (source: <provider deep link>)`: our URL is the link, the
  provider's source-time deep-link is included as a secondary "source:" reference in the copied text,
  never the primary target. This is the same ordering the separate "Source-time deep-links" bullet above
  already implies (it's presented as *its own*, secondary affordance — "watch this moment on the city's
  archive" — not the thing being shared by default).
- **"Report a problem"** — links to the #56 issue template, prefilled with slug + uid.

### City-level entry point (added 2026-07-13 — gap found during this design pass)

**Problem.** The city index page (`render_city_page`, `citypods/site.py:80-116` → `templates/city.html.j2`
→ `docs/<slug>/index.html`) already exists and is unrelated to Phase R — but it renders `feed_eps`, the
**capped current feed window** (`citypods/run.py:542-544`, `feed_eps[:city.max_episodes]`; the section is
literally headed "Recent meetings," `templates/city.html.j2:29`). R1 pages exist for the **full
append-only archive**, deliberately including meetings older than the cap. Without a change here, those
older pages are orphaned — reachable only by a direct URL or, later, search (R2) — for exactly the
visitor this project is for: someone who wants everything their city council has done, not just the
last N meetings. This must ship as part of R1, not be deferred to R2/search, since it's the primary
human browse path, and search doesn't exist yet when R1 ships.

**Approach — two changes, not one:**

1. **Link each episode in the existing "Recent meetings" list to its meeting page.** `render_city_page`'s
   `episode_view` dict (`citypods/site.py:95-104`) currently carries `title`, `duration`, `audio`,
   `links` — no `uid`/page URL at all. Add `"page_url": meeting_page_url(city, e, base_url)`. In
   `templates/city.html.j2:39`, wrap `{{ e.title }}` in `<a href="{{ e.page_url }}">` (or add an explicit
   "Meeting page" link alongside the existing inline play button — **lean: wrap the title**, since the
   play button stays the fast-path for podcast-style inline listening and the title becomes the research
   path, matching how the rest of the page already separates those two affordances).
2. **Add a distinct "browse full archive" view per city**, new `docs/<slug>/archive/index.html`, listing
   **every retained episode** (not capped), each entry: title, date, availability-state badge (so an
   unavailable-media entry is visibly marked before the visitor clicks through — consistent with "never
   imply the meeting didn't occur," the archive list must not just silently omit or silently look normal),
   and a link to its meeting page. New `render_city_archive_page(city, base_url, episodes, *,
   site_config=None) -> str` in `citypods/site.py`, new `templates/city_archive.html.j2` (extends
   `base.html.j2`, structurally similar to `city.html.j2`'s list but without the play buttons/subscribe
   block — this page's job is browsing, not podcast-app subscription). The existing city page gets one
   added line: a "Browse the full archive →" link near the "Recent meetings" heading.
   **Rejected alternative:** just pass the full archive into the existing `render_city_page` instead of
   adding a second page. Rejected because it conflates two different jobs (fast podcast-subscriber feed
   view vs. full research browse) into one page, and would make the primary city page unboundedly large
   for a city with a deep archive — the same "don't inline everything into one page" reasoning already
   applied to transcripts (Implementation paths, below) applies here.

**Data source — resolves an open question from the original draft of this design.** `_process_city`
already has the full per-city archive in scope *before* it gets capped: `episodes` at
`citypods/run.py:522/525/529` (from `pipeline.render_from_records`/`.enrich`/`.archive_from_records`) is
the full archived set; `feed_eps` (`:542-544`) is `filter_by_body(episodes, city.source.get("body"))`
**then** capped to `max_episodes`. So the archive view's input is simply `filter_by_body(episodes,
city.source.get("body"))` **without** the `[:city.max_episodes]` slice — no new data access needed, just
reusing the pre-cap value that's already computed and in scope. This also answers the open question the
original Migration/backfill note below raised about how `_write_meeting_pages` reaches the full retained
set: same value, same place, compute once and pass to both.

**Pagination:** none initially — at today's catalog scale (~85 feeds, modest per-city archive depth) an
unpaginated list is fine. Flag for pagination if a single city's archive page HTML exceeds roughly the
same size discipline `review/16` already applies to search partitions (soft target well under 1 MB); not
a near-term concern, don't build pagination speculatively ahead of that.

### Data model deltas (exact)

1. **New per-episode render hash** — `meeting_page_hash(ep: Episode) -> str` in `citypods/records.py`,
   next to `audio_spec_hash` (`:254-306`), `transcript_media_hash` (`:309-320`), `feed_content_hash`
   (`:328-355`), following the same convention those three already use: a `spec` dict →
   `json.dumps(spec, separators=(",", ":"), sort_keys=True)` → `hashlib.sha256(...).hexdigest()` (full
   digest, unlike the truncated `audio_spec_hash`/`transcript_media_hash` — a collision here silently
   skips a page rewrite, which is worse than an artifact-key collision, so don't truncate; matches
   `feed_content_hash`'s precedent). `spec` fields: `uid`, `episode_served_chapters(ep)`, `ep.links`,
   `ep.summary`/`ep.description`, `transcript_hosted_url`, `transcript_synced`, `transcript_basis`,
   `transcript_words_url` (so a diarization-only update to the word-JSON, once R5 lands, still triggers a
   page re-render), served/source duration, `ep.media_availability.effective_state()` if set else
   `None`, and `timeline_digest(ep.timeline)` if `ep.timeline` else `""` (so deep-link offsets stay in
   sync with the EDL).
2. **New per-episode render cache**, parallel to but distinct from the existing city-level
   `cache[city.slug]["content_hash"]` gate (`citypods/run.py:573-591`): a new cache namespace (e.g.
   `cache["_pages"][ep.uid] = {"hash": meeting_page_hash(ep)}`) persisted in the same cache file the
   city-level gate already uses (confirm the existing cache's persistence mechanism at implementation
   time; do not introduce a second cache file). This is a deliberate deviation from the coarser
   city-level gate `_write_chapter_sidecars` rides on today, because at hundreds of retained
   episodes/city, one changed meeting rewriting every page in the city on every run is a real cost, not
   a theoretical one.
3. **Reserved for R5, not populated here:** the word-level transcript JSON
   (`transcript_words_url`/`transcript_words_key`) gains two optional fields per cue/word once
   diarization ships — `speaker_id: str | null` (a stable identifier distinct from display name; exact
   format is R5's decision, but it must remain stable across re-diarization so cross-meeting aggregation
   never needs a migration) and `speaker_name: str | null` (human-confirmed display name, `null` if
   unconfirmed — never auto-named, per the diarization sketch's integrity rule). **R1's contract:** the
   client-side transcript component must tolerate both fields being absent (true today) or present (true
   after R5) without a template change — render a speaker label linking to `/speakers/<speaker_id>/`
   when present, plain unattributed text otherwise. R1 does no diarization work; it only must not assume
   these fields can't exist.
4. **Reserved URL convention for R5's per-speaker pages** (not built here): `docs/speakers/<speaker_id>/
   index.html`, mirroring `docs/<slug>/<uid>/index.html`'s shape, so the link-out convention in #3 is
   stable once R5 lands and doesn't require R1's template to change.

### Module / file plan (exact)

- `citypods/records.py` — new `meeting_page_hash(ep: Episode) -> str` (~line 320, beside its three
  siblings).
- `citypods/site.py` — new `render_meeting_page(city: City, ep: Episode, base_url: str, *,
  site_config: dict | None = None) -> str`, next to `render_city_page` (`:80-116`), building a flat
  view-model dict the same way `render_city_page` does today (durations via the existing `_duration()`
  helper, links via `episode_resource_links(ep)`), rather than passing `Episode`/`City` objects into the
  template directly.
- `citypods/templates/meeting.html.j2` — new, `{% extends "base.html.j2" %}` (same shell `city.html.j2`
  uses — dark-mode vars, `.wrap`/`.btn`, footer), containing: player block, a transcript-mount `<div>` +
  small vanilla-JS fetch/sync component (Implementation path 1 below), chapters list, official links,
  deep-link buttons, report-a-problem link, and an availability-notice block rendered when
  `ep.media_availability.is_withheld()`.
- `citypods/run.py` — new `_write_meeting_pages(city_dir: Path, city: City, episodes: list[Episode],
  base_url: str, site_config: dict, page_cache: dict) -> dict` (returns the updated per-uid hash cache
  from Data model delta #2), called from `_process_city` immediately after `_write_chapter_sidecars`
  (`:597`) — same "collect the wanted `<uid>/` set → write changed pages → glob + prune stale
  directories" shape as `_write_chapter_sidecars`, but keyed by the new per-episode hash cache, not the
  city-level `content_hash` gate. **This is the one structural difference from "mirrors the chapter
  sidecar pattern"** and is load-bearing — see § Pipeline changes for why the city-level gate can't be
  reused as-is.
- `citypods/feeds.py` — `build_rss`'s item-building loop (~`:240-250`) gains `item.page_url =
  meeting_page_url(city, ep, base_url)`; `templates/feed.xml.j2` gains a guarded `<link>{{
  item.page_url }}</link>` line. **This is a net-new `<item>` element, not a redirect of an existing
  one** — the current `<item>` block has no `<link>` at all (only the channel-level `<link>` exists,
  pointing at `city_url`).
- `citypods/config.py` — `meeting_pages: bool = True`.
- `citypods/site.py` — `render_city_page`'s `episode_view` (`:95-104`) gains a `page_url` field per
  episode; new `render_city_archive_page(city: City, base_url: str, episodes: list[Episode], *,
  site_config: dict | None = None) -> str`.
- `templates/city.html.j2` — wrap the `{{ e.title }}` at line 39 in a link to `e.page_url`; add a
  "Browse the full archive →" link near the "Recent meetings" heading (line 29).
- `templates/city_archive.html.j2` — new, full-archive browse list (title, date, availability badge,
  link), no play buttons/subscribe block.

### Pipeline / stage changes

Insertion point: `citypods/run.py:597`, directly after `_write_chapter_sidecars(...)` and before
`build_rss(...)` (which needs `page_url`, computed deterministically from `uid` regardless of whether
this run actually wrote new bytes for that page).

**A real edge case the exploration surfaced, not just a design nuance.** `_process_city`'s outer skip
gate (`content_hash = feed_content_hash(feed_eps, fingerprint)` + `_city_outputs_exist`, `:573-591`) can
skip the **entire render block** for a city, including `_write_meeting_pages`. `feed_content_hash` is
computed over `feed_eps` — the capped/filtered **current feed window** — not the full retained archive.
Meeting pages are supposed to project the append-only archive, including episodes outside
`max_episodes`. So: a change to an *archived* episode's chapters/links/transcript (outside the feed
window) would never flip the outer gate, and that episode's page would never update, even though the
per-uid page hash (delta #1) would correctly detect the change if it ever got the chance to run.
**Decision: decouple `_write_meeting_pages` from the outer city-level skip gate entirely** — call it
unconditionally on every run for every city (cheap, because the per-uid hash cache from delta #2 makes
unchanged pages a no-op check, not a re-render). Keep the outer gate governing only the feed/index-page
render block, which legitimately is scoped to the feed window. **Data source resolved** (was an open
question in the original draft of this design; closed by the City-level entry point section above):
`_write_meeting_pages` and `render_city_archive_page` both consume `filter_by_body(episodes,
city.source.get("body"))` — the same pre-cap value `feed_eps` (`citypods/run.py:542-544`) is derived
from, just without the `[:city.max_episodes]` slice — computed once in `_process_city` and passed to
both, no new data access needed.

### Migration / backfill

First run after this ships needs a page for every currently-retained meeting record across the whole
catalog (not just the current feed window), plus one new archive page per city — at today's scale (~85
feeds, more per-city archive depth than the feed window) this could be several hundred meeting pages on
one run. Because each page write is a cheap static render and the per-uid cache means only page 1 pays
full cost, **no dedicated backfill workflow is needed** — this ships as a normal scheduled run, just a
longer one the first time.

### Implementation paths

1. **Server-rendered static page + client-side transcript fetch** (preferred) — page is fully static
   HTML; transcript + sync handled by a small vanilla-JS component that fetches the VTT/word-JSON and
   binds to the `<audio>` `timeupdate`. No build-time transcript inlining for big files; SEO via an
   inlined summary + first-N-lines excerpt.
2. **Fully inlined transcript** — simplest, best SEO/no-JS, but bloats `docs/` (100–150 KB/meeting ×
   thousands). Acceptable at current scale; revisit at 1,000+ feeds.
3. **Hybrid** — inline a truncated/served-time excerpt + chapters for SEO and no-JS; lazy-load the full
   transcript. **Recommended end state** (start with path 1, add the SEO excerpt = path 3).

### Tests

`tests/test_render.py`/snapshot, extending the existing suite's fixtures:
- A playable meeting page renders with player, chapters, links, and deep-links.
- An unavailable-media archive record (`is_withheld()` true) renders the notice plus metadata and
  canonical source page, no player, no podcast enclosure — and **the page is still written** (this is
  the case most likely to regress into "skip the page because media is withheld," which would be wrong).
- `meeting_page_hash` changes when, and only when, a spec field changes — one test per field to catch
  a forgotten field, one test confirming an unrelated field change (e.g. a coordination-only telemetry
  field) does *not* change the hash.
- **Per-uid cache isolation**: changing one episode's chapters does not cause a sibling episode's page
  to be rewritten (mtime/write-count assertion) — this is the regression the per-uid cache exists to
  prevent, so it needs its own explicit test, not just an implicit pass.
- **Archive-window decoupling**: an episode outside `feed_eps` (beyond `max_episodes`) still gets its
  page written/updated on a run where only that episode changed and the city-level `feed_content_hash`
  gate would otherwise skip the whole city.
- `meeting_pages: false` writes nothing.
- A deep-link maps served time to source time through a non-identity EDL (concat/trim), asserting against
  `timeline.served_to_source`'s documented half-open-interval convention (note: review/09's INFRA-1 audit
  flagged a boundary-inversion asymmetry between `served_to_source`/`source_to_served` at concat seams —
  pin the exact expected behavior at the seam in this test rather than leaving it implicit).
- The transcript component tolerates a fixture word-JSON with no `speaker_id`/`speaker_name` fields
  (today's shape) and one with them present (R5's future shape) without erroring.
- The city page's "Recent meetings" list links each episode title to its meeting page.
- `render_city_archive_page` lists every retained episode for a city (not capped by `max_episodes`),
  including withheld-media entries with a visible availability badge, each linking to its meeting page.
- An episode outside the feed window appears on the archive page but not the "Recent meetings" list.
- Clicking a chapter entry seeks the player to `chapters[i].start`; the chapter's agenda-packet link
  (where present) remains a separate control that navigates away rather than seeking.
- The "copy quote + link" affordance produces our page URL (with `#t=` fragment) as the primary link and
  the provider source-time deep-link only as a secondary "source:" reference in the copied text — assert
  the provider URL is never the primary/first URL in the copied string.

### Risks

- **`served_to_source` → `video_deeplink` has zero production callers today** (only test call sites and
  one contract-probe call). This is genuinely new integration, not a refactor of a proven path — budget
  real time for surprises, including the concat-boundary edge case review/09 already flagged.
- **The per-uid render cache is new plumbing** with no existing precedent in this codebase (every
  existing hash-gated artifact rides a coarser, already-built gate). Get its persistence right against
  the real cache file/format rather than inventing a second source of truth.
- **The archive-window/outer-gate decoupling must be resolved during implementation, not discovered
  after** — shipping `_write_meeting_pages` still wired to the outer `feed_content_hash` gate would look
  correct in every test that only touches the feed window and silently fail to update archived-episode
  pages in production.

### Sequencing / DAG

Within Part A: `_write_chapter_sidecars` → `_write_meeting_pages` (needs nothing from chapter sidecars,
just sequenced immediately after by file-layout convention) → `build_rss` (needs each episode's
`page_url`) → index/city page render. Across the catalog: Part A (this) before Part B (search, next);
both depend on Phase H having already landed stable transcripts + throughput. R1 precedes R2–R7 in the
outer ROADMAP sequence; nothing in R1 depends on R2 or later.

### Acceptance

Every retained meeting record has a stable `…/<uid>/` page, written independently of the current feed
window. Playable meetings provide the working player, synced transcript, agenda/official links,
source-time deep-link, and report-a-problem link. Unavailable-media meetings provide the same known
civic metadata and canonical provenance link, a clear no-recording notice, and no broken player or
podcast enclosure. Unchanged meetings do not re-render (verified per-episode, not just per-city); an
archived episode outside the feed window still updates its page when it changes; feed-window changes do
not delete archive pages; forks can disable the feature. The transcript component renders correctly
against both today's word-JSON shape and R5's future speaker-attribution shape. **Every city has a
discoverable browse path to its full archive** — the "Recent meetings" list links to meeting pages, and
a "Browse the full archive" page lists every retained meeting, not just the current feed window — so a
visitor interested in one city can reach any of its meetings without needing search (R2) to exist yet.
Chapters seek the player in-page in addition to any outbound agenda-packet link. Sharing a quote or
timestamp always produces our page URL as the primary link, with the provider's source-time deep-link
only as a secondary reference — the site is the thing that gets shared and gains adoption, not the
source portal.

### Proposed GitHub issues (not filed — batch review pending)

1. `meeting_page_hash` + per-uid render cache (`citypods/records.py`, `citypods/run.py`).
2. `render_meeting_page` + `templates/meeting.html.j2` + `_write_meeting_pages` wiring in
   `_process_city`, decoupled from the outer `feed_content_hash` gate.
3. Source-time deep-link chain (`served_to_source` → `SourceMedia` lookup → `video_deeplink`) as its own
   testable unit, given it has no production callers today.
4. `<item><link>` in `feeds.py`/`feed.xml.j2` pointing at the meeting page.
5. Reserve (schema-only, no behavior) the `speaker_id`/`speaker_name` fields in the word-JSON contract
   and the `/speakers/<speaker_id>/` link convention, so R5 has a stable target.
6. City-level entry point: link "Recent meetings" titles to meeting pages, add
   `render_city_archive_page` + `templates/city_archive.html.j2` + the "Browse the full archive" link on
   the city page.
7. Chapter-click seek-to-timestamp wiring in the transcript/player JS component (exact visual treatment
   TBD, behavior required).
8. "Copy quote + link" affordance: our page URL (with `#t=`) as the primary copied link, provider
   source-time deep-link as a secondary reference only.

---

## Part B — Static transcript search (#6)

### Design (chosen approach)

A **client-side** search over a generated JSON index — no server until proven insufficient. Index
documents = all retained meetings, including metadata-only unavailable recordings, with fields:
`title`, `body`, `city`, `date`, `media_availability`, `agenda/resource link text`,
`topic tags` (#4 when available), and **transcript text** (tokenized). Results show snippets with
**transcript timestamps** that deep-link into the meeting page (`…/<uid>/#t=<seconds>`). Filters: city,
body, date range, topic, and recording availability. A meeting without a transcript remains
discoverable from its civic metadata; its result does not advertise playback or transcript seeking.

### Availability review and future query surfaces

H16 PR3 supplies the current availability projection, versioned re-evaluation inputs, redacted
evidence pointer, and weekly GitHub digest. Phase R adds the product/operations surfaces deliberately
left out of that pipeline PR:

- `/admin/status` counts and drill-down by availability reason/state, due-for-recheck status, and
  recovered/overridden outcome;
- auditable operator actions to confirm empty, mark valid, request immediate re-evaluation, or record
  a city-side correction;
- availability/history browsing and evidence comparison without exposing private diagnostic objects
  on public meeting pages;
- the storage-neutral event/history query path described in `review/11` and `review/17`.

### Index source: built from records, not scraped from HTML

> **Resolves a contradiction:** Part A keeps the full transcript *out* of the meeting-page HTML (fetched
> client-side from the bucket for large files), but Pagefind's default mode indexes *rendered HTML* — so a
> naive Pagefind crawl would never see transcript text. The index is therefore built **from records**
> (`build_search_index(records)` → `docs/data/search/…`), engine-agnostic: transcript text + per-segment
> timestamps come straight from the stored VTT / word-JSON (review/12 H12), **not** from the page DOM. If
> Pagefind is chosen, use its **Node indexing API** to feed these custom records rather than its HTML
> crawler; MiniSearch consumes the same records directly. This keeps the engine decision (below) orthogonal
> to where the transcript lives.

### Index strategy & size budgeting

Transcript text dominates index size, so this is a **size-management** problem (the open question in
`spike/static-search-size`). Approach:
- **Shard the index by city/source** (`docs/data/search/<source>.json` or a Pagefind-style fragment
  set), loaded **on demand** when the user scopes/searches a city — not one global multi-MB blob.
- Prefer a library that supports **lazy/partial index loading**: **Pagefind** (builds a static,
  fragmented index designed exactly for large static sites; loads only matching fragments) is the strong
  default; **MiniSearch/Lunr** (single in-memory index) is fine for a single city or small forks but
  doesn't scale to the whole catalog.
- Store **timestamps per token-span** so a hit can jump to the moment (use the served-time transcript;
  the page handles the seek).

### Module / file plan

- A build step that emits the index from records: `citypods/search.py` (`build_search_index(records)`),
  invoked in `build()` after render (or a `citypods search-index` subcommand), writing under
  `docs/data/search/`.
- Frontend: a search UI on the index page + per-city page (extend the existing instant-search), wired to
  Pagefind's runtime (or MiniSearch for the small-fork path); results link to meeting pages.
- Config: `search_engine: pagefind|minisearch` (default chosen after the size spike), `search: true`.

### Implementation paths

1. **Pagefind** (recommended) — fragmented static index, lazy loading, scales to the full catalog; adds
   a build dependency. 2. **MiniSearch sharded per city** — pure-JS, no build dep, but the client loads a
   whole city's index; OK at current scale. 3. **Hybrid** — Pagefind for the global/all-cities search,
   MiniSearch for the small single-fork case. **Lean: validate with the size spike, then (1).**

### Tests

`tests/test_search.py`: index build is deterministic and offline (from fixture records); a known phrase
in a fixture transcript is found with the correct meeting uid + timestamp; index shards per source; size
stays within budget for the fixture set.

### Acceptance

A keyword present in a meeting transcript is findable from the site, returns a snippet with a timestamp,
and the result links to the meeting page seeked to that moment; the index loads incrementally (no
multi-MB blob on first paint); search works offline in tests.

---

## Sequencing & dependencies

Pages (A) → search (B). Both depend on Phase H landing (stable transcripts + throughput). Search depends
on pages (results link into them) and benefits from tags (#4, review/14) for topic filters but does not
require them. Soundbites (#15) and the highlights reel (Phase E) consume the same page/transcript surface
later. Run the `spike/static-search-size` measurement before committing the search engine choice.

**Coverage-gated launch.** Because forced alignment is paused and only post-#249 / H12 transcripts carry
word-level data, transcript coverage at R2 time will be patchy. Launch search in two steps: (1)
**titles + agenda/resource text** across the whole catalog immediately (always present); (2) **add
transcript text per city as that city's transcript coverage passes a threshold** (~60 %), so early
results aren't silently missing half a city's meetings. The index is rebuilt per city, so this is a
data-availability gate, not a code fork.
