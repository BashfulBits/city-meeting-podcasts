- **A claim now reads past a stuck head of the queue.** When the oldest few queued jobs for a model
  (`MAX_JOBS_PER_MODEL_CLAIM`, 4) are ones no free route can take this tick, the claim reads on
  from a per-model frontier that advances each tick, up to `MAX_CANDIDATE_LOOKAHEAD` rows in all,
  and wraps at the end of the queue. The head is still read first, so oldest-first order is
  unchanged. On 2026-10-18 the four oldest Gemma packets were over Google's static size filter
  while NVIDIA, SambaNova and OpenRouter were busy, capped or blocked, so the Google routes idled
  with about 775 packets they could run queued behind. `diagnostics.extended_scans` counts the
  extra reads.
