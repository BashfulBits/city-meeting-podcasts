# Architecture

The **as-built** map of the system: how the code is laid out and how data flows today. This is the
companion to the *forward-looking* design — for where the system is **going**, see
[`review/11-technical-design-roadmap.md`](review/11-technical-design-roadmap.md) (the canonical,
living Technical Design Roadmap). For *why* past decisions were made, see [`review/`](review/).

> Convention: this file is kept **current** with the shipped code. When a change alters the shape
> described here, update this file as part of the same work (see the doc-update contract in
> [CONTRIBUTING.md](CONTRIBUTING.md)).

## What the system does

Turns US city-meeting archives into subscribable **podcast feeds** (audio + video) and a searchable
**directory**, hosted free on GitHub Pages, with derived audio/transcripts on object storage behind a
CDN. It is **provider-agnostic**: each meeting platform is a pluggable adapter behind one interface.

## Pipeline (data flow)

```
config (YAML)                       ── per-city + per-feed config
  │
  ▼
providers.fetch_episodes()          ── Granicus / CivicPlus / CivicClerk / Swagit adapters
  │
  ▼
records.assign_uids()               ── stable real-world episode UID (author+body+date)
  │
  ▼
records.merge_persisted()           ── APPEND-ONLY archive: {**persisted, **fresh}  (#52)
  │
  ▼
stages.run_stages()                 ── ordered enrichment, wall-clock budgeted:
  Chapters → Timeline → Remap → Audio → Transcript → Links
  │
  ▼
records.save_records()              ── state/sources/<key>/episodes.json
  │                                    (bucket = source of truth, via statesync)
  ▼
render / feeds / site               ── feed_content_hash skip → RSS + city pages + chapter sidecars
  │
  ▼
docs/  ──► GitHub Pages              ;   audio + transcripts + state ──► B2 (Cloudflare CDN)
```

