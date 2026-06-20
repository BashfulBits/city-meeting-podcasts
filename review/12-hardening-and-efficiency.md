# review/12 — Hardening & Efficiency (Phase H)

**Maturity: L3 (development-ready) · breakout of [`review/11`](11-technical-design-roadmap.md) Phase H ·
last updated 2026-06-19 (H14 long-audio/routing design + H9 evaluation scope)**

> When the items here ship, stamp this doc "Implemented in PR #N", flip the `review/11` catalog rows to
> Shipped, and add CHANGELOG entries (see the lifecycle contract in CONTRIBUTING.md).

> **2026-06-16 — Phase H tail.** With H1–H5/H6a/H7/H8/H10/H11a/H12/#39 shipped, seven interlocking items remain,
> designed below, in order: **H13** GPU/ASR execution-backend interface (+ `local` adapter — the pre-1.0
> lock) → **H11b** render-only `deploy.yml` (render stops persisting records) → **H6b** split audio + ASR
> into `audio.yml` + `asr.yml`, sharded + scoped state-push + lanes → **Granicus media reliability follow-up**
> (aggregate concurrency reduction + endpoint coordination) → **H14** Modal + Beam free-tier transcription
> adapters (H14b/H14c, async dispatch from `asr.yml`) → **H9** combined-throughput evaluation (diarization
> $/speaker-hour, 80-feed backlog 1-month gate). The maintainer pulled the external-worker *build* (Modal
> + Beam) into Phase H so "compute is pluggable" ships proven by two live GPU adapters before 1.0; the
> first **LLM-API** adapter (the other half of the §5.5 interface) lands with R3/R4.

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

> **Implemented.** GH#154 closed (28 `<podcast:transcript>` tags confirmed live 2026-06-11); GH#110 narrowed to backfill/ops; GH#141 marked umbrella-only.

