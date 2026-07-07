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

### Added

- **`.coderabbit.yaml` settings-as-code, tuned for the free OSS rate limit.** Excludes non-code
  paths from review (per-city/feed data under `config/`, compiled `constraints/*.txt`, all
  `**/*.md`, generated `docs/**`) while keeping every real source dir — including
  `.github/workflows/**` — in scope; skips Renovate-authored PRs (already CI-gated); pauses
  auto-re-review after 2 pushes per PR (`auto_pause_after_reviewed_commits: 2`, needs an explicit
  `@coderabbitai review` comment to resume); and preloads `AGENTS.md`/`ARCHITECTURE.md`/
  `CONTRIBUTING.md` as knowledge-base context plus per-path instructions covering this repo's
  documented invariants (append-only records, split hashes, stage ordering, the wall-clock budget,
  untrusted LLM output, the SSRF gate) so they aren't flagged as bugs. Also turns on
  `auto_apply_labels` (inferred from prior-PR history, not a hardcoded list, so it tracks the
  `type:*`/`area:*` taxonomy without a second place to keep in sync) and an advisory
  (`warning`-mode, non-blocking) custom pre-merge check that flags source-changing PRs missing a
  `CHANGELOG.md`/`ARCHITECTURE.md`/`review/*.md` update per the doc-update contract. AGENTS.md
  gained a "Working with CodeRabbit on a PR" section: agents must triage findings with a
  strong-reasoning model (Opus/GPT-5.5, not the fast default), push back/fix/fix-and-expand per
  comment, resolve CI, and report a summary — now also a `PULL_REQUEST_TEMPLATE.md` checklist item.
- **Austin, TX coverage via Swagit.** Added Austin entity config plus City Council, work session,
  special/budget, Austin Housing Finance Corporation, and active board/commission feeds whose official
  Austin boards list has a matching non-empty Swagit historical subcategory.
- **`citypods compute reclaim-transcript --source-key SK --episode-uid UID [--write]`.** Recovery
  tool for the class of loss #833 fixed: an ASR artifact (VTT + words JSON) already uploaded to
  storage, but the record's `transcript` block never got updated to reference it — the lease
  reaper infers `done` from artifact presence, so nothing else would ever retry it. Recomputes the
  same recipe hash the original transcribing worker used (`_asr_recipe_hash`, deterministic from
  the current city config + episode fields) and re-attaches the existing keys if present — it
  never re-transcribes. Dry-run by default (reports what it found); `--write` pushes the fix
  through the same owned-block-scoped `push_records_merged` path a real worker uses.

### Fixed

- **Cheap timeline-duration fallback no longer files sub-second padding noise, and large fallback
  mismatches can now self-heal through targeted repair.** GH#798/GH#799 were paired
  `timeline-duration-mismatch` / `timeline-short-coverage` issues from the record-only fallback
  path used when no live hosted-audio stream probe is available. That fallback was still using the
  structural 0.1s EDL tolerance, so normal AAC/sample-rounding deltas below the live
  `rendered-duration-mismatch` issue threshold kept opening operational issues. The fallback now
  uses the same `timeline_finding_min_delta` floor (1.0s by default) for feed-health findings, while
  genuinely large stored EDL-vs-served-duration mismatches stamp `timeline-replan`,
  `audio-rematerialize`, and `transcript-regenerate` repair actions when the explicit repair gate is
  enabled. Repairable timeline feed-health issue bodies now link directly to the Feed health audit
  workflow and list the `timeline_repair=true` / `timeline_repair_cohort` inputs to run. Existing
  sub-second rows should clear on the next audit after this ships; truly stale rows remain visible
  and repairable.
- **Work-manifest persistence dropped `duration_hours`, making 100% of the feed-visible
  transcript-asr backlog read as unknown-duration.** `_workitem_to_dict` / `_workitem_from_dict`
  serialized every `WorkItem` field *except* `duration_hours` — a computed ordering input (from
  `audio.duration_served`), not one of the inert reserved fields. `build_manifest` set it correctly
  in memory, but `save_manifest`→`load_manifest` silently reset it to `0.0` on every round trip, so
  every consumer that reads the persisted `state/work.json` (the `long_first` comparator, the
  `asr-worker-report` duration band) saw *unknown duration* for the entire backlog even though the
  records carried a real served duration (confirmed by the pending-unknown diagnostic: 2292/2391
  sampled records had `audio.duration_served` populated while the manifest reported them all as 0h).
  This is what made `long_first` float nothing and kept the duration band pinned at 2393/2393
  unknown across every rebuild. `duration_hours` now round-trips; the manifest self-heals on the
  next `build_manifest`+`save_manifest` (no backfill needed — it is rederived from records each run).
- **Owned-block merge: a better remote plan no longer silently drops an owning lane's just-written
  artifact.** `_preserve_remote_planning_if_better` (part of `merge_preserving_foreign`) overwrote
  *all* artifact blocks — including `transcript` — from remote whenever remote's timeline/source
  planning rank was strictly better than the pushing worker's snapshot, bypassing the
  `protected`/`owned_uids` scoping the rest of the merge respects. When remote had no value for that
  block, it was popped. Surfaced on GH#831's first long ( ~6.6h ) Modal canary: the worker reported
  `completed: 1` and its VTT/words artifact was uploaded, but the record showed `transcript: null` —
  a permanent, invisible loss, because the lease reaper infers *done* from artifact presence while
  nothing reconciles the empty record block. The preservation path now only *replaces* an owned
  block when remote has a truthy (fresher) value for it — the legitimate stale-container-audio →
  remote-decoded-audio case — and never *drops* one the run just produced; non-owned (`protected`)
  blocks and planning fields keep the original replace-or-drop behavior.
- **External worker never persisted its transcript record: an orphaned `return` made the
  `push_records_merged` call dead code.** `_run_transcribe_item` (Modal/Beam pull worker) wrote the
  transcript block into the worker's *local* `state_dir` (`save_records`) and then hit `return
  adopted` — placed directly *above* the `push_records_merged` that durably commits the owned block
  to canonical storage, so the push never executed. The VTT/words artifact still uploaded via
  `put_file`, but the record's `transcript` block only ever lived on the ephemeral worker
  filesystem, discarded when the function exited — so *every* external transcription since the
  regression (PR #824, 2026-07-05) landed its artifact but silently lost its record block, the same
  invisible loss the owned-block-merge fix above guards against but one layer earlier (the guarded
  push was simply never reached). This is why fresh completions kept reading back as un-transcribed
  and needed `reclaim-transcript`. The `return adopted` now runs *after* the push; a regression test
  asserts `push_records_merged` is invoked (with `owned_uids` scoping) on both the fresh-transcription
  and adopted branches. Affected episodes are recoverable via `reclaim-transcript --write` — the
  artifacts were never lost.

### Added

- **`asr-worker-report`'s `--recent N` / `recent` workflow input.** The aggregated worker-telemetry
  counts (success/failed, peak RSS/VRAM) never retained *which* episode a completion was — surfaced
  during live H14b/H14c canary validation ([#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706))
  when a completed run's log gave no way to identify the claimed episode for a post-canary spot-check.
  `report_workers.py --recent N` (or the workflow's `recent` `workflow_dispatch` input) now also lists
  the last N raw telemetry samples — `backend`, `source_key`/`episode_uid`, `outcome`,
  `duration_hours`, `elapsed_seconds`, `finished_at` — reusing fields `_append_telemetry_sample`
  already wrote per-sample; no new storage writes. Defaults to `0` (unchanged report).

- **`asr-worker-report` manifest-freshness and `long_first` backlog-composition diagnostics.**
  Two canary sessions were spent trying to reason about whether `long_first` had "taken effect" by
  cross-referencing GitHub Actions run *start* times against the config merge — which gave a wrong
  answer once (a job starting after the merge can still finish, and rebuild `state/work.json`, well
  after a canary run in between already read the stale pre-merge manifest). The report now reads
  `state/work.json`'s own last-modified time directly from storage (`manifest_last_modified`, an
  exact-key list, not a broad scan) instead of inferring freshness indirectly. It also reports
  `transcript_asr_duration_band` — of the current feed-visible/queued transcript-asr backlog
  (exactly `external_worker.py`'s own candidate filter), how many exceed
  `asr_local_max_duration_hours` (what `long_first` actually floats), how many have unknown
  duration (can never be floated regardless of true length), and the max known duration — so "why
  didn't a canary land on a long meeting" is answered by one report call instead of a live-canary
  guessing game.

### Changed

- **`long_first: 4` enabled in `backlog_priority` — external-required (>4h) transcript work now
  drains first.** Recordings over `asr_local_max_duration_hours` (4h) can only be transcribed by the
  capped external GPU tier (the in-process backend refuses them), so with recency-only ordering a
  steady stream of short episodes could starve them indefinitely. `long_first` floats the >4h
  transcript band ahead of `recency`, catalog-wide. It never reorders `audio`
  (`workqueue.DURATION_AWARE_WORK_CLASSES` excludes it), and the local ASR lane simply defers the
  floated >4h items at preflight (a cheap, pre-download duration check) — so local throughput on
  short meetings is unchanged; only the ordering the external workers see changes. Also lets a
  `max_claims`-elevated canary walk into the long band to validate long-audio + lease renewal.

### Added

- **External-worker adopt/renewal log lines.** The pull worker now prints `[external-worker] adopted
  <source>/<uid>` when it reconciles an already-present artifact instead of transcribing, and
  `[external-worker] lease renewed <source>/<uid> expiry=…` (or `… renew skipped … (no longer held)`)
  each time the renewal thread fires during a long inference. Renewal success was previously silent,
  making it unobservable in a live canary; the interval is now a `_renew_interval()` method so tests
  drive the renewal thread deterministically without a real long transcription. New tests cover the
  renewal-thread wiring and the budget-decline → abandon-to-`queued` path.

### Fixed

- **Pull-worker `max_claims` counts only new transcriptions, not adopted items.** The claim loop
  (`compute/external_worker.py`) incremented `claimed` and checked `max_claims` at lease acquisition —
  before the transcribe path discovers whether the item's ASR artifacts already exist. An
  already-transcribed item that got re-claimed (stale `work.json`, or a prior owner that uploaded then
  crashed before recording) was *adopted* (state reconciled, no GPU work) yet still consumed a
  `max_claims` slot and ended the run, so a manual `max_claims=1` canary would adopt the head-of-queue
  item and stop instead of transcribing a fresh one (surfaced smoke-testing the Modal worker:
  `completed: 1` but `peak_gpu_vram_used_bytes: 0`). `max_claims` now caps new transcriptions:
  adopted items increment a distinct `adopted` summary counter, don't consume a slot, and the loop
  scans past them. A new `max_scan` bound (default `max_claims + 50`; overridable via
  `CITYPODS_WORKER_MAX_SCAN` or the per-backend `site_config` `max_scan`) keeps a fully-stale manifest
  from making one run walk the entire queue. Failed attempts still consume a slot (real work / budget).

- **`S3CompatibleStorage` normalizes a bare-host `endpoint_url`.** First live Beam
  (H14c) scheduled run crashed in `b2_from_env()` — `boto3.client(endpoint_url=...)` raises
  `ValueError: Invalid endpoint` when the URL has no scheme. The Beam secret for `B2_ENDPOINT` had
  been set to the bare host (`s3.us-west-002.backblazeb2.com`), unlike the GitHub Actions secret of
  the same name which includes `https://` — the two secret stores are populated independently, so
  they silently drifted. `_region_from_b2_endpoint()` already tolerated a missing scheme (its
  `split("://")` is a no-op without one), which masked the gap until boto3's stricter endpoint
  validation hit. `S3CompatibleStorage.__init__` now prepends `https://` when `endpoint_url` has no
  `://`, so a bare-host secret in any backend's env store no longer takes down the worker.

- **H11d deploy resilience: `deploy.yml` retries `actions/deploy-pages` on transient GitHub Pages
  backend failures.** Two scheduled/push `Build & Deploy` runs on 2026-07-05 failed at the deploy
  step with GitHub's generic `Deployment failed, try again later.` after an otherwise-clean render
  (this repo's `pages` concurrency group already prevents self-inflicted races, and neither failure
  overlapped another Pages deploy) — a known intermittent backend hiccup in `actions/deploy-pages`
  itself. The deploy step now retries up to 3 attempts total with backoff (15s, then 30s) before
  failing the job, so a single transient GitHub-side error no longer reds out an otherwise-good
  build. See [review/11 H11d](review/11-technical-design-roadmap.md).

### Added

- **Beam worker: resolve pinned deps/model locally instead of referencing build-time repo files (GH#816/#818).**
  Beam's remote image build has no access to local repo files (confirmed against the installed SDK:
  `Image.add_python_packages()` given a file path reads it locally, before anything reaches Beam's
  backend — there is no Modal-style `add_local_dir()` build-time equivalent). `beam_app.py` now reads
  `constraints/asr.txt` and `citypods/asr.py` on the machine invoking `beam deploy` and bakes the
  resolved `package==version` list and HF model repo/revision into the image spec as literal values;
  `add_local_path("citypods/")` is kept for what it actually does — staging the package for the
  deployed function's own runtime import. `scripts/check_dependency_policy.py`'s external-worker
  guard was sharpened to flag only an actual hardcoded version (`pkg==x`/`pkg>=x`), not a bare
  package-name selector key with no adjacent version (the new pattern this fix relies on).

