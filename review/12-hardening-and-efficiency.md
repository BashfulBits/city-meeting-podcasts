# review/12 — Hardening & Efficiency (Phase H)

**Maturity: L3 (development-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) Phase H ·
last updated 2026-06-11**

> When the items here ship, stamp this doc "Implemented in PR #N", flip the `review/11` catalog rows to
> Shipped, and add CHANGELOG entries (see the lifecycle contract in CONTRIBUTING.md).

## Purpose & context

Most of the heavy product machinery (timeline/EDL, audio-cleanup band, ASR, content permanence) has
**shipped**. The current risk has shifted from "missing features" to **operational throughput and
reliability** on free GitHub-hosted runners — ASR now dominates the backlog and Build & Deploy fails on
**~half of scheduled runs**. A 2026-06-08 build-log review (see the dated root-cause section below)
traced these to **runner starvation** (unpinned ffmpeg threads + no memory admission control), not the
clean OOM/preemption the earlier framing assumed — and showed that `continue-on-error` on the enrich
step *cannot* keep the job green when the runner itself is killed. This phase makes the pipeline
**stable, observable, and maximally fast on the free tier** before new user-facing surface is added.

**Current production constraints** (`config/site_config.yml`): 4-hour deploy cron; enrich window
`run_time_budget_minutes: 240 × budget_safety: 0.85 ≈ 204 min`; `max_workers: 8` (source-level),
`max_encodes_per_source: 2`, `asr_workers: 1` (reduced from 20/4 in PR #227 — necessary but **not**
sufficient; runs still fail post-reduction); per-encode timeout 45 min; ffmpeg `-threads` **unpinned**
(each encode grabs all cores). GitHub free public-repo runners: 4-core / ~16 GB box, 6-hour job cap,
≤20 concurrent standard jobs, ~1,000 `GITHUB_TOKEN` req/hr.

**Throughput anchors** (review/03 §7): self-hosted faster-whisper "base" int8 on a 4-vCPU runner ≈ 4–6×
realtime (a 2 h meeting ≈ 20–30 min). Single 4 h-cron job ≈ 380–400 transcript-meetings/week; a 4-job
matrix ≈ ~1,500/week; 8-job ≈ ~3,000/week. Reuse-first means only ~34 mostly-Granicus feeds need ASR.
Current ~80 feeds generate ~55 new meetings/week, so the free tier comfortably covers **inflow**; only
the initial **backlog** takes calendar time.

**Sequencing (DAG) — revised 2026-06-08.** The build-log analysis (below) **reprioritizes** the queue:
the do-now reliability fires (**H10** align bug, **H8** resource guard, **H11** deploy resilience) run
**before** the docs/issues/observability items, because they are what is actually turning runs red and
collapsing throughput today. **H10 shipped in PR #232** and **H8 shipped in PR #235**; H11a is the
remaining active do-now reliability check. The enrich-isolation half of H11 still waits on H5's
manifest.

```
do-now fires:  H10 (align fix; shipped PR #232) ─► H8 (resource guard; shipped PR #235)
                                       └─► H11a (deploy resilience: guard prevents the runner-level kill)

then catch-up:  H1 (docs/issues) ─┐
                H2 (projection)  ─┼─► H4 (feed-health states, uses H2 ETAs) ─► H5 (backlog manifest + priority)
                H3 (valid. gate) ─┘                                                 │
                                                                                    ▼
                                          H6 (benchmark → sharded ASR) ◄─ H11b (isolate enrich into own workflow)
                                                       ▲
                                            H9 (offload evaluation feeds H6)
```

---

## Build-log root-cause analysis (2026-06-08)

A thorough review of recent `Build & Deploy` logs (enabled by the new `[enrich] heartbeat …` lines)
identified why ~half of scheduled runs go red. **This section supersedes the earlier "exit-137 OOM /
exit-143 preemption" framing** with what the logs actually show, and drives the new/expanded items
H8/H10/H11 and the reprioritized DAG above.

### Evidence

Representative failed runs. In **every** one, all steps show ✓ green (including **Enrich**), the
"Warn if enrich was killed" step is **skipped** (so `enrich.outcome == success`), yet the **job** is red:

| Run | Trigger | Dur | Job-level error |
|---|---|---|---|
| 27169048941 | schedule | 33m | `##[error]Process completed with exit code 143` (SIGTERM) |
| 27149025008 | schedule | 1h43m | exit 143 |
| 27114353508 | schedule | 2h8m | exit 143 |
| 27074679697 | push | 3h26m | "The hosted runner lost communication with the server … starves it for CPU/Memory" |

Heartbeat trace from 27169048941 (4-core / 15.6 GiB runner): sustained `load=6.5–7.4/4` (≈75 %
CPU oversubscription); `mem_avail` repeatedly cratering to **~460 MiB** during *concurrent*
`audio encode start/done` of 100–155 MB M4As; `threads` spiking to 25; the job is SIGTERM'd at the next
mem-floor dip. Enrich had run only **~23 min of its 204-min budget** before the kill.

Code confirmations (read at the cited lines):
- **No ffmpeg `-threads` pinning** (`citypods/media.py`) — ffmpeg defaults to all cores, so two
  concurrent encodes (`max_encodes_per_source: 2`) demand ~8 cores on a 4-core box.
- **No memory admission control** — `citypods/run.py:434–494` only *logs* the snapshot/heartbeat.
- **Abandoned daemon ASR threads** — on stop/timeout the semaphore is released and a new worker starts
  while the CTranslate2 daemon thread "continues in background" (`citypods/stages.py:1110`, `:1127`);
  abandoned threads are **not** counted against `asr_workers`.
- **Broken ASR `align`** — `load_model` returns a `faster_whisper.WhisperModel` (`citypods/asr.py:60`)
  that is passed to `align()`, which at `asr.py:187` calls `wm.align(...)` — a method only
  `stable_whisper` models expose → `AttributeError: 'WhisperModel' object has no attribute 'align'` on
  **every** align-mode episode in the logs. The fallback at `stages.py:1060` only catches
  `AlignmentQualityError`, so the `AttributeError` hits the generic handler (`stages.py:1097`) and the
  episode is skipped (`stages.py:1148`) with **no transcript and no fresh-transcribe fallback**.
