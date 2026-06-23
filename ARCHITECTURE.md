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
own concurrency group **sharded** (`strategy.matrix.shard`). The **Audio** (and unscheduled
**align**) lane is **source-atomic** — a `source_key` goes to exactly one shard — because that lane
is throttled per source (per-source encode caps, Granicus media leases, the provider circuit), so the
provider, not the runner, is the ceiling (review/18 §2.3). The **transcribe** lane plans **per
`(source, uid)` episode** (review/18 §3.1): each episode is independent GPU work, so one skewed
source (e.g. a 2,000-episode Granicus backlog) spreads across all shards instead of pinning to one.
Assignment is weighted by each lane's own remaining-work estimate — pending playable/unknown
recording duration plus a small withheld-media recovery-recheck cost for Audio;
routing-aware runner cost per pending episode for ASR. The ASR estimate separates duration-weighted
local inference from cheap external dispatch, blocked/deferred inspection, and already-in-flight work;
`pending_transcribe_items` emits the same per-episode classification so the plan agrees with the
aggregate weight. When the same stable meeting appears in multiple configured source views, matching
`(uid, ASR recipe)` items are assigned to one shard and charged once; a thread-safe run-local result
cache reserves each in-flight recipe and writes that one inference result to each source-scoped
transcript key, preserving the durable blob layout while avoiding duplicate native ASR. The recipe
includes the stable author/body/title prompt and decoding hints in addition to audio/model/version.
Until H14 registers a real external backend,
recordings above the local duration ceiling contribute only the cheap blocked cost rather than their
full audio duration. `asr.yml` restores durable B2 state once in its reconcile/planner job, computes a
versioned `unit=episode` assignment from that canonical snapshot, and uploads the snapshot plus plan as
an immutable workflow artifact. Every matrix shard consumes that same artifact and skips its own full
B2 restore. H14 extends the planner's route classifier with one budget/capacity snapshot; individual
matrix shards must not race to predict changing GPU availability.
The Audio lane runs its CLI inside the version-pinned
`ghcr.io/bashfulbits/citypods-audio-runner:py312-ffmpeg71-v1` image. That image is built weekly and on
runtime-definition changes from a digest-pinned Python base plus a checksum-pinned static
ffmpeg/ffprobe bundle. The image is pulled at step time rather than configured as a job-level
container: if GHCR is unavailable, the job can continue on the host with the same verified static
bundle restored through `actions/cache`; no `apt-get` or Ubuntu mirror is on the Audio critical path.
The ASR lane reuses that checksum-pinned static ffmpeg/ffprobe cache directly on the host, while its
multi-gigabyte Whisper weights remain outside the runtime image in the existing Actions-cache →
Hugging Face/B2 fallback cascade. This avoids large repeated container pulls and keeps model selection
independent from native-tool provisioning. Local faster-whisper/stable-ts inference runs in one
persistent spawned worker process, not an unkillable daemon thread in the orchestrator. The worker
retains its model cache across episodes but can be terminated and restarted on an item timeout;
the timed-out episode records an exponential durable backoff while unrelated episodes continue.
Packaging follows the lane boundary: scheduled fresh ASR installs `asr-transcribe` (faster-whisper
only), a future align-only job installs `asr-align` (stable-ts), and diagnostics install `asr-bench`;
the legacy aggregate `asr` extra remains available for contributors needing all three surfaces.
Encoding/transcription can never block or redden the Pages deploy (H11b), and concurrent shards clear
the backlog without clobbering records (H6b). The render phase writes **only `docs/`**: it persists no
records, leaving the
audio/ASR workflows as the sole record writers; a sharded run pushes back only the `source_key`s it owns
(`statesync.py`'s `push_records_merged`) and skips the reconcile sweep (`full_run=False`), so a
stale or partial push can't clobber a sibling shard's records (the record-write race). Each `citypods
enrich` job pins one **lane** — `audio`, `transcribe`, or `align` — so the two ASR models never co-load
on one runner. Because the audio and ASR workflows write the *same* `source_key`'s `episodes.json` at
overlapping times, the scoped push is also **foreign-block-preserving**: a lane owns only its derived
artifact (`audio` vs `transcript`, per `records.protected_blocks_for_lane`), so on push it re-reads the
freshest remote and keeps the block it doesn't own — closing the cross-*lane* lost update the per-shard
scope alone does not (`records.merge_preserving_foreign`; `stages.LANE_STAGES` keeps each lane to its own
work-class stages so it never re-derives a foreign block — review/12 §H6). When transcribe runs
per-episode, the same merge also preserves **across uids**: a shard passes its `owned_uids`, so it
writes a `transcript` block only for the uids it owns and keeps the freshest remote for siblings'
uids — the cross-*uid* lost update two shards splitting one source would otherwise hit (review/18 §3.2). The heavy
`enrich` phase processes its backlog as a **global, policy-ordered two-pass queue** (`ops/workqueue.py` +
`run.py`): prepare every source, then run an on-runner **audio pass** (`chapters→timeline→remap→audio`,
newest-everywhere-first across all sources) followed by a **decoupled transcript pass**. The transcript
pass is *dispatch-not-await-ready* — transcription/diarization will run on external workers and reconcile
from durable state on a later deploy (design: [`review/12` §H5](review/12-hardening-and-efficiency.md)).

## Module map (`citypods/`)

| Area | Modules |
|---|---|
| **Providers** | `providers/{base,granicus,civicplus,civicclerk,swagit}.py` — `MeetingProvider` Protocol + registry; each normalizes to the episode model. |
| **Records / identity** | `records.py` — stable `uid`, `source_key`, `audio_spec_hash`, `feed_content_hash`, append-only `merge_persisted`, content-addressed keys, orphan-GC refs. `models.py` — `Episode`/`City`. `availability.py` — versioned `media_availability` classification (H16 PR3). |
| **Enrichment stages** | `stages.py` — `EnrichmentStage` Protocol + `default_stages()` (`Chapters→Timeline→Remap→Audio→Transcript→Links`); `StageContext`, `StageStats`, the wall-clock `stop()` budget. |
| **Scheduling / backlog** | `ops/workqueue.py` — the `backlog_priority` policy (comparator registry: windowed `recency`, `city_order`, `body_order`, `feed_visible_first`, …), the derived **work manifest** (`WorkItem` per episode × output `work_class`, persisted to `state/work.json`), and the `lease`/`release`/`is_leased` API — the coordination substrate for off-runner ASR/diarization workers (H6b/H9). |
| **Timeline / EDL** | `timeline.py` (served↔source map), `silence.py` (trim planner), `concat.py` (multi-segment), `clips.py` (clip/soundbite extraction). |
| **Media / audio** | `media.py` — timeline-aware ffmpeg mastering, pinned threads, sample-accurate bounded-memory single-source timeline cuts, versioned multi-mic speech profile (high-pass → dynamic leveling → gentle compression), two-pass measured **linear** EBU R128 normalization via a temporary FLAC, a constant-gain + 192 kHz short-lookahead limiter fallback for peak-constrained recordings, and content-addressed upload. |
| **Transcripts** | `asr.py` — forced alignment (stable-ts) / fresh transcription (faster-whisper) with align-error fallback; emits a clean **segment-cue VTT** (served via `<podcast:transcript>`) **plus a word-level JSON sidecar** (`…-asr-<recipe>.words.json`) for search/clips/diarization; version-aware re-transcribe on an `ASR_PIPELINE_VERSION` bump (provider transcripts never invalidated); both objects are content-addressed + GC-referenced. `bench.py` — `asr-bench` diagnostic. |
| **Compute backend** | `compute/{base,local,budget,dispatch}.py` (H13 + H14a, **pre-1.0 lock**) — the pluggable GPU/ASR execution seam, peer of `storage/`. `base.py` defines `InferenceJob(task, inputs, recipe_hash)` (`task` typed for the full §5.5 verb set: ASR `transcribe`/`align`/`diarize` + reserved LLM `summarize`/`tag`/`soundbite-select`), `JobResult`/`JobHandle`, a `runtime_checkable` `Backend` protocol `run_inference(job)`, and (H14a) a `DispatchBackend` protocol (returns a `JobHandle` + `estimate_gpu_seconds`). `local.py` runs faster-whisper/stable-ts in-process (**byte-identical** to the pre-refactor path). **H14a substrate:** `budget.py` is the free-tier ledger (`state/compute_budget.json`, statesync-backed) that makes exceeding a backend's `monthly_gpu_seconds`/`max_inflight` structurally impossible (the **$0 guarantee**); `dispatch.py` adds the router (fill free tiers → consider `local`), a thread-safe `DispatchCoordinator` (records a live `work.json` lease `lease_owner="modal:<job_id>"` + decrements budget), and `reconcile_compute` (reap dead workers → re-queue; settle completed jobs; run at `asr.yml` start via `citypods compute reconcile`). `make_compute` selects by `compute_backend`: `local` (bypass) or `auto` (default). `TranscriptStage` attempts external dispatch first; only a declined job enters local admission. Known recordings above `asr_local_max_duration_hours` (production: 4h; non-positive disables) remain queued with `reason=external-required` instead of starting in-process inference. H14b/H14c register the real Modal/Beam **dispatch** adapters into the coordinator with no protocol change. |
| **Feeds / site** | `feeds.py`, `render.py`, `site.py`, `templates/*.j2`, `artwork.py` (cover art). |
| **Orchestration** | `run.py` — `SourcePipeline`, `build()`, the **global two-pass enrich queue** (`_run_enrich_global_queue`: newest-everywhere-first on-runner audio + decoupled transcript), run history, graceful yield, resource-guard wiring. `resources.py` — process resource snapshots + memory/load admission guard for expensive native work. `cli.py` — `build / render / enrich / report / doctor / bodies / asr-bench / rebuild-audio / admin`. |
| **State** | `state.py` (build fingerprint), `statesync.py` (bucket↔local; bucket is truth), `storage/{base,local,s3}.py` (`S3CompatibleStorage` b2/r2 presets + local). |
| **Ops / QA** | `audit.py` (+ `scripts/audit_feeds.py`) feed-health; `contracts.py` endpoint contracts; `report.py` + `projection.py` cost/throughput + `/admin/status`; `validate.py` feed validation. |

Scoped workflow telemetry is append-only under `state/run_events/`. Sibling matrix events sharing
`GITHUB_RUN_ID` + phase + lane form one logical run; status/projection aggregates them only after every
declared shard reports, so a fast partial matrix cannot replace the last complete KPI. Each stage also
persists stable `defer_reasons` counters (`insufficient-budget`, `external-required`,
`timeout-backoff`, `alignment-disabled`, and `dispatched-prior-run`), surfaced beside the deferred
total on `/admin/status`.
| **Security** | `security.py` — SSRF gate (`validate_source_url`), host allowlists, redirect/size caps; `http.py` retry/backoff; ffmpeg protocol whitelist; defusedxml. |

## Key invariants (why it extends cleanly)

- **Provider Protocol + registry** — a new platform is a new adapter, no core change.
- **Stage Protocol + `default_stages()`** — a new per-episode feature is a new stage.
- **Split hashes** — `audio_spec_hash` (bytes) and `feed_content_hash` (RSS) invalidate independently.
- **Content-addressed audio + stable UID** — CDN cache-bust, rollback, and provider-migration safe.
- **Append-only archive** — meetings that drop off a provider feed are never lost (#52).
- **Durable media-availability verdict** — `availability.py` carries a versioned `media_availability`
  projection on the record (available / suspected-or-confirmed empty / missing / invalid / recovered
  + operator override). It rides the audio lane's `SilencePlanner` decode (no extra ffmpeg pass);
  withheld verdicts drop the episode from both feeds (`feeds.enclosure_url`) and from `AudioStage`,
  so an empty/missing source is never published while metadata stages keep flowing to the meeting
  page. Confirmation needs two independent successful silent fetches — a transport failure never
  confirms — and re-evaluation (detector version / source fingerprint / detect profile / operator
  override) is decoupled from `AUDIO_PIPELINE_VERSION`, so re-classifying never re-encodes (H16 PR3).
- **Timeline served↔source EDL** — silence-trim/concat/intro/transcripts/clips all reduce to one
  served-vs-source time map (see [`review/08`](review/08-timeline-and-content-transforms.md)).
- **Bucket-as-truth state** — derived artifacts survive Actions cache eviction.
- **Wall-clock budget + graceful yield** — heavy work runs until a time window closes or a newer run
  queues; cheap idempotent bookkeeping always finishes (see `stages.py` "stop convention").
- **Graceful SIGTERM + mid-run checkpoint** — the CLI entry installs a SIGTERM handler
  (`install_signal_handlers`) that latches a process-wide interrupt the `StopSignal` predicate ORs
  in, so a GitHub cancel / lost-comms kill converts into the same graceful-stop path as a wall-clock
  yield: in-flight workers defer, then the run still persists records and writes its `run_history`
  entry instead of dying mid-queue. The global enrich queue persists every source once the **audio
  pass** drains (before the decoupled transcript pass) and again at the end, so the unpersisted
  window is one pass, not the whole run; the repeat persist is idempotent (append-only
  `merge_records`). An interrupted run is tagged `interrupted`/`outcome:"interrupted"` in
  `run_history.jsonl` and exits `143` (128+SIGTERM) so a cut-short run isn't mistaken for a clean
  success — a normal wall-clock/supersession yield is **not** an interrupt and still exits `0`.
- **Resource admission for expensive native work** — ffmpeg/ASR starts can wait for memory/load
  headroom; ASR lanes have a 285m start cutoff and 350m backstop, and use a rolling
  `state/asr_runtime_log.json` ratio buffer to estimate whether a recording can finish before the
  cutoff before starting native transcription. That estimator is a time model, not a peak-memory
  model: a separate configurable local-only duration ceiling prevents multi-hour faster-whisper input
  from overflowing onto the GitHub runner after external dispatch declines. The guard runs before the
  ASR semaphore/download when duration is already known and again after hosted-audio probing; external
  workers are exempt. Abandoned ASR inference continues to occupy its worker slot until the native
  thread exits, so a stopped item does not stack unbounded CPU/RAM work or wait past the Actions hard
  cap. Audio encodes are admitted by a
  **memory reservation** (`MemoryReservation`): production `podcast-speech-v2` encodes reserve a fixed
  768 MiB because both passes are streaming and the lossless intermediate lives on disk, so even an
  all-day meeting has bounded RSS. Monotonic silence timelines use one streaming selector with
  one-sample frames only around cut boundaries, avoiding the old parallel-`atrim` buffering while
  preserving the 48 kHz sample count. The first pass reads provider media once and writes/measures a
  leveled mono FLAC; the second reads that local file and applies measured linear loudnorm or the
  bounded peak limiter + AAC. Fetch, measure, and finalization are admitted separately under the same
  native-process ceiling so provider slots and CPUs can remain occupied without exceeding
  `native_audio_max_active`. The old duration-scaled 64 MiB/min, 12,000 MiB max/unknown model remains
  only for the disabled legacy dynamic-loudnorm path. The mid-flight kill floor
  (`audio_ffmpeg_memory_floor_mb`) stays as the backstop. `TimelineStage`'s `SilencePlanner` runs
  before `AudioStage` and shells out to ffmpeg `silencedetect`, so its pass is admitted through the
  same `NativeWorkGate` (`kind="audio"`) and pinned to the same `-threads` count as `CommandFfmpeg`'s
  encodes — otherwise an unthreaded, ungated silencedetect call defaults to "all cores" and can
  oversubscribe the box alongside (or ahead of) the gated encodes it's meant to share CPU with.
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
  (`provider_rate_limits` in `site_config.yml`, currently `granicus.com: 1`) and is acquired by
  `GuardedHTTPAdapter.send`, ffprobe bitrate/duration probes, and ffmpeg fetch paths. The B2-compatible
  `provider_distributed_leases` layer adds soft candidate-election leases around ffprobe/ffmpeg media
  reads, capping aggregate Granicus overlap across the four audio shard processes (currently 2 total
  slots after the GH#300 Phase 1 reduction). Candidate keys provide immutable FIFO order; waiting and
  acquired candidates renew payload expiry without changing their election position. Active holders
  stop renewal before best-effort deletion, dead owners are reaped after expiry, and payload metadata
  identifies the GitHub run/job that held a stale claim. Storage backends with `get_file` use the
  payload expiry as authoritative; modification time plus TTL is the compatibility fallback. Keys are
  registrable domains so the Granicus-owned Swagit CDN (`*.granicus.com`) is matched by the host the
  tenant sees. Both `HostRateLimiter` and `DistributedProviderLeasePool`, plus `SourceCache`'s
  per-uid fetch lock, accept an optional `stop` predicate and raise `StopRequested` if it fires
  before the wait acquires its slot/lease/lock — so a worker idle past the run's wall-clock budget
  yields immediately instead of blocking out a full queue/lease cycle. `CommandFfmpeg` and
  `SourceCache` bind `stop` once at construction; `_encode_one`, `SwagitConcatPlanner`, and
  `SilencePlanner` treat `StopRequested` as a non-failure defer, the same as a circuit deferral. The
  ffmpeg subprocess call itself remains out of scope — `stop()` can't preempt a thread parked in
  `subprocess.run`, only `audio_encode_timeout_minutes` bounds that.
- **Provider circuit admission happens at the subprocess boundary and is shared across Audio
  shards.** Audio first checks the circuit before entering expensive work, then refreshes its
  authoritative storage marker after distributed and process-local provider slots are acquired and
  immediately before ffmpeg/ffprobe starts. Circuit counters/open markers use deterministic ordinary
  storage objects; a separate one-slot FIFO provider lease serializes mutations because the storage
  API has no compare-and-swap primitive. A circuit that opens while a worker waits therefore defers
  that item without recording a materialization failure/backoff, and all shards observe the same
  threshold/cooldown state.
- **Granicus circuits isolate tenants before escalating domain-wide.** Native archive paths identify
  their tenant (`archive-video.granicus.com/<tenant>/…`); tenant subdomains and the Granicus-owned
  Swagit media host receive stable tenant keys under the shared `granicus.com` domain. Three direct
  throttles open only that tenant's circuit. Two distinct tenant trips within the cooldown window open
  the emergency domain circuit. A domain marker supersedes tenant markers until one storage-claimed
  half-open canary succeeds; an abandoned canary can be reclaimed after its probe TTL. Per-scope run
  telemetry records direct throttles, trips, circuit deferrals, recovery probes/recoveries, while the
  existing domain entry continues to carry lease acquisition/wait/renewal and stale-owner cleanup.
- **Planner throttles are the materialization attempt; Audio does not immediately repeat them.**
  `TimelineStage` can fetch provider media through `SilencePlanner`/`SourceCache` before `AudioStage`.
  A typed 403/429 there records one episode materialization attempt/backoff and halts that episode's
  remaining audio-stage chain. Circuit-open planner work remains a non-failure: it is queued with no
  attempt history, exactly like an AudioStage circuit deferral.
- **Circuit-deferred work is parked and can recover inside the same Audio run.** The global audio queue
  first lets every ordinary candidate—including other providers—drain, retaining circuit-open items
  as a parked set. When the configured cooldown expires (unless the wall-clock/supersession stop signal
  fires), exactly one parked item runs as a half-open canary. A 403/429 immediately reopens the circuit;
  a complete materialization closes it and releases the remaining parked work through the existing
  process-local and distributed provider limits. Deferral telemetry remains cumulative, while recovered
  items are removed from the run's final backlog count.
- **Manual Granicus probes share Audio's workflow queue.** `granicus-probe.yml` uses the same `audio`
  concurrency group with cancellation disabled, then verifies no active/queued Audio run before
  touching provider media. Its low-volume transport mode alternates curl/ffmpeg ordering against the
  same archive objects, captures selected redacted HTTP timing/range metadata, and can prove one
  size-capped curl download through local ffprobe/ffmpeg. Its sustained mode retains the
  request-count/volume/cooldown/concurrency matrix. Its gated `worker` mode compares direct access
  with an authenticated Cloudflare Worker on the same GitHub runner, can prove one Worker-streamed
  file through local ffprobe/ffmpeg, and can optionally run one full Arlington/Pflugerville source
  through the production source-cache plus speech-mastering recipe. A Worker HTTP 200 that ignores
  Range is reported as access success with `range_unsupported`, not as a failed size cap. The Worker
  hard-codes the Granicus archive origin, requires a
  bearer secret, restricts tenants and tenant-prefixed MP4 names, refuses queries/redirects, forwards
  only range/cache validators, and streams `no-store`; it is not a general proxy. A scheduled Audio
  run therefore cannot slip into any experiment after its isolation check.
- **Native Granicus media uses one direct-first alternate-egress fallback.** Production ffmpeg first
  requests the validated canonical `archive-video.granicus.com` object normally. Only an HTTP 403
  can rewrite that strict tenant/filename input to the authenticated Worker; malformed/query URLs,
  other hosts, and other errors never route through it. The retry remains inside the original
  Granicus local limiter, distributed lease, and circuit admission. Worker success does not trip the
  circuit; Worker throttling is recorded once before releasing the lease. Each attempt and its
  outcome are counted per tenant on the circuit (`worker_fallback_attempts`/`successes`/`failures`),
  flowing through the per-run summary and cross-shard report merge so activation is measurable. A
  half-set or invalid `GRANICUS_PROXY_*` configuration disables the fallback for the run (warned once)
  rather than turning an already-handled 403 into a shard-aborting error. The bearer header is never
  logged, the Worker endpoint that ffmpeg echoes in stderr on error is scrubbed before any log line,
  and exceptions expose only the original direct command. Official episode URLs and audio artifact
  identity remain unchanged.
- **Every scheduled Audio run produces one H16 acceptance artifact after the matrix joins.** Each
  shard records per-tenant direct Granicus success/403 and truncation telemetry alongside the
  existing Worker/circuit counters, scans its own log for credential-shaped material, and uploads
  only the run event plus redacted scan findings. The `validate-h16` job merges all four shards,
  verifies the configured 1-local / 2-distributed ceiling, and writes JSON plus a GitHub summary.
  The Audio lane also snapshots every Granicus record immediately after provider/persisted-record
  merge, then verifies post-media stable UID/GUID, official/source URLs, source duration, and
  deterministic content-addressed artifact identity — exempting reused migrated `legacy` artifacts,
  whose pre-content-addressing key/spec/url legitimately differ from the recompute (GH#353). Missing
  shard or Granicus activity yields
  `insufficient_activity`; transport, identity, concurrency, or secret findings yield `fail`. The
  report is observational and does not gate, mutate, or invalidate audio.
- **All media subprocess surfaces use generic credential redaction.** Before ffmpeg/ffprobe stderr,
  timeout/error payloads, or command arguments can reach a log or higher-level exception, media URL
  query strings are removed and bearer/credential-shaped values are replaced. Host, path, process
  status, and HTTP status remain visible for diagnosis. Worker endpoint redaction remains an
  additional layer because that origin is itself secret.
- **Worker deployment is path-scoped.** `granicus-worker-deploy.yml` runs Worker tests and deploys
  only when `main` changes the Worker source, Wrangler config, or deployment workflow (plus a manual
  dispatch escape hatch). A scoped Cloudflare API token/account ID authenticate deployment. The
  runtime `PROXY_TOKEN` remains Cloudflare-managed and is not copied into deployment CI.
- **The process-local slot is acquired before the distributed lease (issue #342).** The ffmpeg/ffprobe
  guard always enters `HOST_LIMITER` first and `provider_distributed_leases` second, for both
  monitored and unmonitored ffmpeg paths and the ffprobe probe. A thread still queued behind its own
  process's local cap must never already hold a cross-shard slot — acquiring distributed-first let one
  early-starting process's other threads win every distributed candidate while they waited locally,
  starving other shards of capacity they could otherwise use.
- **The circuit is recorded/opened at the subprocess boundary, before its provider lease is released
  (issue #343).** `_raise_if_rate_limited` classifies a throttled ffmpeg/ffprobe exit and updates the
  shared circuit breaker from inside the same `with` that still holds the `HOST_LIMITER` and
  distributed-lease slots, for both the `subprocess.run` and monitored/`Popen` paths. Recording it only
  at the higher-level materialization caller — after that `with` had already exited — left a window
  where a queued waiter could acquire the just-released lease, pass the still-closed circuit check, and
  start one extra ffmpeg process per threshold crossing. `RateLimitedMediaFetchError` carries
  `circuit_recorded`/`opened_domain` so the caller does not double-record a failure the boundary already
  handled.
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
