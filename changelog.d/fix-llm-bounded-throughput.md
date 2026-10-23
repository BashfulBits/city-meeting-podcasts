- **Fill bounded LLM dispatch windows more effectively.** Match the stale four-job per-model
  allowance to the existing five-job bundle cap so one healthy model can fill every slot.
  Partially filled model heads can use their existing bounded queue extension, and coordinator
  restarts resume from scan frontiers checkpointed in existing claim accounting. Empty diagnostics
  include their event time. The 32-row lookahead, provider quotas, producer ceilings, concurrency,
  and free-tier row guards stay unchanged; idle-write coalescing still applies. No pipeline-version
  bump or backfill is needed.
