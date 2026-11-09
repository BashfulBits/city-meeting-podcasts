- **OrcaRouter's 800/day free quota is now modelled as one shared pool.** The three OrcaRouter
  free routes (DeepSeek V4 Flash, GLM 5.3 Flash, Tencent HY3) each declared `rpd: 800`, but the
  limit is one account-level pool: on 2026-11-09 all three answered 429 with the same Retry-After
  while HY3 had no recorded usage. Routes are now apportioned 360 / 320 / 80 (sum 760, leaving 40
  for canaries and probes) in `config/provider_limits.yml`, with the catalogs recompiled. The
  Worker has no provider-level rpd ledger, so this is an approximation; a test caps the sum.
