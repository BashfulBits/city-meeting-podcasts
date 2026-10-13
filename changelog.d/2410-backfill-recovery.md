- **Recover stalled P1 evaluation backfill (#2409).** Evaluation-only scheduling retries failed
  questions independently, waits for actual quota resets, and applies bounded route cooldowns.
  Saved diagnostics preserve original provider errors. Persistent controller status and attempt
  intents prevent duplicate successes and blind retries after interrupted calls. Gemini keys
  share one model score; Astra remains retained outside the 904-answer comparison.
  No production routes, quotas, timeouts, feeds or pipeline versions change.
