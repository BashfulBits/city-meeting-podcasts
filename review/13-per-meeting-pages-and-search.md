# review/13 — Per-Meeting Pages & Static Transcript Search (Phase R)

**Maturity: Part A **L3** · Part B **L3** (both matured 2026-07-13) · breakout of
[`review/11`](11-technical-design-roadmap.md) Phase R · last updated 2026-07-13**

> Two tightly-coupled initiatives: per-meeting permalink pages (#46/GH#157, **ROADMAP R1**) and static
> client-side transcript search (#6, **ROADMAP R4**). Pages are the **product hinge** from "podcast
> feeds" to "civic research tool"; search rides on the page content. Build pages first; search second.
> Issues are **not yet cut** for either part — matured to L3 depth pending a maintainer review pass
> across the whole Phase R sequence before filing.

## Why now (after Phase H)

Today the only per-meeting artifact is a feed `<item>`. A real HTML page per meeting — player +
transcript + chapters/agenda + official + source-time links — is the biggest SEO/sharing win, the
natural home for transcript display, and the surface every later phase (highlights, alerts, AI audio)
links into. It also makes meetings **independently crawlable**, which demotes **directory-metadata**
index sharding (#42). Transcript-search partitioning remains part of the R4 launch design below.

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
  position highlights the line. **Forward-compatible with speaker attribution (reserved for R7, not
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
older pages are orphaned — reachable only by a direct URL or, later, search (R4) — for exactly the
visitor this project is for: someone who wants everything their city council has done, not just the
last N meetings. This must ship as part of R1, not be deferred to R4/search, since it's the primary
human browse path, and search doesn't exist yet when R1 ships.

> **Terminology correction (2026-07-14, prompted by a maintainer question about "body pages"):** a
> `City` config entry — and therefore every page/archive described in this section — is, in the common
> case, already scoped to **one government body**, not one municipality. Confirmed against real configs:
> `config/feeds/austin-tx-city-council.yml`, `config/feeds/austin-tx-design-commission.yml`, etc. are
> ~30 separate `City` entries for Austin alone (Dallas ~26, Fort Worth ~14, Denton ~8), each with its own
> `slug`, each already getting the full existing city-page treatment. **So "body pages" already exist and
> ship today — that's what this section calls "city pages."** They share a `city_entity` field
> (`citypods/models.py:205`, e.g. `austin-tx`, pointing at `config/cities/austin-tx.yml`) that supplies
> shared branding, and the homepage groups them by `podcast_author` into a visual accordion
> (`citypods/site.py:49-77`) — but **no dedicated, crawlable, shareable municipality-wide page exists**
> that aggregates meetings across all of a city's boards into one URL/feed/archive. That's a genuinely
> separate, currently undesigned idea, not part of R1's scope — see review/13 Part B's "City vs. body
> scoping in search" for why R4 search may cover this need without a dedicated page ever being necessary.

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
   `transcript_words_url` (so a diarization-only update to the word-JSON, once R7 lands, still triggers a
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
3. **Reserved for R7, not populated here:** the word-level transcript JSON
   (`transcript_words_url`/`transcript_words_key`) gains two optional fields per cue/word once
   diarization ships — `speaker_id: str | null` (a stable identifier distinct from display name; exact
   format is R7's decision, but it must remain stable across re-diarization so cross-meeting aggregation
   never needs a migration) and `speaker_name: str | null` (human-confirmed display name, `null` if
   unconfirmed — never auto-named, per the diarization sketch's integrity rule). **R1's contract:** the
   client-side transcript component must tolerate both fields being absent (true today) or present (true
   after R7) without a template change — render a speaker label linking to `/speakers/<speaker_id>/`
   when present, plain unattributed text otherwise. R1 does no diarization work; it only must not assume
   these fields can't exist.
4. **Reserved URL convention for R7's per-speaker pages** (not built here): `docs/speakers/<speaker_id>/
   index.html`, mirroring `docs/<slug>/<uid>/index.html`'s shape, so the link-out convention in #3 is
   stable once R7 lands and doesn't require R1's template to change.

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
  (today's shape) and one with them present (R7's future shape) without erroring.
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
both depend on Phase H having already landed stable transcripts + throughput. R1 precedes R4–R9 in the
outer ROADMAP sequence; nothing in R1 depends on R4 or later.

### Acceptance

Every retained meeting record has a stable `…/<uid>/` page, written independently of the current feed
window. Playable meetings provide the working player, synced transcript, agenda/official links,
source-time deep-link, and report-a-problem link. Unavailable-media meetings provide the same known
civic metadata and canonical provenance link, a clear no-recording notice, and no broken player or
podcast enclosure. Unchanged meetings do not re-render (verified per-episode, not just per-city); an
archived episode outside the feed window still updates its page when it changes; feed-window changes do
not delete archive pages; forks can disable the feature. The transcript component renders correctly
against both today's word-JSON shape and R7's future speaker-attribution shape. **Every city has a
discoverable browse path to its full archive** — the "Recent meetings" list links to meeting pages, and
a "Browse the full archive" page lists every retained meeting, not just the current feed window — so a
visitor interested in one city can reach any of its meetings without needing search (R4) to exist yet.
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
   and the `/speakers/<speaker_id>/` link convention, so R7 has a stable target.
6. City-level entry point: link "Recent meetings" titles to meeting pages, add
   `render_city_archive_page` + `templates/city_archive.html.j2` + the "Browse the full archive" link on
   the city page.
7. Chapter-click seek-to-timestamp wiring in the transcript/player JS component (exact visual treatment
   TBD, behavior required).
8. "Copy quote + link" affordance: our page URL (with `#t=`) as the primary copied link, provider
   source-time deep-link as a secondary reference only.

---

## Part B — Static transcript search (#6)

**Maturity: L3 · matured 2026-07-13, grounded against current `main` — see exploration citations
inline. ROADMAP R4. Issues not yet cut, per the batch-review hold.**

### Design (chosen approach)

A **client-side** search over a generated JSON index — no server until proven insufficient. Index
documents = all retained meetings, including metadata-only unavailable recordings, with fields:
`title`, `body`, `city`, `date`, `media_availability`, **chapter/agenda-item titles** (own field,
timestamped — see clarification below), resource **link labels** (not document text — see
clarification below), **real agenda-document text** (schema reserved, populated once R3 ships — see
clarification below and § Data model deltas), `tags` (schema reserved, always empty until R5 — see
§ Data model deltas), and **transcript text** (tokenized, when coverage allows). Results show snippets
with **timestamps** that deep-link into the meeting page (`…/<uid>/#t=<seconds>`, R1). Filters: city,
body, date range, topic (inert until R5 populates `tags`), and recording availability. A meeting without
a transcript remains discoverable from its civic metadata and chapter titles; its result does not
advertise playback or transcript seeking.

**Three clarifications prompted by review feedback, all real gaps in the original draft:**
- **Chapter/agenda-item titles are searchable and timestamped, distinct from transcript segments.**
  `ep.chapters`/`episode_served_chapters(ep)` (`[{"start": secs, "title": str}]`, confirmed in R1's
  exploration) are often descriptive human-curated labels ("Public Comment — Zoning Variance, 123 Main
  St") — a meaningfully stronger signal than generic transcript text, and, critically, **available
  independent of transcript coverage**. The original draft's field list only implied transcript text as
  the searchable body content, which would have left chapter titles unsearchable and made the
  coverage-gated launch's "titles + agenda text" step 1 weaker than it needs to be. Fixed in § Data model
  deltas #2 below: chapters get their own `chapters: [{title, start}]` array per document.
- **"Agenda/resource link text" was ambiguous and is now precisely scoped: it means the link *labels*
  ("Agenda," "Minutes," "Canonical Video" — from `episode_resource_links(ep)`), not the *content* of the
  agenda document itself.** No code anywhere in this repo extracts text from agenda PDFs today. Link
  labels are boilerplate and contribute almost nothing to search relevance on their own; they're included
  only because the links themselves (as clickable results) are useful, not as searchable content.
- **Updated 2026-07-14 — real agenda text is coming back as an actual search source, via R3.** Real
  agenda-document text extraction is now **ROADMAP R3** (a minimal, extraction-only slice pulled forward
  from Phase F's "Backup-material (packet) analysis," see `review/11` §5.1), sequenced *before* this item
  specifically so it can be a genuine search source, not just chapter titles. Added as a distinct
  `agenda_text` field (§ Data model deltas #2, below) alongside `chapters` and `segments` — additive, not
  a replacement for either, and `null`/absent for any episode where extraction hasn't run or failed for
  that provider's PDF format, in which case a result still falls back to chapter titles the same way it
  does today. The richer Phase-F "what's being proposed" structured/LLM brief is a separate, still-fully
  post-1.0 feature — R3 only extracts raw text, it doesn't synthesize anything.

### Engine decision — reversed from the L2 sketch, with reasoning

**The prior sketch's default lean ("Pagefind unless the spike says otherwise") doesn't survive contact
with the actual codebase.** Confirmed by exploration: this project has **zero existing JS build
pipeline** — no root `package.json`, no bundler, no Node-based build step anywhere; the entire site is
Python + Jinja2 rendering static HTML with small inline vanilla-JS `<script>` blocks (the one
`package.json` in the repo is an unrelated Cloudflare Worker, `workers/granicus-media-proxy/`). Pagefind
requires a Node-based indexing step at build time to produce its fragment set — introducing it means
introducing an entire second build toolchain (Node/npm) into a project that has deliberately stayed
single-toolchain (Python, `pyproject.toml`, no `requirements*.txt` even) through every prior phase.

**MiniSearch, sharded per city/source (already the plan regardless of engine — see § Index strategy),
already delivers Pagefind's main selling point — lazy, bounded-size loading — because the sharding
happens at the Python/JSON-generation layer, not the search-library layer.** A per-city MiniSearch shard
under review/16's 1 MB budget, fetched only when a visitor searches/scopes that city, is not meaningfully
different in loading behavior from a Pagefind fragment for that city; MiniSearch supports fuzzy/prefix
matching and result scoring natively, so the "real search library" argument for Pagefind doesn't hold
either. **Revised lean: MiniSearch, sharded per city/source, vendored as a static JS file (no build
step) — consistent with the project's existing zero-JS-build-pipeline architecture.** Promote to Pagefind
only if the spike (§ below) or production shard sizes/query latency show MiniSearch genuinely can't hold
the line — an evidence-gated trigger, not a default, matching how this catalog already treats other
speculative infra (e.g. the hosted-runner monitoring item in `review/11`).

### Index-generation performance — real difference, and it cuts toward MiniSearch, not away

**Real question the original draft didn't address — "index generation" is actually two different costs,
and the two engines split them differently:**

1. **Content extraction (Python, build time)** — reading records, pulling `transcript_words_url`
   artifacts, assembling per-document JSON. **Identical cost either way** — both engines need this exact
   extraction step (§ Index source above already established the index must be built from records, not
   crawled HTML, so this isn't optional for Pagefind either).
2. **Index-structure construction** — this is where the two diverge:
   - **MiniSearch does this client-side, on every shard load**, in the visitor's browser
     (`MiniSearch.addAll()` parsing the shard JSON into an in-memory searchable structure). Real but
     small for shards inside review/16's 1 MB budget — needs to be part of the spike's latency
     measurement (below), not assumed away.
   - **Pagefind does this server-side, once, at build time**, via its Node CLI, producing pre-optimized
     fragments — client-side cost is then near-zero (lookups against an already-built structure).

**The scaling concern this surfaces, in Pagefind's favor at first glance but actually against it:**
Pagefind's typical operating model indexes an entire site directory in one CLI invocation per build —
I'm not aware of it supporting the kind of **per-shard incremental skip** this codebase relies on
everywhere else (the content-hash gates on feeds/chapters, and R1's own per-uid render-hash cache built
specifically to avoid "one changed meeting re-renders everything"). If that's accurate, Pagefind's
**build-time** cost scales with the *entire* indexed corpus on *every* build, not just what changed —
exactly the "full-state restoration" anti-pattern `review/16` exists to move the project away from
elsewhere in the pipeline. A hand-rolled Python/MiniSearch generator has no such constraint: `citypods/
search.py` can trivially skip regenerating a city's shard when that city's `feed_content_hash`-equivalent
hasn't changed, the same pattern already used everywhere else in this codebase. **I'm reasoning from
Pagefind's documented default usage, not a verified test of its incremental options** — this needs
confirming, not assuming, which is exactly what the spike below now explicitly measures rather than
leaving unmeasured. If it holds, it's a second, independent reason (beyond the no-build-pipeline
argument) that MiniSearch scales *better* for this project's specific update pattern (small frequent
per-city changes, not full-catalog rebuilds), not worse.

### The `spike/static-search-size` spike — defined now, not yet run

Exploration confirmed **this spike does not exist anywhere** — no branch, no script, no results; it was
named as a plan in review/10 and referenced as "the open question" here, but never scoped concretely.
Defining it now so it's a filable issue, not aspiration. **Scope widened per review feedback to cover
generation performance, not just output size** — the original draft only measured bytes:
- **Method:** for each of the ~85 current feeds, fetch that source's real `transcript_words_url`
  artifacts, build the per-city MiniSearch index exactly as § Module/file plan specifies, measure
  compressed (gzip) size per city shard. **Also measure:** (a) wall-clock time for the Python extraction
  step across the full current catalog, as a per-city-average baseline to extrapolate scaling cost; (b)
  client-side `MiniSearch.addAll()` construction time for the largest produced shard, not just query
  latency after construction; (c) confirm or refute Pagefind's incremental-rebuild capability against its
  actual current docs/CLI options (the § above reasons from its documented default behavior, not a
  verified test) — if Pagefind does support scoped/incremental indexing, the build-time scaling argument
  against it weakens and should be revised; (d) **a real municipality-wide scope test against Austin's
  actual ~30-board shard set** (§ City vs. body scoping above) — parallel-fetch-and-progressively-render
  latency, not just single-shard latency, since that's the realistic worst case a "search all of Austin"
  interaction hits today.
- **Output:** a table of city → shard size, flagging any city over review/16's 1 MB soft target or 2 MB
  hard warning; a p50/p95 client-side query latency measurement (index load + `addAll()` construction +
  first keystroke-to-results) against the largest shard produced; the extraction-time baseline and its
  extrapolated cost at 500/1,000 cities (review/16's own scale gates); the Pagefind incremental-rebuild
  finding; time-to-first-result and time-to-complete for the Austin 30-shard municipality-wide case.
- **Decision gate:** if every current shard is comfortably under 1 MB, query latency (including
  client-side index construction) is acceptable (review/16's search-interaction p95 < 200 ms target
  after required partitions load), and extrapolated extraction time stays well inside the Actions
  wall-clock budget at review/16's scale gates, MiniSearch ships as designed. If a shard blows the
  budget, re-open the Pagefind option **for that city specifically** (hybrid, not a wholesale engine
  swap) before assuming the whole approach needs to change.
- This is implementation work (needs real transcript data), not something to execute inside this
  docs-only design pass — it's Proposed issue #1 below, ideally the *first* thing built once R4 issues
  are cut, since it gates the engine choice becoming final.

### Index source: built from records, not scraped from HTML

> **Resolves a contradiction:** Part A keeps the full transcript *out* of the meeting-page HTML (fetched
> client-side from the bucket for large files), but a naive static-site-search crawl of rendered HTML
> would never see transcript text. The index is therefore built **from records**
> (`build_search_index(records)` → `docs/data/search/…`), reading transcript text + per-segment
> timestamps straight from the stored word-JSON (H12) — confirmed exact shape below — **not** from the
> page DOM.

**Word-JSON schema (confirmed, `citypods/asr.py:158-191`, schema version `"2"`):**
```json
{"schema": "2", "basis": "served", "segments": [
  {"start": 12.345, "end": 15.0, "text": "some readable segment text",
   "words": [{"w": "some", "s": 12.345, "e": 12.6}, {"w": "readable", "s": 12.6, "e": 12.9, "p": 0.9821}]}
]}
```
The index consumes `segments[].text` for readable snippets and `segments[].start` (not per-word — a
segment-level timestamp is precise enough for a search hit, per-word granularity is transcript-page-sync's
job, not search's) for the deep-link `#t=`. Fetched via `Episode.transcript_words_url`/
`transcript_words_key` (`citypods/models.py:93-94`) — a field whose own code comment already earmarks it
for exactly this ("per-word timings for server-side search / clips / diarization").

### Index strategy & size budgeting

Transcript text dominates index size. review/16 already specifies concrete budgets (no need to invent
new ones): **city-scoped search partition target < 1 MB compressed, 2 MB hard warning**
(`review/16-scaling-review-plan.md:292-302`); initial search JS+manifest < 200 KB compressed; **no
transcript index downloaded before a search interaction**; search-interaction p95 < 200 ms after
required partitions load; search-worker memory target < 100 MB.
- **Shard the index by city/source**: `docs/data/search/<source_key>.json`, loaded on demand when the
  visitor scopes/searches a city — never one global multi-MB blob.
- Store the segment-level timestamp per hit (not per-token — see schema note above) so a result can jump
  to the moment.
- A lightweight always-loaded manifest (`docs/data/search/manifest.json`: `{source_key, city, body,
  shard_url, size, episode_count, coverage_pct}[]` — **`city`/`body` per entry added per review
  feedback**, see § Per-body scoping below) drives which shard(s) to fetch for a given query scope, kept
  well under the 200 KB initial-payload budget itself.

### City vs. body scoping in search — corrected after checking real config scale

**Correction to the previous draft of this section, prompted by a maintainer question about "body
pages": per-body sharding is the *dominant* case, not a secondary one, and that changes the design, not
just the terminology.** Confirmed against real configs: `config/feeds/` has **~30 separate per-board
`City` entries for Austin alone** (Dallas ~26, Fort Worth ~14, Denton ~8), each its own `source_key`/
shard. A combined single feed covering multiple boards is the exception. This means the field named
`city` in the document/manifest schema (§ Data model deltas #2) must resolve to the shared **municipality
identity** (`city.city_entity or city.slug`, `citypods/models.py:205`) — **not** the per-board
`City.slug`/`podcast_title`, which is what `body` already covers. Fixed: the schema's `city` field is
now explicitly `city.city_entity or city.slug` (falls back to the per-board slug only for the rare city
with no shared entity file).

**The real problem this scale creates, previously understated as "fetch and merge a couple shards":** a
search scoped to "all of Austin" now means resolving **up to ~30 shards** from the manifest, not two or
three. Blocking on all 30 fetches before showing any result would be a real latency problem and would
also defeat the "nothing downloaded before a search interaction" budget discipline if a municipality-wide
scope became the default entry point. **Approach:**
- **Fetch shards for a municipality-wide scope in parallel and render results progressively** as each
  shard resolves, rather than blocking on the full set — a city-wide search shows results within one
  shard's latency, not thirty.
- **A city with many boards is exactly the case worth prioritizing which shards fetch first** — e.g. by
  each board's recent-activity recency or episode count (data already in the manifest's
  `episode_count`) — so the visible results fill in with the highest-signal boards first rather than in
  arbitrary/alphabetical order.
- Within any one already-fetched shard, `body` scoping is still a **client-side filter** on the
  per-document `body` field — unchanged from the original design, just now clearly the common path
  rather than an edge case.

**Answering the actual question — is a dedicated municipality page needed, given search can do this?**
Body pages (per-board) already exist and ship today (§ Part A's City-level entry point). A dedicated
*municipality-wide* static page does not exist and isn't part of R1's or R4's current scope. Whether it's
worth building later depends on how the progressive-fetch UX above actually performs at Austin's real
scale — **that's a real open question, not a settled "obviously overkill,"** because 30 parallel shard
fetches is a meaningfully different cost than the 2-3 I'd originally sized this against. Recommend
deferring that call until the widened `spike/static-search-size` spike (§ above) includes a real
30-shard-municipality case in its latency measurement, rather than deciding now on the wrong scale
assumption.

### Data model deltas (exact)

1. **`docs/data/search/manifest.json`** and **`docs/data/search/<source_key>.json`** — net-new output
   directory (confirmed: no `docs/data/` prefix exists anywhere today). Follows the existing
   `docs/admin/` precedent (`citypods/cli.py:403-412`: `mkdir(parents=True, exist_ok=True)` +
   `write_text`) rather than inventing a new file-writing convention.
2. **Per-shard document schema**: `{uid, title, body, city, date, media_availability_state,
   is_withheld, page_url, links: [{label, url}], tags: [], chapters: [{title, start}], agenda_text:
   str | null, segments: [{text, start}]}`. `chapters` comes from `episode_served_chapters(ep)` —
   populated whenever the episode has chapters, **independent of transcript coverage**, so it's real
   searchable content even in coverage-gated launch's step 1 (titles/metadata only, no transcript).
   `agenda_text` is `null` until **R3** (agenda text extraction) ships and successfully extracts that
   episode's agenda PDF — additive alongside `chapters`, populated per-episode as extraction succeeds
   (no coverage-gate/threshold needed the way transcript segments have one, since there's no equivalent
   "60% of the city" concept for a per-episode extraction success/fail outcome). `segments` (transcript
   text) is the separate, coverage-gated field. `tags` is **always `[]` today** — schema-reserved for R5
   (topic tags don't exist anywhere in the codebase yet; `review/14` is itself still L2→L3 with no
   implemented `Tag`/`TagsStage`) — populating it later is an additive field-fill, not a schema change,
   so R4 doesn't
   block on R5 and R5 doesn't need to touch R4's index-building code, only the data it reads.
3. **Availability handling is not a copy of `feeds.py`'s gating** — `feeds.py:184-185` *excludes*
   withheld episodes from feeds entirely; the search index must do the opposite of exclusion: **always
   include** withheld-media episodes (so they stay discoverable by civic metadata, per the Design section
   above) but set `is_withheld: true` so the frontend result renderer suppresses any "play"/"seek"
   affordance and shows the same no-recording framing R1's meeting page does. Reading the field is free
   (`Episode.media_availability`, already in the persisted record dict, `citypods/records.py:821`); the
   *behavior* at read time is the opposite of the existing precedent, so don't pattern-match it blindly.
4. **`page_url`** — depends on R1's `meeting_page_url` helper (§ Module/file plan) existing; confirms the
   sequencing dependency already stated (search depends on pages).

### Module / file plan (exact)

- `citypods/search.py` — new. `build_search_index(state_dir: Path, cities: list[City], output_dir: Path)
  -> None`: iterates `cities`, loads each city's persisted records via `load_records(state_dir,
  source_key(city))` (`citypods/records.py:368`, the same function the `--no-refresh` render path already
  uses) — **not** in-memory episodes from the per-city render loop, since `CityResult`
  (`citypods/run.py:420-426`) doesn't carry them — converts each via `record_to_episode(rec)`, fetches
  `transcript_words_url` per episode (network/storage read — batch/cache to avoid re-fetching on every
  build), builds the per-shard JSON per § Data model deltas #2, writes `docs/data/search/<source_key>.json`
  + updates `manifest.json`.
- `citypods/run.py` — call `build_search_index(...)` from `_build_impl` (`:1090`) right after
  `_prune_stale_dirs` (`~:1677`), still inside the existing `if do_render:` block (`~:1663-1684`) —
  `state_dir` is already in scope there (assigned `:1168`, live through `:1664`), so no new plumbing to
  reach records.
- Frontend: new `templates/search.html.j2` (or a search block added to `index.html.j2` and
  `city.html.j2`) + a new **vendored, non-built** MiniSearch JS bundle (no npm step — download once,
  commit the file, matching the project's existing no-build-pipeline discipline) wired to a `#q` input.
  **Explicitly not** "extending the existing instant-search" in any architectural sense — exploration
  confirmed today's `index.html.j2` search (`citypods/site.py:49` `render_index`) is a trivial
  `.includes()` substring filter over a small inlined city/feed-metadata JSON blob (`index.html.j2:26`,
  no external file, no index, no transcript awareness at all). Only the visual pattern (a `#q` input,
  live-filtered results) is worth reusing; there is no indexing infrastructure to build on.
- `citypods/config.py` — `search: bool = True`; no `search_engine:` config knob needed now that the
  engine decision above is a one-time build choice, not a runtime toggle (simplify vs. the L2 sketch,
  which proposed a config flag for an unresolved decision that's now resolved).

### Migration / backfill

Same shape as R1: first run after this ships needs to index every currently-retained meeting across the
whole catalog, not just current feed windows — bounded by the same per-shard budget regardless of when a
meeting was recorded. No dedicated backfill workflow; a normal scheduled run pays the cost once. Depends
on R1 having already shipped `meeting_page_url`/per-episode pages to link results into.

### Coverage-gated launch

Because forced alignment is paused and only post-#249/H12 transcripts carry word-level data, transcript
coverage at R4 launch will be patchy across the catalog. Launch in two steps: **(1) titles + chapter/
agenda-item titles + resource link labels** across the whole catalog immediately — always present, zero
transcript dependency, and meaningfully richer than titles alone since chapter titles are often specific
and descriptive; **(2) add transcript text per city once that city's transcript coverage passes ~60%**,
so early results aren't silently missing half a city's meetings. The index is rebuilt per city/shard, so
this is a data-availability gate on shard content, not a code fork — `build_search_index` computes each
city's coverage % from the records it already has in hand and decides per-shard whether to include
transcript segments.

### Tests

`tests/test_search.py`:
- Index build is deterministic and offline (from fixture records + fixture word-JSON, no network).
- A known phrase in a fixture transcript is found with the correct meeting `uid` + segment timestamp.
- Index shards per `source_key`; the manifest correctly maps sources to shard files.
- A withheld-media fixture episode **appears in the index** (`is_withheld: true`) but its result renders
  no play/seek affordance — the inverse-of-`feeds.py` behavior from § Data model deltas #3 needs its own
  explicit test, since copy-pasting the feeds.py exclusion pattern would be the natural (wrong) instinct.
- `tags: []` on every document today; a fixture with a populated `tags` field (simulating R5's future
  output) round-trips without schema changes.
- `agenda_text: null` on a fixture episode without extraction; a fixture with a populated `agenda_text`
  (simulating R3's output) is findable by a phrase present only in the extracted text, not in that
  episode's chapter titles or transcript — proves it's a genuinely additive search source, not just
  schema plumbing.
- Coverage-gated launch: a city under the 60% transcript-coverage threshold gets a titles/metadata-only
  shard (now including chapter titles, not just the episode title) that a city over threshold gets
  transcript segments added to.
- A fixture episode's chapter titles are indexed and findable independent of that episode having any
  transcript segments at all.
- Link labels ("Agenda," "Minutes") appear in the document but a search for agenda-document *content*
  (not present anywhere in fixtures, since it isn't extracted) correctly returns no match — guards
  against a future contributor assuming link text search means document-content search.
- The manifest's `city` field resolves to `city_entity` (not the per-board slug); a fixture with N
  `source_key`s sharing one `city_entity` (the realistic case — Austin-scale, not a 2-shard toy example)
  resolves to all N shards when scoped to that municipality, and to exactly one shard when scoped to one
  body.
- A municipality-wide scope fetch renders results from whichever shards resolve first, without blocking
  on the full shard set — assert partial results appear before the slowest shard in a fixture set
  responds.
- Size stays within the fixture set's expected budget (a coarse regression guard, not a substitute for
  the real spike against production data).

### Risks

- **The `spike/static-search-size` spike must actually run against real transcript data before the
  MiniSearch decision is treated as final** — the reasoning above is sound but unvalidated; budget it as
  the first R4 issue, not an afterthought. This now includes validating or refuting the Pagefind
  incremental-rebuild assumption (§ Index-generation performance) against its actual current docs/CLI,
  not just output size.
- **Word-JSON fetch cost during index build** — `build_search_index` needs every episode's transcript
  artifact, which means N storage reads on every build unless cached; get the caching story right (this
  design doesn't fully specify it — flag for the implementer to check against existing storage-read
  caching patterns, e.g. `SourceCache`, before assuming naive re-fetching is fine).
- **No existing precedent for a vendored (non-npm) JS library in this repo** — confirm the vendoring
  approach (commit the file vs. a documented manual-update process) before assuming it's as simple as it
  sounds.
- **The Pagefind-scales-worse argument is reasoned from documented default behavior, not a verified
  test** — if the spike shows Pagefind does support scoped/incremental builds, the build-time-scaling
  argument in § Index-generation performance weakens and that section needs revising before it's cited as
  settled.

### Sequencing / DAG

R1 → R4 (search depends on pages: results link into `meeting_page_url`). Both depend on Phase H having
landed stable transcripts + throughput. R4 benefits from R5 tags for topic filters but does not require
them (tags ship as an empty-then-populated field, no R4 code change needed when R5 lands). R4 precedes
R5–R9 in the outer ROADMAP sequence. Soundbites (#15, R6) and the Phase E highlights reel consume the
same page/transcript surface later, independent of search itself.

### Acceptance

A keyword present in a meeting transcript is findable from the site, returns a snippet with a segment
timestamp, and the result links to the meeting page seeked to that moment. A keyword present only in a
chapter title is findable even when that meeting has no transcript yet. The index loads incrementally —
a manifest under 200 KB, per-city shards under 1 MB, no shard fetched before a search interaction scopes
to it. Withheld-media meetings remain discoverable by civic metadata with no play/seek affordance in
their result. Search works offline in tests. Coverage-gated launch means no city's results silently omit
transcript hits it should have — either full transcript coverage or an honest metadata-only (now
including chapter titles) result. A search scoped to one government body returns only that body's
meetings. A search scoped to a whole municipality (potentially dozens of boards, per real Austin/Dallas/
Fort Worth scale) shows results progressively as shards resolve rather than blocking on the full set.

### Proposed GitHub issues (not filed — batch review pending)

1. **Run the `spike/static-search-size` spike** against real transcript data (method/output defined
   above) — gates whether the MiniSearch decision ships as final or a hybrid Pagefind fallback is needed
   for specific oversized cities.
2. `citypods/search.py` `build_search_index` + `docs/data/search/` manifest+shard writer, wired into
   `_build_impl` after `_prune_stale_dirs`.
3. Vendored MiniSearch frontend integration (`templates/search.html.j2` or equivalent, `#q` input,
   result rendering with the withheld-media affordance suppression from § Data model deltas #3).
4. Coverage-gated per-city shard content (titles-only vs. titles+transcript based on the ~60% threshold).
5. Index chapter/agenda-item titles as their own searchable, timestamped field, independent of
   transcript coverage.
6. Manifest `city` field resolved to `city_entity` (not per-board slug) + `body` per-document field +
   frontend scope-resolution logic (shard set for a municipality — potentially dozens of shards at
   Austin/Dallas/Fort Worth scale — vs. a single body's shard).
7. Progressive parallel-fetch-and-render for municipality-wide search scope, prioritized by each board's
   `episode_count`, so results appear incrementally rather than blocking on the full shard set.

---

## Sequencing & dependencies

Pages (A) → search (B). Both depend on Phase H landing (stable transcripts + throughput). Search depends
on pages (results link into them) and benefits from tags (#4, review/14) for topic filters but does not
require them. Soundbites (#15) and the highlights reel (Phase E) consume the same page/transcript surface
later. Run the `spike/static-search-size` measurement (now concretely defined in Part B above) before
treating the MiniSearch engine choice as final. **Coverage-gated launch is specified in Part B above**
(superseding the shorter note that used to live here).