- `continue-on-error: true` on the enrich step (`deploy.yml:163–193`) cannot prevent a runner-level
  SIGTERM/lost-comms from marking the whole job red — confirmed by the green-steps/red-job signature.

### Hypotheses

| ID | Hypothesis | Evidence | Confidence |
|---|---|---|---|
| **H-A** | ffmpeg CPU oversubscription (unpinned `-threads` × concurrent encodes) starves the runner agent → SIGTERM 143 / lost-comms | load 6.5–7.4/4; `threads`→25 during concurrent encodes; no `-threads` in code | **High** — primary, most frequent |
| **H-B** | Memory exhaustion at the OOM cliff (concurrent encodes + page cache + model) | `mem_avail`→~460 MiB, death at next dip; admission control absent | **High** |
| **H-C** | `continue-on-error` cannot catch a runner-level kill | all steps green, "warn" skipped, job still red w/ 143 / lost-comms | **High (confirmed)** |
| **H-D** | Broken ASR alignment → caption-bearing feeds get zero transcripts; also burns the run's early minutes | dozens of `'WhisperModel' object has no attribute 'align'`; fallback only catches `AlignmentQualityError` | **High (confirmed)** |
| **H-E** | Abandoned daemon ASR threads compound CPU/RAM (uncounted background inference) | `stages.py:1110/1127` "inference continues in background"; worst in long timeout-heavy runs (the 3h+ lost-comms run) | **Medium** |
| **H-F** | Throughput collapse from early death — runs die ~23–30 min into a 204-min window, so backlog barely advances | ~50 % of scheduled runs fail *post*-#227; enrich killed at ~23 min | **High** |
| **H-G** | Observability/alerting gap — the red X hides genuine provider failures; no per-stage "where was it when killed" | green-steps/red-job ambiguity; no alert | **Low** (partially H4) |

### Coverage vs the existing H1–H9 plan

| Hypothesis | Prior coverage | Verdict |
|---|---|---|
| H-A ffmpeg threads | H8 listed "pin ffmpeg `-threads`" as a *candidate* lever | **Partial** — not committed; sequenced last |
| H-B memory guard | H8 listed "add a memory guard" | **Partial** — vague; sequenced last |
| H-C continue-on-error gap | — | **Gap** → new H11 |
| H-D align bug | H6 only *measures* alignment-failure rate | **Gap** → new H10 |
| H-E abandoned threads | — | **Gap** → folded into H8 |
| H-F early-death throughput | H6/H8/H9 (broad throughput) | **Indirect** — failure mode now named (fixed by H-A/H-B) |
| H-G observability | H4 (feed-health states) | **Partial** |

**Conclusion.** The active deploy-killer (H-A/H-B) was only loosely covered by H8 *and H8 sat last in the
queue, behind docs/issue work*; H-C, H-D, and H-E were not covered at all. The response is therefore new
items (**H10**, **H11**), concrete/committed changes folded into **H8** (incl. H-E), and the
**reprioritized DAG** above — not just more tuning.

---

## H1 — Docs / roadmap / issue reconciliation

**Problem.** `ROADMAP.md`/`review/01` listed shipped work as future; the issue tracker mixes real
breakage with catch-up status; `<podcast:transcript>` (GH#154) is already emitted.

**Done in this doc set:** ROADMAP rewrite, CHANGELOG, ARCHITECTURE, review/11 catalog, review/01 banner,
ADD_CITY.md wall-clock fix.

**Remaining (code/issue work, L3):**
- `gh` issue reconciliation: **close** GH#154 (verify a production feed emits `<podcast:transcript>`
  first); **narrow** GH#110 ASR to "backfill + ops follow-up"; keep GH#141 timeline epic open only as an
  umbrella; the GH#153/#155/#156/#157 feature issues stay (Phase R/E).
