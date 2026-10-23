- **Google Gemma availability recovery.** recognized INTERNAL 500s now use the bounded upstream
  retry budget. Google 500/503 availability failures get three early cron retry opportunities and
  a five-minute cooldown cap, honoring provider Retry-After. Other routes and all quota/concurrency
  limits remain unchanged. Existing persisted cooldowns require operational reconciliation after
  deployment. No pipeline version bump or stored artifact invalidation occurs.
- **Google Gemma network diagnostics.** attempts log the account alias, Gateway Ray/colo and fixed
  provider-error signature. One bounded, best-effort Cloudflare trace probe per qualifying bundle
  records trace-reported IP/location, labeling the known synthetic Worker IP. These facts are
  explicitly distinct from Google's incoming connection.
  Cleanup ticks skip probes; no R2/DO writes, credentials, prompts or output text are added to logs.
