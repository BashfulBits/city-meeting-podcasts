# review/13 — Per-Meeting Pages & Static Transcript Search (Phase R)

**Maturity: L2→L3 · breakout of [`review/11`](11-technical-design-roadmap.md) Phase R · last updated
2026-06-21**

> Two tightly-coupled initiatives: per-meeting permalink pages (#46/GH#157) and static client-side
> transcript search (#6). Pages are the **product hinge** from "podcast feeds" to "civic research
> tool"; search rides on the page content. Build pages first; search second.

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

### Design (chosen approach)

A new render output, mirroring the existing chapter-sidecar pattern (review/02 Change 7):
`render_meeting_page(city, ep) -> docs/<slug>/<uid>/index.html`, written in `_process_city` **alongside
the chapter sidecars**, governed by a per-meeting render hash so it costs nothing on unchanged
meetings. Pages project the **append-only meeting archive**, not merely the current feed window:
confirmed-empty/missing/invalid recordings are intentionally absent from podcast feeds but must retain
their research/posterity page. Pruning therefore removes a page only when the durable meeting record is
explicitly removed under an archive policy, never because it fell outside `max_episodes` or lacks an
enclosure. Gated by a config flag (`meeting_pages: true`) so forks can opt out. The page URL is stable
(keyed on the stable `uid`) and is linked from playable feed `<item>` entries, city/archive views, and
search results.

**Page contents** (all already in the record or derivable):
- **Audio player when playable** (the hosted M4A or the source video audio track via range) — reuse
  the city-page inline `<audio>` component. For suspected/confirmed empty, missing, or invalid media,
  render no player and show a clear availability notice with reason, last-check date, and recovery/
  operator-review state; never imply that the meeting itself did not occur.
- **Synced transcript** — render the served-time VTT/JSON; clicking a line seeks the player; the player
  position highlights the line.
- **Chapters / agenda items** — the served-time chapters; each links to its agenda-packet page where
  available.
- **Official links** — agenda / minutes / canonical video (from `links`), plus the city's
  `meetings_url`/`city_website` (#51, shipped). The canonical city watch page remains visible for an
  unavailable recording as provenance/manual confirmation, while expiring signed media URLs do not.
- **Source-time deep-links** — "watch this moment on the city's archive" via the provider
  `video_deeplink(ref, t)` capability, forward-mapping served→source time through the EDL
  (`timeline.served_to_source`).
- **Shareable deep-links** — `#t=<seconds>` fragment that deep-links a player position; a "copy quote +
  link" affordance on transcript selections (the served timestamp + source deep-link).
- **"Report a problem"** — links to the #56 issue template, prefilled with slug + uid.

### Data model / module plan

- H16 PR3 adds the optional versioned `media_availability` projection and redacted evidence pointer;
  the page is otherwise a pure projection of the existing `EpisodeRecord` (audio, transcript,
  chapters, links, timeline). The page does not fetch or expose private diagnostic evidence. If a page
  needs the served-time transcript inline and the transcript is stored in the bucket (not `docs/`),
  it either (a) references the bucket URL and fetches client-side, or (b) inlines a compact transcript
  JSON at render. **Lean: (a)** for large transcripts, with a small inline excerpt for SEO/no-JS.
- Files: `citypods/render.py`/`site.py` (`render_meeting_page`), new `templates/meeting.html.j2`
  (extends `base.html.j2`), `citypods/run.py` (`_process_city` writes/prunes the page under the existing
  skip logic), `citypods/feeds.py`/`feed.xml.j2` (point `<item><link>` at the page), `citypods/config.py`
  (`meeting_pages` flag).

### Implementation paths

1. **Server-rendered static page + client-side transcript fetch** (preferred) — page is fully static
   HTML; transcript + sync handled by a small vanilla-JS component that fetches the VTT and binds to the
   `<audio>` `timeupdate`. No build-time transcript inlining for big files; SEO via an inlined summary +
   first-N-lines excerpt.
2. **Fully inlined transcript** — simplest, best SEO/no-JS, but bloats `docs/` (100–150 KB/meeting ×
   thousands). Acceptable at current scale; revisit at 1,000+ feeds.
3. **Hybrid** — inline a truncated/served-time excerpt + chapters for SEO and no-JS; lazy-load the full
   transcript. **Recommended end state** (start with path 1, add the SEO excerpt = path 3).

### Tests

`tests/test_render.py`/snapshot: a playable meeting page renders with player, chapters, links, and
deep-links; an unavailable-media archive record renders the notice plus metadata and canonical source
page with no player; unavailable meetings remain absent from podcast feeds; an unchanged page skips
render and archive retention—not feed capping—governs pruning; `meeting_pages: false` writes nothing;
a deep-link maps served time to source time through a non-identity EDL.

### Acceptance

Every retained meeting record has a stable `…/<uid>/` page. Playable meetings provide the working
player, synced transcript, agenda/official links, source-time deep-link, and report-a-problem link.
Unavailable-media meetings provide the same known civic metadata and canonical provenance link, a
clear no-recording notice, and no broken player or podcast enclosure. Unchanged meetings do not
re-render, feed-window changes do not delete archive pages, and forks can disable the feature.

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
