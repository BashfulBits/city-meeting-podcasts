# Technical Design Roadmap (canonical, living)

**Status: LIVING · last updated 2026-09-05 (interactive direct remedy #1231; review/45 system architecture evolution & refactoring roadmap detailed to L3 per workstream, see review/45; Gemini free-tier hard input ceiling + `/remedy` deferral fix, §Rate-limited LLM dispatch Worker)**

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

### Active reliability follow-up: interactive remedy (#1231)

Maintainer-directed, 2026-09-05; implementation in progress: direct-only, same-Actions-run remedy
classification, local per-finding schema/evidence correction, wildcard selectors for recurring
provider labels, compact complete-evidence batches, and an issue response
within a bounded job rather than a deferred Worker/sweep handoff. Preserve free-route quota accounting;
bypass legacy remedy result caches without deleting unrelated results or changing catalog versions.
Accepted edits must pass the full repository gate before a PR; incomplete classifications remain
explicit in the report. This narrows the existing interactive remedy contract, not the general
background-dispatch roadmap. See ARCHITECTURE's Unexpected-Body Remediation execution contract.

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
| **H4 stale-feed lifecycle + provider-migration continuity** | umbrella [GH#970](https://github.com/BashfulBits/city-meeting-podcasts/issues/970), slices [#971–#975](https://github.com/BashfulBits/city-meeting-podcasts/issues/971) | **Shipped 2026-07-22** | Frozen design: [`review/37`](37-stale-feed-lifecycle-and-provider-migration.md). PR [#976](https://github.com/BashfulBits/city-meeting-podcasts/pull/976) shipped stable logical source identity, fail-closed migration reporting for historical-copy and forward-only provider cutovers, and the reviewed four-state lifecycle. PR [#977](https://github.com/BashfulBits/city-meeting-podcasts/pull/977) shipped capped native cohort/incident reconciliation and conclusive recovery/committed-disposition closure. PR [#978](https://github.com/BashfulBits/city-meeting-podcasts/pull/978) shipped collaborator-authorized `/stale pause\|dormant\|retire` commands that create deterministic validated review PRs. PR [#990](https://github.com/BashfulBits/city-meeting-podcasts/pull/990) supplied the resumable migration tool and converted all 11 legacy GH#774 rows to native children #979–#989 with exact `first_seen` preservation and Operations Project metadata. The children now own real-world triage; GH#774 closes automatically when they resolve. No pipeline-version bump or automatic audio/ASR backfill. |
| H5 stage backlog manifest + prioritization policy | #41, R2 | L3 | **Shipped** ([#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263) ordering engine + [#264](https://github.com/BashfulBits/city-meeting-podcasts/pull/264) manifest/sidecar/status/light-ordering + [#265](https://github.com/BashfulBits/city-meeting-podcasts/pull/265) global two-pass enrich queue) — hybrid manifest (`citypods/ops/workqueue.py`); behavior-preserving deterministic default; comparator registry (windowed `recency`/`within_days`, partial `city_order`, …); diarization-forward artifact-keyed schema; global queue. `recent_archive` is active for the bounded per-body 501–2,000 cohort in review/39, while `deep_archive` remains metadata-only. Design frozen in [review/12 §H5](12-hardening-and-efficiency.md#h5--stage-backlog-manifest--configurable-prioritization-policy). Deferred to H6b/H9: competitive lease acquisition + per-item persistence. |
| Body-aware tiered retention + bounded archive backfill | maintainer-authorized 2026-07-26 | L3 | **Implementation in progress** — explicit deviation from §6's previously deferred archive-backfill: all feeds move to 500 RSS-visible / 2,000 full-artifact / 10,000 metadata per canonical body, unioned in the shared source archive. `feed_visible_first` protects publication while the 501–2,000 cohort drains within the existing wall-clock budget. Design: [review/39](39-body-aware-tiered-retention.md). |
| H6a ASR benchmark workflow (`asr-bench.yml`) | #1 | L3 | **Shipped** ([PR #256](https://github.com/BashfulBits/city-meeting-podcasts/pull/256)) |
| H6b separate audio + ASR workflows, sharded | #1, R1 | L3 | **Shipped** ([#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273)) · `enrich.yml` replaced by `audio.yml` + `asr.yml` (own `audio`/`asr` concurrency groups, `strategy.matrix.shard`=4); `enrich --shard K/N`/`--source`/`--lane {audio,transcribe,align}`; `run.py` filters cities by source-atomic weighted `shard_assignment(source_key)` + threads the lane into the two-pass queue; Audio weights pending playable/unknown work by expected served duration and gives availability-withheld recovery probes only a small fixed cost; **scoped `push_state(only_prefixes=)` + `reconcile_state(full_run=False)`** so shards don't clobber; `audio.yml`=`--lane audio`, `asr.yml`=`--lane transcribe` with `asr-transcribe` only; **`align` lane implemented but unscheduled** and assigned the separate `asr-align` extra (forced alignment deferred — caption feeds get fresh ASR meanwhile); provider leases reserved for H14 except Granicus media fetches now have a targeted cross-shard lease. **Follow-up fix (2026-06-16, `fix/cross-lane-record-clobber`):** the per-shard scope did not cover the *cross-lane* lost update — the audio and ASR workflows write the same `source_key`'s `episodes.json` at overlapping times, so a late ASR run re-uploaded its start-of-run `audio` block over freshly hosted audio (`hosted_audio −16`). Scoped pushes are now **foreign-block-preserving** (`records.protected_blocks_for_lane`/`merge_preserving_foreign`, `statesync.push_records_merged`) and `stages.LANE_STAGES` keeps each lane to its own work-class stages. Block/lane registries extend to the `diarize` lane (review/12 §H5/§H6). **Deferred:** per-stage object files (`audio.json`/`transcript.json`/`speakers.json`) to remove the shared `episodes.json` read-modify-write entirely (closes the residual TOCTOU window — §6). |
| H7 contributor/agent handoff docs | #57 (partial), R9 | L3 | **Shipped** (this doc set: AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING + templates) |
| H8 4-core runner saturation (ffmpeg `-threads` + memory admission + killable ASR process) | new | L3 | **Shipped** ([PR #235](https://github.com/BashfulBits/city-meeting-podcasts/pull/235)); 2026-06-18 follow-up replaces length-growing dynamic loudnorm and parallel-trim buffering with versioned bounded-memory multi-mic speech mastering (sample-accurate streaming timeline → high-pass → `dynaudnorm` → compressor → measured linear loudnorm), fixed 768 MiB reservation, phase-specific native admission, a constant-gain + short-lookahead limiter fallback for peak-constrained material, and gradual content-addressed remastering. 2026-06-20 follow-up moves local ASR into a persistent killable subprocess and persists per-episode exponential timeout backoff so one native stall no longer poisons the rest of a shard. |
| H10 ASR alignment fix (`WhisperModel.align` AttributeError + fallback gap) | new | L3 | **Shipped** ([PR #232](https://github.com/BashfulBits/city-meeting-podcasts/pull/232)); superseded for provider text by WhisperX known-text alignment: source markers are cleaned and remapped to served windows, raw word coverage is gated at 90% before interpolation, and below-gate candidates are marked ineligible for provider-align and return to the full-ASR queue without same-pass transcription. `PROVIDER_ALIGN_PIPELINE_VERSION=5` gradually reprocesses stale provider-align artifacts through recipe-versioned terminal leases; full-ASR artifacts remain unchanged. Version 5 also corrects the internal worker to pass the configured WhisperX CTC model rather than the faster-whisper transcription model. |
| H11a deploy resilience — native work gate + one-slot audio lane + concurrency tuning | new | L3 | **Shipped** ([#239](https://github.com/BashfulBits/city-meeting-podcasts/pull/239)/[#241](https://github.com/BashfulBits/city-meeting-podcasts/pull/241)/[#242](https://github.com/BashfulBits/city-meeting-podcasts/pull/242)/[#243](https://github.com/BashfulBits/city-meeting-podcasts/pull/243)/[#244](https://github.com/BashfulBits/city-meeting-podcasts/pull/244)/[#246](https://github.com/BashfulBits/city-meeting-podcasts/pull/246)/[#247](https://github.com/BashfulBits/city-meeting-podcasts/pull/247)) |
| H11b deploy resilience — render-only deploy | new | L3 | **Shipped** ([#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272)) · `deploy.yml` stripped to render-only (no ffmpeg/ASR, `actions: read` dropped); heavy phase → new `enrich.yml` (own `enrich` concurrency group); **render writes only `docs/`** — `build()` gates `save_records`/`push_state`/`reconcile_state` off `--phase render` so the enrich workflow is the sole record writer (closes the lost-update record-write race); `statesync.push_state(only_prefixes=)` + `reconcile_state(full_run=)` scope hooks ready for H6b sharding. Precedes H6b. |
| H11c deploy resilience — graceful SIGTERM + mid-run checkpoint | GH#377 | L3 | **Shipped** (GH#377 closed 2026-06-20, [#386](https://github.com/BashfulBits/city-meeting-podcasts/pull/386)) · closes the gap noted above that `continue-on-error` can't catch a runner-level SIGTERM/lost-comms: the CLI entry installs a SIGTERM handler latching a process-wide interrupt the existing `StopSignal` predicate ORs in, so a GitHub cancel/lost-comms converts to the graceful-stop path (in-flight workers defer, the run still persists records + writes its `run_history` entry + pushes state). The global enrich queue persists every source as the **audio pass** drains (before the decoupled transcript pass) and again at the end — idempotent (append-only `merge_records`; `persist_source` no longer mutates the caller's notes list). Interrupted runs are tagged `interrupted`/`outcome:"interrupted"` in `run_history.jsonl` + `run_summary.json` and exit `143` (128+SIGTERM); a normal wall-clock/supersession yield is **not** an interrupt and stays exit `0`. Follows H11a/H11b. |
| H11d deploy resilience — retry `actions/deploy-pages` on transient backend failures | new | L3 | **Shipped** ([#822](https://github.com/BashfulBits/city-meeting-podcasts/pull/822)) · two `Build & Deploy` runs on 2026-07-05 failed at the `Deploy to GitHub Pages` step with GitHub's own generic `Deployment failed, try again later.` despite a clean render/upload and no overlapping Pages deploy (the `pages` concurrency group already rules out self-inflicted races) — a known transient hiccup in `actions/deploy-pages` itself, not a build/render defect. `deploy.yml`'s deploy step now attempts up to 3 times with backoff (15s, then 30s) before failing the job; unrelated to H11a–c (no SIGTERM/queue/record-write path involved). |
| H12 transcript artifact rework (segment VTT + word-JSON + version-aware re-transcribe) | #249 regression, R2/#7 | L3 | **Shipped** ([PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253)) |
| #39 per-provider rate limiting (incl. Retry-After clamp) | #39 | L2→L3 | **Shipped** ([#274](https://github.com/BashfulBits/city-meeting-podcasts/issues/274)) · process-global `HostRateLimiter` (per-registrable-domain cap from `provider_rate_limits`) acquired by **both** `GuardedHTTPAdapter.send` **and** the ffmpeg/ffprobe fetch paths (`media.py`, `concat.py`) — the H6b storm was ffmpeg, not `requests`, so capping only the adapter would have missed it; Granicus follow-up adds B2-compatible soft `provider_distributed_leases` around media reads across the four audio shards (initially 6 after 2026-06-15 overlap probes, reduced to 2 by GH#300 Phase 1) plus `rate_limited` classification and a circuit breaker (initially run-local, now storage-shared and tenant-scoped in the #337 follow-up); Granicus 403 backoff lifted into the shared retry (403 in `status_forcelist`, Retry-After clamp kept); also fixed the H6b regressions it surfaced: truncation guard (encode < 50 % of declared duration → #120 backoff, not hosted), source-atomic weighted `shard_assignment` (no empty `audio (0)`, large sources packed first), responsive 0.5 s ffmpeg poll (honest `seconds=`). Design: [review/12 §#39](12-hardening-and-efficiency.md#39--per-provider-rate-limiting-sequence-with-h6b) |
| **H16 Granicus proxy validation + recovery simplification** | [GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353) | L3 | **Shipped** (GH#353 closed 2026-06-24) · shared GitHub egress reputation—not request shape or the configured concurrency ceiling—caused the Actions-runner 403s. PRs #368–#370 shipped a tenant-allowlisted authenticated Cloudflare Worker and a direct-first, single-Worker-attempt fallback inside the unchanged 1-local / 2-distributed envelope. Audio #46/#47 supplied favorable transport evidence but exposed missing acceptance automation, identity proof, generic signed-URL redaction, and a separate need to represent city-supplied empty/invalid recordings without publishing them. **PR1 acceptance reporting shipped in [#405](https://github.com/BashfulBits/city-meeting-podcasts/pull/405); PR2 identity invariants + generic subprocess redaction shipped in [#406](https://github.com/BashfulBits/city-meeting-podcasts/pull/406).** Every Audio shard now proves stable Granicus record/artifact identity and strips signed media queries and credentials from subprocess surfaces, so qualifying runs receive a real identity verdict instead of `not_reported`. **PR3a durable media-availability classification is implemented:** an explicit, versioned `media_availability` verdict (available / suspected/confirmed empty / missing / invalid / recovered + operator override) rides the audio lane's existing silence-detect decode, withholds empty/missing recordings from both feeds and `AudioStage` while metadata stages continue, requires two independent successful silent fetches to confirm (transport failures never confirm), and recovers automatically — all without bumping the audio pipeline version. **PR3b bounded proxy evidence + weekly review digest is implemented:** `availability-digest.yml` / `scripts/availability_digest.py` deterministically sample new/changed empty-recording candidates, emit untrimmed + silence-trimmed low-bitrate proxies plus redacted evidence JSON, and open/update one rolling digest issue only when candidates exist. PR3 narrowly promotes the record/evidence substrate of the Phase-R provider-failure design while leaving presentation/query UI in Phase R. **Identity-mismatch follow-up resolved:** Audio #54 and #56 each failed `identity` with a lone `audio_key`/`audio_spec_hash`/`audio_url` mismatch — a false positive. Proven from #56's per-shard log: an episode's recipe changed mid-run and its re-encode probed a new duration, then the **upload failed transiently** (B2 `ServiceUnavailable`); `materialize_audio` had written `audio_duration_served` before `put_file`, leaving the record carrying the new duration while still pointing at the prior valid artifact, so `verify`'s duration-change branch flagged the retained old key/spec/url against the fresh recompute. Fixes: (1) the encode commits `audio_duration_served` atomically *after* a successful upload; (2) `verify` skips the key/spec/url comparison when the artifact identity is unchanged from capture (pending re-encode, not corruption — generalizing the earlier `legacy_ok` exemption, which was a misdiagnosis: the spec was a real hash, not `"legacy"`). The `stale_leases_reaped` correlation was common-cause, not causal; all six runs (51–56) had **zero** circuit/parking/canary activity. Separately filed ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421)): per-board vs combined feeds with divergent `feed_urls` get distinct `source_key`s, so the same meeting is encoded under two keys (duplicate CAS objects) — efficiency/correctness, analogous to the ASR duplicate-source-view coalescing already shipped. This removes the only observed identity failures. **Circuit/parking/canary machinery removed:** six runs (#51–#56) showed zero circuit trips/deferrals/recovery probes, so the storage-backed rate-limit circuit breaker plus its queue parking and half-open canary recovery were deleted (`provider_circuits.py` → lean `provider_transport.py`, keeping only the per-tenant transport telemetry that feeds the H16 `transport` criterion; the `_run_enrich_global_queue` parking/canary block and `circuit_skipped`/`circuit_keys` plumbing are gone; H16 report schema → v2). The breaker was built for a concurrency-throttle hypothesis H16 disproved; aggregate load stays bound by the provider-lease ceiling and per-episode materialize backoff, and rollback to direct-only remains config-only (unset the two `GRANICUS_PROXY_*` secrets). The remaining open decision is direct-first vs sticky-Worker routing. Older GH#333/#337/#338/#352 proposals are absorbed: concurrency ramping is superseded; provider-media identity/coalescing and selective durable source reuse remain trigger-gated scaling work in [`review/16`](16-scaling-review-plan.md), not current H work. Design: [review/12 §Granicus follow-up](12-hardening-and-efficiency.md#granicus-media-reliability-follow-up-gh300--39-follow-up). |
| H16 duplicate-view audio coalescing | [GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421) | L3 | **Shipped** (GH#421 closed 2026-06-23) · per-board vs combined feeds with divergent `feed_urls` retain distinct `source_key`s, but entity-family audio shard affinity co-locates them and a run-local `(provider, stable uid, audio recipe)` cache fans one successful artifact pointer to every alias. New work encodes once into one deterministic CAS prefix; an existing valid alias artifact may instead become the shared winner. No source-key, UID, recipe, or pipeline-version change means no forced backfill; unreferenced old duplicates fall to normal orphan GC. |
| Retention planning boundary | [GH#1025](https://github.com/BashfulBits/city-meeting-podcasts/issues/1025), review/39 | L3 | **Implemented precursor; superseded operationally by the in-progress body-aware policy.** Fresh observations still enter work only when they survive the exact final-persistence projection; review/39 changes that projection from source-wide prune to per-body tier union and activates bounded backfill for ranks 501–2,000. No pipeline version bump or forced artifact invalidation. |
| **Persisted stage completion + dirty-only scheduling** | [GH#1013](https://github.com/BashfulBits/city-meeting-podcasts/issues/1013) | L3 | **Implemented in the first 1.0 efficiency tranche, before R6/R7 rollout.** Episodes now carry a per-stage terminal envelope (`complete`, `complete-empty`, `deferred`, or `failed`) with stage version and deterministic relevant-input fingerprint. Legacy records are lazily inferred from existing artifacts; unchanged episodes are excluded before stage invocation, while URL/hash, repair, and version changes invalidate only their dependent stage. This is the shared prerequisite for R6/R7 feature stages and preserves the split audio/feed invalidation model. No output-affecting pipeline version bump or backfill is required. |
| Swagit tenant-page Worker fallback | [PR #1011](https://github.com/BashfulBits/city-meeting-podcasts/pull/1011) / [PR #1026](https://github.com/BashfulBits/city-meeting-podcasts/pull/1026) | L3 | **Shipped** · same shared-GitHub-egress-reputation signature as H16 Granicus, on a different host class. Paired local/GitHub-Actions probes (PR #1011: `scripts/probe_swagit_transport.py`, `.github/workflows/swagit-probe.yml`) plus production Audio #257-#259 and the LLM tag-lane enrich showed every known Swagit tenant returning `403` (`server: awselb/2.0`) from GitHub Actions egress while residential requests succeeded. PR #1026 shipped the initial `/views/...` fallback. A recurring tag-lane failure exposed two remaining gaps: the workflow did not receive the proxy secrets, and chapter/video/download requests were direct-only. The follow-up wires tag and Audio and narrowly extends the direct-first fallback to `/videos/{id}` and `/videos/{id}/download`; redirects are never followed by the Worker and returned media targets retain Python SSRF validation. No pipeline-version bump or backfill. Per-tenant telemetry remains a possible follow-up because provider methods do not take `ctx`. |
| **H13 GPU/ASR execution-backend interface (+ `local` adapter)** | §5.5, [#271](https://github.com/BashfulBits/city-meeting-podcasts/issues/271) | L3 | **Shipped** (#271) · **pre-1.0 lock** · `citypods/compute/{base,local}.py` mirrors `storage/`; `base.py` types all task verbs (ASR + the reserved LLM verbs, first adapter lands R2) + `InferenceJob`/`JobResult`/`JobHandle` + `runtime_checkable` `Backend`; the `local` adapter wraps in-process faster-whisper fresh ASR and WhisperX known-text alignment; `TranscriptStage` routes through `backend.run_inference(...)`; `compute_backend: local` default. Design: [review/12 §H13](12-hardening-and-efficiency.md#h13--gpuasr-execution-backend-interface--local-adapter--the-pre-10-lock) |
| **H14 external transcription adapters** | H14b [GH#276](https://github.com/BashfulBits/city-meeting-podcasts/issues/276) · H14c [GH#277](https://github.com/BashfulBits/city-meeting-podcasts/issues/277) | L3 | **H14a substrate Shipped** ([#275](https://github.com/BashfulBits/city-meeting-podcasts/issues/275)); **local-fallback duration admission + routing-aware shard cost + canonical planner snapshot implemented**: external dispatch is attempted first, but a declined/`local` job above the configurable 4h in-process faster-whisper ceiling remains queued (`reason=external-required`) rather than becoming a runner OOM/retry loop. `TranscribeShardWork` separates duration-weighted local fallback from fixed-cost external dispatch, minimal blocked/deferred inspection, and zero-cost in-flight work so external-only long audio does not distort local runner balance. `asr.yml` now restores B2 state once in reconcile, emits a versioned source-atomic `ShardPlan`, and publishes the state snapshot plus plan as one immutable artifact consumed by all matrix shards, removing both assignment races and four duplicate full-state restores. The rolling 100-sample estimator remains the independent time-window guard. **H14b (Modal) Shipped** ([#807](https://github.com/BashfulBits/city-meeting-podcasts/pull/807), closed [GH#276](https://github.com/BashfulBits/city-meeting-podcasts/issues/276) 2026-07-05); **H14c (Beam) Shipped** ([#808](https://github.com/BashfulBits/city-meeting-podcasts/pull/808), closed [GH#277](https://github.com/BashfulBits/city-meeting-podcasts/issues/277) 2026-07-05). Both worker images install the **same version-pinned dependency set as the runner's transcribe lane** (`constraints/asr.txt`, no torch) and **bake the same pinned Whisper revision** (via `citypods.asr`, `ASR_MODEL_PATH`) on a digest-pinned **CUDA 12 + cuDNN 9** base (forward-compatible with a torch diarize step), per the dependency-policy umbrella [GH#804](https://github.com/BashfulBits/city-meeting-podcasts/issues/804). Modal's build genuinely stages local repo files at build time; Beam's remote build cannot ([GH#818](https://github.com/BashfulBits/city-meeting-podcasts/issues/818)), so `beam_app.py` resolves the same pins/model constant **locally** on the machine invoking `beam deploy` and bakes the literal values into the image spec rather than referencing the files by path. Both record per-claim **RSS/GPU-VRAM telemetry** to a CAS object that feeds **H14d** admission/chunking. First **live single-recording validation** (`max_claims: 1`) passed the [pre-live checklist GH#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706) (closed 2026-07-08) / handoff [GH#794](https://github.com/BashfulBits/city-meeting-podcasts/issues/794) (closed 2026-07-08, folded into H14d above). Smoke-testing surfaced a claim-accounting fix: `max_claims` counts **new transcriptions**, not items whose artifacts already exist — those are *adopted* (state reconciled, no GPU) without consuming a slot, so a re-run against a stale manifest scans past already-done head items to reach fresh work (bounded by `max_scan`, default `max_claims + 50`) instead of adopting the head item and stopping. Both must accept recordings above the local ceiling with bounded-memory, backend-independent chunk planning/stitching when required. They consume H17's pull/claim contract; adapters supply routing inputs but must not independently predict live GPU availability or assign ownership. Accepted external work contributes fixed dispatch cost, already in-flight work contributes zero, eligible local overflow contributes recording-duration cost, and work with no eligible backend contributes only minimal blocked cost. “Overflow to local” means local passes both duration/memory and runtime/deadline admission; unavailable external capacity is a queued routing state, not an ASR failure/backoff event. Prefer one external ASR+diarization worker flow for shared I/O/preparation/startup/artifact coordination, while keeping transcripts independently publishable, reconciling speaker identity meeting-wide, preserving the episode-level `transcribe`/`align`/`diarize` `InferenceJob` verbs, and acknowledging the neural models are distinct. Design: [review/12 §H14](12-hardening-and-efficiency.md#h14--external-transcription-adapters-modal--beam-free-tier-bounded-async-dispatch), [§H14b](12-hardening-and-efficiency.md#h14b--modal-transcription-adapter-async-dispatch-backend), and [§H14c](12-hardening-and-efficiency.md#h14c--beam-transcription-adapter-async-dispatch-backend-parallel-with-h14b). Mac-mini/AWS post-1.0. |
| **H14d GPU worker memory/admission optimization** | [GH#794](https://github.com/BashfulBits/city-meeting-podcasts/issues/794) | L3 | **Shipped** (GH#794 closed 2026-07-08, [PR #856](https://github.com/BashfulBits/city-meeting-podcasts/pull/856) + follow-ups #857/#860–864/#866) · H14d converted the first live Modal/Beam telemetry into the production pacing knobs now carried in `config/site_config.yml`: provider-cycle **dollar** budgets (`monthly_dollars`, `reserve_dollars`, `rollover_day_of_month`) plus backend hardware (`hardware.gpu_type`), per-backend preferred days (`modal=even`, `beam=odd`), fresh-work windows, long-meeting preference, and fixed-per-run / fixed-per-claim planning hooks for future diarize/combined flows. The budget ledger now reserves in provider dollars, learns runtime coefficients per backend/task/GPU/model/compute profile, settles Beam from YAML-configured dollars-per-runtime-second, and attempts Modal settlement from exported billing data before falling back to runtime-rate pricing. The current production posture stays deliberately conservative inside the container — **one active transcription at a time** — but raises **sequential** multi-claim ceilings so a worker invocation can actually spend the monthly budget on its preferred days. Measured GPU-type normalization on a fixed Denton pair selected Beam `RTX4090` and Modal `L4` as the default cost-efficient GPUs; H14d also added rerunnable canary entrypoints plus report support that surfaces claimable long/fresh backlog composition and recent telemetry samples. Live validation then found one more control-plane bug: external workers and `asr-worker-report` were trusting stale persisted `state/work.json`; they now rebuild a fresh manifest from canonical records and overlay only persisted operational sidecar state, restoring the real long-meeting backlog view (`91` backlog-long claims over 4h in the first post-fix report). The remaining `unknown duration` bucket is **not blocked** — those episodes stay eligible for claim and can be re-measured later by audio materialization or the hosted-audio/ASR path; they simply do not receive the duration-based long-first preference until a duration is known. Chunking remains **disabled by default** until telemetry shows a clear throughput or memory-pressure need, and any future chunked path still needs an explicit recipe/version story before it can change output. Design: [review/12 §H14d](12-hardening-and-efficiency.md#h14d--gpu-worker-memoryadmission-optimization). |
| H15 transcript-quality metric (periodic caption-trust scoring) | [GH#391](https://github.com/BashfulBits/city-meeting-podcasts/issues/391) | L3 | **Shipped** ([#883](https://github.com/BashfulBits/city-meeting-podcasts/issues/883)/[#884](https://github.com/BashfulBits/city-meeting-podcasts/issues/884)/[#891](https://github.com/BashfulBits/city-meeting-podcasts/pull/891); umbrella [GH#391](https://github.com/BashfulBits/city-meeting-podcasts/issues/391) closed) · the unmeasured "served captions are faithful enough to align against" assumption (why H6b's `align` lane was initially **implemented but unscheduled**) is now a periodic, per-source/body computed trust state instead of a one-time WER study. H15 consumes the shipped provider-transcript registry: city-provided documents stay downloadable as **Original city-provided transcript**, only own `<podcast:transcript>` until ASR/provider-alignment exists, and carry `float \| null` confidence. **No-regeneration invariant:** ASR, provider-transcript-align, and diarize invalidate only on timeline-plan or own-recipe changes; any key migration copies/aliases existing ASR VTT + word JSON instead of recomputing the ~1000 completed episodes. All three layers now ship in production: **L1** free acoustic-fit recorded *every run* from the `stable_whisper.align()` call we already make; **L2** an independent CTC forced aligner over rotating samples; **L3** a human-gold WER/CER calibration anchor with a persistent trend log. Output: per-source trust/confidence that gates `align`/provider-align vs `transcribe` routing + `/admin/status`, including route/calibration distribution, needs-attention rows, and the latest calibration snapshot. Provider-transcript rollout **PT-PR1–PT-PR7** (PT-PR1 shipped in [#452](https://github.com/BashfulBits/city-meeting-podcasts/pull/452); PT-PR2 implemented in [#456](https://github.com/BashfulBits/city-meeting-podcasts/pull/456) with provider-source candidate fetch/backfill and no ASR backfill; PT-PR3 implemented in [#457](https://github.com/BashfulBits/city-meeting-podcasts/pull/457) with render-only provider-original exposure and no ASR backfill; PT-PR4 implemented in [#458](https://github.com/BashfulBits/city-meeting-podcasts/pull/458) with migration-safe ASR key rebase, copy-first migration, and no ASR version bump; PT-PR5 implemented in [#459](https://github.com/BashfulBits/city-meeting-podcasts/pull/459) with provider-transcript-align work, served-time remap, confidence-gated promotion, and no ASR version bump; PT-PR6 implemented in [#460](https://github.com/BashfulBits/city-meeting-podcasts/pull/460) with provider-transcript-diarize work, independent `speakers.json`, transcript-preserving failure status, and no ASR version bump; PT-PR7 implemented in [#461](https://github.com/BashfulBits/city-meeting-podcasts/pull/461) with provider-transcript status/admin slices and no pipeline-version changes; the follow-up in [PR #1190](https://github.com/BashfulBits/city-meeting-podcasts/pull/1190) adds conservative Swagit TXT coarse-window alignment under `PROVIDER_ALIGN_PIPELINE_VERSION=3`, re-evaluating legacy TXT alignments while retaining VTT/SRT artifacts) and its H14b/H14c/H15 phasing are normative in [review/12 §H15](12-hardening-and-efficiency.md#h15--transcript-quality-metric-periodic-caption-trust-scoring); the concurrent-write lanes PT-PR5/PT-PR6 ride the shipped H17 Stage-1 owned-block merge on B2 (records stay on B2 → managed search-DB at Phase R; no R2 record migration). |
| **H17 distributed work/control-plane substrate** | [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390) | L2→L3 | **Implemented — PR1–PR6 all merged, [GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390) closed; unblocked H14b/H14c, both now live.** Promoted the implementation platform from [`review/17`](17-state-store-backend-evaluation.md) + [`review/18`](18-work-distribution-sharding.md) into Phase H: `RoutingStorage` + native R2 `put_cas()`; ownership-keyed per-episode transcript merge and per-`(source,uid)` planning; then the R2 pull-ledger/claim protocol external workers consume. This absorbs GH#340's durable scheduling concern: expensive work is checkpointed, leased, and reclaimed rather than preempted and restarted; a thin cron coalescer remains an optional Actions-cost optimization. H14b/H14c workers are pullers against this claim contract, not passive push executors. **PR phasing ([GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390) comments):** PR1–PR5 merged (`RoutingStorage`/CAS #393, Stage-1 ownership merge #394, `compute_budget.json`→R2 #395, work-lease ledger #397, live validation harness #403). **PR6 implemented (the final H17 PR):** the `DistributedProviderLeasePool` audio-throttle coordination moved from a list-and-sort FIFO candidate election to **per-slot CAS objects** (`provider-leases/<domain>/slot-<i>.json`, added to `COORDINATION_PREFIXES`); the old per-poll *list* was an R2 Class-A op, so the CAS model — which never lists and spends Class-A only on claim/renew/release — both simplifies and de-costs the one "hot" coordination path. Trade-offs: FIFO fairness dropped (the contract is the concurrency *cap*) and a soft N+1 reap-race, both fine for a rate limiter; the pool now requires a CAS backend and degrades to in-process-only on b2/local. Circuit/parking/canary were already deleted by GH#353, so PR6 was lease-pool-only; the PR5 validation harness gained a live provider-slot check. The cutover rides `audio_storage_backend: routing` (already flipped) and is exercised on the next live Granicus audio run. **Stage-2 work-lease reaper: config said on, but the sweep never actually ran until just now.** `work_lease_reaper_enabled: true` was set in `config/site_config.yml` (nested under `defaults:`) believing it flipped the reaper on, but `citypods compute reconcile`'s CLI read the flag at the config document root — a silent `False` fallback on every run, so `reap_work_leases()` was never invoked in production despite H14b/H14c being live. Found closing out [GH#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706) §6(b): a raw-ledger audit turned up 108 leased objects (90 past TTL) that dozens of scheduled reconciles had silently never swept. Fixed to read from `site_config["defaults"]`, with a CLI-level regression test. The 2026-07-06 canaries' "claim → artifact write → budget settle" and artifact-presence-⇒-done confirmations were real, but via the worker's own inline `adopt` path, not `reap()` — the TTL-based crash/preemption-recovery sweep itself is only now live-exercised. **H17's own review/18 §6 step 4 — converting in-Actions shards onto this same claim contract — is now tracked separately below as H19 since its trigger (a second/external worker class live) has fired; see that row, not here, for status.** **Records (`episodes.json`) stay on B2** — the [review/17 swing case](17-state-store-backend-evaluation.md) is **decided** *against* R2-CAS: per-uid lease ownership + the shipped Stage-1 owned-block merge make B2 race-free without CAS, and records migrate straight to a managed search-DB at Phase R (no B2→R2→DB double migration). So neither H14b/H14c nor the provider-transcript lanes need a record-store R2 migration. |
| **H18 timeline/audio integrity repair** | [GH#495](https://github.com/BashfulBits/city-meeting-podcasts/issues/495), feed-health timeline findings | L3 | **Shipped** (GH#495 closed 2026-06-28; GH#795 and GH#702 closed 2026-07-02). Feed-health now emits a `timeline-audio-integrity` JSONL artifact that separates container-only drift from stream-sample/EDL mismatch. Records support an audit-owned `integrity.timeline_audio` repair block, `/admin/status` reports the queue, `SourceMedia.duration_basis` records stream-sample concat probes, source-aware identity detection fixes GH#495 tail-only trims in both hash invalidation and render-path selection, and the timeline/audio/transcript stages consume targeted repair actions without global pipeline-version bumps. A guarded manual feed-health dispatch can stamp a named over-threshold repair cohort and compare before/after telemetry; the PR6 gate that enables `--persist-timeline-integrity` on the scheduled audit has landed. **Withheld/dead lifecycle ([GH#795](https://github.com/BashfulBits/city-meeting-podcasts/issues/795)):** withheld media is terminal for timeline-audio repair (no `rendered-duration-mismatch` for quarantined episodes), confirmed-dead media (`confirmed_empty`/`missing`/`invalid`) polls on a flat 30-day cadence (precedence over any repair flag, since the audit-owned integrity block can't be lane-cleared) instead of exponential backoff, and a repair flag bypasses only the exponential backoff for transient/broken-EDL episodes. **GH#702 PR6** (`silence:3` catalog re-trim, [#709](https://github.com/BashfulBits/city-meeting-podcasts/pull/709)) merged once parts 1–4 proved stable in production (zero `rendered-duration-mismatch` survivors, both stragglers self-resolved) — the single-file silence catalog now re-plans onto the stream-sample clock and drains under the existing stop budget. Design: [review/20](20-timeline-audio-integrity-repair.md). |
| **Unified storage reclaim + R2/B2 lifecycle backstop** | [GH#496](https://github.com/BashfulBits/city-meeting-podcasts/issues/496) (CR-SC-15) | L3 | **Shipped** (GH#496 closed 2026-07-07, [PR #846](https://github.com/BashfulBits/city-meeting-podcasts/pull/846) + hardening follow-ups). The weekly `audio-gc` workflow became **"Storage reclaim"** and now runs three backstops on its existing cron. (1) **Lifecycle-as-code** (`scripts/apply_bucket_lifecycle.py`, `citypods/ops/reclaim.py`): idempotently expires the control-plane validator's R2 scratch prefixes (`work-leases/__validate__/`, `provider-leases/validate-`) after 1 day — the infrastructure fix CR-SC-15 asked for, since a killed runner can't run the validator's best-effort cleanup — and sets a bounded B2 version-retention window (`defaults.b2_retention_days`, default 30d, read back from the live bucket before change); a guardrail refuses any R2 rule broader than a scratch prefix. (2) **Double-confirmed auto-apply GC** (`gc_audio.py --auto-confirm`): a scheduled run auto-deletes only orphans seen unreferenced across ≥2 runs past `defaults.orphan_quarantine_days` (default 21d, ledger `state/orphan-ledger.json`; a reappearing key resets — GH#421 flip-flop guard); manual `apply=true` (main only) still deletes all. (3) **Resurrection watchdog** (`check_reclaim_resurrection.py`): every delete is appended to `state/reclaim-log.jsonl` with a `recover_by` deadline (age-pruned), and a live record re-referencing a still-restorable reaped key opens a `priority:high` issue. **Two distinct windows** by design — the pre-delete *quarantine* (safe-to-delete-yet?) vs the post-delete B2 *retention* (time-to-undo?). Also promotes **"R2 = ephemeral/derivable only"** to a test-enforced invariant (`routing.py` `_EPHEMERAL_R2_PREFIXES`): a coordination prefix not declared ephemeral fails at import + in tests, so a canonical/backup-less record can't reach R2 (which has no soft-delete and is aggressively expired). |
| **H19 in-Actions transcribe migration to the pull/claim contract** | [GH#831](https://github.com/BashfulBits/city-meeting-podcasts/issues/831) | L3 | **Shipped** (GH#831 closed 2026-07-10, [PR #881](https://github.com/BashfulBits/city-meeting-podcasts/pull/881)); **handoff admission/drain tuning shipped in GH#1017**. `asr.yml`'s transcribe matrix is now `N` identical `citypods compute run-internal-worker` jobs against the same Stage-2 lease ledger H14b/H14c use; there is no transcribe `--shard K/N` plan artifact or fan-in path anymore. `compute reconcile` rebuilds the manifest from canonical records before sweeping expired leases, so queue-ordering changes are no longer gated on whichever enrich shard last happened to rewrite `state/work.json`. The shared pull-worker core lives in `citypods.compute.external_worker`, but the GitHub internal worker keeps a distinct supervision layer: a persistent killable local inference subprocess, hard `asr_local_max_duration_hours` admission, shorter-known-item preference, and claim-start gating that refuses any item whose conservative runtime estimate no longer fits before the earlier scheduled handoff or the 350-minute backstop. A queued successor closes admission but does not terminate a healthy admitted claim; lease renewal continues while it drains. The same runtime-estimate substrate external workers use now learns a separate `github-actions` coefficient so that admission shrinks automatically as the worker ages. A locally timed-out claim records ASR timeout backoff on the episode and abandons its lease back to the queue rather than failing terminally; a superseded claim abandons the same way but records no backoff. **Post-review hardening:** that backoff was initially write-only — nothing re-read `transcript_timeout_backoff_until` before a future claim, so `abandon()`'s instant no-TTL requeue let any worker (including Modal/Beam) re-claim and re-time-out the same poisoned recording every run. Fixed by gating `_admit_claim` (shared base class, not internal-worker-only) on the backoff window, and the daily `asr-worker-report` now opens/closes a tracking issue once a recording has failed `asr_timeout_notify_threshold` (default 3) times in a row. External-worker long-meeting preference remains an independent policy hook (`prefer_min_duration_hours`) rather than being baked into the shared loop, preserving future Beam/Modal specialization and keeping future internal diarize workers free to choose different admission heuristics on the same substrate. Design: [review/18 §4.3, §6](18-work-distribution-sharding.md). |
| **H21 duration field consolidation + normalization** | [GH#868](https://github.com/BashfulBits/city-meeting-podcasts/issues/868) | L3 | **Shipped** (GH#868 closed 2026-07-10, [PR1–PR5 #869–#875](https://github.com/BashfulBits/city-meeting-podcasts/pull/875) + follow-up fixes). External-worker telemetry exposed a broader correctness gap: duration is stored and derived through multiple names (`duration`, `audio.duration_served`, `SourceMedia.duration`, `Timeline`/EDL spans, `WorkItem.duration_hours`, report fields), so ASR routing, external-worker reports, feeds, and repair audits can disagree. Consolidate persisted episode-level scalar duration to `source_duration_seconds` and `served_duration_seconds`; keep planned EDL duration derived from `timeline` rather than persisted as a third scalar. Add canonical accessors for both `Episode` objects and record dicts, migrate consumers off raw fields, and run duration healing in an audio-owned/pre-dispatch planner/reconcile normalization step. That step may probe hosted audio when `served_duration_seconds` is missing, but must emit a warning/metric because it means audio materialization failed to persist the canonical value; if no canonical probe is available, the field stays missing rather than being inferred from `timeline` or source metadata. A manual GitHub Action may optionally re-probe existing hosted audio once to populate `served_duration_seconds`; ordinary planner/worker/report paths must otherwise be record-local/manifest-local and must not introduce broad R2 object reads or lists. Design and PR phasing: [review/26](26-duration-field-consolidation.md). |
| **H22 reap only active R2 work leases** | [GH#1018](https://github.com/BashfulBits/city-meeting-podcasts/issues/1018) (child of [GH#1012](https://github.com/BashfulBits/city-meeting-podcasts/issues/1012)) | L3 | **Shipped.** `compute reconcile`'s Stage-2 work-lease sweep derived every candidate `(source_key, uid)` from the discovery index and GETted its lease key regardless of how many were actually claimed — proportional to the whole backlog (measured ~9–11 min probing 6,034 keys for zero active leases). Added a fixed/sharded CAS-managed **active-lease index**: `INDEX_BUCKET_COUNT` (64) bucket objects (`work-leases-index/bucket-<n>.json`, stable-hash bucket assignment) that `claim`/`renew`/`release`/`abandon` optionally mirror into (`update_index=True`, on by default for both external and internal ASR workers); `reap_indexed()` reads only the bounded bucket set and re-validates each entry against the real lease object (still claim authority) before applying the same settle/requeue/leave decision the original `reap()` uses (both share `_settle_leased`) — cost `O(active leases + bucket count)`, not `O(backlog)`. A rotating one-partition-per-run integrity sweep (keyed off `now.toordinal()`) recovers a lease whose index write raced a crash, without ever probing more than a fixed slice per run. `reconcile_compute(..., use_lease_index=False)` (`work_lease_index_enabled: false` under `defaults:`) reverts to the original candidate-probe `reap()` with no code change — the rollout escape hatch the issue asked for. No artifact pipeline-version bump. Design: [review/18 §4.7](18-work-distribution-sharding.md#47-active-lease-index-gh1018--implemented). |
| **H23 batched ASR transcript-record commits** | [GH#1019](https://github.com/BashfulBits/city-meeting-podcasts/issues/1019) (child of [GH#1012](https://github.com/BashfulBits/city-meeting-podcasts/issues/1012)) | L3 | **Shipped.** Each successful transcript commit called `push_records_merged()`, a whole-source fetch+merge+put of `sources/<src>/episodes.json` — on the largest inspected source (~5,480 records), 59 of one run's 93 successes each paid that full-file round-trip to durably record one uid's delta. The issue offered two options: per-episode/lane sidecars (Option A — the "per-stage object files" end-state review/18 §4.5 already deferred once) or same-source commit batching (Option B). **Decided: ship Option B now.** `ExternalTranscribeWorker` (shared by external Modal/Beam and internal workers) now coalesces successful commits into an in-memory per-run batch (`_pending_transcript_records`), flushed as one `push_records_merged()` call — still `owned_uids`-scoped exactly as the single-item call was — on whichever bound is hit first: 5 queued records, 1800s since the oldest, or unconditionally at end of run. (The age bound shipped at 120s first, then was raised to 1800s once every backend's `min_runtime_seconds` floor — 180–240s — was found to exceed 120s, which had been capping real-world batches at ~2 instead of 5; a regression test locks in a realistic per-item gap now.) Lease preservation needed no new keepalive machinery: `lease_ttl_seconds` (6–20h) dwarfs the batch window, and the existing per-item renewal thread keeps a queued item's lease minutes-fresh until it's queued. A failed flush leaves the batch in place for the idempotent owned-block merge to retry on the next queue or end-of-run flush. Media-decode-quarantine and timeout-backoff paths are unchanged (immediate, single-item pushes). No record-layout or schema change, so no migration/rollback plan is needed for this slice. **Option A investigated directly (not deferred on schedule) against R6/R7's actual record-shape additions and Backblaze B2's real, re-verified pricing (transactions are all free; the metered dimension is egress bytes, free up to 3× average monthly storage) — found not currently justified: worked from this project's own real `run_summary.json` numbers, unbatched commit volume sits well inside the free-egress ceiling (realistic case ~30–40× margin; worst-case-all-on-one-source ~1.4×), and Option A doesn't change that margin since it re-shapes per-commit size, not total bytes moved.** Deferred with concrete re-open triggers (flush telemetry showing real cost, the `diarize` lane going live, or the Phase-R managed-search-DB migration's own trigger firing) rather than "once R6/R7 ship." Design: [review/18 §4.8-§4.9](18-work-distribution-sharding.md#48-batched-transcript-record-commits-gh1019--implemented). |

**H6b transcript-lane implementation update (2026-08-11).** The scheduled `asr.yml` matrix now runs
both `transcribe` and `align` lanes. Provider transcript endpoints are probed for every discovered
episode; provider VTT with inline word timing is the only native-serving case, while cue-only VTT,
SRT, and TXT use the provider-transcript-align lane. H15 route decisions are source/body policy and
can dynamically select provider-aligned versus fresh-ASR output; active records and reports preserve
the text/timing provenance needed for the three-way quality review. After provider coverage is
complete, a distinct `transcript-asr-comparison` queue produces full ASR only when normal ASR work
is empty; it is H15 evidence and never replaces the served artifact by itself. See [review/12 §H15](12-hardening-and-efficiency.md#h15--transcript-quality-metric-periodic-caption-trust-scoring).

### Phase R — Research-Tool Surface (toward 1.0)

| **Bounded audio integrity audits** | [GH#1024](https://github.com/BashfulBits/city-meeting-podcasts/issues/1024) (child of [GH#1012](https://github.com/BashfulBits/city-meeting-podcasts/issues/1012)) | L3 | **Implemented in the first 1.0 efficiency tranche.** Audio records now persist the verified immutable key/spec marker, invalidated by a key/spec mismatch or a storage-backend generation/epoch change. Matching trusted pointers bypass routine prefix LIST/HEAD/GET work; a small dirty set uses direct existence probes and larger batches retain one bounded source LIST fallback. `scripts/audit_audio_integrity.py` and `audio-integrity.yml` rotate through 32 stable UID partitions, sweeping every trusted pointer in the day's partition (concurrent HEAD checks, no per-run item cap, so the full catalog is swept monthly regardless of size) under a wall-clock budget that skips remaining sources rather than failing the run; it clears missing pointers and the Audio completion marker and lets ordinary Audio repair them without changing episode identity. Legacy/changed/repair pointers remain fail-closed. No audio pipeline-version bump or byte backfill is required. |

**Implementation status checked against `main` (2026-07-14).** R1 is shipped in PR #897; R10 is
shipped in PR #899 with pacing hardening; R11's core phases are shipped in PRs #906–#908
(Granicus archive-first discovery, calendar composition/backfill, durable no-video rows, and Swagit
agenda/minutes links); R2 is shipped in PR #919; R3 is shipped in PR #920; and R4 static search is
shipped in commits `2f76744` and `60998a0`. R8 remains design-ready but unimplemented; R6 and R7 are
implemented locally, and R7 awaits calibration/rollout;
**R5 is implemented locally** (the unified chapter-only tag-calibration and evaluator overlay,
`review/42`, [PR #1186](https://github.com/BashfulBits/city-meeting-podcasts/pull/1186), not yet
merged as of this paragraph's update); R12 is shipped in PR #927; R9 is shipped with full Renovate coverage, static CI guards, and ffmpeg build automation.
R11's broader vendor coverage (including OneMeeting/Agenda PE/CivicClerk cases) remains follow-up
work as catalog evidence warrants.

**R10 batching update (2026-08-22).** The high-volume v2 topic-tag caller batches new `queue_only`
tagger/prelabeler jobs across the tag lane's worker pool, including recursively split chapter
windows, and the chapter-agenda/chapter-locator lanes now collect their global queues through the
same bounded 1,000-job transport before replaying their existing finalizers against real durable
handles/results. `poll_batch` also chunks every v2 status request at that limit; the deferred sweep
treats a successful bulk pending observation as final for that sweep rather than re-polling every
handle individually. The tag dispatch cap/no-quota short-circuit, direct/v1 behavior, and terminal
recovery remain intact; no pipeline-version bump or backfill is required. See review/44 Phase 4.

**R10 deferred-sweep follow-up (2026-08-30).** The sweep now batches legacy queue-only v2
submissions, and an unresolved v2 status receives at most one recovery *batch*, never a singleton
poll fallback. Its JSON start/end summaries distinguish v1/v2 client-owned outstanding work, with
one bounded v2 coordinator snapshot for independent queue state. V1 deliberately remains unchanged
while it drains: no new R2 ledger, endpoint, or scan. The six-hour workflow is a bounded 30-minute
observation/retry pass (40-minute Actions timeout); a manual CLI invocation can still request a
longer drain. The first bounded production run exposed a pre-budget serial B2 snapshot scan, so the
loader now starts with a flushed phase event, uses 16 bounded concurrent reads, and reports
listed/loaded/omitted snapshot records; it stops admitting reads at the same wall-clock deadline.
No artifact schema, recipe, pipeline version, or backfill behavior changes.

**R10 legacy-v1 recovery follow-up (2026-08-30, [PR
#1349](https://github.com/BashfulBits/city-meeting-podcasts/pull/1349)).** The temporary, manual
`recover_v1_llm_dispatch_results.py` bridge now reconstructs only unfinished agenda/locator jobs
from durable B2 inputs when direct legacy `job_ref` provenance has already been lost. It imports a
completed R2 result only after a unique exact normalized-prompt and response-schema-shape match;
zero/multiple owners and one-to-many historical retry matches remain report-only. The main-only
workflow, structured scan/reconstruction metrics, changelog, review/44 exit plan, and regression
tests ship together. This remains a bounded v1-draining aid: it creates no B2 pending handles,
does not poll the Worker or delete R2 records, and introduces no artifact, recipe, pipeline-version,
or backfill change.

| Item | #/GH | Maturity | Breakout |
|---|---|---|---|
| Per-meeting permalink pages | #46/GH#157 | **Shipped** (#897, 2026-07-13) | [`review/13`](13-per-meeting-pages-and-search.md) Part A · playable meetings get player/transcript/chapters/agenda/deep-links; unavailable recordings retain civic metadata and canonical provenance with a clear no-recording notice · implementation `1790c9f` plus review fixes `20cc5ed` |
| **Rate-limited LLM dispatch Worker** (new, Infra) | new | **Shipped** (#899, 2026-07-13; pacing hardening followed); **extended multi-provider** (review/41, 2026-08-06) · **ROADMAP R10** | [`review/27`](27-llm-backend-and-provider-routing.md) §Worker · Cloudflare edge pacing for tightly rate-limited providers; asynchronous enqueue/poll transport for R2's `JobHandle` path, R2 persistence, bounded queue pacing, and reconciliation-safe polling. [`review/41`](41-multi-provider-llm-dispatch.md) extends the single-Mistral Worker to Gemini/DeepSeek/OpenRouter with a per-route/per-account R2 ledger and multi-account key rotation; its 2026-08-13 ready-marker hardening makes the Free-plan cron selection O(1) in queue depth and supplies an offline migration for pre-index pending records. **Free-plan CPU/throughput work ([`review/43`](43-llm-dispatch-cpu-reduction-plan.md), PR #1219, 2026-08-14):** production `cpuTime` by deployed version drove P50 from `11` to `8` ms and proposes raising `BATCH_CONCURRENCY` to 2 (deployed configuration remains `1/1` until this merges) (measured `8.2`/`9.9`/`15.5` ms at N=1/2/3, and per *request* `8.2`/`4.95`/`5.2` — N=2 is the optimum on both axes; N=3 is superlinear from three resident canonical records). Cost is driven by **R2 operation count**, not bytes: the Worker's own JavaScript is ~`0.4` ms of the `8` ms. Landed: ETag-carrying lease and reservation release, cached ICU formatters, lazy marker-policy parsing, up-front rate commit at every batch size with the concurrency ceiling enforced in memory, one keyed R2 delete per batch, and in-place deferral of heads waiting on short route pacing (a blocked head cost 4 operations — more than a dispatch — and now costs none). **Open, quota-driven:** the Worker is the one component not following the [`COORDINATION_PREFIXES`](../citypods/storage/routing.py) rule — it keeps multi-megabyte prompts on R2, and Class A operations bill *per account*, so dispatch alone is **~43% of the shared 1M/month free tier** at today's throughput. Phasing in review/43: (1) fold `locks/cron.json` into the rate ledger as `state/dispatch_coordinator.json` (shipped in PR #1229; 5 operations → 4 on a dispatching tick, and 4 → 2 on an idle tick); (2) **split the canonical record — control state stays on R2 for the enqueue idempotency CAS, prompts/results/markers move to B2**, roughly doubling the R2-bounded ceiling from ~6,700 to ~11,100 jobs/day (26% of the tier at today's volume; 50,000/day needs the control record off R2, which is step 4, not this one); (3) batch enqueue/poll once throughput passes ~15,000 jobs/day, which addresses the separate 100k/day *Workers request* cap; (4) then choose Workers Paid (`$5`) or a Durable Object coordinator. Rejected with measurements: Queues on the Free plan (10k ops/day, only 16% above the cron ceiling) and Cloud Run for dispatch (bills I/O wait, which Workers do not). **L3 parallel successor (maintainer-directed, 2026-08-17; matured L2→L3 same day after a capacity reconciliation):** [`review/44`](44-bounded-bundled-llm-dispatch.md) specifies an opt-in DO/B2 cron-pull v2 deployment: each existing one-minute Worker tick atomically claims up to four jobs from one SQLite coordinator. It prefers `priority=0` jobs, then distinct routes, then larger conservative jobs, and returns route-local `wait_ms` plans across a 25-second dispatch window; same-route work is admitted only when normal and late timing remain safe. `completeBatch` settles actual attempt start/end time and usage; rare 429s are fenced with a retry-authorization RPC. It has B2-only payload/results, explicit Worker/DO/row-write caps split between ingestion and dispatch-lifecycle, bulk ingress/poll APIs, and a priority-ordered Phase 1–4 build sequence. **Capacity reconciliation (2026-08-17):** the design was prompted by ~126k dispatch-Worker invocations in one day, which turned out to be predominantly v1's un-batched per-job ingress path during an agenda/chapter backfill, not real LLM-call volume (~2,600 that day per AI Gateway); real aggregate free-route capacity (`config/provider_limits.yml`) sums to ~66,620/day, ~15x the ~5,000/day steady-state target, so a previously-drafted Cloudflare Queue push was re-evaluated and rejected again — the actual Free-tier bottleneck is Worker CPU-per-invocation and the shared DO SQLite row-write budget, not provider throughput, and Queues don't relieve either. Migration/coexistence uses a split-cap strategy (v1 and v2 route ledgers each halved against real provider limits while both are live, flipped to 1x once v1's backlog empties) rather than a bespoke pending-record migration tool. v1 remains production until v2's Phase 1 (ingest cutover) lands and Phase 2/3 (paced dispatch, coexistence exit) pass their parity and canary gates. **Phase 1 shipped** ([PR #1253](https://github.com/BashfulBits/city-meeting-podcasts/pull/1253), 2026-08-18): `workers/llm-dispatch-v2/` ingress Worker and SQLite-backed `LLMSchedulerDO` coordinator, `enqueue_batch`/`poll_batch` on `LiteLLMBackend`, and batch v2 polling in `llm_deferred_sweep.py` — the ingress Worker performs zero B2 I/O itself (client-side payload staging only), matching review/44's connection/subrequest-limit revision. `schema-retry`/`resolve-unknown-batch` land as inert/stub endpoints pending Phase 2's dispatch machinery. v2 stays dormant (no route currently selects `llm-dispatch-v2` as a transport) until Phase 2 wires paced dispatch; v1 remains the only production transport until then. **Production incident fix (2026-08-18):** wiring `LLM_DISPATCH_V2_URL`/`LLM_DISPATCH_V2_AUTH_TOKEN` into the six `queue_only` call-site workflows (the first incident report) did not by itself move traffic to v2 — `enqueue_batch`/`poll_batch`'s payload I/O routed through production's `RoutingStorage` (B2 primary + R2 coordination), whose coordination-prefix allow-list correctly excludes `payloads/`/`results/`, so every write hit the router's own non-`cas_capable` B2 guard and raised `NotImplementedError` before the batch ever reached the Worker — `citypods-llm-dispatch-v2`/its DO saw zero traffic while every call kept falling through to v1. Fixed in `_storage_client()` (reach past the router to its `.primary` B2 backend directly, matching this section's own original "write payload with `b2_from_env()`" design), with a regression test against the real `RoutingStorage`; see review/44's Phase 1 revision notes. **Three more hotfixes followed the same day before v2 carried a single real request** ([PR #1257](https://github.com/BashfulBits/city-meeting-podcasts/pull/1257) stale-v1-backlog reconcile storm + Workers Logs config; [PR #1258](https://github.com/BashfulBits/city-meeting-podcasts/pull/1258) `dispatch_v2_url` never wired into the tag/tournament backends; [PR #1259](https://github.com/BashfulBits/city-meeting-podcasts/pull/1259) `console.error`'s second argument silently dropped by Workers Logs; [PR #1260](https://github.com/BashfulBits/city-meeting-podcasts/pull/1260) **the actual root cause** — `LLMSchedulerDO` never extended `DurableObject`, so every RPC call failed from Phase 1's first deploy onward regardless of the other four fixes, invisible to this repo's own test suite because it calls the class directly rather than through the real binding). Full retrospective with guards for Phase 2–4 work: review/44's "Rollout incident retrospective (2026-08-18)". **Durable Objects rows-read overage (2026-08-27):** `LLMSchedulerDO` exhausted the Workers Free plan's 5M daily DO rows-read budget, failing every `jsrpc` call (cron dispatch, `enqueueBatch`, `pollBatch`) until the daily reset. Cause was structural, not load: `claimDispatchWindow` ran two `state`-filtered statements against `bundles` on every cron tick, `bundles` carried only its `bundle_id` primary key so both planned as full scans, and **nothing in the codebase ever deleted from `bundles`** — so cost was 2x the row count of `bundles` and grew with every bundle ever claimed (reads climbing steadily while writes stayed flat at ~21k is the signature). Instrumenting the real coordinator attributed **6,400 of a tick's 6,424 rows read (99.6%)** to those two statements at 3,200 accumulated bundles. Fixed with `bundles (state, created_at)` (**6,424 → 30 rows read**, same tick and table sizes), retention for the two never-deleted bookkeeping tables (`bundles`/`attempts`, default 7 days, bounded per-tick delete budget, a bundle removed only once terminal *and* past its lease so no late `completeBatch` can lose one), and wiring up `purgePendingBatch`/`confirmPurge` — which shipped in Phase 2 *with tests* but were called from nothing, so terminal `jobs` rows and their B2 objects were never released. That wiring needed a second index, `jobs (state, updated_at)`: the purge query's age filter against the existing `(state, priority, created_at)` index would have read every terminal row (**60,189 → 367** VDBE ops at 6,000 terminal jobs), reintroducing the same defect in `SEARCH`-shaped disguise. A **consumption ack** (`/v2/jobs:ack-batch`, called once per poll chunk after `write_deferred` succeeds) replaces the 38-day timer as the real retention trigger, collapsing it to ~1 hour. Standing guard: `test/rows-read.test.js` asserts two invariants over the whole RPC surface — no statement may scan a traffic-growable table, and rows read must not grow against 10x accumulated history — validated by mutation testing rather than assumed. Two plausible, code-derived hypotheses (candidate fan-out; join drive-table choice) were both measured false first, one of them after a committed-then-reverted rewrite; full account in review/44's "Durable Objects rows-read overage retrospective (2026-08-27)". **Rollout compatibility migrations retired (2026-08-28):** both the `job_models` backfill and legacy `retryable` recovery migrations from Phase 2's rollout completed in production (confirmed via a Data Studio query showing both `scheduler.*_complete` flags at `1`), so their code, config knobs, and per-tick state were removed from `coordinator.js`/`index.js`/`wrangler.jsonc`; the `scheduler` columns they wrote are left in place on already-migrated instances rather than dropped. See review/44's "Retired: the rollout compatibility migrations". **Call-site batching follow-up** ([PR #1262](https://github.com/BashfulBits/city-meeting-podcasts/pull/1262), 2026-08-18): none of Phase 1's `queue_only=True` call sites had ever actually accumulated more than one job per `enqueue_batch` call despite the protocol/DO/client all being batch-capable since Phase 1 — confirmed via production Worker logs showing every request byte-identical to a single-job submission. Fixed with a shared `dispatch_job_batch()` helper (`citypods/compute/llm.py`) and batched restructuring of `AgendaChapterCandidatesStage`/`ChapterBoundaryLocatorStage` (`citypods/stages.py`) and `citypods/tournament.py`'s judge-comparison phase; `citypods/tags.py`'s own model-generation dispatch (`llm_tag_suggestions`) remains unbatched — not because its structured-output validation is hard, but because it recursively splits one episode into a variable number of jobs internally and `TagsStage` runs episodes through a worker-thread pool with a live incremental per-run dispatch budget, neither of which the three sites above have to handle — and the transcribe/align call sites are also still unbatched; both deferred to review/44 Phase 4. **AI Gateway custom-provider routing (2026-08-29, [PR #1327](https://github.com/BashfulBits/city-meeting-podcasts/pull/1327)):** every NVIDIA and SambaNova route 404'd through the gateway with no failover — 404 is not in either Worker's `retryableStatus` set. Cause was Cloudflare-side and undocumented: for `custom-` providers AI Gateway does not join the registered Base URL as its docs describe (`{base_url}/{provider-path}`), but rewrites the Base URL's **last path segment to a hardcoded `v1`** before appending the caller path — established by registering a throwaway custom provider against an echo service (`/anything/prefix` → `/anything/v1`). Providers registered at their `api_base` must therefore repeat that path in `ai_gateway_chat_path`; `kilo` is registered as `…/api/gateway/v1` (a path it also serves); and `zai`/`opencode` route through a new **`workers/llm-provider-shim`**, since z.ai's `/api/paas/v4` is inexpressible under the rewrite (`v4` → `v1`, and no `v1` path serves its API). The shim keeps them inside AI Gateway's logging rather than bypassing it, and is hardened as a credential-forwarding proxy: allowlisted destinations, fail-closed secret, opaque rejections, and refused upstream redirects (Workers' `fetch` replays `Authorization` cross-origin). This also retired a misdiagnosis that had disabled both NVIDIA DeepSeek routes as NVIDIA-side entitlement gating; they were failing for the same path reason and are re-enabled. Because the behaviour is undocumented and unobservable offline, `tests/live/test_ai_gateway_contract.py` probes the real gateway weekly from `contracts.yml`, with a canary that fails if Cloudflare ever starts honouring the registered path.  **Lane registry + throughput reconciliation (2026-09-03):** three defects kept the producer lanes from filling quota. (1) `INGRESS_PURPOSE_RESERVATIONS` reserved capacity under keys no client sends (`topic-tags`, `moments`) against real purposes `topic-tags:tagger`/`:prelabeler`/`r6-moments`/`r6-judge`; because admission subtracts every *other* purpose's reservation from a job's usable headroom, those two unreachable keys withheld 10,000 of 30,000 daily ingress write units from every real lane while being unusable by the lanes they named, and four research purposes had no entry at all. Replaced by a canonical `llm_lanes` block in `config/site_config.yml` keyed by exact purpose, compiled to the Worker's `ingress_reservations.json` by `scripts/compile_llm_lanes.py` and drift-checked at deploy; an unregistered purpose is now rejected (`purpose_not_registered`) rather than absorbed, and a sub-purpose does not inherit its prefix's budget. (2) Model choice was split between config (`tagging`/`moments`) and Python constants (chapters, tournament, R5 benchmark); all now resolve from that one block, values unchanged so no recipe hash or artifact is affected, with the recipe-affecting strings pinned in `tests/test_llm_lanes.py`. (3) Throughput: the binding constraint is the shared 100,000/day DO row-write budget, not the 50-subrequest ceiling — `MAX_BUNDLE_JOBS` 4→8 would project 114% of it — so `MAX_BUNDLES_PER_UTC_DAY` goes 1,000→1,400 and `MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY` 30,000→24,000 (ingress was admitting 12,000/day against a 4,000/day drain, growing backlog with write budget that could fund dispatch), lifting the dispatch ceiling 4,000→5,600 calls/day at 69% of the write ceiling. Completes Phase 4 call-site batching for `tournament.py`/`r5_benchmark.py` via a shared run-scoped `PerModelBatchingBackends`, removes the tournament's hard two-sample clamp (~32 jobs/day), makes a failed submission exit non-zero instead of 0, and gives the lane workflow steps a step-level timeout so a 180-minute overrun fails red instead of being cancelled grey with nothing persisted — which `tag.yml` hit on eight consecutive scheduled runs, 2026-08-26 to 09-02. See review/44 "Lane registry and throughput reconciliation (2026-09-03)". **Gemini free-tier hard input ceiling + a silently-deferred `/remedy` path (2026-09-04):** live-tested against the real Gemini API — `tpm` on the free tier is a hard per-request cap with no burst room above it (a single request over the usable window fails outright regardless of idle time), and the real usable ceiling can sit well below both `tpm` and the model's advertised context window (confirmed on an otherwise-idle model: ~125,000 actual vs. the `250000` both config and Google's own quota error report); NVIDIA's free tier, by contrast, accepted a real request ~3x its configured `tpm` outright, so this is provider-specific, not a blanket scheduler rule. `LLMRoute` gains an opt-in `hard_input_ceiling` (`citypods/compute/llm_policy.py`), set only on the 14 Gemini/Gemma routes (`config/provider_limits.yml`; conservative interim values pending per-model calibration), enforced in `select_route` and in the Cloudflare Worker's token-bucket pacing (`workers/llm-dispatch-v2/src/pacing.js`, previously assumed every route could burst 5 windows deep). `COUNCIL_MOMENT_MODELS`/the `chapter-locator` lane gain `deepseek/deepseek-v4-pro` + `moonshotai/kimi-k3` (free NVIDIA routes, no hard cap) as overflow. Separately, `citypods/audit_remedy.py`'s `/remedy`-comment classification had no `purpose` set, so a capacity miss silently persisted a deferred handle for the unrelated `llm-deferred-sweep` cron to retry hours later, disconnected from the original request — fixed with an explicit `purpose` + immediate `discard_deferred` on a capacity miss, plus a token-budget cap on its evidence bundle. Mistral's `mistral-large-2512` (`403` live) and NVIDIA's `nemotron-3-ultra-550b-a55b` route (`404` live) are both flagged, not fixed, here. See CHANGELOG's matching entry for full detail. |
| **Cross-provider agenda & history network** (was: Legistar calendar provider) | new | **L3 for Parts A/B/D; Part C design-complete (JSON API + two live portals verified 2026-07-12), awaiting a catalog city on OneMeeting** · **ROADMAP R11 — number out of table-position on purpose; sequenced third, right after R10** | [`review/15`](15-legistar-catalog-provider.md) · re-scoped from Granicus/Legistar-only into three goals (HTML/portal agenda URLs, PDF agenda URLs, extended meeting history). **Implementation decision (maintainer-confirmed 2026-07-13):** the Granicus adapter becomes archive-first (`ViewPublisher.php`, not capped RSS); a verified Legistar calendar is a composition/backfill source for missing video rows and durable agenda-only records, never a guessed replacement. Phase 1 is [PR #906](https://github.com/BashfulBits/city-meeting-podcasts/pull/906) for [#903](https://github.com/BashfulBits/city-meeting-podcasts/issues/903); Phase 2 [#904](https://github.com/BashfulBits/city-meeting-podcasts/issues/904) retains no-video rows in a separate append-only calendar catalog and renders them outside RSS; Phase 3 [#905](https://github.com/BashfulBits/city-meeting-podcasts/issues/905) gives Swagit first-party agenda/minutes parsing rather than a Granicus-archive substitution. Granicus directly markets three parallel agenda products (Legistar, OneMeeting, Agenda PE) any of which may apply to a Granicus- or Swagit-primary city, plus CivicClerk cross-referencing for CivicPlus cities. **Appendix P (added 2026-07-12, extended to exhaustive same day)** censuses the wider vendor landscape — IQM2/NovusAGENDA (Granicus sunset 2027-09-30: migration tripwires, not build targets), CivicEngage Agenda Center, Municode Meetings, BoardDocs, CivicWeb, eScribe, AgendaQuick (confirmed Swagit hook), SIRE, ClerkBase, BoardBook (TASB — most TX-relevant), Simbli, Catalis, Streamline, independent video hosts (Cablecast, TelVue, Viebit, BoxCast, Open.Media, Castus, PEG Central, IBM Video), a dedicated YouTube analysis (no separate government platform exists — ordinary government channels; Data-API metadata path recorded, media-download decision deliberately deferred), and the unstructured CMS-tier long tail — with URL patterns + verification status per platform. Feeds R3 |
| **LLM backend** (new, Infra) | new | **Shipped** (#919, 2026-07-13) · **ROADMAP R2** | [`review/27`](27-llm-backend-and-provider-routing.md) · LiteLLM owns provider translation and response normalization; direct routes return `JobResult`, while rate-limited routes enqueue through R10 and return `JobHandle` for later reconciliation; Mistral dispatch integration, provider choice, budget-aware reconciliation, and the H13 `tag`/`summarize`/`soundbite-select` verbs are implemented; R5/R6 remain its first feature consumers |
| **LLM quality tournament & champion routing** (new, Infra) | new | **Implemented locally: engine, rolling ticket, and merge-gated route proposal** · [PR #1186](https://github.com/BashfulBits/city-meeting-podcasts/pull/1186) | [`review/34`](34-llm-quality-tournament-champion-routing.md) · weekly per-verb pairwise tournament and a strict `>60%` challenger ticket. A trusted decision opens a scoped route-configuration PR; it never changes production directly or auto-merges. A merged retained-catalog choice starts resumable, recipe-driven bounded backfill without a pipeline-version bump. Complementary to review/35's per-candidate matrix. |
| **Reusable LLM confidence calibration & human review** (new, Infra) | new | **Implemented** (shipped as part of R5) · design write-up 2026-07-17 | [`review/35`](35-llm-confidence-calibration-human-review.md) · `citypods/llm_evaluation.py` — a sparse admission matrix keyed by feature/route/prompt-version/taxonomy-version/label/scope, self-calibrating from weekly human ground-truth review; decides whether *one candidate* at its own reported confidence is trustworthy. Only applies to verbs with a discrete, recurring label (tags, future classification); does not generalize to freeform text (summaries). Currently wired only to R5's tagging config — generalizing the `StageContext`/CLI/workflow integration for a second consumer is open follow-up work |
| **LLM-assisted city/agenda-source discovery** (new, Infra) | new | **Shipped** (#927, 2026-07-15; follow-ups #935) · **ROADMAP R12** | [`review/28`](28-llm-assisted-city-discovery.md) · Tavily search → mode-aware `classify-civic-platforms` → SSRF-gated platform signature plus end-to-end adapter verification → evidence-backed human proposal. Daily new-city processing and weekly/manual auxiliary eligibility use 90-day evidence refresh, skip cities already above the 95% recent-agenda threshold, and retain a quarterly-backfill/recheck state for known no-coverage cities. Research-only findings are assigned by slash command to a canonical unsupported-provider tracker instead of being lost in closed issue traffic. Approval binds to the exact evidence digest and supports approve, reject, recheck, defer, provider assignment, and new-provider creation. **One permission model:** every approved city, auxiliary-source, or provider-backlog change is recreated from fresh main in one automation branch and sent to a maintainer-review PR—never directly to main. The D1-backed Formspark Worker keeps email private and passed its website→GitHub/Discord/Resend production smoke test; Tavily/LiteLLM also passed a live read-only Action smoke. Signed Discord intake, Discussions intake, and idempotent email/Discord/Discussion callbacks are implemented. |
| **Agenda, packet, minutes text, votes, and roster extraction** (new, Infra) | new | **Implementation in progress** (baseline shipped in #920; GH#1092 quality/OCR hardening) · **ROADMAP R3** | [`review/29`](29-agenda-text-extraction.md) · [`review/30`](30-minutes-text-votes-rosters.md) · agenda/portal text plus bounded per-item backup-link union are content-addressed sidecars; minutes links fill only missing `links["minutes"]`; deterministic vote and member-roster artifacts preserve evidence/source URLs; GH#1092 adds selective Tesseract/Poppler OCR, conservative admission, chapter eligibility, and consolidated feed-health alerting |
| Static transcript search | #6 | **Shipped** (2026-07-14; `2f76744` + `60998a0`) | [`review/13`](13-per-meeting-pages-and-search.md) Part B · deterministic, cacheable per-source MiniSearch shards; available transcripts and metadata-only unavailable records are indexed with agenda/backup/minutes/vote/roster fields, city/body coverage, progressive filtering, and availability filtering |
| Topic tags / Strong Towns lens | #4 | **Implemented locally** (unified evaluator overlay in [PR #1186](https://github.com/BashfulBits/city-meeting-podcasts/pull/1186), not yet merged) | [`review/14`](14-topic-tags-strong-towns-lens.md) and [`review/42`](42-unified-tag-calibration-and-evaluator-overlay.md) · 37-source-backed flat tags; deterministic episode/chapter annotations with taxonomy-ordered episode projection; the initial 12/90% tagger gate and later 50/95% independent evaluator overlay share one candidate ledger and weekly review packet. The LLM rollout is chapter-only; deterministic matches remain visible until their qualified overlay can suppress an audited likely-incorrect match without deleting evidence. **2026-07-27 calibration fixes:** real calibration-issue review (GH #1057/#1062/#1068/#1072/#1076) found `zoning-reform` firing on individual rezoning cases and `neighborhood-engagement` firing on hearing-procedure boilerplate — split a new `rezoning` tag out of `zoning-reform` (`config/taxonomy.yml` → `version: 2`) and tightened `neighborhood-engagement`'s description/keywords. Separately, R3's backup-document text was fetched but never fed into either tagger; `episode_tag_inputs()` now includes it, backup-document discovery no longer depends on English keyword matching (validated against real Legistar and Granicus agendas — see [review/29](29-agenda-text-extraction.md) §6a, whose `attribute_links_by_content`/second-hop attribution this implements), and agenda-text preamble before the first chapter title is structurally excluded from tagging input. Also fixed the `LLM Tag Calibration Ingest`/`ASR Quality Ingest` workflows, which had been failing every run on an `actions/checkout` `token: ""` bug. `TAGGER_VERSION` → `"2"`. |
| **Per-agenda-item cards, auto-summaries, soundbites** (#3/GH#155, #2, #15/GH#156) | — | **Implemented locally** ([PR #1271](https://github.com/BashfulBits/city-meeting-podcasts/pull/1271), 2026-08-22) · **ROADMAP R6** | [`review/36`](36-llm-first-cards-summaries-soundbites.md) · grounded R6 candidates, durable human/judge calibration, shareable video clips, and publication surfaces are implemented; staged reprocessing backfills existing records gradually. |
| **Speaker diarization + minimal attendee extraction + per-speaker pages** | #7, #14, [#1274](https://github.com/BashfulBits/city-meeting-podcasts/issues/1274) | **Implemented locally; rollout gated** (2026-08-29, PR #1331; engine + naming policy revised 2026-09-06) · **ROADMAP R7** | [`review/31`](31-speaker-diarization-attendee-extraction.md) · native diarization shares the versioned `speakers_*` artifact family with provider output (provider source takes precedence), preserves transcript text, and projects qualified identity assignments into PR #1271 pull quotes. Private evidence, registry, and evaluation ledgers are serialized per city/source so concurrent episode work cannot lose a golden turn or calibration row. **Engine (§A.1a, 2026-09-06):** pyannote-audio → `sherpa-onnx` (pyannote-segmentation-3.0) + NeMo TitaNet-Small embeddings, ~4× faster on the free-runner CPUs at matched accuracy, run as an admission-controlled `N×1-thread` worker pool (§A.4). **Naming (§C.4, 2026-09-06):** the flat 30-review/30-day/95%-per-cell publish gate (and its per-cell benchmark precondition) is replaced by `citypods/naming.py` — signals fuse into one candidate per (cluster, name), members always require human confirmation, staff auto-publish once their *signal combination* reaches ≥20 verdicts at ≥95% agreement, everyone else is never named; precision pools globally with a per-city divergence guardrail, is derived from the review ledger rather than stored, and cold start is fail-closed. Official minutes silently confirm, reassign, or remove names only when their roster is parseable. Public speaker pages contain only attributed quotes and source meeting/time links; embeddings, clips, scores, and review history remain private. **PR #1331 hardens the pilot boundary to explicit provider-body prefixes with joint/section-only exclusions and makes a present word-sidecar pointer insufficient unless it contains valid timed words; invalid artifacts route back through alignment or fresh ASR, and completion state is never stamped for invalid output.** |
| **Front-end design cycle, accessibility, and funding link** | #55/#20/#54, #50, #16 | **L3** (2026-07-13) · **ROADMAP R8** | [`review/32`](32-frontend-design-accessibility-funding.md) · confirmed the bare `#N` references aren't real GitHub issue numbers (checked — they resolve to unrelated closed feed-health issues) before designing further. Part A specifies a design **process**, not a prescribed identity (corrected same day, see the doc's own header note): ground the direction in this project's own subject matter, draft 2–3 genuinely distinct options, check each against a real boilerplate-pattern checklist, mock up survivors against real content, let the maintainer choose before any template changes — real current-state gaps (the "accordion" is already an accessible native `<details>`/`<summary>`; audio/video labels are bare text suffixes; subscribe buttons have zero iconography) and hard constraints (Apple's official badge verbatim regardless of direction; icon-only controls never acceptable) are fixed regardless of which direction wins. Accessibility gaps found by reading the markup, not generic advice: no `aria-live` on three dynamic updates (search count, play state, copy-RSS feedback), no skip link — plus computed (not eyeballed) contrast ratios for the existing muted-text color in both themes, both passing AA. **Funding platform decided (same day):** split by audience across two surfaces — GitHub Discussions for dev/API support, Ko-fi + Discord (native first-party role-sync, unlike GitHub Sponsors which has no official Discord integration) for sponsors/community/feedback — pointed at from a new self-hosted `/support/` page rather than a third-party link-in-bio tool; `<podcast:funding>` defaults every city's feed to that page's URL via a new site-wide config default. GitHub→Discord activity updates are a native zero-code webhook; a scheduled roadmap-digest bot is flagged as a real future item, not designed here. **Part A also gains a Substack forward-look**: a future subdomain-hosted newsletter/digest layer (confirmed one-time $50 mapping fee) means whichever visual direction is chosen should stay reproducible within Substack's own customization surface |
| Durable provider-failure classification feed | new (absorbs closed GH#379) | L1 · **deprioritized below R6/R7 (diarization)/R8 (2026-07-12)** | §5.1 · append-friendly episode/source failure events with stable categories, terminal-vs-transient state, first/last seen, and references to existing provider telemetry; `/admin/status` is the first reader, with Phase-R query surfaces consuming it later. **Why lower priority:** the safety-critical part already shipped as H16 PR3 (withholding empty/broken media, redacted evidence, weekly digest); what's left here is an observability/trust *presentation* layer on data that's already being captured safely — valuable, but nothing user-facing breaks by deferring it |
| Hosted-runner infrastructure failure monitoring + pinned-runtime fallback | new (Infra) | L1 · **deprioritized below R6/R7 (diarization)/R8 (2026-07-12)** | Track ASR/audio/other long-running GitHub Actions failures whose root cause is hosted-runner infrastructure (`exit 143` without the graceful-yield marker, lost runner communication, missing per-step logs, similar non-deterministic runner shutdowns). If those failures continue at a material rate after H14b/H14c/H19 stabilize the worker mix, promote a repo-owned pinned container runtime for the affected workflow(s) so the execution environment is versioned like the Python/ffmpeg/model inputs instead of relying on `ubuntu-latest`. This is reliability/reproducibility follow-up, not an automatic scope promotion over the external-worker path. **Why lower priority:** this is an evidence-gated watch item with no GitHub issue behind it — there's nothing to build until a `run_history.jsonl`/exit-143 telemetry check confirms the trigger has actually fired, which hasn't been done |
| Runtime/dependency maintenance automation | umbrella [GH#804](https://github.com/BashfulBits/city-meeting-podcasts/issues/804) | L3 | **Shipped** (2026-08-23) — **ROADMAP R9** · normative contract in [`review/22`](22-dependency-and-reproducibility-policy.md): compiled **version-pinned** Python `constraints/*.txt` (single source of truth for CI, the GHCR runner image, **and** the Modal/Beam worker images) with `lock.yml` + a `ci.yml` drift gate; all third-party Actions SHA-pinned ([GH#734](https://github.com/BashfulBits/city-meeting-podcasts/issues/734), closed); HF model revisions pinned ([GH#498](https://github.com/BashfulBits/city-meeting-podcasts/issues/498), closed) and shared canonically in `citypods.asr`; `.github/renovate.json5` two-lane flow (hygiene auto-PRs; a Dashboard-approval gate + **per-source** `dep-bump-smoke` for output-affecting bumps); `scripts/check_dependency_policy.py` CI guard enforcing 100% dependency coverage across Python and Worker runtimes; monthly immutable-URL/checksum FFmpeg build workflow (`.github/workflows/build-ffmpeg.yml`). |
| **System architecture evolution & refactoring** (umbrella) | new | **L3 dev-ready per workstream** (detailed 2026-09-04) | [`review/45`](45-system-architecture-evolution-and-refactoring.md) · Architectural program of 19 initiatives plus state-store partitioning (§2) and v1 retirement (§3), each individually L3 dev-ready — exact file/function references, algorithms, schemas, and test names grounded in the current codebase, not a design-discovery placeholder. Not an adopted sprint schedule: §5 sets the dependency order, and a handful of destructive/irreversible steps (the state-generation pointer flip, deployed-Worker deletion and secret removal, the SSRF transport design) still require explicit maintainer sign-off before executing. Review/26 duration normalization and review/34 tournament work are explicitly superseding/owning designs that review/45 points at rather than restates. |

> **Reprioritized 2026-07-12 (maintainer decision): records → managed SQL moves decisively out of Phase R
> and merges with the Interaction-seam idea as one post-1.0 initiative — see the cross-cutting
> "State-store backend + Interaction seam" row in §5.5's table below.** Rationale: review/17 already
> established the DB is a trigger-gated escalation for federated query / a public API / state
> integrity, not a search-quality requirement — [`review/13`](13-per-meeting-pages-and-search.md)'s
> static pages + partitioned client search remain the Phase-R design and are unaffected. The independent
> review/25 architecture review separately proposed a "dynamic edge tier" (Cloudflare Worker + D1 +
> Vectorize + Queues/DO) to serve alerts/API/personalization/scaled-search — features that themselves
> need the same managed-SQL move. Since neither is scoped for 1.0, designing them **together, once,**
> when the trigger fires avoids re-deriving the entity-model schema twice (build the DB, then redesign
> it for the Worker tier) — the same "don't migrate the data twice" logic review/17 already used to keep
> records on B2 instead of routing them through R2 first. This does not add scope to 1.0; it removes an
> ambiguous half-scoped item from the Phase-R table and gives the deferred work an accurate home.
>
> **Same pass: H9 and H20 moved from this table to the Deferred backlog (§4 "Deferred backlog" block /
> §6), not kept in Phase R.** Both are Phase-H-numbered infrastructure items explicitly deferred *from*
> Phase H on 2026-07-10 — their own entries already said "backlog-only" and "background for a future
> re-open." Phase R is the research-tool-surface series that gates 1.0; listing Phase H backlog items in
> that table risked implying they were part of the toward-1.0 series, the same inconsistency the
> records→SQL move above corrects. No content lost — see the Deferred backlog block below for the full
> rationale, unchanged.

### Phase E — Engagement & Distribution (post-1.0) · sketches §5.2

> **First post-1.0 priority (2026-07-12):** a **social syndication bot** (NEW — see §5.2 below) is
> the recommended first thing built after 1.0 ships, ahead of the rest of this table. It rides directly
> on Phase-R's R6 soundbites (#15) and the already-built-but-unwired `clips.extract_clip` service, so it
> is near-zero incremental cost once R6 lands, and it is a growth lever the maintainer wants prioritized.
> **Substack/newsletter stays sequenced after it**, not before, because it depends on the undesigned
> digest-aggregation pipeline (site-news RSS + weekly look-back digest — first three rows below), which
> is real new scope, not a thin publishing step.

| Item | #/GH | Maturity |
|---|---|---|
| **Social syndication bot** | new — §5.2, review/25 §2.4 #13 | L1 · **first post-1.0 priority** |
| Site-news RSS + static digest pages | new (Feature A) | L1 |
| Weekly look-back digest | new (Feature A) | L1 |
| "National highlights" curated reel | new (Feature A) | L1 |
| Substack newsletter channel | #18 (email split) | L1 |
| Topic/region roll-up feeds (pre-generated combos) | #12/#13 | L1 · static, no DB/Worker — see §5.2 for examples and the internal-plumbing rationale |
| OPML export | #17 | L1 |
| Privacy-respecting download analytics | GH#125 | L1 |

### Phase F — Pre-Meeting Foresight (post-1.0) · sketches §5.3
| Item | #/GH | Maturity |
|---|---|---|
| Upcoming-agenda + staff-report scraping | new (Feature B) | L1 |
| Upcoming-meetings `.ics` calendar | #19 | L1 |
| Watchlists + topic alerts | new (R8 extension) | L1 |
| Backup-material (packet) analysis | new (Feature B) | L1 · **a minimal extraction-only slice is pulled forward to ROADMAP R3, gating 1.0** (§5.1); the richer structured/LLM brief stays here, post-1.0 |
| Legistar provider (rich agendas/votes/rosters — InSite API) | #31 | L1 |
| Vote/roll-call extraction (metadata + minutes) | #8 | L1 |
| Attendee extraction (from minutes) | #14 | L1 · **a minimal name-list slice is pulled forward to ROADMAP R7, gating 1.0** (feeds speaker-diarization naming, §5.1); the richer platform-metadata/vote-linked/entity-model form described in §5.3 stays here, post-1.0 |

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
| Pluggable inference-execution backend (compute offload) | new (Infra) | L3 · **pre-1.0 lock** — GPU/ASR interface = H13; Modal+Beam GPU adapters = H14 (built in Phase H); first LLM API adapter = R2 (dedicated infra item, ahead of its R5/R6 feature consumers) |
| Catalog scaling readiness (10→500 cities) | new (Infra) | L2 · **trigger-gated, not active-phase work** — [`review/16`](16-scaling-review-plan.md); R4 owns the search-size spike/partitioned-search launch, while S0–S4 promote one tranche at a time only when their city/metric gates are reached. GH#1014 shipped the conditional-refresh substrate; GH#1023's follow-on topology design is broken out in [`review/38`](38-discovery-centralization.md) |
| **State-store backend + Interaction seam** (coordination → R2/CAS · records → managed SQL · dynamic edge tier for alerts/API/personalization) | new (Infra) | Coordination (leases/work-queue/budget) → R2/CAS: **L3, shipped as H17**; [`review/17`](17-state-store-backend-evaluation.md). Records→SQL + Interaction seam: **L1, post-1.0** (inline sketch: §5.5) — **reprioritized 2026-07-12:** records→managed-SQL (D1/Turso, kept open) moves decisively **past 1.0** and merges with the [review/25 §3.1](25-future-features-and-architecture.md#31-the-central-recommendation-a-dynamic-edge-tier-the-interaction-seam) "Interaction seam" proposal (Cloudflare Worker + D1 + Vectorize + Queues/DO) into one initiative — design the managed-SQL store, the entity-model schema (Person/Body/AgendaItem/Vote/Document, per review/25 §3.4), and the Worker tier together when the trigger fires (federated query need, a public API, a search partition exceeding budget, or the full custom-query feed builder — review/17 §1.4), rather than scoping the DB now and the Worker tier later. Not yet promoted past L1/sketch; break out to its own `review/NN` when it becomes next-up. |
| Work distribution & sharding for distributed ASR workers | new (Infra) | L2 · Stage 1/Stage 2 substrate shipped as **H17** (closed); the remaining §6 step 4 in-Actions migration is now tracked as **H19** (its trigger fired — H14b/H14c are live); [`review/18`](18-work-distribution-sharding.md) |

### Deferred backlog (ongoing) — §6
#9 translation · #24 bitrate ladders · #25 intro/outro stinger (GH#153) · #26 chapter
images · #34 config-via-issue-comments · #40 B2 actual-cost dashboard · #42 **directory** index sharding
(review/02 Change 6; trigger-gated by `review/16` client budgets, distinct from R4 transcript-search
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
toward 90%) · **H9 combined-throughput evaluation** ([GH#278](https://github.com/BashfulBits/city-meeting-podcasts/issues/278), deferred from Phase H 2026-07-10) — H14d's live telemetry, chosen default
GPUs (`Beam RTX4090`, `Modal L4`), provider-cycle dollar budgets, and the maintained local 4-shard
throughput baseline already answer the launch-gate question this item was meant to close: combined
free-tier capacity clears the 80-feed initial transcription backlog within one month with margin.
Design text kept in [review/12 §H9](12-hardening-and-efficiency.md#h9--combined-throughput-evaluation-diarization-speakerhour)
as background for a future re-open if backlog shape, provider pricing, or the diarization rollout
materially changes · **H20 external work-lease stale-claim hardening** (new, deferred from Phase H
2026-07-10) — H17/H14d fixed the material correctness/operability gaps from the first live worker runs;
remaining stale-claim ergonomics (lease age/renew-at visibility, one-owner/one-item doctoring, TTL
retune) are useful but not currently required for launch or safe routine operation; promote only if
real operator pain or repeated stale-lease incidents justify it.
**Deleted:** #5 entities/NER.

---

## §5. L1 sketches (initiatives not yet broken out)

Each: problem + 1–3 candidate approaches + rough tradeoffs. Promote to a breakout (L2) when chosen.

### §5.1 Phase R remainder

**LLM quota & cost-window scheduler (Infra — ROADMAP R13). Implemented 2026-07-17** — full design in
[`review/33`](33-llm-quota-cost-scheduler.md), simplified 2026-07-16 from an initial L2 draft, then
revised again post-implementation (code review + an interface-unification pass, review/33 §13.2). Adds
the policy layer above R2's LiteLLM adapter and R10's Mistral transport as a stateless selection function
(no new persistent service): a 4-field request contract (`allowed_models`/`allow_paid`/`deadline_at`/
`purpose`), free-model protection, deadline-aware selection, and a CAS-backed quota/cost ledger shared
with review/27 §8's still-unbuilt $ budget ledger — one object, not two. Gemini's RPM/TPM/RPD quotas
and midnight-Pacific reset are enforced via that ledger with **no dedicated Gemini Worker**: unlike
Mistral's ~1–2 RPM limit, Gemini's free tier doesn't need a process that outlives a single Actions run
(review/33 §7), and a real 429 from any provider now reactively blocks that route until its
`Retry-After` hint (review/33 §7.1). DeepSeek off-peak dispatch preference ships as a config-driven price
window, not a blocking constraint. A caller that gets deferred (nothing eligible yet, a real rate limit,
or a genuine in-flight Mistral dispatch) is picked up later via a portable `JobHandle`/`reconcile()`, a
B2-backed deferred-request registry, and a once-daily sweep workflow timed to DeepSeek's off-peak window
(review/33 §10.7) — no caller needs its own retry cadence to eventually get a result. The daily sweep
reuses one decoded registry snapshot for selection, expiry pruning, and its final pending count
(GH#1020), avoiding repeated B2 reads without changing the registry schema. Provider batch
capability is deferred until a real batch-capable provider is confirmed. The city-onboarding consumer
(`citypods/discovery/classify.py`) requires a free, immediate result (`allow_paid=False`, no deadline) —
it acts on the result synchronously and already owns its own daily retry for "not eligible now"; the
scorer/evaluation consumer forces an explicit paid or free model via a singleton allowlist. The R5 topic
tagging consumer is the intentional non-interactive exception: its committed `tagging.llm_mode: dispatch`
setting uses the generic Worker transport so the Actions runner enqueues work and the deferred sweep
reconciles it later. R13 must not move provider-specific scheduling policy into either Worker.

**LLM backend + Rate-limited LLM dispatch Worker (new, Infra — ROADMAP R2 and R10).** Matured to L3 —
full design in [`review/27`](27-llm-backend-and-provider-routing.md): a LiteLLM-backed `Backend` adapter
running three providers (Gemini, DeepSeek, Mistral) under a windowed-recency allocation policy reusing
H5's ordering engine, chapter-boundary retrieval-scoped chunking, and a Cloudflare Worker (R10) that
paces requests to tightly rate-limited providers from the edge rather than idling a GitHub Actions
runner. The untrusted-output rule (all LLM output labeled, cached, never overwriting the official
record, [SECURITY.md](../SECURITY.md)) applies from the first call. **Quality assurance is two separate,
complementary designs, each broken out into its own doc 2026-07-17:**
[`review/34`](34-llm-quality-tournament-champion-routing.md) — a three-way round-robin tournament with
cost-gated per-verb champion routing (a weekly GitHub-issue ticket + checkbox approval flow), for
deciding which *provider* is best; and [`review/35`](35-llm-confidence-calibration-human-review.md) — a
per-candidate ground-truth calibration matrix (implemented, shipped as part of R5), for deciding whether
one *specific candidate* at its own reported confidence is trustworthy. Neither replaces the other.

**Agenda text extraction (new, Infra — ROADMAP R3, inserted 2026-07-14, narrowed 2026-07-16).** Matured
to L3 — full design in [`review/29`](29-agenda-text-extraction.md): extracts plain text from
`ep.links["agenda"]`/`["agenda_portal"]` (R11's discovery output) via `pypdf` (PDF) or `beautifulsoup4`
(HTML) — both new, output-affecting dependencies per `review/22`'s contract — into a content-addressed
`agenda_text_url` sidecar under `AGENDA_TEXT_PIPELINE_VERSION`, mirroring the existing
transcript-sidecar/backoff conventions exactly (`transcript_words_url`,
`transcript_timeout_attempts`/`_last_attempt`).

**Corrected and expanded, same day: backup/packet material is now in scope, stored as a genuinely
separate artifact.** The original draft excluded it citing "hundreds of pages or multi-GB" — checked, and
that number never had a real citation (the only "multi-GB" claim in this session's research describes
source *video* files, not agenda packets); the exclusion itself didn't survive scrutiny once corrected.
The real bound is non-OCR extraction (unchanged, still a non-goal): image-heavy exhibits already
contribute ~0 extracted characters regardless of the source file's byte size. Per the maintainer's
explicit requirement, backup text is a **fully separate sidecar and pipeline version**
(`agenda_backup_url` / `AGENDA_BACKUP_PIPELINE_VERSION`, its own `ARTIFACT_BLOCKS` entry), so "send just
the agenda," "send one item's backup," and "just link to it" (backup URLs are always populated even when
text extraction fails, for exactly this show-notes/HTML-rendering use case) are independent operations,
not different views over one blob. Sourced three ways with three different confidence levels: CivicClerk's
already-coded-but-unwired `agenda_packet` link (whole-meeting), `pypdf.extract_uris()` on the
already-fetched agenda PDF for internal per-item links (order-based chapter attribution, stated as a
heuristic, no new dependency), and — proposed as an R11 follow-on, not this item — Legistar's structured
per-item Attachments API. Runs as an ordinary feed-only Stage (no dedicated H6b lane) after `AudioStage`
and after R11's link-attachment point, before R5's `TagsStage`; both new `ARTIFACT_BLOCKS` entries
(`agenda_text`, `agenda_backup`) protect against regression by a scoped transcribe/align/diarize lane's
whole-record push. **Design-complete; execution should still wait on R11's real link coverage shipping** —
the maintainer's own stated bar for starting this item ("once R11 supplies agenda URLs for almost every
meeting") is a production-execution bar R11 hasn't hit yet (Part A's migration is designed but not yet
executed), distinct from this item's own design-readiness, which is now done.

**LLM-assisted city/agenda-source discovery (new, Infra — ROADMAP R12).** Matured to L3 —
full design in [`review/28`](28-llm-assisted-city-discovery.md). Pipeline: Tavily search (not LLM
recall) → one mode-aware `classify-civic-platforms` task verb against Appendix P's census → two-tier
verification (platform signature, then an end-to-end sample-episode resolution through the classified
provider's *existing* `fetch_episodes`/`resolve_media_url` — confirming a portal loads isn't confirming
media resolves) → propose, never auto-apply. **Scope was reinstated to cover new-city bootstrapping**,
previously deferred in the L2 pass as having "no existing manual process to automate against" — that
premise was wrong: this repo's own `add-city` issue template already promises exactly that, by hand;
R12 automates fulfilling it rather than inventing a new intake path. Two trigger surfaces: a daily
scheduled/new-city sweep plus weekly/manual auxiliary eligibility, and workflow dispatch for a fast
path. Evidence expires after 90 days. Maintainer approval binds to the exact bot-authored evidence and
only queues a fresh-main batch review PR; there is no direct-to-main path. The batch stages exact reviewed
paths, rechecks freshness and additivity, runs config tests, and reconciles issue state only after a
maintainer merges its PR. The companion Formspark-to-D1-to-GitHub/Discord/Resend Worker keeps requester
email private.

**Per-agenda-item cards, auto-summaries, and soundbites (#3/GH#155, #2, #15/GH#156 — ROADMAP R6).**
Matured to L3 — full design in [`review/30`](30-cards-summaries-soundbites.md). Verified before writing
that R2's `Backend` and R5's local tagging/calibration implementation are available: the discrete-label
paths (cards' tags, soundbite candidate selection) should reuse R5's generic evaluator
([`review/35`](35-llm-confidence-calibration-human-review.md)) and remain shadow-only until calibrated;
freeform summaries have no discrete label to calibrate against and depend instead on
[`review/34`](34-llm-quality-tournament-champion-routing.md)'s still-unbuilt tournament, if anything; the
non-LLM paths don't depend on either gate and can ship independently. **Cards:** extractive first
— chapter boundaries + a real transcript-text slice (not a synthesized "best" snippet) + R3's per-item
`agenda_backup` doc links joined by an explicit `chapter_index` when available, a direct payoff from R3's backup-material work this
session. **Corrected from the L1 sketch:** no vote-tally or minutes-parsing code exists anywhere in this
codebase, so "action, vote" drops out of a first cut — a card shows what was discussed, not what was
decided, until Phase F's real vote capture exists (R7's own sketch already scoped that the same way).
LLM one-liner reuses the `summarize` verb with a `scope: "item"` mode rather than a new verb, matching
the mode-aware-verb precedent R12 established. **Summaries:** inline `Episode` fields, not a sidecar —
a deliberate break from this item's other artifacts, justified by size (a few sentences, not thousands of
characters); renders as an additional labeled block on meeting pages/search, never substituted into the
feed's own `<description>`. **Soundbites:** the already-built `extract_clip`/`ClipArtifact`
(`citypods/clips.py`) has zero callers anywhere in this codebase today — this item is its first real
consumer. Two outputs from one selection decision: the `<podcast:soundbite>` RSS tag (metadata only, no
clip file needed, mirrors the existing `podcast_transcript` emission pattern exactly) and a standalone
extracted clip feeding the future Phase E highlights reel, built eagerly since the marginal cost is
near-zero once a range is picked. A longest-chapter heuristic ships free of any new dependency; a
"longest public-comment turn" variant closer to the original wording needs diarization (R7, not yet
shipped) and is deferred, not silently assumed available.

**Front-end design cycle, accessibility, and funding link (#55/#20/#54, #50, #16 — ROADMAP R8).** Matured
to L3 — full design in [`review/32`](32-frontend-design-accessibility-funding.md). First checked whether
the bare `#N` references were real GitHub issues (they're not — `gh issue view` on each resolves to
unrelated, already-closed feed-health issues; this project's older internal backlog numbering, same
system R6's cards/summaries/soundbites used before their `GH#` companions existed).

**Corrected same day: Part A specifies a design process, not a prescribed visual identity.** A first pass
drafted one specific redesign in full — a chevron treatment, particular icons, a complete color/type
system, built and shown as a live mockup. Maintainer correction, on two counts: this branch produces
roadmap/design documents, not the actual visual design; and separately, a single boilerplate-avoiding
identity handed down in prose is still the wrong shape here — a real identity benefits from seeing
genuine, distinct options side by side, not from being locked into one direction described months before
anyone builds it. Part A now specifies the process itself for whoever implements this later: ground the
direction in this project's own subject matter (timecodes, agendas/minutes, docket structure — not
generic "civic tech" or "podcast app" references), draft at least 2–3 genuinely distinct options, check
each against a concrete checklist of identifiable AI-generated-design patterns (specific combinations
like warm-cream-background + generic-serif + terracotta-accent, not "using a serif" in the abstract —
also near-black+neon-accent, purple-blue gradient heroes, Inter/Space Grotesk as "the safe font",
everything centered, `rounded-lg` everywhere), mock up the survivors against real site content, and
present them for the maintainer to choose from before any template changes. The one direction drafted
live this session is preserved as a worked example proving the process produces something distinctive —
explicitly not the chosen design. What stays fixed regardless of which direction wins: the real
current-state gaps found by reading the actual templates (the "accordion" is already a native,
keyboard-accessible `<details>`/`<summary>`; audio/video labels are today a bare `· audio`/`· video` text
suffix; subscribe buttons have zero iconography at all), Apple's official "Listen on Apple Podcasts"
badge policy (confirmed safe to use verbatim per its own published guidelines), the Overcast/Pocket
Casts/Castro licensing gap (no independently-verified official asset, so those fall back to a neutral,
non-trademarked glyph), and the icon-only-controls-never-acceptable accessibility constraint.

Accessibility (Part B, independent of Part A's process) gaps found by reading the
markup, not generic checklist advice: no `aria-live` region on three dynamic updates (search-result
count, play-state change, copy-RSS feedback), no skip-to-content link — plus WCAG contrast ratios
computed precisely from this project's own CSS custom properties (light-mode muted text 4.83:1, dark-mode
7.27:1, both passing AA) rather than eyeballed.

**`<podcast:funding>` (Part C) got a real platform decision, same day.** Split by audience: **GitHub
Discussions** for dev/API-style support (feed broken, integration questions — already the audience's
habit, zero new account), **Ko-fi + Discord** for sponsors/community/general feedback (Ko-fi's Discord
role-sync is native and first-party; GitHub Sponsors has no official Discord integration and would need a
self-hosted third-party bot, so it stays available as an alternative payment method rather than the one
wired to community). `<podcast:funding>` now points at a new self-hosted `/support/` page — not a
third-party link-in-bio tool — listing both surfaces, with `City.funding_url` defaulting from a new
site-wide `site_config.yml` value instead of needing per-city setup. GitHub→Discord activity updates use
a native, zero-code webhook; a scheduled `ROADMAP.md`/`CHANGELOG.md` digest bot (matching the
champion-stats-ticket/feed-health-digest pattern already used elsewhere) is flagged as a real future item,
deliberately not designed in this pass. **Part A also picked up a forward-looking constraint**: Substack
is a real candidate for a future subdomain-hosted digest/newsletter layer (confirmed: a one-time $50
mapping fee, not recurring, via the same Cloudflare DNS infrastructure this project already runs) —
whichever visual direction Part A's process eventually lands on should stay reproducible within
Substack's own customization surface, not depend on custom chrome only a fully bespoke template could
render.

**Sequenced after speaker diarization** (above, R7, matured to L3 this session) unchanged from the L1
sketch's own reasoning — the design pass needs a real answer for speaker-attribution UI before locking a
layout.

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

**Speaker diarization + minimal attendee extraction + per-speaker pages (#7, #14, `review/25` §2.3 #11
— ROADMAP R7).** Matured to L3 — full design in
[`review/31`](31-speaker-diarization-attendee-extraction.md). Verified before designing further: the
execution-backend interface (§5.5) already includes `"diarize"` in its `Task` Literal since H13/H14b/H14c
shipped, and H6b's separate-ASR-runner blocker is cleared — this item does **not** need to build or wait
on backend dispatch, only give it a real adapter (`citypods/diarize.py`, pyannote first with a
WeSpeaker benchmark) wired into the already-reserved-but-inert `diarize`
lane/`speakers` block. Native diarization output unifies with the existing provider-diarize `speakers_*`
schema (built for PT-PR6) rather than a parallel one; meeting-wide identity reconciliation uses
cross-window embedding matching, not naive per-chunk concatenation. Attendee extraction reuses R3's own
PDF/HTML extraction functions against a newly-wired `links["minutes"]` — the identical one-line gap
`agenda_packet` had. **Calibrated provisional automatic attribution** supersedes the original
human-confirm-only gate: two golden references from distinct meetings are required, only recurring
officials are eligible, and a body/engine needs 30 reviews over 30 days at 95% precision before it may
publish. Later minutes silently reassign within the official roster or remove the name. Per-speaker pages
link named grounded quotes back to their source meeting/time, while profiles and embeddings remain private
state. H9 (deferred, not blocking) is flagged as a real candidate for reopening once this item's own cost
profile is measured — not treated as permanently settled.

### §5.2 Phase E — Engagement & Distribution

**Social syndication bot (NEW, review/25 §2.4 #13) — first post-1.0 priority.** *Problem:* near-zero-cost
reach beyond the site itself. *Approach:* an automated Bluesky/Mastodon/ActivityPub bot posts a clip +
quote + source link per soundbite highlight (#15/R6). *Adjacency:* `clips.extract_clip` already exists
and is confirmed unwired (no consumer today); soundbites (already scheduled in Phase R, R6) supply the
selection logic. This is the cheapest item in the whole post-1.0 backlog — it needs no new capture
infrastructure, only a small poster/formatter against artifacts R6 already produces — which is why it's
sequenced ahead of the rest of this table rather than waiting for the heavier digest-pipeline items below.
*Tradeoff:* moderation/defamation care on anything characterizing named individuals, same as the
"national highlights" reel below; keep it strictly quote/clip-sourced, never editorialized.

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

**Topic/region roll-up feeds — pre-generated combos (#12/#13/#17).** *Problem:* "all zoning items in
Texas," "everything my city council did on housing this year," "every meeting where an item was tagged
'budget' across the whole catalog." *Approach:* a **curated set** of pre-generated static feeds/pages,
each a fixed topic × region/city × (optionally) date-range combination — e.g. `zoning + TX`,
`housing + Denton, TX`, `budget + statewide, current fiscal year` — plus **OPML** export so a reader can
subscribe to a bundle at once. Pure static output, no DB, no Worker; ships the same way as the rest of
Phase E once tags (#4/R5) exist to filter by. *Why this may be worth building even before it's exposed
publicly:* the underlying mechanism — filter/aggregate records by topic + city/region + date — is the
**same internal plumbing** the weekly look-back digest and "national highlights" reel (above) need to
pull their content together. Building it as a small reusable query-and-render helper, rather than
one-off code inside each digest generator, pays for itself even if the public-facing roll-up-feed page
ships later. *Tradeoff:* combinatorial explosion if the curated combo set grows unbounded — keep the
published set curated/human-picked, not every possible cross-product.

The **fully-general** custom-query builder (arbitrary reader-chosen filters, not a curated combo) is a
separate, later item that needs the Interaction seam — sketched in §5.5 alongside the other
seam-gated/review/25 items, not here.

**Privacy-respecting download analytics (GH#125).** *Approach:* OP3-style aggregate, self-owned
analytics-prefix subdomain; no per-user tracking. Informs which feeds/cities to invest in.

### §5.3 Phase F — Pre-Meeting Foresight

**Upcoming-agenda + staff-report scraping.** *Problem:* shift from after-the-fact to ahead-of-time.
*Approach:* extend providers with an `upcoming`/agenda capability (review/02 Change 8 capability set
already exists): Legistar/CivicClerk expose structured upcoming events + published files; Granicus/Swagit
need agenda-portal scraping. Persist as forward-looking records. *Tradeoff:* provider-dependent coverage;
strongest with Legistar (#31). The data spine for alerts + look-ahead.

**Deferred document-only meeting discovery.** Some provider calendars expose meetings before or without
a recording, together with agendas, packets, or minutes. Those rows could improve Phase F's upcoming
meeting notifications and calendar awareness, but public entries for meetings with no recording are
deliberately deferred: RSS remains playable-media-only, and no document-only episode pages are created
until Phase F defines their lifecycle and presentation. This does not remove metadata pages for an
existing recorded episode when its media is unavailable.

**Upcoming-meetings `.ics` calendar (#19).** Generate per-city/body `.ics` from upcoming events; reuses
the fetch above. Static, low-cost.

**Watchlists + topic alerts (R8 extension).** *Problem:* "tell me when parking minimums hit an agenda."
*Approach:* match upcoming agenda items against topic tags (#4) → per-topic RSS first (no accounts),
email/Substack later. *Tradeoff:* RSS-first keeps it PII-free; precision depends on tag quality.

**Backup-material (packet) analysis.** *Problem:* the real decision detail is in staff reports/packets.
*Approach:* fetch the published packet (provider capability), extract text (PDF), and produce a
structured/LLM "what's being proposed" brief — cost-gated, cached, **additive + labeled**. *Tradeoff:*
PDF parsing + LLM cost; never overwrites official docs. **A minimal extraction-only slice is pulled
forward to ROADMAP R3 (2026-07-14, gating 1.0)** — the fetch-agenda-PDF + extract-text capability, with
no LLM synthesis, feeding R4's search index and R5's tag generator. The richer structured/LLM "what's
being proposed" brief described here stays fully post-1.0 in this Phase-F item; only the raw-text
extraction moves up.

**Legistar provider (#31) — InSite API.** Rich structured data: agendas, votes, rosters, upcoming
events. Unlocks #8/#14 and high-quality foresight. *Approach:* standard new-adapter work + SSRF
allowlist + fixtures. *Distinct from Phase R's calendar provider* ([`review/15`](15-legistar-catalog-provider.md)),
which scrapes `Calendar.aspx` solely to extend Granicus video coverage past the RSS view-cap; the
InSite API adapter and the calendar scraper are independent and can coexist.

**Vote/roll-call (#8) + attendee (#14) extraction.** From **platform metadata** (CivicClerk per-member
tallies) + scraped **released minutes** — **never inferred from audio**. Shared "minutes-ingestion"
component. ~$0 (parser). **A minimal attendee-only slice is pulled forward to ROADMAP R7 (2026-07-12),
gating 1.0** — just the name list of who was present at a given meeting, parsed from released minutes,
so speaker diarization (§5.1) has ground truth to match voice clusters against. That slice does **not**
need platform per-member vote tallies, roll-call linkage, or the entity model (§3.4 of review/25) — it's
the smallest possible "who was in the room" list. Vote/roll-call extraction and the richer
platform-metadata/entity-linked attendee form stay fully post-1.0 in this Phase-F item.

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
| **Current Phase R** | R4 runs the search-size spike and launches transcript search partitioned by city/source with lazy loading and byte/memory budgets. Keep per-PR artifact previews and preserve the production `github-pages` environment's verified `main`-only policy; this is release hardening, not a separate scaling tranche. |
| **Before systematic breadth onboarding (~25 cities)** | Promote **S0 measurement** only: request/byte/useful-time telemetry, current call graph, and synthetic 10/100/500-city harness. Update `ROADMAP.md`, mature that tranche in `review/16` to L3, and cut issues at promotion time. |
| **Before ~50 cities or redundant provider-call trigger** | **Promoted and implemented in GH#1014 (2026-07-25):** `SourcePipeline` now invokes adapter validators, persists conservative TTL/full-refresh metadata and normalized UID-keyed input fingerprints, and filters heavy planning to dirty episodes; append-only records and provider-error fallback remain authoritative. The remaining S1 topology work (a separately scheduled refresh producer and fully records-only Audio/ASR/render workflows) is tracked by GH#1023 and later S2 slices. |
| **Before ~100 cities or state/empty-job trigger** | **S2 state-sync slice implemented in GH#1015 (2026-07-25):** versioned root manifest, digest-targeted reads, dirty-path registration, explicit tombstones, and safe full-sync fallback. **GH#1021 (2026-07-25) now supplies the Audio demand-planner/zero-variable-worker matrix:** one canonical preflight restores state once, fingerprint-validates the source-atomic plan, emits only positive-load shards, and publishes an explicit no-op for an empty cycle. |
| **Before ~250 cities or repeated-transfer/search trigger** | Promote **S3 selective source cache + bounded adaptive controls + dirty render/search partitions**. Cache only demonstrated high-value providers/failure classes. |
| **Before 500 cities** | Promote **S4 rehearsal/readiness gate** and update the practical Actions ceiling from measurements. |
| **~1,000 cities or two sustained migration signals** | Begin the off-Actions scheduler/media-worker adapter; expected crossover remains ~1,500–2,500 cities. This stays deferred until triggered. |

Metric gates override city guideposts: any artifact lane redundantly polling provider lists; shard state
downloads >2× assigned bytes or broad hot-path listings; empty heavy jobs >5% or useful-work ratio
<80%; repeated media downloads >10% of provider bytes; city search partitions above the 1 MB target
(2 MB hard warning); or the sustained multi-signal migration gate in `review/16` §14.1. Crossing a
round-number city count without the corresponding pressure does not force promotion.

**State-store backend + Interaction seam (records → managed SQL, merged with the review/25 §3.1
"Interaction seam" proposal; post-1.0, L1).** *Problem:* everything the static architecture serves is
read-only-at-serve-time. Alerts/watchlists (Phase F), a public data API + bulk export, personalization,
the **full** custom-query feed builder (below), and semantic search at scale all need *state written at
request time* or *computation at query time* — the Jamstack model structurally cannot do this, and
there is currently no seam for it (unlike storage/compute, which have clean Protocol+registry seams).
*Approach:* one new port — an **Interaction backend** — mirroring the existing `storage`/`compute` seam
pattern, `local`/`none` default so 1.0/dev are unaffected, **Cloudflare Workers** as the first real
adapter (the granicus-media-proxy Worker already proves the deployment path; R2 is already the
coordination backend). Concretely: **D1** (the review/17 records→SQL target, backing the API/query
features), **Vectorize** (semantic search), **Queues + Durable Objects** (alert fan-out, subscriber/
watchlist state), **KV** (hot config). Design the **entity-model schema** (`Person`/`Body`/`AgendaItem`/
`Vote`/`Document`, review/25 §3.4) at the same time the SQL store is designed, even before it's
populated — retrofitting it after an API ships is far more expensive than reserving it now. **Invariant
(non-negotiable):** the public record stays static, free, and un-paywalled; the dynamic tier is strictly
additive and must never become a required path to read a meeting — Pages keeps serving the archive even
if the Worker tier is down, the same "degrade to static" discipline the storage router already uses.
*Tradeoff:* real new infra (a Worker deployment, a subscriber-data trust boundary, D1/Vectorize/Queues
billing) vs. designing it once, together, instead of a records-only SQL migration now followed by a
second redesign for the Worker tier later. *Trigger:* federated cross-catalog query need, a public
query API, a city/source search partition exceeding the client index budget, or the fully-general
custom-query builder (next) — see [review/17 §1.4](17-state-store-backend-evaluation.md). Not yet
promoted past L1/sketch; break out to its own `review/NN` when it becomes next-up. **Unblocks:** Phase
F's watchlists/topic alerts (email/push channel specifically — the RSS-only version doesn't need it),
the fully-general custom-query feed builder (next), and two ideas review/25 flags as NEW/unadopted
(a public data API, semantic search past a static nearest-neighbor index) if they're ever taken up —
none of these ship before this seam exists.

**Fully-general custom-query feed builder (#12+#13, Interaction-seam-gated — split from the
pre-generated-combos version, which is static and lives in Phase E §5.2).** *Problem:* the pre-generated
roll-up combos only cover combinations someone thought to curate and publish; a reader who wants an
arbitrary filter — "just these three tags, this one city, the last 90 days" — has no way to build one
without a request-time query surface. *Approach:* consumes the Interaction seam above directly — a
Worker-backed query handler over the D1/records store, the same handler [review/25 §3.3](25-future-features-and-architecture.md#33-search-that-outgrows-the-client)'s
graduation path names for search past the static client budget. *Disposition:* genuinely separate infra
scope from the static pre-gen version, correctly post-1.0, and one of the seam's named triggers rather
than a feature that can pull the seam forward on its own — it waits for the seam, the seam doesn't get
built just for it.

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
tasks (tag list, summary blob, soundbite timecodes). The callers (`TranscriptStage`, current `TagsStage`
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

**First consumers.** The LLM adapter itself now lands as its own dedicated infra item — **ROADMAP R2**
(inserted 2026-07-14, ahead of both feature consumers so neither builds it under its own time pressure).
R5 (topic tags / Strong Towns lens, [`review/14`](14-topic-tags-strong-towns-lens.md)) and R6
(auto-summaries + soundbite selection, §5.1) are the first *feature* callers of the `tag` / `summarize` /
`soundbite-select` task verbs against that adapter. H9 (free transcription-offload evaluation) is the
first exercise of the GPU backend path.

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
Anthropic, Deepseek, Gemini, OpenAI, Together) lands with **R2** (dedicated infra item), ahead of its
**R5/R6** feature consumers. Self-hosted
Mac-mini + AWS GPU backends stay post-1.0 (no hardware yet) — each is then a single adapter against the
same interface.

---

## §6. Deferred backlog (ongoing)

Items intentionally not in a near-term phase; revisit as scale or demand warrants. (Enumerated in §4
"Deferred backlog".) Notable rationale: **directory index sharding (#42)** remains deferred because
per-meeting pages make meetings independently crawlable; promote it only if `review/16`'s directory
payload budget is crossed. This is distinct from R4 transcript-search partitioning, which is part of
the launch design in `review/13`. The **DerivedArtifact refactor** (review/02 Change 5) is now
**justified** — H12 (shipped, [PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253))
added the third derived-artifact type (audio M4A · transcript VTT · **word-JSON**), the YAGNI trigger it
was waiting on — so it moves from deferred to "do opportunistically now that H12's storage
plumbing lands"; **full video / off-Actions media** are explicitly out of scope now (§8); **hosted DB**
stays deferred *except* the scoped **Phase-R records→SQL** item, now promoted via
[`review/17`](17-state-store-backend-evaluation.md) (federated query / query API / state integrity).
**Archive-backfill:** promoted by the maintainer on 2026-07-26 as the bounded body-aware tiered
retention implementation in [review/39](39-body-aware-tiered-retention.md); no longer Deferred.
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
