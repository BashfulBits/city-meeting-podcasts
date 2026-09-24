/**
 * Durable Object row-write costs for LLM Dispatch v2, in BILLED rows.
 *
 * The Workers Free plan allows 100,000 Durable Object rows written per UTC day across the whole
 * account; past it every DO call fails until 00:00 UTC. Billed rows are not statements or
 * "write units": Cloudflare counts every table row AND every index entry a write touches, plus
 * rows written by triggers.
 *
 * The daily limit is enforced at RUNTIME against what the coordinator actually writes -- every
 * SQL cursor's `rowsWritten`, the same figure Cloudflare bills -- not against a worst-case
 * projection of static caps (coordinator.js _rowsWrittenToday and the DO_ROWS_*_STOP thresholds).
 * A worst-case projection had to assume every lease hit a 429 and requeued (~24 rows), which
 * idled dispatch at roughly half the budget on a typical day (~13 rows per lease).
 *
 * The constants below were MEASURED by `bench/rows-written/` (the real `LLMSchedulerDO` under
 * workerd) on 2026-09-24, after the row-write tiers (#1838, #1840, #1842, #1843): a completed
 * first-try job costs ~20 billed rows end to end (44.3 before). They size the ingress quota and
 * are the reference for re-measuring after a schema, index, or lifecycle-statement change.
 */

/** The platform's account-wide limit; every threshold must stay below it. */
export const DO_ROWS_WRITTEN_PLATFORM_LIMIT = 100000;

/** Rows written per ingress write unit admitted: a job writes 4 + 2 x models billed rows
 * (job row + two unique keys + state index; a clustered model-index row + its unique index each)
 * for 3 + models units -- 1.5 per unit at 1 model, 1.67 at 3, approaching 2 as models grow. 2 is
 * the ceiling at any model count. A rejected job writes nothing. */
export const ROWS_PER_INGRESS_WRITE_UNIT = 2;

/** Fixed per-bundle cost: the bundle row + its index, the claim-outcome scheduler row, and the
 * bundle delete at completion (4.5 claim-side + 1.4 completion-side). */
export const ROWS_PER_BUNDLE = 6;

/** One lease beyond its bundle, worst case: per-job claim 3.8, attemptStarted 3, an in-lease 429
 * retry (authorizeRetry 3 + a second attemptStarted 3), then a requeue completion ~11. A success
 * with its consumption retire is ~13. */
export const ROWS_PER_LEASE_WORST = 24;

/** One job through scheduled cleanup: the purge_pending transition of a failed or aged job (2)
 * plus confirmPurge's row delete (1). A client-retired completion (1 row) never reaches cleanup. */
export const ROWS_PER_CLEANUP_JOB = 3;

export const CRON_TICKS_PER_DAY = 1440;

/** Terminal jobs the scheduled cleanup can retire per UTC day. */
export function cleanupCapacityPerDay({ cleanupIntervalMinutes, purgeBatchLimit }) {
  return Math.floor(CRON_TICKS_PER_DAY / cleanupIntervalMinutes) * purgeBatchLimit;
}
