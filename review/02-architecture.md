# Architecture: changes needed to support the roadmap

The good news first: **most of the 50 features need no architectural change.** The
enrichment-stage pipeline + episode-record store + split-hash invalidation already give you
the seams. This doc covers the handful of changes that *are* needed, ordered by leverage.

## Current architecture (baseline, for reference)

```
config (YAML) ──► providers (fetch_episodes) ──► assign_uids ──► merge_persisted(records)
                                                                        │
                       ┌──── enrichment stages (ordered, budgeted) ─────┤
                       │  chapters → audio → links  (transcript/summary slot in here)
                       ▼
                  save_records (bucket = source of truth, via statesync)
                       │
              per-feed render: feed_content_hash skip → build_rss + city page + chapter sidecars
                       │
                  docs/ ──► GitHub Pages ; audio/state ──► B2 (Cloudflare CDN)
```

Key invariants that make this extensible:
- **Provider Protocol + registry** — new platform = new adapter, no core change.
- **Stage Protocol + `default_stages()`** — new per-episode feature = new stage.
- **Split hashes** — `audio_spec_hash` (bytes) vs `feed_content_hash` (RSS) invalidate independently.
- **Content-addressed audio** + **stable uid** — cache-bust, rollback, provider-migration safe.
- **Bucket-as-truth state** — derived artifacts survive cache eviction.

## Change 1 — Stage cost/throughput accounting (prereq for the resource model)

**Why:** Every paid/heavy stage (transcripts, summaries, host-all-audio) needs the projection
model to know its per-episode **bytes added** and **wall-time added**. Today stages report
`StageStats(ran/reused/skipped/errors)` but nothing about *resource* cost.

**Change:** extend `StageStats` (or add a parallel `StageCost`) with:
- `bytes_written` (sum of object sizes uploaded this run),
- `seconds_spent` (wall time inside the stage),
- `units_remaining` (episodes still needing this stage = backlog for this stage).

`run.build()` already aggregates stats per source; have it also aggregate cost into the
per-run `run_summary.json` (Change 2). This is the data spine for doc 03.

**Effort:** S. Non-breaking (additive fields).

## Change 2 — Persisted run history (`state/run_history.jsonl`)

**Why:** monitoring/trends (#36), projection calibration (measure real per-episode seconds &
bytes instead of guessing), and the admin page all need history, not a snapshot.

**Change:** at the end of `build()`, append one line to `state/run_history.jsonl`:
```json
{"ts":"…","cities":80,"built":12,"skipped":68,"errors":0,
 "stages":{"audio":{"ran":18,"bytes":1.5e9,"seconds":2100,"backlog":240},
           "chapters":{"ran":25,"seconds":300,"backlog":110}},
 "budget":{"audio":25,"chapters":25}}
```
Lives in the durable state (already synced to the bucket). Bounded by periodic truncation
(keep last N=1000 lines). The projection model reads this to derive **measured** rates.

**Effort:** S. Pure addition; `statesync` already syncs `*.json` — extend to `*.jsonl`.

## Change 3 — Generalize budgets (per-stage, dynamic "catch-up")

