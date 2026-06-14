# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project is **pre-1.0 (beta)** and does not
yet cut tagged releases, so entries are grouped by milestone (most recent first) rather than by version.
Once 1.0 ships, entries move under semver tags.

> This is the **living history of what shipped**. The forward-looking plan lives in
> [ROADMAP.md](ROADMAP.md) and [`review/11`](review/11-technical-design-roadmap.md); when an initiative
> is implemented, add it here as part of the same work (see the doc-update contract in
> [CONTRIBUTING.md](CONTRIBUTING.md)).

## Unreleased

_Work in progress toward 1.0 — see [ROADMAP.md](ROADMAP.md) Phase H (Hardening & Efficiency)._

### Fixed
- **PR preview no longer depends on live providers (was failing/ballooning on provider outages).**
  The preview ran `citypods build --phase render` with **no record store**, so it fetched all ~84
  feeds live just to have something to render — slow, and the *only* thing that could fail it (a
  granicus connection-timeout storm, amplified by a concurrent Audio run, produced 33 errors → exit
  1). New `citypods build --no-refresh` renders **purely from the record store with zero provider
  connections** (`SourcePipeline.render_from_records`; an empty store renders an empty feed, not an
  error). `preview.yml` now restores the `build-state-*` Actions cache (read-only, no B2 creds — a PR
  can read its base branch's caches) and runs `--phase render --no-refresh`: ~seconds instead of
  minutes, deterministic, and immune to provider availability. Production deploys are unchanged (they
  still refresh + already fall back to `archive_from_records` on a fetch error). URL/contract
  validation continues to live in `contracts.yml`.
- **Granicus audio now downloads — the CDN `403` was a User-Agent block, not signing/rate-limiting.**
  `archive-video.granicus.com` `403`s non-browser User-Agents; our bare `citypods/0.1` UA (and
  ffmpeg's default `Lavf/…`) were blocked, so Granicus audio had **never** materialized (every run
  encoded only swagit). `USER_AGENT` (`http.py`) is now browser-compatible
  (`Mozilla/5.0 (compatible; citypods/0.1; +…)`, verified `206` live), and `media.py` passes it to
  ffmpeg/ffprobe via `-user_agent` on every remote fetch (`_download_audio`, `_render_identity`,
  `_render_filter`, `_probe_audio_bitrate`). PRs #245/#250/#251 had misdiagnosed this as a
  signing/rate-limit issue and only tested against a **mocked signed redirect**, so it passed CI while
  failing live. To prevent a recurrence, `citypods/contracts.py` gains a **media-fetch** check that
  truncated-downloads each provider's newest clip through the production fetch path (UA + protocol
  whitelist + timeout); it runs in the `-m live` suite and `scripts/check_endpoints.py` (ffmpeg added
  to `contracts.yml`), so a silent "audio never downloads" regression now fails loudly.

