/**
 * Durable Object Coordinator for LLM Dispatch v2.
 * Backed by SQLite inside Cloudflare Workers.
 */

import DISPATCH_LIMITS from "./dispatch_limits.json" with { type: "json" };
import { routesEligibleFor } from "./routes.js";
import { computeRouteLaneWait, routeHasCapacityFor, FULL_TOKEN_BUDGET_WINDOWS } from "./pacing.js";

/**
 * index.js's getCoordinator() reaches this class through env.LLM_SCHEDULER.getByName(), the
 * named-Durable-Object RPC binding style. That style requires the class itself to extend the
 * runtime's `DurableObject` base class (from "cloudflare:workers") -- without it, calling any
 * RPC method on the stub throws "The receiving Durable Object does not support RPC, because its
 * class was not declared with `extends DurableObject`" (confirmed against a live incident,
 * 2026-08-18: every enqueueBatch call failed this way from Phase 1's very first deploy, silently,
 * because the DO's own RPC-transport trace still reports outcome "ok" -- the error surfaces only
 * on the calling Worker's side -- and this repo's test suite calls `new LLMSchedulerDO(...)`
 * directly, bypassing the real binding/RPC layer entirely, so it never exercised this).
 *
 * "cloudflare:workers" only exists under the real Workers runtime; this repo's test suite runs
 * under plain Node (`node --test`, using `node:sqlite`) for speed, so import it dynamically and
 * fall back to a no-op base class there. The fallback is never reached in production -- only in
 * tests that construct LLMSchedulerDO directly and call its methods without a real DO binding.
 */
let DurableObjectBase;
try {
  ({ DurableObject: DurableObjectBase } = await import("cloudflare:workers"));
} catch {
  DurableObjectBase = class {};
}

export class LLMSchedulerDO extends DurableObjectBase {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env || {};
    this.sql = ctx?.storage?.sql || ctx?.sql;

