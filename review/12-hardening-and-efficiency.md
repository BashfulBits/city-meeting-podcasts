# review/12 — Hardening & Efficiency (Phase H)

**Maturity: L3 (development-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) Phase H ·
last updated 2026-06-08**

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

**Files.** `citypods/report.py` (both `ModelInputs` construction sites; `to_markdown`; `build_status`);
`citypods/assets/status.html` (add transcript-backlog + ETA fields); `tests/test_report.py` (cover the
**no-cap wall-clock default** as the primary case; assert transcript backlog row; assert the legacy-cap
message only appears when a cap is explicitly set).

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

## H6 — ASR benchmark workflow → sharded/separate ASR workflow

**Problem.** ASR competes with audio/timeline inside the single enrich job and serializes through the
`pages` concurrency group; it's the dominant backlog and the source of recent OOM/preemption failures.
The `asr-bench` CLI exists ([`bench.py`](../citypods/bench.py)) but there is no benchmark **workflow** and
no **separate ASR workflow**.

**Design (two steps, in order — measurement before architecture).**

**Step 1 — manual benchmark workflow.** Add `.github/workflows/asr-bench.yml` (`workflow_dispatch`,
low concurrency, 1 matrix shard) that runs `citypods asr-bench` over a fixed set of episodes with known
official transcripts and a **fixed wall-clock budget**, recording **transcript-minutes/runner-hour**,
WER/alignment-failure rate, timeout/error rate, and model-load overhead. This lets model/beam/thread
changes (`asr_model`, `asr_beam_size`, `asr_compute_type`, threads) be compared safely before any
architecture change. (See also `spike/asr-model-benchmark`: compare `large-v3-turbo` vs `small.en` vs
`base.en`.)

**Step 2 — separate, sharded ASR workflow.** Once H5's manifest provides safe state coordination, add
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
integration cost, $/transcript-hour, quality.

| Option | Notes |
|---|---|
| **GitHub Actions matrix sharding** (= H6 Step 2) | The primary legitimate lever: free, ≤20 concurrent jobs, native to the repo. ~1,500/wk at 4 shards, ~3,000 at 8. **Recommended first.** |
| Free ASR API quotas (e.g. Groq Whisper, Deepgram credits) | Fast, but quota-limited and external dependency; treat output as untrusted; watch for PII/ToS. |
| Free cloud compute (Oracle Free Tier ARM, Colab/Kaggle/HF Spaces) | Real free compute but reliability/ToS caveats (interactive-notebook ToS, session limits); higher integration + secrets surface. |

**Output.** A short recommendation in this doc + ARCHITECTURE.md once measured: almost certainly
"matrix sharding within Actions" unless backlog at 1,000+ feeds proves otherwise. Keep the others as
documented fallbacks.

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

## H11 — Deploy resilience: survive (then contain) runner-level kills — **PRIORITY: do-now (new 2026-06-08)**

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

## Module-split note (Codex maintainability, opportunistic)

While touching these areas, extract along natural seams (do **not** refactor for size alone): H5 creates
`citypods/ops/workqueue.py`; if ASR/transcript logic grows, split `citypods/stages/transcript.py`; if
admin grows, `citypods/report/{status,projection}.py`; issue reconciliation → `citypods/audit/issues.py`.

## Post-review code queue (recap)

Implement in order (**reprioritized 2026-06-08** per the build-log analysis — do-now reliability fires
first): **H10 (align fix, shipped PR #232)** → **H8 (resource guard, shipped PR #235)** → H11a (native
audio/ASR gate + one-slot audio lane + green-run acceptance) → optional H11a tuning (3-core ASR /
1-core audio lane, only after green baseline) → H1 (issues) → H2 → H3 → H4 → H5 → H11b/H6 (isolate
enrich + sharded ASR) → H9. Each
lands as its own PR with tests; on merge, follow the lifecycle contract (flip review/11, add CHANGELOG,
stamp this doc per item).
