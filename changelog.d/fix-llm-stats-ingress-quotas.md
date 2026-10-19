- **Correct ingress quotas in v2 stats.** Read the normalized purpose reservation map directly
  instead of unwrapping it again, restoring configured job-equivalent quotas and utilization.
  Legacy overrides without a per-job estimate use admission's four-unit minimum. Fresh ingress
  counts remain unchanged: new ledger rows since midnight UTC, excluding replays and retries.
  No extra DO reads or writes, admission changes, pipeline-version bump, or artifact backfill.

- **Separate correction admissions from Worker requeues.** A new schema-correction job counts
  as ingress and no longer increments `retried_today`, which counts completion-time requeues
  of existing jobs. Retain completed/failed/requeue counters in Actions snapshots and report
  observed schema corrections separately per run, with replay caveats. Provider completion
  is distinguished from client validation/content acceptance. Existing daily retry totals
  may include pre-deploy correction admissions until UTC rollover; no historical counts are
  reset or guessed. No new DO counters or row writes.