**Problem.** `ROADMAP.md`/`review/01` listed shipped work as future; the issue tracker mixes real
breakage with catch-up status; `<podcast:transcript>` (GH#154) is already emitted.

**Done in this doc set:** ROADMAP rewrite, CHANGELOG, ARCHITECTURE, review/11 catalog, review/01 banner,
ADD_CITY.md wall-clock fix.

**Issue reconciliation (complete):**
- GH#154 closed — production feed verified (28 tags in Arlington TX feed).
- GH#110 narrowed — title + body updated to "backfill + ops follow-up"; implementation framing removed.
- GH#141 kept open as umbrella — comment added listing remaining Phase R features (#153/#155/#156/#157).
- Feed-health issue cleanup deferred to H4 (don't bulk-close by hand).

**Acceptance:** `gh issue list` shows no open issue describing already-shipped work as unbuilt; review/11
§7 checklist passes.

---

## H2 — Projection wall-clock fix + backlog rows

> **Implemented** — `per_run_cap` defaults to `None` (wall-clock bound) when `materialize_budget_per_run`
> is absent; `to_markdown` updated to say "delete the cap" not "set a new value"; `measured_inputs`
> calibrates from `materialize_encoded` (real encodes only); `_feed_row` adds bytes-based `hours_hosted`
> fallback for Swagit/CivicPlus; `NativeWorkGate.total_wait_seconds` accumulator added; `_ResourceHeartbeat`
> samples `peak_load_per_cpu` + `min_mem_avail_bytes` via `current_snapshot()`; `_record_run_history`
> writes `peak_load_per_cpu`, `min_mem_avail_mb`, `window_used_pct`, `gate_wait_seconds` to
> `run_history.jsonl`; `build_status` returns `audio_backlog` + `transcript_backlog` sub-dicts with ETAs.
> 8 new tests.

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

> **Implemented** — `validate_build(output_dir, known_empty)` added to `citypods/validate.py`:
> scans all `*.xml` under `docs/`, skips redirect feeds (`<itunes:new-feed-url>`), demotes empty
> feeds for slugs in `known_empty` to warnings, fatals on everything else. `citypods validate-build`
> CLI subcommand exits non-zero on fatals. `deploy.yml` gate step added after "Render feeds" /
> "Resource report", before "Upload Pages artifact". 11 new tests.

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

> **Implemented.** All three design sub-deliverables shipped:
> - *Rehost-backlog triage*: catching-up → suppressed; stalled (≥ 3/5 active runs, 0 hosted) →
>   `WARN rehost-backlog`; provider failures stay `ERROR`. `_load_run_history` + `run_history`
>   threaded through `audit_city` / `audit_all`. 6 new tests.
> - *Provider error-rate tracking (B3)*: `_record_run_history` writes `provider_errors: {name: n}`
>   per run; `check_provider_error_rates` (new in `audit.py`) fires `WARN provider-errors:<name>`
>   when a provider has source-fetch failures in ≥ 2 of the last 5 runs. 8 new tests.
> - *Auto-comment on state transitions*: `audit_feeds.py reconcile` now calls `issue comment` with
>   a timestamped state summary when an issue's body changes, in addition to `issue edit --body`.
>   5 new tests in `tests/test_audit_feeds.py`.

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

> **✓ Implemented in [#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263) (ordering
> engine) · [#264](https://github.com/BashfulBits/city-meeting-podcasts/pull/264) (manifest + lean
> sidecar + status + light city-ordering) · [#265](https://github.com/BashfulBits/city-meeting-podcasts/pull/265)
> (global two-pass enrich queue).** This section is frozen as the design of record; the "Decisions
> locked" block and the "Async dispatch & per-episode ordering" note below are the canonical reference
> for the future external-backend work (H9/H6b). Competitive lease acquisition + per-item incremental
> persistence are deferred to H6b/H9, where the external backend first exercises them.

**Problem.** Scheduling is **implicit**: `_materialize_set(episodes, max_per_body)`
([`stages.py:91`](../citypods/stages.py)) picks the top-N-per-body, and whichever source enters the
thread pool first consumes the window. There is no deliberate "recent visible audio first, then
transcripts, then deep archive," no per-stage backlog visibility, and no safe basis for splitting ASR
into its own workflow (H6).

**Decisions locked (2026-06-11) — build to these.** The design below is settled into these choices
(rationale folded in); where the older prose differs, the decisions win.

- **Manifest = hybrid, not authoritative.** Per-source records stay canonical; the pending set is
  *derived* fresh each run (a killed run self-heals — no stale `running` rows). A thin sidecar
  (`state/work/*.json`) persists only what records can't reconstruct: leases, backoff/`next_retry`,
  `observed_seconds`. Because the pending set is derived, the **ordering policy needs no persisted
  file** — which is what lets the work split into two PRs (below).
- **Default order is deterministic and behavior-preserving.** With no `backlog_priority`, the selected
  set is byte-identical to today's `_materialize_set` (top-`max_episodes`/body) and the previously
  *nondeterministic* cross-source processing order becomes deterministic (recency desc). Rendered
  output is unchanged; only the order a budget-limited run picks work in becomes reproducible.
- **Comparator registry (additive table).** Ships: `recency` (`order: asc|desc`, optional
  `within_days: N` **horizon** — inside the window sort newest-first `(0,-ts)`, **beyond it collapse to
  a constant `(1,·)`** so the *next* key governs the backlog instead of date neutering it);
  `recent_first: N` (boolean recent-vs-old bucket); `city_order` (explicit slug list; **partial lists
  allowed** — named cities rank by index, every unnamed city shares a sentinel rank and falls through
  to the next key; placement matters — `city_order` first = city-greedy, after `recency` = same-day
  tie-break only); `body_order`; `feed_visible_first`. Reserved stubs: `requested_first`,
  `strong_towns_first`, `population`.
- **Production policy (initial).** `backlog_priority: [recency: {order: desc, within_days: 30}]` and
  nothing else — keep the last 30 days complete first, then fall to the deterministic default. Every
  other comparator ships and is unit-tested but is unconfigured in production until chosen.
- **Priority buckets are reserved-but-inert today.** feed-visible ≡ materialized ≡
  top-`max_episodes`(50)/body — the *same* set (`projection.py:53` "renders/materializes (==
  max_episodes)"; the `_materialize_set` docstring "exactly what some feed can display — never the deep
  archive"). `max_archive_items`(5000) is **retention only**. So `recent_archive`/`deep_archive` start
  empty and `feed_visible_first` is a registered no-op; they activate only under a future
  **archive-backfill** feature (last bullet).
- **Diarization-forward manifest schema (reserve now, emit nothing).** Key work items by **output
  artifact**: `work_class ∈ {audio, transcript-asr, transcript-align}` now, with `diarization` and
  `transcript-merge` **reserved**. Each item carries its own `stage_version` + `input_hashes` (surgical
  invalidation), and leases are **groupable** (one `lease_owner` may hold `{transcript-asr,
  diarization}` for an episode). This deliberately **defers the fuse-vs-separate execution decision to
  the backend adapter** (H9+) because it is platform-dependent — GPU/Modal favors whisperX-style fusion
  (decode audio once, both models resident); a self-hosted Mac Mini may run pyannote/MPS +
  faster-whisper/CoreML separately. `speakers.json` will be the content-addressed, version-stamped,
  orphan-GC-protected peer of H12's `words.json`. H5 writes no diarization logic.
  - **Cross-lane write isolation is already lane/block-registry-driven (added with the 2026-06-16
    clobber fix — §H6).** When the `diarize` lane lands, wire it through the same three registries and
    nothing else changes in the merge/push/gating machinery:
      1. `records.ARTIFACT_BLOCKS` — add `"speakers"` (the new independently-owned record block).
      2. `records._LANE_OWNED_BLOCKS` — add `"diarize": frozenset({"speakers"})`. `protected_blocks_for_lane`
         then automatically makes every *other* lane preserve a concurrently-written `speakers` block, and
         makes the `diarize` lane preserve the `audio`/`transcript` blocks it doesn't own.
      3. `stages.LANE_STAGES` — add `"diarize": frozenset({"diarize"})` (the global-queue pass split and
         `run_stages` both read this) so the diarize lane runs only its own stage.
    Also extend `episode_to_record` / `record_to_episode` / `referenced_audio_keys` with the `speakers`
    block (so its content-addressed object is GC-protected like `audio`/`transcript`). **Keep owned-stages
    and owned-blocks consistent** — a lane that runs a stage writing a block must own that block, or the
    foreign-block merge will discard its output. The async transcribe→diarize *ordering* and the
    fuse-vs-separate execution decision remain H9/H6b dispatch concerns (see the async-dispatch bullet); the
    registries above only govern *who may overwrite which block* on the shared `episodes.json` (until the
    per-stage-file split in §H6 removes the shared file entirely).
- **PR split (revised 2026-06-12 → three PRs).** **PR1** (shipped, [#263](https://github.com/BashfulBits/city-meeting-podcasts/pull/263)) =
  `ops/workqueue.py` + comparator registry + config parsing + stages wiring — pure within-source
  ordering, *no new persisted state*, behavior-preserving. **PR2** = the derived **work manifest**
  (`build_manifest`) + **lean sidecar** (persist `state/work.json` + `lease`/`release`/`is_leased` API,
  the H6b substrate — leases inert in a single workflow) + **status surface** (backlog by work-class /
  bucket + alignment-disabled counts) + **light city-ordering** (`order_cities_by_policy` — submit
  cities to the pool in policy order; coarse cross-source priority). **PR3** (implemented; async-aware) =
  the **global two-pass queue** for the `enrich` phase: prepare every source in parallel, then process
  the backlog **newest-everywhere-first across all sources** as (1) an on-runner **AUDIO pass**
  (`chapters→timeline→remap→audio`, gated by the H8/H11a `native_work_gate`) followed by (2) a
  **decoupled TRANSCRIPT pass**. `all`/`render` keep the per-city pool untouched. PR3 is the
  execution-model foundation **H6b/H11b** build on — see the dispatch note + the async-dispatch decision
  below.
- **Transcribe/diarize are async "over the wall" (decided 2026-06-12).** Audio re-hosting stays
  **on-runner** (the feed needs the M4A enclosure immediately). Transcription and diarization will run
  on **external workers** (Modal / Beam / self-hosted Mac Mini) — the enrich run **dispatches and does
  not await**; the worker writes results into the durable state on its own clock, and the **next** build
  & deploy's `render` phase reconciles them onto the feed (exactly like a not-yet-hosted episode does
  today). PR2's manifest `state` + `lease_owner`/`lease_expires` is the coordination medium. PR3 already
  models this: the transcript pass is a **separate, dispatch-not-await-ready** pass — it runs
  faster-whisper on-runner *today*, but swapping in the external backend replaces only that pass, and
  neither the audio queue nor feed rendering ever assumes in-run completion. The external backend itself
  is **H9/H6b**; ordering of async work is enforced at **dispatch** time (the policy decides submission
  order; the backend runs its own queue). This is the concrete shape of VISION's "compute is pluggable".
- **"Byte-identical", precisely.** The acceptance criterion constrains the *refactor at fixed config*
  (inert by default), parameterized by the knobs — it does **not** freeze `max_episodes` or forbid
  features. Raising `max_episodes` (feed + materialize stay coupled) is a live knob. **Archive-backfill**
  (decouple feed-visibility from materialize depth so the deep archive drains over runs) is a separate
  **opt-in** feature with its own ASR/encode/storage cost; opt-in keeps the default byte-identical, so
  it is a cohesion/cost call, not a prohibition. Reserved buckets + windowed recency (the natural
  throttle) let it land later with no manifest migration.

**Design (Adopt + EXTEND — builds on existing primitives, adds a first-class, configurable policy).**
*Refined by the **Decisions locked** block above; where they differ, the decisions win.*

**(a) Durable work manifest.** Alongside the existing per-source records + `run_history.jsonl`, write a
lightweight manifest the run consults and updates:
```
state/work/{audio,transcript,timeline}.json   # or one work.json keyed by stage
```
Each work item: `source_key`, `episode_uid`, `work_class` (artifact-keyed — see Decisions),
`stage_version`, `input_hashes`, `state` (`queued|running|done|backoff|dead`), `priority_bucket`
(`feed_visible|recent_archive|deep_archive`), `est_seconds`, `observed_seconds`, `last_error`,
`next_retry`, and (when concurrent workflows arrive in H6) `lease_owner`/`lease_expires`. Under the
**hybrid** model only the non-derivable fields (leases, backoff/`next_retry`, `observed_seconds`)
actually persist to the sidecar; the rest is derived from records each run. This makes "why is this
feed still missing audio/transcripts?" answerable in the status page, and is the **lease/merge
substrate** ASR sharding needs.

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
backlog_priority:            # first → last; ties fall through to the next key
  - recency: {order: desc, within_days: 30}   # PRODUCTION: last 30d newest-first; older → next key
  # keys below ship + are tested, but stay unconfigured in production until chosen:
  # - city_order             # explicit ranking; partial list ok (unnamed fall through)
  # - body_order             # then body
city_order: [denton-tx, dallas-tx, ...]
```
Worked example (the *city-greedy* arrangement `city_order` first): `city_order:[denton, dallas]` ⇒ all
Denton, then all Dallas, then every other city by the next key; a **same-day tie within a city ⇒**
ordered by whatever follows. Key types: `recency` (optional `within_days` horizon — collapses beyond
the window so later keys fire), `recent_first:N`, `city_order` (partial lists fall through),
`body_order`, `feed_visible_first` (**reserved/inert today** — see the buckets decision), and reserved
stubs `requested_first`, `strong_towns_first`, `population`. Each key is a small comparator registered
in a table so new keys are additive.

**Module shape.** Extract a small `citypods/ops/workqueue.py` (Codex's `ops/scheduler.py` idea):
`build_manifest(records, stages) -> WorkItems`, `order(work_items, policy) -> ordered`,
`mark(state)`/`lease()`/`release()`. `_materialize_set` becomes a thin caller of `order(...)` with the
configured policy (default policy = today's behavior, so this is **behavior-preserving** until a policy
is set). `run.py` consults the manifest to choose what each run/stage works on within the wall-clock
window. The transcript stage should claim exactly one ASR lane at a time; an align claim must not
opportunistically fall back to fresh transcription in the same runner unless a later policy explicitly
admits that extra model/cpu cost.

**Dispatch note (PR2 light vs PR3 full).** The current model dispatches one `_process_city` per city to
an 8-worker pool; each city runs its *full* stage pipeline (`fetch→all-stages→persist`) under a per-source
lock, and the expensive native work is globally serialized only by the `native_work_gate`, granted in
*arrival* order. True newest-everywhere-first therefore needs the global backlog enumerated up front and a
worker queue pulling in policy order — i.e. splitting the per-source pipeline into a cheap phase + a global
expensive-work queue with per-item persistence. That is **PR3** (a parallel-execution rewrite on top of the
H8/H11a gate machinery). **PR2** delivers the cheaper, low-risk approximation: `order_cities_by_policy`
submits cities to the pool in policy order (a started city still drains its own within-source-ordered
backlog).

**Async dispatch & per-episode ordering (PR3 → H6b/H9).** The per-episode pipeline is
`audio → transcribe → diarize`, and PR3 enforces that order **without assuming any step finishes in the
run that started it**:

- *Within a run:* the enrich orchestrator runs **sequential passes** — the audio pass completes before
  the transcript pass begins — and each pass is **dependency-gated**: the transcript pass includes only
  episodes whose audio is hosted (`ep.hosted_audio_url`), and a future diarize pass only those with a
  transcript. Within one episode `run_stages` runs its stages in order; the worker pool parallelizes only
  *across* episodes (throughput), never the steps inside an episode.
- *Across runs (the async future):* transcribe/diarize run on **external workers** — the run
  **dispatches and does not await**; results land in durable state and the next deploy's `render`
  reconciles them onto the feed. The dependency order then holds via the manifest `state`: transcribe is
  eligible only when `audio` is `done`, diarize only when the transcript is `done`. So `1→2→3` holds
  whether a step runs in-pass today or lands from a worker two deploys later.
- *Fused vs separate execution is the backend adapter's call* (platform-dependent), expressed through
  **groupable leases** — one `lease_owner` may hold several work items of one episode:
  - A **fused** worker (e.g. whisperX on a GPU/Modal box) claims the episode's `transcript-*` **and**
    `diarization` items under a single lease, runs them in series, writes both, marks both `done` — **one
    dispatch, one reconcile** (audio in run N → both land in run N+1). The shared lease is also what stops
    the orchestrator from separately re-dispatching diarize.
  - A **separate** setup (e.g. transcribe on Modal, diarize on the Mac Mini) takes independent leases —
    **two dispatches** (transcript lands N+1, diarize dispatched N+1, lands N+2).
  The manifest keeps them as **distinct, independently-versioned artifacts** (so re-diarizing without
  re-transcribing stays possible) but never bakes in the grouping — chosen per platform in **H9/H6b**.
  PR3 ships only the on-runner audio pass + the decoupled (on-runner-today) transcript pass; the
  `lease_owner`/`lease_expires` fields + the reserved `diarization` work-class are the PR2 substrate it
  builds on.

**Implementation paths.** Settled: the **hybrid** model (Decisions) derives the pending set from
records, so the ordering policy needs *no* persisted file — which splits the work into PR1 (policy,
behavior-preserving), PR2 (manifest + lean sidecar + status + light ordering), and PR3 (full global
queue). This supersedes the earlier "manifest + policy must ship together" lean, which assumed an
authoritative manifest holding the pending set.

**Files.**
- **PR1** (shipped) — new `citypods/ops/__init__.py`, `citypods/ops/workqueue.py` (`WorkItem`, comparator
  registry, `order(items, policy)`); `citypods/stages.py` (`_materialize_set` → `order(...)`,
  `transcript-asr`/`transcript-align` work-classes); `citypods/config.py` (`backlog_priority`,
  `city_order`); `config/site_config.yml` (the production `recency:{order:desc, within_days:30}` line);
  `tests/test_workqueue.py` (windowed-recency collapse-beyond-horizon; partial-`city_order`
  fallthrough; same-day tie; each key type; **default == legacy selection**).
- **PR2** — `ops/workqueue.py` (`build_manifest` + `manifest_counts`; `save_manifest`/`load_manifest`;
  `lease`/`release`/`is_leased`; `order_cities_by_policy`); `citypods/run.py` (light city-ordering before
  dispatch + persist `state/work.json` each enrich/all run); `citypods/report.py` + `assets/status.html`
  (backlog `by_work_class` + `alignment_disabled` + `deep_archive_items`); tests in `test_workqueue.py`
  (manifest derivation/counts, save/load round-trip, lease acquire/expire/release), `test_report.py`
  (status by-work-class), `test_run.py` (manifest written; policy-ordered build succeeds).
- **PR3** (implemented) — `citypods/stages.py` (`run_stages(..., quiet=)` so per-episode dispatch doesn't
  flood logs); `citypods/run.py` (`SourcePipeline.fetch_merge`/`accumulate_stats`/`persist_source`
  extracted from `enrich` behavior-preservingly; `_order_global_candidates` + `_run_enrich_global_queue`
  two-pass orchestrator; `build()` routes `phase=="enrich"` to it and leaves `all`/`render` on the
  per-city pool); `tests/test_run.py` (cross-source newest-first ordering; identity without policy;
  enrich two-pass + manifest; the new enrich logging contract). Records persist after the passes —
  content-addressed audio means a graceful stop (or kill) never loses an encode. **Competitive lease
  acquisition + per-item incremental persistence are deferred to H6b/H9**, where the external backend
  first exercises them.

**Acceptance.** **PR1:** default (no policy) selection byte-identical to today; comparators per the worked
examples. **PR2:** the status page shows backlog by work-class/bucket; the manifest round-trips through
statesync; a configured policy reorders city submission. **PR3:** the global candidate queue orders
newest-everywhere-first across sources with a configured policy; the enrich phase runs the on-runner audio
pass then the decoupled transcript pass; the existing enrich contract (encode set, persist, run_history,
no render) is preserved; `all`/`render` are untouched.

---

## H6 — ASR benchmark workflow (H6a implemented in PR #256) → sharded/separate ASR workflow (H6b **Implemented**, [#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273))

> **H6b status — Implemented in [#273](https://github.com/BashfulBits/city-meeting-podcasts/issues/273).**
> The combined `enrich.yml` (H11b) is replaced by `audio.yml` (`--lane audio`) and `asr.yml`
> (`--lane transcribe`), each a `strategy.matrix.shard`=4 job on its own concurrency group
> (`audio`/`asr`, distinct from `pages`). `citypods enrich` gained `--shard K/N` / `--source KEY` /
> `--lane {audio,transcribe,align}`; `run.py` filters cities by source-atomic
> `records.shard_assignment(source_key)` and threads the lane into the two-pass queue (audio pass vs
> transcript pass); a sharded/scoped run
> uses the H11b hooks `push_state(only_prefixes=…)` + `reconcile_state(full_run=False)` to push back
> only owned records and skip the orphan sweep. **Per the maintainer's 2026-06-14 decision, `asr.yml`
> runs transcribe-only for now** — the `align` lane (stable-ts, preloads the alignment model, never
> loads faster-whisper, processes only episodes with a source transcript) is implemented and unit-
> tested but **not scheduled**; caption-bearing feeds get fresh transcription until forced alignment
> is re-enabled as its own lane (the "Alignment re-enable criteria" below + a future claim/lease). The
> "Step 2" and "record-write race" design below is the frozen record this implemented. Competitive
> leases stay reserved for the **external** backend (H14).

**Problem.** Both audio encoding and ASR transcription compete with each other and with the Pages deploy
inside the single enrich job, serializing through the `pages` concurrency group. An enrich kill marks
the deploy job red even though the site is already live. Neither process has a dedicated workflow, and
their combined resource draw was the primary cause of recent OOM/preemption failures. The `asr-bench`
CLI exists ([`bench.py`](../citypods/bench.py)) but there is no benchmark **workflow** and no **separate
audio or ASR workflow**.

**Sequencing (2026-06-10):** the two steps are split into **H6a** (Step 1, benchmark workflow) and
**H6b** (Step 2, sharded workflow). **H6a has no H5 dependency and is do-now** — it settles the model
choice and the *measured* cost of `word_timestamps` / segment-vs-word output (H12) **before** any backfill
re-transcribes the catalog, so the catalog is only re-done once. H6b still waits on H5's manifest for safe
cross-workflow state coordination.

**Status.** H6a shipped in [PR #256](https://github.com/BashfulBits/city-meeting-podcasts/pull/256):
the manual benchmark workflow accepts maintainer-selected `city:uid` cases and runs max/med/min
model + beam-size + CPU-thread profiles under a capped runner budget, with the report written to the
Actions summary and uploaded as an artifact. H6b remains pending after H5's manifest/lease.

**Design (two steps, in order — measurement before architecture).**

**Step 1 — manual benchmark workflow (H6a) — Implemented in PR #256.** Add
`.github/workflows/asr-bench.yml` (`workflow_dispatch`, low concurrency, 1 matrix shard) that runs
`citypods asr-bench` over a fixed set of episodes with known
official transcripts and a **fixed wall-clock budget**, recording **transcript-minutes/runner-hour**,
WER/alignment-failure rate, timeout/error rate, and model-load overhead. This lets model/beam/thread
changes (`asr_model`, `asr_beam_size`, `asr_compute_type`, threads) be compared safely before any
architecture change. (See also `spike/asr-model-benchmark`: compare `large-v3-turbo` vs `small.en` vs
`base.en`.)

**Step 2 — separate audio and ASR workflows; render-only deploy (H6b/H11b).** Once H5's manifest
provides safe state coordination, split both heavy work classes out of `deploy.yml` into dedicated
workflows with their own concurrency groups, so the Pages deploy job is never blocked or marked red by
encoding or transcription:

- **`.github/workflows/audio.yml`** (or combined `enrich.yml`): audio materialization (download, ffmpeg
  encode, upload to object storage); scheduled frequently (e.g. every 4 hours, aligned with the deploy
  cron); own concurrency group `audio` distinct from `pages`; `workflow_dispatch`.
- **`.github/workflows/asr.yml`**: ASR transcription; scheduled daily + `workflow_dispatch`; own
  concurrency group `asr` distinct from `pages`; running a **matrix of source-sharded jobs**.

After the split, `deploy.yml` runs only: checkout → install → restore state → render → validate →
upload → deploy (no ffmpeg, no ASR model, no heavy encodes). Deploy finishes in minutes and is never
blocked by or marked red by an audio/ASR failure.

Coordination options for state safety (choose by safety): (1) **source-sharded concurrency**
`audio-${shard}` / `asr-${shard}` with each shard owning disjoint `source_key`s (preferred — no two
writers touch one record file); (2) per-source state files with merge-on-push; (3) a lease file in
object storage (the manifest's `lease_owner`). Render publishes **only completed** audio and transcript
artifacts and ignores in-progress work. Each job stays **below the 6-hour cap** (a daily/4-hour workflow
does **not** grant extra single-job capacity).

> **Review before implementing:** this scope expansion (audio workflow alongside ASR workflow, render-only
> deploy) was drafted 2026-06-10 and has not been validated against the current post-H5 state of
> `deploy.yml`, the H5 lease/manifest implementation, or the `native_audio_max_active` tuning that landed
> in H11a. Verify that the proposed `audio.yml` schedule and the H5 lease sidecar interact correctly
> before cutting implementation issues.

**Resolving the caveat — the record-write race (validated 2026-06-12).** Reading the post-H5 code closes
the open question above and surfaces one hazard the "render-only deploy" framing alone does not cover.
Today only one job writes `state/sources/*.json`, so the whole-directory `push_state`
([`statesync.py`](../citypods/statesync.py)) is safe. Three concurrent workflows (`deploy` render +
`audio` + `asr`) are **not**, for two reasons that must both be fixed:

1. **Render still persists records.** `render_stages()` is links-only and produces no new audio/transcript
   data, yet `build()` still calls `save_records` + `push_state` + `reconcile_state` unconditionally
   ([`run.py`](../citypods/run.py)). If render pulls at T1, an `asr` shard writes a transcript at T2, and
   render pushes its stale view at T3, render **silently erases the transcript** (transcript fields live
   inside the episode record). So "render-only deploy" must also mean **render writes only `docs/`** —
   gate `save_records`/`push_state`/`reconcile_state` off the render phase (H11b). The `audio`/`asr`
   workflows become the sole record writers.
2. **Sharded jobs over-push.** Each shard `pull_state`s the whole prefix (it needs all records for render
   context) but must `push_state(..., only_prefixes=owned)` — push back **only** the `source_key`s it
   owns — or it re-uploads its stale copy of a sibling shard's source. `reconcile_state` must likewise run
   **only on a full, unsharded run**. With these two fixes, option (1) source-sharding is genuinely safe;
   competitive leases stay reserved for the **external** backend (H14), where a worker holds an item
   across runs.

**The third hazard — cross-LANE clobber on a shared source file (validated + fixed 2026-06-16).** The
analysis above is necessary but **not sufficient**: it assumed "source-sharding ⇒ no two writers touch one
record file." That holds only *within* one workflow. The `audio` and `asr` workflows shard over the **same**
deterministic `shard_assignment` partition but run on **different schedules** (audio every 4h, ASR daily),
so the *same* `source_key`'s `state/sources/<key>/episodes.json` is written by **both** — at temporally
overlapping read→write windows. Because each run pulls state once at start, holds it for its whole (multi-hour)
run, then pushes back the **whole** record file, an ASR run that started *before* an audio run wrote a new
hosted-audio URL re-uploads its start-of-run `audio` block on finish — silently erasing freshly hosted audio.
Observed 2026-06-16: `hosted_audio −16` (concentrated in Fort Worth) on a deploy whose only real change was an
ASR transcript update. The regression is **bidirectional** (a late `audio` run can equally erase a transcript).

*Fix (two parts, [#322]-era `fix/cross-lane-record-clobber`):*
- **Foreign-block-preserving push.** A lane owns only its derived-artifact block — `audio` for the audio
  lane, `transcript` for `transcribe`/`align` (`records.protected_blocks_for_lane`). On push a scoped run
  re-reads the **current** remote per owned source and preserves the blocks it does *not* own
  (`records.merge_preserving_foreign`), then writes the merge locally and uploads it
  (`statesync.push_records_merged` / `fetch_remote_records`, called from `run.py`'s scoped branch). Provider/
  render fields stay last-writer-wins (they converge to provider truth); only the expensive cross-lane
  artifacts are protected. **Fail-safe:** a present-but-unreadable remote *skips* that source's push rather
  than risk a stale whole-record overwrite — the owned artifact is re-pushed next run (content-addressed, so
  a cheap re-credit). **Recovery is cheap:** a clobbered `audio`/`transcript` URL re-credits via the
  `_present(key)` reuse check on the next lane run with **no** re-encode/re-transcription, *provided the
  content-addressed object still exists* — it does, because `gc_audio.py` is a manual script (no cron) and
  the foreign-block fix means the key never transiently drops out of `referenced_audio_keys` to begin with.
- **Lane-stage gating.** `stages.LANE_STAGES` (one source of truth, enforced in `run_stages` and the global
  queue's pass split) runs only a lane's own work-class stages, so the ASR lane never re-derives an `audio`
  block (matching `asr.yml`'s "ASR over episodes that already have hosted audio") and the audio lane never
  runs transcription. This makes "preserve the remote's foreign block" strictly safe — the running lane can
  no longer produce a *fresher* local copy of a block it doesn't own.

*Residual (follow-up, not this fix):* a tiny TOCTOU window remains between the re-read and the upload. The
durable elimination is **per-stage object files** — `sources/<key>/audio.json` + `transcript.json` (+
`speakers.json` when diarization lands), each written by exactly one lane so no shared file is ever
read-modify-written. File that as its own item; keep the merge fix surgical. See §H5 for the
diarization-forward block/lane registry the per-stage split and this merge already key off.

**Alignment re-enable criteria.** Reintroduce stable-ts only as an **align-only** workflow lane:
pre-load the stable-ts model, do not load/run the fresh transcription model in that job, and do not run
audio encodes concurrently in the same runner. If alignment fails quality checks, mark the item for a
future `transcript-asr` claim rather than falling back inline. This keeps peak memory lower, makes
throughput measurements comparable, and prevents the 2026-06-09 failure mode where stable-ts alignment
stacked with ffmpeg work and GitHub terminated the runner with exit 143.

**Files.** `.github/workflows/asr-bench.yml`, `.github/workflows/asr.yml`,
`.github/workflows/audio.yml` (new, H6b/H11b); `citypods/cli.py` (ensure
`enrich --stage transcript --lane {transcribe,align} --shard k/N` or `--source <key>` selection exists);
`citypods/asr.py` (global throttle already added — verify under matrix; add explicit preload entrypoints
for the selected lane only — transcription runs through the **H13 `local` adapter**);
`citypods/ops/workqueue.py` (lease/claim for shards); `citypods/run.py` (filter `cities` to the shard via
weighted source-atomic `records.shard_assignment`; gate record-persistence off the render phase);
`citypods/statesync.py`
(`push_state(..., only_prefixes=)` + scope-guard `reconcile_state` to full runs); `deploy.yml` (strip
to render-only — remove enrich step); `tests/test_statesync.py` (scoped push leaves unowned sources
untouched); `tests/test_run.py` (render persists no records; disjoint+exhaustive shard partition); docs
in ARCHITECTURE.md (workflow split) + this file.

**Acceptance:** H6a: the benchmark workflow emits a throughput/quality report artifact. H6b: the audio
workflow and ASR workflow clear their respective backlogs with **no record-file clobbering** (verified by
a concurrent two-shard dry run); `deploy.yml` is a render-only job that never stalls on or is marked red
by audio encoding or transcription.

---

## H7 — Contributor/agent handoff docs

**Shipped** in the doc-set PR (#226): `AGENTS.md` + `CLAUDE.md` pointer, `ARCHITECTURE.md`, the
`CONTRIBUTING.md` lifecycle / doc-update contract, and the PR + issue templates. This was the
documentation foundation the rest of Phase H builds on; no further code work. Kept in the H-sequence so
the numbering is continuous across `ROADMAP.md`, `review/11`, and this doc.

## H8 — Throughput maximization on the free 4-core runner — **Implemented in PR #235**

**Status.** Implemented in [PR #235](https://github.com/BashfulBits/city-meeting-podcasts/pull/235),
merged 2026-06-08. The base design is frozen; dated operational follow-ups are recorded below.

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
  threads from `audio_ffmpeg_threads` or `cpu_count // native_audio_max_active` (PR #257), and keeps heartbeat
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

**2026-06-15 follow-up — predicted-memory encode admission.** The first sharded Audio runs after the
#274 fixes (Audio #8/#9) hosted real audio but terminated **~46%** of the large filter-render (loudnorm)
encodes of multi-hour meetings (`ffmpeg filter-render stopped: mem_avail … below floor`). Log analysis
showed single encodes peaking **0.18–5.9 GiB** and the floor kills firing **220–1080 s into** the
encode — i.e. RSS grows across the whole job, so the H8 *instantaneous* `mem_avail` admission gate is a
**trailing** signal: it still looks healthy when a second big encode starts, and the two then collide.
Fix (PR on `feat/encode-memory-reservation`): a `MemoryReservation` accountant
(`citypods/resources.py`) admits each encode against `audio_memory_budget_mb` (~12 GiB of 15.6) by its
**predicted** peak RSS — `media.estimate_encode_rss_bytes`, keyed on the known-ahead served length from
the `TimelineStage` EDL / feed duration (conservative default when neither is known). The reservation
supersedes `resource_guard_min_available_mb` for audio (that gate now governs only ASR);
`native_audio_max_active` is the hard ceiling (4→3) and the 1.5 GiB floor stays the backstop. Cost-model
coefficients are a first heuristic, calibratable from the per-encode `peak_rss` already logged.

**2026-06-18 follow-up — remove the length-growing filter instead of merely scheduling around it.**
The next successful Audio runs still showed the underlying failure: one-pass dynamic `loudnorm` retains
length-proportional state on multi-hour meetings, with observed peaks around **9–13 GiB**. Reservation
admission prevented collisions but necessarily serialized the longest work and could not make one
encode fit comfortably. The production recipe is now the versioned `podcast-speech-v2` chain:

1. render the served timeline once through an 80 Hz high-pass, moderately smoothed `dynaudnorm`
   (`f=500:g=21:p=.80:m=6:r=.08:t=.015:o=.5`), and a gentle 2.5:1 compressor;
2. write that exact mono signal to a temporary lossless FLAC while streaming `ebur128` measurements;
3. read the local FLAC and apply measured **linear** loudnorm to -16 LUFS / -1.5 dBTP, then AAC.

Provider media is read only in pass 1. Every filter is streaming, so RSS is independent of meeting
duration and admission uses a fixed 768 MiB reservation; the temporary FLAC moves the unavoidable
whole-program state to disk. The final loudnorm target LRA is never set below the measured LRA, and
peak feasibility is checked before launch, preventing ffmpeg from silently falling back to dynamic
mode. Monotonic single-source timelines use one streaming `aselect` path rather than parallel `atrim`
branches. To avoid `aselect`'s whole-frame boundary drift, the graph fixes the stream at 48 kHz,
switches to one-sample frames only in short windows around each cut, selects by integer sample PTS,
then coalesces normal frames. Sub-second post-edit timelines are classified `dead` before ffmpeg.
Provider fetch, speech-measure, and final AAC/limiter passes have separate native admission; the final
pass uses a bounded executor with priority over new measure work while sharing the same total FFmpeg
ceiling. Backfill uses the existing content-addressed contract: `audio_processing_profile` participates
in `audio_spec_hash`, so no `AUDIO_PIPELINE_VERSION` bump is needed; changing v1 → v2 makes prior v1
objects stale and remasters them gradually under the normal wall-clock queue.

Local FFmpeg verification on 2026-06-18 processed a synthetic two-hour, four-level recording in
152.5 seconds (**47× realtime**) with exact 7,200-second output and **-16.0 LUFS** integrated
loudness. A shorter realistic multi-level fixture moved from **12.3 LU LRA → 1.8 LU LRA**, confirming
that quiet microphone sections are brought close to louder ones while the final pass remains a global
linear gain. The sample-accurate timeline follow-up processed the same two-hour source with 120 keep
spans in 123.6 seconds, emitted exactly **6,000.000 seconds**, and measured **-16.0 LUFS**.

**2026-06-18 peak-headroom follow-up.** Fort Worth recordings exposed the remaining mathematical edge
case: a low integrated level plus isolated high transients can require +20 dB or more of constant gain,
predicting +7–8 dBTP. FFmpeg documents that linear `loudnorm` reverts to dynamic mode when that gain
would exceed the TP target; the explicit guard prevented the memory regression but failed the episode.
The normal linear path remains unchanged. Peak-constrained items now use a pass-2 fallback of:

`constant volume gain → 192 kHz resample → alimiter (-2.5 dB, auto-level off, latency compensated)
→ 48 kHz → AAC`.

The limiter retains only millisecond lookahead and resampler state, so memory remains constant with
duration. Its -2.5 dB ceiling leaves reconstruction headroom for AAC (a pathological transient fixture
measured -2.1 dBTP after encoding). Because exact -16 LUFS and -1.5 dBTP are not simultaneously
attainable for every crest factor without changing dynamics, safety wins: heavily limited material may
finish slightly below -16 LUFS rather than clip, invoke dynamic loudnorm, or be dropped. Old
`loudness` failures bypass backoff once after deployment; subsequent real meter failures use the
`loudness_measurement` code. The fallback itself affects only previously failed items; the accompanying
v2 timeline/phase recipe change triggers the gradual content-addressed remaster described above.

---

## H9 — Combined-throughput evaluation across execution homes

> **Rescoped 2026-06-12.** H9 was "decision matrix, no commitment." Now that **H14 builds the Modal +
> Beam adapters**, H9 is the **measurement** of the three execution homes that actually exist behind the
> H13 interface — it answers "how far do the free tiers get us, and when is paid/self-hosted worth it?"
> with numbers, not a paper survey.

**Problem.** After H6b shards the on-runner `local` backend and H14 adds two free-tier GPU backends, what
is the **combined** sustainable throughput at $0, and where is the ceiling?

**Design — measure the built homes against the interface (H13).** Extend the H6a harness across each
backend adapter and the catalog's observed duration distribution. Report throughput and reliability,
but also local peak memory/success by duration and model, CPU/GPU real-time factor, cold start,
download/decode, chunk overlap/stitching, retry granularity, artifact transfer/storage, free-tier
consumption, queue/routing outcomes, transcript boundary quality, speaker consistency, and
**$/transcript-hour** plus incremental **$/diarized speaker-hour**. Compare independent workers with
one combined external ASR+diarization flow; do not infer shared neural-model computation from shared
I/O/preparation.

| Execution home | Adapter | Notes |
|---|---|---|
| **GitHub Actions matrix sharding** (H6b) | `local`, sharded | Free, ≤20 concurrent jobs, native to the repo. ~1,500/wk at 4 shards, ~3,000 at 8. The always-available floor. |
| **Modal free tier** (H14) | `modal` | Serverless GPU; budget-bounded to the monthly free credits (H14 ledger). Measure GPU-seconds/transcript-hour → confirm the free allotment's weekly transcript ceiling. |
| **Beam free tier** (H14) | `beam` | Same serverless-GPU model; independent free allotment, so it stacks with Modal. Measure separately, then summed. |
| *(documented fallbacks, not built)* | Groq/Deepgram credits; Oracle/Colab/Kaggle; self-hosted Mac-mini; AWS | Kept as the post-1.0 menu — each is "just another adapter" once H13 lands. |

**Output.** A written recommendation in this doc + ARCHITECTURE.md, with the measured numbers: the
**combined free-tier weekly transcript ceiling** (`local`-sharded + Modal + Beam), whether it clears the
initial backlog at the current ~80-feed catalog and at 1,000+, the safe local duration default, when GPU
routing should be preferred rather than merely available, whether durable chunk intermediates are worth
their complexity, whether combined ASR+diarization materially reduces cost, and the first paid/self-hosted
step if free tiers do not clear inflow.

**Diarization workload (Phase R #7).** Speaker diarization is the GPU-hungriest workload (pyannote v3 on
CPU is ~3–5× slower than transcription) and rides the **same** H13 interface + H14 backends (a
`task=diarize` job). Include a diarization benchmark: pyannote v3 on Modal/Beam GPU vs the
wespeaker/speechbrain ECAPA-TDNN CPU path on the runner, reporting **$/speaker-hour** alongside the
transcription baseline — this is what tells Phase R whether diarization runs on the GPU backends or the
CPU lane.

**Acceptance:** a written decision with measured per-backend + combined numbers; the recommended mix
wired (sharding via H6b, Modal/Beam via H14); local-duration/routing and chunk-persistence recommendations
recorded; diarization $/speaker-hour and boundary/speaker-consistency results recorded for Phase R.

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

## H11 — Deploy resilience — H11a **Implemented** (PRs #239/241/242/243/244/246/247); H11b **Implemented** ([#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272))

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

**H11b status — Implemented in [#272](https://github.com/BashfulBits/city-meeting-podcasts/issues/272).**
`deploy.yml` is now render-only (checkout → install → restore state → render → validate → upload →
deploy; no ffmpeg, no Whisper model, `actions: read` dropped) and the heavy phase moved to a new
combined **`.github/workflows/enrich.yml`** (own `enrich` concurrency group, the graceful-yield
`actions: read` + `GITHUB_TOKEN` wiring, `cron: "30 */4 * * *"` + `workflow_dispatch`). The
record-write race is closed at its root: `build()` gates `save_records`/`push_state`/`reconcile_state`
off `--phase render` (render writes only `docs/`), so `enrich.yml` is the sole record writer, and
`statesync.push_state(only_prefixes=)` + `reconcile_state(full_run=)` add the per-shard scope hooks
H6b needs. **H6b** still splits the combined `enrich.yml` into source-sharded `audio.yml` + `asr.yml`
and wires those hooks. The "Durable follow-up" and "record-write race" paragraphs below are the
frozen design this implemented.

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
- **Durable follow-up (H11b): isolate all heavy work from the deploy job.** Move both **audio
  materialization** and **ASR/transcription** out of `deploy.yml` into dedicated workflows (this is H6b
  Step 2, expanded to cover audio as well as ASR). `deploy.yml` becomes a lightweight render-only job
  (render + validate + upload + deploy) that finishes in minutes and can never be marked red by encoding
  or transcription failures. Depends on **H5**'s manifest/lease for safe cross-workflow state
  coordination. Until then, the deploy job and enrich share a runner and a red job is possible.
- **Observability (small, alongside H11a):** because a starved runner can die before emitting a clean
  signal, ensure the heartbeat's last line is easy to locate post-mortem and consider a step-summary
  note recording the last heartbeat snapshot, so a genuine provider failure is distinguishable from a
  resource kill (complements H4 / H-G).

**Files.** H11a follow-up: `citypods/resources.py` (`NativeWorkGate` + global native audio cap),
`citypods/media.py` (`audio` shared gate, ffmpeg filter-thread caps, child RSS/min-available logging),
`citypods/stages.py` / `citypods/run.py` (`asr` exclusive gate + `native_audio_max_active` config),
`config/site_config.yml` (one-core production audio lane), `tests/test_resources.py`,
`tests/test_media.py`, `tests/test_encoder.py`. H11b: `.github/workflows/asr.yml`,
`.github/workflows/audio.yml` + `deploy.yml` (strip to render-only, remove enrich step),
`citypods/ops/workqueue.py` (H5 lease), ARCHITECTURE.md (workflow split) — sequenced after H5/H6b.

**Acceptance:** (H11a) several consecutive scheduled Build & Deploy runs complete **green** with enrich
using most of its window and no exit-143/lost-comms kills under the one-slot audio lane. (H11a tuning,
optional) a measured 3-core ASR / 1-core audio-lane experiment improves completed transcript/audio work
per runner-hour while preserving the green-run streak and a safe memory floor. (H11b, later) `deploy.yml`
is a render-only job that never stalls on or is marked red by audio encoding or transcription; both the
audio workflow and ASR workflow clear backlog without clobbering records (shared acceptance with H6b).

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

## H13 — GPU/ASR execution-backend interface (+ `local` adapter) — **the pre-1.0 lock**

**Maturity: L3.** The GPU/process half of the widened execution-backend interface (review/11 §5.5, which
covers **two** backend families: GPU/process backends for ASR/diarize/TTS, and LLM-API backends for the
R3/R4 text tasks). H13 builds the **interface + the `local` GPU/ASR adapter**; the first LLM-API adapter
lands with R3/R4, and the Modal/Beam GPU adapters are **H14**. VISION names this interface a **pre-1.0
lock**.

**Problem.** Heavy inference is the project's main compute cost and the first thing to outgrow the free
runner. Today `TranscriptStage` calls `citypods/asr.py` directly, so swapping in Modal/Beam (H14) would
mean editing the stage. We want backend swaps to be adapter-only.

**Design (mirror `storage/`).** Define one protocol — same `runtime_checkable` Protocol pattern as
`StorageBackend`:
- `citypods/compute/base.py` — `InferenceJob(task, inputs, recipe_hash)` where `task` is **typed for the
  full §5.5 verb set** — GPU/ASR verbs `transcribe`/`align`/`diarize` **and** the reserved LLM verbs
  `summarize`/`tag`/`soundbite-select` (so R3/R4's adapter slots in with no interface change) — plus
  `JobResult`/`JobHandle` and a `Backend` protocol `run_inference(job) -> JobResult | JobHandle`. A
  synchronous backend returns the artifact; a **dispatch** backend (H14) returns a handle.
- `citypods/compute/local.py` — wraps **today's** in-process faster-whisper / stable-ts path (a pure move
  of the calls in `asr.py`; **byte-identical output**). The only adapter that must exist at 1.0 (alongside
  one LLM-API adapter from R3/R4).
- `citypods/stages.py` `TranscriptStage` — call `backend.run_inference(...)`; the H6b `--lane` maps onto
  `task` (`transcribe`/`align`).
- `citypods/config.py` / `config/site_config.yml` — `compute_backend: local` (default).

**Why first.** Low-risk behavior-preserving refactor; it is the seam H6b's lane split and H14's adapters
both depend on. Diarization-forward by construction (the `diarize` verb is already typed) — Phase R #7
adds an adapter call, not a new interface.

**Files.** `citypods/compute/__init__.py`, `citypods/compute/base.py`, `citypods/compute/local.py`;
`citypods/stages.py`; `citypods/config.py`, `config/site_config.yml`; `tests/test_compute_local.py` (the
`local` adapter yields byte-identical VTT + words.json to the pre-refactor path); `ARCHITECTURE.md` (new
`compute/` module, peer of `storage/`).

**Acceptance.** `TranscriptStage` routes through `compute.local`; `ASR_PIPELINE_VERSION` unchanged;
`tests/test_transcript_stage.py` passes untouched; `base.py` types the full §5.5 verb set; the protocol is
documented as the pre-1.0-locked shape.

---

## H14 — External transcription adapters: Modal + Beam (free-tier-bounded async dispatch)

> **H14a substrate — Implemented in [#275](https://github.com/BashfulBits/city-meeting-podcasts/issues/275).**
> The dispatch half of the H13 interface (`base.DispatchBackend` + `JobHandle`), the free-tier budget
> ledger (`compute/budget.py` → `state/compute_budget.json`), the router + thread-safe
> `DispatchCoordinator` + `reconcile_compute` (`compute/dispatch.py`), the live `work.json` lease
> (`lease_owner="modal:<job_id>"`), the `compute_backend: auto` routing wired into `TranscriptStage`
> (overflow-to-`local`), the `citypods compute reconcile` CLI + `asr.yml` reconcile job, and a
> `FakeDispatchBackend` covering it all (`tests/test_compute_dispatch.py`) are **shipped**. Still open:
> **H14b/H14c** — the real `compute/{modal,beam}_backend.py` adapters + `scripts/compute/{modal,beam}_app.py`
> remote workers (the design below), which register into the coordinator with no stage change.

**Maturity: L3.** The first real non-`local` GPU backends — pulled into Phase H on 2026-06-12 so the
"compute is pluggable" lock is proven by **two live adapters** before 1.0 (overriding the earlier
"GPU backends post-1.0" framing for these two specifically). Builds on **H13** (interface), **H5 PR3**
(the transcript pass is already *dispatch-not-await ready* and the `work.json` lease fields were reserved
for exactly this), and **H6b**'s `asr.yml` workflow (which becomes the dispatcher).

**Problem.** Even sharded (H6b), the on-runner `local` backend caps at the free tier's ≤20 concurrent
jobs / 6-h cap, and the GPU-hungry diarization workload (Phase R) won't fit it at all. Free
serverless-GPU tiers (**Modal**, **Beam**) add real capacity at **$0 — but only while usage stays inside
each provider's monthly free allotment**, which the design must *enforce*.

**Design — async dispatch behind H13, bounded by a budget ledger.**
- **Two adapters**, `citypods/compute/modal_backend.py` and `citypods/compute/beam_backend.py`, implement
  the H13 `Backend` protocol in **dispatch mode**: `run_inference(job)` submits the job (audio bucket/
  enclosure URL + `recipe_hash` + `task`) and returns a `JobHandle` **without awaiting**. The remote
  worker writes the **content-addressed** artifact (`transcripts/<src>/<uid>-<recipe>.vtt` + `.words.json`)
  back to the **same object bucket**, then marks the manifest item `done`. The **next** deploy's `render`
  reconciles it onto the feed — exactly how a not-yet-hosted episode is handled today.
- **`asr.yml` is the dispatcher.** With H14, the `asr.yml` workflow's transcribe lane offers work to
  Modal/Beam before considering faster-whisper on-runner. A backend that is out of budget, at capacity,
  missing the required task/model capability, or unable to accept the recording **declines dispatch**;
  that is routing state, not an ASR failure. The `local` adapter is considered only after external
  targets decline and only when both local admission guards pass. The `audio.yml` workflow is
  unaffected — audio stays on-runner.
- **Remote worker entrypoints** (`scripts/compute/modal_app.py`, `scripts/compute/beam_app.py`): a thin
  function running faster-whisper (transcribe) / stable-ts (align) / later pyannote (diarize), reading
  audio from the public URL and writing artifacts to the bucket via the existing storage backend (creds as
  Modal/Beam **secrets**). The untrusted-output rule applies; no PII; verify each provider's free-tier ToS
  permits this batch use (record the check here).
- **Free-tier budget ledger (the $0 guarantee).** New `citypods/compute/budget.py` + a persisted
  `state/compute_budget.json` (rides statesync). Config:
  ```yaml
  compute_backend: local            # default; "auto" enables the dispatcher below
  compute_backends:
    modal: { monthly_gpu_seconds: 108000, max_inflight: 8 }   # ≈ free credits — pin to the live plan
    beam:  { monthly_gpu_seconds: 108000, max_inflight: 8 }
  ```
  The dispatcher checks remaining monthly budget + open in-flight slots, **decrements on dispatch**, and
  **reconciles actuals** on done. When an allotment is spent, that backend is skipped until the month
  resets. Exceeding the free tier is structurally impossible.
- **Routing = configured external policy, then admit an eligible `local` fallback.** For each queued
  item in H5 backlog order, try configured external targets according to budget/capacity policy. The
  policy input set must be able to include monthly/free-tier budget, in-flight capacity, backend
  capability, recording duration, task type (especially `diarize`), estimated GPU runtime/cost,
  backlog priority, and artifact urgency. If an external target accepts, the local duration ceiling is
  irrelevant. If all decline, `local` is eligible only when the duration/memory ceiling **and** the
  rolling runtime/remaining-deadline estimator both admit the job. Otherwise retain it as queued with
  a reason such as `external-required`. Capacity/budget/capability decline must not enter transcript
  failure backoff. The in-flight claim is a **live `work.json` lease**
  (`lease_owner = "modal:<job_id>"`) — **this is where H5's reserved leases activate.**
- **Local faster-whisper duration admission — implemented, unreleased.** The rolling
  `state/asr_runtime_log.json` buffer (100 successful runtime/recording-duration ratios) protects the
  Actions time window; it is not a peak-memory model. Consecutive exit-143 failures on approximately
  7.2h Travis County and 6.7h Dallas recordings showed that a recording can fit the estimated
  285m/350m window yet spike local faster-whisper memory within about 40 seconds. New
  `asr_local_max_duration_hours` (production `4`; non-positive disables) applies only to synchronous
  local inference. External dispatch is attempted first regardless of duration. If dispatch is
  unavailable, out of budget/in-flight capacity, or otherwise declines, a known duration above the
  ceiling is queued with `reason=external-required`, not failed and not entered into ASR backoff.
  Known metadata duration is checked before the ASR semaphore/audio download; an initially unknown
  duration is checked again after the hosted-audio probe and before inference. Genuinely unknown
  durations preserve the prior behavior. Forks remain fully functional within their configured
  hardware envelope; larger self-hosted machines may raise or disable the ceiling.
- **Reconcile dead workers.** An `asr.yml`-start `compute reconcile` reaps **expired** leases (worker died
  → re-queue); a `done` item whose artifact is already present is a no-op (content-addressing makes
  re-dispatch idempotent).
- **Diarization-ready.** The same adapters carry `task=diarize` for Phase R #7 — the workload that most
  needs the GPU (H9 measures $/speaker-hour).

**Long-audio and combined external-worker requirements for H14b/H14c.**

1. Modal/Beam workers must process recordings longer than the local ceiling; the local guard is not a
   global episode limit. Long-audio processing must use bounded memory, with chunking when required by
   the model/runtime or when measurements show better throughput and retryability.
2. When ASR and diarization are both requested, prefer one external worker flow so they share one
   download, decode/resample, temporary normalized/PCM audio, chunk/VAD plan, container/model-start
   lifecycle, intermediate timestamps, and final word-to-speaker assembly. This is shared I/O,
   preparation, startup, planning, transfer avoidance, and artifact coordination—not reuse of one
   neural model or its activations; ASR and diarization generally use different models. The transcript
   remains independently publishable: a diarization failure cannot discard or invalidate successful
   transcription unless a later explicit product requirement changes that contract.
3. Use a backend/model-configurable bounded-window planner. An initial benchmark range of roughly
   **20–30 minutes** is a starting parameter, not a fixed product contract. Prefer VAD/silence-aware
   cuts near nominal boundaries and include overlap so speech crossing a cut is complete in at least
   one window. No design may promise that arbitrary raw-audio cuts never intersect a word or active
   speaker; quality comes from overlap, speech-aware boundaries, and deterministic stitching.
4. Convert chunk-local word/segment times to the complete meeting timeline. Deduplicate overlapping
   hypotheses deterministically using timestamps plus normalized text. Before publishing, validate
   temporal ordering, expected coverage, duplicate density, and excessive gaps. Publish canonical VTT
   and word JSON only after every required chunk is assembled successfully. Chunk boundaries are
   private execution detail and must not appear in public artifact semantics.
5. The transcript recipe hash must cover the chunk planner, target-window/overlap settings, VAD,
   stitcher, model, and decoding versions in addition to the existing audio identity. This preserves
   content-addressed/idempotent behavior when any long-audio semantic changes.
6. Diarization must reconcile speaker identity across the whole meeting rather than concatenate
   independently numbered chunk labels (`SPEAKER_00` in two chunks is not proof of identity). Use
   overlapping diarization windows, speaker embeddings, and meeting-wide clustering/identity
   reconciliation, then assign reconciled speaker turns to transcript words/segments. Diarization
   enriches the canonical timing artifact; it does not require ASR text regeneration.
7. Preserve the locked episode-level compute interface and its `transcribe`/`align`/`diarize` verbs.
   A worker may co-lease transcript and diarization work and internally combine preparation/chunk
   execution without adding a public task verb or breaking `InferenceJob`. Chunking remains below the
   episode-level job boundary.
8. Centralize audio preparation, chunk planning, timestamp rebasing, stitching, and validation so
   Modal, Beam, future self-hosted GPU workers, and eventually `LocalBackend` can share transcript
   semantics. Backend adapters should own transport/lifecycle, not artifact meaning.
9. Immutable per-chunk intermediate artifacts are optional follow-up work. Do not require them for the
   first Modal/Beam implementation if an episode-level worker is simpler; add durable chunk scheduling
   only when H9 retry/parallelism measurements justify its storage, lease, and reconciliation cost.

**Recommended implementation sequence (no code implied by this design update).**

1. Add the configurable local-duration admission guard.
2. Implement the first real H14 Modal and Beam adapters/workers.
3. Require both workers to handle long audio safely.
4. Centralize backend-independent audio preparation, chunk planning, stitching, and validation.
5. Support combined ASR+diarization preparation when Phase R diarization lands.
6. Add capability-, deadline-, and cost-aware routing based on H9 measurements.
7. Add durable chunk-level intermediate artifacts only if retry/parallelism measurements justify them.
8. Later consider reusing the bounded-memory chunking engine in `LocalBackend`, improving the fallback
   available to forks and self-hosted installations.

**Files.** `citypods/compute/{modal_backend,beam_backend,budget}.py`;
`citypods/compute/long_audio.py` (backend-independent normalize/plan/stitch/validate contract);
`scripts/compute/{modal_app,beam_app}.py`;
`citypods/compute/base.py` (the dispatch/`JobHandle` half); `citypods/stages.py` (dispatcher when
`compute_backend != local`); `citypods/ops/workqueue.py` (leases go live + reconcile-expired);
`config/site_config.yml` (`compute_backends`); `.github/workflows/asr.yml` (dispatch + `compute reconcile`
step; Modal/Beam tokens as secrets); `tests/test_compute_dispatch.py` (dispatch records a lease +
decrements budget; budget/capacity decline tries the next target and then only an eligible `local`;
no eligible backend leaves queued work without failure backoff; reconcile reaps an expired lease +
re-queues; the result-write contract round-trips an artifact onto a mock bucket);
`tests/test_compute_long_audio.py` (bounded windows, absolute timestamps, overlap dedupe, validation,
recipe invalidation, and meeting-wide speaker-reconciliation fixtures); `ARCHITECTURE.md` + `SECURITY.md`
(external-worker trust boundary + secrets surface).

**Acceptance.** With Modal + Beam configured, the `asr.yml` transcribe lane dispatches up to each
free-tier budget then falls back to `local` only when locally eligible; artifacts written by a
(mocked) remote worker are reconciled onto the feed by the next render; `state/compute_budget.json` is
never exceeded in a month; no deploy is ever blocked; a killed remote worker's lease expires and the
item re-queues. Both real workers pass a long-recording canary above the production local ceiling
without unbounded memory; canonical transcript artifacts are published only after successful
meeting-relative assembly/validation; dispatch unavailability remains queued and does not create an
ASR failure/backoff record.

---

## H15 — Transcript-quality metric (periodic caption-trust scoring)

> **New 2026-06-16.** Spun out of the PR #324 review discussion. H6b shipped the `align` lane
> "implemented but unscheduled" because we **assume** served (caption-derived) transcripts are faithful
> enough to align against rather than transcribe fresh — but we never measured that assumption. H15
> turns it into a **periodic, per-source, computed metric** that decides, per source, whether `align` is
> safe or fresh `transcribe` is required.

**Problem.** Forced alignment (`align` lane) is far cheaper than fresh transcription and preserves
official wording, but it is only correct if the served captions match what was actually spoken. Some
served captions are human CART / court-reporter output (likely *better* than our Whisper pass); others
are a platform's cheap auto-ASR (likely worse). We have **no signal** telling the two apart, so the
align lane stays globally off and every caption-bearing feed pays for fresh transcription. We also have
no standing measure of transcription quality itself.

**Why a metric, not a one-time eval.** A one-time WER study goes stale the moment a city changes its
captioning vendor or we bump the Whisper model. The same alignment/scoring calls we already make can
emit a quality number **every run, for free** — so the right artifact is a rolling per-source metric,
not a paper.

**The correctness trap (design constraint).** WER and CER are **asymmetric, reference-required** metrics
— word/character-level edit distance over a reference *assumed correct*. Computing WER *between* served
text and an ASR hypothesis measures **disagreement, not correctness**: neither is ground truth, and
provenance means the caption may well be the better one. So the design keeps three things separate:
(1) a **cheap reference-free fit signal** computed every run that grounds each transcript in the audio;
(2) a **fair adjudicator** independent of both generators, used periodically; (3) a **small human-gold
sample** that anchors the absolute scale. (Normalize text — lowercase, strip punctuation, expand
numbers, à la Whisper's `EnglishTextNormalizer` — before any WER/CER, or you measure formatting, not
errors.)

**The scaffold (three layers, increasing cost/fidelity).**

*Layer 1 — free, in-tree, every run (single-transcript audio-fit).*
- `citypods/asr.py::align()` already runs `stable_whisper.align(audio, text)` and already computes
  **coverage** = `timed_words / total_words` (the `_MIN_ALIGN_COVERAGE = 0.60` gate). Today we only
  *branch* on it; H15 **records** it.
- Add the finer signal already present on the returned object: aggregate the per-word `.probability`
  stable-ts populates on `result.segments[*].words` (mean and p10 word-logprob). Coverage answers "did
  the words land in the audio at all?"; word-logprob answers "how confidently?".
- Caveat baked into the metric: **stable-ts is Whisper**, so its `P(text|audio)` is a decoder posterior
  that gives Whisper-shaped text home-field advantage. Layer 1 is therefore a **triage signal for one
  transcript's audio-fit**, never a fair served-vs-ASR comparison. When a source has both a served and a
  fresh ASR transcript on hand (e.g. after an `align`→`transcribe` fallback), record both Layer-1 scores
  but label them `same-generator-biased`.

*Layer 2 — periodic, independent adjudicator (fair served-vs-ASR).*
- For a rotating sample of sources, run a CTC forced aligner that is **not Whisper** —
  `torchaudio.functional.forced_align` (wav2vec2 emissions) or WhisperX's wav2vec2 alignment — against
  **both** the served captions and a fresh Whisper transcript, and compare their acoustic-fit. Because
  the judge is independent of both generators, this is the fair "which transcript does the audio
  actually support?" measurement.
- Output per sampled source: `served_fit`, `asr_fit`, and the sign/margin → a **caption-trust verdict**
  (`high | low | unknown`).
- Cost-bounded: rides the H13 execution-backend interface as a scoring task; runs over a rotating subset
  (e.g. N sources/run, oldest-checked first), not every meeting.

*Layer 3 — human-gold anchor (occasional, calibration only).*
- A tiny stratified human-gold set (≈20–50 segments across vendors/audio conditions), transcribed or
  corrected once, used to attach an **absolute** WER/CER to the Layer-2 fit scores so the trust
  thresholds mean something. Refreshed only when the model or a vendor changes. The only layer needing
  human effort — and only a sample.

**Data model.** A new state-backed rolling log mirroring `state/asr_runtime_log.json` (the merge-pushed,
capped-deque pattern from PR #324):
- `state/transcript_quality_log.json` — append-only, capped, **per `source_key`**:
  `{source_key, sampled_at, layer, served_coverage, served_word_logprob_mean,
  asr_word_logprob_mean (L2), verdict, model_id, caption_provenance}`.
- Merge-push via a new `push_transcript_quality_log_merged` (same union-by-id + keep-newest as
  `push_asr_runtime_log_merged`) so the 4 ASR shards don't clobber each other.
- A derived per-source **`caption_trust`** the lane router reads.

**Wiring.**
- `TranscriptStage` (L1): record coverage + word-logprob whenever `align()` runs — near-zero cost.
- A periodic `asr-quality.yml` job (or a slice of `asr-bench.yml`) drives L2 over the rotating sample via
  the H13 backend, scoring with the independent aligner.
- **Routing payoff (the unblock):** the lane router consults `caption_trust` per source — `high` ⇒
  schedule the cheap `align` lane; `low`/`unknown` ⇒ stay on fresh `transcribe`. This is the concrete
  resolution of H6b's "align lane implemented but unscheduled."
- **Admin surface:** a transcript-quality panel on `/admin/status` (per-source trust distribution,
  sources flagged `low`, last-sampled age) — the glanceable view a one-time eval can't give.

**Provenance first.** Before trusting any score, capture **where the captions come from** (human CART vs
platform auto-ASR) from the source/provider config where known — that's the prior the fit scores then
confirm or override.

**Open decisions (why this is L2, not yet L3).**
- Independent aligner: `torchaudio.functional.forced_align` (lean; may ride torch we already pull) vs
  WhisperX (heavier, batteries-included).
- L2 sampling cadence + sample size (cost vs freshness).
- Trust thresholds — set only after the L3 human-gold anchor exists.
- Whether L2 reuses `asr-bench.yml` or gets its own scheduled workflow.

**Relationship to neighbors.** Distinct from **H6a** (runtime benchmark) and **H9** (throughput, $/hr
across execution homes) — those measure *speed/cost*; H15 measures *correctness*. It reuses H9's "fixed
mix + report WER / alignment-failure" machinery and the **H13** backend interface, and the per-source
`caption_trust` is diarization-adjacent metadata (Phase R #7).

**Acceptance.**
- L1 recorded every run: `state/transcript_quality_log.json` accrues per-source coverage + word-logprob,
  merge-pushed without cross-shard clobber (test mirrors `test_push_asr_runtime_log_merged_*`).
- L2 produces a per-source `caption_trust` verdict from an aligner independent of both generators, over a
  rotating sample, within a bounded GPU/runtime budget.
- The lane router reads `caption_trust` to gate the `align` lane per source (replacing the global
  "unscheduled").
- `/admin/status` shows the trust distribution + stale-sample ages.
- A documented human-gold sample anchors the thresholds to an absolute WER/CER.

---

## #39 — Per-provider rate limiting (sequence with H6b)

**Maturity: Implemented** ([#274](https://github.com/BashfulBits/city-meeting-podcasts/issues/274),
2026-06-14). Sharded `audio.yml`/`asr.yml` (H6b) multiply concurrent hits on shared provider tenants —
the three Granicus `403` fixes in two days (PRs #245/#250/#251) were the warning shot, and the **first**
sharded Audio run then confirmed it at scale (vs. the last pre-sharding Enrich run, source fetches
collapsed from a 5–135 s spread to **all ~5 s** with **zero** encodes). The **Retry-After clamp was
already shipped** (`_ClampedRetry.MAX_RETRY_AFTER_SECONDS = 120`); #39 generalizes the bespoke Granicus
403 backoff into a shared **per-host concurrency cap** so every shard stays polite.

**Design correction made during implementation.** The original design placed the cap in
`GuardedHTTPAdapter.send` only. But the `403`/truncation traffic is **ffmpeg subprocesses**, not the
`requests` session — a cap only in the adapter would not touch the offending traffic. The shipped cap is
a shared primitive (`HostRateLimiter`) acquired by **both** `GuardedHTTPAdapter.send` *and* the ffmpeg
fetch paths (`media.py`: `_download_audio`, `_render_identity`, `_render_filter` via a `rate_limit_urls`
arg on `_run_ffmpeg_guarded`).

**As built.**
- `HostRateLimiter` (`citypods/http.py`): process-global, per-**registrable-domain** `BoundedSemaphore`,
  configured by `provider_rate_limits` (keyed by registrable domain — `granicus.com: 1`, `swagit.com: 2`,
  `civicclerk.com: 4` — not provider short-names, so the Granicus-owned Swagit CDN `*.granicus.com` is
  matched by the host the tenant sees). `slots(urls)` dedupes by domain + acquires in sorted order (no
  self-deadlock on multi-source renders). Configured once in `run.build()` before any fetching.
- `DistributedProviderLeasePool` (`citypods/provider_leases.py`): B2-compatible soft provider leases
  for shard processes that cannot share the in-process semaphore. The protocol writes a unique
  candidate object, lists active candidates, and proceeds only if the candidate sorts into the first
  N entries; it uses ordinary object upload/list/delete operations, not S3 conditional PUT headers.
  The first targeted use is Granicus ffprobe/ffmpeg media
  (`provider_distributed_leases.granicus.com.slots: 2`, reduced from the initial 6 by GH#300 Phase 1).
  Candidate-key order is stable FIFO; waiting and held candidates renew explicit payload expiry so
  a live holder cannot expire or lose election position, while dead owners are reaped after TTL. The
  distributed lease is intentionally not acquired by `GuardedHTTPAdapter.send`; ordinary RSS/page
  fetches keep only the per-process `HostRateLimiter`.
- 403-as-rate-limit lifted into the shared retry (`403` in `_ClampedRetry.status_forcelist`); the bespoke
  `(0, 0.5, 1.5, 3.0)` loop in `GranicusProvider.resolve_media_url` removed.
- ffmpeg/ffprobe 403/429 stderr is classified as `rate_limited`; source-cache throttles no longer
  immediately retry the same URL through the direct render fallback, and a circuit breaker (initially
  run-local; storage-shared and tenant-scoped in the Granicus follow-up below) pauses new work for a
  repeatedly throttled provider domain.
- **Beyond the original scope** (the regressions the run surfaced): truncation guard
  (`_guard_against_truncated_audio` — encode < 50 % of feed-declared duration → #120 backoff, not
  hosted), balanced `records.shard_assignment` (source-atomic greedy packing; replaces hash-mod
  `shard_index`, which left `audio (0)` empty; `run.py` weights sources by configured feed/body count
  so large multi-body sources are not bundled with extra small sources), and a responsive 0.5 s ffmpeg
  guard poll (was 5 s, which made every sub-5 s fetch read `seconds=5.0` and hid the truncation).

**Files.** `citypods/http.py`, `citypods/media.py`, `citypods/provider_leases.py`,
`citypods/providers/granicus.py`, `citypods/concat.py`,
`citypods/records.py`, `citypods/run.py`, `config/site_config.yml` (`provider_rate_limits`),
`tests/test_http.py` + `tests/test_media.py` + `tests/test_provider_leases.py` +
`tests/test_records.py` + `tests/test_run.py`.

**Acceptance (met).** An N-thread burst to one host serializes to the configured cap
(`tests/test_http.py`, `tests/test_media.py`); 403 is retried as a rate-limit; the Retry-After clamp
regression passes; `shard_assignment` is disjoint/exhaustive, source-atomic, and balances equal-weight
sources by count; a truncated encode backs off instead of hosting.

---

## Granicus media reliability follow-up (GH#300 / #39 follow-up)

**Maturity: L1→L3** (detailed sequence below). The endpoint contract test GH#300 reproduces as
`RateLimitedMediaFetchError` from `archive-video.granicus.com` during concurrent `audio.yml` runs, while
local serial contracts pass. Diagnosis: **aggregate GitHub-runner Granicus concurrency** (not a dead URL
or parser bug). Solution: reduce distributed/process-local Granicus caps, coordinate endpoint contracts
with the Audio lane, and only test request-shape alternatives if low/no-overlap fetches still fail.

**Three-phase sequence (addresses the likely cause before expensive experiments):**

### Phase 1: Aggregate Granicus concurrency reduction

Test a matrix of `provider_distributed_leases.granicus.com.slots` (2, 3, 4, 5) and
`provider_rate_limits.granicus.com` (keep at 2, tested independently) to measure **backlog drain time
vs 403 prevalence**. Requirement: **zero audio files consistently abandoned or stuck in backoff.**
Acceptance: a test period confirms the optimal cap without audio throughput regression.

**Config changes:**
```yaml
provider_distributed_leases:
  granicus.com:
    slots: [test 2, 3, 4, 5]      # currently 6; start conservative
    
provider_rate_limits:
  granicus.com: 2                  # test independently (keep vs reduce to 1)
```

**Monitoring:** Track in build logs and admin/status dashboard:
- Audio backlog drain rate (materialized minutes/hour)
- Granicus 403 response count per run (success % of media fetches)
- Timeout/retry patterns (are retries eventually succeeding or being abandoned?)

**Rollback:** Run a test period (≥3 scheduled builds) at each cap level before committing; revert
immediately if audio drain rate drops >20%.

**Lease/circuit correctness prerequisite — issue
[#336](https://github.com/BashfulBits/city-meeting-podcasts/issues/336).** Audio #30 showed that
lower caps alone were not sufficient: waiting workers remained behind one-hour soft leases, then 27
already-admitted fetches entered ffmpeg together when those candidates expired; each process also
logged repeated circuit-open transitions. The implementation therefore:

- elects by immutable FIFO candidate-key order rather than object modification time, so renewal
  cannot move an active winner behind queued candidates;
- refreshes explicit expiry for both waiting and acquired candidates, stops renewal before release,
  and reaps dead owners by payload expiry (`last_modified + TTL` remains the fallback for backends
  without object reads);
- records owner plus GitHub run/job metadata for stale-owner diagnosis and cleans up a candidate if
  acquisition exits by exception;
- rechecks the shared provider circuit after distributed/local slots are acquired and immediately
  before ffmpeg/ffprobe starts, treating a newly opened circuit as deferred rather than failed;
- makes the closed→open transition atomic for one trip per cooldown and records per-domain direct
  throttle, trip, circuit-deferral, lease acquisition, total/max wait, renewal, and stale-reap metrics.

No pipeline version changes and no existing artifact is invalidated. Deferred work remains queued and
retries through the normal Audio lane. After merge, use at least three isolated scheduled Audio runs
to decide whether Phase 3 request-shape work is still necessary.

**Runner provisioning hardening.** Audio #33 also demonstrated that installing ffmpeg from Ubuntu
mirrors per shard is not a bounded setup operation: three shards took 4–15 minutes and one remained in
`apt-get update` until cancellation more than four hours later. The Audio lane now prefers a fixed GHCR
runtime tag whose Dockerfile pins the Python base by digest and a static FFmpeg 7.1.4 archive by
immutable release URL + SHA-256. The workflow pulls and invokes the image inside a normal runner step,
mounting the exact checkout as `/workspace`; this deliberately avoids job-level `container:`, where an
image-pull failure occurs before fallback steps can execute. A five-minute pull failure selects the host
path, which restores/downloads and verifies the same static ffmpeg/ffprobe bundle and caches it for later
runs. The scheduled/manual image workflow rebuilds and smoke-tests the runtime weekly and on definition
changes. No pipeline version changes and no artifact backfill is triggered.

The ASR workflow does not use the container image yet: recent runs showed its Python install was stable
at roughly 75–90 seconds and cached Whisper preparation completed in seconds, while its separate Ubuntu
ffmpeg install varied from about 1 to 40 minutes. ASR therefore restores/downloads the same
checksum-pinned static ffmpeg bundle directly on the host and leaves Whisper weights in the established
Actions-cache → Hugging Face/B2 cascade. This removes the demonstrated mirror dependency without
creating a multi-gigabyte image pull per shard or coupling model changes to the runner image.

### Phase 2: Endpoint contract coordination with Audio lane

`contracts.yml` currently runs in its own concurrency group and does **not** acquire Audio's
storage-backed provider leases, making it an uncoordinated Granicus fetcher that can trip CDN throttling.

**Solution:** Pause Granicus media-fetch in `contracts.yml` when `audio.yml` is active; resume once
`audio.yml` completes. Avoids concurrent uncoordinated load.

**Failure mode:** If a contract's Granicus media-fetch fails after retry, auto-retry once at the end of
the `audio.yml` job. If it still fails, mark as a contract failure (the failure is visible in the status
dashboard / contract report, informing the maintainer of ongoing Granicus issues).

**Test plan:** Run contracts **alongside** active `audio.yml` to validate the pause/resume mechanism.

### Phase 3: Request-shape alternatives (only if phases 1–2 don't resolve)

If low/no-overlap Granicus media fetches still return 403 after phases 1–2, test request-shape changes
in priority order (most to least networking-friendly):
1. Add Granicus-specific HTTP headers (`Referer: https://granicus.com`, `Origin`).
2. Fetch `DownloadFile.php` directly instead of following the pre-resolved `archive-video` URL.
3. Discover and test an HLS/streaming URL that Granicus serves more consistently to Actions runners.

**2026-06-19 evidence and recovery refinement (#337/#353).** The corrected isolated GitHub-hosted
short-fetch matrix passed all six controls after Audio #37: Ubuntu FFmpeg and pinned FFmpeg 7.1.4,
the same archive object, direct `DownloadFile.php`, archive + `Referer`/`Origin`, and two concurrent
archive samples. No request shape distinguished itself. Together with Audio #33's substantial
successful transfer followed by #34–#37's immediate 403s, the leading hypothesis is a rolling
request/byte/egress reputation limit or cooldown state, not a permanently invalid archive transport.

Before activating a transport fallback:

- the manual isolated `granicus-probe.yml` runs `probe_granicus_sustained.py` against both known-good
  controls and the exact Fort Worth archive objects that opened Audio #37's circuit;
- it shares Audio's workflow-level `audio` concurrency group (`cancel-in-progress: false`), so a
  schedule cannot begin between the probe's isolation check and its network cases;
- it measures round-robin repeated short requests, progressively longer bounded transfers, a
  configurable quiet interval and post-cooldown check, then concurrency only after serial evidence;
- output is an uploaded redacted JSON artifact containing hostname/path/outcome/timestamps/bytes but
  never signed query strings.
- the same isolated workflow also offers a low-volume transport mode. On one GitHub-hosted runner it
  alternates which client goes first while pairing production-pinned ffmpeg with curl against the
  exact Arlington, Pflugerville, and Fort Worth Audio #40 archive objects plus an Audio #33 control.
  Curl cases record only selected status/range/timing/final-host metadata, test browser context,
  disable retries and automatic redirects, and cap admission reads. The existing short-fetch matrix
  retains its guarded `DownloadFile.php` comparison. At most a
  configurable number of objects under a configurable size ceiling are downloaded fully and passed
  through local ffprobe plus a 30-second local ffmpeg stream-copy. A production curl source-cache
  fallback is justified only if curl succeeds while remote ffmpeg fails and the local-media proof
  succeeds; if both receive 403, the evidence continues to favor runner egress/CDN cooldown state.

**2026-06-20 alternate-egress gate and chosen deviation.** The GitHub-hosted transport artifact
returned immediate HTTP 403 for all 12 cases: direct ffmpeg, curl Range, and curl with browser context
across Arlington, Pflugerville, Fort Worth, and the historically successful Audio #33 control. The
same exact objects then all succeeded from a Mac (12/12 short cases plus one 89.9 MB full Fort Worth
download, local ffprobe, and local ffmpeg). This rules out a curl fallback on GitHub and makes shared
GitHub-hosted egress/CDN reputation the leading diagnosis.

The earlier abort text below said not to prototype off-Actions solutions during this phase. The
maintainer explicitly chose a narrow deviation after the direct-vs-Mac evidence: test Cloudflare
alternate egress before committing to a self-hosted runner. The implementation is diagnostic and
reversible, not a production routing change:

- `workers/granicus-media-proxy` is a streaming Worker with no arbitrary URL input. It requires a
  bearer secret, hard-codes `archive-video.granicus.com`, accepts only committed tenants and strict
  tenant-prefixed `.mp4` names, refuses queries/encoded paths/upstream redirects, forwards only
  `Range` and cache validators, returns selected media headers, and sends `Cache-Control: no-store`.
- The large MP4 is a response body, not a Worker request body. The GitHub request contains only
  headers and a small path; the Worker returns `upstream.body` without `arrayBuffer()` or R2 storage.
- `granicus-probe.yml` adds a `worker` mode using GitHub secrets for the Worker origin/token. It
  compares direct curl with Worker curl and Worker ffmpeg on one isolated runner, then permits at
  most one full object under the configured size ceiling through local ffprobe/ffmpeg.
- The token and Worker endpoint are redacted from output. Production Audio receives no proxy secret
  and does not construct proxy URLs in this slice.
- `.github/workflows/granicus-worker-deploy.yml` makes deployment operational rather than
  memory-dependent: a push to `main` redeploys only when Worker source, Wrangler configuration, or
  the deployment workflow changes, and a manual dispatch remains available. It tests before deploy,
  authenticates with a scoped `CLOUDFLARE_API_TOKEN` plus account ID, and deliberately leaves the
  runtime `PROXY_TOKEN` in Cloudflare instead of copying it into deployment CI.

Activation gate for a later production fallback: direct requests on the same runner receive 403,
Worker Range returns 206, Worker ffmpeg succeeds, one bounded full download validates locally, and
the Worker remains stable for at least two isolated probes. Only then design one direct-first,
single-Worker-attempt fallback inside the existing Granicus lease/circuit envelope.

Production recovery is also no longer “open circuit, rapidly consume the whole queue as deferred.”
A planner/source-cache 403/429 is persisted once as that episode's materialization failure and halts
the remaining stage chain, preventing an immediate duplicate AudioStage request. Circuit-open items
remain non-failures and are parked while ordinary work drains. After cooldown, one parked item is a
half-open canary: another throttle immediately reopens the circuit; a complete materialization records
recovery and releases the remaining parked work through the unchanged local/distributed caps. The
stop signal cancels the wait, so recovery never overrides the wall-clock or supersession budget.
Run-history convenience totals now sum throttle/deferral events across the whole audio stage chain,
including planner-stage source-cache requests, instead of reporting only AudioStage.

The breaker is storage-coordinated across Audio shards rather than run-local. Deterministic failure,
open, and domain-trip JSON objects are mutated under a separate one-slot FIFO lease, preserving the
ordinary-object/B2-compatible coordination model without assuming compare-and-swap. Native Granicus
archive paths derive a tenant from `/<tenant>/<object>`; tenant subdomains and the Granicus-owned
Swagit media host use the same stable tenant-scope vocabulary. The configured threshold opens only
that tenant. Two distinct tenant trips inside one cooldown window open the emergency
`granicus.com` circuit. The domain marker supersedes tenant state for admission and can be probed by
an already-parked tenant item, so a late escalation does not strand earlier queue groups.

Half-open ownership is also shared: one shard atomically changes the marker to `probing`, siblings
wait for its result, and a probe abandoned by a dead shard is reclaimable after
`probe_ttl_seconds`. A successful probe deletes the shared marker and siblings discard stale local
open caches on their next boundary refresh; a throttled domain canary reopens the domain immediately
without requiring two fresh tenant trips. The queue retains circuit keys in stage statistics and
releases only the matching tenant/domain bucket, avoiding cross-tenant suppression.

New breaker telemetry adds `recovery_probes` and `recoveries`, keyed by tenant/domain circuit scope.
Existing `rate_limited`, `circuit_trips`, and `circuit_deferred` counters remain cumulative. Existing
lease telemetry remains at the registrable-domain key. No audio recipe, object identity, or pipeline
version changes; existing artifacts are untouched and only future attempts use this recovery behavior.

**Abort & escalation condition:** If a single audio file is **attempted across 8+ materialize runs
without completing**, flag it as a high-priority GH issue (stuck download, needs investigation). Do
not enable an off-GitHub-Actions production path without the explicit evidence and activation gate
above.

**Files.** `config/site_config.yml` (`provider_distributed_leases`, `provider_rate_limits`,
`provider_rate_limit_circuit_breakers`);
`.github/workflows/audio.yml` + `contracts.yml` (coordination gates);
`.github/workflows/granicus-probe.yml` +
`scripts/probe_granicus_{sustained,transport,worker}.py` +
`workers/granicus-media-proxy/` (manual sustained, transport, and alternate-egress experiments);
`citypods/provider_circuits.py`, `media.py`, `stages.py`, and `run.py` (shared
tenant/domain state, single-attempt accounting, half-open recovery, parked queue);
`citypods/provider_leases.py` (coordination primitive + metrics); `citypods/report/status.py`
(dashboard flags + budget-remaining visuals).

**Acceptance.** Phase 1: backlog drain rate stable, 403 rate <5% (success ≥95%). Phase 2: contracts
auto-pause/resume without manual intervention, auto-retry closes transient failures. Phase 3 (if needed):
one request-shape change resolves 403s without breaking other providers.

---

## H9 — Combined-throughput evaluation (Diarization $/speaker-hour)

**Maturity: L3** (finalized benchmark scope below). Measures the three built execution homes
(local-sharded H6b + Modal H14b + Beam H14c) for combined free-tier transcript ceiling and diarization
cost, guiding the decision to adopt paid backends for 1.0 launch.

**Benchmark scope (fixed, reproducible):**

### Audio mix
- Record the full catalog's recording-duration distribution and sample from its short, median, long,
  and extreme-duration bands rather than treating one synthetic average as representative.
- Keep a reproducible core set (at least 5 short, 5 medium, and 5 long meetings), and include external
  long-audio canaries above the configured local ceiling, including the approximately 6.7h and 7.2h
  failure class that motivated the memory guard.
- Stratify by model and relevant audio condition so a duration conclusion is not accidentally a
  single-source or single-codec result.

### Transcription comparison
Test **two pipelines:**
1. **Independent:** transcribe first, then diarize in a separate subsequent run.
2. **Combined external flow:** transcribe + diarize in one episode worker while retaining distinct
   artifact/version state and independently publishable transcription.

Measure per-backend (local, Modal, Beam):
- **Duration envelope:** local success/failure rate and peak memory by recording duration and model.
- **Compute throughput:** CPU and GPU real-time factor plus transcript-minutes/runner-hour or
  sustainable wall-week.
- **Startup and I/O:** worker cold-start, audio download, decode/resample, and temporary-storage time.
- **Combined-flow economics:** ASR+diarization in one worker versus separate workers, including avoided
  startup, transfer, download, and decode; do not count unrelated neural-model compute as reused.
- **Chunking:** overlap/stitching wall time, duplicate-removal rate, excessive-gap/coverage validation,
  and boundary transcript quality.
- **Diarization quality:** meeting-wide speaker consistency across chunk boundaries and word/segment
  assignment quality; pyannote v3 GPU versus CPU wespeaker/speechbrain ECAPA-TDNN.
- **Retry economics:** cost and latency of whole-episode retry versus durable chunk-level retry.
- **Artifact overhead:** temporary/final transfer bytes, storage requests, and storage footprint.
- **Capacity/cost:** free-tier consumption by backend, **$/transcript-hour**, and incremental
  **$/diarized speaker-hour**.
- **Operations:** queue age and routing outcome by duration/task/backend, separating dispatch decline
  (`budget`, `capacity`, `capability`, `external-required`) from accepted worker failures.

### Decision gate
**1.0 launch requirement:** The 80-feed catalog must complete its initial backlog **within one
calendar month** using free-tier combined capacity (local + Modal + Beam). Monitor backlog burn-down
in `admin/status` after launch using burndown charts per audio step (transcribe, diarize, encode, etc.).
If 80-feed backlog cannot clear in <1 month, a paid backend is required; post-1.0, continue monitoring
to decide Beam/Modal credit refresh or self-hosted GPU migration.

The measured recommendation must also decide:

- the default local duration ceiling by runner/model;
- when GPU routing should be preferred rather than merely available;
- whether chunk-level persistence repays its storage/lease/reconciliation complexity;
- whether combined ASR+diarization materially reduces cost;
- the first paid or self-hosted step if free tiers cannot clear ongoing inflow.

**Files.** `citypods/bench.py` (extended with duration/memory, per-backend throughput, routing, and cost);
`tests/test_compute_throughput.py` (reproducible duration-stratified mix, combined vs independent runs);
`review/12` (findings + recommendation), `ARCHITECTURE.md` (measured capability),
`admin/status` dashboard (burndown charts per stage, free-tier budget remaining).

**Acceptance.** For each backend + pipeline, report the measures above with sample counts and confidence
intervals where meaningful. Record transcript boundary quality and speaker consistency, not only raw
speed. Write the local-ceiling, preferred-routing, combined-flow, chunk-persistence, and first
paid/self-hosted decisions in `review/12 §H9`; update ARCHITECTURE.md only with capabilities actually
measured and shipped.

---

## H14b — Modal transcription adapter (Async dispatch backend)

**Maturity: L3** (finalized implementation details below). Implements the H13 `Backend` protocol for
Modal serverless GPU, dispatching transcription (and future diarization) jobs asynchronously.

**Job lifecycle & error handling:**

- **Accepted-job failures** (Modal container crash, timeout, network interruption): automatically retry
  **once** on the same backend. If the retry also fails, record an actual worker failure and release or
  re-queue according to the existing lease/backoff policy; content addressing keeps a later retry
  idempotent.
- **Dispatch declines** (monthly budget unavailable, in-flight cap reached, missing capability, or
  pre-acceptance provider unavailability): return declined without marking the transcript failed. The
  router may try Beam, then an eligible local backend; otherwise the item remains queued.
- **Graceful cancellation:** If workflow is cancelled or runner dies mid-dispatch, allow Modal jobs to
  complete silently. The next deploy's `render` phase reconciles any completed artifacts onto the feed.
  No cancellation API call needed (jobs are content-addressed; re-dispatch is idempotent).
- **Long audio:** the worker uses the shared bounded-memory planner/stitcher and must pass a canary above
  the local duration ceiling before catalog-wide enablement.

**Secrets & configuration:**

- Modal API token: `secrets.MODAL_TOKEN` in GitHub Actions environment.
- Config (per `compute_backends.modal` in `site_config.yml`): `monthly_gpu_seconds` (pin to Modal's
  current free tier), `max_inflight` (concurrent job limit).

**Job serialization:** Uses the shared H13 `InferenceJob` format (job `task`, `inputs` audio URL,
`recipe_hash`, etc.). No Modal-specific serialization needed; the `modal_backend.py` adapter translates
to Modal's API at dispatch time.

**Testing:** (1) Mock Modal backend for unit tests (cheap, catches serialization/logic bugs). (2) One
minimal live integration test on CI (short 5-min audio dispatch) **only when Modal code changes**
(path filter on `citypods/compute/modal*` + `scripts/compute/modal_app.py`). (3) Production canary:
after merge, test on 1–2 real backlog files before enabling on full catalog.

**Dispatch priority & budget:** Follow the shared router policy rather than embedding transcript
semantics in this adapter. Initial policy may prefer Modal or rotate providers, but it must preserve
the hard monthly budget/max-inflight guarantee and expose enough estimates for later capability-,
deadline-, and cost-aware routing.

**Files.** `citypods/compute/modal_backend.py`, `scripts/compute/modal_app.py`,
`citypods/compute/budget.py` (Modal budget ledger), `.github/workflows/asr.yml` (dispatch + compute
reconcile), `tests/test_compute_dispatch.py` (mock dispatch, budget tracking), `SECURITY.md` (free-tier
ToS, external-worker trust boundary).

**Acceptance.** A Modal job is dispatched successfully; artifact (`<uid>-<recipe>.vtt` +
`.words.json`) is written to the object bucket by the worker; the next `render` reconciles it onto the
feed. Budget tracking prevents overage; dispatch decline tries the next eligible route without failure
backoff; accepted-job failures retry once; workflow cancellation gracefully orphans in-flight jobs;
a recording above the local ceiling completes through bounded-memory assembly.

---

## H14c — Beam transcription adapter (Async dispatch backend, parallel with H14b)

**Maturity: L3** (finalized implementation details below). Implements the H13 `Backend` protocol for
Beam Cloud serverless GPU, dispatching transcription and future diarization jobs asynchronously.
**Fully parallelizable with H14b** — both are thin wrappers on the same dispatch infrastructure.

**Job lifecycle & error handling:** Identical to H14b: distinguish pre-acceptance routing declines from
accepted-job failures, retry an accepted transient failure once, preserve leases/reconciliation, and
support bounded-memory long audio.

**Budget tracking:** Separate from Modal. `compute_budget.json` tracks `beam_budget` separately so
differing credit periods and free-tier allotments can be managed independently. Each provider has its
own `monthly_gpu_seconds` and `max_inflight`.

**Dispatch & routing:** Participate in the same configured target order as Modal. A Beam budget,
capacity, or capability decline allows the router to try another external target and then only a
locally eligible fallback. If no backend is eligible, retain the item as queued (`external-required`
or a more specific routing reason); do not classify normal free-tier exhaustion as an ASR failure.

**Secrets & configuration:** Beam API token as `secrets.BEAM_TOKEN` in GitHub Actions. Config per
`compute_backends.beam` in `site_config.yml`: `monthly_gpu_seconds`, `max_inflight`.

**Job serialization:** Shares the same H13 `InferenceJob` format as Modal (no conversion logic in
H14c; serialization happens in the `beam_backend.py` adapter at dispatch time).

**Testing:** (1) Mock Beam backend for unit tests. (2) One minimal live integration test on CI (short
5-min audio dispatch) **only when Beam code changes** (path filter on `citypods/compute/beam*` +
`scripts/compute/beam_app.py`). (3) Production canary: after merge, test on 1–2 real backlog files.

**Files.** `citypods/compute/beam_backend.py`, `scripts/compute/beam_app.py`, `citypods/compute/budget.py`
(Beam budget ledger), `.github/workflows/asr.yml` (dispatch + compute reconcile), `tests/test_compute_dispatch.py`
(mock dispatch, budget tracking), `SECURITY.md` (free-tier ToS).

**Acceptance.** A Beam job is dispatched successfully; artifact is written to the object bucket by the
worker; the next `render` reconciles it. Budget tracking is separate from Modal; both backends' budgets
can be managed independently. Dispatch tries the next configured external backend or an eligible
`local` based on policy; otherwise work remains queued without transcript failure backoff. A recording
above the local ceiling completes through the shared bounded-memory assembly path.

**Admin dashboard:** Add a prominent **"Free-tier budget remaining"** visual showing:
- Modal: % of monthly GPU-seconds used
- Beam: % of monthly GPU-seconds used
- Local: estimated capacity (cores × available concurrency slots)

This allows the maintainer to see at a glance whether free tiers are near exhaustion, and plan upgrades
or load reduction accordingly. **No automatic ticket generation on quota exhaustion**—it is expected
operational behavior, not a code bug.

---

## Module-split note (Codex maintainability, opportunistic)

While touching these areas, extract along natural seams (do **not** refactor for size alone): H5 creates
`citypods/ops/workqueue.py`; if ASR/transcript logic grows, split `citypods/stages/transcript.py`; if
admin grows, `citypods/report/{status,projection}.py`; issue reconciliation → `citypods/audit/issues.py`.

## Post-review code queue (recap · updated 2026-06-16)

Implement in order (**reprioritized 2026-06-08** per the build-log analysis — do-now reliability fires
first): **H10 (align fix, shipped PR #232)** → **H8 (resource guard, shipped PR #235)** → H11a (native
audio/ASR gate + one-slot audio lane + green-run acceptance, **shipped**; cap now at `4`) → **H12
(transcript artifact rework, shipped PR #253)** + **H6a (ASR benchmark, shipped PR #256)** → confirm
`native_audio_max_active: 4` against the A2 criterion (else revert toward `1`/`2`) → H1 (issues) →
H2 (incl. the C2 telemetry record) → H3 → H4 (incl. per-provider error rates) → H5 (all **shipped**) →
**#39** (per-provider rate limits, **shipped**).

**Remaining tail (this plan, detailed §5.5+):**

1. **H13** (GPU/ASR execution-backend interface + `local` adapter — the pre-1.0 lock, do first)
2. **H11b** (strip `deploy.yml` to render-only; render stops persisting records)
3. **H6b** (`audio.yml` + `asr.yml`, sharded by `source_key` + scoped `push_state` + align/transcribe lanes)
4. **Granicus media reliability follow-up** (GH#300; three phases: concurrency caps, endpoint coordination, request-shape experiments)
5. **H14b** (Modal free-tier GPU adapter — can parallelize with H14c)
6. **H14c** (Beam free-tier GPU adapter — can parallelize with H14b)
7. **H9** (combined local-sharded + Modal + Beam throughput evaluation, diarization $/speaker-hour, 80-feed 1-month gate)

Each lands as its own PR with tests; on merge, follow the lifecycle contract (flip review/11 catalog
entry + add timestamp, add CHANGELOG, stamp this doc "Implemented in PR #N" per item). Self-hosted
Mac-mini + AWS GPU remain post-1.0 adapters; the first **LLM API adapter** lands with R3/R4.
