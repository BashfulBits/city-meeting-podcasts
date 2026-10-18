- **`/v2/stats` says why an empty claim dispatched nothing.** `claim.last_empty_diagnostics` now
  holds the last empty claim's rejection counts and, for each route that refused on capacity, which
  limit did it (input ceiling, trailing-minute input window, requests per minute or day, token
  bucket, penalty buffer, route block) and `max_wait_ms`, how long after the claim its safe start
  is. Added after Google's idle Gemma routes took almost none of the queued packets they could run
  (2026-10-18) and the existing counters could not say why. Diagnostics only; no admission change.