- **Beam external transcription worker pins dependencies + model to the runner (GH#277, part of #804).**
  Same parity as the Modal worker, applied to `scripts/compute/beam_app.py`: the hand-maintained
  package list is replaced with `pip install '.[storage,asr-transcribe]' -c constraints/asr.txt` (same
  pinned versions as the runner, no torch), a digest-pinned CUDA 12 + cuDNN 9 `base_image`, and the
  pinned Whisper model baked into the image via `ASR_MODEL_PATH`. Stacked on GH#276; validated on live
  bounded single-recording Beam test runs.

- **External worker resource telemetry (GH#276/GH#277).** Shared worker code
  (`citypods/compute/worker_telemetry.py`, `external_worker.py`) records per-claim RSS / GPU-VRAM
  peaks with backend, model, compute type, device, GPU type, and outcome, persisted to a single R2-CAS
  object (`state/asr_worker_telemetry.json`) and surfaced in `asr-worker-report`. Applies to both the
  Modal and Beam workers (it lives in the shared `run_worker` path). Telemetry failures never fail
  transcript work.

- **Modal external transcription worker pins dependencies + model to the runner (GH#276, part of #804).**
  `scripts/compute/modal_app.py` replaced its hand-maintained `>=` dependency list with
  `pip install '.[storage,asr-transcribe]' -c constraints/asr.txt` — the exact same versions
  (`faster-whisper`/`ctranslate2`/`av`) the in-Actions transcribe lane uses, resolved from the
  pyproject extras (no duplicate list; enforced by `scripts/check_dependency_policy.py`), and without
  torch (the `asr-transcribe` extra excludes `stable-ts`). The image base moved to a digest-pinned
  **CUDA 12 + cuDNN 9 runtime** (provides ctranslate2's cuBLAS/cuDNN on GPU and is forward-compatible
  with a future torch-based diarize step), and the **pinned Whisper model revision is baked into the
  image** (fast cold start, same bytes as the runner via `ASR_MODEL_PATH`). The canonical model
  repo+revision constants moved to `citypods.asr` so the runner (`prepare_whisper.py`) and the worker
  share one Renovate-tracked source. First deployment — to be validated on bounded single-recording
  test runs. Beam (GH#277) follows the same pattern.

- **Hugging Face Whisper models are pinned to explicit commit revisions (GH#498).**
  `scripts/prepare_whisper.py` downloaded via mutable `main` on both the direct-CDN and
  `snapshot_download` paths, so model bytes could drift silently while `asr_spec_hash()` still
  treated the recipe as unchanged. Both paths now pin `HF_PREFERRED_REVISION` /
  `HF_FALLBACK_REVISION` commit SHAs, logs show `repo@revision`, and the B2 mirror prefix is
  revision-scoped so a future bump lands under a fresh prefix instead of overwriting the old bytes.
  Pinning the current revision is a reproducibility no-op — **no `ASR_PIPELINE_VERSION` bump and no
  transcript reprocessing**; a later intentional revision bump decides invalidation separately
  (review/22). Renovate surfaces upstream revision changes for Dashboard approval.
- **Repository dependency pinning & update policy (GH#498, GH#734).** New normative contract in
  [`review/22`](review/22-dependency-and-reproducibility-policy.md): pins are the default for
  reproducible builds, and Renovate opens PRs so they do not stall past security/beneficial updates.
  Foundation landed: compiled hash-pinned Python `constraints/*.txt` (source of truth for CI, the
  runner image, and the external workers) with a `lock.yml` compile workflow and a `ci.yml` drift
  gate; all third-party GitHub Actions pinned to full commit SHAs and unified to the current tips
  (GH#734); `.github/renovate.json5` with a light-touch two-lane flow (hygiene auto-PRs; a
  Dependency-Dashboard approval gate + per-source `dep-bump-smoke` for output-affecting bumps);
  `scripts/check_dependency_policy.py` CI guard; and the "adding a dependency" contract in
  CONTRIBUTING/AGENTS. Pure pinning is a reproducibility no-op — no pipeline-version bump, no artifact
  reprocessing. HF model-revision pinning (GH#498) and external-worker (Modal/Beam) parity follow as
  their own PRs.
- **Production media fetches now have a size ceiling before ffmpeg reads a remote source
  (issue #497).** `citypods/media.py:_download_audio()` and the direct-remote render paths
  (identity render, multi-source concat fallback) previously handed ffmpeg a remote URL with no
  byte cap at all — `MAX_RESPONSE_BYTES` only ever covered feed/JSON/HTML fetches through
  `requests`, not media bytes ffmpeg reads directly via libavformat. A new
  `citypods.http.preflight_media_size()` issues a `HEAD` (falling back to a ranged `GET` for CDNs
  that reject/ignore it) before any ffmpeg process starts; a source that honestly discloses a size
  over the new `source_media_max_bytes` config ceiling raises `MediaSourceTooLargeError` and is
  never retried by falling back to an unguarded direct stream — it lands in the normal
  materialize-failure/backoff path instead. Unverifiable ("unknown") sizes are logged and allowed
  through, since ffmpeg's own fetch can't be bounded after the fact. `audio_encode_timeout_minutes`
  (the existing per-encode wall-clock cap) is recalibrated from 45 to 360 minutes and
  `source_media_max_bytes` defaults to 54 GB, both derived from a conservative 12-hour
  longest-meeting ceiling (see the comments in `config/site_config.yml` for the full derivation).
  `scripts/probe_granicus_worker.py`'s production-encode check now inherits this same guard instead
  of its own probe-only `--full-download-max-mib` cap. A new `citypods.audit.check_media_too_large`
  feed-health check files one always-visible finding per rejection (never folded into aggregate
  backoff noise), since this should be rare and each occurrence needs a human to verify the meeting
  and decide whether the cap needs raising.
- **Timeline/audio diagnostics probe MP4 headers instead of downloading whole episodes.** When
  `timeline_diagnostics=true`, `check_timeline_integrity` now defaults to a header-only probe that
  range-reads just an episode's `ftyp`/`moov` boxes (`StorageBackend.get_range`, implemented for
  S3-compatible and local storage) instead of downloading the full hosted `.m4a`. Every hosted
  episode is written `-movflags +faststart`, so `format.duration` and the stream's
  `duration_ts`/`time_base` live entirely in `moov` — ffprobe never reads `mdat` for these fields
  either way, so this yields identical values at a fraction of the bytes (verified against a full
  download for both short and multi-hour synthetic fixtures in `tests/test_media.py`). As a
  standing guard against that assumption ever breaking for a real file, any episode the header
  probe flags as non-"ok" is automatically re-measured with a full download
  (`probe_audio_full`); the full read supersedes the header read for the actual finding/repair
  decision, and a new `timeline-audio-probe-divergence` finding fires if the two disagree beyond
  float noise.
- **Feed-health audit returns to the cheap default path while audio queued work gains UID-level
  evidence.** The audit workflow no longer downloads and ffprobes every hosted audio object on every
  scheduled/default run just to emit the timeline canary artifact; full `timeline-audio-integrity`
  diagnostics are now opt-in via `timeline_diagnostics=true` and still forced when
  `timeline_repair=true` needs persisted repair rows. Audio materialization deferrals now log
  `[enrich] audio materialize deferred ... uid=... reason=...` and carry reason counts/samples into
  the run summary, so a lingering `queued` count can be tied back to specific UIDs.
- **Correction: timeline/audio canary repair is now decoded-only and fails closed (GH#702).**
  The canary stamp still forces targeted `timeline-replan` / `audio-rematerialize`, but a healthy
  `status="ok"` now clears or ignores stale repair actions so resolved episodes stop re-keying.
  `SilencePlanner` resolves media through `SourceCache`, runs `detect_silences` on the local cached
  file only, and no longer falls back to container, provider, or stream-sample duration when the
  decoded duration is missing. Cache/decode/degenerate failures defer as typed timeline reasons
  (`deferred_cache_unavailable`, `deferred_decode_unavailable`, `deferred_degenerate_timeline`) with
  timeline-specific materialization backoff; `AudioStage` skips same-run timeline deferrals so stale
  timelines cannot be credited or encoded. This supersedes the earlier fallback-tier language below:
  non-decoded clocks are diagnostic for this planner, not planning authority.
- **Correction: the rendered-duration survivors were still selecting source spans on raw PTS
  (GH#702).** The run 5 → run 6 audit artifacts showed the prior fix only partially converged:
  9/63 original repair-cohort UIDs fixed, 54/63 still `rendered-duration-mismatch`, with nearly every
  survivor showing a changed `timeline_digest` and `audio_key`. That proved the repair lanes were
  firing and the planner had moved from `duration_basis="container"` to `"decoded"`, but the renderer
  was still not using the same clock for selection. Root cause: `_build_streaming_single_source_filter`
  applied `asetpts=N/SR/TB` **after** `aselect`, so the final served output was left-packed but the
  selector still compared compacted EDL boundaries against raw source PTS. A source with a 2s PTS gap
  therefore rendered a 10s EDL as ~8.056s. The streaming filter now rewrites PTS to the contiguous
  decoded-sample clock before boundary framing / `aselect`, and keeps the post-select reset that packs
  retained samples onto served time. A synthetic PTS-gap regression now renders a 10s EDL as 10.0s.
- **Correction: the "decoded audio-stream end" fix below did not converge in production — fixed by
  resetting the decode pass to a sample-index clock before measuring it (GH#702).** A before/after
  production audit of the repair cohort showed 0/56 survivors improved despite genuine re-encodes and
  re-planned EDLs for many of them. Root cause: ffmpeg's `time=` progress field is a
  **presentation-timestamp clock, not a decoded-sample-count clock** — it carries forward any PTS
  discontinuity in the source (a stream splice, an ad-insertion boundary, a dropped HLS segment) as if
  the gap were real elapsed audio, so it overstates by exactly the gap size and lands on the same value
  as the (also PTS-based) container `Duration` header — confirmed bit-identical for the three largest
  survivors, one of which (`media_kind="direct"`) isn't even HLS, ruling out segment loss as the
  mechanism. `detect_silences` now prepends `asetpts=N/SR/TB` ahead of `silencedetect`, so its `time=`
  reading and silence boundaries are measured on a contiguous sample-index clock. A pure per-frame
  timestamp rewrite at the native rate — no resampling, no second decode pass, a no-op on a source with
  no discontinuity. Reproduced directly: a constructed 10s file with a deliberate 2s forward PTS jump
  read `time=12.0x` unfixed (matching its container header) and `time=10.06s` fixed. The follow-up above
  makes the renderer's selector use that same pre-select clock.
- **`SilencePlanner` now anchors the single-file EDL on the *decoded* audio-stream end when no
  stream-sample clock is exposed, closing the GH#702 `rendered-duration-mismatch` survivor gap.** PR
  #704 made the planner prefer ffprobe's stream-sample duration over the container header, but for the
  exact sources that overstate their audio — HLS manifests and fragmented MP4 — ffprobe exposes no
  stream-level `duration_ts`/`time_base`/`duration`, so `_probe_stream_sample_duration` returns `None`
  and the planner fell straight back to the container header. Those episodes therefore re-planned (even
  under a forced `timeline-replan` flag or a version bump) onto the *same* over-claiming EDL — identical
  `timeline_digest`, identical short rendered file — so the repair cohort never converged. The
  `silencedetect` pass already performs a full `-vn` decode, so its final `time=` progress timestamp is
  the real audio-stream end; `detect_silences` now returns that decoded duration alongside the container
  header, and the planner uses it as a `duration_basis="decoded"` tier between `stream-sample` and
  `container`. Re-planned survivors now produce a corrected (shorter) EDL the renderer matches, and the
  audit artifact shows `source_duration_bases=["decoded"]` instead of `["container"]`. No second media
  pass; the container header remains the honest fallback when even the decode end is unparseable.

### Fixed

- **Withheld/dead episodes no longer file noisy `rendered-duration-mismatch` tickets, and get a
  flat ~30-day recheck lifecycle (GH#795).** Once an episode's media is withheld — silent/quarantined
  (`confirmed_empty`) or unreachable (`missing`/`invalid`) — the stale hosted object no longer
  represents anything served, so the timeline-audit now classifies it as terminal `media-withheld`:
  no finding, no repair, no integrity stamp, and it skips both the full-download reconciliation and
  the cheap stored-field duration checks (`check_enclosures` also skips withheld). Separately, a
  **confirmed-dead** episode now polls on a **flat 30-day cadence** (`confirmed_dead_recheck_due`,
  anchored on the availability verdict's `last_check`) instead of the exponential #120 backoff — a
  recheck that stays dead just sleeps another full interval with no new cooldown escalation or ticket;
  `suspected_empty` keeps the exponential ramp so it can reach its second silent confirmation quickly.
  A **repair flag bypasses the exponential #120 backoff** in `TimelineStage` so a flagged
  transient/broken-EDL episode re-plans immediately (those flags clear via the post-repair audit,
  which owns the integrity block). For **confirmed-dead** media the flat gate deliberately takes
  precedence over the flag: the integrity/repair block is audit-owned and the audio lane preserves
  it from remote on push, so a lane-side clear cannot persist — letting the flag bypass the flat gate
  would re-download a quarantined episode every run. Anchoring the flat cadence on the
  audio-lane-owned `media_availability.last_check` keeps it self-managing (recheck ≤ every 30 days)
  without needing to clear the flag.
- **Scoped state pushes no longer regress repaired timeline plans back to stale container-basis
  records.** `push_records_merged` already re-read remote state to preserve sibling artifact blocks,
  but timeline/source planning metadata lived in the unprotected whole-record body. A long-running
  audio or ASR shard that started before a repair could therefore finish later and overwrite a remote
  `duration_basis="decoded"` plan with its older local `container` or missing-source plan, while still
  preserving enough artifact data to make the feed look partially repaired. The merge now ranks
  planning metadata by timeline version and source duration basis, preserves the fresher remote
  planning fields when they are strictly better, and keeps the matching remote artifact blocks so stale
  local artifacts computed against the old EDL cannot be attached to the newer plan.
- **A B2 connectivity blip on one `state/` key no longer fails the whole Build & Deploy run.**
  `pull_state()` restores every object under `state/` from the durable bucket at build start;
  `S3CompatibleStorage.get_file()` already retried transient connection errors in-process, but once
  those retries were exhausted it re-raised, so a single key that kept timing out (Build & Deploy
  runs #452-455 each failed on `boto3.exceptions.RetriesExceededError` inside `download_file`)
  crashed the render-only deploy outright — even though render "must always finish so the deploy
  isn't gated" and the bucket is meant to be a self-healing cache. `pull_state()` now catches the same
  connectivity-level exceptions (`storage.s3.transient_download_errors()`, hoisted out of
  `get_file()` so both call sites share one definition), logs a warning, and keeps whatever local copy
  already exists for that key instead of aborting — the bucket resyncs it on a later run that can
  reach it. A real (non-transient) error, e.g. a 403 from rotated/invalid credentials, still
  propagates and fails the build loudly.

### Changed

- **Stage-2 work-lease reaper enabled now that H14b/H14c are live (GH#706 §4).**
  `config/site_config.yml`'s `work_lease_reaper_enabled` flips from `false` to `true`: the per-item R2
  lease ledger the Modal/Beam pull workers claim against was dormant (and its sweep skipped as
  pointless backlog-scaled GETs) until those workers existed. With H14b (#807) and H14c (#808) merged,
  `compute reconcile` now sweeps it — a crashed worker's claim is reclaimed/requeued instead of the
  ledger going unswept.

- **`audit_feeds.py` consolidates feed-health GitHub issues from one-per-feed to one-per-check.**
  Filing a separate issue for every `(feed, check)` pair meant a single systemic regression (e.g. a
  code bug affecting every feed's timeline check) could open dozens of near-duplicate issues in one
  run. Every check now files a single issue covering all affected feeds — the title shows a live
  `N feed(s) [across M cities]` count, and the body lists every affected feed with how long it's
  been failing (tracked in a hidden JSON state block in the issue body, not an external file) plus a
  representative example. `meetings-url-dead`/`meetings-url-changed` stay one-issue-per-city, since
  each is a genuinely distinct problem needing a specific human to fix a specific city's YAML.
  Issue matching now uses a hidden `<!-- citypods:feed-health:key=... -->` marker in the body
  instead of the title, so the title can change every run without breaking run-to-run
  create/update/close reconciliation. A visible comment posts only when the affected-feed *set*
  changes (a feed newly failing or clearing), not on every cosmetic body refresh (e.g. a "since Nd
  ago" day count ticking over) — the second-order goal being that fixing the "many issues" chattiness
  doesn't just relocate it into "many comments on one issue." Every check's body also gained
  substantially more verbose causes/resolution guidance specific to that check.
- **`work_leases.py` gains a public `scan_offset`/`ordered_candidates` ordering primitive, extracted
  from `run_claim_loop` (review/18 §4).** The H14b Modal pull-worker prototype (unmerged) needed
  budget-gating/lease-renewal/retry `run_claim_loop` doesn't have, so it composed its own loop directly
  on the `claim`/`release`/`renew` primitives — but reimplemented the scan-offset rotation by reaching
  into `work_leases._scan_offset` rather than sharing it. `scan_offset`/`ordered_candidates` are now
  public and generic over candidate shape, so any worker that builds its own loop (instead of calling
  `run_claim_loop`) shares the rotation logic instead of re-deriving it. Docstrings on `run_claim_loop`,
  the module header, and review/18 §4.3/§6 now spell out precisely what `run_claim_loop` is missing
  (external-budget gating, lease renewal, retry) and flag the in-Actions push→pull migration
  (review/18 §6 step 4) as the moment to fold those in as shared hooks instead of writing a third loop.
- **New `long_first: N` backlog-priority comparator prioritizes the catalog's long-meeting transcript
  backlog ahead of everything else (review/12 §H5).** Once an episode exceeds
  `asr_local_max_duration_hours`, the in-process ASR backend refuses it outright
  (`stages._asr_local_duration_eligible`) — the capped external GPU free tier (H13/H14) is its only
  path to ever being transcribed. With no duration awareness, a steady stream of short episodes (which
  have a working local fallback) could keep consuming that scarce dispatch/claim budget in arrival
  order while long ones starved behind them indefinitely. `long_first` is a binary bucket (same shape
  as `recent_first`) scoped to transcript-producing work classes only — `audio` items are never
  reordered. Bare `long_first` (no params) resolves to the site's configured
  `asr_local_max_duration_hours` so the two boundaries can't silently drift apart; an explicit value
  overrides it. Fixed a latent gap found while adding this: `_materialize_set` unconditionally labelled
  every stage's ordering `WorkItem` `work_class="audio"`, which would have silently neutralized any
  work-class-scoped comparator for local ordering and the live H14a push-dispatch order (only the
  separately built `work.json` manifest the H14b/H14c pull workers also read would have seen it);
  `TranscriptStage` / `ProviderTranscriptDiarizeStage` now pass their real work class. Accepted
  tradeoff: a hard catalog-wide drain — one pathological multi-hour backlog can deprioritize all
  short-meeting transcript throughput until it clears; no reserved-capacity split is implemented.
- **`SilencePlanner.version` bumped 2→3 to re-plan every single-file silence EDL on the stream-sample
  clock (GH#702, PR6).** This re-trims the whole single-file silence catalog onto the corrected source
  clock, eliminating the last container-basis EDLs. Because `Timeline.version` is part of
  `timeline_digest`/`audio_spec_hash`, this re-encodes **every** single-file silence episode (and
  regenerates transcripts), not only the gap-affected cohort — a deliberate but large cost. All four
  GH#702 PR6 merge thresholds were confirmed before enabling: PR2–PR5 have run in production; a
  post-GH#795 full-catalog audit shows zero `rendered-duration-mismatch` survivors (stronger evidence
  than the cohort-scoped comparison the gate originally called for); the re-encode cost is accepted;
  and both stragglers (Dallas, Pflugerville) resolved on their own — Dallas now only shows ordinary
  `container-duration-drift`, and Pflugerville's `missing-audio-key` row resolved into the GH#795
  withheld-media lifecycle (`media-withheld`, `confirmed_empty`). See review/20.
- **The rendered-vs-EDL duration audit uses a 0.5s classification floor, separate from the 0.1s
  structural tolerance (GH#702, PR5).** A clean re-encode legitimately differs from the EDL sum by AAC
  priming/padding plus per-cut sample rounding (~0.1–0.4s) with no cue-integrity problem; classifying
  that band as `rendered-duration-mismatch` produced a long tail of sub-finding artifact noise. A new
  `_RENDERED_DURATION_TOLERANCE` (0.5s) cleanly separates padding noise from genuine drift (cohort
  divergences are ≥1s) while leaving the 1.0s finding/repair thresholds and structural checks untouched.
  review/20 gains an operator remediation runbook for the already-broken cohort and the Dallas /
  missing-audio-key stragglers.
- **Single-source many-cut timelines always render via the bounded-memory streaming filter, with an
  OOM guard on the generic fan-out (GH#702, PR4).** `_build_streaming_single_source_filter` is now
  attempted regardless of `audio_processing_profile` (loudnorm is appended to its output on the legacy
  path), so the OOM-prone single-source `atrim`-fan-out in `build_filter_complex` can no longer be
  reached through the empty-profile branch. If such a shape ever does reach the generic graph the render
  raises `StreamingFilterBypassedError` rather than risking the RSS-growth OOM that motivated the
  streaming graph. `build_filter_complex` is retained for its legitimate uses — multi-source concat
  assembly/fallback and intro/outro inserts.
- **`audio_duration_served` is now the probed hosted-stream duration, never the EDL sum (GH#702,
  PR3).** The post-encode/ASR/reuse paths no longer overwrite the measured duration of the actual
  hosted object with the EDL total (`_backfill_served_duration` / `_refresh_served_duration_from_audio`
  are fill-when-missing / probe-first), so a render that disagrees with its EDL stays visible to the
  audit instead of being masked. The RSS `<itunes:duration>` for audio feeds now advertises this
  served duration (a trimmed episode's real played length) instead of the longer source duration. The
  cheap stored-field `timeline-duration-mismatch` / `timeline-short-coverage` checks defer to the
  precise live `rendered-duration-mismatch` probe when one is supplied, so a broken slug is filed once.
- **`SilencePlanner` now plans single-file silence EDLs against the source's audio stream-sample
  clock, not the container `Duration` header (GH#702, PR2).** When a source's container overstates its
  audio (HLS manifests, or a direct MP4 whose video stream outlasts its audio), anchoring the
  trailing-silence test and the final keep-span on the header made the renderer hit EOF early, so the
  rendered file came out shorter than the planned EDL — the single-file `rendered-duration-mismatch`
  class. The planner now ffprobes `duration_ts * time_base` (mirroring `SwagitConcatPlanner`, which
  already used this basis) and records `duration_basis="stream-sample"`, falling back to the container
  header then the provider duration. Re-planned episodes get a corrected EDL that the renderer matches.
- **The EDL (cue) clock now derives from a single `timeline.edl_duration` primitive (GH#702, PR1).**
  `media._served_duration`, `stages._edited_timeline_served_duration`, and `audit._timeline_duration`
  previously each re-summed served segment spans with subtly different fallbacks; they now delegate to
  one canonical accessor so the three duration facts review/20 must keep distinct — source,
  served/hosted, and EDL/cue — cannot drift apart through divergent local math. No behavior change;
  foundation for making the probed hosted-stream duration authoritative for `audio_duration_served` and
  for the single-file silence stream-sample planner fix.
- **Timeline/audio integrity diagnostics and targeted repair plumbing are implemented, with PR6 still
  gated.** The feed-health workflow now uploads an `audit-timeline-integrity.jsonl` artifact that
  distinguishes container-only duration drift from real stream-sample/EDL mismatches. Episode records
  can carry an audit-owned `integrity.timeline_audio` repair block, `/admin/status` reports the repair
  queue, `SourceMedia` records duration basis, and source-aware identity detection now reaches both
  hashing and render-path selection so tail-only trims cannot collapse to identity/copy handling
  (GH#495). `TimelineStage`, `AudioStage`, ASR, and provider-align consume targeted repair actions (`timeline-replan`, `audio-rematerialize`,
  `transcript-regenerate`) without bumping global pipeline versions. Automatic persistence/repair is
  still off in the scheduled audit; `--persist-timeline-integrity` is a manual gate. The feed-health
  audit now queues `timeline-replan` for confirmed stream-vs-EDL mismatches up front, so the next
  repair pass rebuilds the EDL from planner inputs before rematerializing audio/transcripts instead
  of faithfully reproducing the same bad timeline.
  The feed-health
  workflow can now be manually dispatched from `main` to stamp a named repair cohort above a chosen
  stream-delta threshold, while keeping scheduled runs read-only and suppressing sub-threshold
  feed-health noise. A new `scripts/compare_timeline_diagnostics.py` helper compares the cohort's
  before/after artifacts so the operator can verify fixed, still-mismatched, missing-after, worsened,
  audio-key-changed, and timeline-digest-changed counts before widening repair scope. The audit
  artifact now also records planner-facing source telemetry for each row: whether the episode was
  single-file or multi-part, the per-source measured durations and duration bases, the timeline
  version/digest, and the total source-span lengths the EDL mapped. Silence-planned single-file
  episodes now persist that source duration/basis too, so a stubborn mismatch can be traced back to
  whether the planner used a measured container duration or a provider fallback. Backfill story:
  no global invalidation, no `ASR_PIPELINE_VERSION` or `AUDIO_PIPELINE_VERSION` bump, and only records
  explicitly flagged for repair get new audio/transcript recipes. The diagnostics artifact now also
  records `probe_error` reasons such as missing audio keys, storage/download failures, ffprobe
  failures, or absent duration metadata so PR6 can gate on actionable evidence instead of a single
  opaque `duration-probe-inconclusive` bucket. Scheduled feed-health no longer files per-slug issues
  for inconclusive diagnostics, and the workflow now installs `ffmpeg`/`ffprobe` before probing.
- **Timeline/audio integrity repair is now an L3 Phase-H series with a cheap sample-clock duration
  probe.** `review/20` breaks the work into read-only diagnostics, persisted repair flags, planner
  duration-basis fixes, and targeted re-plan/re-materialize/re-transcribe consumers. PR1 adds
  `AudioDurationProbe`, which reads both `format.duration` and the first audio stream's
  `duration_ts * time_base` without decoding the whole file. This does not change audit behavior,
  records, pipeline versions, or artifact invalidation yet.
- **ASR audio-duration refresh preserves edited timeline durations.** The transcript stage no longer
  overwrites `audio_duration_served` on non-identity timelines with ffprobe's container duration, which
  kept resolved `timeline-duration-mismatch` / `timeline-short-coverage` feed-health issues open after
  the audit started reading durable state. Identity/no-timeline audio still uses hosted-file probes for
  ASR budgeting. This is a metadata correction only: no pipeline-version bump, no automatic artifact
  invalidation, and affected records update gradually as audio/ASR touches them again.
- **Multi-source (`SwagitConcatPlanner`) concat episodes now use local-concat source caching
  ([`review/11`](review/11-technical-design-roadmap.md) "Per-segment source caching for
  multi-source concat episodes").** `SourceCache.get_or_fetch_concat` downloads each segment
  individually (own bounded timeout, releases the rate-limit slot between segments) and
  concatenates them once into a cached local file, rendered as a single source instead of
  streaming N remote URLs into one `filter_complex` invocation on every encode attempt.
  `ep.timeline`/`ep.sources` on the persisted record are unchanged — clips/soundbites still
  resolve through the real per-segment EDL; only the render-time encoder input changes.
- **Admin status now exposes provider-transcript rollout health
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR7).**
  `/admin/status` now includes a provider-transcript rollout block with source-document fetch/storage
  counts, `known_good`/candidate/history and rejected-rollback counts, provider-align and
  provider-diarize work-state slices, coarse confidence distributions, diarize error reasons, and
  operator recovery guidance. This is a reporting/UI-only change: it does **not** change transcript,
  provider-align, provider-diarize, or ASR pipeline versions and triggers no artifact backfill.
- **Selected provider-aligned transcripts now produce independent speaker-turn artifacts
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR6).**
  The work manifest emits `provider-transcript-diarize` after a provider-aligned transcript is active.
  `ProviderTranscriptDiarizeStage` conservatively extracts `SPEAKER: text` cues from the served-time
  provider-align VTT into a content-addressed `speakers.json` block and records diarization status on
  the provider registry. If speaker extraction fails or finds no labels, the successful transcript text
  remains active and the episode records only a speakers error/status for retry/operator inspection.
  The new `speakers` block is owned by the `diarize` lane and protected from audio/transcript pushes.
  This adds `PROVIDER_DIARIZE_PIPELINE_VERSION` but does **not** bump `ASR_PIPELINE_VERSION`; no ASR
  artifacts are invalidated or regenerated.
- **Timed provider transcript documents now have a provider-align queue and confidence gate
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR5).**
  Hosted episodes with a synced provider VTT/SRT registry entry now surface as
  `provider-transcript-align` work. `TranscriptStage` parses the provider document in source time,
  remaps cues through the canonical timeline, publishes a served-time `provider-align` VTT when no
  active transcript already owns the episode, and records a `float | null` confidence on the provider
  registry. Changed candidates promote to `known_good` only when their confidence is at least the
  prior known-good artifact; worse candidates move to bounded history and the known-good remains active.
  The provider registry is now a transcript-lane-owned record block so audio-lane pushes preserve
  concurrent confidence/promotion updates. This adds `PROVIDER_ALIGN_PIPELINE_VERSION` but does **not**
  bump `ASR_PIPELINE_VERSION`; no ASR artifacts are invalidated or regenerated.
- **ASR transcript keys now use a timeline/recipe transcript media hash instead of the audio-byte
  recipe hash ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR4).**
  ASR VTT and word JSON keys are now based on source media identity plus the served timeline and ASR
  recipe, so codec, loudness, chapter, or audio-processing recipe changes no longer mark completed ASR
  transcripts stale. Current-version ASR records with old audio-spec-derived keys are migrated by copying
  the existing VTT and word sidecar to the new key shape when the old objects are present; missing/corrupt
  artifacts are reported and only those episodes fall through to regeneration. The run summary/history now
  reports ASR migration counts (`copied`, `already_present`, `missing`, `regenerated`). This does **not**
  bump `ASR_PIPELINE_VERSION`; the expected ASR regeneration count is zero except for genuinely missing
  old artifacts.
- **Provider transcript source documents now surface separately from the active podcast transcript
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR3).**
  A synced `provider_transcript.known_good` document can fill `<podcast:transcript>` only while no
  ASR/provider-aligned active transcript exists; once `transcript_hosted_url` is synced, that served-time
  artifact owns the Podcasting 2.0 tag. The known-good provider document remains exposed in feed notes and
  city pages as **Original city-provided transcript**. This is a render-only exposure change: transcript
  pipeline versions, artifact keys, and stored bytes are unchanged, so **no ASR backfill or regeneration**
  is triggered.
- **Provider transcript source documents are now fetched into the H15 provider registry
  ([GH#453](https://github.com/BashfulBits/city-meeting-podcasts/issues/453), PT-PR2).**
  `TranscriptStage` keeps the current provider transcript URL in `links["transcript"]` and stores
  each non-empty provider document under a content-addressed `provider-` transcript key in
  `provider_transcript.candidate`. Re-fetching identical bytes refreshes `checked_at`; changed bytes
  become the new candidate and the superseded candidate is retained in bounded history for
  later rollback. Candidates are **not** promoted to `known_good` and do not replace the active
  podcast transcript until the follow-up provider-alignment/scoring path proves them at least as good.
  No ASR or transcript pipeline version changes, so **no ASR backfill or regeneration** is triggered.
- **The distributed provider concurrency-slot pool moved to per-slot R2 compare-and-swap (H17 PR6,
  the final H17 PR; [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390)).**
  `DistributedProviderLeasePool` (the cross-process Granicus/ffmpeg throttle that caps aggregate
  overlap across the four audio shards) no longer emulates an N-slot FIFO semaphore by writing a
  per-waiter candidate object and **listing + sorting** the prefix every poll. It now models a
  domain's N slots as N fixed CAS objects `provider-leases/<domain>/slot-<i>.json` (`i` in `0..N-1`),
  each with an independent ETag: a worker reads a slot (cheap Class-B) and claims a free one with
  `put_cas(if_none_match="*")` or an expired one (dead owner) with `put_cas(if_match=<etag>)`, walking
  the slots from a per-owner offset. Because the old per-poll *list* was itself an R2 Class-A op, a
  blocked waiter used to burn Class-A continuously; the CAS model **never lists** and spends Class-A
  only on a claim, renewal, or release (waiting is read-only). `provider-leases/` is added to
  `COORDINATION_PREFIXES` so the slots route to R2 and are excluded from the bulk B2 state sync.
  Behavioral changes: waiters no longer acquire in strict FIFO arrival order (the contract is the
  concurrency *cap*, not fairness), and the soft cap can briefly admit N+1 holders on a
  reap-vs-release race — both acceptable for a rate limiter. The pool now requires a **CAS-capable**
  backend; on a non-CAS backend (b2-only / local dev) the distributed layer disables and only the
  in-process `HostRateLimiter` applies (production runs on `audio_storage_backend: routing` → R2). The
  live validation harness (PR5) gains a provider-slot check (acquire two of two slots, third caller
  blocked, release frees) under a `provider-leases/__validate__-…` scratch namespace. Slot payloads,
  TTL/renew cadence, telemetry, and `stop`-budget abort are unchanged; no audio bytes, pipeline
  versions, or artifacts change — **no backfill**.
- **`compute reconcile`'s Stage-2 work-lease sweep is now gated behind `work_lease_reaper_enabled`
  (default `false`).** Flipping `audio_storage_backend: routing` activated the reaper on the CAS
  path, but the per-item lease ledger external pull workers claim against is **dormant** until those
  workers (H14b/H14c) exist — so the sweep would GET one R2 lease key per pending `transcript-asr`
  item only to find every one absent (cheap Class-B, but pointless and backlog-scaled). The sweep is
  lossless to skip while dormant (nothing to settle/requeue), so it stays off until a deployment sets
  the flag once external workers are live. `reconcile_compute(..., sweep_work_leases=False)` and the
  matching `compute reconcile --dry-run` preview are both gated.
- **The Granicus rate-limit circuit breaker (plus its queue parking and half-open canary recovery)
  was removed ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)).** It was
  built for a hypothesis H16 disproved — the Actions-runner 403s were shared GitHub-egress IP
  reputation (handled by the authenticated Cloudflare Worker), not request-shape or concurrency
  throttling — and it never tripped across Audio runs #51–#56 (zero trips/deferrals/recovery probes).
  `citypods/provider_circuits.py` is replaced by a lean `citypods/provider_transport.py`
  (`ProviderTransportTelemetry`) that keeps only the per-tenant direct/Worker-fallback/truncation
  counters feeding the H16 `transport` criterion; the storage-backed open/trip/defer state, the
  `_run_enrich_global_queue` parking/canary loop (and its latent double-retry race), the
  `CircuitOpenMediaFetchError` admission gate, and the `circuit_skipped`/`circuit_keys` stage plumbing
  are gone. Telemetry domains are configured via `provider_transport_telemetry_domains` (replacing
  `provider_rate_limit_circuit_breakers`); the H16 report schema bumps to v2 (per-tenant rows drop the
  circuit columns). Aggregate provider load stays bound by the distributed provider-lease ceiling and
  the per-episode materialize backoff, and rollback to direct-only fetch remains config-only (unset
  `GRANICUS_PROXY_BASE_URL` / `GRANICUS_PROXY_TOKEN`). Audio bytes, pipeline versions, and stored
  artifacts are unchanged, so **no backfill**.
- **Duplicate combined/per-board audio views now share one encode and one CAS object
  ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421)).** Some per-board
  feeds use a wider `feed_urls` set than their city's combined feed, so stripping only the `body`
  filter produces distinct `source_key`s for the same stable meeting. Audio shard planning now
  keeps all source keys for one configured city entity on the same shard, and a thread-safe
  run-local `(provider, stable uid, audio recipe)` cache lets the first successful alias supply its
  artifact pointer to every follower. New duplicate work chooses one deterministic source prefix;
  existing valid artifacts can be adopted as the shared winner, and superseded duplicate objects
  become ordinary orphan-GC candidates once no record references them. Source keys, episode UIDs,
  audio recipes, and pipeline versions are unchanged, so this causes **no catalog backfill or
  re-encode storm**.
- **The per-episode ASR timeout now carries a configurable safety margin
  (`asr_timeout_safety_margin`, default `1.2`).** ASR run #32 timed out and discarded a 3.4h
  recording that was actively transcribing, not hung — a sibling episode from the same run
  finished at ratio=0.503 against a budget computed assuming ratio 0.5, leaving only ~3% of
  margin. The base+per-audio-hour budget is now multiplied by this margin (values <1.0 are
  ignored) before being clamped to the existing hard backstop deadline, so routine variance no
  longer kills genuinely-progressing inference. The hard backstop and timeout-backoff behavior are
  unchanged.
- **Audio shard assignment is duration-weighted and availability-aware.** Source-atomic Audio
  planning now sums the expected served duration of pending encodes whose media is available,
  recovered, or not yet classified, using the current Timeline first, then the last served duration,
  then provider duration; unknown durations use their source's known-duration average. Media already
  classified as withheld contributes only a small recovery-recheck cost because TimelineStage still
  probes it but AudioStage will not encode it. This replaces flat pending-episode counts, preventing
  short/empty-media backlogs from monopolizing a shard while sibling shards carry hours of playable
  audio. This changes scheduling only: audio recipes, pipeline versions, stored artifacts, and
  backfill behavior are unchanged.

### Added

- **Provider transcript retention schema added for the H15 rollout.** Episode records can now carry a
  separate `provider_transcript` registry (`known_good`, `candidate`, and `history`) for city-supplied
  transcript documents while the existing `transcript` block remains the active podcast transcript. The
  schema stores URL/B2-key/content-hash/format/basis/confidence metadata for later provider-transcript
  fetch, alignment, diarization, and rollback work; referenced provider transcript objects are included
  in the GC live set. This is schema-only and does not change transcript recipes or invalidate existing
  ASR artifacts, so **no ASR backfill or regeneration** is triggered.
- **A scheduled Audio orphan-GC workflow reports reclaimable storage and only deletes on demand
  ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421) follow-up).** Until now
  `scripts/gc_audio.py` was operator-run only, so superseded content-addressed objects (regenerated
  artifacts, retired recipes, and the now-coalesced duplicate source views) accumulated until
  someone swept by hand. A new `audio-gc.yml` workflow runs weekly as a **dry-run**: it restores the
  bucket state, finds objects no record references, and — when any exist — opens/updates one rolling
  *operations* issue with a per-city summary table (file count + total size per city, plus a grand
  total) and attaches the full object list (`orphans.tsv`) as a run artifact. It never deletes on a
  schedule; reclaiming is a manual **Run workflow** with `apply = true`. `gc_audio.py` gains
  `--pull-state` (restore the durable state so the live set is current before sweeping), `--out`
  (write the tsv/json/markdown report), and per-city attribution via `source_key → city` entity;
  storage backends gain `iter_objects` (a size-bearing listing, free from S3/B2/R2 pagination and the
  local stat). The GC live set, `--min-age-days` floor, and `state/` exclusion are unchanged.
- **Weekly empty-recording review digest emits bounded audio evidence
  ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353), H16 PR3b).** A new
  `availability-digest.yml` workflow (`scripts/availability_digest.py`) scans the persisted
  media-availability verdicts for meetings classified suspected/confirmed empty, deterministically
  samples a small set of *new or changed* candidates (keyed by uid + source fingerprint + detector
  version, so a re-classification re-surfaces), and for each renders an evidence record (durations,
  sizes, hashes, silence intervals, profile/detector version, canonical watch-page URL, and a
  **redacted** source identity) plus two low-bitrate mono proxies — the untrimmed source audio and
  the silence-trimmed candidate. It zips the bundle as a workflow artifact and opens/updates a
  single rolling digest issue **only when** new/changed candidates exist; an already-reviewed
  candidate is recorded in a `state/availability_digest.json` ledger so it is not re-digested. The
  issue body and evidence never carry a signed/credential-bearing URL.
- **Durable media-availability classification withholds empty/missing recordings from feeds
  ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353), H16 PR3a).** A
  meeting whose source media is missing or (near-)totally silent now carries an explicit, versioned
  `media_availability` verdict on its record (`available` / `suspected_empty` / `confirmed_empty` /
  `missing` / `invalid` / `recovered`, plus operator overrides) instead of being re-attempted every
  run with no durable outcome. The verdict rides the audio lane's existing silence-detection decode
  (no extra ffmpeg pass): a successful decode that is near-totally silent is *suspected* and, after
  a second independent successful silent fetch, *confirmed*; a transport failure (403/429/timeout/
  truncation) can never confirm silence or flip a known-good episode. Withheld verdicts are kept out
  of both audio and video feeds and out of `AudioStage`, so a bad/empty enclosure is never published
  and a confirmed-unavailable meeting keeps its prior known-good artifact — while metadata stages
  (chapters/links) keep running so agenda/minutes still reach the meeting page. Classification is
  re-evaluable via a dedicated detector version, a query-stripped source fingerprint, the detection
  profile, and operator overrides, and recovers automatically when the city later supplies playable
  media — none of which bumps the audio pipeline version or backfills the catalog. Per-run
  availability counts flow through each shard run event into the H16 acceptance report as
  informational observability (not a transport pass/fail criterion).
- **H16 Audio acceptance now proves Granicus record and artifact identity and generically redacts
  subprocess diagnostics ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)).**
  The audio lane snapshots each Granicus meeting after provider/persisted-record merge and verifies
  after media processing that stable UID, provider GUID, official/source URLs, canonical video URL,
  and source duration did not drift. Reused current-spec artifacts must retain key, public URL, and
  served duration; newly materialized or refreshed artifacts must match the deterministic
  content-addressed spec/key/public URL and report a positive served duration. Aggregate checked,
  artifact-checked, mismatch, and bounded category counts flow through each shard run event into
  the existing H16 report. ffmpeg/ffprobe stderr, timeout/error payloads, and exception command
  arguments now strip all media URL queries and redact bearer or credential-shaped values while
  preserving host/path/status diagnostics. This is transport/observability only: no audio bytes,
  pipeline versions, artifact recipes, or backfill behavior change.
- **Audio runs now publish a machine-readable GH#353/H16 acceptance report after all four shards
  finish.** Each shard uploads only its run event plus redacted secret-scan metadata—never the raw
  log—to a post-matrix `validate-h16` job. The merged JSON artifact and GitHub step-summary table
  classify transport recovery per Granicus tenant, including direct successes/403s, Worker
  successes/failures, circuit activity, truncations, lease behavior, and the unchanged 1-local /
  2-distributed ceiling. Identity stability consumes the record/artifact invariant checks described
  above; a run without applicable identity activity is `insufficient_activity`, not a false pass.
  Credential-shaped query strings, bearer values, and Worker endpoint paths are detected locally
  on each shard and represented only by redacted category/file/line metadata. This adds telemetry
  and workflow evidence only: no audio bytes, pipeline versions, artifact identities, or backfill
  behavior change ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)).
- **A live B2+R2 validation harness to verify the R2/CAS control plane before production cutover
  (H17 PR5, [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390)).**
  `scripts/validate_control_plane.py` + the `Validate R2/CAS control plane` workflow
  (`workflow_dispatch` + weekly schedule) exercise the *real* plumbing end-to-end against live
  services — `RoutingStorage` routing/`cas_capable`, native R2 compare-and-swap
  (`put_cas`/`get_bytes`: create-if-absent, conditional update, stale-ETag rejection), and the
  Stage-2 work-lease ledger (`claim`/contended-skip/`renew`/`release`/`reap`) — and emit a per-check
  JSON report (+ R2 Class-A/B op telemetry). **It never touches production data:** every object is
  written under a unique scratch namespace (`work-leases/__validate__/<run-id>/…`) and deleted on
  exit; the real budget/lease keys are never read or written, and the discovery index never
  references `__validate__`. The workflow header documents the recommended pre-cutover sequence (set
  secrets → run → confirm all checks pass → only then flip `audio_storage_backend: routing`). The
  validation logic is unit-tested offline against an in-memory CAS fake.
- **Stage-2 pull-based work-lease ledger — the frozen contract distributed ASR workers claim against
  (H17 PR4, [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390); review/18 §4).**
  New `citypods/ops/work_leases.py` adds per-item compare-and-swap lease objects on R2
  (`work-leases/<source_key>/<uid>.json`) so heterogeneous workers (in-Actions shards today; external
  Modal/Beam/Mac-mini workers next) can **competitively claim** transcribe work from a shared ledger
  instead of being handed a static `--shard K/N` slice. Per-item objects have independent ETags, so
  concurrent claims of different uids never contend (the CAS-retry-storm mitigation, review/17 §6).
  The module implements the full claim protocol — `claim`/`renew`/`release`/`reap` plus the
  `run_claim_loop` orchestrator (read discovery index → CAS-claim → injected `transcribe` → durable
  artifact/record commit → settle), with the neural inference left as the injected seam H14b/H14c
  fill. `compute reconcile` now also reaps the ledger (expired claim → requeue; artifact present →
  done), derived from the discovery index. **Cost discipline (review/18 §4.6)** keeps it at ≈1 R2
  Class-A op per *claimed* item: never list the lease prefix (derive keys from the B2 index),
  read-before-claim + per-worker scan offset (no failed-claim writes; workers target different items
  first), infer completion from the artifact (no `done` write), and a generous TTL (renew is the
  exception). `work-leases/` routes to R2 via `COORDINATION_PREFIXES`. **In-Actions matrix shards keep
  using the Stage-1 static plan** (review/18 §6) — this PR freezes the contract and lands the
  substrate so external workers build against it from day one; it changes no scheduled production
  behavior. No pipeline-version bump, no backfill.
- **The free-tier GPU budget ledger moved to R2 with an atomic compare-and-swap decrement (H17 PR3,
  [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390); review/17 §3/§5).**
  `state/compute_budget.json` is the first coordination artifact to migrate off the bulk B2 state
  sync onto the R2 CAS path: `RoutingStorage` now routes it to R2 (`COORDINATION_PREFIXES`), and
  every `reserve`/`settle`/`release` is a compare-and-swap read-modify-write (`budget.mutate_budget`:
  GET ETag → apply → `put_cas(if_match=…)` → re-read and retry with bounded backoff + jitter on a
  412). Concurrent shards can no longer lose each other's reservations or overspend the monthly
  free-tier cap: the reservation is an **atomic check-and-reserve** (`reserve_if_available`) that
  re-evaluates availability against the freshest ledger on every CAS retry, taken **before** the
  irreversible remote submit (released if the submit fails) — so two shards selecting the same
  backend from a stale snapshot can't both commit. `statesync` excludes CAS-managed keys from
  `pull_state`/`push_state` (so a
  plain `put_file` can't clobber the CAS object), gated on a new `cas_capable` flag set **only for
  R2** (B2 silently ignores conditional headers); a plain-B2 / local / dry-run backend keeps the
  prior local-file ledger behavior byte-for-byte. External dispatch is still dormant (no adapter
  registered), so this is a no-op in production today — it proves the router + CAS helper on the
  lowest-stakes coordination key before the throttle-path migration. No pipeline-version bump, no
  backfill (a pre-existing B2 `compute_budget.json`, if any, is simply superseded by the R2 ledger).
- **Per-episode transcribe sharding so one skewed source spreads across all shards (H17 Stage 1,
  [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390); review/18 §3).** The
  transcribe lane now plans per `(source, uid)` episode instead of per source: a Granicus source with
  thousands of pending episodes no longer pins to a single shard while its siblings idle. `ShardPlan`
  gains a `unit` field (`source` for the source-atomic audio/align lanes, `episode` for transcribe)
  and `SHARD_PLAN_VERSION` bumps to `2` (reconcile emits a fresh plan every run, so there is no
  durable v1 artifact to migrate — **no backfill**). New `records.pending_transcribe_items` emits the
  per-episode backlog from the same classifier as the aggregate `estimate_transcribe_shard_work`, so
  per-uid weights sum to the source's shard weight. `sources_for_shard` becomes `episodes_for_shard`,
  returning both the owned sources and a per-source owned-uid set. **The load-bearing safety change:**
  `records.merge_preserving_foreign` gains an `owned_uids` axis — a per-episode-sharded shard writes a
  `transcript` block only for the uids it owns and preserves the freshest remote for siblings' uids,
  closing the cross-*uid* lost update two shards splitting one source would otherwise hit (the
  reviewer's race). `owned_uids=None` reproduces the prior source-atomic behavior byte-for-byte, so
  audio/align and the unsharded full enrich are unchanged. Audio stays source-atomic (its bottleneck
  is the per-source provider rate limit, not the runner — review/18 §2.3). No pipeline-version bump.
- **Storage substrate for the R2/CAS control plane (H17, [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390)).**
  `S3CompatibleStorage` gains compare-and-swap primitives — `put_cas()` (native boto3
  `IfNoneMatch`/`IfMatch`, raising `CASConflict` on a 412) and its `get_bytes()` read companion — the
  storage-level gain R2 has over B2 (review/17 §1.3/§5; confirmed by the §7 spike on boto3 1.43). A new
  `RoutingStorage` backend (`audio_storage_backend: routing`) implements the `StorageBackend` Protocol
  and dispatches by key prefix to a B2 *primary* and an R2 *coordination* backend, degrading to
  B2-only when R2 creds are absent, and tallies R2 Class-A/Class-B op counts for free-tier telemetry
  (review/17 §4). **Routing is a deliberate no-op in this change** (`COORDINATION_PREFIXES` is empty),
  so no artifact moves to R2 yet and production behavior is unchanged; later H17 work appends prefixes
  as each coordination artifact migrates. No pipeline-version bump or backfill: nothing about audio or
  transcript identity changes.
- **A mid-run kill of the enrich phase (SIGTERM, GitHub cancel, lost-comms) now shuts down
  gracefully instead of silently losing every record update for the run.** Previously the global
  enrich queue persisted each source only once, after *both* the audio and transcript passes
  finished, and nothing intercepted SIGTERM — a kill mid-queue dropped all in-memory record updates
  since the last (end-of-run) persist and left no trailing `run_history.jsonl` entry. The CLI entry
  now installs a SIGTERM handler (`install_signal_handlers`) that latches a process-wide interrupt
  the existing `StopSignal` predicate ORs in, so in-flight workers start deferring immediately and
  the run flows through its normal persist + run-history + state-push path on the way out. The
  global queue also persists every source as soon as the **audio pass** drains — before the
  decoupled transcript pass even starts — and again at the end, shrinking the unpersisted window for
  *every* run, not just killed ones; the repeat persist is idempotent (append-only `merge_records`,
  and `persist_source` no longer mutates the caller's notes list so the "{n} archived" note can't
  double-append). An interrupted run is tagged `interrupted: true` / `outcome: "interrupted"` in
  `run_history.jsonl` + `run_summary.json` and the `enrich`/`build` CLI exits `143` (128+SIGTERM) so
  `continue-on-error` and log readers don't mistake a cut-short run for a clean success — a normal
  wall-clock/supersession yield is **not** an interrupt and still exits `0` (GH#377,
  [#386](https://github.com/BashfulBits/city-meeting-podcasts/pull/386)).
- **Native Granicus audio can fall back once to authenticated Cloudflare egress after a direct
  GitHub-runner HTTP 403.** The retry applies only to strict canonical
  `archive-video.granicus.com/<tenant>/<tenant>_*.mp4` inputs and remains inside the existing local
  limiter, distributed lease, and circuit admission. Worker success prevents the direct 403 from
  tripping the circuit; Worker throttling is counted once before lease release. Audio workflow
  secrets are passed to both container and host-fallback runtimes, while bearer headers are redacted
  from logs and exception commands and the Worker endpoint ffmpeg echoes on error is scrubbed from
  logs. A half-configured `GRANICUS_PROXY_*` pair disables the fallback (warned once) instead of
  aborting the shard, and each attempt/outcome is counted per Granicus tenant on the circuit
  (`worker_fallback_attempts`/`successes`/`failures`) — surfaced in the build log and run summary —
  so the three post-activation runs required by GH#337 can be judged from telemetry rather than log
  archaeology. The isolated probe now classifies authenticated HTTP 200 responses that ignore Range
  as `range_unsupported` access successes and can run one full Arlington/Pflugerville source through
  the production source-cache and `podcast-speech-v2` recipe. This changes transport only: no
  official metadata, audio recipe, pipeline version, artifact key, existing object, or backfill
  behavior changes.
- **An authenticated Cloudflare Worker probe can test alternate egress for Granicus archive media.**
  The GitHub-hosted transport artifact returned 403 for all 12 direct curl/ffmpeg/header cases while
  the same exact objects all succeeded from a Mac, including one full download and local media
  validation. `workers/granicus-media-proxy` therefore provides a deliberately narrow streaming
  experiment: fixed Granicus archive origin, bearer authentication, committed tenant allowlist,
  tenant-prefixed MP4 validation, no queries/redirects/cache, selected Range validators only, and no
  response buffering. The manual Audio-isolated Granicus workflow adds a `worker` mode that compares
  direct versus Worker-routed curl and ffmpeg on one GitHub runner, then performs at most one
  size-capped full-download/local-processing proof. Setup and teardown are documented in the Worker
  README. A path-filtered deployment workflow tests and redeploys the
  Worker automatically when its source or Wrangler configuration changes on `main`, using a scoped
  Cloudflare deployment token while leaving the runtime bearer secret Cloudflare-managed. No audio
  recipe, pipeline version, artifact identity, or stored artifact changes.
- **The isolated Granicus probe can now distinguish HTTP transport behavior from runner/CDN
  throttling.** Manual `granicus-probe.yml` defaults to a low-volume transport mode that pairs curl
  and the production-pinned ffmpeg against the same exact Audio #40 Arlington, Pflugerville, and Fort
  Worth archive objects plus an Audio #33 control, alternating which client goes first. It records
  selected redacted response status/range/timing metadata, tests browser-context curl requests, and
  performs at most one size-capped full curl download by default before validating it with local
  ffprobe and a local 30-second ffmpeg stream-copy. Curl is restricted to the already-resolved archive
  object without automatic redirects; the existing request-shape matrix retains the separately
  guarded `DownloadFile.php` test. The sustained request-count/volume/cooldown matrix also remains
  selectable. Both modes retain Audio-queue isolation and bounded transfers; this is diagnostic only
  and does not change production media fetching, audio identity, pipeline versions, or stored
  artifacts.
- **Local ASR now has a configurable duration admission guard for runner memory safety.**
  `asr_local_max_duration_hours` defaults to 4 hours in production and applies only to synchronous
  faster-whisper/stable-ts execution. `compute_backend: auto` still attempts external dispatch first;
  when dispatch declines—or under `compute_backend: local`—a known oversized recording is deferred with
  `reason=external-required` before semaphore acquisition/download, or after the hosted-audio probe if
  duration was initially unknown. It is not marked failed and remains eligible for later external
  dispatch. Non-positive values disable the guard. The existing rolling 100-sample runtime estimator,
  timeout formula, 285-minute start cutoff, and 350-minute backstop are unchanged. No ASR pipeline
  version or artifact identity changed, so stored transcripts are not invalidated and no backfill is
  triggered.
- **Audio runners now use a prebuilt, version-pinned GHCR runtime with a verified static fallback.**
  `.github/workflows/audio-runner-image.yml` builds and smoke-tests the linux/amd64 runtime weekly and
  whenever its definition changes. The image pins the official Python base by digest and installs an
  immutable FFmpeg 7.1.4 archive only after SHA-256 verification. `audio.yml` pulls that image with a
  five-minute bound and runs the current checkout inside it; if GHCR is unavailable, the shard restores
  or downloads the same checksum-pinned ffmpeg/ffprobe bundle and runs on the host. This removes
  `apt-get update/install` and its unbounded Ubuntu-mirror failure mode from all Audio shards. ASR
  shards reuse the same verified static ffmpeg cache directly on the host, while Whisper model weights
  remain in their existing Actions-cache/Hugging Face/B2 cascade rather than inflating the runtime
  image. No pipeline version or artifact identity changes, so there is no audio or transcript backfill.
- **H14a — external-dispatch substrate + free-tier budget ledger, wired into the live ASR flow
  ([#275](https://github.com/BashfulBits/city-meeting-podcasts/issues/275)).** The dispatch half of the
  H13 compute seam now routes the transcribe/align path. New `citypods/compute/budget.py`
  (statesync-backed `state/compute_budget.json`) enforces each backend's `monthly_gpu_seconds` /
  `max_inflight` as a **hard cap — the $0 guarantee** (decrement-on-dispatch, settle actuals on done,
  reap on expiry, monthly reset). New `citypods/compute/dispatch.py` adds the router (fill free tiers,
  then **overflow to `local`**), a thread-safe `DispatchCoordinator` that records a live `work.json`
  lease (`lease_owner="modal:<job_id>"` — the first competitive use of the H5 lease API) and decrements
  budget, and `reconcile_compute` (reap a dead worker's expired lease → re-queue; settle completed
  jobs). `compute_backend: auto` (now the default) routes inference through the coordinator; with no
  external adapter registered yet (Modal/Beam land in H14b/H14c) every job **overflows to `local`** —
  behavior-identical to before. A new `citypods compute reconcile` CLI runs at `asr.yml` start (a
  dedicated job the sharded `asr` job `needs`), and a `FakeDispatchBackend` exercises the whole path in
  `tests/test_compute_dispatch.py`.

### Added
- **A stall-diagnostics progress registry surfaces which episode/source/phase a stuck enrich
  thread is on, with a thread-stack dump as a backstop.** `audio.yml` runs had intermittently shown
  a shard stuck for the whole run with no further log output, and the existing heartbeat only
  printed CPU/memory snapshots — useless for telling a stuck shard apart from a slow-but-healthy
  one. New `citypods/progress.py` (`PROGRESS`, a thread-safe per-thread-ident registry) is updated
  by `AudioStage`'s encode worker and `TimelineStage`'s planner loop on entry/exit; the heartbeat
  now prints the longest-running active operations every tick (`[enrich] active work: ...`) and, if
  the oldest tracked operation has made no progress for `CITYPODS_STALL_DUMP_SECONDS` (default
  600s, 0 disables), dumps every thread's stack via `faulthandler.dump_traceback` (cooled down to
  once per 30 minutes so a genuine stall doesn't flood the log).
- **The host rate limiter, distributed provider lease pool, and per-run source cache now stop
  waiting once the run's wall-clock budget expires, instead of blocking out a full queue/lease
  cycle.** These three coordination waits were previously unbounded by `stop()` — a worker idle
  past the run's deadline still queued behind whichever thread held the slot/lease/lock, sometimes
  for minutes, before the caller could even check the budget. `HostRateLimiter`/`_Slots`
  (`citypods/http.py`) and `DistributedProviderLeasePool`/`_acquire` (`citypods/provider_leases.py`)
  now accept an optional `stop` predicate and raise a new `StopRequested` if it fires before the
  wait acquires; `SourceCache.get_or_fetch` (`citypods/media.py`) does the same for its per-uid
  lock. `CommandFfmpeg` and `SourceCache` bind `stop` once at construction (`citypods/run.py`)
  rather than threading it through the `FfmpegRunner` Protocol, so existing test doubles are
  unaffected. `StopRequested` is handled as a graceful defer (no backoff recorded) in
  `_encode_one`, `SwagitConcatPlanner.plan()`, and `SilencePlanner.plan()` — the same treatment
  already given to `CircuitOpenMediaFetchError` — since running out of time isn't a source/provider
  failure and shouldn't count against an episode's retry backoff. The actual ffmpeg subprocess call
  remains intentionally out of scope: `stop()` still can't preempt a thread parked in
  `subprocess.run`, only `audio_encode_timeout_minutes` bounds that.
- **The heartbeat now surfaces live `NativeWorkGate` occupancy and provider-lease queue depth each
  tick, not just cumulative end-of-run totals.** `total_wait_seconds` and `telemetry()` could only
  show *how much* waiting had happened over the whole run, not *whether* the current tick was
  blocked — useless for telling "the gate is fully booked right now" apart from "nothing has
  contended in a while." `NativeWorkGate.current_counts()` (`citypods/resources.py`) and
  `DistributedProviderLeasePool.current_waiting_counts()` (`citypods/provider_leases.py`, a new
  live per-domain gauge incremented/decremented around `_acquire`'s wait loop) expose the live
  state; `_ResourceHeartbeat` (`citypods/run.py`) prints a `[enrich] gate: ...` line and one
  `[enrich] leases: <domain> ...` line per tick, suppressed entirely when idle to avoid log noise
  on quiet ticks (GH#376). Observability-only — no change to gate/lease admission logic.

### Fixed
- **The audio orphan GC now allow-lists managed artifacts, so an `--apply` run can no longer delete
  the ASR model mirror or other bucket infrastructure
  ([#448](https://github.com/BashfulBits/city-meeting-podcasts/issues/448) investigation).** The
  unscoped sweep (`--prefix ""`) only protected the `state/` prefix, so the dry-run report flagged
  `models/faster-whisper-large-v3-turbo/*` — including the 1.6 GB `model.bin` written by
  `scripts/prepare_whisper.py` and depended on by the ASR workers — as reclaimable orphans; an apply
  run would have broken transcription. `scripts/gc_audio.py` now treats a key as a deletion candidate
  only when it is a managed artifact (`is_managed_artifact`: content-addressed audio `*.m4a`, or a
  `transcripts/…` object); everything else (`state/`, `models/`, `clips/`, or any future infra prefix)
  is allow-listed out and counted as "protected" in the run summary. This is an allow-list of artifact
  shapes rather than a deny-list of known infra, so a newly introduced infrastructure prefix can never
  be reaped by an older copy of the script. Report/scan logic only — no audio bytes or stored artifacts
  change.
- **The H16 identity check no longer false-fails on coalesced duplicate source views, and a coalesced
  follower keeps a valid served duration ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421)
  follow-up).** Audio run #58 — the first with the GH#421 duplicate-coalescing active — failed the
  `identity` criterion on 20 Fort Worth episodes. Coalescing makes a combined feed's record adopt the
  per-board feed's *canonical* shared object (same `uid` + spec, different source prefix). The
  `_artifact_matches_recipe` exemption already tolerated that for `audio_key`/`audio_url`, but
  `current_artifact_changed` still fired on the accompanying served-duration delta. That category now
  fires only when the artifact no longer resolves to a valid content-addressed object for the recipe —
  a re-probe or coalesced-sibling adoption is metadata-only, not a changed artifact. Separately, a
  *credited* canonical winner can carry no probed duration; `_apply_artifact` (`citypods/media.py`) no
  longer downgrades a follower to `0s` by adopting that — it keeps the shared duration when present and
  otherwise backfills from the episode's own timeline/source (which fixes the 5 episodes that also
  tripped `served_duration`). Diagnostics + an in-place metadata fix only — no audio bytes, pipeline
  versions, or stored artifacts change, so **no backfill**.
- **The H16 `concurrency_ceiling` criterion's expected ceiling is now configurable.** It was hard-coded
  to the GH#300 `1`-local / `2`-distributed envelope, so deliberately tuning Granicus concurrency (e.g.
  bumping `provider_distributed_leases.granicus.com.slots`) made the acceptance report `fail` for an
  unrelated reason. A new `provider_audio_concurrency_ceiling` config key (default `1`/`2`) declares the
  intended ceiling; the criterion asserts the operative `provider_rate_limits` + distributed `slots`
  match it, still catching an accidental drift between the two operative knobs. Update the declared
  ceiling in lockstep when tuning.
- **A failed audio upload no longer leaves the record partially mutated, and the H16 identity check
  no longer reports a false mismatch for any artifact retained across a transient failure
  ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)).** Audio runs #54 and
  #56 failed the `identity` criterion with a single `1/~939` mismatch (`audio_key` + `audio_spec_hash`
  + `audio_url`, with neither `served_duration` nor `current_artifact_changed` firing). Root cause
  (proven from run #56's per-shard log): an episode's recipe changed during the run and its re-encode
  probed a new served duration, then the **upload failed transiently** (B2 `ServiceUnavailable`).
  `materialize_audio` (`citypods/media.py`) had already written `audio_duration_served` *before* the
  `put_file`, so the failed upload left the record carrying the new artifact's duration while still
  pointing at the prior, valid artifact (old spec). `H16IdentityTracker.verify`
  (`citypods/h16_identity.py`) saw the duration change, entered the artifact-comparison branch, and
  flagged the legitimately-retained old key/spec/url against the freshly-recomputed `_expected()`
  spec. Two fixes: (1) the encode now commits `audio_duration_served` **atomically with the artifact
  pointer, only after a successful upload**, so a failed upload leaves the episode untouched and
  simply retries next run; (2) `verify` no longer compares key/spec/url when the artifact identity is
  **unchanged from capture** (no successful re-materialization this run) — a divergence from the
  recompute is then a pending re-encode, not corruption — which also covers budget-deferred re-encodes
  and reused migrated `legacy` artifacts (generalizing the earlier `legacy_ok` exemption). A freshly
  *written* artifact is still validated, so genuine content-addressing drift is still caught. The
  earlier same-issue entry attributing this to legacy reuse was incorrect — the mismatching artifact
  carried a real content-addressed spec, not `"legacy"`, and the lease `stale_leases_reaped`
  correlation was common-cause (infra-troubled runs), not causal. No audio bytes, pipeline versions,
  or stored artifacts change, so **no backfill**.
- **Swagit concat probes no longer deadlock the global Granicus media pool.** The concat duration
  probe now acquires the process-local host limiter before the cross-shard distributed lease,
  matching every other ffmpeg/ffprobe media path and the #342 lock-order invariant. The reversed
  order could let concat probes hold both distributed slots while waiting for a local slot held by
  source-cache work that was itself waiting for a distributed slot; Audio #51 exposed the cycle by
  renewing both leases for the full run without launching ffprobe. This changes coordination only:
  audio bytes, artifact identity, and pipeline versions are unchanged, so **no backfill** is
  triggered.
- **Duplicate source views of one stable meeting no longer run the same ASR recipe twice.** The
  per-episode planner now groups matching `(stable uid, ASR recipe)` work, co-locates every
  source-local alias on one shard, and charges the inference weight once. `TranscriptStage` uses a
  thread-safe run-local result cache with per-key in-flight reservations, so concurrent aliases fan
  one completed VTT/word-JSON result out to their existing source-scoped object keys instead of both
  entering native inference — even if multiple ASR worker permits are configured. Fresh-ASR recipes
  now include the stable `author + body + title` prompt plus language, compute type, and beam size;
  different inference inputs remain independent. Existing current-version transcripts are still
  accepted before recipe recomputation, and `ASR_PIPELINE_VERSION` is unchanged, so already-stored
  artifacts are **left as-is** and no catalog backfill is queued. Pending items use the complete recipe.
  The ASR workflow also suppresses the known upstream Node `Buffer()` deprecation only for the pinned
  `actions/download-artifact` step; application warnings remain visible.
- **A busy ASR shard transcribing a multi-hour recording no longer looks idle/stalled, errored
  audio no longer wastes scarce ASR slots, and one unprobed source no longer skews shard weighting
  into the thousands of hours.** Investigating a run where three shards appeared to have "no work"
  while one ground on surfaced four issues — all observability/efficiency, no recipe or
  pipeline-version change:
  - **ASR inference is now registered in the progress registry.** `TranscriptStage` runs native
    inference in a killable child process while the parent thread only polls; it never wrapped that
    wait in `PROGRESS.track`, so the heartbeat printed `active work: no tracked work active` for the
    entire (sometimes 90-minute) transcription — a healthy run indistinguishable from a hung one,
    the exact failure the registry exists to prevent. The poll loop now registers an `asr-<mode>`
    entry (`citypods/stages.py`), so the heartbeat shows the in-flight episode/elapsed and the
    stall-dump backstop can actually fire.
  - **The heartbeat now surfaces `ResourceAdmission` waiters.** Worker threads parked on the
    `load>N`/`mem_avail<N` guard block *before* reaching the `NativeWorkGate`, so its `asr_waiting`
    stayed `0` even with real work queued behind a running ASR job — reading as "no demand."
    `ResourceAdmission.current_waiting_counts()` (`citypods/resources.py`) exposes the live per-kind
    queue and `_ResourceHeartbeat` (`citypods/run.py`) prints a `resource guard: waiting ...` line.
  - **Episodes with a materialization error are no longer queued for ASR.** Audio that failed to
    materialize (e.g. bytes uploaded but no probeable duration) still passed the transcribe
    audio-readiness gate, wasting a serial ASR slot on broken audio and inflating the backlog/shard
    weight (~600 such episodes in the investigated run). `TranscriptStage` skips them
    (`reason=audio-error`), `estimate_transcribe_shard_work` excludes them, and `materialize_audio`
    no longer reuses/credits an errored record so the audio lane re-encodes it (clearing the error +
    recording a duration on success, with the existing exponential backoff guarding genuinely-broken
    sources).
  - **Unknown-duration items are weighted by their source's own average, not a flat 2h ceiling.**
    `estimate_transcribe_shard_work` previously added a 2-hour fallback per unprobed episode, so one
    source with thousands of them estimated at ~3,550h and pinned its whole (source-atomic,
    unsplittable) backlog to a single shard. Unknown items now take the average known local duration
    in the same source (`citypods/records.py`); the constant fallback applies only when the source
    has no known duration to average against.
- **Fresh transcription no longer installs the alignment and benchmark dependency stacks.**
  Optional dependencies are split into `asr-transcribe` (faster-whisper), `asr-align` (stable-ts
  with its faster-whisper adapter), and `asr-bench` (both plus jiwer), while the existing `asr`
  aggregate remains backward-compatible. Scheduled `asr.yml` installs only
  `asr-transcribe,storage`; the manual benchmark installs `asr-bench`; the future align-only lane
  is explicitly assigned `asr-align,storage`. This reduces install time/disk and prevents
  transcribe runners from importing the unused torch/stable-ts alignment stack.
- **Logical-run telemetry no longer presents a partial shard matrix as the latest completed run,
  and ASR deferrals now expose structured reasons.** Scoped events receive a stable logical-run id
  from GitHub run + phase + lane; KPI selection retains the previous complete run until every
  expected shard reports, while a first-ever partial run remains explicitly marked incomplete.
  `StageStats.defer_reasons` now records stable ASR reason tokens through run history, cross-shard
  aggregation, status JSON, and the `/admin/status` stage table instead of collapsing every queued
  item into one opaque Deferred count.
- **Local ASR timeouts now terminate native inference and back off only the offending episode.**
  Production local execution moved into a persistent spawned subprocess that keeps model caches warm
  across episodes but can be terminated/restarted when faster-whisper or stable-ts exceeds the
  per-item deadline. The prior daemon-thread fallback could not stop CTranslate2 work, abandoned the
  runner slot, and skipped all remaining ASR for the run. Timeout attempt count and timestamp now
  persist in the transcript record with exponential 1–30 day backoff; successful reuse or inference
  resets it, shard weighting treats active timeout backoff as blocked work, and other episodes
  continue after a killed worker. No transcript recipe or pipeline-version change.
- **ASR shard ownership now comes from one canonical pre-matrix snapshot, eliminating divergent
  assignments and four redundant full B2 restores per workflow.** The reconcile job restores durable
  state once, reconciles leases/budget, writes a versioned `ShardPlan`, and uploads both state and plan
  as one run-scoped artifact. All four ASR matrix jobs validate and consume that exact assignment,
  fail closed on lane/shard/source drift, and skip `pull_state`; they no longer independently restore
  the full `state/` prefix or calculate weights while sibling state changes. The local CLI path keeps
  deterministic in-process planning when no artifact is supplied.
- **The scheduled transcribe lane no longer defers episodes merely because they have untimed
  provider text while forced alignment is disabled.** `TranscriptStage` previously applied the
  `alignment-disabled` guard before `--lane transcribe` discarded the alignment hint, so exactly
  the caption/minutes-bearing episodes that the fresh-ASR lane was intended to cover stayed queued.
  Lane routing now happens first: `transcribe` always selects fresh faster-whisper, while the
  unscheduled `align` lane and combined auto mode retain the alignment-disabled defer behavior.
- **`SilencePlanner`'s `ffmpeg silencedetect` pass no longer oversubscribes the CPU alongside
  `AudioStage`'s encodes.** `detect_silences()` shelled out to ffmpeg with no `-threads` cap and
  wasn't gated by `NativeWorkGate`, so `TimelineStage`'s per-episode planner threads (parallelized up
  to `ctx.max_encodes_per_source`) could each spawn an unbounded, all-cores ffmpeg `silencedetect`
  process — running concurrently with, or ahead of, the gated audio encodes the gate exists to budget.
  `detect_silences()` now accepts a `threads: int | None` param applied as `-threads N` the same way
  `CommandFfmpeg` pins its encode passes, and `SilencePlanner.plan()` acquires/releases
  `ctx.native_work_gate` (`kind="audio"`) around the call using the same `ffmpeg_threads` value
  `CommandFfmpeg` is configured with — so a silencedetect pass competes for the same admission slots
  as `AudioStage`'s encodes instead of running outside the budget. A denied/stopped admission defers
  the planner pass (`ep.timeline` stays unstamped) rather than running ungated or raising.
- **The silence-trim and Swagit-concat planners no longer produce or silently swallow degenerate
  results.** `SilencePlanner` could stamp a near-empty served timeline (observed: 0.005s/0.010s
  outputs) when `detect_silences` misread a throttled/truncated source as almost entirely silent;
  `build_silence_timeline`'s result is now checked against `is_degenerate_served_duration` (new
  `silence_min_served_seconds`/`silence_min_served_fraction` `StageContext`/site-config knobs,
  defaults 5.0s / 2%) — a degenerate result preserves the prior valid timeline if one exists,
  otherwise falls back to the untrimmed identity timeline instead of hosting near-silence.
  `SilencePlanner.version` bumped 1→2 to re-examine episodes that may already carry a degenerate
  stamped timeline from before this guard existed (a one-time, wall-clock-bounded re-trim).
  Separately, `SwagitConcatPlanner` collapsed page-fetch and per-segment duration-probe failures
  into one bare `return None` with no record of which sub-operation failed; both paths now call
  `record_materialize_failure` with a distinct code (`concat-fetch`, `concat-probe:s<i>`) so
  retries/backoff and diagnostics target the actual failure instead of a generic deferral.
- **ASR shard assignment is now weighted by routing-aware transcription cost, not the audio lane's
  pending-encode backlog.** `run.py` fed `asr.yml`'s `--shard K/4` partition the same
  `pending_audio_work` signal as `audio.yml`; in steady state (Audio runs more often than ASR) that
  backlog sits near zero for nearly every source, which silently collapsed ASR shard assignment to
  alphabetical round-robin — blind to how much transcription work a source actually had outstanding,
  so one shard could own a multi-hour local backlog while a sibling finished and sat idle. New
  `estimate_transcribe_shard_work` / `TranscribeShardWork` (`citypods/records.py`) mirror the
  `lane="transcribe"` reuse check and separate duration-weighted local inference from cheap external
  dispatch, blocked/deferred inspection, and already-in-flight work. Current production has no real
  external adapter, so locally eligible recordings are weighted by duration, known recordings above
  the 4-hour local ceiling contribute only a minimal blocked cost, and unknown durations receive a
  conservative local estimate. H14's canonical planner can inject one route classification computed
  from restored state and a single GPU budget/capacity snapshot; matrix shards will consume that
  immutable decision instead of independently guessing dynamic external availability. `run.py`
  selects the estimate only for `lane == "transcribe"`, leaving Audio weighting unchanged. No
  audio/transcript recipe, pipeline version, stored artifact, or backfill behavior changes.
- **Granicus throttle circuits are now shared across Audio shards and isolated by tenant
  ([#337](https://github.com/BashfulBits/city-meeting-podcasts/issues/337)).** The previous breaker
  counted failures independently in each shard and opened one registrable-domain circuit, so the same
  three provider failures could be repeated by every shard and a Fort Worth throttle could defer
  healthy Denton or Granicus-owned Swagit work. Circuit failure/open/probe state now uses deterministic
  ordinary storage objects protected by a separate one-slot FIFO lease. Native archive paths and
  tenant subdomains receive stable tenant scopes; three throttles open that tenant only, while two
  distinct tenant trips inside the cooldown window trigger a domain emergency. Exactly one shard owns
  a half-open canary, siblings observe its recovery, and abandoned probes are reclaimable after a
  bounded TTL. The global queue parks and releases work by tenant/domain scope. Existing Granicus caps
  remain 1 process-local / 2 distributed, and no audio recipe, pipeline version, stored artifact, or
  backfill changes.
- **Granicus throttle failures no longer cause an immediate duplicate request or force every
  remaining meeting out of the run ([#337](https://github.com/BashfulBits/city-meeting-podcasts/issues/337)).**
  A 403/429 raised while `TimelineStage`/`SilencePlanner` fills the per-run source cache now records
  exactly one persisted materialization attempt and halts that episode before `AudioStage` can fetch
  the same URL again. Circuit-open meetings remain deferred without backoff, but the global queue now
  parks them while other-provider work drains, waits only while the normal stop budget permits, and
  runs one half-open canary after cooldown. A canary throttle immediately reopens the circuit; a
  completed materialization records recovery and releases the parked work through the unchanged
  Granicus caps. Run telemetry adds recovery-probe/recovery counts. A new manual, isolated
  `granicus-probe.yml` measures repeated request count, progressive bounded transfer volume, cooldown,
  exact Audio #37 Fort Worth failures, and concurrency-last behavior with redacted JSON artifacts.
  No audio recipe/pipeline version changed and no stored artifact is invalidated.
- **Stale-lease and release/renewal logs now name the GitHub run, job, matrix shard, and lease
  state ([#345](https://github.com/BashfulBits/city-meeting-podcasts/issues/345)).** GH#336 already
  stored `github_run_id`/`github_run_attempt`/`github_job` in renewable lease payloads, but
  stale-reap logs only ever surfaced the internal `hostname:pid:uuid` owner token — Audio #33 reaped
  two stale Granicus candidates and an operator could not tell which prior run or shard had held
  either one. Lease payloads now also carry the writer's `K/N` matrix shard label (threaded through
  `DistributedProviderLeasePool.configure(shard=...)` from `build()`'s existing shard tuple), and
  stale-reap/release/renewal-failure log lines append a concise `owner=… run_id=… job=… shard=…
  state=…` suffix built only from the fields present in the payload. Legacy or unreadable payloads
  still reap safely via the object-modification-time fallback, with no metadata suffix. No secrets
  are stored; payload-read caching is unchanged.
- **The global audio queue now drains promptly after a graceful `stop()`
  ([#344](https://github.com/BashfulBits/city-meeting-podcasts/issues/344)).** The H5 PR3 global queue
  dispatches `AudioStage` once per *episode* (for true newest-everywhere-first ordering across
  sources), so `materialize_audio()`'s `_hosted_keys()` re-listed the same source's storage prefix once
  per episode instead of once per source — Audio #32 shard 0 spent ~25 minutes draining queued-but-cheap
  items after its last in-flight encode finished. A new `HostedKeysCache` (wired through
  `StageContext.hosted_keys_cache`) shares one `list_objects` listing per source across every
  `AudioStage` call for that source during a build, so listings scale with the number of sources, not
  episodes. Cheap reuse/credit bookkeeping is unchanged and still runs regardless of `stop()`; only the
  redundant listing is eliminated.
- **The ffmpeg/ffprobe rate-limit circuit now opens before the failed attempt's provider lease is
  released ([#343](https://github.com/BashfulBits/city-meeting-podcasts/issues/343)).** GH#336 added
  post-lease circuit admission, but the circuit was only recorded/opened by the higher-level
  materialization caller, after the subprocess boundary had already released its distributed and
  process-local provider slots on the way out. A queued waiter could acquire that just-released lease,
  pass the still-closed circuit check, and start one extra ffmpeg process per threshold crossing —
  Audio #33 shard 3 showed four direct Granicus 403s against a configured threshold of three, twice.
  `_raise_if_rate_limited` now records the failure (and atomically opens the circuit, when the
  threshold is crossed) from inside the same `with` block that holds both provider slots, for both the
  `subprocess.run` and monitored/`Popen` ffmpeg paths; `RateLimitedMediaFetchError` carries
  `circuit_recorded`/`opened_domain` so the materialization caller skips re-recording (no
  double-counting) while still logging the open transition and applying episode backoff. Circuit-open
  logging remains once per transition; rate-limit/circuit-deferred telemetry is unchanged.
- **Process-local workers can no longer hoard distributed provider slots
  ([#342](https://github.com/BashfulBits/city-meeting-podcasts/issues/342)).** The ffmpeg/ffprobe
  guard now acquires the process-local `HostRateLimiter` slot *before* joining the distributed
  provider-lease election (previously the other way around), for both monitored and unmonitored
  ffmpeg paths and the ffprobe probe. A process with a local cap of one can therefore hold at most
  one distributed slot for a domain at a time, instead of letting several of its own threads win
  every distributed candidate while they wait behind the local cap — which had let one early-starting
  shard occupy both Granicus slots in Audio runs #32/#33 while other shards were still starting up.
  Existing aggregate slot limits and post-slot circuit admission are unaffected.
- **Distributed provider leases now renew safely, reap dead owners promptly, and stop queued media
  work after a provider circuit opens ([#336](https://github.com/BashfulBits/city-meeting-podcasts/issues/336)).**
  Waiting and acquired lease candidates refresh their explicit expiry while alive; winner election
  uses immutable FIFO candidate-key order, so renewal cannot demote an active holder behind waiters.
  Lease payloads include GitHub run/job metadata, storage reads use payload expiry when available
  (object modification time remains the compatibility fallback), and acquisition failures clean up
  their candidate. The ffmpeg/ffprobe boundary now rechecks the run-local circuit only after both
  distributed and process-local provider slots are held, preventing already-queued workers from
  starting after another worker trips the circuit. Circuit opening is atomic per cooldown instead of
  being logged once per concurrent failure. Run telemetry now records lease acquisitions, total/max
  wait, renewals, stale reaps, direct throttles, trips, and circuit deferrals per media domain. No
  pipeline version changed and no stored artifact is invalidated; deferred work retries naturally.
- **Peak-constrained recordings no longer fail bounded linear loudness normalization.** Some
  low-average/high-transient Granicus recordings required enough constant gain to predict +7–8 dBTP,
  which correctly prevented FFmpeg's linear `loudnorm` from silently reverting to dynamic mode but
  dropped the episode. The normal measured-linear path is unchanged. When peak headroom is
  mathematically insufficient, pass 2 now applies the same constant integrated-loudness gain followed
  by FFmpeg's short-lookahead `alimiter` at 192 kHz, then returns to 48 kHz for AAC. The limiter uses a
  -2.5 dB ceiling to leave AAC reconstruction headroom; memory remains duration-independent because
  only resampler and millisecond lookahead buffers are retained. Extremely high-crest-factor material
  may land slightly below -16 LUFS rather than clip or disappear. Existing records carrying the old
  `loudness` error code bypass their stale exponential backoff once and retry immediately; new genuine
  measurement failures use `loudness_measurement`. This fallback changes only items that previously
  failed, but it ships inside the `podcast-speech-v2` recipe described below.
- **Long-meeting podcast mastering and edited timelines are now bounded-memory.** Production audio
  uses the versioned `podcast-speech-v2` profile:
  `80 Hz high-pass → dynaudnorm → gentle compressor → final measured linear EBU R128 loudnorm
  (-16 LUFS, -1.5 dBTP)`. Monotonic single-source silence timelines now use one streaming selector
  instead of parallel `atrim` branches; it switches to one-sample frames only near each boundary,
  selects by integer 48 kHz sample PTS, and coalesces normal frames afterward. This keeps RSS bounded
  without the cumulative frame-boundary duration drift of plain `aselect`. Pass 1 applies the timeline
  and speech leveling while streaming once from the provider into a temporary mono FLAC and measuring
  that exact signal with `ebur128`; pass 2 reads the local FLAC, applies measured **linear** loudnorm
  or the bounded peak-limiter fallback, and encodes 96 kb/s AAC. Native admission is phase-specific:
  source caching happens before a CPU slot is held, and finalization has a small dedicated executor
  while sharing the same total FFmpeg cap. The path reserves a fixed 768 MiB independent of duration,
  retains the 1.5 GiB mid-flight safety floor, and rejects sub-second edited timelines as unusable
  audio. `audio_processing_profile` participates in `audio_spec_hash`; changing v1 → v2 gradually
  invalidates and remasters already-hosted v1 artifacts through the normal wall-clock queue. No
  separate pipeline-version constant changed.
- **ASR shard provider-fetch outages now fall back to the persisted archive.** The `transcribe` and
  `align` lanes are best-effort transcript backfill over already-hosted audio in `episodes.json`, so a
  transient source refresh failure now loads the last-known record archive and continues ASR instead of
  returning a run error. If no archive exists, the source is skipped/deferred for that ASR run. Audio and
  full enrich lanes still surface provider fetch failures as errors. No pipeline version changed and no
  artifact backfill is triggered.
- **ASR shards now run every 5h with a 285m start cutoff, 350m backstop, and rolling runtime
  estimates.** The `transcribe`/`align` lanes stop starting new local ASR after
  `asr_start_cutoff_minutes` (285m), but an already-started transcript may continue until
  `asr_backstop_minutes` (350m), even if the next scheduled ASR run is queued. `TranscriptStage` keeps
  a fixed-size `state/asr_runtime_log.json` buffer of the previous 100 successful ASR runtime /
  recording-duration samples, falling back to the conservative timeout formula until real samples exist,
  and starts a recording only when `recording_duration × average_ratio` fits before the start cutoff. The
  ASR semaphore remains as the single-transcript gate; waiters poll `stop()` / abort so a timed-out
  native ASR call cannot pin sibling workers, and the shared runtime log is merge-pushed so ASR shards do
  not overwrite each other's samples. No pipeline version changed and no artifact backfill is triggered.
- **`/admin/status` "Last Run" block now reports the Build & Deploy action, not the latest enrich run.**
  Run history (`run_summary.json`) is recorded only by the time-bounded enrich (audio/ASR) workflows, so
  the at-a-glance "Last Run" card was surfacing the newest audio/ASR lane — duplicating the adjacent
  Audio/Transcribe/Diarize run cards and never reflecting the deploy that actually rendered the page.
  `build_status` now reads the GitHub env it runs under (it executes *inside* `deploy.yml`) into a new
  `kpis.last_deploy` block (`status`, `workflow`, `github_run_id`, `github_run_url`, `ts`); the status
  page renders that block, so the card shows the Build & Deploy workflow, the render timestamp, and a
  link to that Actions run. Off-CI it degrades to a link-less `local` status. No pipeline version
  changed and no artifact backfill is triggered.
- **Cross-lane record clobber in the sharded enrich workers (the `hosted_audio −16` regression).** The
  `audio` and `asr` workflows shard over the same `source_key` partition but run on different schedules,
  so both write the *same* `state/sources/<key>/episodes.json` at overlapping read→write windows. Each
  run pulled state once at start, held it for its whole multi-hour run, then pushed back the **whole**
  record file — so an ASR run that started before an audio run hosted new audio re-uploaded its
  start-of-run `audio` block on finish, silently erasing the freshly hosted URLs (and, symmetrically, a
  late audio run could erase transcripts). The scoped push prevented cross-*shard* clobber but not this
  cross-*lane* lost update. Now a scoped run owns only its lane's artifact block
  (`records.protected_blocks_for_lane`: `audio` vs `transcript`) and, on push, re-reads the freshest
  remote per owned source and preserves the block it doesn't own (`records.merge_preserving_foreign`,
  `statesync.push_records_merged`/`fetch_remote_records`); a present-but-unreadable remote skips that
  source's push rather than clobber. `stages.LANE_STAGES` (one source of truth, enforced in `run_stages`
  and the global queue) keeps each lane to its own work-class stages so it never re-derives a foreign
  block. Because the status KPIs read straight from the record store, this also stops the periodic
  `/admin/status` `hosted_audio`/`transcripts_synced` numbers from bouncing backwards after an ASR-only
  update. The block/lane registries are designed to extend to the near-term `diarize` lane (review/12
  §H5/§H6).
- **Admin status now reports latest telemetry per pipeline stage.** `/admin/status` keeps the existing
  newest-lane `stage_totals` for compatibility, but now also exposes `backlog.stage_runs` keyed by
  stage (`audio`, `transcript`, and future stages such as `diarize`). Each entry points at the latest
  completed logical run that actually reported that stage, with sibling shard totals aggregated, so a
  later ASR-only run no longer makes the audio row look like "not run." Scoped `run_events/` remain
  upload-only and are not deleted by later lane pushes. At-a-glance now includes Audio / Transcribe /
  Diarize run-status cards next to Last Run. The Hosted Audio card also shows feed-visible audio
  coverage, not-hosted count, and stale re-encode count, while the Transcripts card surfaces the
  text-only/provider bucket that sits between synced transcripts and missing ones. Linked Video counts
  are now config-aware: direct-provider records are counted as linked only when `extract_audio` is
  false; with the production default `extract_audio: true`, unhosted direct records are audio backlog.
  The run-status cards now render warning-level alerts in yellow separately from run-level errors in
  red. Workflow cache steps were updated to Node 24-compatible `actions/cache@v5` / restore `@v5`,
  and the temporary `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24` runner override was removed now that GitHub
  runners default to Node 24. No pipeline version changed and no artifact backfill is triggered.
- **Admin status actuals now use source records and the work manifest instead of overlapping feed
  rows.** `/admin/status` headline totals for archived meetings, hosted audio, linked video, storage,
  stale items, and issue counts are now aggregated once per canonical `source_key`, so combined feeds
  and per-board feeds no longer hide or double-count source records. The backlog block derives a fresh
  work manifest from records and overlays live `work.json` sidecar state such as shard/dispatch leases,
  while shard `run_events/` continue to drive last-run telemetry.
- **Audio #11 source-cache no longer forces non-AAC audio into an M4A container.** The per-run source
  cache now remuxes provider audio into a local Matroska audio copy (`.mka`) instead of writing
  `*.m4a` with the iPod muxer during download. Final materialization still writes podcast M4A, but the
  identity path now probes both codec and bitrate: only under-cap AAC is stream-copied, while MP2/MP3/
  PCM/other source codecs are transcoded to AAC. No pipeline version was bumped: already-hosted audio
  is left as-is, and deferred/failed source-cache items retry naturally through the normal audio lane.
- **Audio #10 encode-failure follow-up: Granicus storms are now cross-shard capped and classified.**
  `provider_rate_limits.granicus.com: 2` is still the per-process cap, but audio has four shard jobs;
  the new `provider_distributed_leases` layer uses B2-compatible soft lease candidate objects so
  shards share one aggregate Granicus limit for ffprobe/ffmpeg media reads. Live probes on 2026-06-15
  showed 1–8 concurrent short Granicus ffprobe/ffmpeg reads succeed from this client, while Audio run
  #10 failed under sustained 8-way Actions overlap, so production starts at 6 aggregate Granicus slots
  rather than dropping to a conservative 3–4. ffmpeg/ffprobe 403/429 stderr is now classified as
  `rate_limited`, source-cache throttles no longer immediately fall through into a second direct render
  attempt, and a run-local circuit breaker pauses new Granicus media work after repeated throttles.
- **B2-compatible provider leases.** The first cross-shard lease implementation used S3 conditional
  `PutObject` (`IfNoneMatch="*"`), which Backblaze B2 rejects with `NotImplemented` and broke
  post-merge `Build & Deploy` while fetching Granicus feeds. Leases now use only ordinary
  upload/list/delete operations and are scoped to ffprobe/ffmpeg media reads, not the shared
  `requests` adapter.
- **Swagit legacy concat probes now use the same browser UA and provider slots as other media reads.**
  The Addison 55844 failure was reproduced: the page parser finds three legacy segments and the
  concat planner is registered before silence planning, but the first segment's MP4 is unreadable
  (`moov atom not found`) and its HLS playlist returns 404, so publishing partial audio would be
  unsafe. The planner still defers that episode, but healthy legacy segments are no longer falsely
  deferred by bare ffprobe calls without the Granicus-compatible UA or rate-limit guards.
- **Feed-health fixes for Dallas meeting links and edited-timeline audio duration metadata.** Dallas'
  `meetings_url` now points at the live Swagit archive URL instead of the old `dallascityhall.com`
  page whose TLS certificate fails Python Requests verification. Edited/non-identity timelines now
  record `audio_duration_served` from the EDL's served-length total even when ffprobe reports a
  slightly rounded container duration or an existing record carries that stale rounded value. No
  pipeline version was bumped: already-hosted audio is not re-encoded, and affected records self-heal
  as the audio lane revisits them through reuse, credit, or encode paths.
- **`-user_agent` is now passed only for remote ffmpeg/ffprobe inputs (regression from the granicus
  UA fix).** The browser-compatible `-user_agent` was added to *every* ffmpeg/ffprobe invocation, but
  the encode pass reads the **local cached copy** from the source-cache (`/tmp/citypods_src_*`), and
  `-user_agent` is an HTTP-only option — ffmpeg errors `Option user_agent not found` on a `file:`
  input. The first post-fix Audio run (#6) hit this on ~1,300 cache-hit encodes (`returncode=8`,
  zero hosted). New `_ua_args(url)` emits `-user_agent` only when the input is `http(s)://`; local
  files (and insert assets) omit it. Verified end-to-end with real ffmpeg (remote → UA sent + works;
  local → no UA + encodes). `_download_audio`, `_render_identity`, `_render_filter`,
  `_probe_audio_bitrate` all route through it.
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
- **Swagit deep-link contract check no longer false-fails on the SPA player route.** Swagit's
  `/play/{id}/{t}` is a client-side route the server `404`s on a direct request — even the real
  chapter-anchor timestamps the watch page itself links — so the contract check's `HEAD` (which
  assumed a server-resolvable 2xx, true only for Granicus' `?starttime=`) flagged a false breakage.
  The check now, on a 4xx for an SPA-style path-timestamp deeplink, confirms the scheme is still
  current by finding the deeplink's path on the live watch page (`citypods/contracts.py`,
  `_is_spa_seek_url`). The deeplink *generation* was always correct (it matches the page's anchors).
- **Granicus CDN UA block round 2: drop bot-disclosure form from `USER_AGENT`.** After the initial
  Granicus UA fix landed (`Mozilla/5.0 (compatible; citypods/0.1; …)`), Granicus CDN updated its
  bot-detection to also block the `(compatible; citypods/…)` disclosure form — the Monday contracts
  check failed the next day (`arlington-tx` `media-fetch`, issue #300). `USER_AGENT` is now a plain
  Chrome-on-Linux string (`Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 … Chrome/124.0.0.0
  Safari/537.36`) with no citypods identifier; this is the only form that reliably passes CDN
  bot-detection. Also fixed: `_download_audio_file` in `stages.py` was using its own bare
  `citypods/0.1` session instead of the shared `USER_AGENT` constant — same 403 risk, now unified.
- **Granicus audio now downloads — the CDN `403` was a User-Agent block, not signing/rate-limiting.**
  `archive-video.granicus.com` `403`s non-browser User-Agents; our bare `citypods/0.1` UA (and
  ffmpeg's default `Lavf/…`) were blocked, so Granicus audio had **never** materialized (every run
  encoded only swagit). `USER_AGENT` (`http.py`) is now browser-compatible, and `media.py` passes it
  to ffmpeg/ffprobe via `-user_agent` on every remote fetch (`_download_audio`, `_render_identity`,
  `_render_filter`, `_probe_audio_bitrate`). PRs #245/#250/#251 had misdiagnosed this as a
  signing/rate-limit issue and only tested against a **mocked signed redirect**, so it passed CI while
  failing live. To prevent a recurrence, `citypods/contracts.py` gains a **media-fetch** check that
  truncated-downloads each provider's newest clip through the production fetch path (UA + protocol
  whitelist + timeout); it runs in the `-m live` suite and `scripts/check_endpoints.py` (ffmpeg added
  to `contracts.yml`), so a silent "audio never downloads" regression now fails loudly.

### Changed
- **Granicus distributed-lease TTL dropped from 3600s to 900s so a dead holder's slot is reclaimable
  in ~15 minutes instead of up to an hour.** A holder that dies without releasing (crash, SIGKILL,
  lost comms) previously pinned one of the two Granicus lease slots for the full hour-long TTL before
  `stale_leases_reaped` logic could reclaim it. This is free in renewal traffic:
  `DistributedProviderLeasePool._renew_interval` clamps the renewal cadence to 60s for any
  `ttl_seconds >= 180`, so 900/3=300 still resolves to the same 60s interval as 3600/3=1200 did — a
  legitimate fetch+encode (bounded by the 45-minute `audio_encode_timeout_minutes`) renews well
  within the shorter window. Config-only diff in `config/site_config.yml`; a new
  `test_renew_interval_is_capped_so_lowering_ttl_costs_no_renewal_traffic` locks in the
  no-extra-renewals reasoning (GH#378).
- **Audio memory admission recalibrated from Audio run #10 telemetry.** Long loudnorm/filter encodes
  in the run peaked around 9–13 GiB, beyond the old 6.5 GiB clamp. `estimate_encode_rss_bytes` now
  uses a 64 MiB/min served-duration coefficient with a 12,000 MiB max/unknown reservation, so very
  long or unknown-length filter jobs run alone against the 12 GiB budget instead of being admitted
  beside another large encode and then hitting the 1.5 GiB memory floor.
- **Audio encodes are admitted by *predicted* memory, not instantaneous free memory (stops mid-flight
  ffmpeg terminations).** The first clean Audio runs (#8/#9) hosted real audio but terminated ~46% of
  the large filter-render (loudnorm) encodes of multi-hour meetings with `ffmpeg filter-render stopped:
  mem_avail … below floor`. Root cause: admission was an *instantaneous* `mem_available` check, which is
  a **trailing** signal — a long loudnorm encode grows for minutes (memory-floor kills fired **220–1080 s**
  into encodes that peaked at up to **5.9 GiB**), so free memory still looks healthy when a *second* big
  encode starts, and the two then collide. New `MemoryReservation` (`citypods/resources.py`) admits each
  encode against a **budget** (`audio_memory_budget_mb`, ~12 GiB of the 15.6 GiB runner): each encode
  reserves its **estimated peak RSS** — `media.estimate_encode_rss_bytes`, keyed on the known-ahead
  served length (the EDL the `TimelineStage` already built, or the feed duration; a conservative default
  when neither is known) — and a new encode begins only when `reserved + estimate ≤ budget`. That gates
  on the job's *future* footprint, so ≈2 big encodes (or many small ones) overlap with headroom and a
  third big job waits instead of colliding. `native_audio_max_active` drops `4 → 3` (the hard
  concurrency ceiling); the 1.5 GiB `audio_ffmpeg_memory_floor_mb` stays as the backstop for estimate
  misses. The reservation supersedes the old `resource_guard_min_available_mb` gate for audio (that gate
  now governs only ASR). The cost-model coefficients are a first heuristic, calibratable from the
  per-encode `peak_rss` already logged.
- **Source shards are now weighted by configured feed/body count instead of raw source count.**
  `records.shard_assignment` still assigns each `source_key` to exactly one shard (so scoped state
  pushes remain safe), but it now greedily packs heavier sources onto the lightest shard. `run.py`
  passes a stable config-derived weight — the number of configured feeds sharing the source — so
  every matrix job computes the same partition while large multi-body sources like Dallas/Fort Worth
  are no longer bundled with extra small sources merely because source counts balanced.
  **Superseded:** this config-derived weight was later replaced by each source's actual pending
  audio-encode backlog (`pending_audio_work`), and — for the ASR lane only — by pending
  routing-aware transcription cost (`estimate_transcribe_shard_work`, above).
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
    few sources) is replaced by `records.shard_assignment`: initially round-robin over sorted
    source_keys, later upgraded to weighted greedy packing by configured feed/body count. It remains
    deterministic, source-atomic, disjoint, and exhaustive.
  - **Accurate ffmpeg timing** — the guard's poll cadence drops from 5 s to 0.5 s so the logged
    `seconds=` reflects a child's real runtime (the 5 s cadence had made every sub-5 s fetch read as
    `seconds=5.0`, masking the truncation).
- **Sharded `audio.yml` + `asr.yml` workflows, lane-pinned (H6b)** —
  ([#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273)). The combined
  `enrich.yml` (H11b) is replaced by two dedicated workflows, each on its own concurrency group
  (`audio` / `asr`, both distinct from `pages`) and a `strategy.matrix.shard` of 4 source-shards, so
  a deploy is never canceled by heavy work and concurrent shards clear the backlog without clobbering
  records. New `citypods enrich` flags: `--shard K/N` (keep only sources assigned to shard `K`;
  source-atomic, disjoint + exhaustive across `K`), `--source KEY`, and
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
- **`reset-backoff` recovery tool + workflow — drain the #120 backoff after a fixed encode bug.** When
  a now-fixed bug made encodes fail (Granicus UA 403s #293/#297, Swagit truncation #274), each failure
  incremented the record's `materialize_attempts`, so those episodes are skipped for up to 30 days even
  though the bug is gone. Granicus never hosted at all, so `clear-materialization.yml` (which keys on
  `audio encode done`) can't reach its records. New `scripts/reset_materialize_backoff.py` scans the
  durable record store and clears the backoff fields (`attempts`/`last_attempt`/`error`) of every
  record that is **un-hosted *and* in backoff** — never touching a hosted record, the transcript block,
  or the durable `state/` snapshot. Optional `--provider`/`--source` filters (verify one provider
  end-to-end first); dry-run unless `--apply`; pushes back only the affected `sources/<key>/` prefixes.
  Dispatch via the new **Reset materialization backoff** workflow (shares the `audio` concurrency group
  so it never races an Audio run's state push).
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
