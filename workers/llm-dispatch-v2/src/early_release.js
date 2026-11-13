/**
 * Early slot release (review/58 B1, WP4).
 *
 * A bundle's calls run concurrently and used to settle in ONE completeBatch once the slowest call
 * ended, so a call that returned in 3 s held its in-flight slot, and its route's lease, for up to
 * twelve minutes (58% of all held slot-time was this idle hold, 2026-11-04..11).
 *
 * The executor now flushes settled results through a PARTIAL completion (`completePartial`) when
 * at least one call has settled, at least one is still running, and the debounce has passed since
 * the bundle started or since its last flush. A bundle makes at most MAX_COMMITS commits in total:
 * MAX_COMMITS - 1 partial flushes, then the final completeBatch, which closes the bundle as before.
 * A partial settles its jobs exactly as completeBatch would, decrements the bundle's
 * active_call_count and deletes their leases, so the next claim can use the slot and the route.
 *
 * Idempotency: the DO records every job a commit has processed on the bundle entry
 * (`settled_job_ids`, on the scheduler row the commit already writes). A job listed there is
 * skipped by every later commit of that bundle, so a retried partial, or a final completeBatch that
 * repeats a partial's job, settles nothing twice and counts its usage once. A commit for a bundle
 * that is gone (closed, or reaped after its lease expired) is a no-op, as completeBatch always was.
 *
 * Rows: a partial costs one extra billed row (two in packed mode when it and the final share a
 * telemetry shard). The DO refuses a partial, writing nothing, once the day's partials have
 * written EARLY_SLOT_RELEASE_DAILY_ROWS (prorated over the day; the hard bound) or while the
 * day's rows are ahead of the EARLY_SLOT_RELEASE_ROW_PACE line (the heavy-day yield).
 *
 * Switch: EARLY_SLOT_RELEASE_ENABLED 0 (or MAX_COMMITS 1) restores the single-commit behaviour
 * without a code rollback: the executor sends no partial and the DO refuses any it receives.
 */

/**
 * Code defaults; config/dispatch_tuning.yml carries the same values (tuning lockstep test).
 * review/58 proposed 20 s and three commits; at 6,000 results/day that projects past 85,000 rows
 * (row-accounting test), so the defaults are one partial per bundle after 60 s. DAILY_ROWS 3,000
 * sits above the most partials can write in a day at MAX_COMMITS 2 (MAX_BUNDLES_PER_UTC_DAY 1,400,
 * plus at most MAX_ACTIVE_BUNDLES 12 carried over midnight, x 2 rows = 2,824), so in practice it
 * never refuses for budget, yet it stays a hard bound if MAX_COMMITS or the bundle cap is raised.
 */
export const EARLY_RELEASE_DEFAULTS = Object.freeze({
  EARLY_SLOT_RELEASE_ENABLED: 1,
  EARLY_SLOT_RELEASE_DEBOUNCE_SECONDS: 60,
  EARLY_SLOT_RELEASE_MAX_COMMITS: 2,
  EARLY_SLOT_RELEASE_ROW_PACE: 85000,
  EARLY_SLOT_RELEASE_DAILY_ROWS: 3000,
});

/** review/58 section 8 budgets at most two extra commits per bundle. */
export const EARLY_RELEASE_MAX_COMMITS_CEILING = 3;

/** The bound on the per-bundle list of processed job ids (a corruption backstop). */
export const SETTLED_JOB_IDS_MAX = 64;

/** The most rows one partial adds (accounting row + one shared telemetry shard; row-accounting test). */
export const PARTIAL_ROWS_WORST = 2;

const DAY_MS = 86_400_000;

/**
 * Row pace: a partial may write only while today's billed rows, plus the partial's worst cost,
 * stay on or under `rowPace` rows per day prorated to the time of day. Partials therefore only
 * spend headroom the day has, and a day whose other work is on course for `rowPace` or more gets
 * none (its bundles commit once, as before WP4). Pace 0 refuses every partial.
 */
export function rowPaceAllows(rowsToday, rowPace, nowMs) {
  return rowsToday + PARTIAL_ROWS_WORST <= rowPace * dayFraction(nowMs);
}

/**
 * Daily budget: the day's partials may write at most `dailyRows` rows in all (charged at the
 * worst case), prorated to the time of day so they spread across it. This is the bound that
 * holds whatever the shape of the day: a day ends at most `dailyRows` above what its other work
 * wrote. The pace line alone does not hold it on a back-loaded day, whose early partials would
 * spend headroom its evening needs.
 */
export function dailyBudgetAllows(partialRowsToday, dailyRows, nowMs) {
  return partialRowsToday + PARTIAL_ROWS_WORST <= dailyRows * dayFraction(nowMs);
}

/** Why the DO refuses a partial now ("daily_rows", "row_pace"), or null when it may write. */
export function partialRefusal({ rowsToday, partialRowsToday, config, nowMs }) {
  if (!dailyBudgetAllows(partialRowsToday, config.dailyRows, nowMs)) return "daily_rows";
  if (!rowPaceAllows(rowsToday, config.rowPace, nowMs)) return "row_pace";
  return null;
}

function dayFraction(nowMs) {
  return (((nowMs % DAY_MS) + DAY_MS) % DAY_MS) / DAY_MS;
}

/**
 * The switch as configured. `read(name, fallback)` returns a number. Partial flushes are on only
 * when the switch is on and the commit cap leaves room for at least one partial before the final.
 */
