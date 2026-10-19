- **Correct ingress quotas in v2 stats.** Read the normalized purpose reservation map directly
  instead of unwrapping it again, restoring configured job-equivalent quotas and utilization.
  Legacy overrides without a per-job estimate use admission's four-unit minimum. Fresh ingress
  counts remain unchanged: new ledger rows since midnight UTC, excluding replays and retries.
  No extra DO reads or writes, admission changes, pipeline-version bump, or artifact backfill.

- **Clarify the three job metrics.** Actions snapshots highlight new DO job admissions,
  Worker requeues of existing jobs, and completed jobs for runner validation. New job admissions
  no longer increment `retried_today`. Existing retry totals may include pre-deploy admissions
  until UTC rollover; no historical counts are reset or guessed. No new DO counters or writes.