    this._initSchema();
  }

  _getSql() {
    if (!this.sql) {
      this.sql = this.ctx?.storage?.sql || this.ctx?.sql;
    }
    return this.sql;
  }

  _initSchema() {
    const sql = this._getSql();
    if (!sql) return;

    sql.exec(`
      CREATE TABLE IF NOT EXISTS jobs (
        id                          TEXT PRIMARY KEY,
        idempotency_key             TEXT NOT NULL UNIQUE,
        request_digest              TEXT NOT NULL,
        provider_idempotency_key    TEXT,
        state                       TEXT NOT NULL CHECK (state IN
                                       ('queued','leased','unknown_attempt','completed','retryable',
                                        'failed','purge_pending')),
        priority                    INTEGER NOT NULL DEFAULT 1 CHECK (priority IN (0,1)),
        policy_json                 TEXT NOT NULL,
        prompt_family               TEXT NOT NULL,
        input_token_estimate        INTEGER NOT NULL,
        max_output_token_estimate   INTEGER NOT NULL,
        payload_key                 TEXT NOT NULL,
        result_key                  TEXT,
        lease_token                 TEXT,
        lease_route_id              TEXT,
        lease_expires_at            INTEGER,
        bundle_id                   TEXT,
        attempts                    INTEGER NOT NULL DEFAULT 0,
        created_at                  INTEGER NOT NULL,
        updated_at                  INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_jobs_state_priority_created
        ON jobs (state, priority, created_at);

      CREATE TABLE IF NOT EXISTS routes (
        route_id                TEXT PRIMARY KEY,
        rpm_window_start        INTEGER NOT NULL DEFAULT 0,
        rpm_count               INTEGER NOT NULL DEFAULT 0,
        rpd_window_start        INTEGER NOT NULL DEFAULT 0,
        rpd_count               INTEGER NOT NULL DEFAULT 0,
        tpm_window_start        INTEGER NOT NULL DEFAULT 0,
        tpm_reserved            INTEGER NOT NULL DEFAULT 0,
        full_token_budget       REAL NOT NULL DEFAULT 0,
        token_budget_updated_at INTEGER NOT NULL DEFAULT 0,
        provisional_reservation INTEGER NOT NULL DEFAULT 0,
        settled_usage           INTEGER NOT NULL DEFAULT 0,
        cost_accumulated         REAL NOT NULL DEFAULT 0,
        throttle_streak          INTEGER NOT NULL DEFAULT 0,
        last_provider_status     INTEGER,
        blocked_until            INTEGER,
        buffer_seconds           REAL NOT NULL DEFAULT 0
      );

      CREATE TABLE IF NOT EXISTS bundles (
        bundle_id            TEXT PRIMARY KEY,
        execution_token      TEXT NOT NULL,
        state                TEXT NOT NULL CHECK (state IN ('active','completed','expired')),
        lease_expires_at     INTEGER NOT NULL,
        active_call_count    INTEGER NOT NULL DEFAULT 0,
        dispatch_window_end  INTEGER NOT NULL,
        created_at           INTEGER NOT NULL
      );

      CREATE TABLE IF NOT EXISTS attempts (
        attempt_id              TEXT PRIMARY KEY,
        job_id                  TEXT NOT NULL,
        route_id                TEXT NOT NULL,
        planned_at              INTEGER NOT NULL,
        actual_start_at         INTEGER,
        actual_end_at           INTEGER,
        observed_input_tokens   INTEGER,
        observed_output_tokens  INTEGER,
        start_state             TEXT NOT NULL CHECK (start_state IN ('planned','started','unknown')),
        outcome                 TEXT CHECK (outcome IN
                                   ('success','retryable_error','terminal_error','deferred_late')),
        provider_status_code    INTEGER,
        gateway_correlation_id  TEXT,
        created_at              INTEGER NOT NULL
      );

      CREATE TABLE IF NOT EXISTS estimates (
        key                     TEXT PRIMARY KEY,
        margin_tokens           INTEGER NOT NULL,
        sample_count            INTEGER NOT NULL DEFAULT 0,
        recent_observed_summary TEXT,
        updated_at              INTEGER NOT NULL
      );

      CREATE TABLE IF NOT EXISTS scheduler (
        id                         INTEGER PRIMARY KEY CHECK (id = 1),
        utc_day                    TEXT NOT NULL,
        bundle_count_today         INTEGER NOT NULL DEFAULT 0,
        jobs_ingested_today        INTEGER NOT NULL DEFAULT 0,
        cleanup_cursor             TEXT,
        next_maintenance_alarm_at  INTEGER
      );
    `);

    // CREATE TABLE IF NOT EXISTS only creates the table on its first-ever run for this DO
    // instance; it does not retroactively add a column introduced later (rpd_window_start/
    // rpd_count, added alongside Phase 2's claimDispatchWindow) to a `routes` table an earlier
    // deploy already created. Defensive, cheap, and a no-op on a fresh instance.
    this._ensureColumn("routes", "rpd_window_start", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("routes", "rpd_count", "INTEGER NOT NULL DEFAULT 0");

    const today = new Date().toISOString().slice(0, 10);
    const existing = [...sql.exec("SELECT id FROM scheduler WHERE id = 1")];
    if (existing.length === 0) {
      sql.exec(
        `INSERT INTO scheduler (id, utc_day, bundle_count_today, jobs_ingested_today)
         VALUES (1, ?, 0, 0)`,
        today
      );
    }
  }

  _ensureColumn(table, column, definition) {
    const sql = this._getSql();
    const columns = [...sql.exec(`PRAGMA table_info(${table})`)];
    if (columns.some((c) => c.name === column)) return;
    sql.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
  }

  _currentUtcDay(now = Date.now()) {
    return new Date(now).toISOString().slice(0, 10);
  }

  _maxJobsPerUtcDay() {
    const configured = Number(this.env.MAX_JOBS_PER_UTC_DAY);
    return Number.isFinite(configured) && configured > 0 ? configured : 20000;
  }

  _envInt(name, fallback) {
    const configured = Number(this.env[name]);
    return Number.isFinite(configured) && configured >= 0 ? configured : fallback;
  }

  _maxBundleJobs() {
    return this._envInt("MAX_BUNDLE_JOBS", 4);
  }

  _maxJobsPerRoutePerBundle() {
    return this._envInt("MAX_JOBS_PER_ROUTE_PER_BUNDLE", 4);
  }

  _maxConcurrentRouteLanes() {
    return this._envInt("MAX_CONCURRENT_ROUTE_LANES", 5);
  }

  _maxActiveBundles() {
    return this._envInt("MAX_ACTIVE_BUNDLES", 2);
  }

  _maxInFlightLlmCalls() {
    return this._envInt("MAX_IN_FLIGHT_LLM_CALLS", 8);
  }

  _maxBundlesPerUtcDay() {
    return this._envInt("MAX_BUNDLES_PER_UTC_DAY", 1000);
  }

  _maxQueueWaitSeconds() {
    return this._envInt("MAX_QUEUE_WAIT_SECONDS", 3600);
  }

  _leaseDurationMs() {
    return this._envInt("LEASE_DURATION_SECONDS", 840) * 1000;
  }

  /** Conservative upper bound on how long one provider call might take, so a job whose safe
   * start technically fits before the window's end but would still plausibly still be running
   * past it is excluded during admission rather than discovered as deferred_late later. */
  _callDurationCeilingMs() {
    return this._envInt("ESTIMATED_CALL_DURATION_CEILING_SECONDS", 20) * 1000;
  }

  _estimateFloor() {
    return this._envInt("ESTIMATE_MARGIN", 0);
  }

  _max429Retries() {
    return this._envInt("MAX_429_RETRIES", 1);
  }

  _max429BackoffMs() {
    return this._envInt("MAX_429_BACKOFF_SECONDS", 60) * 1000;
  }

  _maxRouteBufferSeconds() {
    return this._envInt("MAX_ROUTE_BUFFER_SECONDS", 120);
  }

  /** The real compiled catalog, unless a test has injected its own via the (object-valued, so
   * never confusable with a real Cloudflare string env var) env.DISPATCH_LIMITS_OVERRIDE. */
  _dispatchLimits() {
    return this.env.DISPATCH_LIMITS_OVERRIDE || DISPATCH_LIMITS;
  }

  // Cloudflare's SQLite-backed Durable Object storage caps bound parameters per query at 100
  // (https://developers.cloudflare.com/durable-objects/platform/limits/) -- chunk any query whose
  // parameter count scales with caller-supplied batch size (e.g. POLL_BATCH_MAX up to 1000) into
  // multiple queries of at most this many ids each.
  static MAX_SQL_BOUND_PARAMS = 100;

  *_chunks(items, size = LLMSchedulerDO.MAX_SQL_BOUND_PARAMS) {
    for (let offset = 0; offset < items.length; offset += size) {
      yield items.slice(offset, offset + size);
    }
  }

  /**
   * Roll scheduler.utc_day forward and zero both daily counters if the stored day differs from
   * `now`'s UTC day (including a DO idle for more than one day, which still rolls forward exactly
   * once here, not once per elapsed day -- both counters only ever mean "since this stored day
   * started"). Shared by enqueueBatch and claimDispatchWindow so both counters -- and every
   * caller relying on scheduler's row -- see one consistent rollover implementation, not two
   * independently-maintained copies. Must be called inside the RPC's own transactionSync.
   * Returns the current (possibly just-reset) scheduler row.
   */
  _rollUtcDayIfNeeded(now) {
    const sql = this._getSql();
    const today = this._currentUtcDay(now);
    const rows = [...sql.exec("SELECT utc_day, bundle_count_today, jobs_ingested_today FROM scheduler WHERE id = 1")];
    if (rows.length === 0) {
      sql.exec(
        "INSERT INTO scheduler (id, utc_day, bundle_count_today, jobs_ingested_today) VALUES (1, ?, 0, 0)",
        today
      );
      return { utc_day: today, bundle_count_today: 0, jobs_ingested_today: 0 };
    }
    const sched = rows[0];
    if (sched.utc_day !== today) {
      sql.exec(
        "UPDATE scheduler SET utc_day = ?, bundle_count_today = 0, jobs_ingested_today = 0 WHERE id = 1",
        today
      );
      return { utc_day: today, bundle_count_today: 0, jobs_ingested_today: 0 };
    }
    return sched;
  }

  async enqueueBatch(jobs) {
    const sql = this._getSql();
    const now = Date.now();
    const maxJobsToday = this._maxJobsPerUtcDay();

    // The whole batch commits or rolls back as one unit -- see review/44 Unit 2 ("one SQLite
    // transaction for the whole batch"). ctx.storage.transactionSync requires its callback to
    // run fully synchronously (no await inside), which this loop already does.
    return this.ctx.storage.transactionSync(() => {
      const accepted = [];
      const rejected = [];

      const sched = this._rollUtcDayIfNeeded(now);
      let jobsIngestedToday = sched.jobs_ingested_today;

      let newlyInsertedCount = 0;

      for (const job of jobs) {
        // Check idempotency_key FIRST, before the daily cap. A replay of an already-accepted
        // job must still succeed once the cap is reached that day -- it doesn't consume new
        // admission capacity, and rejecting it as daily_cap_exceeded would orphan the caller's
        // retry of a job that was, in fact, already accepted.
        const existing = [...sql.exec(
          "SELECT id, request_digest, state FROM jobs WHERE idempotency_key = ?",
          job.idempotency_key
        )];

        if (existing.length > 0) {
          const row = existing[0];
          if (row.request_digest === job.request_digest) {
            // Idempotent replay: return the existing row's canonical id, tagged with the
            // caller's own submitted id so the caller can always match this response back to
            // its own request even when the canonical id differs (e.g. a retry that generated
            // a fresh id locally before learning the original was already accepted).
            accepted.push({ id: row.id, submitted_id: job.id });
          } else {
            // Reused key with different payload: fail loudly
            rejected.push({ id: job.id, reason: "idempotency_conflict" });
          }
          continue;
        }

        // Only a genuinely new job consumes daily admission capacity.
        if (jobsIngestedToday + newlyInsertedCount >= maxJobsToday) {
          rejected.push({ id: job.id, reason: "daily_cap_exceeded" });
          continue;
        }

        // Insert new job
        const priority = job.priority !== undefined ? job.priority : 1;
        const policyJson = typeof job.policy_json === "string" ? job.policy_json : JSON.stringify(job.policy_json || {});
        const providerIdempotencyKey = job.provider_idempotency_key || null;

        sql.exec(
          `INSERT INTO jobs (
            id, idempotency_key, request_digest, provider_idempotency_key,
            state, priority, policy_json, prompt_family,
            input_token_estimate, max_output_token_estimate,
            payload_key, attempts, created_at, updated_at
          ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, 0, ?, ?)`,
          job.id,
          job.idempotency_key,
          job.request_digest,
          providerIdempotencyKey,
          priority,
          policyJson,
          job.prompt_family,
          job.input_token_estimate,
          job.max_output_token_estimate,
          job.payload_key,
          now,
          now
        );

        newlyInsertedCount++;
        accepted.push({ id: job.id, submitted_id: job.id });
      }

      if (newlyInsertedCount > 0) {
        sql.exec(
          "UPDATE scheduler SET jobs_ingested_today = jobs_ingested_today + ? WHERE id = 1",
          newlyInsertedCount
        );
      }

      return { accepted, rejected };
    });
  }

  async pollBatch(ids) {
    if (!ids || ids.length === 0) {
      return { statuses: [] };
    }

    const sql = this._getSql();
    const statuses = [];
    for (const chunk of this._chunks(ids)) {
      const placeholders = chunk.map(() => "?").join(",");
      const rows = [...sql.exec(
        `SELECT id, state, result_key, attempts FROM jobs WHERE id IN (${placeholders})`,
        ...chunk
      )];
      for (const row of rows) {
        statuses.push({
          id: row.id,
          state: row.state,
          result_key: row.state === "completed" ? row.result_key : null,
          error: row.state === "failed" ? "job_failed" : null,
          attempts: row.attempts,
        });
      }
    }

    return { statuses };
  }

  async resolveUnknownBatch(attemptIds) {
    if (!attemptIds || attemptIds.length === 0) {
      return { resolved: [], not_found: [] };
    }
    const sql = this._getSql();
    const foundIds = new Set();
    for (const chunk of this._chunks(attemptIds)) {
      const placeholders = chunk.map(() => "?").join(",");
      const rows = [...sql.exec(
        `SELECT attempt_id FROM attempts WHERE attempt_id IN (${placeholders})`,
        ...chunk
      )];
      for (const row of rows) {
        foundIds.add(row.attempt_id);
      }
    }
    const resolved = [];
    const not_found = [];
    for (const attemptId of attemptIds) {
      (foundIds.has(attemptId) ? resolved : not_found).push(attemptId);
    }
    return { resolved, not_found };
  }

  // ---------------------------------------------------------------------------------------
  // Phase 2: route ledger, admission/pacing (Unit 4), attempt fencing and retry authorization,
  // completion settlement and calibration (Units 5 and 7).
  // ---------------------------------------------------------------------------------------

  /** Read a route's live ledger row, seeding a fresh one (full token budget, no prior usage) the
   * first time this route is ever touched. `catalogRoute` supplies the static tpm limit used to
   * size the seed budget; it is not persisted (the catalog itself is not per-DO state). */
  _getOrCreateRouteLedger(routeId, now, catalogRoute) {
    const sql = this._getSql();
    const rows = [...sql.exec("SELECT * FROM routes WHERE route_id = ?", routeId)];
    if (rows.length > 0) return rows[0];
    const tpm = Number(catalogRoute?.tpm) || 0;
    const seedBudget = tpm * FULL_TOKEN_BUDGET_WINDOWS;
    sql.exec(
      `INSERT INTO routes (
        route_id, rpm_window_start, rpm_count, rpd_window_start, rpd_count,
        tpm_window_start, tpm_reserved, full_token_budget, token_budget_updated_at,
        provisional_reservation, settled_usage, cost_accumulated,
        throttle_streak, last_provider_status, blocked_until, buffer_seconds
      ) VALUES (?, 0, 0, 0, 0, 0, 0, ?, ?, 0, 0, 0, 0, NULL, NULL, 0)`,
      routeId,
      seedBudget,
      now
    );
    return {
      route_id: routeId,
      rpm_window_start: 0,
      rpm_count: 0,
      rpd_window_start: 0,
      rpd_count: 0,
      tpm_window_start: 0,
      tpm_reserved: 0,
      full_token_budget: seedBudget,
      token_budget_updated_at: now,
      provisional_reservation: 0,
      settled_usage: 0,
      cost_accumulated: 0,
      throttle_streak: 0,
      last_provider_status: null,
      blocked_until: null,
      buffer_seconds: 0,
    };
  }

  /**
   * Advance a route's ledger forward to account for one newly-admitted reservation taking effect
   * at `notBeforeAt`, mirroring pacing.js's earliestSafeStart read-side logic on the write side.
   * Persists to SQLite and returns the updated merged route object so the caller's in-memory
   * lane-sequencing state (and its ledger cache) stays consistent with what was just written,
   * without a redundant read back from SQL.
   */
  _applyProvisionalReservation(mergedRoute, reservation, notBeforeAt) {
    const sql = this._getSql();
    const tpm = Number(mergedRoute.tpm) || 0;

    let rpmWindowStart = mergedRoute.rpm_window_start;
    let rpmCount = mergedRoute.rpm_count;
    if (!Number.isFinite(rpmWindowStart) || notBeforeAt - rpmWindowStart >= 60_000) {
      rpmWindowStart = notBeforeAt;
      rpmCount = 1;
    } else {
      rpmCount += 1;
    }

    let rpdWindowStart = mergedRoute.rpd_window_start;
    let rpdCount = mergedRoute.rpd_count;
    if (!Number.isFinite(rpdWindowStart) || notBeforeAt - rpdWindowStart >= 86_400_000) {
      rpdWindowStart = notBeforeAt;
      rpdCount = 1;
    } else {
      rpdCount += 1;
    }

    // Only a reservation that fits within one window's tpm allowance participates in the rolling
    // per-window check -- an oversized reservation is gated by the token bucket alone (below).
    let tpmWindowStart = mergedRoute.tpm_window_start;
    let tpmReserved = mergedRoute.tpm_reserved;
    if (tpm > 0 && reservation <= tpm) {
      if (!Number.isFinite(tpmWindowStart) || notBeforeAt - tpmWindowStart >= 60_000) {
        tpmWindowStart = notBeforeAt;
        tpmReserved = reservation;
      } else {
        tpmReserved += reservation;
      }
    }

    let fullTokenBudget = mergedRoute.full_token_budget;
    let tokenBudgetUpdatedAt = mergedRoute.token_budget_updated_at;
    if (tpm > 0) {
      const elapsedMs = Math.max(0, notBeforeAt - (tokenBudgetUpdatedAt || 0));
      const refilled = Math.min(
        tpm * FULL_TOKEN_BUDGET_WINDOWS,
        (fullTokenBudget || 0) + (elapsedMs * tpm) / 60_000
      );
      fullTokenBudget = Math.max(0, refilled - reservation);
      tokenBudgetUpdatedAt = notBeforeAt;
    }

    const provisionalReservation = (mergedRoute.provisional_reservation || 0) + reservation;

    sql.exec(
      `UPDATE routes SET
        rpm_window_start=?, rpm_count=?, rpd_window_start=?, rpd_count=?,
        tpm_window_start=?, tpm_reserved=?, full_token_budget=?, token_budget_updated_at=?,
        provisional_reservation=?
       WHERE route_id=?`,
      rpmWindowStart,
      rpmCount,
      rpdWindowStart,
      rpdCount,
      tpmWindowStart,
      tpmReserved,
      fullTokenBudget,
      tokenBudgetUpdatedAt,
      provisionalReservation,
      mergedRoute.route_id
    );

    return {
      ...mergedRoute,
      rpm_window_start: rpmWindowStart,
      rpm_count: rpmCount,
      rpd_window_start: rpdWindowStart,
      rpd_count: rpdCount,
      tpm_window_start: tpmWindowStart,
      tpm_reserved: tpmReserved,
      full_token_budget: fullTokenBudget,
      token_budget_updated_at: tokenBudgetUpdatedAt,
      provisional_reservation: provisionalReservation,
    };
  }

  _calibratedMargin(routeId, model, promptFamily) {
    const sql = this._getSql();
    const key = `${routeId}:${model}:${promptFamily}`;
    const rows = [...sql.exec("SELECT margin_tokens FROM estimates WHERE key = ?", key)];
    return rows.length > 0 ? rows[0].margin_tokens : 0;
  }

  /** Aged-first, then larger-conservative-estimate-first, then FIFO -- see Unit 4 pass 2. */
  _orderByAgingThenSize(jobs, now, maxQueueWaitSeconds) {
    const agedThresholdMs = maxQueueWaitSeconds * 1000;
    const aged = [];
    const notAged = [];
    for (const job of jobs) {
      const waitedMs = now - job.created_at;
      (waitedMs >= agedThresholdMs ? aged : notAged).push(job);
    }
    const bySizeThenFifo = (a, b) => {
      const sizeA = (a.input_token_estimate || 0) + (a.max_output_token_estimate || 0);
      const sizeB = (b.input_token_estimate || 0) + (b.max_output_token_estimate || 0);
      if (sizeB !== sizeA) return sizeB - sizeA;
      return a.created_at - b.created_at;
    };
    aged.sort(bySizeThenFifo);
    notAged.sort(bySizeThenFifo);
    return [...aged, ...notAged];
  }

  static EMPTY_CLAIM_RESULT = { bundle_id: null, execution_token: null, jobs: [] };

  /** review/44 Unit 4: fenced admission, ordering, and pacing. One SQLite transaction for the
   * whole call -- see the unit's own pseudocode for the exact two-pass selection this mirrors. */
  async claimDispatchWindow(now, windowSeconds) {
    const sql = this._getSql();
    const dispatchLimits = this._dispatchLimits();

    return this.ctx.storage.transactionSync(() => {
      const maxBundlesPerDay = this._maxBundlesPerUtcDay();
      const maxActiveBundles = this._maxActiveBundles();
      const maxInFlightCalls = this._maxInFlightLlmCalls();
      const maxBundleJobs = this._maxBundleJobs();
      const maxConcurrentLanes = this._maxConcurrentRouteLanes();
      const maxJobsPerRoutePerBundle = this._maxJobsPerRoutePerBundle();
      const maxQueueWaitSeconds = this._maxQueueWaitSeconds();
      const estimateFloor = this._estimateFloor();
      const callDurationCeilingMs = this._callDurationCeilingMs();
      const leaseDurationMs = this._leaseDurationMs();
      const EMPTY = LLMSchedulerDO.EMPTY_CLAIM_RESULT;

      const sched = this._rollUtcDayIfNeeded(now);
      if (sched.bundle_count_today >= maxBundlesPerDay) return EMPTY;

      const activeBundles = [...sql.exec("SELECT active_call_count FROM bundles WHERE state='active'")];
      if (activeBundles.length >= maxActiveBundles) return EMPTY;
      const inFlightCalls = activeBundles.reduce((sum, b) => sum + b.active_call_count, 0);
      if (inFlightCalls >= maxInFlightCalls) return EMPTY;

      const candidates = [...sql.exec(
        "SELECT * FROM jobs WHERE state='queued' ORDER BY priority ASC, created_at ASC LIMIT ?",
        maxBundleJobs * 4
      )];
      if (candidates.length === 0) return EMPTY;

      const ledgerCache = new Map();
      const getMergedRoute = (route) => {
        if (!ledgerCache.has(route.route_id)) {
          const ledger = this._getOrCreateRouteLedger(route.route_id, now, route);
          ledgerCache.set(route.route_id, { ...route, ...ledger });
        }
        return ledgerCache.get(route.route_id);
      };
      const capacityOptions = (route, job) => ({
        estimateFloor,
        calibratedMargin: this._calibratedMargin(route.route_id, route.model, job.prompt_family),
        callDurationCeilingMs,
      });

      const chosen = []; // { job, route }
      const seenRoutes = new Set();

      // Pass 1: distinct eligible routes, capped at maxConcurrentLanes lanes total.
      for (const job of candidates) {
        if (chosen.length >= maxBundleJobs) break;
        if (seenRoutes.size >= maxConcurrentLanes) break;
        for (const route of routesEligibleFor(job, dispatchLimits)) {
          if (seenRoutes.has(route.route_id)) continue;
          const merged = getMergedRoute(route);
          if (!routeHasCapacityFor(merged, job, now, windowSeconds, capacityOptions(route, job))) continue;
          chosen.push({ job, route });
          seenRoutes.add(route.route_id);
          break;
        }
      }

      // Pass 2: aged-then-larger-then-FIFO fill, may still open a new lane under the cap, or add
      // to an already-open one up to maxJobsPerRoutePerBundle.
      const chosenJobIds = new Set(chosen.map((c) => c.job.id));
      const remaining = candidates.filter((c) => !chosenJobIds.has(c.id));
      const ordered = this._orderByAgingThenSize(remaining, now, maxQueueWaitSeconds);
      for (const job of ordered) {
        if (chosen.length >= maxBundleJobs) break;
        for (const route of routesEligibleFor(job, dispatchLimits)) {
          const isNewLane = !seenRoutes.has(route.route_id);
          if (isNewLane && seenRoutes.size >= maxConcurrentLanes) continue;
          const countInBundle = chosen.filter((c) => c.route.route_id === route.route_id).length;
          if (countInBundle >= maxJobsPerRoutePerBundle) continue;
          const merged = getMergedRoute(route);
          if (!routeHasCapacityFor(merged, job, now, windowSeconds, capacityOptions(route, job))) continue;
          chosen.push({ job, route });
          seenRoutes.add(route.route_id);
          break;
        }
      }

      if (chosen.length === 0) return EMPTY;

      // Sequence each route lane independently, in selection order. A job chosen above can still
      // fall out here if an earlier job in the SAME lane pushed the lane's cumulative time past
      // the window deadline -- the eligibility passes above checked each route independently, not
      // lane-sequenced; this final pass is the authoritative one (Unit 4 step 4/6).
      const byRoute = new Map();
      for (const { job, route } of chosen) {
        if (!byRoute.has(route.route_id)) byRoute.set(route.route_id, { route, jobs: [] });
        byRoute.get(route.route_id).jobs.push(job);
      }

      const bundleId = crypto.randomUUID();
      const leaseExpiresAt = now + leaseDurationMs;
      const dispatchWindowEnd = now + windowSeconds * 1000;
      const resultJobs = [];

      for (const { route, jobs: routeJobs } of byRoute.values()) {
        let laneTime = now;
        let workingRoute = getMergedRoute(route);
        for (const job of routeJobs) {
          const margin = this._calibratedMargin(route.route_id, route.model, job.prompt_family);
          const waitResult = computeRouteLaneWait(workingRoute, job, laneTime, now, {
            estimateFloor,
            calibratedMargin: margin,
          });
          if (waitResult === null) continue; // exceeds this route's burst capacity outright
          if (waitResult.not_before_at + callDurationCeilingMs > dispatchWindowEnd) continue;

          const leaseToken = crypto.randomUUID();
          sql.exec(
            `UPDATE jobs SET state='leased', lease_token=?, lease_route_id=?, lease_expires_at=?,
                              bundle_id=?, updated_at=? WHERE id=?`,
            leaseToken,
            route.route_id,
            leaseExpiresAt,
            bundleId,
            now,
            job.id
          );

          workingRoute = this._applyProvisionalReservation(workingRoute, waitResult.reservation, waitResult.not_before_at);
          ledgerCache.set(route.route_id, workingRoute);

          resultJobs.push({
            id: job.id,
            payload_key: job.payload_key,
            lease_token: leaseToken,
            route_id: route.route_id,
            token_reservation: waitResult.reservation,
            wait_ms: waitResult.wait_ms,
            not_before_at: waitResult.not_before_at,
            min_inter_request_gap_ms: waitResult.min_inter_request_gap_ms,
          });
          laneTime = waitResult.not_before_at + waitResult.min_inter_request_gap_ms;
        }
      }

      if (resultJobs.length === 0) return EMPTY;

      const executionToken = crypto.randomUUID();
      sql.exec(
        `INSERT INTO bundles (bundle_id, execution_token, state, lease_expires_at, active_call_count,
                               dispatch_window_end, created_at)
         VALUES (?, ?, 'active', ?, ?, ?, ?)`,
        bundleId,
        executionToken,
        leaseExpiresAt,
        resultJobs.length,
        dispatchWindowEnd,
        now
      );
      sql.exec("UPDATE scheduler SET bundle_count_today = bundle_count_today + 1 WHERE id = 1");

      return { bundle_id: bundleId, execution_token: executionToken, jobs: resultJobs };
    });
  }

  /**
   * Fence and persist an attempt record before the executor sends bytes to a provider that
   * doesn't support a stable provider-side idempotency key (see "Claim, ordering, pacing, and
   * execution flow" step 7). Returns { fenced: false } if this lease is no longer current (e.g.
   * reaped by a lease-expiry sweep after a slow/hung previous tick) -- the executor must not
   * proceed with the provider call in that case.
   */
  async attemptStarted(jobId, leaseToken, attemptId, now) {
    const sql = this._getSql();
    return this.ctx.storage.transactionSync(() => {
      const rows = [...sql.exec(
        "SELECT lease_token, lease_route_id, state FROM jobs WHERE id = ?",
        jobId
      )];
      if (rows.length === 0 || rows[0].lease_token !== leaseToken || rows[0].state !== "leased") {
        return { fenced: false };
      }
      sql.exec(
        `INSERT INTO attempts (attempt_id, job_id, route_id, planned_at, start_state, created_at)
         VALUES (?, ?, ?, ?, 'started', ?)`,
        attemptId,
        jobId,
        rows[0].lease_route_id,
        now,
        now
      );
      sql.exec("UPDATE jobs SET attempts = attempts + 1, updated_at = ? WHERE id = ?", now, jobId);
      return { fenced: true };
    });
  }

  /**
   * "Timeout and 429 behavior": a first 429 asks for authorization before any retry. Bounded by
   * MAX_429_RETRIES (via the attempts already recorded for this job -- see attemptStarted; a
   * future provider-idempotent route that skips attemptStarted would need its own counter, since
   * none exists yet this is not implemented) and by the bundle's own deadline -- an authorized
   * retry that wouldn't fit before the dispatch window or lease expires is declined, not granted
   * late.
   */
  async authorizeRetry(jobId, leaseToken, attemptId, now) {
    const sql = this._getSql();
    return this.ctx.storage.transactionSync(() => {
      const jobRows = [...sql.exec(
        "SELECT lease_token, lease_route_id, bundle_id, state, attempts FROM jobs WHERE id = ?",
        jobId
      )];
      if (jobRows.length === 0 || jobRows[0].lease_token !== leaseToken || jobRows[0].state !== "leased") {
        return { authorized: false, retry_not_before: null };
      }
      const job = jobRows[0];

      if (job.attempts > this._max429Retries()) {
        return { authorized: false, retry_not_before: null };
      }

      const bundleRows = [...sql.exec(
        "SELECT dispatch_window_end, lease_expires_at FROM bundles WHERE bundle_id = ?",
        job.bundle_id
      )];
      if (bundleRows.length === 0) {
        return { authorized: false, retry_not_before: null };
      }
      const bundle = bundleRows[0];

      const routeId = job.lease_route_id;
      const ledger = this._getOrCreateRouteLedger(routeId, now, {});
      const newStreak = (ledger.throttle_streak || 0) + 1;
      const maxBufferSeconds = this._maxRouteBufferSeconds();
      const addedBufferSeconds = this._max429BackoffMs() / 1000;
      const newBufferSeconds = Math.min(maxBufferSeconds, (ledger.buffer_seconds || 0) + addedBufferSeconds);
      sql.exec(
        "UPDATE routes SET throttle_streak = ?, last_provider_status = 429, buffer_seconds = ? WHERE route_id = ?",
        newStreak,
        newBufferSeconds,
        routeId
      );

      const backoffMs = Math.min(this._max429BackoffMs(), newStreak * 1000);
      const retryNotBefore = now + backoffMs;
      const deadline = Math.min(bundle.dispatch_window_end, bundle.lease_expires_at);
      if (retryNotBefore >= deadline) {
        return { authorized: false, retry_not_before: null };
      }

      return { authorized: true, retry_not_before: retryNotBefore };
    });
  }

  /**
   * review/44 Unit 5 + Unit 7 (calibration folded in). Stale (bundle not found, or a mismatched
   * execution_token from a superseded/expired bundle) completions are silently ignored -- see
   * "Timeout and 429 behavior" on why a late executor must never be allowed to settle a
   * lease/bundle another attempt already reaped.
   */
  async completeBatch(bundleId, executionToken, results) {
    const sql = this._getSql();
    return this.ctx.storage.transactionSync(() => {
      const bundleRows = [...sql.exec(
        "SELECT execution_token FROM bundles WHERE bundle_id = ?",
        bundleId
      )];
      if (bundleRows.length === 0 || bundleRows[0].execution_token !== executionToken) {
        return; // stale completion; no-op
      }

      const now = Date.now();
      let settledCount = 0;

      for (const result of results || []) {
        // Look up the job first (a plain read, no side effects) so the attempts insert below can
        // use its already-fetched lease_route_id directly -- a route_id derived from a
        // `(SELECT ... FROM jobs WHERE id=?)` subquery evaluates to NULL for a bogus/unknown
        // job_id, which would throw on attempts.route_id's NOT NULL constraint and abort the
        // whole batch's completion, not just skip this one stale/unrecognized result.
        const jobRows = [...sql.exec(
          "SELECT lease_token, state, lease_route_id, prompt_family FROM jobs WHERE id = ?",
          result.job_id
        )];
        const routeIdForAttempt = jobRows.length > 0 ? jobRows[0].lease_route_id : "unknown";

        // attemptStarted (fencing, before the provider call -- see that method) already inserted
        // this exact attempt_id for every non-provider-idempotent route, which is every route
        // today. UPSERT rather than INSERT: fill in the terminal fields on that existing row when
        // it's already there, or insert fresh for the (currently unreachable, but still-correct)
        // provider-idempotent path that never called attemptStarted at all.
        sql.exec(
          `INSERT INTO attempts (
            attempt_id, job_id, route_id, planned_at, actual_start_at, actual_end_at,
            observed_input_tokens, observed_output_tokens, start_state, outcome,
            provider_status_code, gateway_correlation_id, created_at
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(attempt_id) DO UPDATE SET
            planned_at = excluded.planned_at,
            actual_start_at = excluded.actual_start_at,
            actual_end_at = excluded.actual_end_at,
            observed_input_tokens = excluded.observed_input_tokens,
            observed_output_tokens = excluded.observed_output_tokens,
            start_state = excluded.start_state,
            outcome = excluded.outcome,
            provider_status_code = excluded.provider_status_code,
            gateway_correlation_id = excluded.gateway_correlation_id`,
          result.attempt_id,
          result.job_id,
          routeIdForAttempt || "unknown",
          result.planned_at ?? null,
          result.actual_start_at ?? null,
          result.actual_end_at ?? null,
          result.observed_input_tokens ?? null,
          result.observed_output_tokens ?? null,
          result.actual_start_at != null ? "started" : "planned",
          result.outcome,
          result.provider_status_code ?? null,
          result.gateway_correlation_id ?? null,
          now
        );

        if (jobRows.length === 0 || jobRows[0].lease_token !== result.lease_token) {
          continue; // stale/duplicate completion for an already-settled job
        }
        const job = jobRows[0];

        let newState;
        switch (result.outcome) {
          case "success":
            newState = "completed";
            break;
          case "retryable_error":
            newState = "retryable";
            break;
          case "terminal_error":
            newState = "failed";
            break;
          case "deferred_late":
            newState = "queued";
            break;
          default:
            continue; // unrecognized outcome; leave the job's state untouched
        }

        if (result.outcome === "deferred_late") {
          sql.exec(
            `UPDATE jobs SET state='queued', lease_token=NULL, lease_route_id=NULL,
                              lease_expires_at=NULL, bundle_id=NULL, updated_at=? WHERE id=?`,
            now,
            result.job_id
          );
        } else if (result.outcome === "success") {
          sql.exec(
            "UPDATE jobs SET state=?, result_key=?, updated_at=? WHERE id=?",
            newState,
            result.result_key ?? null,
            now,
            result.job_id
          );
        } else {
          sql.exec("UPDATE jobs SET state=?, updated_at=? WHERE id=?", newState, now, result.job_id);
        }
        settledCount += 1;

        // Settle the route's provisional reservation and decay its throttle state on success.
        // Never touch rpm/rpd/tpm-window/full-token-budget bookkeeping here: those were already
        // advanced forward at claim time to the reservation's planned not_before_at, and
        // under/over-consumption doesn't change WHEN that capacity was consumed, only how much --
        // adjusting them retroactively risks pulling a future reservation forward, which Unit 5
        // explicitly forbids.
        if (job.lease_route_id) {
          const observedTotal =
            result.observed_input_tokens != null && result.observed_output_tokens != null
              ? result.observed_input_tokens + result.observed_output_tokens
              : null;
          const reservation = result.token_reservation ?? observedTotal ?? 0;
          const settledUsage = observedTotal ?? reservation;
          sql.exec(
            `UPDATE routes SET
               provisional_reservation = MAX(0, provisional_reservation - ?),
               settled_usage = settled_usage + ?
             WHERE route_id = ?`,
            reservation,
            settledUsage,
            job.lease_route_id
          );

          if (result.outcome === "success") {
            sql.exec(
              "UPDATE routes SET throttle_streak = 0, buffer_seconds = 0, last_provider_status = ? WHERE route_id = ?",
              result.provider_status_code ?? 200,
              job.lease_route_id
            );
          } else if (result.provider_status_code != null) {
            sql.exec(
              "UPDATE routes SET last_provider_status = ? WHERE route_id = ?",
              result.provider_status_code,
              job.lease_route_id
            );
          }

          // Unit 7 calibration: only ever raises the recorded margin, never lowers it.
          if (observedTotal != null) {
            this._calibrateEstimate(job.lease_route_id, job.prompt_family, observedTotal, now);
          }
        }
      }

      if (settledCount > 0) {
        const remainingLeased = [...sql.exec(
          "SELECT COUNT(*) as n FROM jobs WHERE bundle_id = ? AND state = 'leased'",
          bundleId
        )];
        if ((remainingLeased[0]?.n || 0) === 0) {
          sql.exec("UPDATE bundles SET state = 'completed' WHERE bundle_id = ?", bundleId);
        }
      }
    });
  }

  /** Unit 7: never decrease an existing margin; insert the configured floor as the first
   * observation for a route:model:prompt_family key. `model` is looked up from the route's own
   * lease, since jobs does not itself store the resolved model name. */
  _calibrateEstimate(routeId, promptFamily, observedTotal, now) {
    const sql = this._getSql();
    const model = this._modelForRoute(routeId);
    const key = `${routeId}:${model}:${promptFamily}`;
    const existing = [...sql.exec("SELECT margin_tokens, sample_count FROM estimates WHERE key = ?", key)];
    if (existing.length === 0) {
      sql.exec(
        `INSERT INTO estimates (key, margin_tokens, sample_count, recent_observed_summary, updated_at)
         VALUES (?, ?, 1, ?, ?)`,
        key,
        Math.max(observedTotal, this._estimateFloor()),
        JSON.stringify([observedTotal]),
        now
      );
      return;
    }
    const current = existing[0];
    if (observedTotal > current.margin_tokens) {
      sql.exec(
        "UPDATE estimates SET margin_tokens = ?, sample_count = sample_count + 1, updated_at = ? WHERE key = ?",
        observedTotal,
        now,
        key
      );
    } else {
      sql.exec(
        "UPDATE estimates SET sample_count = sample_count + 1, updated_at = ? WHERE key = ?",
        now,
        key
      );
    }
  }

  _modelForRoute(routeId) {
    const catalog = this._dispatchLimits();
    return catalog?.routes_by_id?.[routeId]?.model || catalog?.routes_by_id?.[routeId]?.upstream_model || routeId;
  }

  // ---------------------------------------------------------------------------------------
  // B2-only payload storage and bounded cleanup.
  // ---------------------------------------------------------------------------------------

  /** At most `limit` terminal (completed/failed) jobs older than COMPLETED_RETENTION_DAYS,
   * transitioned to purge_pending so the caller (executor Worker, which already holds B2
   * credentials) can delete their B2 payload/result keys and then confirm via confirmPurge. Never
   * performs an unbounded scan -- bounded by `limit`, same discipline as pollBatch's chunking. */
  async purgePendingBatch(limit) {
    const sql = this._getSql();
    const retentionDays = this._envInt("COMPLETED_RETENTION_DAYS", 38);
    const cutoff = Date.now() - retentionDays * 86_400_000;
    return this.ctx.storage.transactionSync(() => {
      const rows = [...sql.exec(
        `SELECT id, payload_key, result_key FROM jobs
         WHERE state IN ('completed', 'failed') AND updated_at < ?
         ORDER BY updated_at ASC LIMIT ?`,
        cutoff,
        limit
      )];
      if (rows.length === 0) return { jobs: [] };
      const ids = rows.map((r) => r.id);
      for (const chunk of this._chunks(ids)) {
        const placeholders = chunk.map(() => "?").join(",");
        sql.exec(`UPDATE jobs SET state='purge_pending' WHERE id IN (${placeholders})`, ...chunk);
      }
      return {
        jobs: rows.map((r) => ({ id: r.id, payload_key: r.payload_key, result_key: r.result_key })),
      };
    });
  }

  /** Removes the SQLite rows for jobs whose B2 keys the caller has already deleted. Idempotent: a
   * crash after the B2 deletes but before this call merely repeats it on the next cleanup run. */
  async confirmPurge(jobIds) {
    if (!jobIds || jobIds.length === 0) return { purged: 0 };
    const sql = this._getSql();
    return this.ctx.storage.transactionSync(() => {
      let purged = 0;
      for (const chunk of this._chunks(jobIds)) {
        const placeholders = chunk.map(() => "?").join(",");
        sql.exec(
          `DELETE FROM jobs WHERE id IN (${placeholders}) AND state = 'purge_pending'`,
          ...chunk
        );
        purged += chunk.length;
      }
      return { purged };
    });
  }

  /** For the orphan sweep: which of these preassigned job ids were never actually accepted by
   * enqueueBatch. The executor deletes an orphaned B2 payload key only after confirming this. */
  async confirmNeverAccepted(jobIds) {
    if (!jobIds || jobIds.length === 0) return { neverAccepted: [] };
    const sql = this._getSql();
    const existingIds = new Set();
    for (const chunk of this._chunks(jobIds)) {
      const placeholders = chunk.map(() => "?").join(",");
      const rows = [...sql.exec(`SELECT id FROM jobs WHERE id IN (${placeholders})`, ...chunk)];
      for (const row of rows) existingIds.add(row.id);
    }
    return { neverAccepted: jobIds.filter((id) => !existingIds.has(id)) };
  }
}
