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
external workers instead of repeatedly overflowing onto and terminating a hosted runner.

## Current phase: **H — Hardening & Efficiency** (next up)
Stabilize and maximize the throughput of what just shipped *before* layering on new user-facing
features. Detailed design: [`review/12`](review/12-hardening-and-efficiency.md).

> **Remaining tail (updated 2026-06-21).** With
> H1–H8/H10–H13/**#39** shipped, the remaining items are:
> **H16** Granicus proxy production validation → **H17** R2/CAS + durable pull-work substrate
> (`review/17`/`review/18`) → **H14** the first real external workers
> (**Modal + Beam**, free-tier-bounded) → **H9**
> combined-throughput eval. The maintainer pulled the external-worker *build* into Phase H so "compute
> is pluggable" ships proven by two live GPU adapters before 1.0. (**#39** per-provider rate limiting
> shipped in [#274](https://github.com/BashfulBits/city-meeting-podcasts/issues/274) — it fixed the
> Granicus 403 / truncated-fetch storm the first sharded Audio run hit.)
> Runtime/dependency update automation is now the final **Phase-R** item; completing Phase R is the
> gate for 1.0.
> The local-ASR duration guard is a narrow H14 reliability prerequisite, not a promotion of later
> scaling work: ship it now, then continue with Modal and Beam workers that explicitly support audio
> longer than the local ceiling.
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
| **H4** | ✓ Shipped — Feed-health triage: catching-up suppressed, stalled → `WARN`; `provider_errors` per run in `run_history.jsonl` → `check_provider_error_rates` fires before deploys go red; `audit_feeds.py` auto-comments on state transitions |
| **H5** | ✓ Shipped — Stage **backlog manifest** + configurable **prioritization policy** ([#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263)/[#264](https://github.com/BashfulBits/city-meeting-podcasts/pull/264)/[#265](https://github.com/BashfulBits/city-meeting-podcasts/pull/265)): `citypods/ops/workqueue.py` policy engine (windowed `recency`, `city_order`, …; prod `recency:{desc, within_days:30}`); derived work manifest + lease sidecar + `/admin/status` backlog-by-work-class; **global two-pass enrich queue** — newest-everywhere-first on-runner audio + decoupled async-ready transcript pass (transcribe/diarize go over-the-wall to external workers, H9/H6b). Whole-archive backfill split out as a separate opt-in. |
| **H6a** | ✓ Shipped — ASR **benchmark workflow** (`asr-bench.yml`, manual, PR #256): compares max/med/min model + beam-size + CPU-thread profiles before any backfill decision |
| **H6b** | ✓ Shipped — **Split audio + ASR into dedicated sharded workflows** (`audio.yml` + `asr.yml`, own `audio`/`asr` concurrency groups, `matrix.shard`=4) replacing the combined `enrich.yml`; `enrich --shard K/N`/`--source`/`--lane {audio,transcribe,align}`; cities filtered by source-atomic weighted `shard_assignment(source_key)` + scoped `push_state`/`reconcile_state` so shards don't clobber; `asr.yml` runs transcribe-only with the `asr-transcribe` dependency extra (forced-alignment `align` lane implemented but unscheduled and assigned `asr-align`); Granicus media fetches now also use cross-shard storage-backed leases so the 4 audio shards share one provider cap ([#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273)) |
| **H7** | ✓ Shipped — contributor/agent handoff docs (AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING + PR/issue templates) |
| **H8** | ✓ Shipped — throughput maximization on the free 4-core runner (PR #235): pinned ffmpeg `-threads`, memory/CPU admission guard, and a killable persistent local-ASR subprocess with per-episode timeout backoff (replacing abandoned native threads) |
| **H9** | **Combined-throughput and routing evaluation** — measure local-sharded (H6b) + Modal + Beam (H14), including duration/memory success, cold start and I/O, long-audio chunk/stitch overhead, separate-vs-combined ASR+diarization cost, free-tier consumption, queue/routing outcomes, transcript boundary quality, and diarization speaker consistency; use results to set local limits, GPU preference, chunk-persistence policy, and the first paid/self-hosted step |
| **H10** | ✓ Shipped — ASR alignment fix (PR #232): caption-bearing feeds use a stable-ts align model and fall back to fresh transcription on align errors |
| **H11a** | ✓ Shipped — **Deploy resilience**: native work gate + one-slot audio lane + concurrency tuning + Retry-After fix (PRs #239/241/242/243/244/246/247) |
| **H11b** | ✓ Shipped — Render-only `deploy.yml` (no ffmpeg/ASR; `actions: read` dropped) + heavy phase → new `enrich.yml` (own `enrich` concurrency group) **+ render stops persisting records** — `build()` gates `save_records`/`push_state`/`reconcile_state` off `--phase render` so the enrich workflow is the sole record writer (closes the lost-update race); `statesync` `only_prefixes=`/`full_run=` scope hooks ready for H6b ([#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272)) |
| **H11c** | **Graceful SIGTERM + mid-run checkpoint** (implemented, unreleased — [#386](https://github.com/BashfulBits/city-meeting-podcasts/pull/386)): a runner-level SIGTERM/GitHub-cancel now converts to the existing graceful-stop path (workers defer, the run still persists records + writes its `run_history` entry) instead of dying mid-queue; the enrich queue persists each source as the audio pass drains and again at the end (idempotent); interrupted runs are tagged in `run_history.jsonl` and exit `143`. Directly serves the Phase-H exit criterion "no exit-143 / lost-comms kills" below |
| **H12** | ✓ Shipped — transcript artifact rework (PR #253): clean segment-cue VTT for players + a word-level JSON sidecar for search/clips/diarization + version-aware gradual re-transcribe (fixes #249's word-per-cue regression) |
| **H13** | **GPU/ASR execution-backend interface** (+ `local` adapter) — the pre-1.0 "compute is pluggable" lock; `citypods/compute/` mirrors `storage/`. Do **first** (seam for H6b lanes + H14 adapters). LLM-API half of the interface lands with R3/R4 |
| **H14** | **External transcription workers — Modal + Beam** (free-tier-bounded async dispatch behind H13; `asr.yml` dispatches). _H14a substrate **shipped** ([#275](https://github.com/BashfulBits/city-meeting-podcasts/issues/275)): budget ledger ($0 guarantee), router + `DispatchCoordinator`, live H5 leases, `compute reconcile`, `compute_backend: auto`._ The unreleased local guard defers known recordings above 4h when external dispatch declines; the separate runtime estimator still protects the Actions deadline. ASR shard weighting separates local duration cost from cheap dispatch, blocked, and in-flight work; a once-per-run planner now restores B2 state once and publishes one immutable state/assignment artifact consumed by every matrix shard. H14 extends that canonical planner with one external budget/capacity route snapshot. **H14b/H14c next:** real Modal + Beam workers that support longer recordings with bounded memory. Local overflow is allowed only when both local guards pass; otherwise work remains queued, not failed. The locked episode-level `transcribe`/`align`/`diarize` interface remains unchanged; `diarize` implementation stays in Phase R and Mac-mini/AWS stay post-1.0. |
| **H15** | **Transcript-quality metric** — periodic caption-trust scoring that gates align-vs-transcribe routing ([GH#391](https://github.com/BashfulBits/city-meeting-podcasts/issues/391)) |
| **H16** | **Granicus proxy validation + simplification** — three production Audio runs, direct-first vs sticky-Worker decision, then remove or justify redundant circuit machinery ([GH#353](https://github.com/BashfulBits/city-meeting-podcasts/issues/353)). Duplicate combined/per-board audio work is fixed (implemented, unreleased — [GH#421](https://github.com/BashfulBits/city-meeting-podcasts/issues/421)): entity-family shard affinity + run-local stable-uid/recipe coalescing, with no source-key migration or backfill. |
| **H17** | **Distributed work/control-plane substrate** — `RoutingStorage` + native R2 CAS, per-episode ownership-safe merge/planning, and the pull-ledger claim contract H14 workers consume ([GH#390](https://github.com/BashfulBits/city-meeting-podcasts/issues/390); `review/17` + `review/18`) |

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

> **Scope (depth-first):** prove R1–R5 across the **entire current city catalog** before onboarding new
> cities (VISION "Depth-first"). The pilot set *is* today's roster — not a hand-picked subset — so search
> index size and page volume stay bounded by the current ~85 feeds while the engine choices (e.g.
> Pagefind) are validated.

| Pri | Item |
|----:|------|
| **R1** | **#46/#157** per-meeting permalink pages over the append-only archive: playable meetings get player/transcript/chapters/agenda/deep-links; unavailable recordings retain civic metadata + canonical provenance with a clear no-recording notice and no broken player |
| **R2** | **#6** static client-side transcript/meeting search, including metadata-only unavailable recordings and an availability filter |
| **R3** | **#4** topic tags / **Strong Towns lens** (transparent rules + human overrides; LLM-assist later) |
| **R4** | **#3** per-agenda-item "what changed" cards · **#2** auto-summaries · **#15** soundbites |
| **R5** | **#55** front-end design cycle · **#50** accessibility · **#16** funding link |
| **R6** | **Automated runtime/dependency maintenance** — Dependabot for Python/Docker/Actions, reproducible constraints, and tested immutable FFmpeg update PRs |

## 1.0 milestone (drop the beta tag)

Complete Phase **H** and the Phase **R** research-tool/release-hardening series above. R6 carries the
former standalone runtime-maintenance gate, so Phase-R completion is the single canonical 1.0 gate.

## Beyond 1.0 (the long-horizon phases)
Documented in [VISION.md](VISION.md); designed at sketch level in [`review/11`](review/11-technical-design-roadmap.md):
- **Phase E — Engagement & Distribution**: site-news RSS + Substack newsletter, weekly look-back digest,
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
  images (#26), "new since last visit" (#48), full video re-hosting, hosted DB/API, off-Actions media.
  (Speaker diarization (#7) moved to **Phase R** — see [`review/11`](review/11-technical-design-roadmap.md).)

## How priorities work here
Items are scoped, rationalized, and cost-modeled in [`review/11`](review/11-technical-design-roadmap.md)
(which links the deep breakout designs). The maintainer drives sequencing; once contribution opens
(1.0), well-scoped low-cost items get labeled **good first issue**.
