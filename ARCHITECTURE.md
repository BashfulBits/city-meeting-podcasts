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
  ├─ optional verified calendar companion (Legistar / CivicClerk / PrimeGov-OneMeeting)
  │  ├─ recorded rows merge by canonical provider clip ID
  │  └─ no-video rows ──► records.assign_uids()
  │       └─► records.merge_calendar_records()
  │            └─► state/sources/<key>/calendar.json ──► city archive pages
  │                                                 (no RSS, meeting page, or episode stages)
  │
  ▼ recorded rows only
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
`audio.yml` (ffmpeg encode → object storage) and `asr.yml` (faster-whisper transcription). `audio.yml`
remains source-sharded; `asr.yml` now launches a matrix of **identical pull workers** that all run
`citypods compute run-internal-worker` against the shared Stage-2 lease ledger. The **Audio** (and
unscheduled **align**) lane is **source-atomic** — a `source_key` goes to exactly one shard — because that lane
is throttled per source (per-source encode caps, Granicus media leases), so the
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
full audio duration. `asr.yml` now restores durable state once in its reconcile job, rebuilds the work
manifest from canonical records, reaps expired work leases, and then starts identical local pull
workers; the matrix no longer consumes a static transcribe-plan artifact. H14 extends the planner's
route classifier with one budget/capacity snapshot; individual workers must not race to predict
changing GPU availability.
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
the timed-out episode records an exponential durable backoff — enforced on the next admission
attempt, by any worker, not merely recorded — while unrelated episodes continue.
Packaging follows the lane boundary: scheduled fresh ASR installs `asr-transcribe` (faster-whisper
only), a future align-only job installs `asr-align` (stable-ts), and diagnostics install `asr-bench`;
the legacy aggregate `asr` extra remains available for contributors needing all three surfaces.
All Python installs — CI, the runner image, and the external Modal/Beam worker images — resolve against
compiled **version-pinned** `constraints/*.txt` (one source of truth; [`review/22`](review/22-dependency-and-reproducibility-policy.md)),
so a fresh install cannot silently drift; third-party GitHub Actions are commit-SHA-pinned and the
Hugging Face Whisper model revisions are pinned (canonical in `citypods.asr`, baked into the worker
images), with Renovate opening update PRs and CI guards keeping the pins current and enforced.
Modal's build genuinely stages local repo files at build time (`add_local_dir`/`add_local_file`), so
its image installs directly against `constraints/asr.txt`. Beam's remote build has **no** such
access — its `add_local_path()` only syncs files for the deployed function's own runtime import, not
for `add_commands()`'s build steps (confirmed against the SDK: `Image.add_python_packages()` given a
file path reads it locally, before anything reaches Beam's backend). So `beam_app.py` resolves the
same pinned package set and HF model constant **locally**, on the machine invoking `beam deploy`, and
bakes the literal resolved values into the image spec instead of referencing the files by path.
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
Audio planning remains source-atomic for record safety, but source keys that reference the same
configured city entity are assigned to one shard. Within that process, `AudioArtifactCache` coalesces
identical `(provider, stable uid, audio recipe)` candidates: one alias encodes or reuses the artifact
and every other source-local record adopts that single CAS pointer (GH#421).

## Module map (`citypods/`)

| Area | Modules |
|---|---|
| **Providers** | `providers/{base,granicus,legistar,civicplus,civicclerk,swagit}.py` — `MeetingProvider` Protocol + registry; each normalizes to the episode model. Granicus is archive-first; verified calendar companions are explicit config, never guessed at runtime. Swagit keeps first-party video discovery and carries its per-video agenda/minutes links directly from the archive list page. |
| **Records / identity** | `records.py` — stable `uid`, `source_key`, entity-level `source_family_key` (audio shard affinity only), `audio_spec_hash`, `feed_content_hash`, append-only `merge_persisted`, and a separate append-only `calendar.json` metadata catalog. Calendar video rows merge by canonical clip ID; no-video rows are never Episodes or audio work. Also owns content-addressed keys, orphan-GC refs, and canonical persisted duration scalars (`source_duration_seconds`, `served_duration_seconds`) with compatibility reads for legacy `duration` / `audio.duration_served`. `models.py` — `Episode`/`AgendaRecord`/`City`. `availability.py` — versioned `media_availability` classification (H16 PR3). |
| **Enrichment stages** | `stages.py` — `EnrichmentStage` Protocol + `default_stages()` (`Chapters→Timeline→Remap→Audio→Transcript→Links`); `StageContext`, `StageStats`, the wall-clock `stop()` budget. |
| **Scheduling / backlog** | `ops/workqueue.py` — the `backlog_priority` policy (comparator registry: windowed `recency`, `city_order`, `body_order`, `feed_visible_first`, …), the derived **work manifest** (`WorkItem` per episode × output `work_class`, persisted to `state/work.json`), and the `lease`/`release`/`is_leased` API — the coordination substrate for ASR/diarization workers. `ops/work_leases.py` (H17 Stage 2, review/18 §4) — the **pull-based lease ledger**: per-item CAS objects `work-leases/<source>/<uid>.json` on R2 + `claim`/`renew`/`release`/`reap`. Both external GPU workers and the in-Actions internal worker now pull against this same ledger; `compute reconcile` rebuilds the manifest from canonical records before sweeping expired leases, so queue-ordering changes are no longer gated on an enrich shard finishing first. |
| **Timeline / EDL** | `timeline.py` (served↔source map), `silence.py` (trim planner), `concat.py` (multi-segment), `clips.py` (clip/soundbite extraction). |
| **Media / audio** | `media.py` — timeline-aware ffmpeg mastering, pinned threads, sample-accurate bounded-memory single-source timeline cuts, versioned multi-mic speech profile (high-pass → dynamic leveling → gentle compression), two-pass measured **linear** EBU R128 normalization via a temporary FLAC, a constant-gain + 192 kHz short-lookahead limiter fallback for peak-constrained recordings, content-addressed upload, and run-local duplicate-view artifact coalescing. |
| **Transcripts** | `asr.py` — forced alignment (stable-ts) / fresh transcription (faster-whisper) with align-error fallback; emits a clean **segment-cue VTT** (served via `<podcast:transcript>`) **plus a word-level JSON sidecar** (`…-asr-<recipe>.words.json`) for search/clips/diarization; version-aware re-transcribe on an `ASR_PIPELINE_VERSION` bump (provider transcripts never invalidated); ASR keys are content-addressed by transcript media/timeline + ASR recipe, not by audio mastering bytes, and old audio-spec-derived ASR VTT/word objects migrate by copy before any missing artifact regenerates. `stages.py` also fetches city/provider source transcript documents into `provider_transcript.candidate` under content-addressed `provider-` keys, remaps timed provider VTT/SRT cues through `timeline.py` into served-time `provider-align` VTT artifacts, stores `float \| null` confidence, and promotes candidates to `known_good` only when they are at least as good as the prior known-good; selected provider-aligned VTTs can produce a separate content-addressed `speakers.json` block when they already contain `SPEAKER: text` labels, and diarization failures never discard the active transcript. Render/feed code exposes `known_good` as **Original city-provided transcript** and lets it fill `<podcast:transcript>` only until an ASR/provider-aligned transcript is complete. `bench.py` — `asr-bench` diagnostic. |
| **Compute backend** | `compute/{base,local,budget,dispatch,external_worker,worker_telemetry,policy}.py` (H13 + H14a/H14b/H14c/H14d/H19, **pre-1.0 lock**) — the pluggable GPU/ASR execution seam, peer of `storage/`. `base.py` defines `InferenceJob(task, inputs, recipe_hash)` (`task` typed for the full §5.5 verb set: ASR `transcribe`/`align`/`diarize` + reserved LLM `summarize`/`tag`/`soundbite-select`), `JobResult`/`JobHandle`, a `runtime_checkable` `Backend` protocol `run_inference(job)`, and the internal-GitHub `DispatchBackend` protocol (returns a `JobHandle` + `estimate_gpu_seconds`). `local.py` runs faster-whisper/stable-ts in-process. `budget.py` is the free-tier ledger (`state/compute_budget.json`) that makes exceeding a backend's configured budget/capacity structurally impossible (the **$0 guarantee**); on an R2 (`cas_capable`) backend it is CAS-backed, so concurrent reservations cannot overspend. H14d now keys spend to **provider-cycle dollars** per backend, with config-driven `rollover_day_of_month`, cycle-aware settlement, and a persisted runtime-estimate model that learns `runtime-seconds / audio-second` by backend/task/GPU/model/compute profile. `policy.py` parses the richer backend YAML (`compute_backends.<backend>.{hardware,budget,dispatch,tasks}`): GPU target, provider-cycle dollar caps, preferred run days, long-meeting preference, freshness windows, and fixed-per-run / fixed-per-claim planning knobs. `dispatch.py` remains the internal coordinator for GitHub workers and `compute reconcile`; it also reaps Stage-2 pull-worker leases and releases/settles any preempted worker budget reservation. `external_worker.py` now holds the shared pull-worker core for both external and internal ASR workers: claim/adopt/write/settle orchestration is common, while backend-specific admission and supervision live in worker subclasses. Modal/Beam reserve provider budget, renew leases during long inference, retry once, and settle provider spend. The GitHub internal worker instead uses a persistent killable local inference daemon, prefers shorter known-duration recordings, enforces the hard `asr_local_max_duration_hours` ceiling, refuses to start a claim whose estimated runtime no longer fits before the 350-minute backstop, and learns its own runtime coefficient in the same runtime-estimate ledger. A locally timed-out claim terminates the child process, records timeout backoff on that episode (checked by every worker's admission before a future claim, not merely recorded), and abandons the lease back to the queue rather than failing it terminally; a superseded claim (a newer run queued behind it) terminates and abandons the same way but records no backoff, since the item itself did nothing wrong. `worker_telemetry.py` records non-secret per-claim peak RSS/GPU-VRAM samples in the CAS-managed `state/asr_worker_telemetry.json` object for `asr-worker-report` and admission tuning. H14b/H14c are combined-capable by contract but enable only `transcript-asr`; `transcript-diarize` is reserved for future diarize-only claims over transcripts produced by GitHub ASR. Per-backend worker caps such as `max_claims` default from `config/site_config.yml` and may be overridden by deploy env for canaries/manual runs; `max_claims` caps **new transcriptions** — an item whose content-addressed artifacts already exist is *adopted* (state reconciled, no GPU) without consuming a slot, so the loop scans past a stale-manifest head of already-done items to reach fresh work, bounded by `max_scan` (default `max_claims + 50`). Known recordings above `asr_local_max_duration_hours` (production: 4h; non-positive disables) remain queued with `reason=external-required` instead of starting local inference. |
| **Feeds / site** | `feeds.py`, `render.py`, `site.py`, `templates/*.j2`, `artwork.py` (cover art). |
| **Orchestration** | `run.py` — `SourcePipeline`, `build()`, the **global two-pass enrich queue** (`_run_enrich_global_queue`: newest-everywhere-first on-runner audio + decoupled transcript), pre-dispatch duration normalization (bounded hosted-audio probe, missing-duration warning telemetry, no timeline/source fallback writes), run history, graceful yield, resource-guard wiring. `resources.py` — process resource snapshots + memory/load admission guard for expensive native work. `cli.py` — `build / render / enrich / report / doctor / bodies / asr-bench / rebuild-audio / admin`. |
| **State** | `state.py` (build fingerprint), `statesync.py` (bucket↔local; bucket is truth), `storage/{base,local,s3}.py` (`S3CompatibleStorage` b2/r2 presets + local). |
| **Ops / QA** | `audit.py` (+ `scripts/audit_feeds.py`) feed-health; `contracts.py` endpoint contracts; `report.py` + `projection.py` cost/throughput + `/admin/status` (including provider-transcript rollout slices for fetch, align, diarize, confidence, rollback history, recovery guidance, and the H15 transcript-quality trust/calibration panel); `validate.py` feed validation. `report._classify_record` assigns each episode one **mutually-exclusive state** — `served` / `stale` (hosted; `stale` = the current recipe would re-encode it, computed by recomputing `audio_spec_hash` per record; a `legacy`/`None` spec hash counts as `served` only under the default profile, but classifies `stale` when a loudness or processing profile override is set) / `linked_video` (direct MP4, config says don't host) / `deferred` / `dead` / `transient_error` (in #120 backoff) / `pending` — which drives the dashboard taxonomy, `gb_exact` (false when any hosted record predates `audio.bytes`), and the archive-cap cost slider (#124). |
| **Manual recovery** | `scripts/normalize_durations.py` + `.github/workflows/duration-normalize.yml` — manual dry-run-first catalog repair that probes hosted audio by object key (range reads only), leaves missing served duration unset when no canonical probe is available, uploads JSONL/summary artifacts, and on apply pushes only touched source records through the audio-lane-safe merge path. |

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
- **Duplicate source views share audio work without changing durable identity** — feeds under one
  configured city entity are co-located on an audio shard; identical stable-uid + recipe candidates
  converge on one CAS object while retaining separate source records. No source-key or pipeline-version
  migration is required (GH#421).
- **Append-only archive** — meetings that drop off a provider feed are never lost (#52).
- **Durable media-availability verdict** — `availability.py` carries a versioned `media_availability`
  projection on the record (available / suspected-or-confirmed empty / missing / invalid / recovered
  + operator override). It rides the audio lane's `SilencePlanner` decode (no extra ffmpeg pass);
  withheld verdicts drop the episode from both feeds (`feeds.enclosure_url`) and from `AudioStage`,
  so an empty/missing source is never published while metadata stages keep flowing to the meeting
  page. Confirmation needs two independent successful silent fetches — a transport failure never
  confirms — and re-evaluation (detector version / source fingerprint / detect profile / operator
  override) is decoupled from `AUDIO_PIPELINE_VERSION`, so re-classifying never re-encodes (H16 PR3).
  A **confirmed-dead** verdict (`confirmed_empty`/`missing`/`invalid`) polls on a flat 30-day cadence
  (`records.confirmed_dead_recheck_due`) rather than the exponential #120 backoff, and the audit
  treats withheld media as terminal for timeline-audio repair — no `rendered-duration-mismatch`
  finding for a quarantined episode. A `timeline-replan` repair flag bypasses the exponential backoff
  for transient/broken-EDL episodes, but the flat confirmed-dead cadence takes precedence (the
  audit-owned integrity block can't be lane-cleared, so a flag never forces an every-run re-decode of
  quarantined media) (GH#795, [`review/20`](review/20-timeline-audio-integrity-repair.md)).
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
  headroom; internal ASR workers have a 285m start cutoff and 350m backstop, and use the shared
  `state/compute_budget.json` runtime-estimate ledger to decide whether a new claim can still finish
  before the backstop before starting local transcription. That estimator is a time model, not a
  peak-memory model: a separate configurable local-only duration ceiling prevents multi-hour
  faster-whisper input from overflowing onto the GitHub runner after external dispatch declines.
  Internal workers also prefer shorter known-duration recordings for this reason. A timeout or
  supersession stops the killable local inference subprocess and abandons the claim back to the
  queue; the timed-out episode (not a merely-superseded one) records durable timeout backoff
  instead of becoming a terminal failure, and every worker's admission check refuses to re-claim
  it until that backoff window lapses. Audio encodes are admitted by a
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
  audio + transcripts + durable state. The coordination control-plane (budget/work/provider leases)
  routes to Cloudflare R2 for compare-and-swap (`RoutingStorage`, `COORDINATION_PREFIXES`).
  **Lifecycle backstops** (as-code, `scripts/apply_bucket_lifecycle.py`, GH#496): R2 expires the
  validator's scratch prefixes `work-leases/__validate__/` + `provider-leases/validate-` after **1 day**
  (so a crashed runner's un-run cleanup can't leak scratch); B2 keeps deleted/overwritten object
  **versions for `defaults.b2_retention_days` (default 30d)** before purge — the recoverable-delete
  window the resurrection watchdog relies on. **Invariant — R2 holds only ephemeral/derivable objects:**
  R2 has no soft-delete and is aggressively expired, and its keys are excluded from the B2 state backup,
  so a canonical record there would be unrecoverable; `routing.py` enforces this (`_EPHEMERAL_R2_PREFIXES`
  + an import-time/test guard fails if a coordination prefix isn't declared ephemeral).
- **Rate-limited LLM dispatch** → `workers/llm-dispatch-proxy` is a separate Cloudflare Worker and
  private R2 queue. Its authenticated OpenAI-shaped **asynchronous** enqueue/poll API persists pending
  requests; a per-minute Cron Trigger claims one ready request with an R2 conditional write, reserves a
  durable dispatch interval slot, calls the configured OpenAI-shaped HTTPS route, and persists either
  the response or a bounded retry/failure state. R2's planned Python LLM backend will use this as its
  `JobHandle` path; direct provider translation remains LiteLLM's responsibility, either in Python or
  in an explicitly configured LiteLLM Proxy upstream. The queue is ephemeral/derivable and is not part
  of the B2-backed catalog records or the Python `RoutingStorage` control-plane prefixes.
- **Workflows** (`.github/workflows/`): `ci.yml` (ruff + pytest on PR/push), `preview.yml` (per-PR
  downloadable site preview), `deploy.yml` (**render-only** Pages publish on `main` push + 4h cron;
  retries `actions/deploy-pages` up to 3× with backoff on GitHub's own transient deploy failures),
  `audio.yml` (sharded audio materialization, 4h cron; own `audio` concurrency group),
  `asr.yml` (shared-ledger faster-whisper pull workers, every 5h; own `asr` concurrency group) — the
  two heavy record-writers, with `audio.yml` still a `--shard K/N` × `--lane` matrix while `asr.yml`
  runs identical `compute run-internal-worker` slots after a reconcile/manifest-rebuild job,
  `modal-deploy.yml` (path-scoped deploy of the
  Modal pull worker from `main`, protected by the `modal-production` GitHub Environment),
  `beam-deploy.yml` (same path-scoped deploy for the Beam pull worker, protected by `beam-production`),
  `llm-dispatch-worker-deploy.yml` (path-scoped test/deploy for the Cron-paced LLM Worker),
  `asr-worker-report.yml` (storage-only Modal/Beam/GitHub ASR completion, budget, and memory report; no GPU
  provider calls), `audit.yml` (daily feed-health → GitHub issues), `contracts.yml` (weekly live endpoint
  contracts), `asr-bench.yml` (manual ASR benchmark), `audio-gc.yml` (**"Storage reclaim"**, weekly —
  the unified reclaim policy, GH#496): reconciles the R2/B2 lifecycle rules, runs the orphan GC
  (a scheduled run **auto-deletes only the double-confirmed subset** — orphans seen across ≥2 runs past
  `defaults.orphan_quarantine_days`, tracked in `state/orphan-ledger.json`; full deletion still needs a
  manual `apply=true` dispatch from `main`), and runs the **resurrection watchdog** — every delete is
  logged to `state/reclaim-log.jsonl` with a `recover_by` deadline and, if a live record re-references a
  still-restorable reaped key, opens a `priority:high` issue to restore the B2 version before it purges.
  Opens/updates one rolling reclaimable-orphans issue + `orphans.tsv` artifact.
- **Tests** run fully offline against recorded fixtures; feeds have byte-for-byte snapshot tests.

## Provider notes & gotchas

Hard-won facts that bite anyone adding/debugging providers:

- **Granicus discovery is archive-first.** `ViewPublisherRSS.php` is hard-capped at 100 items per
  view; `ViewPublisher.php` is its uncapped native archive, so `GranicusProvider` derives archive URLs
  from the configured RSS view IDs and never fetches RSS at runtime. This preserves the existing
  provider/source-key and Granicus clip identity while adding historical recordings and native
  Agenda/Minutes links. Optional explicit archive URLs cover a tenant whose public shape differs.
  The view-cap audit returns no finding for archive-first sources; calendar companions remain separately
  configured, verified sources for agenda-only records and archive-missing clips (R11).
- **Granicus archive pages carry no reliable `ETag`/`Last-Modified`**, so `detect_change` returns
  `None` and the incremental-build content hash in `state.py` performs the actual unchanged-work skip.
- **Swagit serves a "Carmel, IN" placeholder page for unknown subdomains** — a false-positive trap when
  probing/discovering. Cross-check the returned locality against the requested city before trusting it.
- **Swagit `/videos/{id}/download` is broken for older meetings.** The 302 presigns only the bucket
  *prefix* — no object key — so ffmpeg fails (age-correlated across the Dallas archive; the presign is
  freshly minted each call, so it is **not** URL expiry). `resolve_media_url` detects the keyless
  redirect (`_s3_object_key()` empty, issue #120) and falls back to the per-agenda-item `dfile` MP4
  segments scraped from the `/videos/{id}` page JSON (multi-segment meetings go through the #122 concat
  planner). Newer meetings return a proper keyed object and are fine. Details in `swagit.py`'s module
  docstring.
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
  `GuardedHTTPAdapter.send`, ffprobe bitrate/duration probes, and ffmpeg fetch paths. The
  `provider_distributed_leases` layer adds **per-slot compare-and-swap leases** around ffprobe/ffmpeg
  media reads (H17 PR6), capping aggregate Granicus overlap across the four audio shard processes
  (reduced to 2 slots by the GH#300 Phase 1 reduction, then deliberately re-raised to 4 on 2026-06-23
  as a concurrency test after H16 disproved the concurrency-causes-403 hypothesis; the operative caps
  must match the declared `provider_audio_concurrency_ceiling` in `site_config.yml` — update both in
  lockstep, or the H16 `concurrency_ceiling` criterion flags the drift). A domain's N slots are N fixed keys
  `provider-leases/<domain>/slot-<i>.json` (`i` in `0..N-1`), each an independent CAS object; a worker
  claims a free slot with `put_cas(if_none_match="*")` or an expired one (a dead owner) with
  `put_cas(if_match=<etag>)`, walking the slots from a per-owner offset so workers don't all collide on
  slot 0. This replaced the earlier write-a-candidate-then-list-and-sort FIFO emulation, whose every
  poll spent an R2 Class-A *list*; the CAS model never lists, spending Class-A only on a claim,
  renewal, or release (waiting is read-only Class-B). The trade-off is the loss of strict FIFO arrival
  order — the contract is the concurrency *cap*, not fairness — and a soft cap that can briefly admit
  N+1 holders on a reap-vs-release race, which is acceptable for a rate limiter. A background thread
  renews the held slot via CAS; on a renewal conflict the holder has over-run its TTL and another
  worker reclaimed the slot, so it stops renewing. Release is an **atomic CAS handoff** — it
  conditionally writes an immediately-expired "released" tombstone with `if_match=<our ETag>` rather
  than deleting, so a holder that lapsed its TTL (a renewal error or stalled thread, not just an
  observed conflict) can never evict a slot another worker has since reclaimed: the `if_match` simply
  fails and leaves their lease alone. (A read-then-delete would race between the check and the delete;
  there is no conditional-DELETE primitive, so the tombstone reuses the conditional PUT R2 is
  validated to honor. At most N tombstones exist per domain — one per slot, reclaimed on next
  acquire.) Payload metadata
  identifies the GitHub run/job that held a stale claim (logged on reap). The pool needs a CAS-capable
  backend (R2 via `RoutingStorage` routing the `provider-leases/` coordination prefix); on a non-CAS
  backend (b2-only / local dev) the distributed layer disables and only the in-process `HostRateLimiter`
  applies. Keys are registrable domains so the Granicus-owned Swagit CDN (`*.granicus.com`) is matched
  by the host the tenant sees. Both `HostRateLimiter` and `DistributedProviderLeasePool`, plus `SourceCache`'s
  per-uid fetch lock, accept an optional `stop` predicate and raise `StopRequested` if it fires
  before the wait acquires its slot/lease/lock — so a worker idle past the run's wall-clock budget
  yields immediately instead of blocking out a full queue/lease cycle. `CommandFfmpeg` and
  `SourceCache` bind `stop` once at construction; `_encode_one`, `SwagitConcatPlanner`, and
  `SilencePlanner` treat `StopRequested` as a non-failure budget defer (retried next run). The
  ffmpeg subprocess call itself remains out of scope — `stop()` can't preempt a thread parked in
  `subprocess.run`, only `audio_encode_timeout_minutes` bounds that.
- **Granicus transport telemetry is recorded per tenant (no rate-limit circuit breaker).** Native
  archive paths identify their tenant (`archive-video.granicus.com/<tenant>/…`); tenant subdomains and
  the Granicus-owned Swagit media host receive stable tenant keys under the shared `granicus.com`
  domain. For each configured domain (`provider_transport_telemetry_domains`) the Audio lane counts,
  per tenant, direct-fetch success/403, Worker-fallback attempts/successes/failures, and truncations,
  and folds them into the H16 acceptance report's `transport` criterion. This is observational only —
  it never defers or gates media work. The storage-backed *circuit breaker* that used to trip / park /
  canary-recover here was removed in GH#353: it never tripped across Audio runs #51–#56 (the runner
  403s were shared GitHub-egress IP reputation, handled by the Worker, not request-shape or concurrency
  throttling), and aggregate load is already bound by the provider-lease ceiling and per-episode
  materialize backoff. Rollback to direct-only stays config-only (unset the two `GRANICUS_PROXY_*`
  secrets).
- **Planner throttles are the materialization attempt; Audio does not immediately repeat them.**
  `TimelineStage` can fetch provider media through `SilencePlanner`/`SourceCache` before `AudioStage`.
  A typed 403/429 there records one episode materialization attempt/backoff and halts that episode's
  remaining audio-stage chain, so AudioStage does not re-issue the same provider request that run.
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
  Granicus local limiter and distributed lease. Each attempt and its outcome are counted per tenant
  in the transport telemetry (`worker_fallback_attempts`/`successes`/`failures`),
  flowing through the per-run summary and cross-shard report merge so activation is measurable. A
  half-set or invalid `GRANICUS_PROXY_*` configuration disables the fallback for the run (warned once)
  rather than turning an already-handled 403 into a shard-aborting error. The bearer header is never
  logged, the Worker endpoint that ffmpeg echoes in stderr on error is scrubbed before any log line,
  and exceptions expose only the original direct command. Official episode URLs and audio artifact
  identity remain unchanged.
- **Every scheduled Audio run produces one H16 acceptance artifact after the matrix joins.** Each
  shard records per-tenant direct Granicus success/403 and truncation telemetry alongside the
  Worker-fallback counters, scans its own log for credential-shaped material, and uploads
  only the run event plus redacted scan findings. The `validate-h16` job merges all four shards,
  verifies the operative concurrency knobs against the **declared**
  `provider_audio_concurrency_ceiling` (`site_config.yml`; currently 1 local / 4 distributed — the
  config key exists so a deliberate tune passes while accidental drift between the knobs is caught,
  GH#421 run-#58 follow-up PR #449), and writes JSON plus a GitHub summary. The report is
  observational — `validate-h16` never exits non-zero, so an `identity`/`concurrency` failure shows
  as a report `fail`, not a red workflow run.
  The Audio lane also snapshots every Granicus record immediately after provider/persisted-record
  merge, then verifies post-media stable UID/GUID, official/source URLs, source duration, and
  deterministic content-addressed artifact identity — validating key/spec/url only for an artifact
  this run actually re-materialized, so an artifact retained unchanged across a transient (a
  deferred or upload-failed re-encode, or a reused `legacy` object) is not misreported (GH#353).
  Missing shard or Granicus activity yields
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
- **A throttled media fetch records the per-episode backoff at the subprocess boundary.**
  `_raise_if_rate_limited` classifies a throttled ffmpeg/ffprobe exit (HTTP 403/429) and raises
  `RateLimitedMediaFetchError` so the materialization caller records the normal exponential backoff
  (#120) and the episode retries next run. (A Granicus 403 normally never reaches here — the
  direct-first fetch falls back to the Worker first.)
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

`MAX_RESPONSE_BYTES` only bounds fetches that go through `requests` (feeds/JSON/HTML) — ffmpeg reads
media URLs directly via libavformat, bypassing that cap entirely. `citypods.http.preflight_media_size()`
(issue #497) closes that gap with a `HEAD`/ranged-`GET` check against a separate, much larger
`source_media_max_bytes` ceiling before any ffmpeg process starts; a source that honestly discloses an
oversized total raises `MediaSourceTooLargeError` and is never retried unguarded. An unverifiable size is
logged and allowed through — nothing can enforce a cap on bytes ffmpeg itself will fetch regardless.
