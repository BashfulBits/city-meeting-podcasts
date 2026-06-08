# review/13 — Per-Meeting Pages & Static Transcript Search (Phase R)

**Maturity: L2→L3 · breakout of [`review/11`](11-technical-design-roadmap.md) Phase R · last updated
2026-06-08**

> Two tightly-coupled initiatives: per-meeting permalink pages (#46/GH#157) and static client-side
> transcript search (#6). Pages are the **product hinge** from "podcast feeds" to "civic research
> tool"; search rides on the page content. Build pages first; search second.

## Why now (after Phase H)

Today the only per-meeting artifact is a feed `<item>`. A real HTML page per meeting — player +
transcript + chapters/agenda + official + source-time links — is the biggest SEO/sharing win, the
natural home for transcript display, and the surface every later phase (highlights, alerts, AI audio)
links into. It also makes meetings **independently crawlable**, which demotes index sharding (#42).

Prerequisite that is now satisfied: transcripts exist (ASR #1 shipped), chapters/agenda links exist, the
timeline EDL can produce **source-time deep-links** (`video_deeplink` capability, review/08), and the
render/enrich split means pages render cheaply from known state.

---

## Part A — Per-meeting permalink pages (#46/GH#157)

### Design (chosen approach)

A new render output, mirroring the existing chapter-sidecar pattern (review/02 Change 7):
`render_meeting_page(city, ep) -> docs/<slug>/<uid>/index.html`, written in `_process_city` **alongside
the chapter sidecars**, governed by the **same `feed_content_hash` skip + prune logic** so it costs
nothing on unchanged meetings and is GC'd with the feed. Gated by a config flag (`meeting_pages: true`)
so forks can opt out. The page URL is stable (keyed on the stable `uid`) and is linked from the feed
`<item>` (`<link>`) and the city page.

**Page contents** (all already in the record or derivable):
- **Audio player** (the hosted M4A or the source video audio track via range) — reuse the city-page
  inline `<audio>` component.
- **Synced transcript** — render the served-time VTT/JSON; clicking a line seeks the player; the player
  position highlights the line.
- **Chapters / agenda items** — the served-time chapters; each links to its agenda-packet page where
  available.
- **Official links** — agenda / minutes / canonical video (from `links`), plus the city's
  `meetings_url`/`city_website` (#51, shipped).
- **Source-time deep-links** — "watch this moment on the city's archive" via the provider
  `video_deeplink(ref, t)` capability, forward-mapping served→source time through the EDL
  (`timeline.served_to_source`).
- **Shareable deep-links** — `#t=<seconds>` fragment that deep-links a player position; a "copy quote +
  link" affordance on transcript selections (the served timestamp + source deep-link).
- **"Report a problem"** — links to the #56 issue template, prefilled with slug + uid.

### Data model / module plan

- No record-schema change required — the page is a pure projection of an existing `EpisodeRecord`
  (audio, transcript, chapters, links, timeline). If a page needs the served-time transcript inline and
  the transcript is stored in the bucket (not `docs/`), the page either (a) references the bucket URL and
  fetches client-side, or (b) inlines a compact transcript JSON at render. **Lean: (a)** for large
  transcripts (keeps `docs/` small), with a small inline excerpt for SEO/no-JS.
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

`tests/test_render.py`/snapshot: a meeting page renders with player, chapters, links, deep-links;
skip/prune respects `feed_content_hash`; `meeting_pages: false` writes nothing; deep-link maps a served
timestamp to the correct source time through a non-identity EDL (trimmed meeting).

### Acceptance

Every rendered feed item has a stable `…/<uid>/` page with working player, synced transcript, agenda +
official links, a source-time deep-link verified against a trimmed-audio EDL, and a report-a-problem
link; unchanged meetings don't re-render; forks can disable it.

---

## Part B — Static transcript search (#6)

### Design (chosen approach)

A **client-side** search over a generated JSON index — no server until proven insufficient. Index
documents = meetings, with fields: `title`, `body`, `city`, `date`, `agenda/resource link text`,
`topic tags` (#4 when available), and **transcript text** (tokenized). Results show snippets with
**transcript timestamps** that deep-link into the meeting page (`…/<uid>/#t=<seconds>`). Filters: city,
body, date range, topic.

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
