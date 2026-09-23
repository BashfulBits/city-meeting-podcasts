/**
 * Durable Object row-write budget for LLM Dispatch v2, in BILLED rows.
 *
 * The Workers Free plan allows 100,000 Durable Object rows written per UTC day across the whole
 * account; past it every DO call fails until 00:00 UTC. Billed rows are not statements or
 * "write units": Cloudflare counts every table row AND every index entry a write touches, plus
 * rows written by triggers. The Worker's own ingress currency (`3 + models` write units per job)
 * therefore understates the real cost, and nothing bounded the dispatch half at all -- on
 * 2026-09-23 the account reached 97,877 rows written by 15:00 UTC.
 *
 * Every constant below was MEASURED, not derived: `bench/rows-written/` runs the real
 * `LLMSchedulerDO` under workerd (the runtime that meters billing) and sums each SQL cursor's
 * `rowsWritten` per lifecycle phase. Cross-check: these constants predict 70.2k rows for
 * 2026-09-22's real counts (2,202 jobs ingested, 661 bundles, 1,316 provider attempts, ~980
 * completions) against 71.1k billed. Re-run the bench and update these whenever the schema, an
 * index, a trigger, or a lifecycle statement changes.
 */

/** Rows written per ingress write unit admitted (12 for a 1-model job, 18 for 3, 27 for 6).
 * A rejected job writes nothing. */
export const ROWS_PER_INGRESS_WRITE_UNIT = 3;

/** One lease, worst case (a one-job bundle): claim 17 (9.4 per bundle + 7.6 per job),
 * attemptStarted 6, completeBatch 10.3. A retried attempt is a new lease and costs the same. */
export const ROWS_PER_LEASE_WORST = 34;

/** Retiring one terminal job: the consumption ack (or the retention transition for a job no
 * client acks) 5, plus confirmPurge's row delete 1. At most one per lease. */
export const ROWS_PER_TERMINAL_JOB = 6;

/** An empty cron claim writes the claim-outcome snapshot row. */
export const ROWS_PER_IDLE_TICK = 1;

export const CRON_TICKS_PER_DAY = 1440;

/**
 * The worst-case rows one UTC day can write under these caps: every admissible ingress unit,
 * every admissible lease at its one-job-bundle cost, a terminal retirement per lease, and every
 * tick idle. Maintenance (cancels, schema retries, route_failures upserts) is NOT modeled here --
 * it is what the gap between this and the platform's 100,000 exists to absorb.
 */
export function projectedDailyRowsWritten({ maxIngressWriteUnits, maxLeases }) {
  return (
    ROWS_PER_INGRESS_WRITE_UNIT * maxIngressWriteUnits +
    (ROWS_PER_LEASE_WORST + ROWS_PER_TERMINAL_JOB) * maxLeases +
    ROWS_PER_IDLE_TICK * CRON_TICKS_PER_DAY
  );
}
