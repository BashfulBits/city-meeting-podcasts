- **LLM pending-handle reconciliation.** Fix LLM pending/submission accounting across producer
  lanes. Register both judge response contracts in the deferred sweep and preserve handles on
  local result-read/setup failures instead of treating those errors as provider failures.
  Complement terminal-feed consumption with a rotating 1,000-recipe pending audit; weekly full
  maintenance refreshes advisory pointers it has already read. Explicit missing-id proofs come
  from existing batch primary-key reads with zero additional DO row writes; only matching client
  handles are cleared, without spending provider retries. Completed results and concurrent
  replacement handles remain protected. Add a bounded manual catch-up input.

- **Producer reservation accounting.** Return temporary producer reservations without
  permanently latching exhaustion. Batched tagger and prelabeler allowances count newly prepared
  work, shadow failures/caches release their slots, and agenda checks its current recipe before
  reserving new-work allowance. Show sibling outstanding counts and room per model without
  adding persistent counters.

- **Payload-free accounting telemetry.** Retain payload-free accounting in Actions JSONL
  artifacts and job summaries: producer cap usage, unsettled reservations, closures below cap,
  per-model judge room, per-task poll outcomes, bounded audit coverage, missing-handle recovery,
  and unresolved reads. Existing lightweight snapshots now show UTC-day ingress quotas and DO
  row-budget gates. Repeated poll observations and stage no-op reuse are explicitly
  distinguished from fresh admissions and successful model outputs.

- **Release and admission invariants.** No pipeline-version bump, recipe change, blanket
  backfill, or automatic artifact invalidation. Existing admission ceilings and real
  queued/leased work are preserved. Existing exhausted failure markers are not reset; a
  confirmed missing handle becomes eligible under normal producer limits.
