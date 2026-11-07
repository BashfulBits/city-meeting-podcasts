- **Aggregate lane completions from packed telemetry in /v2/stats.** Read completed job
  counts per lane from in-memory packed telemetry shards when packed state authority is
  active, falling back to SQLite `attempt_usage` only when packed state is inactive.
