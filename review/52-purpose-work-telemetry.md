# Purpose-bound LLM work telemetry

**Implemented in PR #2004 · 2026-10-04 · FROZEN**

Maintainer-directed follow-on to PR #1983. Implementation merged on 2026-10-04; this document
is a point-in-time record. Future changes belong in review/11 and a new design when needed.

## Decision and scope

The maintainer requested implementation of registration-enforced telemetry through the shared
producer/job layer. This deliberately extends frozen review/50's read-only consumer scope and
supersedes inference from stage names/defer tokens for new events. review/49's eventual incremental
eligibility ledger remains future work: this change records bounded run snapshots during existing
producer traversal, with no additional per-job remote writes or catalog scans.

Each dispatch purpose declares a telemetry producer, accounting unit, coverage scope and completion rule in
`llm_lanes`. Invalid/missing contracts fail registry validation and compilation. Work is registered
before caps/stop/submission gates; a thread-safe run tracker deduplicates a purpose/unit and tracks
standard ready, queued, ingress-limited, stopped, blocked, policy-held and errored states. Backends record
job outcomes automatically through purpose-bound work handles. Backend completion is separate from
successful producer consumption. Existing batching and recipe identities remain unchanged.

## Implementation files

- `citypods/compute/llm_work.py`: registry-bound tracker, work handle, tracked backend adapter,
  schema-versioned aggregate snapshots; no prompts, credentials or unit identities in snapshots.
- `citypods/compute/llm_lanes.py`, `config/site_config.yml`: required telemetry contract.
- `citypods/stages.py`: bind production work before gates, use tracked backends,
  mark consumption only after producer finalization; account shared-stage purposes independently.
- `citypods/compute/llm.py`: validate binding at shared dispatch entry points; retain bindings through
  deferred batching/flush and capture submission outcomes without extra network I/O.
- `citypods/run.py`: serialize purpose snapshots with existing append-only run events after source
  persistence; partial/interrupted runs are not trusted backlog censuses.
- `citypods/tournament.py`, `citypods/r5_benchmark.py`: research-purpose work tracking during existing
  sample traversal and one aggregate event after local result-state persistence. Research units and
  sample coverage are explicit, not compared as full-catalog episode backlog.
- `citypods/ops/backlog_trend.py`: prefer explicit per-purpose snapshots, retain legacy reading for
  old events, use actual consumed counts and units, suppress recommendations for partial coverage.
- Tests for registry enforcement, dedup/replay/concurrency, caps before submission, failed finalization,
  batch flush outcomes, shared-stage ownership, report migration and retirement.
- `ARCHITECTURE.md`, `CHANGELOG.md`, `ROADMAP.md`, `review/11`: lifecycle documentation.

## Invariants and acceptance

No Worker or canonical episode schema changes; the run-event telemetry field is additive. No provider calls, new dependencies, quota/configuration-budget changes,
recipe/pipeline-version bumps or artifact invalidation. An unregistered or mismatched purpose must
be rejected before submission. New purposes using this API need registration and eligibility logic
in their producer, but no custom telemetry implementation or report edits. Retired purposes remain
readable from historical explicit events without loading their old registry entry.

Deduplication is run-local, keyed by purpose and producer work identity; recipe-keyed job outcomes
are deduplicated separately, so one episode can have several jobs without inflated episode backlog.
No cumulative created-minus-completed ledger is introduced; report daily points are scoped run
snapshots. Incomplete traversals cannot establish a zero/full backlog. Completion throughput counts
only successfully consumed work, never rule-only processing, cached reuse or provider responses.
Telemetry is additive observability; encoding, transcripts, job recipes and their reuse are unchanged.

Run whole-repo Ruff checks and the offline suite. Open one implementation PR; resolve review/CI,
merge with a merge commit, then freeze this design and record delivery under the lifecycle contract.

## Integration contract for a new purpose

1. Register `telemetry: {producer: <stage-name>, unit: <nonempty-unit>, completion: consumed,
   scope: retained_catalog|sample}` alongside the model/budget contract. Registry compilation rejects
   a missing or invalid contract; units are open strings so adding a new domain unit needs no enum edit.
2. Production stages extend `LLMProducerStage`: declare enabled purposes and cheap `work_items`
   eligibility. The runner requires this census hook even when completion-cache filtering skips work.
   Research traversals use `@tracked_producer` and share one tracker across their phases.
3. Obtain `tracker.item(purpose, stable_identity, producer=...)` before admission/stop gates. Submit
   through `work.backend(...)` or `work.bind(job)`; the shared backend rejects unbound jobs in producer
   scopes and mismatched purpose/run/producer bindings before any I/O. Manual standalone API calls
   remain compatible outside these scopes; deferred sweep consumes previously registered jobs.
4. Call `work.consumed()` after the producer validates and installs the result, or
   `work.consumed(reused=True)` for already-current artifacts. Deferrals use the shared state enum.
   Successful consumption survives cached batch replay and late stop gates.
5. Persist source/result state through its existing owner. Emit one aggregate snapshot in the existing
   run-event channel; research emits one additional aggregate object per traversal, never per job.

The report automatically discovers explicit historical purposes and their units, even after registry
retirement. Complete, unscoped catalog events provide daily measurements; a full set of disjoint
shards can also provide them. Missing shards, source/city subsets and research samples cannot establish
catalog backlog or recommend catalog capacity. Research reports expose sample scope, observed units,
consumption and job outcomes. Legacy defer-token reading remains for older event windows.

This is a run snapshot, not the incremental ledger proposed by review/49. New eligibility semantics
still belong to their producer; telemetry and report wiring need no per-purpose implementation.

Unvisited ready work is reported as ready rather than guessed to be ingress-limited or stopped.
A capacity action is suppressed while ready work has no measured admission disposition; it remains
part of working backlog. Enabled purposes with a completed empty census are measured zero; absent or
incomplete purposes are unmeasured. Unit changes do not mix incomparable historical daily points.
