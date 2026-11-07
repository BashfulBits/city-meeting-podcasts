- **Deduplicated lane pending jobs and unified model aliases in /v2/stats.**
  Expose deduplicated queued job counts per lane in `ingress.by_purpose[].pending` when
  requested via `?pending=1` or `?lanes=1`. Queries leverage the partial covering index
  `idx_jobs_queued_purpose` with an in-memory 15-second TTL cache, and are skipped completely
  when the global queue count is zero to avoid query overhead during routine cron or producer ticks.
  Also expose unified `model_aliases` and per-lane `completed` job counts to simplify widget
  and dashboard consumers.