- Feed-health issue cleanup happens as a side effect of **H4** (don't bulk-close by hand).

**Acceptance:** `gh issue list` shows no open issue describing already-shipped work as unbuilt; review/11
§7 checklist passes.

---

## H2 — Projection wall-clock fix + backlog rows

**Problem.** `citypods/report.py` still models the legacy per-run cap:
- `report.py:164` and `report.py:549`: `per_run_cap=int(defaults.get("materialize_budget_per_run", 25))`.
- `to_markdown()` (≈ `report.py:235–239`) can therefore report "the per-run cap (25) is the bottleneck"
  and recommend `materialize_budget_per_run`, which **no longer exists** in production (PR #128 → wall
  clock). This misleads planning.

**Design (single chosen path).**
1. Default `per_run_cap` to **`None`** when `materialize_budget_per_run` is absent (it always is now), so
   the model is wall-clock-bound by default. Keep honoring an explicit value if a fork sets one.
2. Replace the "per-run cap is the bottleneck" branch with a **wall-clock** framing: report
   estimated throughput from measured `sec_per_ep` × window, and label any legacy cap as "legacy cap".
3. **Calibrate audio throughput from `materialize_encoded`** (real download+ffmpeg+upload), not
   `materialized` (which includes cheap credited-object metadata work).
4. Add explicit **backlog rows**: an **audio backlog** and a **transcript backlog**, the latter from
   `stage_totals.transcript.{aligned, transcribed, seconds, backlog, errors}`. Add "estimated time to
   clear" (days) for each from `run_history.jsonl` rates.
5. **Per-run telemetry summary record (C2).** Persist a compact summary per enrich run to
   `run_history.jsonl` — peak `load`, min `mem_avail`, per-encode child peak RSS, encode count,
   native-gate wait seconds, ASR minutes, and **window-used %** — so H8/H11a acceptance ("enrich uses most
   of its window"; "`mem_avail` never near the floor") and future concurrency-tuning decisions (the
   `native_audio_max_active` 1→4 raise being the live example) are **machine-checkable from one record**
   instead of hand-read from raw logs. This also feeds H4 its ETA inputs and gives the Phase-H exit
   criteria their measurement source.

**Files.** `citypods/report.py` (both `ModelInputs` construction sites; `to_markdown`; `build_status`);
`citypods/assets/status.html` (add transcript-backlog + ETA fields); `citypods/run.py` +
`citypods/resources.py` (emit the per-run telemetry summary from the existing heartbeat snapshots into
`run_history.jsonl`); `tests/test_report.py` (cover the **no-cap wall-clock default** as the primary case;
assert transcript backlog row; assert the legacy-cap message only appears when a cap is explicitly set;
assert the telemetry summary is written and round-trips).

**Known related bug (fold in here).** `hours_hosted` reports **0** for Swagit/CivicPlus feeds because
re-hosted M4A duration isn't captured. Deferred fix: probe the output M4A for duration at encode time and
have `report.py` fall back to `audio.duration_served`. Fix alongside the backlog-row work so projected
hours are correct.

**Acceptance:** with no `materialize_budget_per_run` set, `citypods report` shows wall-clock framing,
separate audio + transcript backlogs with ETAs, `hours_hosted` is non-zero for re-hosted feeds, and it
never recommends the removed key. Tests updated.

---

## H3 — Feed-validation publish gate (#53)

**Problem.** `citypods/validate.py` runs in CI/tests but not in the production deploy path; a malformed
generated feed could publish.

**Design.** Add a **gate step in `deploy.yml`** *after render, before the Pages artifact upload*: run
`citypods` validation over the generated `docs/**/*.xml`. Fail the deploy on a structural/iTunes/
Podcasting-2.0 error. **Policy for expected-empty feeds:** an empty feed that is *known/expected* (e.g.
a brand-new city mid-backfill, flagged by the feed-health `empty`/`catching-up` state) must **not** fail
the deploy — pass a known-empty allowlist (derived from records/state), and only fail on *structural*
invalidity or an *unexpectedly* empty feed.

**Implementation paths.** (1) **CLI subcommand** `citypods validate-build docs/` that exits non-zero on
fatal findings (preferred — reusable locally + in CI). (2) inline workflow script (rejected — duplicates
logic). 

**Files.** `citypods/validate.py` (add a `validate_build(output_dir, known_empty)` entry + severity
split: fatal vs warn), `citypods/cli.py` (`validate-build`), `.github/workflows/deploy.yml` (gate step),
`tests/test_validate.py` (fatal vs warn; known-empty pass-through).

**Acceptance:** a deliberately corrupted feed fails the deploy job before upload; a known-empty new city
does not; warnings are surfaced but non-blocking.

---

## H4 — Feed-health catch-up vs stalled states + ETA + auto-comment

**Problem.** ~50+ open `rehost-backlog`/`view-cap` issues conflate *pipeline catching up* with *real
breakage*, so genuine failures hide in noise.

**Design.**
- Split the `rehost-backlog` finding into two states in `citypods/audit.py`:
  - **`rehost-backlog:catching-up`** — hosted count is increasing across recent successful enrich runs
    (read `run_history.jsonl`); include an **ETA** (from H2 backlog/rate). Severity `info`/`warn`.
  - **`rehost-backlog:stalled`** — **no** progress after N successful enrich runs. Severity `error`.
- Add a third, orthogonal signal already present (don't fold in): real **provider/source failure**
  (dead enclosure, expired URL, SSRF reject, parse-to-0) stays `error` and is **never** suppressed by
  catch-up awareness.
- **Per-provider error-rate tracking (B3, new).** Provider drift (e.g. the three Granicus `403` fixes in
  two days, PRs #245/#250/#251) is currently caught only by red deploys, not by the audit. Emit
  per-provider 4xx/timeout counts per enrich run into `run_history.jsonl` (rides on the H2/C2 telemetry
  summary), and have the daily audit raise/annotate an issue when a provider's error rate jumps above a
  baseline — so a provider going bad is visible *before* it turns a deploy red.
- `scripts/audit_feeds.py`: when an issue's computed state changes, **auto-comment** with the current
  fields (hosted count, feed-visible missing, transcript queued, last successful stage run, estimated
  remaining runs) instead of churning the body; close on resolve.

**Files.** `citypods/audit.py` (`check_rehost_backlog` → returns state + progress metrics; new helper to
read run-history progress), `scripts/audit_feeds.py` (state-transition comment), `tests/test_audit.py`
(catching-up vs stalled vs real-failure fixtures; ETA presence).

**Acceptance:** the existing backlog issues re-triage into catching-up (with ETA) vs stalled; a simulated
dead enclosure still reports `error`; no real failure is hidden.

---

## H5 — Stage backlog manifest + configurable prioritization policy

**Problem.** Scheduling is **implicit**: `_materialize_set(episodes, max_per_body)`
([`stages.py:91`](../citypods/stages.py)) picks the top-N-per-body, and whichever source enters the
thread pool first consumes the window. There is no deliberate "recent visible audio first, then
transcripts, then deep archive," no per-stage backlog visibility, and no safe basis for splitting ASR
into its own workflow (H6).

**Design (Adopt + EXTEND — builds on existing primitives, adds a first-class, configurable policy).**

**(a) Durable work manifest.** Alongside the existing per-source records + `run_history.jsonl`, write a
lightweight manifest the run consults and updates:
```
state/work/{audio,transcript,timeline}.json   # or one work.json keyed by stage
```
Each work item: `source_key`, `episode_uid`, `stage`, `stage_version`, `state`
(`queued|running|done|backoff|dead`), `priority_bucket` (`feed_visible|recent_archive|deep_archive`),
`est_seconds`, `observed_seconds`, `last_error`, `next_retry`, and (when concurrent workflows arrive in
H6) `lease_owner`/`lease_expires`. This makes "why is this feed still missing audio/transcripts?"
answerable in the status page, and is the **lease/merge substrate** ASR sharding needs.

**ASR lane split (new H11a mitigation, 2026-06-09).** Production temporarily sets
`asr_alignment_enabled: false`, so untimed provider transcripts remain notes-only and their timed
upgrade is skipped as `alignment-disabled` rather than falling back to generated text. H5 should model
that explicitly instead of hiding it inside the generic transcript backlog: use separate work classes
for `transcript-asr` (fresh large-v3-turbo transcription for meetings with no source text) and
`transcript-align` (stable-ts forced alignment for untimed provider text). The manifest should preserve
the provider-text hash used for alignment and expose `alignment-disabled` / `needs_alignment` counts in
the status surface, so re-enabling alignment later is a backlog-drain decision, not a behavioral surprise.

**(b) Configurable prioritization policy.** A declarative, ordered list of **sort keys** applied
lexicographically to the pending set; ties fall through to the next key. Config (`site_config.yml`):
```yaml
backlog_priority:            # first → last
  - recency: desc            # newest meeting first
  - city_order               # then an explicit city ranking
  - body_order               # then body
city_order: [denton-tx, dallas-tx, ...]
```
Worked example: `recency:desc` + `city_order:[denton, dallas]` ⇒ the newest meeting from *either* city
goes first; a **same-day tie ⇒ Denton before Dallas**. Extensible key types to implement:
`recency`, `city_order`, `body_order`, `feed_visible_first` (currently-rendered episodes before deep
archive), `requested_first`, `strong_towns_first`, `population`. Each key is a small comparator
registered in a table so new keys are additive.

**Module shape.** Extract a small `citypods/ops/workqueue.py` (Codex's `ops/scheduler.py` idea):
`build_manifest(records, stages) -> WorkItems`, `order(work_items, policy) -> ordered`,
`mark(state)`/`lease()`/`release()`. `_materialize_set` becomes a thin caller of `order(...)` with the
configured policy (default policy = today's behavior, so this is **behavior-preserving** until a policy
is set). `run.py` consults the manifest to choose what each run/stage works on within the wall-clock
window. The transcript stage should claim exactly one ASR lane at a time; an align claim must not
opportunistically fall back to fresh transcription in the same runner unless a later policy explicitly
admits that extra model/cpu cost.

**Implementation paths.** (1) **manifest + policy together** (preferred — the policy needs the manifest's
pending set). (2) policy-only over the in-memory record set first, manifest later (faster to ship, but
no cross-run visibility/leases → would be redone for H6). **Lean: (1).**

**Files.** new `citypods/ops/__init__.py`, `citypods/ops/workqueue.py`; `citypods/stages.py`
(`_materialize_set` → `order(...)`); `citypods/run.py` (consult/update manifest); `citypods/config.py`
(`backlog_priority`, `city_order`); `citypods/report.py` + `status.html` (per-stage backlog by priority
bucket); `tests/test_workqueue.py` (the same-day tie example; each key type; default == legacy order;
lease acquire/expire).

**Acceptance:** with a configured policy, the order matches the worked example deterministically; with
no policy, output is byte-identical to today; the status page shows per-stage backlog by bucket; manifest
round-trips through statesync.

---

## H6 — ASR benchmark workflow (H6a) → sharded/separate ASR workflow (H6b)

**Problem.** ASR competes with audio/timeline inside the single enrich job and serializes through the
`pages` concurrency group; it's the dominant backlog and the source of recent OOM/preemption failures.
The `asr-bench` CLI exists ([`bench.py`](../citypods/bench.py)) but there is no benchmark **workflow** and
no **separate ASR workflow**.

**Sequencing (2026-06-10):** the two steps are split into **H6a** (Step 1, benchmark workflow) and
**H6b** (Step 2, sharded workflow). **H6a has no H5 dependency and is do-now** — it settles the model
choice and the *measured* cost of `word_timestamps` / segment-vs-word output (H12) **before** any backfill
re-transcribes the catalog, so the catalog is only re-done once. H6b still waits on H5's manifest for safe
cross-workflow state coordination.

**Design (two steps, in order — measurement before architecture).**

**Step 1 — manual benchmark workflow (H6a).** Add `.github/workflows/asr-bench.yml` (`workflow_dispatch`,
low concurrency, 1 matrix shard) that runs `citypods asr-bench` over a fixed set of episodes with known
official transcripts and a **fixed wall-clock budget**, recording **transcript-minutes/runner-hour**,
WER/alignment-failure rate, timeout/error rate, and model-load overhead. This lets model/beam/thread
changes (`asr_model`, `asr_beam_size`, `asr_compute_type`, threads) be compared safely before any
architecture change. (See also `spike/asr-model-benchmark`: compare `large-v3-turbo` vs `small.en` vs
`base.en`.)

**Step 2 — separate, sharded ASR workflow (H6b).** Once H5's manifest provides safe state coordination, add
`.github/workflows/asr.yml`: scheduled (e.g. daily) + `workflow_dispatch`, **own concurrency group**
distinct from `pages` (so it never hard-cancels a deploy), running a **matrix of source-sharded jobs**.
Coordination options (choose by safety): (1) **source-sharded concurrency** `asr-${shard}` with each
shard owning disjoint `source_key`s (preferred — no two writers touch one record file); (2) per-source
state files with merge-on-push; (3) a lease file in object storage (the manifest's `lease_owner`).
Render publishes **only completed** transcript artifacts and ignores in-progress ASR. Each job stays
**below the 6-hour cap** (a daily workflow does **not** grant extra single-job capacity).

**Alignment re-enable criteria.** Reintroduce stable-ts only as an **align-only** workflow lane:
pre-load the stable-ts model, do not load/run the fresh transcription model in that job, and do not run
audio encodes concurrently in the same runner. If alignment fails quality checks, mark the item for a
future `transcript-asr` claim rather than falling back inline. This keeps peak memory lower, makes
throughput measurements comparable, and prevents the 2026-06-09 failure mode where stable-ts alignment
stacked with ffmpeg work and GitHub terminated the runner with exit 143.

**Files.** `.github/workflows/asr-bench.yml`, `.github/workflows/asr.yml`; `citypods/cli.py` (ensure
`enrich --stage transcript --lane {transcribe,align} --shard k/N` or `--source <key>` selection exists);
`citypods/asr.py` (global throttle already added — verify under matrix; add explicit preload entrypoints
for the selected lane only); `citypods/ops/workqueue.py` (lease/claim for shards); docs in
ARCHITECTURE.md (workflow split) + this file.

**Acceptance:** the benchmark workflow emits a throughput/quality report artifact; the ASR workflow
clears transcript backlog across shards with **no record-file clobbering** (verified by a concurrent
two-shard dry run) and never cancels a Pages deploy.

---

## H7 — Contributor/agent handoff docs

**Shipped** in the doc-set PR (#226): `AGENTS.md` + `CLAUDE.md` pointer, `ARCHITECTURE.md`, the
`CONTRIBUTING.md` lifecycle / doc-update contract, and the PR + issue templates. This was the
documentation foundation the rest of Phase H builds on; no further code work. Kept in the H-sequence so
the numbering is continuous across `ROADMAP.md`, `review/11`, and this doc.

## H8 — Throughput maximization on the free 4-core runner — **Implemented in PR #235**

**Status.** Implemented in [PR #235](https://github.com/BashfulBits/city-meeting-podcasts/pull/235),
merged 2026-06-08. This section is frozen as the implementation design record.

**Problem (confirmed by the build-log analysis above, H-A/H-B/H-E).** The encode/ASR concurrency mix
starves the runner: ffmpeg `-threads` is **unpinned**, so two concurrent encodes drive `load` to 6–7 on
4 cores, and concurrent 100–155 MB encodes drop `mem_avail` to ~460 MiB. The runner agent is then
SIGTERM'd (exit 143) or "loses communication." The PR #227 concurrency cut (20→8 / 4→2) helped but
~half of scheduled runs still fail. This is no longer "confirm saturation" — it is the **active
deploy-killer**, so the levers below move from *candidates* to **committed changes**.

**Design — committed changes (do-now):**
1. **Pin ffmpeg `-threads`** in `citypods/media.py` so total encode threads across in-flight encodes
   stay ≤ core count (e.g. `-threads = max(1, cpu_count // max_encodes_per_source)`). This is the single
   biggest lever against H-A.
2. **Memory/CPU admission guard** — reuse the existing `run.py` snapshot helpers (`_proc_meminfo_bytes`,
   load avg) to gate *admission* of new encode/ASR work: when `mem_avail` falls below a threshold (e.g.
   ~1.5 GiB) or load exceeds cores, hold new workers until headroom returns instead of OOM-/SIGTERM-ing
   the job. Lighter than full resource-class pools; ships first.
3. **Count abandoned ASR daemon threads against the worker budget (H-E)** — on stop/timeout
   (`stages.py:1110/1127`) the semaphore is released while the CTranslate2 thread keeps running. Track
   live abandoned threads and treat them as occupying an ASR/CPU slot (or join with a short grace before
   admitting new ASR) so a timeout storm can't stack background inference on top of new work.

**Design — follow-on (after the do-now fires land):**
- **Profile** one representative enrich run: CPU utilization, peak RSS, and wall-time split across
  download / ffmpeg encode / silence / ASR (extend the structured build-log + `bench.py`).
- **Resource classes** (Codex throughput rec #1): treat chapter/link (network), encode (ffmpeg/RAM), ASR
  (CPU/RAM), future LLM (API/rate-limit) as separate pools with separate concurrency, scheduled via H5.
- Re-tune the mix (raise `asr_workers` only if RAM headroom allows; overlap CPU-bound ASR with
  network-bound chapter/link work; stagger model load to avoid load+encode RAM spikes).

**Implemented shape (PR #235).**
- `citypods/media.py`: `CommandFfmpeg` accepts a thread count and adds `-threads N` on AAC encode paths
  only (copy paths stay unmodified).
- `citypods/resources.py`: shared resource snapshots + `ResourceAdmission` wait loop for memory/load
  headroom.
- `citypods/run.py`: creates the guard for time-bounded non-dry-run Actions builds, derives ffmpeg
  threads from `audio_ffmpeg_threads` or `cpu_count // max_encodes_per_source`, and keeps heartbeat
  formatting on the same snapshot helper.
- `citypods/stages.py`: passes the guard into `AudioStage`/`TranscriptStage`; abandoned ASR daemon work
  retains the ASR semaphore slot until the background inference thread actually exits.
- `config/site_config.yml`: production defaults now run audio conservatively with
  `audio_ffmpeg_threads: 1`, `native_audio_max_active: 1`,
  `resource_guard_min_available_mb: 1536`, `resource_guard_max_load_per_cpu: 1.0`,
  `resource_guard_poll_seconds: 10`.

**Files.** `citypods/media.py`, `citypods/resources.py`, `citypods/run.py`, `citypods/stages.py`,
`config/site_config.yml`, `tests/test_encoder.py`, `tests/test_media.py`,
`tests/test_transcript_stage.py`, `tests/test_resources.py`. Mostly tuning + guards; no schema change.

**Acceptance:** across several consecutive Build & Deploy runs the heartbeat shows `load` staying near
core count and `mem_avail` never approaching the OOM floor, **no exit-143 / lost-communication kills**,
and enrich consistently uses most of its 204-min window; transcript-minutes/runner-hour improves
measurably vs the H6 Step-1 baseline; documented recommended settings.

---

## H9 — Evaluate free transcription-offload tiers

**Problem.** If a single tuned runner can't keep up with backlog at the target scale, where else can ASR
run for ~$0 without violating anyone's ToS?

**Design — decision matrix, no commitment.** Evaluate each option on: legitimacy/ToS, reliability,
integration cost, $/transcript-hour, quality — **against the execution-backend interface** (review/11
§5.5), so each option is a candidate *backend adapter* rather than a one-off integration (Modal /
self-hosted Mac-mini runner / AWS join the free tiers in the matrix below).

| Option | Notes |
|---|---|
| **GitHub Actions matrix sharding** (= H6 Step 2) | The primary legitimate lever: free, ≤20 concurrent jobs, native to the repo. ~1,500/wk at 4 shards, ~3,000 at 8. **Recommended first.** |
| Free ASR API quotas (e.g. Groq Whisper, Deepgram credits) | Fast, but quota-limited and external dependency; treat output as untrusted; watch for PII/ToS. |
| Free cloud compute (Oracle Free Tier ARM, Colab/Kaggle/HF Spaces) | Real free compute but reliability/ToS caveats (interactive-notebook ToS, session limits); higher integration + secrets surface. |

**Output.** A short recommendation in this doc + ARCHITECTURE.md once measured: almost certainly
"matrix sharding within Actions" unless backlog at 1,000+ feeds proves otherwise. Keep the others as
documented fallbacks.

**Diarization workload (Phase R #7, after H6).** Speaker diarization is CPU-heavy (pyannote v3 on CPU
is ~3–5× slower than transcription alone) and is the primary Phase R workload that would benefit from
GPU offload. Include a diarization benchmark in H9's evaluation: measure pyannote v3 on a free GPU API
(Groq, Colab) vs the wespeaker/speechbrain ECAPA-TDNN CPU path. The H9 output should include a
diarization $/speaker-hour estimate alongside the transcription baseline.

**Acceptance:** a written decision with measured numbers; the chosen lever wired (matrix sharding is H6).

---

## H10 — Fix ASR alignment type mismatch + broaden the fallback — **Implemented in PR #232**

**Status.** Shipped in [PR #232](https://github.com/BashfulBits/city-meeting-podcasts/pull/232) on
2026-06-08. This section is frozen as the implementation design record: `load_model()` now carries the
faster-whisper transcriber plus model metadata; `align()` lazily loads/caches a stable-ts
`load_faster_whisper(...)` model with `.align()`; `TranscriptStage` falls back to fresh transcription
for any alignment error.

**Problem (H-D, confirmed).** Every `mode=align` episode in the logs fails instantly with
`'WhisperModel' object has no attribute 'align'`. `load_model` (`citypods/asr.py:60`) returns a
`faster_whisper.WhisperModel` and caches it process-wide; `TranscriptStage` passes that same instance to
both `transcribe()` and `align()`. `transcribe()` works, but `align()` (`asr.py:158–187`) only works on a
**`stable_whisper`** model — given a plain `WhisperModel`, `wm.align(...)` raises `AttributeError`. Worse,
the fallback at `stages.py:1060` only catches `AlignmentQualityError`, so the `AttributeError` propagates
to the generic handler (`stages.py:1097`) and the episode is skipped at `stages.py:1148` with **no
transcript and no fresh-transcribe fallback**. Net effect: **caption-bearing feeds produce zero
transcripts**, invisibly (the site still deploys), while wasting the run's early minutes on
guaranteed-failing align attempts.

**Design (two parts — both required).**
1. **Make the align path use a model that actually has `.align`.** Either (a) have the alignment path
   load/cache a `stable_whisper.load_faster_whisper(...)` model, or (b) make `load_model` return a
   stable-ts faster-whisper model used for *both* paths. **Constraint to verify before choosing:**
   stable-ts's `.transcribe()` returns a `WhisperResult`, not the `(segments, info)` tuple that
   `transcribe()` currently unpacks (`asr.py:135` `segments, _ = wm.transcribe(...)`) — so option (b)
   requires adapting that call, and option (a) means two model instances in RAM (weigh against H-B
   memory pressure; prefer one shared instance if the API allows). Pin to the installed `stable-ts`
   version's actual API.
2. **Broaden the fallback** so an align failure for *any* reason (not just `AlignmentQualityError`) falls
   back to fresh `transcribe()` rather than skipping the episode — defensive even once part 1 lands.

**Files.** `citypods/asr.py` (`load_model` / `align`), `citypods/stages.py` (~999 mode select, 1048–1097
align/fallback), `tests/` (assert: align failure → fresh-transcribe fallback produces a VTT; a
caption-bearing fixture yields an *aligned* transcript).

**Acceptance:** a caption-bearing feed produces aligned transcripts (logged `transcript asr done … aligned`);
an injected align failure falls back to transcribe instead of producing nothing; no
`'WhisperModel' object has no attribute 'align'` in enrich logs.

---

## H11 — Deploy resilience — H11a **Implemented** (PRs #239/241/242/243/244/246/247); H11b pending after H5

**H11a status.** Native work gate, one-slot audio lane, ffmpeg filter-thread cap, per-child RSS/memory
logging, ASR teardown hardening, concurrency tuning, and Retry-After fix implemented across
[PR #239](https://github.com/BashfulBits/city-meeting-podcasts/pull/239) /
[#241](https://github.com/BashfulBits/city-meeting-podcasts/pull/241) /
[#242](https://github.com/BashfulBits/city-meeting-podcasts/pull/242) /
[#243](https://github.com/BashfulBits/city-meeting-podcasts/pull/243) /
[#244](https://github.com/BashfulBits/city-meeting-podcasts/pull/244) /
[#246](https://github.com/BashfulBits/city-meeting-podcasts/pull/246) /
[#247](https://github.com/BashfulBits/city-meeting-podcasts/pull/247), merged 2026-06-09/10. Design text
below is preserved as the implementation record.

> **Correction (2026-06-10).** An earlier stamp here read "three consecutive scheduled runs confirmed
> green **at** `native_audio_max_active: 4`." That overstated the evidence: the three green scheduled runs
> (02:54 / 08:03 / 15:22 UTC on 06-10) ran at **`1`**; #246 raised the cap to `4` at 16:29 and the stamp
> landed at 16:45 — before *any* scheduled run had completed at `4`, and the `2`-lane setting was never
> tested (the jump was 1→4). **Confirmation criterion for `4`:** ≥ 6 consecutive green scheduled runs
> (~24 h at the 4 h cron) with the heartbeat showing `mem_avail` never below the ~1.5 GiB floor and
> per-encode child peak RSS within budget — record those numbers here once met (the H2/C2 telemetry record
> makes this a one-line check). **Revert trigger:** any exit-143 / lost-comms kill or a `mem_avail` floor
> breach → drop to last-known-green (`1`) immediately, then retry `2` *with* the child-RSS metrics before
> attempting `4` again.

**H11b status.** Isolating enrich into its own workflow depends on H5's manifest/lease (not yet
started); the "Durable follow-up" paragraph below remains the active design.

**Problem (H-C, confirmed + H-F).** `continue-on-error: true` on the enrich step (`deploy.yml:163–193`)
plus the graceful-yield/“warn-if-killed” machinery was designed to keep the job green when enrich exits
non-zero. But the observed failures are **runner-level** SIGTERM (exit 143) / "lost communication" from
resource starvation — which terminate the whole job regardless of `continue-on-error`. The signature is
unmistakable: every step (incl. Enrich) shows ✓, "Warn if enrich was killed" is **skipped**
(`enrich.outcome == success`), yet the job is red. Because runs die ~23–30 min into the 204-min window
(**H-F**), enrich backlog also barely advances even though the site itself published fine.

**Design.**
- **Do-now (H11a): prevent the kill at its source.** The H8 resource guard (ffmpeg `-threads` pin +
  memory/CPU admission + abandoned-thread accounting) keeps the runner alive, which is the only thing
  that actually turns these runs green and recovers the ~180 min/run of lost budget (H-F). H11a began as
  the reliability acceptance tied to H8 landing; the 2026-06-09 follow-up below adds the missing
  resource-class exclusion that the first guard did not enforce.
- **2026-06-09 refinement:** disabling stable-ts alignment removed the two-model alignment spike, but
  the next run still died when fresh large-v3-turbo transcription overlapped two active ffmpeg encodes
  (`mem_avail` dropped to ~95 MiB, load >6/4). PR #239 added a native-work gate so ASR waits for active
  audio and blocks new audio admissions.
- **2026-06-09 H11a follow-up:** the first post-#239 deploy died while ASR was correctly waiting on the
  gate (`native gate wait kind=asr ... active_audio=2`). No ASR inference had started; the two active
  ffmpeg encodes alone drove `mem_avail` below 300 MiB and the runner exited 143. Until H5/H6 split heavy
  work into separate workflows, make production enrich a strict **one-core audio lane**: one global
  native audio slot and `ffmpeg -threads 1`, with ASR still exclusive and prioritized.
- **2026-06-09 measurement pass before loosening concurrency:** after the one-slot lane, ASR steady-state
  memory looked safe (~12+ GiB available) while ffmpeg still produced transient memory pressure. Before
  raising `native_audio_max_active`, pin ffmpeg filter workers too (`-filter_threads 1` and
  `-filter_complex_threads 1`) and log per-child peak RSS plus minimum runner `MemAvailable` for each
  guarded ffmpeg process. Keep `native_audio_max_active: 1` for this PR; use the new child metrics to
  decide whether a later `2`-lane audio experiment is safe.
- **Near-term throughput refinement after green baseline:** if several scheduled/push deploys stay
  green under the one-slot audio lane, evaluate loosening to a measured **bounded lane split**: ASR uses
  ~3 CPU threads, while at most one one-core audio lane continues low-risk download/cache/probe or
  `ffmpeg -threads 1` work. This should happen **before** returning to the rest of H1–H5 only if the
  gate proves stable, because it can recover network/download throughput without waiting for the full H5
  manifest. Do not simply let multiple audio encodes overlap again; the refined gate must account for
  active native work, keep a higher memory floor, and preserve ASR priority so an endless audio backlog
  cannot starve transcription.
- **Data needed for that tuning pass:** use the existing heartbeat (`mem_avail`, load, thread count,
  disk free) plus native-gate wait/acquire logs and per-stage `audio encode done` / `transcript asr done`
  timings. The measurement pass adds per-ffmpeg child peak RSS and min `MemAvailable`, which is enough
  to compare a stable exclusive baseline against a later 2-lane audio experiment. If the split decision
  still needs more precision, add active ffmpeg count, ASR-active state, and optional `/proc` child CPU
  snapshots before loosening the gate further.
- **Durable follow-up (H11b): isolate heavy enrich from the deploy job.** Move enrich into its **own
  workflow** (this is H6 Step 2) with a concurrency group distinct from `pages`, so the Pages deploy job
  *cannot* be marked red by enrich regardless of what happens to the enrich runner. Depends on **H5**'s
  manifest/lease for safe cross-workflow state coordination. Until then, the deploy job and enrich share
  a runner and a red job is possible.
- **Observability (small, alongside H11a):** because a starved runner can die before emitting a clean
  signal, ensure the heartbeat's last line is easy to locate post-mortem and consider a step-summary
  note recording the last heartbeat snapshot, so a genuine provider failure is distinguishable from a
  resource kill (complements H4 / H-G).

**Files.** H11a follow-up: `citypods/resources.py` (`NativeWorkGate` + global native audio cap),
`citypods/media.py` (`audio` shared gate, ffmpeg filter-thread caps, child RSS/min-available logging),
`citypods/stages.py` / `citypods/run.py` (`asr` exclusive gate + `native_audio_max_active` config),
`config/site_config.yml` (one-core production audio lane), `tests/test_resources.py`,
`tests/test_media.py`, `tests/test_encoder.py`. H11b: `.github/workflows/asr.yml` + `deploy.yml`
(remove/decouple the heavy enrich step), `citypods/ops/workqueue.py` (H5 lease), ARCHITECTURE.md
(workflow split) — sequenced after H5/H6.

**Acceptance:** (H11a) several consecutive scheduled Build & Deploy runs complete **green** with enrich
using most of its window and no exit-143/lost-comms kills under the one-slot audio lane. (H11a tuning,
optional) a measured 3-core ASR / 1-core audio-lane experiment improves completed transcript/audio work
per runner-hour while preserving the green-run streak and a safe memory floor. (H11b, later) a deploy job
is never marked red by enrich because enrich no longer runs in it; the separate ASR workflow clears backlog
without clobbering records (shared acceptance with H6).

---

## H12 — Transcript artifact: segment-cue VTT + word-JSON sidecar + version-aware re-transcribe — **Implemented in PR #253**

**Status.** Shipped in [PR #253](https://github.com/BashfulBits/city-meeting-podcasts/pull/253) on
2026-06-11. This section is frozen as the implementation design record.

**Maturity: L3.** Implemented as designed: `asr.py` returns `TranscriptArtifacts` (segment VTT +
word-JSON), `stages.py` stores both and does version-aware reuse keyed on the `{uid}-asr-` filename
prefix, `records.py` persists the sidecar key and GC-protects it, and `ASR_PIPELINE_VERSION` is `"3"`.

**Problem.** PR #249 made the ASR stage emit **one VTT cue per word** (`word_timestamps=True` → `_to_vtt`
loops over `seg.words`, [`asr.py`](../citypods/asr.py)). Three regressions: (i) `<podcast:transcript>`
consumers render cues as lines, so listeners get one-word-at-a-time captions; (ii) a 2 h meeting is ~18k
words → ~5× larger VTT (~1 MB vs ~200 KB) served to every app and fed into the review/13 search index,
whose core problem *is* transcript size; (iii) review/13's "click a line to seek" + search snippets want
sentence/segment granularity. Separately, the CHANGELOG claimed the `ASR_PIPELINE_VERSION = "2"` bump
re-transcribes existing transcripts "gradually" — but the transcript stage **reuses any present transcript
regardless of version** (`stages.py` step 1 fast-paths on `transcript_synced` before the recipe is
recomputed), so nothing is re-done.

**Decision (maintainer, 2026-06-10).** **Dual artifact** — player compatibility is the #1 goal and
server-side features need structured word data: a clean **segment-cue VTT** for `<podcast:transcript>`
*and* a **word-level JSON sidecar** for search / clip selection / diarization. Re-transcription is
**auto-gradual** (a version bump invalidates stored ASR transcripts; they re-do across budget-gated enrich
runs) — cheap now because few meetings are transcribed yet.

**Design.**
1. **`citypods/asr.py`** — `_to_vtt` reverts to **segment-level** cues (readable lines). `transcribe()`
   and `align()` each return **both** the segment-VTT bytes and a compact word-JSON
   (`{"version","basis","segments":[{"start","end","text","words":[{"w","s","e"}]}]}`), built from
   faster-whisper `seg.words` / stable-ts `result.segments`. Bump `ASR_PIPELINE_VERSION → "3"`.
2. **`citypods/stages.py`** — store the JSON under a sibling content-addressed key
   (`transcripts/<src>/<uid>-<recipe>.words.json`); set `ep.transcript_words_key` / `…_url`. **Make the
   reuse fast-path version-aware:** an ASR-produced transcript whose stored pipeline version differs from
   the current `ASR_PIPELINE_VERSION` is **not** fast-path-reused — it falls through to the ASR slot and
   re-transcribes (budget-/stop-gated, so it's gradual). Provider-supplied transcripts (no ASR version)
   are **never** invalidated by an ASR-version bump.
3. **`citypods/records.py`** — serialize/deserialize `transcript.words_key` / `words_url`; add the
   word-JSON key to `referenced_audio_keys` (the orphan-GC live set) so it isn't reaped.
4. **`citypods/models.py`** — `transcript_words_key` / `transcript_words_url` + a
   `transcript_pipeline_version` field (so "stale by version?" is a direct read, not re-derived from the
   recipe hash).

This makes the word-JSON the project's **third derived-artifact type** (audio · VTT · word-JSON) — the
YAGNI trigger for the deferred `DerivedArtifact` refactor (review/11 §6); do that refactor
opportunistically while this plumbing is open, not as a prerequisite.

**Backfill story (required by the pipeline-version convention, AGENTS.md).** The v2→v3 bump **does**
auto-invalidate ASR transcripts; they re-do gradually across scheduled enrich runs within the wall-clock
budget. Run **H6a** first so the model + `word_timestamps` cost is settled before the catalog
re-transcribes (re-do once).

**Tests.** `tests/test_asr.py` (segment-VTT shape; word-JSON shape + timings; align path emits both);
`tests/test_transcript_stage.py` (a stored v2 transcript re-does under v3; a provider transcript is **not**
invalidated; the word-JSON key is stored + GC-referenced); records round-trip.

**Acceptance.** A fresh transcript yields a clean segment-cue VTT *and* a word-JSON sidecar; an existing v2
transcript is re-transcribed on a later run (gradually, budget-gated); provider transcripts are untouched
by the bump; the word-JSON key survives orphan GC; `<podcast:transcript>` still validates.

---

## Module-split note (Codex maintainability, opportunistic)

While touching these areas, extract along natural seams (do **not** refactor for size alone): H5 creates
`citypods/ops/workqueue.py`; if ASR/transcript logic grows, split `citypods/stages/transcript.py`; if
admin grows, `citypods/report/{status,projection}.py`; issue reconciliation → `citypods/audit/issues.py`.

## Post-review code queue (recap)

Implement in order (**reprioritized 2026-06-08** per the build-log analysis — do-now reliability fires
first): **H10 (align fix, shipped PR #232)** → **H8 (resource guard, shipped PR #235)** → H11a (native
audio/ASR gate + one-slot audio lane + green-run acceptance, **shipped**; cap now at `4`) → **H12
(transcript artifact rework, shipped PR #253)** + **H6a (ASR benchmark, do-now)** → confirm
`native_audio_max_active: 4` against the A2 criterion (else revert toward `1`/`2`) → H1 (issues) →
H2 (incl. the C2 telemetry record) → H3 → H4 (incl. per-provider error rates) → H5 → H11b/H6b (isolate
enrich + sharded ASR) → H9 (against the execution-backend interface). Each
lands as its own PR with tests; on merge, follow the lifecycle contract (flip review/11, add CHANGELOG,
stamp this doc per item).
