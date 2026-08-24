# Roadmap

Public, maintainer-curated **near-term** direction: converting US city-meeting archives into
subscribable podcast feeds + a searchable, civic-research directory. Priorities use a **0 = highest**
scale; lower number = sooner.

> **Status:** beta, single-maintainer development. Issues are filed **just-in-time** for the current
> working set rather than all at once. This file is **forward-looking**; the history of what shipped
> lives in [CHANGELOG.md](CHANGELOG.md), the long-horizon direction in [VISION.md](VISION.md), and the
> full design + the exhaustive backlog in **[`review/11`](review/11-technical-design-roadmap.md)**.
> The backlog opens for outside contribution after **1.0** (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Recently shipped (summary)
Timeline/EDL foundation; **#52** append-only content permanence; audio-cleanup band (**#22** silence
trim, **#21** loudness, **#23** host-all, **#122** concat, clips); **#1/#110** ASR transcripts (reuse
provider transcripts first, self-host the rest); **#11** `<podcast:transcript>`; **#124** status
dashboard; durable bucket-backed state; SSRF gate; feed-health audit; endpoint contracts; resource
projection + admin page. Full history → [CHANGELOG.md](CHANGELOG.md).
Recently shipped Phase H reliability work: **H10** ASR alignment fallback fix (PR #232), **H8**
runner resource guard (PR #235), and **H11a** deploy resilience — native work gate + one-slot audio
lane + concurrency tuning + Retry-After fix (PRs #239/241/242/243/244/246/247). Phase H follow-up:
bounded-memory multi-mic audio mastering (streaming speech leveling + measured linear loudnorm) and
**H6a** manual ASR benchmark workflow (PR #256). Phase H observability/scheduling: **H1–H4** docs +
projection + validation gate + feed-health triage; **H5** backlog manifest + prioritization policy +
global newest-everywhere-first enrich queue (PRs #263/#264/#265). The current unreleased reliability
change adds a 4-hour local faster-whisper admission ceiling: longer recordings remain queued for H14
external workers instead of repeatedly overflowing onto and terminating a hosted runner. **Supply-chain
/ reproducibility (Phase-R foundation, umbrella [#804](https://github.com/BashfulBits/city-meeting-podcasts/issues/804)):**
a repo-wide **dependency pinning & update policy** ([`review/22`](review/22-dependency-and-reproducibility-policy.md),
PRs #805/#806/#807) — version-pinned Python `constraints/*.txt`, all Actions SHA-pinned (#734), HF model
revisions pinned (#498), Renovate two-lane update flow + CI guards — plus **H14b Modal and H14c Beam**
(#807/#808) live external transcription workers, tuned by **H14d**'s production pacing defaults.
**H15 transcript-quality is now fully shipped too:** L1 evidence capture, L2 independent forced
alignment, L3 human-gold calibration/trend reporting, and the `/admin/status` trust panel. Rounding out
Phase H: **H16** Granicus proxy validation + circuit-machinery removal, **H18** timeline/audio integrity
repair, **H19** in-Actions transcribe migration to the shared pull/claim contract, **H21** duration-field
consolidation, a unified storage-reclaim/lifecycle backstop, and **H4 stale-feed lifecycle + provider
migration continuity** (PRs #976–#978/#990): four durable lifecycle states, archive-preserving source
cutovers, native per-feed incident cohorts, maintainer commands that open review PRs, and live conversion
of legacy GH#774 to children #979–#989.

## Current phase: **H — Hardening & Efficiency** (next up)
Stabilize and maximize the throughput of what just shipped *before* layering on new user-facing
features. Detailed design: [`review/12`](review/12-hardening-and-efficiency.md).

> **Catalog complete (updated 2026-07-12).** As of a 2026-07-12 pass cross-checking every Phase H
> catalog row against actual closed GitHub issues and merged PRs, **H1–H21 (and storage reclaim) are
> all Shipped** — several rows (H14d, H16, H18, H19, H21, storage reclaim) had their underlying issues
> closed and PRs merged between 2026-06-20 and 2026-07-10 without the catalog-flip step of the
> doc-update contract keeping pace; that sync is now done (see [`review/11`](review/11-technical-design-roadmap.md)
> for per-item PR/issue links). **H9 combined-throughput evaluation was deferred to backlog on
> 2026-07-10** after H14d's live telemetry and current capacity arithmetic already showed the 80-feed
> one-month free-tier backlog gate is met with margin; **H20** stale-claim hardening was deferred the
> same day as useful-but-not-launch-required polish.
> **Not yet verified:** this confirms the Phase H *code* catalog is complete — it does not confirm the
> operational exit criteria below (≥95% run success, zero stalled feeds, declining backlog ETAs), which
> need a live read of `run_history.jsonl`/the status dashboard over a trailing 14-day window before
> Phase H is declared formally closed.
> Runtime/dependency update automation (**R9**) is the final release gate; its foundation is shipped,
> with activation and recurring FFmpeg update work still open, and it now lives in
> **Phase R**, not Phase H: `.github/renovate.json5` is committed but the Renovate GitHub App has not
> yet been activated on the repo (zero Renovate-authored PRs, no dependency-dashboard issue as of
> 2026-07-12). Completing R9 is the final gate to 1.0.
> The local-ASR duration guard is a narrow H14 reliability prerequisite, not a promotion of later
> scaling work: ship it now, then continue with Modal and Beam workers that explicitly support audio
> longer than the local ceiling.
>
> **LLM dispatch Worker — R2 quota, not CPU, is the live constraint (2026-08-14).** PR #1219 measured
> production `cpuTime` by deployed version and cut it from P50 `11` to `8` ms, raising
> `BATCH_CONCURRENCY` to 2 for double throughput (N=3 measured worse *per request* than N=2, so 2 is
> the optimum). **Deployed configuration is `1/1`; `2/2` is this PR's candidate and rolls out when it
> merges** — the `2/2` production run was a time-boxed canary that CI has since superseded. The open item is quota, not speed: R2 Class A operations bill **per account**, so the
> dispatch Worker competes with the H17 coordination plane for one 1M/month free tier and already
> consumes **~43% of it** at current throughput. The Worker is the one component not following the
> `COORDINATION_PREFIXES` rule (CAS state on R2, everything else on B2) — it keeps multi-megabyte
> prompts on R2. **Next: fold `locks/cron.json` into the rate ledger, then split the canonical record
> so prompts, results and ready markers move to B2** (control state stays on R2 for the enqueue
> idempotency CAS), which roughly doubles the R2-bounded ceiling from ~6,700 to ~11,100 jobs/day (26% of the tier at today's volume). 50,000 jobs/day needs the control record off R2 entirely — that is the Durable Object step, not this one. Batch enqueue/poll endpoints and the
> Workers-Paid-versus-Durable-Object decision are both deferred until throughput actually demands them.
> Phasing and measurements: [`review/43`](review/43-llm-dispatch-cpu-reduction-plan.md). A
> maintainer-directed L2 parallel successor,
> [`review/44`](review/44-bounded-bundled-llm-dispatch.md), now specifies a separately deployable
> DO/B2 cron-pull path. Each existing one-minute Worker tick
> claims up to four jobs from a SQLite coordinator; it prefers distinct routes, then larger
> conservative jobs, and returns safe per-route waits across a 25-second dispatch window. It has
> explicit Free-tier caps and does not alter the current
> R2-backed production transport until its parity and canary gates pass.
>
> **Granicus media reliability follow-up (2026-06-16).** Endpoint issue #300 still reproduces when
> `contracts.yml` overlaps active `audio.yml`: Arlington's Granicus RSS/media/chapter checks pass, but
> ffmpeg receives HTTP 403 from `archive-video.granicus.com` on the GitHub-hosted runner. A local serial
> contracts run passes, so treat this first as aggregate Actions-runner Granicus concurrency rather than
> a broken URL. Recommended sequence: (1) lower `provider_distributed_leases.granicus.com.slots` from 6
> toward 2 and consider lowering `provider_rate_limits.granicus.com` from 2 to 1; (2) make endpoint
> `media-fetch` participate in the same coordination envelope (shared leases or skip/defer while Audio
> is active); (3) only if low/no-overlap runner fetches still 403, test browser-fidelity alternatives
> (`Referer`/`Origin`, direct `DownloadFile.php`, Granicus playback/HLS URLs) or move Granicus media
> fetching off GitHub-hosted runners. The 2026-06-20 paired transport result crossed that third gate:
> every direct GitHub curl/ffmpeg/header case returned immediate 403 while all exact objects succeeded
> from a Mac. The authenticated, allowlisted Cloudflare Worker then produced ffmpeg audio for all
> four same-runner samples; Fort Worth honored Range and its full 89.9 MB object validated locally,
> while the larger Arlington/Pflugerville objects authenticated successfully but ignored Range.
> A direct-first, one-attempt production fallback is implemented under the existing 1-local /
> 2-distributed coordination ceiling, with per-tenant Worker-fallback telemetry
> (`worker_fallback_attempts`/`successes`/`failures`) added to the circuit so usage is measurable in
> the build log and run summary. **Before merge/activation,** require one full production-recipe
> Arlington or Pflugerville encode from the isolated GitHub-hosted probe. **Then evaluate over the
> three post-activation `audio.yml` runs tracked by H16/GH#353:** the fallback is effective when those
> counters show successes ≈ attempts and failures ≈ 0 per Granicus tenant, Granicus
> `circuit_trips`/deferrals fall to ~0, and no new truncation backoffs or episode-identity changes
> appear. If direct stays 403 every run, evaluate a sticky Worker-preference; rollback is unsetting
> the two proxy secrets (no code change). Full criteria:
> [`review/12` §Granicus follow-up](review/12-hardening-and-efficiency.md#granicus-media-reliability-follow-up-gh300--39-follow-up).

> **Reprioritized 2026-06-08** after a build-log root-cause review: **H10 shipped in PR #232** and
> **H8 shipped in PR #235**; the remaining do-now reliability item **H11a** runs **ahead of H1–H5**.
> These fixes address what is turning Build & Deploy red on ~half of scheduled runs (runner
> starvation; H10 fixed the broken ASR `align` path). See
> [`review/12`](review/12-hardening-and-efficiency.md#build-log-root-cause-analysis-2026-06-08).

| Pri | Item |
|----:|------|
| **H1** | ✓ Shipped — Docs/roadmap/issue reconciliation (this doc set; close/narrow shipped issues — [PR #258](https://github.com/BashfulBits/city-meeting-podcasts/pull/258)) |
| **H2** | ✓ Shipped — Projection wall-clock fix — `per_run_cap` defaults to `None`; `sec_per_ep` calibrated from real encodes; `hours_hosted` bytes fallback; complete logical-run telemetry + structured defer reasons in `run_history.jsonl`/`run_events`; audio + transcript backlog ETAs in `build_status` ([PR #259](https://github.com/BashfulBits/city-meeting-podcasts/pull/259)) |
| **H3** | ✓ Shipped — **#53** feed-validation publish gate: `citypods validate-build` CLI + `deploy.yml` gate before Pages upload ([PR #260](https://github.com/BashfulBits/city-meeting-podcasts/pull/260)) |
| **H4** | ✓ Shipped — stale-feed lifecycle + provider-migration continuity ([GH#970](https://github.com/BashfulBits/city-meeting-podcasts/issues/970), [`review/37`](review/37-stale-feed-lifecycle-and-provider-migration.md), PRs [#976](https://github.com/BashfulBits/city-meeting-podcasts/pull/976)/[#977](https://github.com/BashfulBits/city-meeting-podcasts/pull/977)/[#978](https://github.com/BashfulBits/city-meeting-podcasts/pull/978)/[#990](https://github.com/BashfulBits/city-meeting-podcasts/pull/990)). Stable `source_id` preserves the append-only archive for historical-copy and forward-only provider cutovers; four-state lifecycle decisions live in reviewed YAML; stale feeds use native child incidents with automatic recovery/disposition closure; maintainer commands create deterministic review PRs; legacy GH#774 was converted without losing any row or `first_seen`. Existing core remains: catching-up suppressed, stalled → `WARN`, per-provider error-rate tracking, and transition-only audit comments. |
| **H5** | ✓ Shipped — Stage **backlog manifest** + configurable **prioritization policy** ([#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263)/[#264](https://github.com/BashfulBits/city-meeting-podcasts/pull/264)/[#265](https://github.com/BashfulBits/city-meeting-podcasts/pull/265)): `citypods/ops/workqueue.py` policy engine (windowed `recency`, `city_order`, …; prod `recency:{desc, within_days:30}`); derived work manifest + lease sidecar + `/admin/status` backlog-by-work-class; **global two-pass enrich queue** — newest-everywhere-first on-runner audio + decoupled async-ready transcript pass (transcribe/diarize go over-the-wall to external workers, H9/H6b). Whole-archive backfill split out as a separate opt-in. |
| **H6a** | ✓ Shipped — ASR **benchmark workflow** (`asr-bench.yml`, manual, PR #256): compares max/med/min model + beam-size + CPU-thread profiles before any backfill decision |
| **H6b** | ✓ Shipped — **Split audio + ASR into dedicated sharded workflows** (`audio.yml` + `asr.yml`, own `audio`/`asr` concurrency groups, `matrix.shard`=4) replacing the combined `enrich.yml`; `enrich --shard K/N`/`--source`/`--lane {audio,transcribe,align}`; cities filtered by source-atomic weighted `shard_assignment(source_key)` + scoped `push_state`/`reconcile_state` so shards don't clobber; `asr.yml` runs transcribe-only with the `asr-transcribe` dependency extra (forced-alignment `align` lane implemented but unscheduled and assigned `asr-align`); Granicus media fetches now also use cross-shard storage-backed leases so the 4 audio shards share one provider cap ([#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273)) |
| **H7** | ✓ Shipped — contributor/agent handoff docs (AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING + PR/issue templates) |
| **H8** | ✓ Shipped — throughput maximization on the free 4-core runner (PR #235): pinned ffmpeg `-threads`, memory/CPU admission guard, and a killable persistent local-ASR subprocess with per-episode timeout backoff (replacing abandoned native threads) |
| **H10** | ✓ Shipped — ASR alignment fix (PR #232), superseded by WhisperX known-text alignment: provider text is cleaned/remapped, gated at 90% raw coverage, and below-gate candidates return to the full-ASR queue |
| **H11a** | ✓ Shipped — **Deploy resilience**: native work gate + one-slot audio lane + concurrency tuning + Retry-After fix (PRs #239/241/242/243/244/246/247) |
| **H11b** | ✓ Shipped — Render-only `deploy.yml` (no ffmpeg/ASR; `actions: read` dropped) + heavy phase → new `enrich.yml` (own `enrich` concurrency group) **+ render stops persisting records** — `build()` gates `save_records`/`push_state`/`reconcile_state` off `--phase render` so the enrich workflow is the sole record writer (closes the lost-update race); `statesync` `only_prefixes=`/`full_run=` scope hooks ready for H6b ([#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272)) |
| **H11c** | **Graceful SIGTERM + mid-run checkpoint** (implemented, unreleased — [#386](https://github.com/BashfulBits/city-meeting-podcasts/pull/386)): a runner-level SIGTERM/GitHub-cancel now converts to the existing graceful-stop path (workers defer, the run still persists records + writes its `run_history` entry) instead of dying mid-queue; the enrich queue persists each source as the audio pass drains and again at the end (idempotent); interrupted runs are tagged in `run_history.jsonl` and exit `143`. Directly serves the Phase-H exit criterion "no exit-143 / lost-comms kills" below |
| **H12** | ✓ Shipped — transcript artifact rework (PR #253): clean segment-cue VTT for players + a word-level JSON sidecar for search/clips/diarization + version-aware gradual re-transcribe (fixes #249's word-per-cue regression) |
| **H13** | **GPU/ASR execution-backend interface** (+ `local` adapter) — the pre-1.0 "compute is pluggable" lock; `citypods/compute/` mirrors `storage/`. Do **first** (seam for H6b lanes + H14 adapters). LLM-API half of the interface lands with **R2** (dedicated infra item, inserted 2026-07-14 — see Phase R below), consumed by R5 (tags) and R6 (auto-summaries) |
| **H14** | ✓ Shipped — **External transcription workers — Modal + Beam** (free-tier-bounded async dispatch behind H13; `asr.yml` dispatches). H14a substrate ([#275](https://github.com/BashfulBits/city-meeting-podcasts/issues/275)): budget ledger ($0 guarantee), router + `DispatchCoordinator`, live H5 leases, `compute reconcile`, `compute_backend: auto`. The local guard defers known recordings above 4h when external dispatch declines; the separate runtime estimator still protects the Actions deadline. ASR shard weighting separates local duration cost from cheap dispatch, blocked, and in-flight work; a once-per-run planner restores B2 state once and publishes one immutable state/assignment artifact consumed by every matrix shard. **H14b Modal** ([#807](https://github.com/BashfulBits/city-meeting-podcasts/pull/807), GH#276 closed 2026-07-05) and **H14c Beam** ([#808](https://github.com/BashfulBits/city-meeting-podcasts/pull/808), GH#277 closed 2026-07-05): real Modal + Beam workers that install the runner's version-pinned deps + baked pinned Whisper model on a CUDA12+cuDNN9 base (dependency-policy umbrella [#804](https://github.com/BashfulBits/city-meeting-podcasts/issues/804)), support longer recordings with bounded memory, and report peak RSS/GPU VRAM telemetry. Live validation passed the pre-live checklist ([#706](https://github.com/BashfulBits/city-meeting-podcasts/issues/706), closed 2026-07-08). **H14d** ([GH#794](https://github.com/BashfulBits/city-meeting-podcasts/issues/794), closed 2026-07-08) turned that telemetry into production admission/chunking/pacing defaults. Local overflow is allowed only when both local guards pass; otherwise work remains queued, not failed. The locked episode-level `transcribe`/`align`/`diarize` interface remains unchanged; `diarize` implementation stays in Phase R and Mac-mini/AWS stay post-1.0. |
| **H15** | ✓ Shipped — **Transcript-quality metric** ([GH#391](https://github.com/BashfulBits/city-meeting-podcasts/issues/391), now closed): all three layers are live — L1 free acoustic-fit evidence on every production transcript/alignment run, L2 independent CTC forced-alignment scoring ([#883](https://github.com/BashfulBits/city-meeting-podcasts/issues/883)), and L3 human-gold calibration/trend reporting ([#884](https://github.com/BashfulBits/city-meeting-podcasts/issues/884)). Trusted `route_mode` now gates align-vs-transcribe routing per source/body, and `/admin/status` now includes the H15 trust panel via [PR #891](https://github.com/BashfulBits/city-meeting-podcasts/pull/891). Provider-transcript rollout through PT-PR7 remains implemented without invalidating ASR. |
| **H16** | ✓ Shipped — **Granicus proxy validation + simplification** ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353), closed 2026-06-24): direct-first Cloudflare Worker fallback validated across production Audio runs; the storage-backed rate-limit circuit breaker/parking/canary machinery was disproved and removed. Duplicate combined/per-board audio work is also fixed ([GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421)): entity-family shard affinity + run-local stable-uid/recipe coalescing, with no source-key migration or backfill. |
| — | **Body-aware tiered retention — implementation in progress** ([review/39](review/39-body-aware-tiered-retention.md), maintainer-authorized 2026-07-26): replaces the source-wide cap with independent per-body windows in the shared source store: 500 RSS-visible, 2,000 full-artifact/audio, and 10,000 metadata/non-audio. It promotes the previously deferred bounded archive backfill; feed-visible work remains first and the 501–2,000 cohort drains gradually under the wall-clock budget. |
| **H17** | ✓ Shipped — **Distributed work/control-plane substrate** — `RoutingStorage` + native R2 CAS, per-episode ownership-safe merge/planning, and the pull-ledger claim contract H14 workers consume ([GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390), closed 2026-06-25; `review/17` + `review/18`) |
| **H18** | ✓ Shipped — **Timeline/audio integrity repair** ([GH#495](https://github.com/BashfulBits/city-meeting-podcasts/issues/495) closed 2026-06-28; GH#795/GH#702 closed 2026-07-02). Feed-health uploads timeline/audio duration diagnostics, records carry `integrity.timeline_audio`, `/admin/status` reports repair queues, source-aware identity hashing fixes GH#495 tail-only trims, and repair actions re-plan/re-materialize/re-transcribe affected records without a full-catalog invalidation ([`review/20`](review/20-timeline-audio-integrity-repair.md)) |
| **H19** | ✓ Shipped — **In-Actions transcribe migration to the pull/claim contract** ([GH#831](https://github.com/BashfulBits/city-meeting-podcasts/issues/831), closed 2026-07-10): `asr.yml`'s transcribe matrix now runs identical `citypods compute run-internal-worker` jobs against the same Stage-2 lease ledger external workers use, retiring the static shard-plan/fan-in path. |
| **H14d** | ✓ Shipped — **GPU worker memory/admission optimization** ([GH#794](https://github.com/BashfulBits/city-meeting-podcasts/issues/794), closed 2026-07-08): provider-cycle dollar budgets, learned per-backend/task/GPU runtime coefficients, and preferred-day pacing turned first live Modal/Beam telemetry into production defaults (Beam `RTX4090`, Modal `L4`). |
| **H21** | ✓ Shipped — **Duration field consolidation + normalization** ([GH#868](https://github.com/BashfulBits/city-meeting-podcasts/issues/868), closed 2026-07-10): canonical `source_duration_seconds`/`served_duration_seconds` fields, shared accessors, pre-dispatch normalization, and a manual repair action ([`review/26`](review/26-duration-field-consolidation.md)). |
| — | ✓ Shipped — **Unified storage reclaim + R2/B2 lifecycle backstop** ([GH#496](https://github.com/BashfulBits/city-meeting-podcasts/issues/496), closed 2026-07-07): lifecycle-as-code R2 scratch expiry, double-confirmed auto-apply orphan GC, and a resurrection watchdog. |

Deferred from the active Phase-H queue on 2026-07-10: **H9** combined-throughput/routing evaluation. The current local 4-shard baseline plus H14d's chosen GPUs, budgets, and measured worker coefficients already indicate the 80-feed initial backlog clears within one month on free-tier capacity, so the dedicated benchmark/policy project moved back to backlog unless pricing, backlog shape, or diarization requirements change materially.

**Phase H exit criteria ("green").** Phase H is done — and Phase R may start — when, measured off
`run_history.jsonl` + the status page (instruments built in H2/H4):
- **≥ 95 % of scheduled Build & Deploy runs succeed** over a trailing 14-day window (no exit-143 /
  lost-comms kills);
- **zero feeds in `rehost-backlog:stalled`** — every backlog is `catching-up` with a finite, declining ETA;
- **audio + transcript backlog ETAs are computed and trending down** run-over-run at current inflow.
(These are proposed defaults — adjust the thresholds to taste.)

## Toward 1.0: **R — Research-Tool Surface**
Turn feeds into a civic-research tool. Design: [`review/13`](review/13-per-meeting-pages-and-search.md)
(pages + search) and [`review/14`](review/14-topic-tags-strong-towns-lens.md) (tags).

> **Scope (depth-first):** prove the full Phase R feature set (**R1, R4–R8** — everything except the R2/
> R3 infrastructure items and the R9 maintenance gate, neither of which are catalog features) across the
> **entire current city catalog** before onboarding new cities (VISION "Depth-first"). The pilot set *is*
> today's roster — not a hand-picked subset — so search index size and page volume stay bounded by the
> current ~85 feeds while the engine choices (e.g. Pagefind) are validated.

> **Reprioritized 2026-07-12 (maintainer decision): speaker diarization is fully pulled forward as R7,
> gating 1.0** — previously an L1 catalog item with no committed slot. Rationale: the front-end design
> cycle (R8) needs a real speaker-attribution taxonomy (labels, per-speaker linking) to design around,
> not a placeholder — see [`review/11`](review/11-technical-design-roadmap.md) §4/§5.1. **This also
> pulls forward a minimal slice of Phase F's attendee extraction (#14)** — diarization alone only
> produces anonymous voice clusters ("Speaker 2"); turning that into real names needs a "who was present"
> ground truth from official minutes. Only that minimal name-list slice moves up; the richer Phase-F item
> (vote-linked, full entity model) stays post-1.0. **Called out as a real scope increase, not a free
> reorder:** diarization is GPU/ML work (speaker-embedding models, meeting-wide identity reconciliation)
> and the attendee slice is a new minutes-parsing capability — both genuinely land before the 1.0 tag
> now, traded for a front-end design cycle that doesn't need a later redesign.

> **Reprioritized 2026-07-14 (maintainer decision): two infrastructure items inserted as R2/R3, between
> per-meeting pages and search.** R1 (pages) needs neither. R4 (search, was R2) and R5 (tags, was R3)
> both benefit from real agenda-document text as a search/tagging input, richer than the chapter-title
> proxy those designs otherwise use — and R5's LLM-assisted tagging path needs a working LLM adapter.
> Per H13's own precedent (build+prove the compute-backend interface before features depend on it), both
> land as dedicated infra ahead of their first consumer rather than under a feature's time pressure.
> **R2 (LLM backend) is real infra work, not a config flip** — provider choice, a cost/budget ledger
> mirroring H14d's provider-cycle dollar model, and prompt-management conventions all need deciding.
> Everything from the old R2 onward shifts down by two (R2→R4, R3→R5, R4→R6, R5→R7, R6→R8, R7→R9).

> **Added 2026-07-14 (maintainer decision): a rate-limited LLM dispatch item, numbered R10 but sequenced
> second in the table below, right after R1.** This is deliberate, not a mistake — the maintainer asked
> to avoid the renumbering churn a mid-sequence insert caused last time, so new items now get the next
> unused number and are positioned by an explicit note rather than by forcing the label to match table
> order. Needs to exist and be testable as an **asynchronous OpenAI-shaped enqueue/poll transport** by
> the time R2 is built, since R2's Mistral integration is the first thing that needs its `JobHandle`
> path. It is not a synchronous LiteLLM provider; LiteLLM remains in R2's Python adapter or an explicitly
> configured LiteLLM Proxy upstream — see [`review/27`](review/27-llm-backend-and-provider-routing.md).

> **Added 2026-07-16 (maintainer decision): "Legistar calendar provider" re-scoped into a broader
> cross-provider agenda & history network, numbered R11, sequenced third (right after R10, before R2).**
> Same no-renumbering convention as R10. Research found Swagit and CivicPlus each have a real
> agenda-management sibling under common ownership (OneMeeting for Swagit, via Rock Solid Technologies;
> CivicClerk for CivicPlus) — generalizing the existing, proven Legistar/Granicus mechanism to cover
> them closes the agenda-URL gap for two of the four current providers that otherwise have zero agenda
> data today. **R3 (agenda text extraction) now depends on R11 and narrows to text extraction only** —
> R11 owns URL discovery, R3 owns extracting text from what R11 finds. See
> [`review/15`](review/15-legistar-catalog-provider.md), which absorbs and expands the original item.

> **Added 2026-07-16 (maintainer decision): LLM quota/cost-window scheduling is R13, simplified
> 2026-07-16 to a stateless selection function + one shared CAS ledger.** This is a policy-layer
> companion to the shipped R2 adapter and R10 Mistral transport, not a replacement for either. It adds
> caller model allowlists (with scorer fan-out expressed as the caller looping singleton allowlists,
> no separate scheduler mode), a 4-field request contract (`allowed_models`/`allow_paid`/
> `deadline_at`/`purpose`), Gemini RPM/RPD/TPM quota accounting via a CAS-backed ledger (no dedicated
> Worker — see review/33 §7 for why Gemini doesn't need Mistral's pacing problem solved), and a
> DeepSeek off-peak dispatch preference. The quota ledger ships as the same object as review/27 §8's
> still-unbuilt $ budget ledger, not a separate one. Provider-neutral batch capability is deferred
> until a real batch-capable provider is confirmed (review/33 §9).
> Full proposal: [`review/33`](review/33-llm-quota-cost-scheduler.md).
>
> **Update 2026-07-17: R13 implemented**, plus one interface change found necessary during the build —
> a caller no longer distinguishes "nothing eligible yet" from "genuinely in flight at the Mistral
> Worker": both return the same portable `JobHandle`, completed later via `reconcile()`, a B2-backed
> deferred-request registry, and a once-daily sweep workflow timed to DeepSeek's off-peak window
> (review/33 §10.7). A real provider 429 now also reactively blocks that route (review/33 §7.1). City
> discovery (the only current caller) requires a free, immediate result — no deadline needed, since it
> acts on results synchronously and already retries daily on its own. Full changelog: review/33 §13.

> **Added 2026-07-12 (maintainer question → item): LLM-assisted city/agenda-source discovery, numbered
> R12, sequenced right after R2 (before R3).** Same no-renumbering convention as R10/R11. Prompted by
> asking whether R11's manual per-city discovery checklist (§B.2) — and new-city onboarding generally —
> could be automated: given a city name, search for its real website/portal links (not LLM recall — the
> same guessed-URL failure mode R11 already hit), classify the result against Appendix P's platform
> census, live-verify the candidate before ever proposing it, and open a GitHub issue with a checkbox
> for a human to approve applying the config. **Automates the research, keeps the approval step
> manual** — not literally "ingest without a manual step" as first framed, matching every other
> automation in this codebase (H4's audit-issue reconciliation, R2's champion-routing checkbox).
>
> **Promoted to L2, 2026-07-12 (maintainer request: "push toward L2, research the best answer for
> each"), full design in [`review/28`](review/28-llm-assisted-city-discovery.md).** Both open questions
> resolved by research, and the answers converged on one architecture change: search turns out not to be
> an LLM call at all. **Search mechanism: Tavily (dedicated search API), not native LLM-provider
> grounding** — Gemini's free-tier grounding is model-restricted (500 RPD on 2.5 Flash only; Gemini 3 has
> no free-tier grounding at all) and LiteLLM has confirmed bugs mixing search with structured output;
> Bing Search API is fully retired (Aug 2025); Brave killed its free tier (Feb 2026); SerpApi carries an
> active Google DMCA lawsuit. Tavily's free tier (1,000 searches/month, no card) comfortably covers this
> catalog. **Architecture: only the classification step rides `InferenceJob`**, as one new task verb
> (`classify-civic-platforms`) — the budget ledger/tournament are already Stage-agnostic, so this needed
> no new plumbing; the search step is a plain, non-LLM API call outside that interface.
>
> **Matured to L3, same day (maintainer request: "push further, ask me with anything you're not sure
> of").** Two remaining decisions were put to the maintainer directly rather than assumed: trigger
> cadence, and what checking the approval box actually does. The answers **reinstated new-city
> bootstrapping into scope** — previously deferred in the L2 pass as "no existing process to automate
> against," which was wrong: this repo's `add-city` issue template already promises exactly that, by
> hand. **Two trigger surfaces**: a quarterly scheduled sweep (+ `workflow_dispatch`) for cities already
> in the catalog missing an agenda source, and a workflow triggered off the existing `add-city` label
> that replies on the requesting issue rather than opening a new one. **Checking the checkbox commits
> directly to `main`** when a fresh-checkout additivity check confirms the change is purely additive (a
> new file, or new keys into a file that doesn't already have them), backstopped by a redundant
> zero-deletions diff-stat assertion; anything else falls back to a PR instead. This was verified
> achievable by checking this repo's *actual live* branch ruleset (`gh api .../rulesets` — only
> `deletion`/`non_fast_forward` enforced, no required-PR rule) rather than trusting `lock.yml`'s own
> comment ("branch protection blocks main"), which turned out to be stale. Also added: a second
> verification tier beyond "the portal loads" — an end-to-end sample-episode resolution through the
> classified provider's existing adapter, gating whether a config is ever offered as apply-able at all.
> Full design: [`review/28`](review/28-llm-assisted-city-discovery.md). Flagged explicitly as this
> codebase's first automation with write access to `main`.

> **Matured to L3, 2026-07-12: agenda text extraction (R3), full design in
> [`review/29`](review/29-agenda-text-extraction.md).** Extracts from `ep.links["agenda"]`/
> `["agenda_portal"]` via two new output-affecting dependencies (`pypdf`, `beautifulsoup4`) into a
> content-addressed sidecar under `AGENDA_TEXT_PIPELINE_VERSION`, mirroring the existing
> transcript-artifact/backoff conventions exactly. Two non-goals: OCR, and any LLM synthesis.
>
> **Corrected and expanded, same day: backup/packet material is now in scope too, stored as a fully
> separate artifact.** The original draft excluded it citing "hundreds of pages or multi-GB" — that
> number was never actually verified (the only "multi-GB" claim anywhere in this session's research
> describes source *video* files, not agenda packets), and the exclusion didn't survive the maintainer's
> follow-up questions. Per the maintainer's explicit requirement, backup text gets its own sidecar and
> pipeline version (`agenda_backup_url` / `AGENDA_BACKUP_PIPELINE_VERSION`), independent of agenda-only
> text, so "just the agenda," "one item's backup," and "just a link" (backup URLs are populated even when
> text extraction fails, for show-notes/HTML rendering) are all independently usable. Sourced from
> CivicClerk's already-coded `agenda_packet` link, `pypdf.extract_uris()` on internal PDF links
> (order-based chapter attribution, explicitly a heuristic), and — proposed as an R11 follow-on —
> Legistar's structured per-item Attachments API. **Design-complete; execution should still wait on R11's
> real link coverage shipping** — the maintainer's own bar for starting this item ("once R11 supplies
> agenda URLs for almost every meeting") is about production execution, and R11's Part A migration is
> designed but not yet executed, distinct from this item's own design readiness.

> **Matured to L3, 2026-07-12: R6 (cards, summaries, soundbites), full design in
> [`review/30`](review/30-cards-summaries-soundbites.md).** Verified against the live Pri table before
> starting — R4/R5 are already L3, so R6 was the next item actually needing work, not R7 as first
> guessed. Also checked directly rather than assumed: R2's LLM `Backend` is now shipped and R5's tag
> implementation is in progress; every LLM-assisted path across all three Parts remains flagged as
> depending on R2 shipping; the non-LLM paths ship independently. **Cards** correct a scoping error in
> the original sketch (no vote-tally/minutes-parsing code exists anywhere in this codebase — "action,
> vote" drops out of a first cut) and get a direct payoff from R3's backup-material work this session
> (per-item doc links joined by an explicit `chapter_index` when the provider/parser supplies one;
> flat packet links remain unattributed). **Summaries** are inline record fields, not a sidecar
> — the first artifact in this stretch of items small enough to justify breaking that pattern — and never
> touch the feed's own `<description>`. **Soundbites** give `citypods/clips.py`'s already-built,
> zero-caller `extract_clip` its first real consumer; a longest-chapter heuristic ships free of any new
> dependency, while a "longest public-comment turn" variant closer to the original wording is deferred
> since it needs diarization (R7, not yet shipped). Also fixed two stale "ROADMAP R5" references in
> review/11's diarization row, left over from before the R2/R3 insertion shifted numbering — corrected
> to R7 to match the live table.

> **Matured to L3, 2026-07-13: R7 (diarization, minimal attendee extraction, per-speaker pages), full
> design in [`review/31`](review/31-speaker-diarization-attendee-extraction.md).** Checked every
> dependency the L1 sketch named against the live codebase rather than trusting the sketch: H6b (its
> named blocker) is shipped; H9 was already deferred/closed (H14d's telemetry answered its question),
> flagged only as a real candidate to reopen once diarization's own cost profile is measured; and the
> execution-backend interface already includes `"diarize"` in its `Task` Literal since H13/H14b/H14c
> shipped — this item never needed to build backend dispatch, only a real adapter. That adapter
> (`citypods/diarize.py`, wespeaker ECAPA-TDNN) wires into an already-reserved-but-inert `diarize`
> lane/`speakers` block and **unifies with the existing provider-diarize schema** (built for PT-PR6)
> rather than a parallel one. Attendee extraction reuses R3's own PDF/HTML extraction functions against a
> newly-wired `links["minutes"]` — the identical one-line gap `agenda_packet` had. Identify-then-
> human-confirm only, never auto-named; per-speaker pages render only for confirmed speakers.

> **Matured to L3, 2026-07-13: R8 (front-end design, accessibility, funding link), full design in
> [`review/32`](review/32-frontend-design-accessibility-funding.md).** First confirmed the L1 sketch's
> `#55`/`#20`/`#54`/`#50`/`#16` references aren't real GitHub issue numbers — `gh issue view` on each
> resolves to unrelated, already-closed feed-health issues (this project's older internal backlog
> numbering, not GH issues, same system R6's items used before their `GH#` companions existed). Real
> gaps grounded against the actual current templates, not the abstract: the index page's "accordion" is
> already a native, keyboard-accessible `<details>`/`<summary>`; audio/video labels are today a bare
> `· audio`/`· video` text suffix; subscribe buttons have zero iconography at all.
>
> **Corrected same day: this doc specifies a design *process*, not a prescribed visual identity.** An
> earlier pass drafted one specific redesign in full (chevron treatment, particular icons, a complete
> color/type system, built and shown as a mockup) — maintainer correction on two counts: this branch
> produces roadmap/design documents, not the actual visual design, and separately, a single
> boilerplate-avoiding identity handed down in prose is still the wrong shape for this doc. Part A now
> specifies a concrete process for whoever implements this later: ground the direction in this project's
> own subject matter (not generic "civic tech" or "podcast app" references), draft at least 2–3 genuinely
> distinct options, check each against a real checklist of identifiable AI-generated-design patterns
> (specific combinations like warm-cream+serif+terracotta, not "using a serif" in general), mock up the
> survivors against real site content, and present them for the maintainer to choose from before any
> template changes. The one direction drafted live this session is kept as a worked example proving the
> process produces something distinctive — explicitly not the chosen design. Apple's official "Listen on
> Apple Podcasts" badge is confirmed safe to use verbatim regardless of direction; Overcast/Pocket Casts/
> Castro have no independently-verified official asset, falling back to a neutral glyph. Accessibility
> gaps (Part B, independent of Part A's process) found by reading the markup, not generic checklist
> advice: no `aria-live` region on three dynamic updates (search-result count, play-state change,
> copy-RSS feedback), no skip-to-content link — plus WCAG contrast ratios computed precisely from this
> project's own CSS custom properties (light-mode muted text 4.83:1, dark-mode 7.27:1, both passing AA)
> rather than eyeballed. `<podcast:funding>` (Part C) is a near-trivial channel-level tag + two new
> `City` fields.

| Pri | Item |
|----:|------|
| **R1** | **Shipped** (#897, 2026-07-13) — **#46/#157** per-meeting permalink pages over the append-only archive: playable meetings get player/transcript/chapters/agenda/deep-links; unavailable recordings retain civic metadata + canonical provenance with a clear no-recording notice and no broken player |
| **R10** | **Shipped** (#899, 2026-07-13; follow-up hardening on `main`) — **Rate-limited LLM dispatch Worker** (new, infra) — a Cloudflare Worker that paces tightly rate-limited LLM providers from the edge, exposing an OpenAI-shaped **asynchronous enqueue/poll transport** for R2's `JobHandle` path. Results land in R2 for the next scheduled run to pick up, reusing the existing artifact-ready/skip-and-retry pattern |
| **R11** | **Core implementation shipped** (#903/#904/#905, 2026-07-13) — **Cross-provider agenda & history network**: Granicus archive-first discovery, verified calendar composition/backfill, durable no-video calendar rows, and first-party Swagit agenda/minutes links are live. Remaining follow-up is broader vendor coverage (including OneMeeting/Agenda PE/CivicClerk cases) as catalog evidence warrants. Feeds R3 and, transitively, R4/R5 |
| **R2** | **Shipped** (#919, 2026-07-13) — **LLM backend**: LiteLLM owns provider translation and response normalization; direct routes return `JobResult`, while rate-limited routes enqueue through R10 and return `JobHandle` for later reconciliation. Provider choice, cost/budget ledger, prompt management, and the H13 LLM verbs are wired; R5/R6 remain its first feature consumers |
| **R12** | **Shipped** (#927, 2026-07-15; follow-ups #935) — **LLM-assisted city/agenda-source discovery**, L3 design in [`review/28`](review/28-llm-assisted-city-discovery.md). The implementation adds Tavily-grounded classification, SSRF-gated adapter verification, daily new-city evidence/weekly auxiliary eligibility, 90-day refresh, maintainer slash controls, an unsupported-provider tracker, one review-PR batch path, and a D1-backed Formspark/Discord/GitHub/Resend intake Worker. |
| **R13** | **Proposed — LLM quota & cost-window scheduler**, L3 design in [`review/33`](review/33-llm-quota-cost-scheduler.md): a stateless selection function (not a new service) above R2/R10; a 4-field request contract with caller model allowlists (scorer fan-out is the caller looping singleton allowlists); Gemini RPD/RPM/TPM quota via a CAS ledger shared with review/27's $ budget ledger (no dedicated Worker); DeepSeek off-peak dispatch preference. Batch capability deferred until a real batch-capable provider is confirmed. |
| **R3** | **Shipped** (#920, 2026-07-14) — **Agenda text extraction**: agenda/portal text plus bounded backup/packet material, minutes, per-item votes, and member-roster extraction are implemented as content-addressed artifacts; no OCR or LLM synthesis in the first pass. Feeds R4's search index and R5's tag generator |
| **R4** | **Shipped** (2026-07-14; commits `2f76744` + `60998a0`) — **#6 static client-side transcript/meeting search**, including deterministic cacheable per-source MiniSearch shards, agenda/backup/minutes/vote/roster fields, metadata-only unavailable recordings, and availability filtering |
| **R5** | **Implemented locally with unified calibration overlay** — **#4 topic tags / Strong Towns lens**: 37-source-backed flat tags, deterministic episode/chapter annotations, taxonomy-ordered episode rollups, chapter-aware meeting/search payloads, and a chapter-only LLM path over Gemini direct transport. Deterministic and LLM candidates share one persisted ledger; the initial LLM 12-review/90%-precision gate remains independent from the pre-labeler's later 50-review/95% overlay gate. The weekly mixed-source review packet defaults to 80 stratified examples and exposes qualification distance, deterministic phrase audits, and display suppression without deleting evidence. The manual R5 benchmark freezes 200–300 chapters in a separate shadow artifact before route promotion: Gemini 3.1 Flash Lite, Gemma 4 26B, and GLM 4.7 Flash are candidate taggers; Gemma 4 31B is the independent pre-labeler. R3 agenda text is used at episode scope for deterministic tagging and only with explicit chapter mapping for chapter evidence. Full design: [`review/42`](review/42-unified-tag-calibration-and-evaluator-overlay.md); original implementation review: [PR #945](https://github.com/BashfulBits/city-meeting-podcasts/pull/945); overlay implementation is in the current change. |
| **R6** | **Not started** — **#3 per-agenda-item cards · #2 auto-summaries · #15 soundbites** remain L3 design in [`review/30`](review/30-cards-summaries-soundbites.md); R5's stable chapter IDs, bounded transcript windows, and chapter tags now provide the shared context for cards, summaries, quote/highlight selection, and the longest-chapter heuristic. |
| **R7** | **Pilot implementation in progress (unmerged)** — calibrated native speaker diarization, voice profiles, minutes-backed roster correction, R6 pull-quote attribution, and static speaker pages; the revised design, private benchmark/release gate, and remaining live validation are recorded in [`review/31`](review/31-speaker-diarization-attendee-extraction.md) and [#1274](https://github.com/BashfulBits/city-meeting-podcasts/issues/1274). This remains a 1.0 gate. |
| **R8** | **Not started** — **#55 front-end design, #50 accessibility, and #16 funding/support** remain L3 design in [`review/32`](review/32-frontend-design-accessibility-funding.md); the eventual UI should expose episode facets plus chapter-level topic hits and transcript seek/highlight context without exposing ingestion architecture. |
| **R9** | **Shipped** (2026-08-23) — **Automated runtime/dependency maintenance**: version-pinned constraints (`constraints/*.txt`), SHA-pinned Actions, model revisions, complete `.github/renovate.json5` configuration across all runtime/hygiene/output-affecting packages and Cloudflare Workers, static CI policy guard (`scripts/check_dependency_policy.py`), and monthly immutable-URL/checksum FFmpeg build workflow (`.github/workflows/build-ffmpeg.yml`). |

## 1.0 milestone (drop the beta tag)

Complete Phase **H** and the Phase **R** research-tool/release-hardening series above. R9 carries the
former standalone runtime-maintenance gate, so Phase-R completion is the single canonical 1.0 gate.

## Beyond 1.0 (the long-horizon phases)
Documented in [VISION.md](VISION.md); designed at sketch level in [`review/11`](review/11-technical-design-roadmap.md):
- **Phase E — Engagement & Distribution**: a **social syndication bot** is the recommended first
  post-1.0 build (near-zero cost — rides directly on R6 soundbites + the already-built, currently-unwired
  `clips.extract_clip`); then site-news RSS + Substack newsletter, weekly look-back digest,
  "national highlights", **#13** roll-up feeds, **#17** OPML, **#12** custom-query feed builder.
- **Phase F — Pre-Meeting Foresight**: upcoming agendas + weekly staff reports, **#19** `.ics` calendar,
  watchlists + topic alerts, **#31** Legistar (rich agendas/votes), backup-material analysis.
- **Phase C — Co-Creation & AI Audio**: look-ahead/look-back outlines & drafts, AI-generated podcast.

## Cross-cutting
- **Discovery (#27/#32) targets Strong Towns Local Conversation cities** (<https://www.strongtowns.org/local>)
  and human-requested cities (**#28/#56**) — not raw population rank.
- **Monetization**: free + donations/grants (**#16**, **#125** OP3 analytics); freemium considered only
  if the project grows enough to sustain it; never paywall the public record.
- **Security**: LLM output is untrusted; SSRF gate on any user-submitted source (see [SECURITY.md](SECURITY.md)).
- **Compute scaling**: heavy inference runs behind a **pluggable execution-backend interface** (free
  Actions runner now; Modal / Kaggle / self-hosted Mac-mini runner / AWS later) so scaling is adapter-only
  — the interface is a **pre-1.0 lock**. See VISION "Compute is pluggable" and `review/11`.
- **Deferred backlog**: translation (#9), bitrate ladders (#24), chapter
  images (#26), "new since last visit" (#48), full video re-hosting, off-Actions media. Records→managed-SQL
  (D1/Turso) moves decisively **past 1.0**, merged with the "Interaction seam" dynamic-edge-tier proposal
  (alerts/API/personalization/scaled search) into one initiative designed together when its trigger
  fires — see [`review/11`](review/11-technical-design-roadmap.md) §5.5.
  (Speaker diarization (#7) moved to **Phase R**, now **R7** — gating 1.0, see
  [`review/11`](review/11-technical-design-roadmap.md).)

## How priorities work here
Items are scoped, rationalized, and cost-modeled in [`review/11`](review/11-technical-design-roadmap.md)
(which links the deep breakout designs). The maintainer drives sequencing; once contribution opens
(1.0), well-scoped low-cost items get labeled **good first issue**.
