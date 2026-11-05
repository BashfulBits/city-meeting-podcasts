- **Optimized Google AI Studio throughput and rate-limit handling.** Anchored Google input-window
  accounting to scheduled start/arrival time rather than response completion time, removed the
  unnecessary 15-second `tpm_window_margin_seconds` buffer on Gemma 4 31B routes, honored exact
  provider 429 `retryDelay` headers (+1s buffer) globally on `own_tpm` instead of forcing a 60s
  cooldown, capped Google transient 5xx route backoff at 5 seconds, and added paced in-window
  retries for transient Google 500/503 errors within the bundle execution deadline.
