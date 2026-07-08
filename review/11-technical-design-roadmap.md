# Technical Design Roadmap (canonical, living)

**Status: LIVING · last updated 2026-07-08 (Stage-2 work-lease reaper config-nesting fix, GH#706 §6(b))**

This is the canonical **forward design** reference for the project — the single map of every initiative
needed to deliver [ROADMAP.md](../ROADMAP.md) and [VISION.md](../VISION.md), the maturity of each, and a
pointer to its detailed design. It exists to:

- be the place a developer or AI agent goes to **pick the next thing to build** with full understanding;
- **seed GitHub Issues** (a development-ready entry contains enough to file issues with no further design);
- keep the project's design documentation from **going stale** (the failure mode of `review/00–10`).

Unlike `review/00–10` (point-in-time snapshots), **this document is kept current**. Its companions:
[ARCHITECTURE.md](../ARCHITECTURE.md) (as-built), [CHANGELOG.md](../CHANGELOG.md) (what shipped),
[CONTRIBUTING.md](../CONTRIBUTING.md) (process — the *normative* copy of the lifecycle below).

This roadmap also folds in and supersedes the standing parts of the **Codex review**
([`review/10`](10-codex-architecture-throughput-roadmap-review.md)); see §8 for the appraisal and the
post-review code queue.

---

## §1. How to use this document

1. Find an initiative in the **current phase** (Phase H first) whose **maturity is L3**.
2. Open its **breakout doc** (`review/12+`) and implement from the file/test/sequencing plan there.
3. Update the docs per the **lifecycle contract (§2)** in the same change — including flipping this
   document's catalog entry and adding a [CHANGELOG.md](../CHANGELOG.md) line.
4. When a planned development step is completed (especially once its PR merges), do the **Implemented**
   row in §2 immediately: mark it Shipped here with the PR link, add the CHANGELOG entry, stamp/freeze
   its breakout, move/narrow the ROADMAP item, and update ARCHITECTURE if the shipped code changed the
   as-built system shape.
5. Before committing an edit to *this* file, run the **standing verification checklist (§7)**.

---

## §2. Feature lifecycle & doc-update contract

The normative copy lives in [CONTRIBUTING.md](../CONTRIBUTING.md); this is the mirror. Every feature
travels this pipeline; when you move it forward, update the listed docs **in the same change**:

| Stage | Trigger | Update (change X → update Y) |
|---|---|---|
| **Idea (L0)** | captured | `VISION.md` (long-horizon) **or** this doc's Deferred-backlog (§6) |
| **Committed (L0→L1)** | promoted to near-term | `ROADMAP.md` + catalog (§4) at L1 + write the L1 sketch inline (§5) |
| **Designed (L1→L2)** | approach chosen / next-up | **break out** to a new `review/NN`; catalog entry → L2 + link |
| **Dev-ready (L2→L3)** | full design done | mature `review/NN` to L3; **cut GitHub Issue(s)**; catalog → L3 |
| **Implemented** | PR merged | catalog → **Shipped** (+PR link); add **CHANGELOG.md** entry; update **ARCHITECTURE.md** if architecture changed; **freeze + stamp** the breakout ("Implemented in PR #N"); move ROADMAP item to "Recently shipped"; close/narrow issues; capture any durable decision in the relevant committed doc |
| **Superseded** | abandoned/replaced | mark the catalog entry; note in CHANGELOG if ever partially shipped |

---

## §3. Maturity ladder (and where each level lives)

| Level | Meaning | Lives in | Promote when |
|---|---|---|---|
| **L0 Idea** | named only | `VISION.md` / `ROADMAP.md` (+ catalog row) | an approach is worth sketching |
| **L1 Sketch** | problem + 1–3 candidate approaches, rough tradeoffs | **inline in this doc (§5)** | an approach is chosen / it's next-up |
| **L2 Designed** | chosen approach; data-model deltas; module/file plan; interfaces; risks | **breakout `review/NN`** | the design is detailed enough to estimate |
| **L3 Dev-ready** | full 08/09 depth: concrete file/function changes, test plan, sequencing/DAG, migration/backfill, acceptance criteria | the **same breakout**, matured | ready to cut into Issues & build |

**Break out at L1→L2.** A breakout doc is born when an initiative is committed to an approach and is
about to be worked next (or when its design exceeds ~one screen). Until then it stays an L1 sketch here.

---

## §4. Initiative catalog (exhaustive)

Every feature named anywhere in the project's docs (review/01 #1–#57, review/02–09, ROADMAP, PLAN, and
open issues) appears here, in a phase or the Deferred backlog (§6). `#NN` = review/01 roadmap item;
`GH#` = GitHub issue.

GitHub development issues are cut just-in-time only for remaining Phase-H work. Auto-managed health
signals and individual city requests may remain open as operational inputs; later-phase initiatives
live here until their series becomes active.

### Shipped (foundations) — see [CHANGELOG.md](../CHANGELOG.md)
INFRA-1..9 timeline foundation (GH#141) · #52 content permanence (absorbs **#45** self-healing
dead-enclosure re-resolve) · #51 official meetings link · #22
silence-trim · #21 loudness · #23 host-all · #122 concat · clips service · #1/#110 ASR · #11
`<podcast:transcript>` · #124 status dashboard/admin (#49 partial) · #29 SSRF gate · #35 projection ·
#36 run-history · #37 endpoint contracts · #38 retry/backoff · #43 alias validation · review/02 Change 8
provider capabilities · resource/agenda links + `content:encoded` + `<podcast:chapters>` · durable state
sync · #20 video enclosures (partial).

### Phase H — Hardening & Efficiency · breakout [`review/12`](12-hardening-and-efficiency.md)

