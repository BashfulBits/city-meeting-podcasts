- **Correct ingress quotas in v2 stats.** Read the normalized purpose reservation map directly
  instead of unwrapping it again, restoring configured job-equivalent quotas and utilization.
  Legacy overrides without a per-job estimate use admission's four-unit minimum. Fresh ingress
  counts remain unchanged: new ledger rows since midnight UTC, excluding replays and retries.
  No extra DO reads or writes, admission changes, pipeline-version bump, or artifact backfill.