### Changed
- **Per-provider (per-host) rate limiting + sharding-regression fixes (#39)** —
  ([#274](https://github.com/BashfulBits/city-meeting-podcasts/issues/274)). The first sharded Audio
  run after H6b regressed: comparing it to the last pre-sharding Enrich run, source fetches collapsed
  from a real 5–135 s spread to **all ~5 s** and produced **zero** encodes, with Granicus `403`s and
  no logged error. Root cause: 4 parallel shard jobs each concentrate their workers on a few sources
  sharing one provider CDN, and that burst throttles the tenant (Granicus answers `403`; Swagit
  returns short responses ffmpeg copies and exits 0 on — a truncated "5-second" episode that passed
  the old `size > 0` check). Fixes:
  - **`HostRateLimiter`** (`citypods/http.py`) — a process-global per-**registrable-domain**
    concurrency cap, configured by `provider_rate_limits` in `config/site_config.yml`
    (`granicus.com: 2`, `swagit.com: 2`, `civicclerk.com: 4`). Acquired by **both**
    `GuardedHTTPAdapter.send` *and* the ffmpeg fetch paths (`citypods/media.py`), so one cap bounds
    requests *and* the media downloads that actually caused the storm. Keyed by registrable domain so
    the Granicus-owned Swagit CDN (`*.granicus.com`) is matched by the host the tenant sees.
  - **403-as-rate-limit lifted into the shared layer** — `403` joins the `_ClampedRetry`
    `status_forcelist` (provider throttle, never auth, since media bytes never go through `requests`);
    the bespoke backoff loop in `GranicusProvider.resolve_media_url` is removed. The Retry-After clamp
    is preserved.
  - **Truncation safety net** — an encode that probes shorter than 50 % of the feed-declared duration
    (Granicus/CivicClerk) **or** is empty/near-empty (under a small absolute byte floor — catches
    duration-less Swagit, whose throttled `/download` produced 258-byte stubs) is failed into the
    existing #120 backoff instead of being hosted, so a throttled fetch never ships a 5-second meeting.
  - **Cleanup tool for the already-hosted bad audio** — `scripts/clear_run_materializations.py`
    (+ a `workflow_dispatch` **Clear materialization** workflow) takes an Actions run ID, parses its
    `audio encode done` lines, and resets those records (optionally deleting the B2 objects) so the
    next `audio.yml` re-encodes them. Dry-run by default. Undoes the first sharded run's truncated
    output wholesale.
  - **Balanced shard assignment** — `records.shard_index` (hash-mod, which left `audio (0)` empty with
    few sources) is replaced by `records.shard_assignment`, a round-robin over the sorted source_keys:
    every shard gets `floor`/`ceil(total/N)` sources, never empty until `#sources < N`. Still
    deterministic, disjoint, and exhaustive.
  - **Accurate ffmpeg timing** — the guard's poll cadence drops from 5 s to 0.5 s so the logged
    `seconds=` reflects a child's real runtime (the 5 s cadence had made every sub-5 s fetch read as
    `seconds=5.0`, masking the truncation).
- **Sharded `audio.yml` + `asr.yml` workflows, lane-pinned (H6b)** —
  ([#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273)). The combined
  `enrich.yml` (H11b) is replaced by two dedicated workflows, each on its own concurrency group
  (`audio` / `asr`, both distinct from `pages`) and a `strategy.matrix.shard` of 4 source-shards, so
  a deploy is never canceled by heavy work and concurrent shards clear the backlog without clobbering
  records. New `citypods enrich` flags: `--shard K/N` (keep only sources with
  `shard_index(source_key) == K`; disjoint + exhaustive across `K`), `--source KEY`, and
  `--lane {audio,transcribe,align}`. `run.py` filters cities to the shard and threads the lane into
  the two-pass queue (`audio` → audio pass only; `transcribe`/`align` → transcript pass only), and a
  sharded/scoped run uses the H11b hooks — `push_state(only_prefixes=…owned sources…)` +
  `reconcile_state(full_run=False)` — so it pushes back only the records it owns and never sweeps a
  sibling's. `audio.yml` runs `--lane audio` (no `[asr]` extra, no Whisper); `asr.yml` runs
  `--lane transcribe` (fresh faster-whisper only). The `align` lane (stable-ts forced alignment) is
  implemented but **not scheduled** — forced alignment is deferred to a later issue, so caption-bearing
  feeds get fresh transcription for now. A direct `citypods enrich` (no lane/shard) is unchanged.
- **Render-only deploy; the enrich workflow is the sole record writer (H11b)** —
  ([#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272)). `deploy.yml` is stripped
  to render-only (checkout → install → restore state → render → validate → upload → deploy): no
  ffmpeg, no Whisper model, no encodes, and the `actions: read` graceful-yield token is dropped (only
  the time-bounded heavy phase polls the Actions API). The heavy phase moves to a new
  **`.github/workflows/enrich.yml`** with its own `enrich` concurrency group, so audio/ASR work can
  never block or redden the Pages deploy. Critically, **the render phase now writes only `docs/`**:
  `build()` gates `save_records` / `push_state` / `reconcile_state` off `--phase render`, so a stale
  render push can no longer silently erase a transcript/hosted-audio that the enrich workflow wrote
  (the lost-update "record-write race" — review/12 §H6/H11b). No pipeline-version bump and no record
  migration: existing artifacts are untouched; this only changes *which workflow* persists them.
  `statesync.push_state(..., only_prefixes=)` and `reconcile_state(..., full_run=)` add the
  scope hooks H6b's source-sharded jobs will use (no behavior change at the single-writer default).

### Added
- **GPU/ASR execution-backend interface + `local` adapter (H13)** — the pre-1.0 "compute is
  pluggable" lock ([#271](https://github.com/BashfulBits/city-meeting-podcasts/issues/271)). New
  `citypods/compute/` module, peer of `storage/`: `base.py` defines `InferenceJob(task, inputs,
  recipe_hash)` — `task` typed for the **full §5.5 verb set** (ASR `transcribe`/`align`/`diarize`
  + the reserved R3/R4 LLM verbs `summarize`/`tag`/`soundbite-select`) — plus `JobResult`/
  `JobHandle` and a `runtime_checkable` `Backend` protocol `run_inference(job)`. `local.py` wraps
  the in-process faster-whisper/stable-ts path (**byte-identical** VTT + words.json output);
  `TranscriptStage` now routes inference through `backend.run_inference(...)`, and
  `make_compute` selects the backend from `compute_backend` (`site_config.yml` default `local`;
  `COMPUTE_BACKEND` env override). Behavior-preserving refactor — `ASR_PIPELINE_VERSION`
  unchanged. The seam H6b's lane split and H14's Modal/Beam **dispatch** adapters (which return a
  `JobHandle`) both build on.
- **Stage backlog manifest + configurable prioritization policy (H5)** — shipped across three PRs
  ([#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263) ·
  [#264](https://github.com/BashfulBits/city-meeting-podcasts/pull/264) ·
  [#265](https://github.com/BashfulBits/city-meeting-podcasts/pull/265)):
  - *Ordering engine (PR1)*: new `citypods/ops/workqueue.py` — a declarative `backlog_priority` policy
    (`site_config.yml`) of composable comparator keys: `recency` (with an optional `within_days`
    horizon that collapses beyond the window so the next key governs), `recent_first`, `city_order`
    (explicit slug list, partial lists fall through), `body_order`, `feed_visible_first`. `order()` is
    the identity with no policy, so wiring it into `_materialize_set` is byte-identical by default.
    Production runs `recency: {order: desc, within_days: 30}`.
  - *Work manifest + lean sidecar + status (PR2)*: `build_manifest` derives a `WorkItem` per
    (episode, output `work_class`) from records — `audio` / `transcript-asr` / `transcript-align`,
    tagged done/queued/alignment-disabled, bucketed feed_visible vs deep_archive — persisted to
    `state/work.json` (statesync-synced) with a `lease`/`release`/`is_leased` API (the H6b substrate).
    `/admin/status` gains a backlog-by-work-class block. `order_cities_by_policy` adds coarse
    cross-source ordering to the per-city pool.
  - *Global two-pass enrich queue (PR3)*: the time-bounded `enrich` phase becomes a global,
    policy-ordered queue — prepare all sources in parallel, then process the backlog
    **newest-everywhere-first across all sources** as an on-runner **audio pass**
    (`chapters→timeline→remap→audio`, gated by the H8/H11a `native_work_gate`) followed by a
    **decoupled transcript pass**. `all`/`render` keep the per-city pool. The transcript pass is
    **dispatch-not-await-ready**: transcription/diarization will run on external workers
    ("over the wall", H9/H6b), reconciled from durable state on a later deploy — per-episode
    `audio→transcribe→diarize` order is enforced by sequential dependency-gated passes in-run and by
    manifest `state` across runs; fused vs separate execution is the backend adapter's call via
    groupable leases. Design: [`review/12` §H5](review/12-hardening-and-efficiency.md).
- **Feed-health backlog triage + provider drift (H4)**: three sub-deliverables:
  - *Rehost-backlog triage*: `check_rehost_backlog` applies a three-tier model — catching-up
    (any hosted > 0, or pipeline not yet active enough) is **suppressed** (existing issues
    auto-close via reconcile); stalled (≥ 3 of last 5 runs encoded but feed still 0 hosted) is
    **`WARN`**; real provider failures stay `ERROR`. `_load_run_history` + `run_history` threaded
    through `audit_city` / `audit_all`. 6 new tests.
  - *Provider error-rate tracking*: `_record_run_history` in `run.py` now writes a
    `provider_errors: {name: count}` dict of city-level source-fetch failures per run to
    `run_history.jsonl`. `check_provider_error_rates` in `audit.py` raises a `WARN
    provider-errors:<name>` finding for any provider with failures in ≥ 2 of the last 5 runs,
    surfacing provider drift before it turns deploys red. 8 new tests.
  - *Auto-comment on state transitions*: `audit_feeds.py` now adds a timestamped comment to an
    existing issue whenever its computed body changes (state transition), in addition to updating
    the body — making transitions visible in the GitHub issue timeline. The close-on-resolve
    comment was already in place. 5 new tests in `tests/test_audit_feeds.py`.
- **Feed-validation publish gate (H3, #53)**: `citypods validate-build docs/` scans every
  generated `*.xml`, skips redirect feeds, demotes empty feeds for known-backfill-in-progress
  cities to warnings, and exits non-zero on structural errors or unexpectedly empty feeds.
  Wired as a gate step in `deploy.yml` after render, before the Pages artifact upload, so a
  malformed feed can't slip through to production. 11 new tests.
- **H2 projection wall-clock fix + per-run telemetry**: `per_run_cap` now defaults to `None`
  (wall-clock-bounded) when `materialize_budget_per_run` is absent; `measured_inputs` calibrates
  `sec_per_ep` from `materialize_encoded` (real encodes only, not cheap re-credits); `to_markdown`
  updated to say "delete the cap" rather than recommending the removed config key; `_feed_row` adds
  a bytes-based `hours_hosted` estimate for providers (Swagit/CivicPlus) that never supply duration
  metadata; `_ResourceHeartbeat` now samples `peak_load_per_cpu` + `min_mem_avail_bytes` via
  `current_snapshot()`, and `NativeWorkGate` accumulates `total_wait_seconds`; `_record_run_history`
  persists `peak_load_per_cpu`, `min_mem_avail_mb`, `window_used_pct`, and `gate_wait_seconds` to
  `run_history.jsonl`; `build_status` returns `audio_backlog` + `transcript_backlog` sub-dicts with
  ETAs so the status dashboard shows both queues without JS math.
- **H1 issue reconciliation**: closed GH#154 (`<podcast:transcript>` — 28 tags confirmed live in the Arlington TX feed); narrowed GH#110 (ASR transcripts) to backfill + ops follow-up only; marked GH#141 (timeline epic) umbrella-only for remaining Phase R features (#153/#155/#156/#157).
- **ASR benchmark workflow (H6a)**: added a manual `asr-bench.yml` workflow that runs
  `citypods asr-bench` over maintainer-selected `city:uid` cases, compares max/med/min
  model + beam-size + CPU-thread profiles under a capped runner budget, and publishes a text report
  artifact. The CLI now accepts `--beam-size` for targeted WER/speed checks.
- **Documentation architecture & handoff**: `VISION.md`, forward-looking `ROADMAP.md`, this
  `CHANGELOG.md`, `ARCHITECTURE.md`, `SECURITY.md`, `AGENTS.md` + `CLAUDE.md`; the living canonical
  design index `review/11-technical-design-roadmap.md` + breakouts `review/12–14`; the feature
  lifecycle / doc-update contract in `CONTRIBUTING.md`.
- **Contributor scaffolding (partial #57)**: PR template, feature-request + bug-report issue templates,
  and an `area:*` / `needs-*` GitHub label taxonomy.
- **Word-level transcript timestamps**: `citypods/asr.py` now passes `word_timestamps=True` to
  faster-whisper. `ASR_PIPELINE_VERSION` bumped to `"2"`; transcripts produced after this carry
  word-level timing, which speaker diarization (#7) and phrase-level search / clip selection need.
  *(Superseded by the **H12 transcript artifact rework** below (PR #253): the served VTT reverts to clean
  segment cues, a word-level JSON sidecar is added, and version-aware gradual re-transcription is wired —
  `ASR_PIPELINE_VERSION` is now `"3"`.)*

### Fixed
- **Transcript artifact rework (H12, [PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253))**:
  ASR now emits a clean **segment-cue VTT** for `<podcast:transcript>` (fixing #249's one-word-per-cue
  regression and ~5× size bloat) **plus a word-level JSON sidecar** (`…-asr-<recipe>.words.json`) for
  phrase search / clip selection / diarization. `ASR_PIPELINE_VERSION` → `"3"`; already-stored **ASR**
  transcripts re-transcribe gradually across enrich runs, while provider-supplied transcripts are never
  invalidated. The word-JSON key is content-addressed and protected from orphan-GC.
- **ASR alignment fallback (H10, PR #232)**: forced alignment now uses a stable-ts faster-whisper model
  that supports `.align()`, and any alignment failure falls back to fresh transcription instead of
  skipping caption-bearing episodes.
- **Runner resource guard (H8, PR #235)**: AAC ffmpeg encodes now pin `-threads`, heavy audio/ASR
  work waits for memory/load headroom before admission, and abandoned ASR daemon inference keeps its
  worker slot until the native thread exits instead of stacking new CPU/RAM work on top.
- **ffmpeg audio memory guard**: silence detection ignores video streams, source-cache/identity/filter
  ffmpeg phases log distinct start/finish markers, and audio ffmpeg children stop when runner memory
  falls below the configured floor instead of risking an Actions lost-comms kill.
- **Deploy resilience — native work gate + one-slot audio lane (H11a, PRs #239/241/242/243/244)**:
  `NativeWorkGate` strictly serializes ASR and audio so they never run concurrently; `native_audio_max_active`
  caps the global ffmpeg encode slots; ffmpeg filter/complex threads pinned to 1; per-child peak RSS and
  minimum runner `MemAvailable` logged per encode; ASR teardown hardened to avoid post-state-push crashes.
  Together with H8, enrich now completes its full 204-min window without exit-143/lost-comms kills.
- **Audio concurrency tuning (H11a, PR #246)**: raised `native_audio_max_active` from 1 → 4 after 3
  consecutive green scheduled runs; at `-threads 1` per encode, 4 slots saturate all 4 cores while
  targeting ~8 GiB RAM.
- **`audio_ffmpeg_threads` auto-calc divisor ([PR #257](https://github.com/BashfulBits/city-meeting-podcasts/pull/257))**:
  the auto-calc for per-encode ffmpeg thread count divided `cpu_count` by `max_encodes_per_source`
  (per-source limit, default 1) instead of `native_audio_max_active` (global encode slots, currently 4).
  Latent bug — production pins `audio_ffmpeg_threads: 1` explicitly so it was never triggered, but
  clearing that pin would have assigned 16 threads to 4 cores. Config comment and regression test added.
- **HTTP Retry-After clamp (PRs #247/[#254](https://github.com/BashfulBits/city-meeting-podcasts/pull/254))**:
  the shared session honors `Retry-After` but **caps it at 120s** rather than obeying it verbatim — a
  Granicus 429 returning `Retry-After: 3600` previously caused urllib3 to sleep inside the retry loop for a
  full hour, blocking the entire build. Short, legitimate delays are still respected; a request that keeps
  failing surfaces as a `ProviderError` for the next scheduled run. (#247 first ignored the header; #254
  clamps instead, so a well-behaved provider's backoff is still honored.)
- **Granicus archive-video URL resolution (PRs #245/#250/#251)**: the adapter now bypasses the broken
  `DownloadFile.php` path by pre-following its redirect to the signed `archive-video.granicus.com` URL and
  handing ffmpeg the signed URL directly; concurrent-access `403`s (Granicus rate-limits with `403`, not
  `429`) are retried with backoff+jitter. Resolves the recurring Granicus `403` enclosure failures.

## Timeline & content-transform foundation

### Added
- **Served↔source timeline/EDL model** (`citypods/timeline.py`) unifying silence-trim, concat, intros,
  transcripts, and clips behind one served-vs-source time map (design: `review/08`; audit: `review/09`;
  INFRA-1..9, epic #141).
- **Silence trimming** (`citypods/silence.py`, `trim_silence`) — removes long lead/trail/mid-meeting
  dead air, remapping chapters and transcripts onto the served audio (#22/#111).
- **EBU R128 loudness normalization** (`audio_loudness_profile: ebuR128:-16LUFS`) (#21).
- **Multi-segment concat** (`citypods/concat.py`) for meetings split across source segments (#122).
- **Clip / soundbite extraction** (`citypods/clips.py`) — forward-maps a served range to source cuts.
- **ASR transcripts** (`citypods/asr.py`) — reuse provider transcripts first; otherwise forced alignment
  (stable-ts) or fresh transcription (faster-whisper); `asr-bench` CLI for WER/throughput (#1/#110).
- **`<podcast:transcript>`** emission for synced hosted transcripts (#11/#154).
- **Operational status dashboard** at `/admin/status/` rendered by `citypods report` (#124).

### Changed
- **Materialization budget replaced** with a wall-clock window + graceful yield: a run processes
  recordings until a shared `stop()` predicate (time window spent, or a newer Build & Deploy run queued).
  Removed `materialize_budget_per_run` / per-source count budgets (PR #128).

## Episode-record & enrichment-stage foundation

### Added
- **Append-only archive** (`records.merge_persisted`): meetings that drop off a provider feed (Granicus
  100-item cap, Swagit windowing) are retained and rendered from the full store (#52).
- **Stable episode UID** (author+body+date), **content-addressed audio keys**, and **split hashes**
  (`audio_spec_hash` vs `feed_content_hash`) for independent re-encode vs re-render invalidation.
- **Enrichment-stage pipeline** (`citypods/stages.py`): `EnrichmentStage` Protocol + `default_stages()`;
  adding a feature = adding a stage.
- **Stable feed URLs** — aliases + `<itunes:new-feed-url>` + redirect map for provider migrations.
- **No-cost feature stages**: resource/agenda links + `content:encoded` notes; chapters across capable
  providers; universal Podcasting 2.0 `<podcast:chapters>` sidecars.
- **Durable bucket-backed state** (`statesync.py`): object storage is the source of truth; Actions cache
  is latency only.
- **Resource cost/throughput projection** (`projection.py`) + `citypods report` + static what-if admin
  page; persisted `run_history.jsonl`.

## Scale, QA & discovery foundation (Phase 5 PR-A)

### Added
- **Feed-health audit** (`citypods/audit.py`, `scripts/audit_feeds.py`, `audit.yml`): staleness,
  view-cap, enclosure liveness, empty-feed, rehost-backlog checks → idempotent GitHub issues.
- **Endpoint contract tests** (`contracts.yml`, opt-in `@pytest.mark.live`) kept out of PR CI.

### Security
- **SSRF / source-URL gate** (`validate_source_url`), per-provider host allowlists, bounded
  redirects/response-size; **ffmpeg `-protocol_whitelist`**; **defusedxml** provider parsing; fetch
  retry/backoff; alias/slug collision validation.

## Earlier foundations (Phases 0–4)

### Added
- Python `citypods/` package; provider adapters for **Granicus, CivicPlus/CivicMedia, CivicClerk,
  Swagit**; one-feed-per-board generation.
- Audio materialization pipeline (ffmpeg → M4A) with **Backblaze B2 + Cloudflare CDN** (free egress).
- Static frontend (Jinja2 → `docs/`): index with instant search + group-by-city accordion, per-city
  feed pages with inline player + subscribe links, generated cover art, custom domain.
- Offline pytest suite with byte-for-byte feed snapshots; CI (`ci.yml`), per-PR preview (`preview.yml`),
  scheduled deploy (`deploy.yml`, 4h cron); incremental builds + content-hash change detection.
