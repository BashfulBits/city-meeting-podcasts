- **Google Gemma availability recovery:** recognized INTERNAL 500s now use the bounded upstream
  retry budget. Google 500/503 availability failures get three early cron retry opportunities and
  a five-minute cooldown cap, honoring provider Retry-After. Other routes and all quota/concurrency
  limits remain unchanged. Existing persisted cooldowns require operational reconciliation after
  deployment. No pipeline version bump or stored artifact invalidation occurs.