The production deploy **splits render from enrich** into **separate workflows** (separate CLI commands,
see below): `deploy.yml` is render-only — it publishes Pages quickly from already-known state and never
runs ffmpeg/ASR — while the heavy, best-effort, resumable backfill runs in two dedicated workflows,
`audio.yml` (ffmpeg encode → object storage) and `asr.yml` (faster-whisper transcription), each on its
own concurrency group **sharded by `source_key`** (`strategy.matrix.shard`). Assignment is
source-atomic and weighted by the number of configured feeds/bodies sharing a source, so large sources
are not casually bundled with small ones while concurrent shards still never write the same record file.
Encoding/transcription can never block or redden the Pages deploy (H11b), and concurrent shards clear
the backlog without clobbering records (H6b). The render phase writes **only `docs/`**: it persists no
records, leaving the
audio/ASR workflows as the sole record writers; a sharded run pushes back only the `source_key`s it owns
(`statesync.py`'s `push_state(only_prefixes=)`) and skips the reconcile sweep (`full_run=False`), so a
stale or partial push can't clobber a sibling shard's records (the record-write race). Each `citypods
enrich` job pins one **lane** — `audio`, `transcribe`, or `align` — so the two ASR models never co-load
on one runner. The heavy
`enrich` phase processes its backlog as a **global, policy-ordered two-pass queue** (`ops/workqueue.py` +
`run.py`): prepare every source, then run an on-runner **audio pass** (`chapters→timeline→remap→audio`,
newest-everywhere-first across all sources) followed by a **decoupled transcript pass**. The transcript
pass is *dispatch-not-await-ready* — transcription/diarization will run on external workers and reconcile
from durable state on a later deploy (design: [`review/12` §H5](review/12-hardening-and-efficiency.md)).

## Module map (`citypods/`)

| Area | Modules |
|---|---|
| **Providers** | `providers/{base,granicus,civicplus,civicclerk,swagit}.py` — `MeetingProvider` Protocol + registry; each normalizes to the episode model. |
| **Records / identity** | `records.py` — stable `uid`, `source_key`, `audio_spec_hash`, `feed_content_hash`, append-only `merge_persisted`, content-addressed keys, orphan-GC refs. `models.py` — `Episode`/`City`. |
| **Enrichment stages** | `stages.py` — `EnrichmentStage` Protocol + `default_stages()` (`Chapters→Timeline→Remap→Audio→Transcript→Links`); `StageContext`, `StageStats`, the wall-clock `stop()` budget. |
| **Scheduling / backlog** | `ops/workqueue.py` — the `backlog_priority` policy (comparator registry: windowed `recency`, `city_order`, `body_order`, `feed_visible_first`, …), the derived **work manifest** (`WorkItem` per episode × output `work_class`, persisted to `state/work.json`), and the `lease`/`release`/`is_leased` API — the coordination substrate for off-runner ASR/diarization workers (H6b/H9). |
| **Timeline / EDL** | `timeline.py` (served↔source map), `silence.py` (trim planner), `concat.py` (multi-segment), `clips.py` (clip/soundbite extraction). |
| **Media / audio** | `media.py` — ffmpeg encode, pinned AAC encode threads, loudness (EBU R128), content-addressed upload. |
| **Transcripts** | `asr.py` — forced alignment (stable-ts) / fresh transcription (faster-whisper) with align-error fallback; emits a clean **segment-cue VTT** (served via `<podcast:transcript>`) **plus a word-level JSON sidecar** (`…-asr-<recipe>.words.json`) for search/clips/diarization; version-aware re-transcribe on an `ASR_PIPELINE_VERSION` bump (provider transcripts never invalidated); both objects are content-addressed + GC-referenced. `bench.py` — `asr-bench` diagnostic. |
| **Compute backend** | `compute/{base,local,budget,dispatch}.py` (H13 + H14a, **pre-1.0 lock**) — the pluggable GPU/ASR execution seam, peer of `storage/`. `base.py` defines `InferenceJob(task, inputs, recipe_hash)` (`task` typed for the full §5.5 verb set: ASR `transcribe`/`align`/`diarize` + reserved LLM `summarize`/`tag`/`soundbite-select`), `JobResult`/`JobHandle`, a `runtime_checkable` `Backend` protocol `run_inference(job)`, and (H14a) a `DispatchBackend` protocol (returns a `JobHandle` + `estimate_gpu_seconds`). `local.py` runs faster-whisper/stable-ts in-process (the only adapter at 1.0; **byte-identical** to the pre-refactor path). **H14a substrate:** `budget.py` is the free-tier ledger (`state/compute_budget.json`, statesync-backed) that makes exceeding a backend's `monthly_gpu_seconds`/`max_inflight` structurally impossible (the **$0 guarantee**); `dispatch.py` adds the router (fill free tiers → **overflow to `local`**), a thread-safe `DispatchCoordinator` (records a live `work.json` lease `lease_owner="modal:<job_id>"` + decrements budget — the first competitive use of the H5 lease API), and `reconcile_compute` (reap dead workers → re-queue; settle completed jobs; run at `asr.yml` start via `citypods compute reconcile`). `make_compute` selects by `compute_backend`: `local` (bypass) or `auto` (default — route `TranscriptStage` inference through the coordinator; with no external adapter registered yet, every job overflows to `local`, behavior-identical). H14b/H14c register the real Modal/Beam **dispatch** adapters into the coordinator with no stage change. |
| **Feeds / site** | `feeds.py`, `render.py`, `site.py`, `templates/*.j2`, `artwork.py` (cover art). |
| **Orchestration** | `run.py` — `SourcePipeline`, `build()`, the **global two-pass enrich queue** (`_run_enrich_global_queue`: newest-everywhere-first on-runner audio + decoupled transcript), run history, graceful yield, resource-guard wiring. `resources.py` — process resource snapshots + memory/load admission guard for expensive native work. `cli.py` — `build / render / enrich / report / doctor / bodies / asr-bench / rebuild-audio / admin`. |
| **State** | `state.py` (build fingerprint), `statesync.py` (bucket↔local; bucket is truth), `storage/{base,local,s3}.py` (`S3CompatibleStorage` b2/r2 presets + local). |
| **Ops / QA** | `audit.py` (+ `scripts/audit_feeds.py`) feed-health; `contracts.py` endpoint contracts; `report.py` + `projection.py` cost/throughput + `/admin/status`; `validate.py` feed validation. |
| **Security** | `security.py` — SSRF gate (`validate_source_url`), host allowlists, redirect/size caps; `http.py` retry/backoff; ffmpeg protocol whitelist; defusedxml. |

## Key invariants (why it extends cleanly)

- **Provider Protocol + registry** — a new platform is a new adapter, no core change.
- **Stage Protocol + `default_stages()`** — a new per-episode feature is a new stage.
- **Split hashes** — `audio_spec_hash` (bytes) and `feed_content_hash` (RSS) invalidate independently.
- **Content-addressed audio + stable UID** — CDN cache-bust, rollback, and provider-migration safe.
- **Append-only archive** — meetings that drop off a provider feed are never lost (#52).
- **Timeline served↔source EDL** — silence-trim/concat/intro/transcripts/clips all reduce to one
  served-vs-source time map (see [`review/08`](review/08-timeline-and-content-transforms.md)).
- **Bucket-as-truth state** — derived artifacts survive Actions cache eviction.
- **Wall-clock budget + graceful yield** — heavy work runs until a time window closes or a newer run
  queues; cheap idempotent bookkeeping always finishes (see `stages.py` "stop convention").
- **Resource admission for expensive native work** — ffmpeg/ASR starts can wait for memory/load
  headroom, and abandoned ASR inference continues to occupy its worker slot until the native thread
  exits, so a stopped item does not stack unbounded CPU/RAM work. Audio encodes are admitted by a
  **memory reservation** (`MemoryReservation`): each encode reserves its *predicted* peak RSS
  (`estimate_encode_rss_bytes`, from the known-ahead served length) against `audio_memory_budget_mb`,
  so a new encode begins only when its *future* footprint fits — a leading signal the instantaneous
  `mem_available` check (which still governs ASR) lacks. The mid-flight kill floor
  (`audio_ffmpeg_memory_floor_mb`) stays as the backstop, and `native_audio_max_active` is the hard
  concurrency ceiling. Audio run #10 recalibrated the filter/loudnorm model to a 64 MiB/min served
  coefficient with a 12,000 MiB max/unknown reservation after real jobs peaked around 9–13 GiB.
- **Backlog prioritization + async-ready dispatch** — the enrich phase orders work
  *newest-everywhere-first* across all sources by a configurable `backlog_priority` policy; audio
  re-hosting runs on the runner while transcribe/diarize are modeled as **dispatch-not-await** work
  reconciled from the manifest across runs, so moving that compute to an external backend is an adapter
  swap (no change to the audio queue or feed rendering). See [`review/12` §H5](review/12-hardening-and-efficiency.md).

## Hosting & CI/CD

- **Static site** → Jinja2 templates render to `docs/`; Jekyll disabled (`docs/.nojekyll`); served by
  GitHub Pages at the configured custom domain.
- **Object storage** → Backblaze B2 (S3 API) fronted by a Cloudflare Worker/CDN (free egress) for
  audio + transcripts + durable state.
- **Workflows** (`.github/workflows/`): `ci.yml` (ruff + pytest on PR/push), `preview.yml` (per-PR
  downloadable site preview), `deploy.yml` (**render-only** Pages publish on `main` push + 4h cron),
  `audio.yml` (sharded audio materialization, 4h cron; own `audio` concurrency group),
  `asr.yml` (sharded faster-whisper transcription, daily; own `asr` concurrency group) — the two heavy
  record-writers, each a `--shard K/N` × `--lane` matrix, `audit.yml` (daily feed-health → GitHub
  issues), `contracts.yml` (weekly live endpoint contracts), `asr-bench.yml` (manual ASR benchmark).
- **Tests** run fully offline against recorded fixtures; feeds have byte-for-byte snapshot tests.

## Provider notes & gotchas

Hard-won facts that bite anyone adding/debugging providers:

- **Granicus RSS is hard-capped at 100 items per view.** Cities split bodies across multiple views, so
  a single feed can be incomplete — use `source.feed_urls` (merged + deduped by guid) to combine views,
  and the feed-health `view-cap` check flags any view sitting at exactly 100.
- **Swagit serves a "Carmel, IN" placeholder page for unknown subdomains** — a false-positive trap when
  probing/discovering. Cross-check the returned locality against the requested city before trusting it.
- **Swagit `/videos/{id}/download` is broken for older meetings** (returns a keyless presigned S3 URL
  that 403s); the real media for old meetings is the per-segment files on the video page. Newer meetings
  are fine. See `.claude` history / `swagit.py`.
- **Swagit's `/play/{id}/{t}` deep-link is a client-side SPA route** — the server `404`s it on a direct
  `HEAD`/`GET` (even the real chapter-anchor timestamps the watch page links), though it works in a
  browser. It's the correct user-facing format (the watch page's own anchors use it); just don't expect
  a 2xx when probing it. The endpoint-contract deep-link check handles this (`_is_spa_seek_url`).
- **CLI console-script can fail to import the editable package from a script dir** — prefer
  `python -m citypods.cli …` (and `PYTHONPATH=. python scripts/…`).
- **Granicus `DownloadFile.php` 302-redirects to a real MP4** even when the RSS `type` says WMV — that
  legacy type is metadata, not the actual media.
- **The Granicus media CDN `403`s non-browser User-Agents** — this, not signing or rate-limiting, is
  why Granicus audio `403`'d. `DownloadFile.php` 302-redirects to a plain (unsigned)
  `archive-video.granicus.com` URL; that URL serves fine to a `Mozilla/5.0 (compatible; …)` UA but
  `403`s our old bare `citypods/0.1` UA and ffmpeg's default `Lavf/…`. The fix: `USER_AGENT`
  (`http.py`) is browser-compatible, and `media.py` passes it to ffmpeg/ffprobe via `-user_agent`.
  *(History: PRs #245/#250/#251 misdiagnosed this as a signing/rate-limit problem — their tests
  mocked a **signed** redirect, so they passed CI while granicus never actually downloaded. The
  `tests/live` **media-fetch** check now truncated-downloads each provider's newest clip through the
  real fetch path, so a UA/endpoint regression fails loudly instead of silently.)* `resolve_media_url`
  still pre-follows `DownloadFile.php` (one redirect resolved in Python, not per ffmpeg process), and
  resolution stays at **fetch time**, never persisted.
- **Source-cache files are not podcast M4As.** The per-run cache stores provider audio as local
  Matroska audio (`.mka`) via stream-copy, preserving source codecs without re-fetching the CDN.
  Final materialization is the M4A boundary: only under-cap AAC is copied into the podcast file; other
  codecs are transcoded to AAC so the iPod/M4A muxer never sees incompatible source streams.
- **Per-host concurrency cap — `requests`, ffprobe, and ffmpeg share the local guard; media reads also
  use distributed leases**
  (`HostRateLimiter` in `http.py`; `DistributedProviderLeasePool` in `provider_leases.py`, issue #39
  follow-up). Sharded `audio.yml`/`asr.yml` (H6b) concentrate many workers on a few sources sharing one
  provider CDN; that burst throttles the tenant (Granicus `403`; Swagit short/truncated responses).
  `HostRateLimiter` caps simultaneous in-flight requests **per registrable domain** inside one process
  (`provider_rate_limits` in `site_config.yml`, e.g. `granicus.com: 2`) and is acquired by
  `GuardedHTTPAdapter.send`, ffprobe bitrate/duration probes, and ffmpeg fetch paths. The B2-compatible
  `provider_distributed_leases` layer adds soft candidate-election leases around ffprobe/ffmpeg media
  reads, capping aggregate Granicus overlap across the four audio shard processes (currently 6 total
  slots, based on 2026-06-15 probes plus Audio #10 telemetry). Keys are registrable domains so the
  Granicus-owned Swagit CDN (`*.granicus.com`) is matched by the host the tenant sees.
- **`403` is retried as a rate-limit signal** by the shared session (`403` in `_ClampedRetry`'s
  `status_forcelist`): media bytes never go through `requests`, so a `403` a `requests` call sees is a
  provider throttle, not auth — retrying with backoff generalizes the old bespoke Granicus loop.
- **A truncated source fetch is failed, not hosted** (`media.py`, issue #39). A throttled provider can
  return a short response ffmpeg copies and exits 0 on — a 5-second clip of a multi-hour meeting. When
  the feed declares a duration (Granicus/CivicClerk), an encode under 50 % of it is failed into the #120
  backoff instead of being published (Swagit declares none → the concurrency cap is its guard).
- **`Retry-After` is honored but clamped to 120s** by the shared HTTP session (`_ClampedRetry` in
  `http.py`). A Granicus 429 returning `Retry-After: 3600` once hung the whole build for an hour inside
  urllib3's retry sleep; capping keeps short legitimate backoffs without letting one header stall the run.

## Security boundary

Sources are maintainer-authored today, but the SSRF/abuse boundary is already firm for when onboarding
opens to submissions: https-only, per-provider host allowlists, private/loopback/link-local IP
rejection, bounded redirects + response size, ffmpeg `-protocol_whitelist`, defusedxml parsing. See
[SECURITY.md](SECURITY.md). **Any future LLM output is treated as untrusted** and must never overwrite
official links, titles, dates, or transcript text.