export function earlyReleaseConfig(read) {
  const value = name => {
    const number = Number(read(name, EARLY_RELEASE_DEFAULTS[name]));
    return Number.isFinite(number) ? number : EARLY_RELEASE_DEFAULTS[name];
  };
  const maxCommits = Math.min(
    EARLY_RELEASE_MAX_COMMITS_CEILING, Math.max(1, Math.floor(value("EARLY_SLOT_RELEASE_MAX_COMMITS")))
  );
  return {
    enabled: value("EARLY_SLOT_RELEASE_ENABLED") > 0 && maxCommits >= 2,
    debounceMs: Math.max(0, Math.floor(value("EARLY_SLOT_RELEASE_DEBOUNCE_SECONDS"))) * 1000,
    maxCommits,
    rowPace: Math.max(0, Math.floor(value("EARLY_SLOT_RELEASE_ROW_PACE"))),
    dailyRows: Math.max(0, Math.floor(value("EARLY_SLOT_RELEASE_DAILY_ROWS"))),
  };
}

/** The config from a Worker env (string values, a missing name means the code default). */
export function earlyReleaseConfigFromEnv(env) {
  return earlyReleaseConfig((name, fallback) => {
    const raw = env?.[name];
    return raw === undefined || raw === null || raw === "" ? fallback : Number(raw);
  });
}

/**
 * Collects one bundle's settled results and commits them: partial flushes while calls are still
 * running, then the final completeBatch. Only the executor's own bookkeeping lives here; every
 * settlement decision is the DO's.
 *
 * - `settle(result)` is called once per job, with the job's FINAL result (superseded in-window
 *   retries never reach it).
 * - `laneClosed()` is called when a route lane has finished all its jobs (its connection is free).
 * - `finish()` sends the final completeBatch with every result no partial has confirmed, without
 *   waiting for a partial still in flight (the DO dedupes the overlap), so a partial never moves
 *   the final commit later.
 *
 * Connections: Cloudflare allows six simultaneous open connections per invocation. A partial is
 * one DO RPC, so it starts only while open lanes + 1 (the slot the Gemma probe or cleanup may hold)
 * + 1 (the partial itself) fit in six, and at most one partial is in flight at a time.
 */
export function createBundleSettler({
  coordinator, bundleId, executionToken, totalJobs, laneCount, startAt, config,
  now = () => Date.now(), setTimer = setTimeout, clearTimer = clearTimeout,
  maxConnections = 6, onError = () => {},
}) {
  const entries = [];
  let openLanes = laneCount;
  let lastFlushAt = startAt;
  let partialsSent = 0; // attempts, for the cap
  let partialsAccepted = 0; // confirmed by the DO, for the report
  let partialRefused = null; // the DO's reason, when it refused one
  let partialsDisabled = !config.enabled;
  let inflight = null;
  let timer = null;
  let finished = false;

  const release = (batch, at, by) => {
    for (const entry of batch) {
      if (entry.releasedAt === null) {
        entry.releasedAt = at;
        entry.releasedBy = by;
      }
    }
  };

  const flush = (batch) => {
    partialsSent += 1;
    lastFlushAt = now();
    for (const entry of batch) entry.state = "inflight";
    inflight = (async () => {
      let accepted = false;
      try {
        const response = await coordinator.completePartial(
          bundleId, executionToken, batch.map(entry => entry.result)
        );
        accepted = response?.accepted === true;
        // A refusal (row budget, switch, commit cap) is expected, not an error: reported in
        // bundle_completed, and the final carries the results.
        if (!accepted) partialRefused = String(response?.reason ?? "unknown");
      } catch (err) {
        // An older coordinator has no completePartial; a lost reply may or may not have
        // committed (not counted as a commit). Either way the final completeBatch carries these
        // results, deduped by the DO.
        onError(err);
      }
      if (accepted) {
        partialsAccepted += 1;
        const at = now();
        for (const entry of batch) if (entry.state === "inflight") entry.state = "committed";
        release(batch, at, "partial");
      } else {
        for (const entry of batch) if (entry.state === "inflight") entry.state = "pending";
        partialsDisabled = true;
      }
      inflight = null;
      maybeFlush();
    })();
  };

  function maybeFlush() {
    if (finished || partialsDisabled || inflight) return;
    if (partialsSent >= config.maxCommits - 1) return;
    if (entries.length >= totalJobs) return; // every call settled: the final commit takes them
    const pending = entries.filter(entry => entry.state === "pending");
    if (pending.length === 0) return;
    if (openLanes + 2 > maxConnections) return; // re-checked when a lane closes
    const due = lastFlushAt + config.debounceMs;
    const at = now();
    if (at < due) {
      if (timer === null) {
        timer = setTimer(() => {
          timer = null;
          maybeFlush();
        }, due - at);
      }
      return;
    }
    flush(pending);
  }

  return {
    settle(result) {
      entries.push({
        result, settledAt: now(), state: "pending", releasedAt: null, releasedBy: null,
      });
      maybeFlush();
    },
    laneClosed() {
      openLanes = Math.max(0, openLanes - 1);
      maybeFlush();
    },
    async finish() {
      finished = true;
      if (timer !== null) clearTimer(timer);
      timer = null;
      const batch = entries.filter(entry => entry.state !== "committed");
      let commitOk = true;
      if (batch.length > 0) {
        try {
          await coordinator.completeBatch(bundleId, executionToken, batch.map(entry => entry.result));
        } catch (err) {
          commitOk = false;
          onError(err, { final: true });
        }
      }
      const commitAt = now();
      release(batch, commitAt, "final");
      // After the final, never before it: a partial still in flight cannot delay the commit.
      if (inflight) await inflight;
      return {
        commitOk,
        commitAt,
        partialFlushes: partialsAccepted,
        commits: partialsAccepted + (batch.length > 0 ? 1 : 0),
        partialRefused,
        releases: new Map(entries.map(entry => [entry.result.job_id, {
          releasedAt: entry.releasedAt, releasedBy: entry.releasedBy,
        }])),
      };
    },
  };
}
