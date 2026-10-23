- **Google Gemma availability recovery.** recognized INTERNAL 500s now use the bounded upstream
  retry budget. Google 500/503 availability failures get three early cron retry opportunities and
  a five-minute cooldown cap, honoring provider Retry-After. Other routes and all quota/concurrency
  limits remain unchanged. Existing persisted cooldowns require operational reconciliation after
  deployment. No pipeline version bump or stored artifact invalidation occurs.
- **Google Gemma network diagnostics.** Attempts log the account alias, Gateway Ray/colo and fixed
  provider-error signature. A bounded diagnostic set compares Cloudflare trace facts with an
  AWS-observed public IP and IP.SB country/ASN lookup of that exact address. Synthetic Worker IPs
  are labeled; observations remain distinct from Google's incoming connection. Cleanup ticks skip
  probes and all three requests fit the Free subrequest budget. No R2/DO writes, credentials,
  prompts or output text are added to logs.