> **Reprioritized 2026-06-08** (build-log root-cause analysis — see
> [review/12 § Build-log root-cause analysis](12-hardening-and-efficiency.md#build-log-root-cause-analysis-2026-06-08)):
> **H10 shipped in PR #232** and **H8 shipped in PR #235**; the remaining do-now reliability item
> **H11a** runs **ahead of H1–H5**. These fixes address what is actually turning Build & Deploy red on
> ~half of scheduled runs — runner
> starvation (unpinned ffmpeg `-threads` + no memory admission control; `continue-on-error` can't catch a
> runner-level SIGTERM/lost-comms). H10 fixed the broken ASR `align` path that yielded zero transcripts
> for caption-bearing feeds.

| Item | #/GH | Maturity | Status |
|---|---|---|---|
| H1 docs/issue reconciliation | #52-health, GH#110/#141/#154 | L3 | **Shipped** (this doc set + GH#154 closed, GH#110 narrowed to backfill/ops; GH#141 later closed when `review/11` became the sole cross-series backlog) |
| H2 projection wall-clock fix | R3 | L3 | **Shipped** (per_run_cap→None default; materialize_encoded calibration; hours_hosted bytes fallback; gate wait + peak load + window % telemetry in run_history.jsonl; complete logical-run aggregation for shard events; per-stage structured defer reasons; audio + transcript backlog ETAs in build_status) |
| H3 feed-validation publish gate | #53 | L3 | **Shipped** (`validate_build` + `citypods validate-build` CLI + `deploy.yml` gate before Pages upload; redirect feeds skipped; known-empty slugs demoted to warn) |
| H4 feed-health catch-up vs stalled states + per-provider error-rate tracking | R5 | L3 | **Shipped** (suppress catching-up; warn stalled ≥ 3/5; `provider_errors` in `run_history.jsonl` + `check_provider_error_rates`; `audit_feeds.py` auto-comments on state transitions; 19 new tests). **Extended:** `audit_feeds.py` now files one consolidated issue per *check* (not per `(slug, check)`) for every check except `meetings-url-dead`/`meetings-url-changed`, which stay one-issue-per-city since each needs a distinct human YAML fix. The title carries a live affected feed/city count; matching uses a hidden `key=` marker in the body (not the title) so the title can change every run without breaking reconciliation; per-feed "first seen" is tracked in a hidden JSON state block in the body itself (no external ledger); a visible comment posts only when the affected-feed *set* changes, not on a cosmetic "since Nd ago" refresh; bodies gained verbose causes/resolution guidance per check. |
| H5 stage backlog manifest + prioritization policy | #41, R2 | L3 | **Shipped** ([#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263) ordering engine + [#264](https://github.com/BashfulBits/city-meeting-podcasts/pull/264) manifest/sidecar/status/light-ordering + [#265](https://github.com/BashfulBits/city-meeting-podcasts/pull/265) global two-pass enrich queue) — hybrid manifest (`citypods/ops/workqueue.py`); behavior-preserving deterministic default; comparator registry (windowed `recency`/`within_days`, partial `city_order`, …); diarization-forward artifact-keyed schema; prod policy `recency:{desc, within_days:30}`; global newest-everywhere-first enrich (on-runner audio pass + decoupled async-ready transcript pass — transcribe/diarize go over-the-wall to external workers, H9/H6b); buckets reserved-but-inert; archive-backfill → Deferred (§6). Design frozen in [review/12 §H5](12-hardening-and-efficiency.md#h5--stage-backlog-manifest--configurable-prioritization-policy). Deferred to H6b/H9: competitive lease acquisition + per-item persistence. |
| H6a ASR benchmark workflow (`asr-bench.yml`) | #1 | L3 | **Shipped** ([PR #256](https://github.com/BashfulBits/city-meeting-podcasts/pull/256)) |
| H6b separate audio + ASR workflows, sharded | #1, R1 | L3 | **Shipped** ([#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273)) · `enrich.yml` replaced by `audio.yml` + `asr.yml` (own `audio`/`asr` concurrency groups, `strategy.matrix.shard`=4); `enrich --shard K/N`/`--source`/`--lane {audio,transcribe,align}`; `run.py` filters cities by source-atomic weighted `shard_assignment(source_key)` + threads the lane into the two-pass queue; Audio weights pending playable/unknown work by expected served duration and gives availability-withheld recovery probes only a small fixed cost; **scoped `push_state(only_prefixes=)` + `reconcile_state(full_run=False)`** so shards don't clobber; `audio.yml`=`--lane audio`, `asr.yml`=`--lane transcribe` with `asr-transcribe` only; **`align` lane implemented but unscheduled** and assigned the separate `asr-align` extra (forced alignment deferred — caption feeds get fresh ASR meanwhile); provider leases reserved for H14 except Granicus media fetches now have a targeted cross-shard lease. **Follow-up fix (2026-06-16, `fix/cross-lane-record-clobber`):** the per-shard scope did not cover the *cross-lane* lost update — the audio and ASR workflows write the same `source_key`'s `episodes.json` at overlapping times, so a late ASR run re-uploaded its start-of-run `audio` block over freshly hosted audio (`hosted_audio −16`). Scoped pushes are now **foreign-block-preserving** (`records.protected_blocks_for_lane`/`merge_preserving_foreign`, `statesync.push_records_merged`) and `stages.LANE_STAGES` keeps each lane to its own work-class stages. Block/lane registries extend to the `diarize` lane (review/12 §H5/§H6). **Deferred:** per-stage object files (`audio.json`/`transcript.json`/`speakers.json`) to remove the shared `episodes.json` read-modify-write entirely (closes the residual TOCTOU window — §6). |
| H7 contributor/agent handoff docs | #57 (partial), R9 | L3 | **Shipped** (this doc set: AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING + templates) |
| H8 4-core runner saturation (ffmpeg `-threads` + memory admission + killable ASR process) | new | L3 | **Shipped** ([PR #235](https://github.com/BashfulBits/city-meeting-podcasts/pull/235)); 2026-06-18 follow-up replaces length-growing dynamic loudnorm and parallel-trim buffering with versioned bounded-memory multi-mic speech mastering (sample-accurate streaming timeline → high-pass → `dynaudnorm` → compressor → measured linear loudnorm), fixed 768 MiB reservation, phase-specific native admission, a constant-gain + short-lookahead limiter fallback for peak-constrained material, and gradual content-addressed remastering. 2026-06-20 follow-up moves local ASR into a persistent killable subprocess and persists per-episode exponential timeout backoff so one native stall no longer poisons the rest of a shard. |
| H9 combined-throughput evaluation | [GH#278](https://github.com/BashfulBits/city-meeting-podcasts/issues/278) | L3 | **Committed** · measure the three execution homes (local-sharded H6b + Modal + Beam H14) across the real recording-duration distribution, including local success/peak memory by duration/model, CPU/GPU real-time factor, cold start, download/decode, chunk/stitch overhead and boundary quality, whole-episode-vs-chunk retry cost, artifact transfer/storage, free-tier consumption, queue age/routing outcomes, $/transcript-hour, and incremental $/diarized speaker-hour. Compare separate workers with one combined external ASR+diarization flow. Use the results to set the safe local default, preferred-routing thresholds, chunk-persistence decision, and first paid/self-hosted step. **Final GPU-type profiling waits for H14d's memory telemetry/admission optimization** so throughput measurements are not polluted by avoidable OOM/preemption retries. Gate: the 80-feed catalog must complete backlog in <1 month on free tiers. Design: [review/12 §H9](12-hardening-and-efficiency.md#h9--combined-throughput-evaluation-diarization-speakerhour). |
| H10 ASR alignment fix (`WhisperModel.align` AttributeError + fallback gap) | new | L3 | **Shipped** ([PR #232](https://github.com/BashfulBits/city-meeting-podcasts/pull/232)) |
| H11a deploy resilience — native work gate + one-slot audio lane + concurrency tuning | new | L3 | **Shipped** ([#239](https://github.com/BashfulBits/city-meeting-podcasts/pull/239)/[#241](https://github.com/BashfulBits/city-meeting-podcasts/pull/241)/[#242](https://github.com/BashfulBits/city-meeting-podcasts/pull/242)/[#243](https://github.com/BashfulBits/city-meeting-podcasts/pull/243)/[#244](https://github.com/BashfulBits/city-meeting-podcasts/pull/244)/[#246](https://github.com/BashfulBits/city-meeting-podcasts/pull/246)/[#247](https://github.com/BashfulBits/city-meeting-podcasts/pull/247)) |
| H11b deploy resilience — render-only deploy | new | L3 | **Shipped** ([#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272)) · `deploy.yml` stripped to render-only (no ffmpeg/ASR, `actions: read` dropped); heavy phase → new `enrich.yml` (own `enrich` concurrency group); **render writes only `docs/`** — `build()` gates `save_records`/`push_state`/`reconcile_state` off `--phase render` so the enrich workflow is the sole record writer (closes the lost-update record-write race); `statesync.push_state(only_prefixes=)` + `reconcile_state(full_run=)` scope hooks ready for H6b sharding. Precedes H6b. |
| H11c deploy resilience — graceful SIGTERM + mid-run checkpoint | GH#377 | L3 | **Implemented (unreleased)** ([#386](https://github.com/BashfulBits/city-meeting-podcasts/pull/386)) · closes the gap noted above that `continue-on-error` can't catch a runner-level SIGTERM/lost-comms: the CLI entry installs a SIGTERM handler latching a process-wide interrupt the existing `StopSignal` predicate ORs in, so a GitHub cancel/lost-comms converts to the graceful-stop path (in-flight workers defer, the run still persists records + writes its `run_history` entry + pushes state). The global enrich queue persists every source as the **audio pass** drains (before the decoupled transcript pass) and again at the end — idempotent (append-only `merge_records`; `persist_source` no longer mutates the caller's notes list). Interrupted runs are tagged `interrupted`/`outcome:"interrupted"` in `run_history.jsonl` + `run_summary.json` and exit `143` (128+SIGTERM); a normal wall-clock/supersession yield is **not** an interrupt and stays exit `0`. Follows H11a/H11b. |
| H11d deploy resilience — retry `actions/deploy-pages` on transient backend failures | new | L3 | **Shipped** ([#822](https://github.com/BashfulBits/city-meeting-podcasts/pull/822)) · two `Build & Deploy` runs on 2026-07-05 failed at the `Deploy to GitHub Pages` step with GitHub's own generic `Deployment failed, try again later.` despite a clean render/upload and no overlapping Pages deploy (the `pages` concurrency group already rules out self-inflicted races) — a known transient hiccup in `actions/deploy-pages` itself, not a build/render defect. `deploy.yml`'s deploy step now attempts up to 3 times with backoff (15s, then 30s) before failing the job; unrelated to H11a–c (no SIGTERM/queue/record-write path involved). |
| H12 transcript artifact rework (segment VTT + word-JSON + version-aware re-transcribe) | #249 regression, R2/#7 | L3 | **Shipped** ([PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253)) |
| #39 per-provider rate limiting (incl. Retry-After clamp) | #39 | L2→L3 | **Shipped** ([#274](https://github.com/BashfulBits/city-meeting-podcasts/issues/274)) · process-global `HostRateLimiter` (per-registrable-domain cap from `provider_rate_limits`) acquired by **both** `GuardedHTTPAdapter.send` **and** the ffmpeg/ffprobe fetch paths (`media.py`, `concat.py`) — the H6b storm was ffmpeg, not `requests`, so capping only the adapter would have missed it; Granicus follow-up adds B2-compatible soft `provider_distributed_leases` around media reads across the four audio shards (initially 6 after 2026-06-15 overlap probes, reduced to 2 by GH#300 Phase 1) plus `rate_limited` classification and a circuit breaker (initially run-local, now storage-shared and tenant-scoped in the #337 follow-up); Granicus 403 backoff lifted into the shared retry (403 in `status_forcelist`, Retry-After clamp kept); also fixed the H6b regressions it surfaced: truncation guard (encode < 50 % of declared duration → #120 backoff, not hosted), source-atomic weighted `shard_assignment` (no empty `audio (0)`, large sources packed first), responsive 0.5 s ffmpeg poll (honest `seconds=`). Design: [review/12 §#39](12-hardening-and-efficiency.md#39--per-provider-rate-limiting-sequence-with-h6b) |
| **H16 Granicus proxy validation + recovery simplification** | [GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353) | L3 | **In development** · shared GitHub egress reputation—not request shape or the configured concurrency ceiling—caused the Actions-runner 403s. PRs #368–#370 shipped a tenant-allowlisted authenticated Cloudflare Worker and a direct-first, single-Worker-attempt fallback inside the unchanged 1-local / 2-distributed envelope. Audio #46/#47 supplied favorable transport evidence but exposed missing acceptance automation, identity proof, generic signed-URL redaction, and a separate need to represent city-supplied empty/invalid recordings without publishing them. **PR1 acceptance reporting shipped in [#405](https://github.com/BashfulBits/city-meeting-podcasts/pull/405); PR2 identity invariants + generic subprocess redaction shipped in [#406](https://github.com/BashfulBits/city-meeting-podcasts/pull/406).** Every Audio shard now proves stable Granicus record/artifact identity and strips signed media queries and credentials from subprocess surfaces, so qualifying runs receive a real identity verdict instead of `not_reported`. **PR3a durable media-availability classification is implemented (unreleased):** an explicit, versioned `media_availability` verdict (available / suspected/confirmed empty / missing / invalid / recovered + operator override) rides the audio lane's existing silence-detect decode, withholds empty/missing recordings from both feeds and `AudioStage` while metadata stages continue, requires two independent successful silent fetches to confirm (transport failures never confirm), and recovers automatically — all without bumping the audio pipeline version. **PR3b bounded proxy evidence + weekly review digest is implemented (unreleased):** `availability-digest.yml` / `scripts/availability_digest.py` deterministically sample new/changed empty-recording candidates, emit untrimmed + silence-trimmed low-bitrate proxies plus redacted evidence JSON, and open/update one rolling digest issue only when candidates exist. PR3 narrowly promotes the record/evidence substrate of the Phase-R provider-failure design while leaving presentation/query UI in Phase R. **Identity-mismatch follow-up resolved (unreleased):** Audio #54 and #56 each failed `identity` with a lone `audio_key`/`audio_spec_hash`/`audio_url` mismatch — a false positive. Proven from #56's per-shard log: an episode's recipe changed mid-run and its re-encode probed a new duration, then the **upload failed transiently** (B2 `ServiceUnavailable`); `materialize_audio` had written `audio_duration_served` before `put_file`, leaving the record carrying the new duration while still pointing at the prior valid artifact, so `verify`'s duration-change branch flagged the retained old key/spec/url against the fresh recompute. Fixes: (1) the encode commits `audio_duration_served` atomically *after* a successful upload; (2) `verify` skips the key/spec/url comparison when the artifact identity is unchanged from capture (pending re-encode, not corruption — generalizing the earlier `legacy_ok` exemption, which was a misdiagnosis: the spec was a real hash, not `"legacy"`). The `stale_leases_reaped` correlation was common-cause, not causal; all six runs (51–56) had **zero** circuit/parking/canary activity. Separately filed ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421)): per-board vs combined feeds with divergent `feed_urls` get distinct `source_key`s, so the same meeting is encoded under two keys (duplicate CAS objects) — efficiency/correctness, analogous to the ASR duplicate-source-view coalescing already shipped. This removes the only observed identity failures. **Circuit/parking/canary machinery removed (unreleased):** six runs (#51–#56) showed zero circuit trips/deferrals/recovery probes, so the storage-backed rate-limit circuit breaker plus its queue parking and half-open canary recovery were deleted (`provider_circuits.py` → lean `provider_transport.py`, keeping only the per-tenant transport telemetry that feeds the H16 `transport` criterion; the `_run_enrich_global_queue` parking/canary block and `circuit_skipped`/`circuit_keys` plumbing are gone; H16 report schema → v2). The breaker was built for a concurrency-throttle hypothesis H16 disproved; aggregate load stays bound by the provider-lease ceiling and per-episode materialize backoff, and rollback to direct-only remains config-only (unset the two `GRANICUS_PROXY_*` secrets). The remaining open decision is direct-first vs sticky-Worker routing. Older GH#333/#337/#338/#352 proposals are absorbed: concurrency ramping is superseded; provider-media identity/coalescing and selective durable source reuse remain trigger-gated scaling work in [`review/16`](16-scaling-review-plan.md), not current H work. Design: [review/12 §Granicus follow-up](12-hardening-and-efficiency.md#granicus-media-reliability-follow-up-gh300--39-follow-up). |
| H16 duplicate-view audio coalescing | [GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421) | L3 | **Implemented (unreleased)** · per-board vs combined feeds with divergent `feed_urls` retain distinct `source_key`s, but entity-family audio shard affinity co-locates them and a run-local `(provider, stable uid, audio recipe)` cache fans one successful artifact pointer to every alias. New work encodes once into one deterministic CAS prefix; an existing valid alias artifact may instead become the shared winner. No source-key, UID, recipe, or pipeline-version change means no forced backfill; unreferenced old duplicates fall to normal orphan GC. |
| **H13 GPU/ASR execution-backend interface (+ `local` adapter)** | §5.5, [#271](https://github.com/BashfulBits/city-meeting-podcasts/issues/271) | L3 | **Shipped** (#271) · **pre-1.0 lock** · `citypods/compute/{base,local}.py` mirrors `storage/`; `base.py` types all task verbs (ASR + the reserved R3/R4 LLM verbs) + `InferenceJob`/`JobResult`/`JobHandle` + `runtime_checkable` `Backend`; the `local` adapter wraps in-process faster-whisper/stable-ts (byte-identical); `TranscriptStage` routes through `backend.run_inference(...)`; `compute_backend: local` default. Design: [review/12 §H13](12-hardening-and-efficiency.md#h13--gpuasr-execution-backend-interface--local-adapter--the-pre-10-lock) |
| **H14 external transcription adapters** | H14b [GH#276](https://github.com/BashfulBits/city-meeting-podcasts/issues/276) · H14c [GH#277](https://github.com/BashfulBits/city-meeting-podcasts/issues/277) | L3 | **H14a substrate Shipped** ([#275](https://github.com/BashfulBits/city-meeting-podcasts/issues/275)); **local-fallback duration admission + routing-aware shard cost + canonical planner snapshot implemented (unreleased)**: external dispatch is attempted first, but a declined/`local` job above the configurable 4h in-process faster-whisper ceiling remains queued (`reason=external-required`) rather than becoming a runner OOM/retry loop. `TranscribeShardWork` separates duration-weighted local fallback from fixed-cost external dispatch, minimal blocked/deferred inspection, and zero-cost in-flight work so external-only long audio does not distort local runner balance. `asr.yml` now restores B2 state once in reconcile, emits a versioned source-atomic `ShardPlan`, and publishes the state snapshot plus plan as one immutable artifact consumed by all matrix shards, removing both assignment races and four duplicate full-state restores. The rolling 100-sample estimator remains the independent time-window guard. **H14b (Modal) implemented + merged** ([#807](https://github.com/BashfulBits/city-meeting-podcasts/pull/807), closes [GH#276](https://github.com/BashfulBits/city-meeting-podcasts/issues/276)); **H14c (Beam) in review** ([#808](https://github.com/BashfulBits/city-meeting-podcasts/pull/808), [GH#277](https://github.com/BashfulBits/city-meeting-podcasts/issues/277)). Both worker images install the **same version-pinned dependency set as the runner's transcribe lane** (`constraints/asr.txt`, no torch) and **bake the same pinned Whisper revision** (via `citypods.asr`, `ASR_MODEL_PATH`) on a digest-pinned **CUDA 12 + cuDNN 9** base (forward-compatible with a torch diarize step), per the dependency-policy umbrella [GH#804](https://github.com/BashfulBits/city-meeting-podcasts/issues/804). Modal's build genuinely stages local repo files at build time; Beam's remote build cannot ([GH#818](https://github.com/BashfulBits/city-meeting-podcasts/issues/818)), so `beam_app.py` resolves the same pins/model constant **locally** on the machine invoking `beam deploy` and bakes the literal values into the image spec rather than referencing the files by path. Both record per-claim **RSS/GPU-VRAM telemetry** to a CAS object that feeds **H14d** admission/chunking. First **live single-recording validation** (`max_claims: 1`) is gated by the [pre-live checklist GH#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706) / handoff [GH#794](https://github.com/BashfulBits/city-meeting-podcasts/issues/794). Smoke-testing surfaced a claim-accounting fix: `max_claims` counts **new transcriptions**, not items whose artifacts already exist — those are *adopted* (state reconciled, no GPU) without consuming a slot, so a re-run against a stale manifest scans past already-done head items to reach fresh work (bounded by `max_scan`, default `max_claims + 50`) instead of adopting the head item and stopping. Both must accept recordings above the local ceiling with bounded-memory, backend-independent chunk planning/stitching when required. They consume H17's pull/claim contract; adapters supply routing inputs but must not independently predict live GPU availability or assign ownership. Accepted external work contributes fixed dispatch cost, already in-flight work contributes zero, eligible local overflow contributes recording-duration cost, and work with no eligible backend contributes only minimal blocked cost. “Overflow to local” means local passes both duration/memory and runtime/deadline admission; unavailable external capacity is a queued routing state, not an ASR failure/backoff event. Prefer one external ASR+diarization worker flow for shared I/O/preparation/startup/artifact coordination, while keeping transcripts independently publishable, reconciling speaker identity meeting-wide, preserving the episode-level `transcribe`/`align`/`diarize` `InferenceJob` verbs, and acknowledging the neural models are distinct. Design: [review/12 §H14](12-hardening-and-efficiency.md#h14--external-transcription-adapters-modal--beam-free-tier-bounded-async-dispatch), [§H14b](12-hardening-and-efficiency.md#h14b--modal-transcription-adapter-async-dispatch-backend), and [§H14c](12-hardening-and-efficiency.md#h14c--beam-transcription-adapter-async-dispatch-backend-parallel-with-h14b). Mac-mini/AWS post-1.0. |
| **H14d GPU worker memory/admission optimization** | [GH#794](https://github.com/BashfulBits/city-meeting-podcasts/issues/794) | L3 | **Implemented (unreleased)** · H14d converted the first live Modal/Beam telemetry into the production pacing knobs now carried in `config/site_config.yml`: generic budget units + soft reserve, per-backend preferred days (`modal=even`, `beam=odd`), fresh-work windows, long-meeting preference, and fixed-per-run / fixed-per-claim planning hooks for future diarize/combined flows. The current production posture stays deliberately conservative inside the container — **one active transcription at a time** — but raises **sequential** multi-claim ceilings so a worker invocation can actually spend the monthly budget on its preferred days. Measured GPU-type normalization on a fixed Denton pair selected Beam `RTX4090` and Modal `L4` as the default cost-efficient GPUs; H14d also added rerunnable canary entrypoints plus report support that surfaces claimable long/fresh backlog composition and recent telemetry samples. Live validation then found one more control-plane bug: external workers and `asr-worker-report` were trusting stale persisted `state/work.json`; they now rebuild a fresh manifest from canonical records and overlay only persisted operational sidecar state, restoring the real long-meeting backlog view (`91` backlog-long claims over 4h in the first post-fix report). The remaining `unknown duration` bucket is **not blocked** — those episodes stay eligible for claim and can be re-measured later by audio materialization or the hosted-audio/ASR path; they simply do not receive the duration-based long-first preference until a duration is known. Chunking remains **disabled by default** until telemetry shows a clear throughput or memory-pressure need, and any future chunked path still needs an explicit recipe/version story before it can change output. Design: [review/12 §H14d](12-hardening-and-efficiency.md#h14d--gpu-worker-memoryadmission-optimization). |
| H15 transcript-quality metric (periodic caption-trust scoring) | [GH#391](https://github.com/BashfulBits/city-meeting-podcasts/issues/391) | L2→L3 | **Committed** · turn the unmeasured "served captions are faithful enough to align against" assumption (why H6b's `align` lane is **implemented but unscheduled**) into a **periodic, per-source, computed metric** instead of a one-time WER study. H15 now consumes a separate provider-transcript registry: city-provided documents stay downloadable as **Original city-provided transcript**, only own `<podcast:transcript>` until ASR/provider-alignment exists, and carry `float \| null` confidence. **No-regeneration invariant:** ASR, provider-transcript-align, and diarize invalidate only on timeline-plan or own-recipe changes; any key migration copies/aliases existing ASR VTT + word JSON instead of recomputing the ~1000 completed episodes. Three layers: **L1** = free acoustic-fit recorded *every run* from the `stable_whisper.align()` call we already make; **L2** = an independent CTC forced aligner over rotating samples; **L3** = a small human-gold sample anchoring absolute WER/CER. Output: per-source trust/confidence that gates `align`/provider-align vs `transcribe` routing + `/admin/status`. Provider-transcript rollout **PT-PR1–PT-PR7** (PT-PR1 shipped in [#452](https://github.com/BashfulBits/city-meeting-podcasts/pull/452); PT-PR2 implemented in [#456](https://github.com/BashfulBits/city-meeting-podcasts/pull/456) with provider-source candidate fetch/backfill and no ASR backfill; PT-PR3 implemented in [#457](https://github.com/BashfulBits/city-meeting-podcasts/pull/457) with render-only provider-original exposure and no ASR backfill; PT-PR4 implemented in [#458](https://github.com/BashfulBits/city-meeting-podcasts/pull/458) with migration-safe ASR key rebase, copy-first migration, and no ASR version bump; PT-PR5 implemented in [#459](https://github.com/BashfulBits/city-meeting-podcasts/pull/459) with provider-transcript-align work, served-time remap, confidence-gated promotion, and no ASR version bump; PT-PR6 implemented in [#460](https://github.com/BashfulBits/city-meeting-podcasts/pull/460) with provider-transcript-diarize work, independent `speakers.json`, transcript-preserving failure status, and no ASR version bump; PT-PR7 implemented in [#461](https://github.com/BashfulBits/city-meeting-podcasts/pull/461) with provider-transcript status/admin slices and no pipeline-version changes) and its H14b/H14c/H15 phasing are normative in [review/12 §H15](12-hardening-and-efficiency.md#h15--transcript-quality-metric-periodic-caption-trust-scoring); the concurrent-write lanes PT-PR5/PT-PR6 ride the shipped H17 Stage-1 owned-block merge on B2 (records stay on B2 → managed search-DB at Phase R; no R2 record migration). |
| **H17 distributed work/control-plane substrate** | [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390) | L2→L3 | **Implemented — PR1–PR6 all merged, [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390) closed; unblocked H14b/H14c, both now live.** Promoted the implementation platform from [`review/17`](17-state-store-backend-evaluation.md) + [`review/18`](18-work-distribution-sharding.md) into Phase H: `RoutingStorage` + native R2 `put_cas()`; ownership-keyed per-episode transcript merge and per-`(source,uid)` planning; then the R2 pull-ledger/claim protocol external workers consume. This absorbs GH#340's durable scheduling concern: expensive work is checkpointed, leased, and reclaimed rather than preempted and restarted; a thin cron coalescer remains an optional Actions-cost optimization. H14b/H14c workers are pullers against this claim contract, not passive push executors. **PR phasing ([GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390) comments):** PR1–PR5 merged (`RoutingStorage`/CAS #393, Stage-1 ownership merge #394, `compute_budget.json`→R2 #395, work-lease ledger #397, live validation harness #403). **PR6 implemented (the final H17 PR):** the `DistributedProviderLeasePool` audio-throttle coordination moved from a list-and-sort FIFO candidate election to **per-slot CAS objects** (`provider-leases/<domain>/slot-<i>.json`, added to `COORDINATION_PREFIXES`); the old per-poll *list* was an R2 Class-A op, so the CAS model — which never lists and spends Class-A only on claim/renew/release — both simplifies and de-costs the one "hot" coordination path. Trade-offs: FIFO fairness dropped (the contract is the concurrency *cap*) and a soft N+1 reap-race, both fine for a rate limiter; the pool now requires a CAS backend and degrades to in-process-only on b2/local. Circuit/parking/canary were already deleted by GH#353, so PR6 was lease-pool-only; the PR5 validation harness gained a live provider-slot check. The cutover rides `audio_storage_backend: routing` (already flipped) and is exercised on the next live Granicus audio run. **Stage-2 work-lease reaper: config said on, but the sweep never actually ran until just now.** `work_lease_reaper_enabled: true` was set in `config/site_config.yml` (nested under `defaults:`) believing it flipped the reaper on, but `citypods compute reconcile`'s CLI read the flag at the config document root — a silent `False` fallback on every run, so `reap_work_leases()` was never invoked in production despite H14b/H14c being live. Found closing out [GH#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706) §6(b): a raw-ledger audit turned up 108 leased objects (90 past TTL) that dozens of scheduled reconciles had silently never swept. Fixed to read from `site_config["defaults"]`, with a CLI-level regression test. The 2026-07-06 canaries' "claim → artifact write → budget settle" and artifact-presence-⇒-done confirmations were real, but via the worker's own inline `adopt` path, not `reap()` — the TTL-based crash/preemption-recovery sweep itself is only now live-exercised. **H17's own review/18 §6 step 4 — converting in-Actions shards onto this same claim contract — is now tracked separately below as H19 since its trigger (a second/external worker class live) has fired; see that row, not here, for status.** **Records (`episodes.json`) stay on B2** — the [review/17 swing case](17-state-store-backend-evaluation.md) is **decided** *against* R2-CAS: per-uid lease ownership + the shipped Stage-1 owned-block merge make B2 race-free without CAS, and records migrate straight to a managed search-DB at Phase R (no B2→R2→DB double migration). So neither H14b/H14c nor the provider-transcript lanes need a record-store R2 migration. |
| **H18 timeline/audio integrity repair** | [GH#495](https://github.com/BashfulBits/city-meeting-podcasts/issues/495), feed-health timeline findings | L3 | **Implemented through PR5; PR6 auto-enable pending cohort verification.** Feed-health now emits a `timeline-audio-integrity` JSONL artifact that separates container-only drift from stream-sample/EDL mismatch. Records support an audit-owned `integrity.timeline_audio` repair block, `/admin/status` reports the queue, `SourceMedia.duration_basis` records stream-sample concat probes, source-aware identity detection fixes GH#495 tail-only trims in both hash invalidation and render-path selection, and the timeline/audio/transcript stages consume targeted repair actions without global pipeline-version bumps. A guarded manual feed-health dispatch can stamp a named over-threshold repair cohort and compare before/after telemetry; the scheduled audit remains non-mutating until the PR6 gate enables `--persist-timeline-integrity`. **Withheld/dead lifecycle ([GH#795](https://github.com/BashfulBits/city-meeting-podcasts/issues/795)):** withheld media is terminal for timeline-audio repair (no `rendered-duration-mismatch` for quarantined episodes), confirmed-dead media (`confirmed_empty`/`missing`/`invalid`) polls on a flat 30-day cadence (precedence over any repair flag, since the audit-owned integrity block can't be lane-cleared) instead of exponential backoff, and a repair flag bypasses only the exponential backoff for transient/broken-EDL episodes. **GH#702 PR6** (`silence:3` catalog re-trim, [#709](https://github.com/BashfulBits/city-meeting-podcasts/pull/709)) merged once parts 1–4 proved stable in production (zero `rendered-duration-mismatch` survivors, both stragglers self-resolved) — the single-file silence catalog now re-plans onto the stream-sample clock and drains under the existing stop budget. Design: [review/20](20-timeline-audio-integrity-repair.md). |
| **Unified storage reclaim + R2/B2 lifecycle backstop** | [GH#496](https://github.com/BashfulBits/city-meeting-podcasts/issues/496) (CR-SC-15) | L2 | **Implemented (unreleased).** The weekly `audio-gc` workflow became **"Storage reclaim"** and now runs three backstops on its existing cron. (1) **Lifecycle-as-code** (`scripts/apply_bucket_lifecycle.py`, `citypods/ops/reclaim.py`): idempotently expires the control-plane validator's R2 scratch prefixes (`work-leases/__validate__/`, `provider-leases/validate-`) after 1 day — the infrastructure fix CR-SC-15 asked for, since a killed runner can't run the validator's best-effort cleanup — and sets a bounded B2 version-retention window (`defaults.b2_retention_days`, default 30d, read back from the live bucket before change); a guardrail refuses any R2 rule broader than a scratch prefix. (2) **Double-confirmed auto-apply GC** (`gc_audio.py --auto-confirm`): a scheduled run auto-deletes only orphans seen unreferenced across ≥2 runs past `defaults.orphan_quarantine_days` (default 21d, ledger `state/orphan-ledger.json`; a reappearing key resets — GH#421 flip-flop guard); manual `apply=true` (main only) still deletes all. (3) **Resurrection watchdog** (`check_reclaim_resurrection.py`): every delete is appended to `state/reclaim-log.jsonl` with a `recover_by` deadline (age-pruned), and a live record re-referencing a still-restorable reaped key opens a `priority:high` issue. **Two distinct windows** by design — the pre-delete *quarantine* (safe-to-delete-yet?) vs the post-delete B2 *retention* (time-to-undo?). Also promotes **"R2 = ephemeral/derivable only"** to a test-enforced invariant (`routing.py` `_EPHEMERAL_R2_PREFIXES`): a coordination prefix not declared ephemeral fails at import + in tests, so a canonical/backup-less record can't reach R2 (which has no soft-delete and is aggressively expired). |
| **H19 in-Actions transcribe migration to the pull/claim contract** | [GH#831](https://github.com/BashfulBits/city-meeting-podcasts/issues/831) | L2→L3 | **Committed — trigger fired, not yet scoped to L3.** [review/18 §4.3](18-work-distribution-sharding.md#43-github-actions-shards-become-just-another-worker)/§6 step 4 already designed this: once a second worker class (the first external GPU worker) goes live, in-Actions transcribe shards convert from the Stage-1 static `--shard K/N` plan onto the same claim-loop contract H14b/H14c already use — GitHub Actions becomes "just another worker" instead of the privileged manifest-owner. **That trigger fired 2026-07-06** (H14b + H14c both live in production), but this follow-on step was only ever prose inside review/18, not its own tracked item, and H17/[GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390) — the umbrella it rode under — is now closed. Split out here so it isn't lost. **Concrete gap live-confirmed during [GH#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706)'s canary validation:** `state/work.json` (including `backlog_priority` ordering, e.g. `long_first`) is rebuilt only as a side effect of whichever in-Actions Stage-1 enrich lane (`audio.yml`/`asr.yml`) last completed — external pull workers only ever rotate-read it, never re-sort it — so manifest freshness is coupled to local push-worker cadence, not to anything an external worker controls (`asr-worker-report`'s `manifest_last_modified`/`transcript_asr_duration_band`, [GH#829](https://github.com/BashfulBits/city-meeting-podcasts/pull/829), now makes this directly observable). **Scope (review/18 §4.3/§6 step 4):** convert `asr.yml`'s matrix to `N` identical claim-loop jobs (no `--shard K/N`, no plan artifact, no fan-in); fold lease renewal + retry into `run_claim_loop` as optional hooks before reusing it as-is (budget-gating stays external-worker-only); shrink `reconcile` to discovery-index rebuild + lease reap. Audio lane stays source-atomic (out of scope, review/18 §2.3). Design: [review/18 §4.3, §6](18-work-distribution-sharding.md). |
| **H20 external work-lease stale-claim hardening** | new | L2 | **Committed.** H14d canaries and first live worker runs confirmed the Stage-2 R2 lease ledger works end-to-end, but also exposed one operational gap: a cancelled or preempted external worker can leave a stray `work-leases/<source>/<uid>.json` claim visible until TTL/reconcile, and today that state is awkward to inspect or clear when the operator is trying to distinguish "really in flight" from "abandoned." Scope: tighten stale-claim observability and recovery for external workers without weakening the lease safety model — e.g. lease age / renew-at telemetry in reports, an explicit reconcile/doctor surface for one owner or one item, and a review of whether the current 20h TTL is still justified once renewals are proven. This is control-plane hardening, not a routing-policy change; it follows H14/H17/H19. Breakout: to be added with the issue. |

### Phase R — Research-Tool Surface (toward 1.0)
| Item | #/GH | Maturity | Breakout |
|---|---|---|---|
| Per-meeting permalink pages | #46/GH#157 | L2→L3 | [`review/13`](13-per-meeting-pages-and-search.md) |
| Static transcript search | #6 | L2→L3 | [`review/13`](13-per-meeting-pages-and-search.md) |
| Topic tags / Strong Towns lens | #4 | L2→L3 | [`review/14`](14-topic-tags-strong-towns-lens.md) |
| Legistar calendar provider (historical Granicus coverage) | new | L2→L3 | [`review/15`](15-legistar-catalog-provider.md) |
| Per-agenda-item "what changed" cards | #3/GH#155 | L1 | §5.1 |
| Auto-summaries | #2 | L1 | §5.1 |
| Soundbite highlights | #15/GH#156 | L1 | §5.1 |
| Front-end design cycle | #55 (#20/#54) | L1 | §5.1 |
| Accessibility (WCAG) | #50 | L1 | §5.1 |
| `<podcast:funding>` link | #16 | L1 | §5.1 |
| Speaker diarization | #7 | L1 | §5.1 — after H6b; runs on the execution backend (H9 / §5.5), preferably sharing an external episode worker with ASR while remaining an independently versioned/publishable enrichment |
| Records → managed SQL (federated query / query API / state integrity) | new (Infra) | L1 | [`review/17`](17-state-store-backend-evaluation.md) — promoted from Deferred; D1/Turso kept open; trigger-gated |
| Durable provider-failure classification feed | new (absorbs closed GH#379) | L1 | §5.1 · append-friendly episode/source failure events with stable categories, terminal-vs-transient state, first/last seen, and references to existing provider telemetry; `/admin/status` is the first reader, with Phase-R query surfaces consuming it later |
| Runtime/dependency maintenance automation | umbrella [GH#804](https://github.com/BashfulBits/city-meeting-podcasts/issues/804) | L2→L3 | **Final Phase-R release item; completing Phase R is the 1.0 gate. Foundation shipped** ([#805](https://github.com/BashfulBits/city-meeting-podcasts/pull/805), [#806](https://github.com/BashfulBits/city-meeting-podcasts/pull/806), [#807](https://github.com/BashfulBits/city-meeting-podcasts/pull/807)) — normative contract in [`review/22`](22-dependency-and-reproducibility-policy.md): compiled **version-pinned** Python `constraints/*.txt` (single source of truth for CI, the GHCR runner image, **and** the Modal/Beam worker images) with `lock.yml` + a `ci.yml` drift gate; all third-party Actions SHA-pinned ([GH#734](https://github.com/BashfulBits/city-meeting-podcasts/issues/734), closed); HF model revisions pinned ([GH#498](https://github.com/BashfulBits/city-meeting-podcasts/issues/498), closed) and shared canonically in `citypods.asr`; `.github/renovate.json5` two-lane flow (hygiene auto-PRs; a Dashboard-approval gate + **per-source** `dep-bump-smoke` for output-affecting bumps); `scripts/check_dependency_policy.py` CI guard keeps pins from rotting. **Remaining to close Phase R:** activate Renovate on the repo, the monthly immutable-URL/checksum FFmpeg update PR, and (optional hardening) hash-verified `--require-hashes` image installs. Weekly image builds verify pinned inputs but do not silently advance them. Absorbs the remaining useful scope of closed GH#339. |

### Phase E — Engagement & Distribution (post-1.0) · sketches §5.2
| Item | #/GH | Maturity |
|---|---|---|
| Site-news RSS + static digest pages | new (Feature A) | L1 |
| Weekly look-back digest | new (Feature A) | L1 |
| "National highlights" curated reel | new (Feature A) | L1 |
| Substack newsletter channel | #18 (email split) | L1 |
| Topic/issue + region roll-up feeds | #12/#13 | L1 |
| Custom-query feed builder | #12+#13 | L1 |
| OPML export | #17 | L1 |
| Privacy-respecting download analytics | GH#125 | L1 |

### Phase F — Pre-Meeting Foresight (post-1.0) · sketches §5.3
| Item | #/GH | Maturity |
|---|---|---|
| Upcoming-agenda + staff-report scraping | new (Feature B) | L1 |
| Upcoming-meetings `.ics` calendar | #19 | L1 |
| Watchlists + topic alerts | new (R8 extension) | L1 |
| Backup-material (packet) analysis | new (Feature B) | L1 |
| Legistar provider (rich agendas/votes/rosters — InSite API) | #31 | L1 |
| Vote/roll-call extraction (metadata + minutes) | #8 | L1 |
| Attendee extraction (from minutes) | #14 | L1 |

### Phase C — Co-Creation & AI Audio (furthest horizon) · sketches §5.4
| Item | #/GH | Maturity |
|---|---|---|
| Look-ahead / look-back outlines & drafts | new (Feature C) | L1 |
| AI-generated discussion podcast w/ meeting audio | new (Feature C) | L1 |

### Cross-cutting / ongoing · sketches §5.5
| Item | #/GH | Maturity |
|---|---|---|
| Strong Towns-focused city discovery | #27/#32 (rescoped) | L1 |
| City-request issue + `/approve` onboarding | #28 | L1 |
| User "report a feed problem" template | #56 | L1 |
| Auto-detect provider from a city URL | #30 | L1 |
| Contributor scaffolding (labels, PR template, board) | #57 | L1 (partial: handoff docs shipped) |
| Pluggable inference-execution backend (compute offload) | new (Infra) | L3 · **pre-1.0 lock** — GPU/ASR interface = H13; Modal+Beam GPU adapters = H14 (built in Phase H); first LLM API adapter = R3/R4 |
| Catalog scaling readiness (10→500 cities) | new (Infra) | L2 · **trigger-gated, not active-phase work** — [`review/16`](16-scaling-review-plan.md); R2 owns the search-size spike/partitioned-search launch, while S0–S4 promote one tranche at a time only when their city/metric gates are reached |
| State-store backend (coordination → R2/CAS · records per-artifact · SQL at Phase R) | new (Infra) | L3 · active control-plane implementation promoted to **H17**; [`review/17`](17-state-store-backend-evaluation.md). Phase-R managed SQL remains trigger-gated |
| Work distribution & sharding for distributed ASR workers | new (Infra) | L2 · Stage 1/Stage 2 substrate shipped as **H17** (closed); the remaining §6 step 4 in-Actions migration is now tracked as **H19** (its trigger fired — H14b/H14c are live); [`review/18`](18-work-distribution-sharding.md) |

### Deferred backlog (ongoing) — §6
#9 translation · #24 bitrate ladders · #25 intro/outro stinger (GH#153) · #26 chapter
images · #34 config-via-issue-comments · #40 B2 actual-cost dashboard · #42 **directory** index sharding
(review/02 Change 6; trigger-gated by `review/16` client budgets, distinct from R2 transcript-search
partitioning) · #44 structured logging · #47 map browser · #48 new-since-visit · #10 agenda-packet chapter
descriptions · #14 `podcast:person/location` tags · #33 dead-city archival · review/02 Change 5
DerivedArtifact refactor · review/04 B3 stale-record bucket leak · review/04 R4 per-host rate-limit (#39)
· admin dashboard extension (#49) · full video re-hosting · hosted DB/API (Phase-R records→SQL split out
& **promoted to Phase R** — [`review/17`](17-state-store-backend-evaluation.md)) · off-Actions media ·
coordination → dedicated KV/DO fallback (trigger-gated — [`review/17`](17-state-store-backend-evaluation.md) §8)
· 98 open code-quality/security findings from the full-repo CodeRabbit audit, triaged for fix-over-time
— [`review/19`](19-coderabbit-findings-audit.md) · per-segment source caching for multi-source concat
episodes (new, 2026-06-25 — see §5.5) · 17 findings from a manual follow-up audit (CodeRabbit CLI
unavailable in-session), triaged for fix-over-time, incl. an unguarded SSRF gap in the legacy Swagit
concat duration-probe and two more presigned-URL-into-GitHub-issue leaks beyond CR-CP-03 —
[`review/21`](21-manual-code-audit-2026-07.md) · 100 open findings from a second full-repo CodeRabbit
CLI sweep (2026-07-04), triaged for fix-over-time and cross-linked against both prior audits, incl.
two still-unmitigated **critical** GitHub Actions bugs (a `UID`-named env var shadowed by bash's own
readonly builtin, breaking `reset-backoff.yml`'s `--uid` filter; unsanitized `${{ inputs.run_id }}`
shell interpolation in `clear-materialization.yml`) and a real gap in the core SSRF blocklist (RFC
6598 shared address space not blocked) — [`review/23`](23-coderabbit-findings-audit-followup.md) ·
external-worker billing calibration + host-RSS trimming (new, 2026-07-08) — H14d showed recent Modal
and Beam ASR runs are dominated by GPU cost and are only a small CPU/RAM cost adder on the free tier, so
this stays deferred for now; follow-up scope is to (1) replace the current repo-month generic-unit ledger
with a **provider-cycle dollar ledger** keyed to each backend's real rollover date, (2) reserve work
using YAML-configured per-task/backend cost coefficients but settle the ledger against a closer-to-actual
provider signal — **Beam** via YAML-driven dollars-per-second rates times the task API runtime duration,
**Modal** via exported actual dollar cost with no rate conversion in-ledger — while persisting an
estimated runtime model per task/backend and updating it after every completion from estimate vs actual
GPU runtime, and (3) revisit the host-memory guardband once
download/decode buffering is measured well enough to safely tighten it (likely from the current 80%
toward 90%). 
**Deleted:** #5 entities/NER.

---

## §5. L1 sketches (initiatives not yet broken out)

Each: problem + 1–3 candidate approaches + rough tradeoffs. Promote to a breakout (L2) when chosen.

### §5.1 Phase R remainder

**Per-agenda-item "what changed" cards (#3/GH#155).** *Problem:* a freeform meeting summary is risky and
low-trust; residents want "what did they decide on item 7?" *Approaches:* (1) **extractive** — join
chapter/agenda-item boundaries (already have chapters) with the transcript span + official
minutes/vote metadata into a structured card (title, action, vote, excerpt+timestamp, doc link), no LLM;
(2) **LLM-assisted** — same skeleton, LLM drafts a one-line "what changed" per item from the transcript
span, clearly labeled + cached. *Tradeoff:* (1) is auditable and ~$0 but only as rich as the source
metadata; (2) adds cost + the untrusted-output rule. **Lean: (1) first, (2) additive.** Depends on tags
(#4) for topic chips. Auditable structure precedes any freeform summary (#2).

**Auto-summaries (#2).** *Problem:* a short "what happened" blurb in notes/pages. *Approaches:* (1)
agenda-derived templated blurb ($0); (2) LLM 3–5 sentence summary from transcript (sub-cent–3¢/meeting,
cached). *Tradeoff:* cost-gated (<$20/mo near-term); never overwrites official text. Build **after** #3
cards so summaries cite structured items.

**Soundbite highlights (#15/GH#156).** *Problem:* surface a shareable ~60s clip per meeting. *Approach:*
consume the existing EDL via `clips.extract_clip` (forward-map a served range → source cuts); selection
is (1) manual/config, (2) heuristic (longest public-comment turn / agenda-item peak), or (3) LLM-picked
from the transcript. *Tradeoff:* infra exists; only selection is new. Powers `<podcast:soundbite>` +
the highlights reel (Phase E).

**Front-end design cycle (#55, absorbs #20/#54).** *Problem:* index accordion polish, subscribe-button
**app iconography**, clear **audio-vs-video** labeling. *Approach:* iterative mockup-driven redesign;
coordinate with per-meeting pages (R1). 1.0-gating. *Tradeoff:* design effort, low risk.

**Accessibility (#50).** WCAG pass on generated pages (player labels, contrast, keyboard nav, transcript
semantics). 1.0-gating. Pairs with R1 pages.

**`<podcast:funding>` link (#16).** Trivial feed tag + config; funds the rest. Near-$0 do-now.

**Durable provider-failure classification feed (closed GH#379).** *Problem:* provider health beyond
`catching-up`/`stalled` currently requires log archaeology. *Approach:* persist append-friendly
episode-level failure events (provider, tenant/source, stable media identity, extensible category,
terminal/transient, first/last seen, strategy/hostname and redacted evidence pointer), then derive a
source-level current-state rollup rather than duplicating health rules. Existing Granicus circuit/proxy,
Swagit terminal-media, parser-drift, and run-history telemetry remain authoritative inputs. H16 PR3
narrowly promotes the current per-episode media-availability projection, re-evaluation contract,
redacted diagnostic evidence, and weekly review digest because feed safety needs them before Phase R.
Phase R still owns archive/page presentation, availability filters, `/admin/status` review/override
surfaces, history browsing, and the later query API. Keep the event/history schema
storage-adapter-neutral so review/17's SQL path can index it later.

**Speaker diarization (#7).** *Problem:* transcripts don't identify who's speaking — council members,
staff, and public commenters look identical. *Approach:* run a speaker-embedding diarization model over
the audio after transcription, align speaker-change boundaries to the word-level VTT cues (now emitted
by the ASR stage), and enrich the canonical word/segment artifact with reconciled speaker assignments;
do not regenerate or discard successful transcript text merely because diarization fails. For long
audio, overlapping diarization windows must reconcile identities across the whole meeting through
speaker embeddings plus meeting-wide clustering/identity reconciliation — independently numbered
per-chunk labels cannot be concatenated. Two CPU-viable backends:
(a) **wespeaker ECAPA-TDNN** (~100 MB, no HF gate, ~2× transcription cost on CPU); (b) **speechbrain
ECAPA-TDNN** via simple-diarizer (~300 MB, similarly lightweight). A free/low-cost GPU API
(H9 evaluation) cuts diarization cost further — pyannote v3 on GPU is fast and accurate but gated;
the CPU-only path uses the lighter backends to stay within the Actions runner budget. It runs on the
**execution backend** (§5.5) — the same interface as transcription — so the diarization model can target a
GPU backend (Modal/Kaggle/self-hosted/AWS) without changing the diarization logic; this is exactly the
infra the maintainer wants **locked pre-1.0**. *Depends on:* word timing — H12 moves it into the
word-JSON sidecar, which diarization consumes (built on PR #249's `word_timestamps`); H6b sharded ASR
workflow (dedicated runner/lane for heavy inference); H9 offload evaluation (cost/quality baseline against
the backend interface). **New H14d note:** do not reuse ASR's current budget coefficients or admission
thresholds blindly here; diarize needs its own measured GPU/host-memory profile, its own budget-unit
coefficient, and likely a stricter host-RSS guard than VRAM guard because the first external ASR runs were
host-memory-heavier than GPU-memory-heavy. *Sequencing:* implement after H6b lands a separate ASR runner — do not add
diarization to the current single-runner enrich path. When both products are requested externally,
prefer one episode worker flow that shares download, decode/resample, normalized temporary audio,
VAD/chunk planning, startup, and final timestamp/artifact coordination. ASR and diarization normally
use different neural models, so the expected gain is operational reuse rather than shared model
computation. This records the integration direction without promoting diarization out of Phase R.

### §5.2 Phase E — Engagement & Distribution

**Site-news RSS + static digest pages.** *Problem:* let people subscribe to the *project*, not just one
city. *Approach:* a generated `docs/news/` with static digest pages + a dedicated `news.xml` RSS;
content is auto-generated outlines (feature announcements + cross-city highlights). *Tradeoff:* RSS-first
= no PII/infra (fits the static model). Foundation for the newsletter.

**Weekly look-back digest.** *Problem:* "what happened in your city's meetings this week." *Approach:*
aggregate recent records (titles, #3 cards, tags, soundbites) per city/region into a static page + RSS
item; optionally per-topic. *Tradeoff:* extends #3/#4; the bridge to Feature C look-back outlines.

**"National highlights" curated reel.** *Problem:* make land-use/fiscal processes legible to a wide
audience (good public comment, good council members, transparently-quoted opposition). *Approach:*
lightly-curated, **quote/clip-driven** items drawn from soundbites + transcript spans across cities,
each linking the source meeting/timestamp; raw clips downloadable. *Tradeoff (voice):* lightly curated,
sourced — **expose the raw material** so groups take it further; never editorialize the factual record;
moderation/defamation care on anything characterizing named individuals.

**Substack newsletter channel (#18 email split).** *Problem:* reach beyond RSS. *Approach:* publish the
digests/highlights to **Substack** (external — avoids native email/PII/CAN-SPAM infra near-term); the
static digest is the source content. *Tradeoff:* some lock-in vs fast reach; keep RSS as the open mirror.

**Topic/region roll-up feeds + custom-query builder (#12/#13/#17).** *Problem:* "all zoning items in
TX," "my whole city." *Approach:* pre-generated combos (region/state/topic) as static feeds + **OPML**
export; a custom-query builder needs either pre-gen combinations or a Cloudflare Worker (Pages is
static). *Tradeoff:* combinatorial explosion → start with a curated set; depends on tags (#4).

**Privacy-respecting download analytics (GH#125).** *Approach:* OP3-style aggregate, self-owned
analytics-prefix subdomain; no per-user tracking. Informs which feeds/cities to invest in.

### §5.3 Phase F — Pre-Meeting Foresight

**Upcoming-agenda + staff-report scraping.** *Problem:* shift from after-the-fact to ahead-of-time.
*Approach:* extend providers with an `upcoming`/agenda capability (review/02 Change 8 capability set
already exists): Legistar/CivicClerk expose structured upcoming events + published files; Granicus/Swagit
need agenda-portal scraping. Persist as forward-looking records. *Tradeoff:* provider-dependent coverage;
strongest with Legistar (#31). The data spine for alerts + look-ahead.

**Upcoming-meetings `.ics` calendar (#19).** Generate per-city/body `.ics` from upcoming events; reuses
the fetch above. Static, low-cost.

**Watchlists + topic alerts (R8 extension).** *Problem:* "tell me when parking minimums hit an agenda."
*Approach:* match upcoming agenda items against topic tags (#4) → per-topic RSS first (no accounts),
email/Substack later. *Tradeoff:* RSS-first keeps it PII-free; precision depends on tag quality.

**Backup-material (packet) analysis.** *Problem:* the real decision detail is in staff reports/packets.
*Approach:* fetch the published packet (provider capability), extract text (PDF), and produce a
structured/LLM "what's being proposed" brief — cost-gated, cached, **additive + labeled**. *Tradeoff:*
PDF parsing + LLM cost; never overwrites official docs.

**Legistar provider (#31) — InSite API.** Rich structured data: agendas, votes, rosters, upcoming
events. Unlocks #8/#14 and high-quality foresight. *Approach:* standard new-adapter work + SSRF
allowlist + fixtures. *Distinct from Phase R's calendar provider* ([`review/15`](15-legistar-catalog-provider.md)),
which scrapes `Calendar.aspx` solely to extend Granicus video coverage past the RSS view-cap; the
InSite API adapter and the calendar scraper are independent and can coexist.

**Vote/roll-call (#8) + attendee (#14) extraction.** From **platform metadata** (CivicClerk per-member
tallies) + scraped **released minutes** — **never inferred from audio**. Shared "minutes-ingestion"
component. ~$0 (parser).

### §5.4 Phase C — Co-Creation & AI Audio

**Look-ahead / look-back outlines & drafts.** *Problem:* lower the bar for residents/groups to publish.
*Approach:* generate a structured outline + draft (article or podcast script) from look-ahead foresight
(Phase F) and look-back digests (Phase E), with citations to meetings/timestamps; offered as
downloadable starting material. *Tradeoff:* LLM cost; drafts are clearly "starting points," sourced.

**AI-generated discussion podcast.** *Problem:* a listenable weekly show. *Approach:* compose an LLM
host script from the outline, interleave **real meeting audio clips** (via `clips.extract_clip` + EDL)
and TTS narration, render to an episode. *Tradeoff:* the most expensive + quality-sensitive item;
gated on revenue; the untrusted-output rule applies to all generated narration. The differentiator if it
works.

### §5.5 Cross-cutting / ongoing

**Catalog scaling readiness (10→500 cities; [`review/16`](16-scaling-review-plan.md)).** *Problem:* the
existing architecture can support hundreds of cities, but provider polling, full-state restoration,
fixed shard schedules, repeated source-media downloads, and static-index rebuilds can amplify well
before city 500. *Disposition:* this is an **L2 trigger-gated program**, not a new current phase and not
a requirement to implement all ten proposed PRs now. Reuse H2/H4 telemetry, H5 durable work, H6b
source/lane sharding, H11b render isolation, H13/H14 compute offload, provider leases, and
content-addressed artifacts first.

Promotion ladder:

| Gate | Canonical action |
|---|---|
| **Current Phase R** | R2 runs the search-size spike and launches transcript search partitioned by city/source with lazy loading and byte/memory budgets. Keep per-PR artifact previews and preserve the production `github-pages` environment's verified `main`-only policy; this is release hardening, not a separate scaling tranche. |
| **Before systematic breadth onboarding (~25 cities)** | Promote **S0 measurement** only: request/byte/useful-time telemetry, current call graph, and synthetic 10/100/500-city harness. Update `ROADMAP.md`, mature that tranche in `review/16` to L3, and cut issues at promotion time. |
| **Before ~50 cities or redundant provider-call trigger** | Promote **S1 refresh separation**: one due/conditional provider refresh path; audio/ASR/render consume persisted records and durable work. |
| **Before ~100 cities or state/empty-job trigger** | Promote **S2 targeted state + demand planner**: root manifest, shard-specific reads, dirty writes, zero/variable worker matrix. |
| **Before ~250 cities or repeated-transfer/search trigger** | Promote **S3 selective source cache + bounded adaptive controls + dirty render/search partitions**. Cache only demonstrated high-value providers/failure classes. |
| **Before 500 cities** | Promote **S4 rehearsal/readiness gate** and update the practical Actions ceiling from measurements. |
| **~1,000 cities or two sustained migration signals** | Begin the off-Actions scheduler/media-worker adapter; expected crossover remains ~1,500–2,500 cities. This stays deferred until triggered. |

Metric gates override city guideposts: any artifact lane redundantly polling provider lists; shard state
downloads >2× assigned bytes or broad hot-path listings; empty heavy jobs >5% or useful-work ratio
<80%; repeated media downloads >10% of provider bytes; city search partitions above the 1 MB target
(2 MB hard warning); or the sustained multi-signal migration gate in `review/16` §14.1. Crossing a
round-number city count without the corresponding pressure does not force promotion.

**Production/staging gate.** A live staging URL is also user-risk-triggered rather than city-triggered.
The current beta keeps `preview.yml`'s read-only downloadable PR artifact plus the sole production Pages
deploy. Before 1.0/public launch, activate a shared render-only staging site only when meaningful users,
risky URL/feed/frontend changes, multiple releasers, or demonstrated rollback pain make artifact review
insufficient. Because GitHub Actions environments do not create a second Pages site and public Pages PR
previews are unavailable, the Pages-native design is a separate staging repository/site rendering an
exact source SHA against production records read-only. Do not duplicate audio/ASR/provider polling; add
an isolated small canary bucket only for future state/artifact write-path migrations. Full design and
acceptance criteria: `review/16` §9.5.

### Granicus media reliability follow-up (#300/#39)

*Problem:* after PR #316 made endpoint issues more descriptive, endpoint issue #300 still reproduces on
GitHub-hosted runners as `RateLimitedMediaFetchError('ffmpeg source-cache hit provider throttle (HTTP
403)')` for Arlington's Granicus `media-fetch`. The representative RSS, media-resolution, chapters,
view-count, and deeplink checks pass; the failure is the ffmpeg byte fetch from
`archive-video.granicus.com`. A local serial contracts run on 2026-06-16 passed (`51943B from first 3s`),
and the failed workflow-dispatch contracts run overlapped an active sharded `audio.yml` run. So the
leading hypothesis is **aggregate GitHub-runner Granicus concurrency**, not a dead meeting URL or a
bad provider parser.

*Recommended sequence:*

1. **Reduce the actual disease first: aggregate Granicus media concurrency.** Lower
   `provider_distributed_leases.granicus.com.slots` from 6 toward 2, and consider lowering the
   process-local `provider_rate_limits.granicus.com` cap from 2 to 1. Watch Audio throughput/backlog
   against endpoint #300 closure before tuning back upward.
2. **Put endpoint contracts inside the same coordination envelope.** `contracts.yml` currently runs in
   its own concurrency group and calls the same low-level ffmpeg helper, but it does not configure or
   acquire the Audio lane's storage-backed provider leases. Either wire contracts into the shared
   Granicus lease pool for `media-fetch`, or skip/defer Granicus `media-fetch` while an Audio run is
   active. This prevents the monitor itself from becoming the extra uncoordinated fetch that trips CDN
   throttling.
3. **Only if low/no-overlap Actions-runner fetches still 403, test request-shape alternatives.** Candidate
   experiments: add Granicus-specific `Referer`/`Origin` headers to ffmpeg, let ffmpeg fetch
   `DownloadFile.php` directly rather than the pre-followed `archive-video` URL, discover a playback/HLS
   URL that Granicus serves more consistently to Actions, or move Granicus media fetching off
   GitHub-hosted runners.

*Tradeoff:* lower concurrency slows the Granicus audio backfill but attacks the likely cause. Request-shape
changes are higher risk because prior fixes misdiagnosed Granicus media 403s as signing/URL issues; keep
those experiments behind live endpoint checks and only after the low-concurrency/no-overlap case still
fails.

**Implemented (2026-06-20): step 3 via authenticated alternate egress.** Steps 1–2 did not close #300 —
the paired transport artifact returned 403 for all 12 GitHub direct cases while the same objects
succeeded from a Mac, isolating the cause to GitHub-runner egress reputation rather than concurrency or
URL shape. Step 3 is therefore taken as a closed, tenant-allowlisted Cloudflare **Worker fallback**:
production ffmpeg tries the canonical archive object directly first and, only on an immediate HTTP 403,
retries once through the authenticated Worker inside the same 1-local / 2-distributed lease and circuit
admission. Per-tenant `worker_fallback_attempts`/`successes`/`failures` counters on the circuit make
activation measurable; a misconfigured `GRANICUS_PROXY_*` pair disables the fallback safely; the bearer
header and Worker endpoint stay out of logs. The pre-merge gate (one full production-recipe encode), the
three-run post-activation acceptance criteria, the data-driven sticky-Worker/circuit-simplification
decision, and config-only rollback are specified in
[review/12 §Granicus follow-up](12-hardening-and-efficiency.md#granicus-media-reliability-follow-up-gh300--39-follow-up).

**Audio runner setup hardening (implemented alongside this follow-up).** Audio #33 showed Ubuntu mirror
fetches ranging from 4 minutes to a shard that remained in `apt-get update` for more than 4.5 hours.
`audio.yml` therefore uses a version-pinned GHCR runtime built from a digest-pinned Python base and a
checksum-pinned static ffmpeg bundle. The image is selected at step time so a failed registry pull can
fall back to the same verified static bundle on the host; a job-level container was rejected because
GitHub fails the job before steps run, making fallback impossible. The image build is scheduled weekly,
also runs on runtime-definition changes, and smoke-tests Python, ffmpeg, ffprobe, boto3, and citypods.
ASR uses the same verified static ffmpeg cache on the host, eliminating its observed 1–40 minute
`apt-get` setup variance without embedding the multi-gigabyte Whisper model. Whisper remains in the
existing Actions-cache/Hugging Face/B2 cascade, where cache hits prepare in seconds and model/runtime
versions can evolve independently.

### Per-segment source caching for multi-source concat episodes (Shipped 2026-06-26)

*Problem:* Audio run #70 showed that multi-segment Swagit/Granicus concat episodes (`ep.sources` with
`len > 1`, owned by the `SwagitConcatPlanner` / #122) bypass `SourceCache` entirely — `SilencePlanner.plan()`
(`silence.py:288`) and `AudioStage`'s encode worker (`media.py:2265`) both explicitly skip the cache for
these episodes "by design" (the concat planner owns stable `.ref` URLs, not `resolve_media_url`),
streaming every segment directly from the provider through one `filter_complex` ffmpeg invocation with
dozens of `-i` inputs. Two related production symptoms surfaced on a 41-segment 2013-archive Granicus
concat in that run: (1) the per-input `-rw_timeout=120s` did not reliably bound a stalled segment buried
inside the multi-input filter graph — only the monolithic 45-minute `audio_encode_timeout_minutes`
Python-side backstop eventually killed it (`audio encode error ... timed out after 2699.999943974
seconds`), after ~36 minutes at 0% CPU; (2) `HOST_LIMITER`'s per-host concurrency slot (`media.py:369-374`,
issue #39) is held for the *entire* subprocess duration, so one slow concat job pins a scarce
Granicus/Swagit concurrency slot for 30+ minutes even though only one segment is transferring at any
instant — starving other shards/episodes that share the same provider cap.

*Candidate approaches:*
1. **Per-segment download via `SourceCache`** — fetch each `SourceMedia.ref` individually through the
   existing `_download_audio`/`get_or_fetch` path (already proven to enforce `-rw_timeout` reliably for
   single-source episodes) before composing the `filter_complex` graph. This releases the rate-limiter
   slot between segments and gives each segment its own ~120s timeout instead of riding the
   episode-wide 45-minute backstop — turning "one bad segment kills the whole 41-segment episode" into
   "one bad segment fails/retries in isolation."
2. **Persistent cross-run segment cache** — same mechanism as (1), but keyed so a retried concat episode
   doesn't re-stream segments that already downloaded cleanly in a prior failed attempt. Most valuable
   for archival concat episodes with dozens of segments, where today one bad segment forces a full
   re-fetch of all of them on the next run.
3. **Bound the pathological case instead of caching it** — short-probe each segment (a few seconds)
   before committing to the full concat measure pass, so segments from clearly degraded archival
   sources fail fast rather than consuming the full 45-minute backstop. Complementary to (1)/(2), not a
   substitute — it shortens the failure but doesn't avoid repeated full re-streams on retry.

*Tradeoff:* (1)/(2) lift the "concat planner owns these episodes" boundary that `silence.py:288` /
`media.py:2265` currently draw deliberately, and add per-segment disk/bookkeeping (N temp files instead
of one streamed graph) — real but contained scope. (3) is cheaper but only shortens the failure.
Related to the Granicus media reliability follow-up above and to `review/16`'s **S3 selective source
cache** scaling item (line "Promote S3 selective source cache..." in the promotion ladder) — this is a
concrete, run-evidenced instance of that broader item, narrowly scoped to the multi-segment concat case
rather than general per-provider caching.

**Shipped:** a variant of (1), going one step further than per-segment caching alone —
`SourceCache.get_or_fetch_concat` (`media.py`) downloads each segment individually (own bounded
timeout, releases the rate-limiter slot between segments, exactly as (1) proposed), then
*concatenates them once into a single local file* and renders that file as a single source instead
of still feeding N inputs into one `filter_complex` on every encode attempt. `ep.timeline`/`ep.sources`
on the persisted record are untouched (clips/soundbites still resolve through the real per-segment
EDL); only the render-time input to the encoder changes (`_concat_render_timeline`). (3) (fast-fail
short probe) and (2) (persistent cross-run segment cache) remain open follow-ups — (2) only if repeated
full concat re-fetches prove to be a real recurring cost now that (1)/Shipped has landed.

**Strong Towns-focused discovery (#27/#32, rescoped).** *Problem:* grow toward where it helps most.
*Approach:* seed discovery from cities with active **Strong Towns Local Conversations**
(<https://www.strongtowns.org/local>), prevalidate against round-one provider traps (review/02 Change 9
security gate already exists), rank by group activity + archive quality — not population. *Tradeoff:*
list maintenance; aligns the catalog with the mission. Pairs with onboarding (#28).

**City-request + `/approve` onboarding (#28) · "report a feed problem" (#56) · auto-detect provider
(#30) · contributor scaffolding (#57).** The human-in-the-loop onboarding/health loop is
sketched here; issue templates + PR template **shipped**, label taxonomy (`area:*`, `needs-*`)
**shipped**, Projects board lands at 1.0.
Handoff docs (AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING) **shipped** with this doc set.
When the city-request/import form is revisited for Phase R, include a branding-discovery pass:
evaluate alternatives to the current `fetch_seals.py` representative-city/favicon fallback for city
colors, seals, and logos (for example official site metadata, OpenGraph icons, municipal brand pages,
Wikipedia/Commons, and operator-provided overrides), while keeping the committed entity config as the
source of truth for published branding.

**Pluggable inference-execution backend (compute offload).** *Problem:* heavy inference — transcription,
forced alignment, diarization, and (Phase R) text inference for summaries, topic tags, and soundbite
selection — is the project's main compute cost and the first thing to outgrow the free 4-core GitHub
Actions runner. We do **not** want to rearchitect the pipeline each time the compute home changes as the
catalog scales. The backend design must be **provider-agnostic at two levels**:

- **GPU/process backends** (ASR, diarization, TTS): `local` (current — faster-whisper/stable-ts
  in-process on the runner), `modal` (serverless GPU), `kaggle`/`colab`/`hf-spaces` (free notebook
  compute), `self-hosted` (M4/M5 Mac mini, MPS/CoreML), `aws` (Batch/EC2/SageMaker)
- **LLM API backends** (text inference — summaries, tags, soundbite selection): `anthropic` (Claude
  Haiku 4.5 / Sonnet via Batch API + prompt caching), `openai` (GPT family), `deepseek`, `gemini`
  (Google), `together` (open models — Llama, Mistral, Qwen, etc. via Together AI API); open weights
  self-hosted on a GPU backend are also a peer option

*Approach:* define one **execution-backend interface**, mirroring the pluggable storage backend in
`storage/` (same `runtime_checkable` Protocol pattern as `StorageBackend`): a small protocol
`run_inference(job) -> artifact` where a `job` specifies:

- `task` — one of the GPU/ASR verbs (`transcribe` / `align` / `diarize`) **or** the text-inference
  verbs (`summarize` / `tag` / `soundbite-select`)
- `inputs` — audio ref + optional source text for ASR tasks; transcript text + meeting metadata for LLM
  tasks
- `recipe_hash` — for ASR tasks: audio-content hash + model config (existing); **for LLM tasks: must
  include `prompt_hash` + `model_id`** so any prompt revision or model swap triggers re-derivation of the
  artifact (version-aware re-tagging), consistent with the project's content-addressed artifact strategy

The backend returns the content-addressed artifact — VTT / word-JSON for ASR; structured JSON for LLM
tasks (tag list, summary blob, soundbite timecodes). The callers (`TranscriptStage`, a future `TagStage`
/ `SummarizeStage`, a future `DiarizeStage`) stay backend-agnostic; backend selection is config/env.
The pre-1.0 GPU/process protocol remains episode-level: the public verbs stay `transcribe`, `align`,
and `diarize`. A worker may co-lease transcript and diarization work and internally combine preparation
or chunk execution without adding a public combined verb or exposing chunk boundaries in artifact
semantics. Chunk-level durable scheduling is a later measured optimization, not part of the lock.

**LLM adapter cost note.** A hosted LLM API adapter running Haiku 4.5 via the Batch API + prompt
caching costs ~2–3.5¢/meeting for tagging + summarization — well under the <$20/mo cost gate. This is
the correct allocation: LLM text tasks should use a hosted API adapter, **not** GPU credits. GPU credits
(Modal/Beam free tiers, $30/mo each) are better reserved for the genuinely GPU-bound ASR and diarization
work (H6b, H9, diarization #7). Folding `prompt_hash` + `model_id` into the recipe hash means switching
providers or refining a prompt automatically re-runs only stale artifacts without touching pipeline
logic.

**H5 manifest integration.** H5's artifact-keyed work manifest already enforces the
`audio → transcribe → diarize` DAG and explicitly reserves downstream work-class slots. The LLM
text-inference work-classes (`summary`, `tags`, `soundbite-select`) extend this DAG naturally: they gate
on `transcript: done` and consume `diarization` opportunistically (a speaker-labeled transcript improves
tag and summary quality). No manifest migration is required — H5's `buckets` reservation and
`within_days` windowing were designed with exactly this extension in mind.

**First consumers.** R3 (topic tags / Strong Towns lens, [`review/14`](14-topic-tags-strong-towns-lens.md))
and R4 (auto-summaries + soundbite selection, §5.1) are the first callers of the `tag` / `summarize` /
`soundbite-select` task verbs. H9 (free transcription-offload evaluation) is the first exercise of the
GPU backend path.

*Tradeoff:* the interface is real design work and each non-`local` / non-API backend adds a secrets/ToS
surface. But **only the `local` backend and one LLM API adapter need to exist at 1.0**; every later swap
is a single adapter. The untrusted-output rule — all LLM outputs labeled, cached, and never overwriting
the official record ([SECURITY.md](../SECURITY.md)) — applies to every LLM adapter regardless of
provider.

**The interface design — in its widened form, covering both GPU/ASR backends and provider-agnostic LLM
API backends — is the pre-1.0 lock.** The compute + inference architecture must be settled before 1.0
so post-1.0 scaling (new providers, new model tiers, new task verbs) is always adapter-only and never
touches pipeline logic. *Sequencing (revised 2026-06-12):* the GPU/ASR interface + `local` adapter ship
as **H13** (do-first — the pre-1.0 lock). The maintainer then pulled the first non-`local` GPU backends
into Phase H: **H14** builds **Modal + Beam** as free-tier-bounded async-dispatch transcription backends
(so the lock is proven by two live adapters before 1.0), dispatched from the `asr.yml` workflow; **H9**
measures the combined local-sharded + Modal + Beam throughput. The first **LLM API adapter** (evaluate
Anthropic, Deepseek, Gemini, OpenAI, Together) lands with **R3/R4**, its first consumers. Self-hosted
Mac-mini + AWS GPU backends stay post-1.0 (no hardware yet) — each is then a single adapter against the
same interface.

---

## §6. Deferred backlog (ongoing)

Items intentionally not in a near-term phase; revisit as scale or demand warrants. (Enumerated in §4
"Deferred backlog".) Notable rationale: **directory index sharding (#42)** remains deferred because
per-meeting pages make meetings independently crawlable; promote it only if `review/16`'s directory
payload budget is crossed. This is distinct from R2 transcript-search partitioning, which is part of
the launch design in `review/13`. The **DerivedArtifact refactor** (review/02 Change 5) is now
**justified** — H12 (shipped, [PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253))
added the third derived-artifact type (audio M4A · transcript VTT · **word-JSON**), the YAGNI trigger it
was waiting on — so it moves from deferred to "do opportunistically now that H12's storage
plumbing lands"; **full video / off-Actions media** are explicitly out of scope now (§8); **hosted DB**
stays deferred *except* the scoped **Phase-R records→SQL** item, now promoted via
[`review/17`](17-state-store-backend-evaluation.md) (federated query / query API / state integrity).
**Archive-backfill** (new, 2026-06-11) — decouple materialize depth from feed-visibility so the
retained archive (records beyond top-`max_episodes`/body) drains audio/transcripts over many runs.
**Opt-in**, gated on its own ASR/encode/storage cost analysis; H5 reserves the `recent_archive`/
`deep_archive` buckets + the windowed-recency throttle so it lands later with no manifest migration.
**Per-stage object files** (new, 2026-06-16) — split the per-source `episodes.json` into one
content-addressed object per derived artifact (`audio.json` / `transcript.json` / `speakers.json`),
each written by exactly one lane, so no shared file is ever read-modify-written across workflows. The
2026-06-16 foreign-block-preserving merge (review/12 §H6) closes the cross-lane clobber but leaves a tiny
re-read→upload TOCTOU window; this split removes the shared file entirely and pairs naturally with the
diarization lane. Sequence after the `diarize` lane lands so the file set is defined once.
**Deleted:** #5 NER (the city's own document search is better ground truth).

---

## §7. Standing verification checklist (run on every update to this document)

Whenever this file, `VISION.md`, or `ROADMAP.md` changes, verify:

- [ ] **VISION ↔ this doc** — every long-horizon idea in VISION has a catalog entry; every Deferred
  entry is consistent with VISION.
- [ ] **ROADMAP ↔ this doc** — every committed near-term ROADMAP item has an L1+ catalog entry; statuses
  agree.
- [ ] **Catalog completeness** — every feature named in any doc (review/01–10, ROADMAP, PLAN, and open
  issues) is present, in a phase or the Deferred backlog.
- [ ] **Maturity/status consistency** — each entry's maturity (L0–L3) matches where its design lives
  (L1 inline / L2–L3 breakout) and reality (Shipped ⇒ merged PR).
- [ ] **CHANGELOG sync** — every Shipped entry has a CHANGELOG line; ARCHITECTURE.md updated if the
  architecture changed.
- [ ] **Breakout freeze** — every shipped initiative's breakout is stamped "Implemented in PR #N".
- [ ] **Cross-link integrity** — links across root docs ↔ this doc ↔ breakouts ↔ memory resolve.
- [ ] **Bump** the `Status: LIVING · last updated` date.

---

## §8. Codex review appraisal & post-review queue

The Codex review ([`review/10`](10-codex-architecture-throughput-roadmap-review.md)) was verified
against live code and is **accurate and well-grounded**. Disposition of its recommendations:

| Codex | Verdict | Home |
|---|---|---|
| R1 ASR benchmark → sharded split | Modify (benchmark workflow shipped; sharded split pending) | H6/H9 |
| R2 stage backlog manifest | Adopt + **extend** with a configurable prioritization policy | H5 |
| R3 projection wall-clock fix | Adopt | H2 |
| R4 roadmap/docs reconciliation | Adopt (this doc set) | H1 |
| R5 feed-health catch-up states | Adopt | H4 |
| R6 feed-validation publish gate | Adopt | H3 |
| R7 per-meeting pages + search | Adopt | Phase R / review/13 |
| R8 Strong Towns tags/watchlists/alerts | Adopt (tags now; alerts → Phase F) | review/14 / §5.3 |
| R9 contributor/agent handoff guide | Adopt | shipped (AGENTS/ARCHITECTURE/CONTRIBUTING) |
| "defer email" | Modify → RSS/static first, **Substack** shortcut | Phase E |
| module splits | Adopt opportunistically (e.g. `ops/workqueue.py` from H5) | as touched |

**Post-review code queue (historical through the shipped H1–H14a work):**
H8 resource guard (shipped [PR #235](https://github.com/BashfulBits/city-meeting-podcasts/pull/235)) →
H11a deploy resilience (shipped [PRs #239](https://github.com/BashfulBits/city-meeting-podcasts/pull/239)/[#241](https://github.com/BashfulBits/city-meeting-podcasts/pull/241)/[#242](https://github.com/BashfulBits/city-meeting-podcasts/pull/242)/[#243](https://github.com/BashfulBits/city-meeting-podcasts/pull/243)/[#244](https://github.com/BashfulBits/city-meeting-podcasts/pull/244)/[#246](https://github.com/BashfulBits/city-meeting-podcasts/pull/246)/[#247](https://github.com/BashfulBits/city-meeting-podcasts/pull/247)) →
**Do-now (this review's follow-ups):** **H12** (shipped, [PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253)) transcript-artifact rework (clean segment-cue VTT + a
word-JSON sidecar + version-aware gradual re-transcribe — fixes #249's word-per-cue regression and
unblocks search/clips/diarization); **H6a** ASR benchmark workflow (shipped,
[PR #256](https://github.com/BashfulBits/city-meeting-podcasts/pull/256): manual max/med/min
model + beam-size + CPU-thread benchmark before backfill); **B2** Retry-After **clamp** (fold into #39).
**Then confirm `native_audio_max_active: 4` / H1 (next):** `gh` issue reconciliation — close/narrow GH#154
(`<podcast:transcript>` shipped), GH#110 (ASR → backfill/ops), GH#141 (timeline epic; later closed);
H2 projection wall-clock fix + tests (incl. a per-run telemetry summary record — see review/12 H2);
H3 validation gate; H4 feed-health states + per-provider error rates; H5 backlog manifest +
prioritization (including an explicit alignment-deferred lane for untimed provider transcripts);
**remaining Phase H tail (canonical order, 2026-06-27):** H16 Granicus proxy validation/simplification
(GH#353) → H17 R2/CAS control plane + durable pull-work substrate (GH#390) → H18 timeline/audio
integrity repair ([review/20](20-timeline-audio-integrity-repair.md)) → H14b Modal and H14c Beam
pull workers in parallel (GH#276/#277) → H9 combined-throughput/routing evaluation (GH#278). H15
caption-trust scoring (GH#391) may proceed after H17 in parallel with the external-worker tail because
it reuses the same durable source/telemetry conventions but does not gate H14. Runtime/dependency
maintenance is the final Phase-R item, so completing Phase R—not a separate cross-phase gate—delivers
automated, reproducible, smoke-tested dependency and FFmpeg updates.
When each step completes, apply §2's Implemented-row doc updates in the same PR or immediate post-merge
docs PR.

**Explicitly out of scope (now):** move to a hosted DB/API — *with one scoped exception:* the **Phase-R
records→SQL** item (federated query / query API / state integrity) is promoted to Phase R (L1,
trigger-gated) per [`review/17`](17-state-store-backend-evaluation.md). Note **R2 object storage is not a
hosted DB** — the coordination/records → R2 move stays within bucket-as-truth and is *not* superseded by
this stance. Move media off GitHub Actions (keep as
a fallback only); full video re-hosting (deferred — storage + legal surface). **Already satisfied:** live
endpoint contracts kept out of PR CI (separate `contracts.yml`).

---

## §9. Shared conventions (referenced by all breakouts)

Pointers, not copies — the source of truth for each:

- **Identity & invalidation** — stable UID, split hashes (`audio_spec_hash` vs `feed_content_hash`),
  content-addressed keys: `records.py`, [ARCHITECTURE.md](../ARCHITECTURE.md).
- **Append-only archive** — `records.merge_persisted` (#52).
- **Stage pipeline + stop budget** — audio-affecting stages precede `AudioStage`; gate only expensive
  restartable work; deferred ≠ failed: `stages.py` docstring, [ARCHITECTURE.md](../ARCHITECTURE.md).
- **Timeline served↔source basis** — [`review/08`](08-timeline-and-content-transforms.md),
  [`review/09`](09-infra-1-9-pr-audit.md).
- **Resource/throughput model** — [`review/03`](03-resource-model.md), `projection.py`.
- **Security** — SSRF gate, ffmpeg whitelist, defusedxml, **untrusted LLM output**:
  [SECURITY.md](../SECURITY.md), [`review/04`](04-audit-bugs-security.md), `security.py`.
- **Endpoint contracts / fixtures** — [`review/05`](05-endpoint-contract-tests.md), `contracts.yml`.
