# Technical Design Roadmap (canonical, living)

**Status: LIVING · last updated 2026-06-18**

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
| H1 docs/issue reconciliation | #52-health, GH#110/#141/#154 | L3 | **Shipped** (this doc set + GH#154 closed, GH#110 narrowed to backfill/ops, GH#141 umbrella-only) |
| H2 projection wall-clock fix | R3 | L3 | **Shipped** (per_run_cap→None default; materialize_encoded calibration; hours_hosted bytes fallback; gate wait + peak load + window % telemetry in run_history.jsonl; audio + transcript backlog ETAs in build_status) |
| H3 feed-validation publish gate | #53 | L3 | **Shipped** (`validate_build` + `citypods validate-build` CLI + `deploy.yml` gate before Pages upload; redirect feeds skipped; known-empty slugs demoted to warn) |
| H4 feed-health catch-up vs stalled states + per-provider error-rate tracking | R5 | L3 | **Shipped** (suppress catching-up; warn stalled ≥ 3/5; `provider_errors` in `run_history.jsonl` + `check_provider_error_rates`; `audit_feeds.py` auto-comments on state transitions; 19 new tests) |
| H5 stage backlog manifest + prioritization policy | #41, R2 | L3 | **Shipped** ([#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263) ordering engine + [#264](https://github.com/BashfulBits/city-meeting-podcasts/pull/264) manifest/sidecar/status/light-ordering + [#265](https://github.com/BashfulBits/city-meeting-podcasts/pull/265) global two-pass enrich queue) — hybrid manifest (`citypods/ops/workqueue.py`); behavior-preserving deterministic default; comparator registry (windowed `recency`/`within_days`, partial `city_order`, …); diarization-forward artifact-keyed schema; prod policy `recency:{desc, within_days:30}`; global newest-everywhere-first enrich (on-runner audio pass + decoupled async-ready transcript pass — transcribe/diarize go over-the-wall to external workers, H9/H6b); buckets reserved-but-inert; archive-backfill → Deferred (§6). Design frozen in [review/12 §H5](12-hardening-and-efficiency.md#h5--stage-backlog-manifest--configurable-prioritization-policy). Deferred to H6b/H9: competitive lease acquisition + per-item persistence. |
| H6a ASR benchmark workflow (`asr-bench.yml`) | #1 | L3 | **Shipped** ([PR #256](https://github.com/BashfulBits/city-meeting-podcasts/pull/256)) |
| H6b separate audio + ASR workflows, sharded | #1, R1 | L3 | **Shipped** ([#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273)) · `enrich.yml` replaced by `audio.yml` + `asr.yml` (own `audio`/`asr` concurrency groups, `strategy.matrix.shard`=4); `enrich --shard K/N`/`--source`/`--lane {audio,transcribe,align}`; `run.py` filters cities by source-atomic weighted `shard_assignment(source_key)` + threads the lane into the two-pass queue; **scoped `push_state(only_prefixes=)` + `reconcile_state(full_run=False)`** so shards don't clobber; `audio.yml`=`--lane audio`, `asr.yml`=`--lane transcribe`; **`align` lane implemented but unscheduled** (forced alignment deferred — caption feeds get fresh ASR meanwhile); provider leases reserved for H14 except Granicus media fetches now have a targeted cross-shard lease. **Follow-up fix (2026-06-16, `fix/cross-lane-record-clobber`):** the per-shard scope did not cover the *cross-lane* lost update — the audio and ASR workflows write the same `source_key`'s `episodes.json` at overlapping times, so a late ASR run re-uploaded its start-of-run `audio` block over freshly hosted audio (`hosted_audio −16`). Scoped pushes are now **foreign-block-preserving** (`records.protected_blocks_for_lane`/`merge_preserving_foreign`, `statesync.push_records_merged`) and `stages.LANE_STAGES` keeps each lane to its own work-class stages. Block/lane registries extend to the `diarize` lane (review/12 §H5/§H6). **Deferred:** per-stage object files (`audio.json`/`transcript.json`/`speakers.json`) to remove the shared `episodes.json` read-modify-write entirely (closes the residual TOCTOU window — §6). |
| H7 contributor/agent handoff docs | #57 (partial), R9 | L3 | **Shipped** (this doc set: AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING + templates) |
| H8 4-core runner saturation (ffmpeg `-threads` + memory admission + abandoned-thread accounting) | new | L3 | **Shipped** ([PR #235](https://github.com/BashfulBits/city-meeting-podcasts/pull/235)) |
| H9 combined-throughput evaluation | new | L3 | **Committed** · measure three execution homes (local-sharded H6b + Modal + Beam H14): throughput (transcript-min/runner-hr), failure rate %, $/transcript-hour; diarization $/speaker-hr (pyannote v3 GPU vs CPU ECAPA-TDNN). Fixed meeting mix (5 short/medium/long); test independent transcription vs pipelined transcription+diarize. Gate: 80-feed catalog must complete backlog in <1 month on free tiers. Design: [review/12 §H9](12-hardening-and-efficiency.md#h9--combined-throughput-evaluation-diarization-speakerhour). |
| H10 ASR alignment fix (`WhisperModel.align` AttributeError + fallback gap) | new | L3 | **Shipped** ([PR #232](https://github.com/BashfulBits/city-meeting-podcasts/pull/232)) |
| H11a deploy resilience — native work gate + one-slot audio lane + concurrency tuning | new | L3 | **Shipped** ([#239](https://github.com/BashfulBits/city-meeting-podcasts/pull/239)/[#241](https://github.com/BashfulBits/city-meeting-podcasts/pull/241)/[#242](https://github.com/BashfulBits/city-meeting-podcasts/pull/242)/[#243](https://github.com/BashfulBits/city-meeting-podcasts/pull/243)/[#244](https://github.com/BashfulBits/city-meeting-podcasts/pull/244)/[#246](https://github.com/BashfulBits/city-meeting-podcasts/pull/246)/[#247](https://github.com/BashfulBits/city-meeting-podcasts/pull/247)) |
| H11b deploy resilience — render-only deploy | new | L3 | **Shipped** ([#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272)) · `deploy.yml` stripped to render-only (no ffmpeg/ASR, `actions: read` dropped); heavy phase → new `enrich.yml` (own `enrich` concurrency group); **render writes only `docs/`** — `build()` gates `save_records`/`push_state`/`reconcile_state` off `--phase render` so the enrich workflow is the sole record writer (closes the lost-update record-write race); `statesync.push_state(only_prefixes=)` + `reconcile_state(full_run=)` scope hooks ready for H6b sharding. Precedes H6b. |
| H12 transcript artifact rework (segment VTT + word-JSON + version-aware re-transcribe) | #249 regression, R2/#7 | L3 | **Shipped** ([PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253)) |
| #39 per-provider rate limiting (incl. Retry-After clamp) | #39 | L2→L3 | **Shipped** ([#274](https://github.com/BashfulBits/city-meeting-podcasts/issues/274)) · process-global `HostRateLimiter` (per-registrable-domain cap from `provider_rate_limits`) acquired by **both** `GuardedHTTPAdapter.send` **and** the ffmpeg/ffprobe fetch paths (`media.py`, `concat.py`) — the H6b storm was ffmpeg, not `requests`, so capping only the adapter would have missed it; Granicus follow-up adds B2-compatible soft `provider_distributed_leases` around media reads across the four audio shards (`granicus.com: 6` after 2026-06-15 overlap probes) plus `rate_limited` classification and a run-local circuit breaker; Granicus 403 backoff lifted into the shared retry (403 in `status_forcelist`, Retry-After clamp kept); also fixed the H6b regressions it surfaced: truncation guard (encode < 50 % of declared duration → #120 backoff, not hosted), source-atomic weighted `shard_assignment` (no empty `audio (0)`, large sources packed first), responsive 0.5 s ffmpeg poll (honest `seconds=`). Design: [review/12 §#39](12-hardening-and-efficiency.md#39--per-provider-rate-limiting-sequence-with-h6b) |
| Granicus media reliability follow-up | GH#300/#39 | L3 | **Committed** · three-phase sequence: (1) test distributed/process-local concurrency caps (2,3,4,5 slots vs 2/1 rate-limits) to reduce 403 prevalence; requirement: zero stuck files, test period confirms drain vs throughput; (2) pause Granicus media-fetch in `contracts.yml` when `audio.yml` active, auto-retry on contract failure; (3) request-shape alternatives only if phases 1–2 don't resolve (add headers, direct DownloadFile.php, HLS discovery). Escalate stuck downloads (8+ attempt failures) as high-priority GH issues. Design: [review/12 §Granicus follow-up](12-hardening-and-efficiency.md#granicus-media-reliability-follow-up-gh300--39-follow-up). |
| **H13 GPU/ASR execution-backend interface (+ `local` adapter)** | §5.5, [#271](https://github.com/BashfulBits/city-meeting-podcasts/issues/271) | L3 | **Shipped** (#271) · **pre-1.0 lock** · `citypods/compute/{base,local}.py` mirrors `storage/`; `base.py` types all task verbs (ASR + the reserved R3/R4 LLM verbs) + `InferenceJob`/`JobResult`/`JobHandle` + `runtime_checkable` `Backend`; the `local` adapter wraps in-process faster-whisper/stable-ts (byte-identical); `TranscriptStage` routes through `backend.run_inference(...)`; `compute_backend: local` default. Design: [review/12 §H13](12-hardening-and-efficiency.md#h13--gpuasr-execution-backend-interface--local-adapter--the-pre-10-lock) |
| **H14 external transcription adapters** | new, #7-adjacent | L3 | **H14a substrate Shipped** ([#275](https://github.com/BashfulBits/city-meeting-podcasts/issues/275)). **H14b (Modal)** + **H14c (Beam)** detailed for development: spurious-error retry-once then fail-hard policy; hard-limit quota → fallback to other backend then `local`; round-robin dispatch with smart batching near quota exhaustion; separate budget ledgers per provider; secrets via GitHub Actions. Testing: mock backend for unit tests, minimal live integration CI test (path-filtered to Modal/Beam code changes only), production canary post-merge. Admin dashboard: prominent free-tier budget-remaining visual (% GPU-seconds used, no auto-ticket on quota exhaustion—expected operational behavior). Design: [review/12 §H14b](12-hardening-and-efficiency.md#h14b--modal-transcription-adapter-async-dispatch-backend) + [§H14c](12-hardening-and-efficiency.md#h14c--beam-transcription-adapter-async-dispatch-backend-parallel-with-h14b). Mac-mini/AWS post-1.0. |
| H15 transcript-quality metric (periodic caption-trust scoring) | #1-adjacent, #7 | L2 | **Committed** · turn the unmeasured "served captions are faithful enough to align against" assumption (why H6b's `align` lane is **implemented but unscheduled**) into a **periodic, per-source, computed metric** instead of a one-time WER study. Three layers: **L1** = free acoustic-fit recorded *every run* from the `stable_whisper.align()` call we already make (existing `_MIN_ALIGN_COVERAGE` coverage + mean word-logprob → new merge-pushed `state/transcript_quality_log.json`, same union-by-id pattern as `asr_runtime_log.json`); **L2** = a CTC forced aligner **independent of both generators** (`torchaudio.functional.forced_align`/WhisperX) over a rotating sample for a *fair* served-vs-ASR verdict (WER between two machine outputs measures disagreement, not correctness); **L3** = a small human-gold sample anchoring absolute WER/CER. Output: a per-source `caption_trust` (`high\|low\|unknown`) that gates `align`-vs-`transcribe` routing + an `/admin/status` panel. Distinct from **H6a** (runtime benchmark) / **H9** (throughput, $/hr) — H15 measures **correctness**; reuses the H13 backend interface. Design: [review/12 §H15](12-hardening-and-efficiency.md#h15--transcript-quality-metric-periodic-caption-trust-scoring). |

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
| Speaker diarization | #7 | L1 | §5.1 — after H6b; runs on the execution backend (H9 / §5.5) |

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

### Deferred backlog (ongoing) — §6
#9 translation · #24 bitrate ladders · #25 intro/outro stinger (GH#153) · #26 chapter
images · #34 config-via-issue-comments · #40 B2 actual-cost dashboard · #42 **directory** index sharding
(review/02 Change 6; trigger-gated by `review/16` client budgets, distinct from R2 transcript-search
partitioning) · #44 structured logging · #47 map browser · #48 new-since-visit · #10 agenda-packet chapter
descriptions · #14 `podcast:person/location` tags · #33 dead-city archival · review/02 Change 5
DerivedArtifact refactor · review/04 B3 stale-record bucket leak · review/04 R4 per-host rate-limit (#39)
· admin dashboard extension (#49) · full video re-hosting · hosted DB/API · off-Actions media.
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

**Speaker diarization (#7).** *Problem:* transcripts don't identify who's speaking — council members,
staff, and public commenters look identical. *Approach:* run a speaker-embedding diarization model over
the audio after transcription, align speaker-change boundaries to the word-level VTT cues (now emitted
by the ASR stage), and emit `<podcast:person>` or a speaker-labeled VTT. Two CPU-viable backends:
(a) **wespeaker ECAPA-TDNN** (~100 MB, no HF gate, ~2× transcription cost on CPU); (b) **speechbrain
ECAPA-TDNN** via simple-diarizer (~300 MB, similarly lightweight). A free/low-cost GPU API
(H9 evaluation) cuts diarization cost further — pyannote v3 on GPU is fast and accurate but gated;
the CPU-only path uses the lighter backends to stay within the Actions runner budget. It runs on the
**execution backend** (§5.5) — the same interface as transcription — so the diarization model can target a
GPU backend (Modal/Kaggle/self-hosted/AWS) without changing the diarization logic; this is exactly the
infra the maintainer wants **locked pre-1.0**. *Depends on:* word timing — H12 moves it into the
word-JSON sidecar, which diarization consumes (built on PR #249's `word_timestamps`); H6b sharded ASR
workflow (dedicated runner/lane for heavy inference); H9 offload evaluation (cost/quality baseline against
the backend interface). *Sequencing:* implement after H6b lands a separate ASR runner — do not add
diarization to the current single-runner enrich path.

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
plumbing lands"; **full video / hosted DB / off-Actions media** are explicitly out of scope now (§8).
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

**Post-review code queue (updated 2026-06-11 after H6a shipped):**
H8 resource guard (shipped [PR #235](https://github.com/BashfulBits/city-meeting-podcasts/pull/235)) →
H11a deploy resilience (shipped [PRs #239](https://github.com/BashfulBits/city-meeting-podcasts/pull/239)/[#241](https://github.com/BashfulBits/city-meeting-podcasts/pull/241)/[#242](https://github.com/BashfulBits/city-meeting-podcasts/pull/242)/[#243](https://github.com/BashfulBits/city-meeting-podcasts/pull/243)/[#244](https://github.com/BashfulBits/city-meeting-podcasts/pull/244)/[#246](https://github.com/BashfulBits/city-meeting-podcasts/pull/246)/[#247](https://github.com/BashfulBits/city-meeting-podcasts/pull/247)) →
**Do-now (this review's follow-ups):** **H12** (shipped, [PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253)) transcript-artifact rework (clean segment-cue VTT + a
word-JSON sidecar + version-aware gradual re-transcribe — fixes #249's word-per-cue regression and
unblocks search/clips/diarization); **H6a** ASR benchmark workflow (shipped,
[PR #256](https://github.com/BashfulBits/city-meeting-podcasts/pull/256): manual max/med/min
model + beam-size + CPU-thread benchmark before backfill); **B2** Retry-After **clamp** (fold into #39).
**Then confirm `native_audio_max_active: 4` / H1 (next):** `gh` issue reconciliation — close/narrow GH#154
(`<podcast:transcript>` shipped), GH#110 (ASR → backfill/ops), GH#141 (timeline epic → umbrella only);
H2 projection wall-clock fix + tests (incl. a per-run telemetry summary record — see review/12 H2);
H3 validation gate; H4 feed-health states + per-provider error rates; H5 backlog manifest +
prioritization (including an explicit alignment-deferred lane for untimed provider transcripts);
**remaining Phase H tail (this plan):** H13 GPU/ASR execution-backend interface (+ `local` adapter —
pre-1.0 lock, do first) → H11b render-only `deploy.yml` (record-write stops in render) → H6b `audio.yml`
+ `asr.yml` workflows, sharded by `source_key` + scoped `push_state` + align-only/transcribe-only lanes
(so stable-ts and faster-whisper model loads do not stack in one runner) → #39 per-provider rate limits
→ H14 Modal + Beam free-tier transcription adapters (async dispatch from `asr.yml`; H5 leases go live)
→ H9 combined-throughput evaluation. The execution-backend **interface design** (§5.5/H13) is a
**pre-1.0 lock**, proven in Phase H by H14's two live GPU adapters; the first **LLM API adapter** lands
with R3/R4.
When each step completes, apply §2's Implemented-row doc updates in the same PR or immediate post-merge
docs PR.

**Explicitly out of scope (now):** move to a hosted DB/API (no); move media off GitHub Actions (keep as
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
