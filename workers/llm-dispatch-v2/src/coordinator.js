/**
 * Durable Object Coordinator for LLM Dispatch v2.
 * Backed by SQLite inside Cloudflare Workers.
 */

import DISPATCH_LIMITS from "./dispatch_limits.json" with { type: "json" };
import { canonicalModelName, routesEligibleFor } from "./routes.js";
import {
  availableTokenBudget,
  computeRouteLaneWait,
  routeHasCapacityFor,
  FULL_TOKEN_BUDGET_WINDOWS,
  paymentRequiredBackoffUntil,
} from "./pacing.js";

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
        transient_retry_count       INTEGER NOT NULL DEFAULT 0,
        created_at                  INTEGER NOT NULL,
        updated_at                  INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_jobs_state_priority_created
        ON jobs (state, priority, created_at);

      -- purgePendingBatch selects terminal jobs by age. The index above is keyed
      -- (state, priority, created_at), so that query could only seek on state and then had to
      -- read EVERY completed/failed row to filter and sort on updated_at -- 60k+ rows once the
      -- terminal backlog is large, the same unbounded-scan shape that caused the 2026-08-27
      -- rows-read overage. Measured: 60,189 -> 367 VDBE ops at 6,000 terminal jobs.
      CREATE INDEX IF NOT EXISTS idx_jobs_state_updated
        ON jobs (state, updated_at);

      -- A queued job can be compatible with more than one model. This small index is the
      -- scheduler's route-independent work index: admission asks for a few jobs belonging to a
      -- capacity-ranked model rather than reading an arbitrary prefix of the whole queue.
      CREATE TABLE IF NOT EXISTS job_models (
        job_id      TEXT NOT NULL,
        model       TEXT NOT NULL,
        priority    INTEGER NOT NULL,
        created_at  INTEGER NOT NULL,
        PRIMARY KEY (job_id, model)
      );
      CREATE INDEX IF NOT EXISTS idx_job_models_model_priority_created
        ON job_models (model, priority, created_at, job_id);

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
        buffer_seconds           REAL NOT NULL DEFAULT 0,
        payment_required_streak  INTEGER NOT NULL DEFAULT 0
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

      -- Without this, the bundles table has only its bundle_id primary key, so BOTH of
      -- claimDispatchWindow's per-tick bundle statements (the expire-sweep UPDATE and the
      -- active-bundle SELECT) degrade to full table scans -- and nothing ever deletes from
      -- the bundles table, so that scan grew by one row per claimed bundle forever. Measured
      -- against the
      -- real coordinator at 3,200 bundles: 6,400 of a tick's 6,424 rows read (99.6%) came from
      -- exactly those two statements, which is what exhausted the Durable Objects free tier's
      -- 5M daily rows-read budget on 2026-08-27. With this index both become index seeks over
      -- the 'active' rows only -- a population MAX_ACTIVE_BUNDLES already bounds -- so
      -- lease_expires_at deliberately is NOT part of the key; created_at is, because it also
      -- makes _pruneTerminalRecords's terminal-bundle lookup an ordered seek.
      CREATE INDEX IF NOT EXISTS idx_bundles_state_created
        ON bundles (state, created_at);

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

      -- attempts is otherwise keyed only by attempt_id, so _pruneTerminalRecords's age-based
      -- lookup would have to scan the whole (unbounded, append-only) table to find its batch.
      CREATE INDEX IF NOT EXISTS idx_attempts_created
        ON attempts (created_at);

      CREATE TABLE IF NOT EXISTS estimates (
        key                     TEXT PRIMARY KEY,
        margin_tokens           INTEGER NOT NULL,
        sample_count            INTEGER NOT NULL DEFAULT 0,
        recent_observed_summary TEXT,
        updated_at              INTEGER NOT NULL
      );

      CREATE TABLE IF NOT EXISTS scheduler (
        id                                  INTEGER PRIMARY KEY CHECK (id = 1),
        utc_day                             TEXT NOT NULL,
        bundle_count_today                  INTEGER NOT NULL DEFAULT 0,
        jobs_ingested_today                 INTEGER NOT NULL DEFAULT 0,
        cleanup_cursor                      TEXT,
        next_maintenance_alarm_at           INTEGER,
        job_models_backfill_complete        INTEGER NOT NULL DEFAULT 0,
        legacy_retryable_recovery_complete  INTEGER NOT NULL DEFAULT 0,
        job_models_backfill_high_watermark  INTEGER,
        job_models_backfill_cursor          INTEGER NOT NULL DEFAULT 0,
        job_models_backfill_pending_rowid   INTEGER,
        job_models_backfill_model_offset    INTEGER NOT NULL DEFAULT 0,
        legacy_retryable_recovery_job_id    TEXT,
        legacy_retryable_recovery_model_offset INTEGER NOT NULL DEFAULT 0,
        migration_rows_scanned_today        INTEGER NOT NULL DEFAULT 0,
        migration_write_units_today         INTEGER NOT NULL DEFAULT 0
      );

      -- Keeps the model-queue index's ordering priority synchronized with a direct jobs.priority
      -- edit -- review/44 documents an operator promoting an already-queued job for
      -- recovery/testing as a direct SQLite edit through Cloudflare's dashboard Data Studio or
      -- wrangler dev's Local Explorer SQL Studio. job_models.priority is otherwise only ever set
      -- once, at enqueue/backfill time, so without this trigger such a promotion would silently
      -- never change admission order: the index row already exists, so backfill never revisits
      -- it either. A no-op UPDATE (0 rows matched) when the job isn't currently indexed -- e.g.
      -- already claimed -- is expected and harmless.
      CREATE TRIGGER IF NOT EXISTS trg_jobs_priority_sync
      AFTER UPDATE OF priority ON jobs
      WHEN NEW.priority IS NOT OLD.priority
      BEGIN
        UPDATE job_models SET priority = NEW.priority WHERE job_id = NEW.id;
      END;
    `);

    // CREATE TABLE IF NOT EXISTS only creates the table on its first-ever run for this DO
    // instance; it does not retroactively add a column introduced later (rpd_window_start/
    // rpd_count, added alongside Phase 2's claimDispatchWindow) to a `routes` table an earlier
    // deploy already created. Defensive, cheap, and a no-op on a fresh instance.
    this._ensureColumn("routes", "rpd_window_start", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("routes", "rpd_count", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("routes", "payment_required_streak", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("jobs", "transient_retry_count", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("scheduler", "job_models_backfill_complete", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("scheduler", "legacy_retryable_recovery_complete", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("scheduler", "job_models_backfill_high_watermark", "INTEGER");
    this._ensureColumn("scheduler", "job_models_backfill_cursor", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("scheduler", "job_models_backfill_pending_rowid", "INTEGER");
    this._ensureColumn(
      "scheduler",
      "job_models_backfill_model_offset",
      "INTEGER NOT NULL DEFAULT 0"
    );
    this._ensureColumn("scheduler", "legacy_retryable_recovery_job_id", "TEXT");
    this._ensureColumn(
      "scheduler",
      "legacy_retryable_recovery_model_offset",
      "INTEGER NOT NULL DEFAULT 0"
    );
    this._ensureColumn("scheduler", "migration_rows_scanned_today", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("scheduler", "migration_write_units_today", "INTEGER NOT NULL DEFAULT 0");

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
    const ALLOWED_TABLES = new Set([
      "routes",
      "jobs",
      "bundles",
      "attempts",
      "estimates",
      "scheduler",
    ]);
    const ALLOWED_COLUMNS = new Map([
      ["rpd_window_start", "INTEGER NOT NULL DEFAULT 0"],
      ["rpd_count", "INTEGER NOT NULL DEFAULT 0"],
      ["payment_required_streak", "INTEGER NOT NULL DEFAULT 0"],
      ["transient_retry_count", "INTEGER NOT NULL DEFAULT 0"],
      ["job_models_backfill_complete", "INTEGER NOT NULL DEFAULT 0"],
      ["legacy_retryable_recovery_complete", "INTEGER NOT NULL DEFAULT 0"],
      ["job_models_backfill_high_watermark", "INTEGER"],
      ["job_models_backfill_cursor", "INTEGER NOT NULL DEFAULT 0"],
      ["job_models_backfill_pending_rowid", "INTEGER"],
      ["job_models_backfill_model_offset", "INTEGER NOT NULL DEFAULT 0"],
      ["legacy_retryable_recovery_job_id", "TEXT"],
      ["legacy_retryable_recovery_model_offset", "INTEGER NOT NULL DEFAULT 0"],
      ["migration_rows_scanned_today", "INTEGER NOT NULL DEFAULT 0"],
      ["migration_write_units_today", "INTEGER NOT NULL DEFAULT 0"],
    ]);
    if (!ALLOWED_TABLES.has(table) || ALLOWED_COLUMNS.get(column) !== definition) {
      throw new Error(`_ensureColumn rejected unallowed schema mutation: ${table}.${column} ${definition}`);
    }
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
    return Number.isFinite(configured) && configured > 0 ? configured : 5000;
  }

  _envInt(name, fallback) {
    const configured = Number(this.env[name]);
    return Number.isFinite(configured) && configured >= 0 ? configured : fallback;
  }

  _maxBundleJobs() {
    return this._envInt("MAX_BUNDLE_JOBS", 4);
  }

  _maxJobsPerModelClaim() {
    const configured = Number(this.env.MAX_JOBS_PER_MODEL_CLAIM);
    // A model is only allowed to contribute one bundle's worth of candidates. Exact token and
    // lane checks still run below, but never over an arbitrary global-queue prefix.
    return Number.isInteger(configured) && configured > 0 ? configured : this._maxBundleJobs();
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

  /** AI Gateway already made its own short retry series, so this is a small durable outer budget. */
  _max5xxRetries() {
    return this._envInt("MAX_5XX_RETRIES", 1);
  }

  /** Maximum route-only cooldown applied after a final post-Gateway 5xx. */
  _max5xxBackoffMs() {
    return this._envInt("MAX_5XX_BACKOFF_SECONDS", 300) * 1000;
  }

  /**
   * Migration-only write cap. Zero deliberately pauses historical repair work without pausing
   * normal dispatch: new jobs are indexed at enqueue time and already-indexed jobs stay eligible.
   */
  _maxQueuedJobModelBackfillPerClaim() {
    return this._envInt("MAX_QUEUED_JOB_MODEL_BACKFILL_PER_CLAIM", 1000);
  }

  /** Zero deliberately pauses recovery of legacy, pre-5xx-fix retryable rows. */
  _maxLegacyRetryableRecoveryPerClaim() {
    return this._envInt("MAX_LEGACY_RETRYABLE_RECOVERY_PER_CLAIM", 100);
  }

  /** Conservative daily migration budget; zero is a complete migration pause. */
  _maxMigrationWriteUnitsPerUtcDay() {
    return this._envInt("MAX_MIGRATION_WRITE_UNITS_PER_UTC_DAY", 5000);
  }

  /** Bound migration reads independently of ordinary capacity-ranked dispatch reads. */
  _maxMigrationRowsScannedPerUtcDay() {
    return this._envInt("MAX_MIGRATION_ROWS_SCANNED_PER_UTC_DAY", 250000);
  }

  /**
   * Retention for the DO's two append-only bookkeeping tables. Neither `bundles` nor `attempts`
   * has any B2 counterpart or client-side dependency -- they are internal scheduler history, so
   * unlike `jobs` (whose row must outlive the client's result fetch, see purgePendingBatch)
   * they can be aged out entirely inside the DO with no coordination. Kept long enough to stay
   * useful for incident diagnosis, short enough that neither table grows without bound.
   */
  _bundleRetentionMs() {
    return this._envInt("BUNDLE_RETENTION_DAYS", 7) * 86_400_000;
  }

  _attemptRetentionMs() {
    return this._envInt("ATTEMPT_RETENTION_DAYS", 7) * 86_400_000;
  }

  /** Per-tick delete budget for each table. Deletes are row WRITES, so this bounds the drain of
   * an existing backlog (and steady-state upkeep needs only a row or two per tick). Zero on
   * either is an emergency pause, matching the migration knobs' convention. */
  _maxBundlePrunePerTick() {
    return this._envInt("MAX_BUNDLE_PRUNE_PER_TICK", 50);
  }

  _maxAttemptPrunePerTick() {
    return this._envInt("MAX_ATTEMPT_PRUNE_PER_TICK", 50);
  }

  /** Return an exponential route cooldown, starting at one minute, for a final Gateway 5xx. */
  _5xxBlockedUntil(retryCount, now) {
    const baseMs = 60_000;
    const delayMs = Math.min(baseMs * 2 ** Math.max(0, retryCount - 1), this._max5xxBackoffMs());
    return now + delayMs;
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
    const rows = [...sql.exec(
      `SELECT utc_day, bundle_count_today, jobs_ingested_today, job_models_backfill_complete,
              legacy_retryable_recovery_complete, job_models_backfill_high_watermark,
              job_models_backfill_cursor, job_models_backfill_pending_rowid,
              job_models_backfill_model_offset, legacy_retryable_recovery_job_id,
              legacy_retryable_recovery_model_offset, migration_rows_scanned_today,
              migration_write_units_today
       FROM scheduler WHERE id = 1`
    )];
    if (rows.length === 0) {
      sql.exec(
        "INSERT INTO scheduler (id, utc_day, bundle_count_today, jobs_ingested_today) VALUES (1, ?, 0, 0)",
        today
      );
      return {
        utc_day: today,
        bundle_count_today: 0,
        jobs_ingested_today: 0,
        job_models_backfill_complete: 0,
        legacy_retryable_recovery_complete: 0,
        job_models_backfill_high_watermark: null,
        job_models_backfill_cursor: 0,
        job_models_backfill_pending_rowid: null,
        job_models_backfill_model_offset: 0,
        legacy_retryable_recovery_job_id: null,
        legacy_retryable_recovery_model_offset: 0,
        migration_rows_scanned_today: 0,
        migration_write_units_today: 0,
      };
    }
    const sched = rows[0];
    if (sched.utc_day !== today) {
      sql.exec(
        `UPDATE scheduler SET utc_day = ?, bundle_count_today = 0, jobs_ingested_today = 0,
                              migration_rows_scanned_today = 0, migration_write_units_today = 0
         WHERE id = 1`,
        today
      );
      // Both completion flags are one-time migration latches, not daily counters.
      return {
        ...sched,
        utc_day: today,
        bundle_count_today: 0,
        jobs_ingested_today: 0,
        migration_rows_scanned_today: 0,
        migration_write_units_today: 0,
      };
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
        this._indexQueuedJobModels(
          { ...job, policy_json: policyJson, priority, created_at: now },
          priority,
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
        throttle_streak, last_provider_status, blocked_until, buffer_seconds,
        payment_required_streak
      ) VALUES (?, 0, 0, 0, 0, 0, 0, ?, ?, 0, 0, 0, 0, NULL, NULL, 0, 0)`,
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
      payment_required_streak: 0,
    };
  }

  /**
   * Canonical model groups a job may use. Persist every explicit allowed model, even when it has
   * no configured route yet, so a later catalog addition makes an already-queued job searchable
   * without rewriting it. This follows aliases only: v2 does not expand config/model_routing.
   *
   * Omits a model whose *every* currently configured route is structurally too small for this
   * job's own token estimates (routesEligibleFor's input/output context-limit check, evaluated
   * here independent of any live RPM/RPD/TPM/blocked_until state, which fluctuates and must never
   * exclude a job from the index). Without this, a handful of oversized jobs land at the head of
   * that model's bounded per-claim candidate window (MAX_JOBS_PER_MODEL_CLAIM) and stay there
   * forever -- routesEligibleFor always returns empty for them, so claimDispatchWindow re-reads
   * the exact same unclaimed rows every tick and a smaller, perfectly dispatchable job queued
   * behind them is never even read, let alone claimed. A model with no configured route at all is
   * kept (can't be size-checked against a limit that doesn't exist); a job with no route left
   * under any of its allowed models falls through to _indexQueuedJobModels's own
   * "__unroutable__" sentinel, exactly like a malformed/unknown-model policy already does.
   */
  _modelsForQueuedJob(job, dispatchLimits = this._dispatchLimits()) {
    let policy;
    try {
      policy = typeof job.policy_json === "string" ? JSON.parse(job.policy_json) : job.policy_json;
    } catch {
      return [];
    }
    const allowedModels = Array.isArray(policy?.allowed_models) ? policy.allowed_models : [];
    const allowPaid = Boolean(policy?.allow_paid);
    const inputTokens = job.input_token_estimate || 0;
    const outputTokens = job.max_output_token_estimate || 0;
    const routesByModel = dispatchLimits?.model_routes_map || {};
    const catalog = dispatchLimits?.routes_by_id || {};

    const models = new Set();
    for (const rawModel of allowedModels) {
      if (typeof rawModel !== "string" || rawModel.trim() === "") continue;
      const canonical = canonicalModelName(rawModel.trim(), dispatchLimits);
      const routeIds = routesByModel[canonical];
      if (!Array.isArray(routeIds) || routeIds.length === 0) {
        models.add(canonical);
        continue;
      }
      const fitsSomeConfiguredRoute = routeIds.some((routeId) => {
        const route = catalog[routeId];
        if (!route) return false;
        if (!allowPaid && !route.free) return false;
        return (
          inputTokens <= (route.input_context_limit || 32768) &&
          outputTokens <= (route.output_context_limit || 1024)
        );
      });
      if (fitsSomeConfiguredRoute) models.add(canonical);
    }
    return [...models];
  }

  _modelsToIndex(job) {
    const models = this._modelsForQueuedJob(job);
    return models.length > 0 ? models : ["__unroutable__"];
  }

  _indexQueuedJobModels(job, priority = job.priority, createdAt = job.created_at, models) {
    const sql = this._getSql();
    // Preserve one sentinel for an unrouteable policy so rollout backfill does not reconsider the
    // same malformed/unknown job on every cron tick. It is never present in model_routes_map.
    for (const model of models || this._modelsToIndex(job)) {
      sql.exec(
        `INSERT OR IGNORE INTO job_models (job_id, model, priority, created_at)
         VALUES (?, ?, ?, ?)`,
        job.id,
        model,
        priority,
        createdAt
      );
    }
  }

  static MIGRATION_MODEL_INDEX_WRITE_UNITS = 3;

  static MIGRATION_RECOVERY_JOB_WRITE_UNITS = 2;

  _migrationIndexWriteUnits(models) {
    return models.length * LLMSchedulerDO.MIGRATION_MODEL_INDEX_WRITE_UNITS;
  }

  _indexMigrationModelChunk(job, models, modelOffset, remainingWriteUnits) {
    const offset = Math.min(Math.max(0, Number(modelOffset || 0)), models.length);
    const count = Math.min(
      models.length - offset,
      Math.max(
        0,
        Math.floor(remainingWriteUnits / LLMSchedulerDO.MIGRATION_MODEL_INDEX_WRITE_UNITS)
      )
    );
    if (count > 0) {
      this._indexQueuedJobModels(
        job,
        job.priority,
        job.created_at,
        models.slice(offset, offset + count)
      );
    }
    return {
      modelOffset: offset + count,
      writeUnits: count * LLMSchedulerDO.MIGRATION_MODEL_INDEX_WRITE_UNITS,
    };
  }

  /**
   * Walk every job present at rollout exactly once by rowid. A fixed high-water mark excludes
   * post-rollout jobs, which are indexed synchronously on enqueue. This avoids repeatedly
   * restarting a NOT EXISTS scan at the queue head as earlier repaired rows accumulate there.
   */
  _backfillQueuedJobModels({
    limit,
    highWatermark,
    cursor,
    pendingRowid,
    modelOffset,
    remainingRows,
    remainingWriteUnits,
  }) {
    const sql = this._getSql();
    let scanned = 0;
    let writeUnits = 0;
    let nextHighWatermark = highWatermark;
    let nextCursor = Number(cursor || 0);
    let nextPendingRowid = pendingRowid == null ? null : Number(pendingRowid);
    let nextModelOffset = Number(modelOffset || 0);

    if (remainingRows <= 0 || remainingWriteUnits <= 0) {
      return {
        scanned,
        writeUnits,
        highWatermark,
        cursor: nextCursor,
        pendingRowid: nextPendingRowid,
        modelOffset: nextModelOffset,
        complete: false,
      };
    }
    if (nextHighWatermark == null) {
      const rows = [...sql.exec("SELECT COALESCE(MAX(rowid), 0) AS high_watermark FROM jobs")];
      scanned += 1;
      nextHighWatermark = Number(rows[0]?.high_watermark || 0);
    }
    if (nextCursor >= nextHighWatermark || remainingRows <= scanned) {
      return {
        scanned,
        writeUnits,
        highWatermark: nextHighWatermark,
        cursor: nextCursor,
        pendingRowid: nextPendingRowid,
        modelOffset: nextModelOffset,
        complete: nextPendingRowid == null && nextCursor >= nextHighWatermark,
      };
    }

    const indexJob = (job, offset) => {
      if (job.state !== "queued") {
        nextCursor = job.migration_rowid;
        nextPendingRowid = null;
        nextModelOffset = 0;
        return true;
      }
      const models = this._modelsToIndex(job);
      const chunk = this._indexMigrationModelChunk(
        job,
        models,
        offset,
        remainingWriteUnits - writeUnits
      );
      writeUnits += chunk.writeUnits;
      if (chunk.modelOffset < models.length) {
        nextPendingRowid = job.migration_rowid;
        nextModelOffset = chunk.modelOffset;
        return false;
      }
      nextCursor = job.migration_rowid;
      nextPendingRowid = null;
      nextModelOffset = 0;
      return true;
    };

    if (nextPendingRowid != null) {
      const rows = [...sql.exec(
        "SELECT rowid AS migration_rowid, * FROM jobs WHERE rowid = ?",
        nextPendingRowid
      )];
      scanned += rows.length;
      if (rows.length === 0) {
        nextCursor = Math.max(nextCursor, nextPendingRowid);
        nextPendingRowid = null;
        nextModelOffset = 0;
      } else {
        indexJob(rows[0], nextModelOffset);
      }
      return {
        scanned,
        writeUnits,
        highWatermark: nextHighWatermark,
        cursor: nextCursor,
        pendingRowid: nextPendingRowid,
        modelOffset: nextModelOffset,
        complete: nextPendingRowid == null && nextCursor >= nextHighWatermark,
      };
    }

    const batchLimit = Math.min(limit, remainingRows - scanned);
    const rows = [...sql.exec(
      `SELECT rowid AS migration_rowid, * FROM jobs
       WHERE rowid > ? AND rowid <= ?
       ORDER BY rowid ASC
       LIMIT ?`,
      nextCursor,
      nextHighWatermark,
      batchLimit
    )];
    scanned += rows.length;
    for (const job of rows) {
      if (!indexJob(job, 0)) break;
    }
    return {
      scanned,
      writeUnits,
      highWatermark: nextHighWatermark,
      cursor: nextCursor,
      pendingRowid: nextPendingRowid,
      modelOffset: nextModelOffset,
      complete: nextPendingRowid == null && nextCursor >= nextHighWatermark,
    };
  }

  /**
   * Prior deployments recorded a final 5xx as `retryable`, but claimDispatchWindow only reads
   * `queued` jobs. Recover a small batch on every cron tick so those rows become dispatchable
   * without a manual SQLite edit. They receive one post-Gateway recovery execution: initialize
   * their outer-retry counter at the cap so another final 5xx is surfaced as failed, never cycled.
   */
  _recoverLegacyRetryableJobs(
    now,
    { limit, pendingJobId, modelOffset, remainingRows, remainingWriteUnits }
  ) {
    const sql = this._getSql();
    if (limit <= 0 || remainingRows <= 0 || remainingWriteUnits <= 0) {
      return { scanned: 0, writeUnits: 0, pendingJobId, modelOffset, complete: false };
    }
    let pendingId = pendingJobId || null;
    let nextModelOffset = Number(modelOffset || 0);
    let writeUnits = 0;
    const recover = (job, offset) => {
      const models = this._modelsToIndex(job);
      const normalizedOffset = Math.min(Math.max(0, offset), models.length);
      let mappingCount = Math.min(
        models.length - normalizedOffset,
        Math.floor(
          (remainingWriteUnits - writeUnits) / LLMSchedulerDO.MIGRATION_MODEL_INDEX_WRITE_UNITS
        )
      );
      // Hold two units for the state change when this chunk could finish the model list. Otherwise
      // a final mapping would be persisted but the retryable job could never afford its transition.
      if (
        mappingCount === models.length - normalizedOffset &&
        remainingWriteUnits - writeUnits - this._migrationIndexWriteUnits(models.slice(
          normalizedOffset,
          normalizedOffset + mappingCount
        )) < LLMSchedulerDO.MIGRATION_RECOVERY_JOB_WRITE_UNITS
      ) {
        mappingCount -= 1;
      }
      if (mappingCount > 0) {
        const chunk = models.slice(normalizedOffset, normalizedOffset + mappingCount);
        this._indexQueuedJobModels(job, job.priority, job.created_at, chunk);
        writeUnits += this._migrationIndexWriteUnits(chunk);
      }
      const nextOffset = normalizedOffset + mappingCount;
      if (nextOffset < models.length) {
        pendingId = job.id;
        nextModelOffset = nextOffset;
        return false;
      }
      if (
        remainingWriteUnits - writeUnits < LLMSchedulerDO.MIGRATION_RECOVERY_JOB_WRITE_UNITS
      ) {
        pendingId = job.id;
        nextModelOffset = nextOffset;
        return false;
      }
      sql.exec(
        `UPDATE jobs SET state='queued', lease_token=NULL, lease_route_id=NULL,
                         lease_expires_at=NULL, bundle_id=NULL, transient_retry_count=?, updated_at=?
         WHERE id=?`,
        this._max5xxRetries(),
        now,
        job.id
      );
      writeUnits += LLMSchedulerDO.MIGRATION_RECOVERY_JOB_WRITE_UNITS;
      pendingId = null;
      nextModelOffset = 0;
      return true;
    };

    if (pendingId != null) {
      const rows = [...sql.exec("SELECT * FROM jobs WHERE id=? AND state='retryable'", pendingId)];
      if (rows.length === 0) {
        return { scanned: 0, writeUnits, pendingJobId: null, modelOffset: 0, complete: false };
      }
      recover(rows[0], nextModelOffset);
      return {
        scanned: 1,
        writeUnits,
        pendingJobId: pendingId,
        modelOffset: nextModelOffset,
        complete: false,
      };
    }

    const batchLimit = Math.min(limit, remainingRows);
    const legacy = [...sql.exec(
      `SELECT * FROM jobs
       WHERE state='retryable'
       ORDER BY priority ASC, created_at ASC
       LIMIT ?`,
      batchLimit
    )];
    for (const job of legacy) {
      if (!recover(job, 0)) break;
    }
    return {
      scanned: legacy.length,
      writeUnits,
      pendingJobId: pendingId,
      modelOffset: nextModelOffset,
      complete: pendingId == null && legacy.length < batchLimit,
    };
  }

  /**
   * Delete one bounded batch of aged-out terminal bundles and attempt records.
   *
   * A bundle is only ever removed once it is terminal ('completed'/'expired'), its lease can no
   * longer be current, AND it is older than the retention window -- so a late completeBatch or
   * authorizeRetry can never lose a bundle it might still legitimately settle. (Those two both
   * already treat a missing bundle as a stale no-op, which is the correct outcome long after a
   * lease expired, but the lease_expires_at guard means we never rely on that.)
   *
   * Both statements delete by primary key from an id list gathered by an indexed, LIMIT-ed
   * subquery, rather than `DELETE ... LIMIT` (which requires a SQLite compile-time option that
   * is not guaranteed to be enabled) or an unbounded `DELETE ... WHERE created_at < ?` (which
   * would scan the very tables this retention exists to keep small).
   */
  _pruneTerminalRecords(now) {
    const sql = this._getSql();
    const bundleLimit = this._maxBundlePrunePerTick();
    const attemptLimit = this._maxAttemptPrunePerTick();
    let bundlesDeleted = 0;
    let attemptsDeleted = 0;

    if (bundleLimit > 0) {
      const cutoff = now - this._bundleRetentionMs();
      const ids = [...sql.exec(
        `SELECT bundle_id FROM bundles
         WHERE state IN ('completed','expired') AND created_at < ? AND lease_expires_at < ?
         ORDER BY created_at ASC
         LIMIT ?`,
        cutoff,
        now,
        bundleLimit
      )].map((row) => row.bundle_id);
      for (const chunk of this._chunks(ids)) {
        const placeholders = chunk.map(() => "?").join(",");
        sql.exec(`DELETE FROM bundles WHERE bundle_id IN (${placeholders})`, ...chunk);
        bundlesDeleted += chunk.length;
      }
    }

    if (attemptLimit > 0) {
      const cutoff = now - this._attemptRetentionMs();
      const ids = [...sql.exec(
        `SELECT attempt_id FROM attempts WHERE created_at < ? ORDER BY created_at ASC LIMIT ?`,
        cutoff,
        attemptLimit
      )].map((row) => row.attempt_id);
      for (const chunk of this._chunks(ids)) {
        const placeholders = chunk.map(() => "?").join(",");
        sql.exec(`DELETE FROM attempts WHERE attempt_id IN (${placeholders})`, ...chunk);
        attemptsDeleted += chunk.length;
      }
    }

    return { bundlesDeleted, attemptsDeleted };
  }

  _freshRouteLedger(catalogRoute, now) {
    const tpm = Number(catalogRoute?.tpm) || 0;
    return {
      rpm_window_start: 0,
      rpm_count: 0,
      rpd_window_start: 0,
      rpd_count: 0,
      tpm_window_start: 0,
      tpm_reserved: 0,
      full_token_budget: tpm * FULL_TOKEN_BUDGET_WINDOWS,
      token_budget_updated_at: now,
      blocked_until: null,
      buffer_seconds: 0,
    };
  }

  _capacityFraction(route, now, windowSeconds) {
    if (Number(route.blocked_until) > now) return 0;
    if (Number(route.buffer_seconds || 0) * 1000 >= windowSeconds * 1000) return 0;

    const windowFraction = (limit, windowStart, count, durationMs) => {
      // rpd: 0 is the repository's explicit "paused/exhausted" convention, not an unlimited
      // route. Other absent limits do not constrain this coarse, route-ranking score.
      if (limit === 0) return 0;
      if (!Number.isFinite(limit) || limit < 0) return 1;
      if (!Number.isFinite(windowStart) || now - windowStart >= durationMs) return 1;
      return Math.max(0, Math.min(1, (limit - Math.max(0, count || 0)) / limit));
    };

    const rpmFraction = windowFraction(
      Number(route.rpm),
      route.rpm_window_start,
      route.rpm_count,
      60_000
    );
    const rpdFraction = windowFraction(
      Number(route.rpd),
      route.rpd_window_start,
      route.rpd_count,
      86_400_000
    );
    const tpm = Number(route.tpm);
    const tokenFraction =
      Number.isFinite(tpm) && tpm > 0
        ? Math.max(
            0,
            Math.min(1, availableTokenBudget(route, now) / (tpm * FULL_TOKEN_BUDGET_WINDOWS))
          )
        : 1;
    return Math.min(rpmFraction, rpdFraction, tokenFraction);
  }

  /** Read the small route ledger once, rank model pools by their aggregate free fraction, and
   * retain a route-level score for preferring the best account inside the chosen model. */
  _rankModelsByCapacity(now, windowSeconds, dispatchLimits) {
    const sql = this._getSql();
    const ledgers = new Map(
      [...sql.exec("SELECT * FROM routes")].map((row) => [row.route_id, row])
    );
    const catalog = dispatchLimits?.routes_by_id || {};
    return Object.entries(dispatchLimits?.model_routes_map || {})
      .map(([model, routeIds]) => {
        const routes = routeIds
          .map((routeId) => {
            const catalogRoute = catalog[routeId];
            if (!catalogRoute) return null;
            const route = {
              ...catalogRoute,
              ...(ledgers.get(routeId) || this._freshRouteLedger(catalogRoute, now)),
              route_id: routeId,
              model,
            };
            return { route, score: this._capacityFraction(route, now, windowSeconds) };
          })
          .filter(Boolean)
          .sort(
            (left, right) =>
              right.score - left.score || left.route.route_id.localeCompare(right.route.route_id)
          );
        // Daily request capacity is the common unit across providers. Weighting avoids a tiny
        // fallback account counting as much as a high-volume primary account; a paused rpd: 0
        // route has no configured capacity weight at all, while a 402-blocked normal route keeps
        // its weight and therefore correctly pulls the model's available percentage down.
        const totalWeight = routes.reduce((sum, entry) => {
          const rpd = Number(entry.route.rpd);
          const rpm = Number(entry.route.rpm);
          const weight = Number.isFinite(rpd) ? Math.max(0, rpd) : Math.max(0, rpm) * 1440;
          return sum + weight;
        }, 0);
        const score =
          totalWeight === 0
            ? 0
            : routes.reduce((sum, entry) => {
                const rpd = Number(entry.route.rpd);
                const rpm = Number(entry.route.rpm);
                const weight = Number.isFinite(rpd) ? Math.max(0, rpd) : Math.max(0, rpm) * 1440;
                return sum + entry.score * weight;
              }, 0) / totalWeight;
        return {
          model,
          score,
          routeScores: new Map(routes.map((entry) => [entry.route.route_id, entry.score])),
        };
      })
      .filter((entry) => entry.score > 0)
      .sort((left, right) => right.score - left.score || left.model.localeCompare(right.model));
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

  static EMPTY_CLAIM_RESULT = { bundle_id: null, execution_token: null, jobs: [] };

  /** review/44 Unit 4: fenced, capacity-ranked admission and pacing in one SQLite transaction. */
  async claimDispatchWindow(now, windowSeconds) {
    const sql = this._getSql();
    const dispatchLimits = this._dispatchLimits();

    return this.ctx.storage.transactionSync(() => {
      const maxBundlesPerDay = this._maxBundlesPerUtcDay();
      const maxActiveBundles = this._maxActiveBundles();
      const maxInFlightCalls = this._maxInFlightLlmCalls();
      const maxBundleJobs = this._maxBundleJobs();
      const maxJobsPerModelClaim = this._maxJobsPerModelClaim();
      const maxConcurrentLanes = this._maxConcurrentRouteLanes();
      const maxJobsPerRoutePerBundle = this._maxJobsPerRoutePerBundle();
      const estimateFloor = this._estimateFloor();
      const callDurationCeilingMs = this._callDurationCeilingMs();
      const leaseDurationMs = this._leaseDurationMs();
      const EMPTY = LLMSchedulerDO.EMPTY_CLAIM_RESULT;

      const sched = this._rollUtcDayIfNeeded(now);
      if (sched.bundle_count_today >= maxBundlesPerDay) return EMPTY;

      const migrationRowsLimit = this._maxMigrationRowsScannedPerUtcDay();
      const migrationWriteUnitsLimit = this._maxMigrationWriteUnitsPerUtcDay();
      let migrationRowsScanned = Number(sched.migration_rows_scanned_today || 0);
      let migrationWriteUnits = Number(sched.migration_write_units_today || 0);
      let legacyRetryableRecoveryComplete = Boolean(sched.legacy_retryable_recovery_complete);
      let jobModelsBackfillComplete = Boolean(sched.job_models_backfill_complete);
      let jobModelsBackfillHighWatermark = sched.job_models_backfill_high_watermark;
      let jobModelsBackfillCursor = Number(sched.job_models_backfill_cursor || 0);
      let jobModelsBackfillPendingRowid = sched.job_models_backfill_pending_rowid;
      let jobModelsBackfillModelOffset = Number(sched.job_models_backfill_model_offset || 0);
      let legacyRetryableRecoveryJobId = sched.legacy_retryable_recovery_job_id;
      let legacyRetryableRecoveryModelOffset = Number(
        sched.legacy_retryable_recovery_model_offset || 0
      );
      const initialMigrationState = {
        rows: migrationRowsScanned,
        writeUnits: migrationWriteUnits,
        legacyComplete: legacyRetryableRecoveryComplete,
        backfillComplete: jobModelsBackfillComplete,
        highWatermark: jobModelsBackfillHighWatermark,
        cursor: jobModelsBackfillCursor,
        pendingRowid: jobModelsBackfillPendingRowid,
        backfillModelOffset: jobModelsBackfillModelOffset,
        recoveryJobId: legacyRetryableRecoveryJobId,
        recoveryModelOffset: legacyRetryableRecoveryModelOffset,
      };
      const persistMigrationState = () => {
        if (
          migrationRowsScanned === initialMigrationState.rows &&
          migrationWriteUnits === initialMigrationState.writeUnits &&
          legacyRetryableRecoveryComplete === initialMigrationState.legacyComplete &&
          jobModelsBackfillComplete === initialMigrationState.backfillComplete &&
          jobModelsBackfillHighWatermark === initialMigrationState.highWatermark &&
          jobModelsBackfillCursor === initialMigrationState.cursor &&
          jobModelsBackfillPendingRowid === initialMigrationState.pendingRowid &&
          jobModelsBackfillModelOffset === initialMigrationState.backfillModelOffset &&
          legacyRetryableRecoveryJobId === initialMigrationState.recoveryJobId &&
          legacyRetryableRecoveryModelOffset === initialMigrationState.recoveryModelOffset
        ) {
          return;
        }
        sql.exec(
          `UPDATE scheduler
           SET legacy_retryable_recovery_complete = ?, job_models_backfill_complete = ?,
               job_models_backfill_high_watermark = ?, job_models_backfill_cursor = ?,
               job_models_backfill_pending_rowid = ?, job_models_backfill_model_offset = ?,
               legacy_retryable_recovery_job_id = ?,
               legacy_retryable_recovery_model_offset = ?, migration_rows_scanned_today = ?,
               migration_write_units_today = ?
           WHERE id = 1`,
          Number(legacyRetryableRecoveryComplete),
          Number(jobModelsBackfillComplete),
          jobModelsBackfillHighWatermark,
          jobModelsBackfillCursor,
          jobModelsBackfillPendingRowid,
          jobModelsBackfillModelOffset,
          legacyRetryableRecoveryJobId,
          legacyRetryableRecoveryModelOffset,
          migrationRowsScanned,
          migrationWriteUnits
        );
        initialMigrationState.rows = migrationRowsScanned;
        initialMigrationState.writeUnits = migrationWriteUnits;
        initialMigrationState.legacyComplete = legacyRetryableRecoveryComplete;
        initialMigrationState.backfillComplete = jobModelsBackfillComplete;
        initialMigrationState.highWatermark = jobModelsBackfillHighWatermark;
        initialMigrationState.cursor = jobModelsBackfillCursor;
        initialMigrationState.pendingRowid = jobModelsBackfillPendingRowid;
        initialMigrationState.backfillModelOffset = jobModelsBackfillModelOffset;
        initialMigrationState.recoveryJobId = legacyRetryableRecoveryJobId;
        initialMigrationState.recoveryModelOffset = legacyRetryableRecoveryModelOffset;
      };
      const remainingMigrationRows = () => Math.max(0, migrationRowsLimit - migrationRowsScanned);
      const remainingMigrationWriteUnits = () =>
        Math.max(0, migrationWriteUnitsLimit - migrationWriteUnits);

      // A bounded compatibility migration prevents pre-recovery deployments from leaving jobs
      // in the unclaimable retryable state indefinitely. Once its final short batch is empty,
      // persist that fact so later cron ticks spend no read on a state new code never writes.
      const legacyRetryableRecoveryLimit = this._maxLegacyRetryableRecoveryPerClaim();
      if (!legacyRetryableRecoveryComplete && legacyRetryableRecoveryLimit > 0) {
        const result = this._recoverLegacyRetryableJobs(now, {
          limit: legacyRetryableRecoveryLimit,
          pendingJobId: legacyRetryableRecoveryJobId,
          modelOffset: legacyRetryableRecoveryModelOffset,
          remainingRows: remainingMigrationRows(),
          remainingWriteUnits: remainingMigrationWriteUnits(),
        });
        migrationRowsScanned += result.scanned;
        migrationWriteUnits += result.writeUnits;
        legacyRetryableRecoveryJobId = result.pendingJobId;
        legacyRetryableRecoveryModelOffset = result.modelOffset;
        legacyRetryableRecoveryComplete ||= result.complete;
        // This migration runs before the active-bundle early return, so persist its counters now.
        persistMigrationState();
      }

      // Reap bundles whose lease expired without ever reaching completeBatch -- an executor
      // crash, a CPU/wall-clock eviction mid-tick, or an uncaught error before the final
      // completeBatch RPC all leave a bundle stuck 'active' forever otherwise, since nothing
      // else in this file ever moves a bundle out of 'active'. Left unreaped, each one
      // permanently consumes one of MAX_ACTIVE_BUNDLES's slots -- once that many accumulate,
      // every future claimDispatchWindow call returns EMPTY at the very next check below,
      // regardless of how many jobs are queued, with no error anywhere to signal why. Mirrors
      // the same lease-timeout requeue this DO already does per-job on a `deferred_late`
      // completeBatch outcome, just applied at the bundle level, before that outcome can ever
      // be reported.
      sql.exec(`UPDATE bundles SET state='expired' WHERE state='active' AND lease_expires_at < ?`, now);
      const expiredJobs = [...sql.exec(
        "SELECT * FROM jobs WHERE state='leased' AND lease_expires_at < ?",
        now
      )];
      if (expiredJobs.length > 0) {
        sql.exec(
          `UPDATE jobs SET state='queued', lease_token=NULL, lease_route_id=NULL,
                            lease_expires_at=NULL, bundle_id=NULL, updated_at=?
           WHERE state='leased' AND lease_expires_at < ?`,
          now,
          now
        );
        for (const job of expiredJobs) this._indexQueuedJobModels(job);
      }

      // Retention runs right after the expire-sweep, so a bundle this tick just marked
      // 'expired' is eligible on a later tick once it ages out. Bounded per tick; see
      // _pruneTerminalRecords.
      this._pruneTerminalRecords(now);

      const activeBundles = [...sql.exec("SELECT active_call_count FROM bundles WHERE state='active'")];
      if (activeBundles.length >= maxActiveBundles) return EMPTY;
      const inFlightCalls = activeBundles.reduce((sum, b) => sum + b.active_call_count, 0);
      if (inFlightCalls >= maxInFlightCalls) return EMPTY;

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

      // One bounded, forward-only migration pass makes pre-index rollout jobs visible. Every normal
      // claim below then reads only the top few jobs for each capacity-ranked model, never a global
      // queue head. Its high-water cursor latches after the finite pre-rollout population is read.
      const queuedJobModelBackfillLimit = this._maxQueuedJobModelBackfillPerClaim();
      if (!jobModelsBackfillComplete && queuedJobModelBackfillLimit > 0) {
        const result = this._backfillQueuedJobModels({
          limit: queuedJobModelBackfillLimit,
          highWatermark: jobModelsBackfillHighWatermark,
          cursor: jobModelsBackfillCursor,
          pendingRowid: jobModelsBackfillPendingRowid,
          modelOffset: jobModelsBackfillModelOffset,
          remainingRows: remainingMigrationRows(),
          remainingWriteUnits: remainingMigrationWriteUnits(),
        });
        migrationRowsScanned += result.scanned;
        migrationWriteUnits += result.writeUnits;
        jobModelsBackfillHighWatermark = result.highWatermark;
        jobModelsBackfillCursor = result.cursor;
        jobModelsBackfillPendingRowid = result.pendingRowid;
        jobModelsBackfillModelOffset = result.modelOffset;
        jobModelsBackfillComplete ||= result.complete;
        persistMigrationState();
      }
      const chosen = [];
      const chosenJobIds = new Set();
      const seenRoutes = new Set();
      for (const modelPlan of this._rankModelsByCapacity(now, windowSeconds, dispatchLimits)) {
        if (chosen.length >= maxBundleJobs) break;
        const candidates = [...sql.exec(
          `SELECT jobs.* FROM job_models
           JOIN jobs ON jobs.id = job_models.job_id
           WHERE job_models.model = ? AND jobs.state = 'queued' AND jobs.rowid != ?
           ORDER BY job_models.priority ASC, job_models.created_at ASC, job_models.job_id ASC
           LIMIT ?`,
          modelPlan.model,
          jobModelsBackfillPendingRowid || 0,
          maxJobsPerModelClaim
        )];

        for (const job of candidates) {
          if (chosen.length >= maxBundleJobs) break;
          if (chosenJobIds.has(job.id)) continue;
          const eligibleRoutes = routesEligibleFor(job, dispatchLimits)
            .filter((route) => route.model === modelPlan.model)
            .sort(
              (left, right) =>
                (modelPlan.routeScores.get(right.route_id) || 0) -
                  (modelPlan.routeScores.get(left.route_id) || 0) ||
                left.route_id.localeCompare(right.route_id)
            );
          const routeOrder = [
            ...eligibleRoutes.filter((route) => !seenRoutes.has(route.route_id)),
            ...eligibleRoutes.filter((route) => seenRoutes.has(route.route_id)),
          ];
          for (const route of routeOrder) {
            const isNewLane = !seenRoutes.has(route.route_id);
            if (isNewLane && seenRoutes.size >= maxConcurrentLanes) continue;
            const countInBundle = chosen.filter(
              (entry) => entry.route.route_id === route.route_id
            ).length;
            if (countInBundle >= maxJobsPerRoutePerBundle) continue;
            const merged = getMergedRoute(route);
            if (
              !routeHasCapacityFor(merged, job, now, windowSeconds, capacityOptions(route, job))
            ) {
              continue;
            }
            chosen.push({ job, route });
            chosenJobIds.add(job.id);
            seenRoutes.add(route.route_id);
            break;
          }
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
          // Keep the model index queue-only: a completed historical backlog must never make a
          // later model lookup walk terminal rows before it reaches current work.
          sql.exec("DELETE FROM job_models WHERE job_id = ?", job.id);

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
        const jobRows = [...sql.exec("SELECT * FROM jobs WHERE id = ?", result.job_id)];
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

        const isFinal5xx =
          result.outcome === "retryable_error" &&
          Number.isInteger(result.provider_status_code) &&
          result.provider_status_code >= 500 &&
          result.provider_status_code <= 599;
        const nextTransientRetryCount = (job.transient_retry_count || 0) + 1;
        const blockedUntil = isFinal5xx
          ? this._5xxBlockedUntil(nextTransientRetryCount, now)
          : null;
        const shouldRetry5xx = isFinal5xx && job.transient_retry_count < this._max5xxRetries();

        let newState;
        switch (result.outcome) {
          case "success":
            newState = "completed";
            break;
          case "deferred_late":
            newState = "queued";
            break;
          case "retryable_error":
            // A final 5xx has already exhausted AI Gateway's short retry sequence. Give it one
            // durable, minute-scale retry; ambiguous transport failures and B2-write failures
            // must surface as failed instead of silently becoming an unclaimable state.
            newState = shouldRetry5xx ? "queued" : "failed";
            break;
          case "terminal_error":
            newState = "failed";
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
          this._indexQueuedJobModels(job);
        } else if (shouldRetry5xx) {
          sql.exec(
            `UPDATE jobs SET state='queued', lease_token=NULL, lease_route_id=NULL,
                             lease_expires_at=NULL, bundle_id=NULL, transient_retry_count=?,
                             updated_at=? WHERE id=?`,
            nextTransientRetryCount,
            now,
            result.job_id
          );
          this._indexQueuedJobModels(job);
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
            // A successful call proves the route is healthy again -- clear every backoff signal,
            // 402's included, not just the 429 ones already cleared here.
            sql.exec(
              `UPDATE routes SET throttle_streak = 0, buffer_seconds = 0,
                                  payment_required_streak = 0, blocked_until = NULL,
                                  last_provider_status = ? WHERE route_id = ?`,
              result.provider_status_code ?? 200,
              job.lease_route_id
            );
          } else if (result.provider_status_code === 402) {
            // Payment required / provider quota exhausted -- a billing-layer signal no amount of
            // rpm/rpd/tpm pacing fixes, so force the route unavailable via blocked_until (see
            // pacing.js's paymentRequiredBackoffUntil) instead of letting every future tick keep
            // re-attempting and re-failing against it.
            const ledger = this._getOrCreateRouteLedger(job.lease_route_id, now, {});
            const newStreak = (ledger.payment_required_streak || 0) + 1;
            sql.exec(
              `UPDATE routes SET payment_required_streak = ?, blocked_until = ?,
                                  last_provider_status = 402 WHERE route_id = ?`,
              newStreak,
              paymentRequiredBackoffUntil(newStreak, now),
              job.lease_route_id
            );
          } else if (isFinal5xx) {
            // The Gateway has already retried this request. Temporarily remove only this route
            // from the capacity ranking so other models/accounts can drain while it recovers.
            sql.exec(
              `UPDATE routes SET blocked_until = MAX(COALESCE(blocked_until, 0), ?),
                                 last_provider_status = ? WHERE route_id = ?`,
              blockedUntil,
              result.provider_status_code,
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
    const modelRoutesMap = catalog?.model_routes_map || {};
    for (const [model, routeIds] of Object.entries(modelRoutesMap)) {
      if (Array.isArray(routeIds) && routeIds.includes(routeId)) {
        return model;
      }
    }
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
        sql.exec(`DELETE FROM job_models WHERE job_id IN (${placeholders})`, ...chunk);
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