**Why:** Today `budgets` is a dict keyed by stage name with a fixed per-run `GlobalBudget`.
To "maximize use of the 6-hour window once feature rollout slows" (#41) the budget should be
**derived from a wall-clock target**, not a hardcoded count.

**Change:**
- Add `defaults.run_time_budget_minutes` (e.g. 300 = 5h, leaving headroom under the 6h limit).
- Before the run, estimate per-episode seconds per stage from `run_history` (Change 2). Set each
  stage's `GlobalBudget = floor(remaining_time_budget × share / est_seconds_per_episode)`.
- Keep the explicit `materialize_budget_per_run` as an optional hard ceiling.

Result: when there's a big backlog and no feature churn, the run auto-fills the window; when a
feature change touches every feed (re-render storm), time is spent there instead. This is the
single change that makes the 6h window self-tuning.

**Effort:** M. The `GlobalBudget` abstraction already exists; this changes how its size is chosen.

## Change 4 — Transcript artifact storage (keystone for group A)

**Why:** transcripts are large text blobs that ~10 features depend on (summaries, search, NER,
votes, translation). They must be stored once, durably, and addressed like audio.

**Change:** mirror the audio design.
- `Episode.transcript_url` already exists; add `transcript_key` + `transcript_spec_hash`
  (model = ASR engine + version + source audio spec). Content-addressed object key
  `…/<uid>-<spec>.json` (or `.vtt`).
- A `TranscriptStage` (audio-affecting? No — feed-only, but depends on audio existing, so it
  must run **after** audio). It needs hosted audio as input → so order is `chapters, audio,
  transcript, summary, links`. The stage skips episodes without `hosted_audio_url` yet
  (picked up a later run, same as backfill).
- Store transcript text in the bucket (not docs/) — it's large; reference via
  `<podcast:transcript url=… type="application/json"/>` and link in notes.
- `feed_content_hash` already includes `transcript_url`, so adding it re-renders correctly.

**Effort:** L (mostly the ASR integration + cost controls). Architecture is a clean clone of audio.

## Change 5 — A derived-artifact abstraction (de-dupe audio/transcript/summary plumbing)

**Why:** audio, transcript, summary, chapters all repeat the same pattern: spec hash →
content-addressed key → budgeted produce → persist → reference. After transcripts land you'll
have three near-identical implementations.

**Change (refactor, do *after* transcripts prove the pattern):** a small `DerivedArtifact`
helper capturing {spec_hash_fn, object_key_fn, produce_fn, budget_key}. Stages become thin.
Don't do this preemptively — wait until the third instance exists so the abstraction is
informed by real variation (YAGNI until then).

**Effort:** M. Pure refactor; defer.

## Change 6 — Directory index sharding (scale past ~hundreds of feeds)

**Why:** `render_index` emits one JSON blob the client paginates in memory. At 1,000+ feeds
this is multi-MB on every page load. (Also flagged in the earlier review.)

**Change:** shard the dataset by state/region into `docs/data/<state>.json`, load on demand;
or pre-render static region pages. Keep the single-blob path for small deployments (forks).

**Effort:** M.

## Change 7 — Per-meeting permalink pages (#46) + a page-generation seam

**Why:** today the only per-meeting artifact is a feed `<item>`. A real HTML page per meeting
(player + chapters + transcript + agenda links) is the biggest SEO/sharing win and the natural
home for transcript display.

**Change:** add `render_meeting_page(city, ep)` → `docs/<slug>/<uid>/index.html`, written in
`_process_city` alongside the chapter sidecars (same skip/prune logic). Gate by a config flag so
forks can opt out. This also makes Change 6 less urgent (meeting pages are individually
crawlable/searchable).

**Effort:** M.

## Change 8 — Provider capability declaration (for discovery + UI)

**Why:** the codebase increasingly asks "does this provider support chapters/agenda/transcript?"
via `hasattr(provider, "fetch_chapters")`. As providers multiply (#31) and the admin/projection
UI needs to reason about capabilities, make this explicit.

**Change:** add a `capabilities: frozenset[str]` (or properties) to the provider Protocol, e.g.
`{"agenda", "chapters", "transcript", "video", "upcoming"}`. `hasattr` checks become capability
checks; the directory and projection model can display/segment by capability.

**Effort:** S. Mechanical; improves readability and unlocks UI.

## Change 9 — Security trust boundary for Phase-5 onboarding (see audit #S1)

**Why:** once a source URL can originate from a GitHub-issue submission, `fetch_episodes` becomes
an SSRF/abuse vector (internal IPs, file://, huge responses, redirect loops).

**Change:** a `validate_source_url()` gate enforced at config-load and at fetch:
- scheme allowlist (`https` only), host allowlist by provider (e.g. `*.granicus.com`,
  `*.swagit.com`, `*.api.civicclerk.com`, the city's own domain for CivicPlus),
- block private/loopback/link-local IPs (resolve + check), cap redirects, cap response size,
  enforce timeouts (already have timeouts).
- The `/approve` flow stays human-in-the-loop, but defense-in-depth matters since the build runs
  with repo write/secrets.

**Effort:** M. Do **before** Phase 5 PR-B/C.

## What does NOT need to change

- Adding **transcripts, summaries, tags, votes, soundbites, loudness-norm, host-all-audio,
  translations, calendar, OPML, topic feeds** = new stages and/or new render outputs. No core change.
- Adding **Legistar/YouTube/Zoom/Cablecast** providers = new adapters. No core change.
- The hashing/identity/state model is sound at 100× scale.

## Suggested order

1. Change 1 + 2 (cost accounting + run history) — tiny, unlocks doc 03.
2. Change 3 (dynamic budgets) — self-tuning 6h window.
3. Change 9 (security gate) — before Phase 5.
4. Change 4 (transcript storage) — keystone; then summaries/search/votes ride on it.
5. Change 7 (meeting pages), Change 8 (capabilities), Change 6 (index sharding) — as scale demands.
6. Change 5 (derived-artifact refactor) — last, once the pattern has three instances.
