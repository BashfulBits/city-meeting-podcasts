# DO rows-written bench

Measures the **billed** Durable Object rows each LLM job lifecycle phase writes, by running the
real `LLMSchedulerDO` under workerd (the runtime that meters billing) and summing every SQL
cursor's `rowsWritten`. The results are the constants in `src/write_budget.js`, which size the
ingress quota (`MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY`) under the enqueue threshold. **Re-run after any change to the schema, an index, a trigger, or a lifecycle
statement**, and update `write_budget.js` and `wrangler.jsonc` together.

```bash
cd workers/llm-dispatch-v2
npx wrangler@4 dev --local --port 8799 -c bench/rows-written/wrangler.jsonc
# in another shell:
curl -s "http://127.0.0.1:8799/?name=run1&n=60&retire=1"   # full lifecycle, 60 one-model jobs, client retire
curl -s "http://127.0.0.1:8799/?name=run2&n=60"            # same, ack + scheduled-cleanup path
curl -s "http://127.0.0.1:8799/?name=run3&n=30&bundle=1"   # one-job bundles (per-bundle vs per-job split)
curl -s "http://127.0.0.1:8799/retry?name=run4"            # a retried (requeued) attempt
curl -s "http://127.0.0.1:8799/r429?name=run5"             # an in-lease 429 retry authorization
curl -s "http://127.0.0.1:8799/ingress?name=run6&purpose=chapter-locator&models=gemini/gemini-3.5-flash-lite,deepseek/deepseek-v4-pro,moonshotai/kimi-k3&n=200"
```

Use a fresh `name` per run (each is a new DO instance).

## Measured 2026-09-24 (after the row-write tiers #1838/#1840/#1842/#1843)

| Phase | Billed rows |
|---|---|
| enqueue | 4 + 2 per model per job: 6.0 for 1 model (1.5 per write unit), 10.0 for 3 (1.67); a rejected job writes 0 |
| claim | ~4.5 per bundle + ~3.8 per leased job (8.3 for a one-job bundle, 4.95 per job at 4 per bundle) |
| attemptStarted | 3 per attempt |
| completeBatch, success | ~1.4 per bundle + ~4.7 per job (6.1 one-job bundle, 5.05 per job at 4 per bundle) |
| completeBatch, requeue | ~12 (job + re-indexed model rows + attempt + route ledger + route_failures + counter) |
| authorizeRetry (in-lease 429) | 3 |
| retire (consumption, client default) | 1 per job |
| ack + cleanup (fallback, failed jobs) | ack 2 + confirmPurge 1; a failed job's purge_pending transition 2 |
| idle cron tick | 1 |

A completed first-try job costs ~20 rows end to end at 4 jobs per bundle (44.3 before the tiers).
`src/write_budget.js` records these (worst case 2 rows per ingress unit, 6 per bundle, 24 per
lease, 3 per cleaned-up job). The daily limit itself is enforced at runtime against the rows the
coordinator actually writes (the `DO_ROWS_*_STOP` thresholds), not against these constants.
