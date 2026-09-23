# DO rows-written bench

Measures the **billed** Durable Object rows each LLM job lifecycle phase writes, by running the
real `LLMSchedulerDO` under workerd (the runtime that meters billing) and summing every SQL
cursor's `rowsWritten`. The results are the constants in `src/write_budget.js`, which size
`MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY` / `MAX_LEASES_PER_UTC_DAY` against the Free plan's
100,000 rows/day. **Re-run after any change to the schema, an index, a trigger, or a lifecycle
statement**, and update `write_budget.js` and `wrangler.jsonc` together.

```bash
cd workers/llm-dispatch-v2
npx wrangler@4 dev --local --port 8799 -c bench/rows-written/wrangler.jsonc
# in another shell:
curl -s "http://127.0.0.1:8799/?name=run1&n=60"            # full lifecycle, 60 one-model jobs
curl -s "http://127.0.0.1:8799/retry?name=run2"            # a retried (requeued) attempt
curl -s "http://127.0.0.1:8799/r429?name=run3"             # an in-lease 429 retry authorization
curl -s "http://127.0.0.1:8799/ingress?name=run4&purpose=chapter-locator&models=gemini/gemini-3.5-flash-lite,deepseek/deepseek-v4-pro,moonshotai/kimi-k3&n=200"
```

Use a fresh `name` per run (each is a new DO instance).

## Measured 2026-09-23

| Phase | Billed rows |
|---|---|
| enqueue | 3 per ingress write unit (12 for 1 model, 18 for 3, 27 for 6); a rejected job writes 0 |
| claim | 9.4 per bundle + 7.6 per leased job (17 for a one-job bundle) |
| attemptStarted | 6 per attempt |
| completeBatch | 10.3 per job (success); a requeued retry ~17 |
| authorizeRetry (in-lease 429) | 3 |
| ack | 5 per job |
| purge (confirmPurge) | 1 per job |
| idle cron tick | 1 |

Cross-check against production: for 2026-09-22's counts (2,202 jobs ingested, 661 bundles, 1,316
provider attempts, ~980 completions) these predict 70.2k rows; Cloudflare billed 71.1k.
