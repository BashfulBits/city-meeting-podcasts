- **Bound idle ASR lease reads.** `max_scan` now counts every attempted claim, including
  denied reads of held or same-recipe terminal leases; `scanned` reports actual attempts instead
  of total candidates. Internal workers default to 256 probes independently of their unlimited
  claim count, with CLI > environment > backend-policy precedence for overrides. Invalid or
  negative scan limits fail clearly; explicit zero disables probing. Matrix slots
  inspect adjacent windows that advance with the Actions run number, preserving eventual coverage
  of a stable queue when each window is consumed. External owner-based rotation is unchanged.
  Exhausted scans log candidate and attempt counts; unvisited work remains queued for later runs.
  Internal shutdown flushes partial transcript batches before closing the inference process.
  No pipeline-version bump, artifact invalidation, lease-schema change, or catalog backfill.
