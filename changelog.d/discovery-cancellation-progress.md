- **Preserve discovery diagnostics on hard cancellation.** Streamed discovery transports report
  throttled, payload-free progress to the local parent. Timeout results retain the last request
  phase, structural stream state and bounded counters; progress never supplies capacity proof
  or extends deadlines. Configured-job paths, provider requests and budgets remain unchanged.
