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
# full lifecycle of a three-model pool (claim deletes every model index row, not just one)
curl -s "http://127.0.0.1:8799/?name=run7&n=60&retire=1&purpose=topic-tags:tagger&models=gemini/gemini-3.1-flash-lite,kilo/stepfun/step-3.7-flash:free,deepseek/deepseek-v4-flash"
```

Every lifecycle run reports `lifecycle_w` (rows a job writes while it is live) and `total_w`,
which adds `prune_w`: the retention prune that deletes its bookkeeping rows days later
(`attempts`, terminal bundles), run directly past every retention window. `per_job` divides both
by `n`.

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

## Pool-size accounting correction (2026-10-02)

The full-lifecycle run above uses one indexed model per job; the multi-model `/ingress` run
measures enqueue only. `claimDispatchWindow` also deletes all `job_models` rows for each leased
job, including their unique index. The capacity projection must include those deletes: two extra
billed rows per additional model at claim as well as two at enqueue. A constant 4.9 claim rows
for every pool size undercounts multi-model jobs. The corrected design projection is in
[capacity evidence](../../../../review/evidence/2026-10-02-llm-800-meeting-capacity.md).
Re-run the full lifecycle
with representative pool sizes before treating its row totals as measured production capacity;
the existing one-model measurement remains the baseline, not proof for all pools.

## Atomic accounting remeasurement (2026-10-04)

The meter now wraps the raw SQL handle and delegates to the production counting wrapper,
including its accounting writes. The previous override bypassed production accounting.
With 60 one-model jobs, four jobs per bundle, and consumption retirement, measured billed
rows are: enqueue 364, claim 312, attemptStarted 240, completeBatch 318, retire 61;
**1,295 total / 60 = 21.58 rows per job**. Writing transactions add one singleton accounting
row; read-only snapshots, replays, and idle braked claims add none. An ordinary idle claim
writes two rows (claim outcome plus accounting).

```bash
curl -s "http://127.0.0.1:8799/accounting?name=accounting-recreation"
```

This recreates coordinator instances against the same workerd SQLite storage between writing
RPCs and compares the independent billed-write meter with the persisted counter delta.
Both must match exactly. Worst-case projection constants now include accounting: 8 rows per
bundle, 28 per leased job, and 5 per cleanup job. Configured quotas and thresholds are unchanged.


## Quota-schema guard regression (2026-10-05)

Run `curl -s "http://127.0.0.1:8799/schema?name=schema-guard"` against a fresh local object.
The harness removes the four quota columns after enqueue/claim, attempts three expired-lease
claims, then recreates the coordinator to migrate and resume. Each failed claim must write zero
rows; migration must add only missing columns, resumed jobs must be nonzero, and the next
current-schema recreation must write zero rows. The independent meter uses real workerd cursors,
including index entries. Use local storage only: this harness deliberately removes columns.

Measured with the deployment's workerd 1.20260921.1: three rejected claims each wrote/read zero
rows; all four missing-column ALTERs plus accounting cost five writes once; dispatch resumed four
jobs; the next recreation wrote zero rows. The separate accounting recreation check still measured
71 writes and an exactly matching persisted delta.


## Row-write reduction ledger (#1844, from 2026-10-07)

Each PR in this series re-runs the commands above against a fresh object and records the result
here before updating `src/write_budget.js`. Figures are billed rows per job at four jobs per
bundle with consumption retirement (`retire=1`); "total" includes the deferred prune. Measured
with workerd from wrangler 4.131.1.

| Step | 1 model live | 1 model total | 3 models live | 3 models total | idle tick | requeue | in-lease 429 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Baseline (main @ 065a1f1f) | 21.58 | 22.62 | 27.18 | 28.22 | 2 | 17 | 4 |
| 1. No `job_models (job_id, model)` index | 20.58 | 21.62 | 24.18 | 25.22 | 2 | 16 | 4 |

Baseline phase split (one model, 60 jobs, 15 bundles): enqueue 364, claim 312, attemptStarted
240, completeBatch 318, retire 61, prune 62. Three models (12 bundles): enqueue 604, claim 410,
attemptStarted 240, completeBatch 316, retire 61, prune 62. Deletes bill one row per deleted
table row: index entries are billed on insert but not on delete in these measurements.
`requeue` is the `/retry` endpoint's attemptStarted + requeueing completion; `in-lease 429` is
`/r429`'s authorizeRetry, both including their accounting row.

Step 1 removes the unique `(job_id, model)` index: a queued job records its model-index keys in
`jobs.queue_models` (in the statement that already writes its row) and every unindex is a
primary-key delete. Enqueue is now `4 + models` rows per job (one model 304 = 5.07/job, three
models 424 = 7.07/job); claim deletes were already one billed row each and are unchanged.
`ROWS_PER_INGRESS_WRITE_UNIT` falls from 2 to 1.25 once the legacy index is retired; until then
enqueue reserves `ROWS_PER_INGRESS_WRITE_UNIT_LEGACY_INDEX` (2), since each model-index insert also
writes that index's entry.
