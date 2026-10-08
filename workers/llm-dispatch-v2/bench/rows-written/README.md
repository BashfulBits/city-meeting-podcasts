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
| 2. Idle claim outcome every 10 min | 20.58 | 21.62 | 24.18 | 25.22 | 0.2 | 16 | 4 |
| 3. No `attemptStarted` call | 17.58 | 18.62 | 21.18 | 22.22 | 0.2 | 13 | 4 (grant 5) |
| 4. Daily `attempt_usage` cells, no `attempts` rows | 16.33 | 16.40 | 19.98 | 20.07 | 0.2 | 12 | 4 (grant 5) |
| 5. Bundles and claim outcome on the scheduler row | 15.33 | 15.40 | 19.18 | 19.27 | 0.1 | 10 | 4 (grant 5) |

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

Step 2 persists an empty claim whose reason matches the stored last outcome at most every ten
minutes (`EMPTY_CLAIM_REFRESH_MS`); skipped ticks are counted in memory and folded into the next
write, and `stats()` adds them. A claimed tick, a reaped lease or a changed reason is written at
once. The harness's 20 idle ticks after the run (61 s apart) wrote 4 rows: about 290 rows/day on
an idle queue instead of 2,880. Per-job lifecycle costs are unchanged.

Step 3 drops the executor's per-attempt `attemptStarted` RPC (attempt row insert 2, `attempts++`
1, accounting 1). The claim counts the attempt in the lease UPDATE it already makes, a granted
429 retry counts its own in its transaction, and completion gives the count back when no call
started. The executor fences locally on the `lease_expires_at` it is handed. The attempt row is
now written once, at completion, as an insert (2 rows instead of the old upsert's 1), so
completion rose 318 -> 378 while the attempt phase fell 240 -> 0. `/r429` now also reports a
granted retry (`authorize_grant_w`: 5 rows, versus 4 plus the retry's own 4-row `attemptStarted`
before). `ROWS_PER_LEASE_WORST` falls from 28 to 23.

Step 4 removes the per-attempt `attempts` journal (insert 2 rows at completion, retention delete 1
later). Each `completeBatch` folds its calls into one `attempt_usage` row per (UTC day, lane,
route), which `usage_today` reads; failed attempts are logged (`attempt_outcome`) instead. In this
bench every bundle uses one route, so completion writes one usage row per bundle (378 -> 303) and
the deferred prune falls from 62 to 4. A bundle spread over several routes writes one per route.
The old table is dropped at migration: `DROP TABLE` bills no rows under workerd, where pruning it
would bill one per row. `ROWS_PER_LEASE_WORST` falls from 23 to 21.

Step 5 drops the `bundles` table: active bundles are a map in `scheduler.active_bundles_json`,
and every scheduler change in a transaction (claim outcome and counters, bundles, a completion's
requeue counter) is staged and written by that transaction's single accounting UPDATE. Per bundle
that removes the bundle insert and its index entry (2), the separate claim-outcome row (1) and the
bundle delete (1): claim 312 -> 267 and completion 303 -> 288 over 15 bundles. A persisted idle
outcome now costs 1 row (idle tick 0.2 -> 0.1), and a one-job requeue costs 9 + 10 instead of
12 + 12. `ROWS_PER_BUNDLE` falls from 8 to 4 and `ROWS_PER_LEASE_WORST` from 21 to 20.
The table is dropped at migration after its active bundles are copied, which bills one row.

### Where this leaves a job (2026-10-07)

A first-try one-model job now costs 15.4 billed rows end to end including retention, against
22.6 at the start of this series (-32%) and 44.3 before the 2026-09 tiers; a three-model job
19.3 against 28.2. At the 90,000-row safe stop that is about 5,800 one-model first-try jobs a day
before retries and idle overhead (previously about 4,000).

### Slice 3a existing-index rescue measurements (2026-10-08, #2190)

Reuse of `(state, updated_at, id)` avoids the proposed extra `(state, id)` index. The rejected
index measured 21 writes and 65 reads to build over 20 retained jobs, and added three lifecycle
writes per first-try job. The maintainer approved reuse rather than that unbounded migration.

A 20-job structural failure page now bills 82 writes and 104 reads with the current model index,
or 82 writes and 123 reads with the legacy model index. Conservative page reservations are 181
and 221 writes respectively, including scheduler progress. Run the existing local-only harness
with `?rescue=1` or `?rescue=1&legacy=1`. Add `&outage=1` to inject a transaction failure, verify
persisted jobs/indexes/scheduler are unchanged, recreate the coordinator and replay the page.
This simulates an outage locally; it does not exhaust production quota.
The outage run confirmed rollback of all persisted state, zero writes on coordinator recreation
(22 reads), and a successful replay costing the same 82 writes and 104 reads.
With `?rescue=1&n=1000`, all 1,000 jobs were processed in 51 pages including the final empty
page. No page exceeded 104 reads or 82 writes, confirming bounded seeks even when timestamps tie.

A first-try one-model job remains 15.40 writes (60 jobs, 15 bundles) including retirement and
retention. The tested three-model pool costs 19.97 (60 jobs, 12 bundles; the model list from the
usage example above). Retry costs 9 claim writes plus 10 attempt/requeue writes. An accounting
check recorded 66 actual writes and a persisted delta of 66. Ingress/lifecycle reservation
constants and configured budgets remain unchanged.
