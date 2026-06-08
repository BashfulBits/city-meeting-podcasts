# review/12 — Hardening & Efficiency (Phase H)

**Maturity: L3 (development-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) Phase H ·
last updated 2026-06-08**

> When the items here ship, stamp this doc "Implemented in PR #N", flip the `review/11` catalog rows to
> Shipped, and add CHANGELOG entries (see the lifecycle contract in CONTRIBUTING.md).

## Purpose & context

Most of the heavy product machinery (timeline/EDL, audio-cleanup band, ASR, content permanence) has
**shipped**. The current risk has shifted from "missing features" to **operational throughput and
reliability** on free GitHub-hosted runners — ASR now dominates the backlog and recent Build & Deploy
runs clustered failures around ASR preemption/OOM (Codex review §"Actions Evidence"). This phase makes
the pipeline **stable, observable, and maximally fast on the free tier** before new user-facing surface
is added.

**Current production constraints** (`config/site_config.yml`): 4-hour deploy cron; enrich window
`run_time_budget_minutes: 240 × budget_safety: 0.85 ≈ 204 min`; `max_workers: 20` (source-level),
`max_encodes_per_source: 4`, `asr_workers: 1`; per-encode timeout 45 min. GitHub free public-repo
runners: 6-hour job cap, ≤20 concurrent standard jobs, ~1,000 `GITHUB_TOKEN` req/hr.

**Throughput anchors** (review/03 §7): self-hosted faster-whisper "base" int8 on a 4-vCPU runner ≈ 4–6×
realtime (a 2 h meeting ≈ 20–30 min). Single 4 h-cron job ≈ 380–400 transcript-meetings/week; a 4-job
matrix ≈ ~1,500/week; 8-job ≈ ~3,000/week. Reuse-first means only ~34 mostly-Granicus feeds need ASR.
Current ~80 feeds generate ~55 new meetings/week, so the free tier comfortably covers **inflow**; only
the initial **backlog** takes calendar time.

**Sequencing (DAG).** Cheap reconciliation first, then observability, then the manifest, then ASR
isolation/efficiency:

```
H1 (docs/issues) ─┐
H2 (projection)  ─┼─► H4 (feed-health states, uses H2 ETAs) ─► H5 (backlog manifest + priority)
H3 (valid. gate) ─┘                                                 │
                                                                    ▼
                                              H8 (4-core saturation) ─► H6 (benchmark → sharded ASR)
                                                                    ▲
                                                       H9 (offload evaluation feeds H6)
```

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
window.

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

**Files.** `.github/workflows/asr-bench.yml`, `.github/workflows/asr.yml`; `citypods/cli.py` (ensure
`enrich --stage transcript --shard k/N` or `--source <key>` selection exists); `citypods/asr.py`
(global throttle already added — verify under matrix); `citypods/ops/workqueue.py` (lease/claim for
shards); docs in ARCHITECTURE.md (workflow split) + this file.

**Acceptance:** the benchmark workflow emits a throughput/quality report artifact; the ASR workflow
clears transcript backlog across shards with **no record-file clobbering** (verified by a concurrent
two-shard dry run) and never cancels a Pages deploy.

---

## H8 — Throughput maximization on the free 4-core runner

**Problem.** Before paying for sharding complexity, confirm a single free runner is **saturated**.
Recent exit-137 (OOM) and exit-143 (preemption) failures suggest the encode/ASR concurrency mix is not
yet tuned for the 4-vCPU / ~16 GB box.

**Design (measure → tune).**
- **Profile** one representative enrich run: CPU utilization, peak RSS, and wall-time split across
  download / ffmpeg encode / silence / ASR (extend the structured build-log + `bench.py`).
- **Tune the concurrency mix** so cores stay busy without OOM: ASR (CPU+RAM heavy, one model per worker)
  vs `max_encodes_per_source` (ffmpeg) vs `max_workers` (network-bound source fetch). Candidate levers:
  raise `asr_workers` only if RAM headroom allows; pin ffmpeg `-threads`; **overlap CPU-bound ASR with
  network-bound chapter/link work** (different resource class); stagger model load to avoid load+encode
  RAM spikes (the exit-137 cause noted in Codex evidence).
- **Resource classes** (Codex throughput rec #1): treat chapter/link (network), encode (ffmpeg/RAM), ASR
  (CPU/RAM), future LLM (API/rate-limit) as separate pools with separate concurrency, scheduled via H5.
- Add a **memory guard**: if available RAM drops below a threshold, reduce in-flight ASR/encode workers
  rather than OOM-killing the job.

**Files.** `citypods/config.py` (expose/validate the levers), `citypods/run.py` / `citypods/stages.py`
(resource-class-aware concurrency + memory guard), `citypods/bench.py` (throughput profiling output),
`config/site_config.yml` (tuned defaults with comments). Mostly tuning + guards; no schema change.

**Acceptance:** a representative run shows sustained high CPU utilization with no OOM across several
consecutive Build & Deploy runs; transcript-minutes/runner-hour improves measurably vs the H6 Step-1
baseline; documented recommended settings.

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

## Module-split note (Codex maintainability, opportunistic)

While touching these areas, extract along natural seams (do **not** refactor for size alone): H5 creates
`citypods/ops/workqueue.py`; if ASR/transcript logic grows, split `citypods/stages/transcript.py`; if
admin grows, `citypods/report/{status,projection}.py`; issue reconciliation → `citypods/audit/issues.py`.

## Post-review code queue (recap)

Implement in order: **H1 (issues) → H2 → H3 → H4 → H5 → H8 → H6 → H9**. Each lands as its own PR with
tests; on merge, follow the lifecycle contract (flip review/11, add CHANGELOG, stamp this doc per item).
