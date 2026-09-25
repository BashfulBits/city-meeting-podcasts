/**
 * Durable Object Coordinator for LLM Dispatch v2.
 * Backed by SQLite inside Cloudflare Workers.
 */

import DISPATCH_LIMITS from "./dispatch_limits.json" with { type: "json" };
// Compiled from config/site_config.yml's `llm_lanes` block by scripts/compile_llm_lanes.py,
// the same block citypods/compute/llm_lanes.py reads, so client and Worker cannot disagree
// about which purposes exist or what each may spend. Drift-checked in the deploy workflow.
import INGRESS_RESERVATIONS from "./ingress_reservations.json" with { type: "json" };
import {
  ROWS_PER_BUNDLE,
  ROWS_PER_CLEANUP_JOB,
  ROWS_PER_INGRESS_WRITE_UNIT,
  ROWS_PER_LEASE_WORST,
} from "./write_budget.js";
import {
  canonicalModelName,
  jobPolicy,
  modelForRouteId,
  modelsForJob,
  routeFitsContext,
  routesEligibleFor,
} from "./routes.js";
import {
  availableTokenBudget,
  computeRouteLaneWait,
  minInterRequestGapMs,
  rpmWindowDurationMs,
  routeHasCapacityFor,
  FULL_TOKEN_BUDGET_WINDOWS,
  paymentRequiredBackoffUntil,
  dailyQuotaReadyAt,
  zonedDateKey,
  routeResetTimezone,
  nextZonedMidnightMs,
  effectiveBufferSeconds,
} from "./pacing.js";
import {
  CALIBRATION_WINDOW,
  calibrationFor,
  parseCalibrationSummary,
  recordCalibrationSample,
} from "./calibration.js";

/** Persist calibration for 1 in this many completions once a key's window is full. */
const CALIBRATION_SAMPLE_EVERY = 4;

/** A stable 0..buckets-1 bucket for an id (FNV-1a), so sampling is deterministic per job. */
function sampleBucket(id, buckets) {
  let hash = 0x811c9dc5;
  for (const ch of String(id ?? "")) {
    hash ^= ch.codePointAt(0);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash % buckets;
}

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

/**
 * Failure classes where the provider never served the request, so it consumed none of our own
 * rate/token quota and the claim-time reservation must be refunded (see completeBatch).
 *
 * `own_rpm` / `own_rpd` / `own_tpm` are excluded on purpose: those rejections are evidence the
 * counters were RIGHT. `unknown_429` is excluded because we cannot demonstrate the request was
 * not ours, and over-charging a route is recoverable while under-charging it invites a real ban.
 */
const NON_CONSUMING_FAILURE_CLASSES = new Set([
  "upstream_capacity",
  "gateway_limit",
  "server_error",
  "route_input_limit",
  "payment_required",
  "request_defect",
  // A 410 for a retired model: nothing was served.
  "route_unavailable",
]);

/**
 * A route's configured numeric limit, or `null` when the route declares none.
 *
 * `Number(null) === 0`, and this repository uses an explicit `0` to mean "paused/exhausted" --
 * so coercing an unset limit with `Number()` made "no daily limit configured" indistinguishable
 * from "deliberately paused". `_capacityFraction` scored both 0, and `claimDispatchWindow`
 * filters `score > 0`, so every route with no `rpd` was silently dropped from the ranking: never
 * ranked, never claimed, never dispatched, with no `blocked_until` and no error to show for it.
 * That was 34 of 69 catalog routes, including all 14 Mistral routes -- which is why 21,287
 * `mistral/mistral-medium-latest` jobs sat queued for 22 days behind a route whose last
 * observed provider status was a plain 200.
 *
 * `undefined` (key absent) happened to survive, because `Number(undefined)` is `NaN` and the
 * `!Number.isFinite` branch returns a full score -- so this only ever bit routes whose compiled
 * JSON carried an explicit `null`, which is exactly what `compile_llm_limits.py` emits.
 */
function configuredLimit(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function parseJsonObject(value, fallback = {}) {
  if (typeof value !== "string" || value.trim() === "") return fallback;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : fallback;
  } catch {
    return fallback;
  }
}

/**
 * Merge two independently indexed ascending result sets without asking SQLite to globally sort
 * them.
 */
function mergeSortedRows(left, right, limit, compare) {
  const merged = [];
  let i = 0;
  let j = 0;
  while (merged.length < limit && (i < left.length || j < right.length)) {
    if (i >= left.length) {
      merged.push(right[j++]);
    } else if (j >= right.length) {
      merged.push(left[i++]);
    } else if (compare(left[i], right[j]) <= 0) {
      merged.push(left[i++]);
    } else {
      merged.push(right[j++]);
    }
  }
  return merged;
}

/** Keeps job_models' ordering priority in step with a direct jobs.priority edit (see the
 * comment where _initSchema installs it). Shared with _migrateJobModelsClustered, which must drop
 * and re-create it around the job_models rebuild. */
const JOB_PRIORITY_SYNC_TRIGGER = `
      CREATE TRIGGER IF NOT EXISTS trg_jobs_priority_sync
      AFTER UPDATE OF priority ON jobs
      WHEN NEW.priority IS NOT OLD.priority
      BEGIN
        UPDATE job_models SET priority = NEW.priority WHERE job_id = NEW.id;
      END;`;

export class LLMSchedulerDO extends DurableObjectBase {
  constructor(ctx, env) {
    super(ctx, env);
    this.ctx = ctx;
    this.env = env || {};
    this.sql = ctx?.storage?.sql || ctx?.sql;

    this._initSchema();
  }

  /**
   * The SQL handle every statement goes through. It records each cursor so the rows it writes
   * can be summed once the RPC finishes (_drainRowCount) -- `cursor.rowsWritten` is the same
   * figure Cloudflare bills against the Free plan's 100,000 rows/day, and it is only final once
   * the cursor is consumed. That running total is what the daily row thresholds gate on.
   */
  _getSql() {
    if (!this.sql) {
      this.sql = this.ctx?.storage?.sql || this.ctx?.sql;
    }
    if (!this.sql) return this.sql;
    if (!this._countingSql || this._countingSqlFor !== this.sql) {
      const raw = this.sql;
      const cursors = (this._openCursors = []);
      this._countingSqlFor = raw;
      this._countingSql = {
        exec: (...args) => {
          const cursor = raw.exec(...args);
          cursors.push(cursor);
          return cursor;
        },
        get databaseSize() {
          return raw.databaseSize;
        },
      };
    }
    return this._countingSql;
  }

  /** Fold the rows written by every statement since the last drain into the in-memory tally. */
  _drainRowCount() {
    const cursors = this._openCursors;
    if (!cursors || cursors.length === 0) return;
    let written = 0;
    for (const cursor of cursors) {
      try {
        for (const _row of cursor) {
          // consume: rowsWritten is final only once the cursor is exhausted
        }
      } catch {
        // an already-closed cursor has nothing left to read
      }
      written += Number(cursor?.rowsWritten) || 0;
    }
    cursors.length = 0;
    this._rowsUnflushed = (this._rowsUnflushed || 0) + written;
  }

  /** Rows written but not yet added to scheduler.rows_written_today; reset when persisted. */
  _takeUnflushedRows() {
    const rows = this._rowsUnflushed || 0;
    this._rowsUnflushed = 0;
    return rows;
  }

  /**
   * Billed rows this DO has written today: the persisted counter (flushed on the scheduler
   * writes each claim and enqueue already make, and on a braked claim tick whenever rows are
   * pending, so tracking it costs almost no extra rows) plus what is still in memory. Every cron
   * tick flushes, so an eviction loses at most about one tick of writes, which the reserve above
   * each threshold absorbs.
   */
  _rowsWrittenToday(sched) {
    const persisted =
      sched && sched.utc_day === this._currentUtcDay(Date.now())
        ? Number(sched.rows_written_today) || 0
        : 0;
    return persisted + (this._rowsUnflushed || 0);
  }

  /** Read-only variant for handlers that do not otherwise touch the scheduler row. */
  _readRowsWrittenToday() {
    const sched = [...this._getSql().exec(
      "SELECT utc_day, rows_written_today FROM scheduler WHERE id = 1"
    )][0];
    return this._rowsWrittenToday(sched);
  }

  // Daily row thresholds (billed rows written; the account-wide platform limit is 100,000).
  // Below the enqueue threshold everything runs. Above it, new enqueues, schema retries,
  // scheduled cleanup and retention pruning stop, so the remaining rows fund dispatch. Above the
  // claim threshold no new leases are claimed. Above the optional threshold acks, retires and
  // cancels are refused as well (all safe to skip: the client already holds the result).
  // In-flight completions, attempt fencing, retry authorization and polls are never refused.
  _enqueueRowStop() {
    return this._envInt("DO_ROWS_ENQUEUE_STOP", 90000);
  }

  _claimRowStop() {
    return this._envInt("DO_ROWS_CLAIM_STOP", 97000);
  }

  _optionalRowStop() {
    return this._envInt("DO_ROWS_OPTIONAL_STOP", 99000);
  }

  _rowBudgetSnapshot(sched) {
    const rows = this._rowsWrittenToday(sched);
    return {
      rows_written_today: rows,
      enqueue_stop: this._enqueueRowStop(),
      claim_stop: this._claimRowStop(),
      optional_stop: this._optionalRowStop(),
      enqueue_open: rows < this._enqueueRowStop(),
      claims_open: rows < this._claimRowStop(),
    };
  }

  /**
   * Read-only admission preflight for producers (`GET /v2/ingress-status`): would a new job for
   * `purpose` be admitted right now? Producers call this before building prompts and staging
   * payloads, so a closed day costs them one request instead of a whole run of work the Worker
   * would reject. Advisory only -- enqueueBatch re-checks every condition, since the window can
   * close between this call and the enqueue. Writes nothing, including across a UTC rollover
   * (yesterday's counters simply read as zero).
   */
  async ingressStatus(purpose, now = Date.now()) {
    const sql = this._getSql();
    const today = this._currentUtcDay(now);
    const sched = [...sql.exec(
      `SELECT utc_day, jobs_ingested_today, ingress_write_units_today, rows_written_today,
              queued_job_count
         FROM scheduler WHERE id = 1`
    )][0] || {};
    const sameDay = sched.utc_day === today;
    const rowBudget = this._rowBudgetSnapshot(sched);
    const jobsToday = sameDay ? Number(sched.jobs_ingested_today) || 0 : 0;
    const unitsToday = sameDay ? Number(sched.ingress_write_units_today) || 0 : 0;
    const queued = Number(sched.queued_job_count) || 0;
    const maxQueued = this._maxQueuedJobs();
    const maxJobs = this._maxJobsPerUtcDay();
    const reasons = [];
    if (!rowBudget.enqueue_open) reasons.push("daily_row_budget");
    if (queued >= maxQueued) reasons.push("queue_full");
    if (jobsToday >= maxJobs) reasons.push("daily_cap_exceeded");

    let lane = null;
    if (purpose) {
      const reservations = this._ingressPurposeReservations();
      const config = Object.hasOwn(reservations, purpose) ? reservations[purpose] : null;
      if (!config) {
        reasons.push("purpose_not_registered");
      } else {
        const usage = new Map(
          sameDay
            ? [...sql.exec(
              "SELECT purpose, write_units FROM ingress_purpose WHERE utc_day = ?",
              today
            )].map((row) => [row.purpose, Number(row.write_units) || 0])
            : []
        );
        const used = usage.get(purpose) || 0;
        const daily = Number(config.daily_write_units);
        const otherReservations = Object.entries(reservations).reduce((sum, [other, cfg]) => {
          if (other === purpose || other.startsWith("_")) return sum;
          const reserved = Number(cfg?.reserved_write_units);
          if (!Number.isFinite(reserved) || reserved <= 0) return sum;
          return sum + Math.max(0, reserved - (usage.get(other) || 0));
        }, 0);
        const globalAvailable = this._maxIngressWriteUnitsPerUtcDay() - otherReservations - unitsToday;
        const laneAvailable = Number.isFinite(daily) && daily >= 0 ? daily - used : Infinity;
        // The smallest job this lane submits (compiled from its model list); 3 + 1 by default.
        const minUnits = Number(config.write_units_per_job) || 4;
        if (laneAvailable < minUnits) reasons.push("purpose_write_budget_exceeded");
        if (globalAvailable < minUnits) reasons.push("ingress_write_budget_reserved");
        lane = {
          write_units_used: used,
          daily_write_units: Number.isFinite(daily) ? daily : null,
          write_units_available: Math.max(0, Math.min(laneAvailable, globalAvailable)),
        };
      }
    }
    return {
      open: reasons.length === 0,
      reasons,
      purpose: purpose || null,
      row_budget: rowBudget,
      queued_jobs: queued,
      max_queued_jobs: maxQueued,
      jobs_ingested_today: jobsToday,
      max_jobs_per_day: maxJobs,
      lane,
    };
  }

  /** Global cap on queued jobs: new work is refused once this many are waiting. */
  _maxQueuedJobs() {
    return this._envInt("MAX_QUEUED_JOBS", 20000);
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
        purpose                     TEXT NOT NULL DEFAULT '',
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
        schema_retry_count          INTEGER NOT NULL DEFAULT 0,
        transient_retry_count       INTEGER NOT NULL DEFAULT 0,
        token_reservation           INTEGER NOT NULL DEFAULT 0,
        reservation_rpm_window_start INTEGER NOT NULL DEFAULT 0,
        reservation_rpd_day_key     TEXT NOT NULL DEFAULT '',
        reservation_tpm_window_start INTEGER NOT NULL DEFAULT 0,
        created_at                  INTEGER NOT NULL,
        updated_at                  INTEGER NOT NULL
      );
      -- The ONE state index on jobs. Every state-filtered query seeks on it: purgePendingBatch
      -- and terminalFeed by (state, updated_at[, id]) -- an (state, priority, created_at) index
      -- forced them to read EVERY completed/failed row (60,189 -> 367 VDBE ops at 6,000 terminal
      -- jobs, the 2026-08-27 rows-read overage) -- and the queued/leased lookups by state alone.
      --
      -- Each index on jobs is a billed row written on every insert and every state change, and a
      -- job changes state ~4 times, so jobs deliberately carries no other secondary index beyond
      -- its id/idempotency_key uniqueness. Three were dropped on 2026-09-23 (see _initSchema):
      -- (state, updated_at), an exact prefix of this one; (state, priority, created_at), obsolete
      -- since job_models took over admission ordering; and (purpose, state, created_at), which no
      -- query used. Measured under workerd: 44.3 -> 30.3 billed rows per completed job.
      CREATE INDEX IF NOT EXISTS idx_jobs_state_updated_id
        ON jobs (state, updated_at, id);

      -- A queued job can be compatible with more than one model. This small index is the
      -- scheduler's route-independent work index: admission asks for a few jobs belonging to a
      -- capacity-ranked model rather than reading an arbitrary prefix of the whole queue.
      --
      -- Clustered on the admission scan order (WITHOUT ROWID), so the claim reads it directly and
      -- an insert writes 2 billed rows (the row + the (job_id, model) index) instead of 3. The
      -- UNIQUE index keeps one row per job/model and serves deletes by job_id. Coordinators
      -- created before 2026-09-23 are rebuilt into this shape by _migrateJobModelsClustered.
      CREATE TABLE IF NOT EXISTS job_models (
        job_id      TEXT NOT NULL,
        model       TEXT NOT NULL,
        priority    INTEGER NOT NULL,
        created_at  INTEGER NOT NULL,
        PRIMARY KEY (model, priority, created_at, job_id)
      ) WITHOUT ROWID;

      CREATE TABLE IF NOT EXISTS routes (
        route_id                TEXT PRIMARY KEY,
        rpm_window_start        INTEGER NOT NULL DEFAULT 0,
        rpm_count               INTEGER NOT NULL DEFAULT 0,
        rpd_window_start        INTEGER NOT NULL DEFAULT 0,
        rpd_count               INTEGER NOT NULL DEFAULT 0,
        rpd_day_key             TEXT NOT NULL DEFAULT '',
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
        buffer_updated_at        INTEGER NOT NULL DEFAULT 0,
        payment_required_streak  INTEGER NOT NULL DEFAULT 0,
        upstream_capacity_streak INTEGER NOT NULL DEFAULT 0,
        last_failure_class       TEXT NOT NULL DEFAULT ''
      );

      CREATE TABLE IF NOT EXISTS providers (
        provider                TEXT PRIMARY KEY,
        tpm_window_start        INTEGER NOT NULL DEFAULT 0,
        tpm_reserved            INTEGER NOT NULL DEFAULT 0,
        full_token_budget       REAL NOT NULL DEFAULT 0,
        token_budget_updated_at INTEGER NOT NULL DEFAULT 0
      );

      CREATE TABLE IF NOT EXISTS bundles (
        bundle_id            TEXT PRIMARY KEY,
        execution_token      TEXT NOT NULL,
        state                TEXT NOT NULL CHECK (state IN ('active','completed','expired')),
        lease_expires_at     INTEGER NOT NULL,
        active_call_count    INTEGER NOT NULL DEFAULT 0,
        dispatch_window_end  INTEGER NOT NULL,
        created_at           INTEGER NOT NULL
      ) WITHOUT ROWID;
      -- WITHOUT ROWID (2026-09-23): a rowid table stores the TEXT primary key in a separate
      -- autoindex, so every bundle insert/delete cost one extra billed row. Bundles created
      -- before then are rebuilt by _migrateBundlesClustered.

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

      -- No created_at index: _pruneTerminalRecords reads attempts oldest-first by rowid
      -- (insertion order) with a bounded LIMIT, and an index here cost a billed row per attempt.

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
        queued_job_count                    INTEGER NOT NULL DEFAULT 0,
        queued_job_count_initialized        INTEGER NOT NULL DEFAULT 0,
        ingress_write_units_today           INTEGER NOT NULL DEFAULT 0,
        rows_written_today                  INTEGER NOT NULL DEFAULT 0,
        claim_empty_count_today             INTEGER NOT NULL DEFAULT 0,
        claim_reason_counts_json            TEXT NOT NULL DEFAULT '{}',
        last_claim_at                       INTEGER,
        last_claim_result                   TEXT NOT NULL DEFAULT '',
        last_claim_reason                   TEXT NOT NULL DEFAULT '',
        last_claim_diagnostics_json         TEXT NOT NULL DEFAULT '{}',
        cleanup_cursor                      TEXT,
        next_maintenance_alarm_at           INTEGER
      );

      -- One small row per purpose/day makes ingress reservations durable without turning a
      -- batch admission check into a scan of the jobs table.  The global scheduler counter
      -- remains the circuit breaker; these rows keep bulk work from consuming capacity held for
      -- other feature lanes.
      CREATE TABLE IF NOT EXISTS ingress_purpose (
        utc_day                             TEXT NOT NULL,
        purpose                             TEXT NOT NULL,
        jobs_ingested                       INTEGER NOT NULL DEFAULT 0,
        write_units                         INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (utc_day, purpose)
      );

      -- Bounded per-class telemetry table (Initiative 20 / review/45 §20.7).
      -- Keyed so it can never grow with traffic (bounded at 65 routes * 9 classes
      -- * retention days).
      CREATE TABLE IF NOT EXISTS route_failures (
        utc_day       TEXT    NOT NULL,
        route_id      TEXT    NOT NULL,
        failure_class TEXT    NOT NULL,
        count         INTEGER NOT NULL DEFAULT 0,
        last_status   INTEGER,
        last_seen_at  INTEGER NOT NULL,
        PRIMARY KEY (utc_day, route_id, failure_class)
      );

      -- Operator dispatch pauses (POST /v2/dispatch:pause). One row per active scope --
      -- 'global', 'provider:<name>' or 'route:<route_id>' -- so the table stays at a handful of
      -- rows. Every pause carries paused_until: a pause ends by itself, so a probe that crashes
      -- mid-run cannot leave production halted. Written only by pause/resume; the claim path reads
      -- an in-memory copy (see _pauseRows) and never touches this table per tick.
      CREATE TABLE IF NOT EXISTS dispatch_pause (
        scope         TEXT PRIMARY KEY,
        paused_until  INTEGER NOT NULL,
        reason        TEXT NOT NULL DEFAULT '',
        created_at    INTEGER NOT NULL
      );

      -- Keeps the model-queue index's ordering priority synchronized with a direct jobs.priority
      -- edit -- review/44 documents an operator promoting an already-queued job for
      -- recovery/testing as a direct SQLite edit through Cloudflare's dashboard Data Studio or
      -- wrangler dev's Local Explorer SQL Studio. job_models.priority is otherwise only ever set
      -- once, at enqueue/backfill time, so without this trigger such a promotion would silently
      -- never change admission order: the index row already exists, so backfill never revisits
      -- it either. A no-op UPDATE (0 rows matched) when the job isn't currently indexed -- e.g.
      -- already claimed -- is expected and harmless.
      ${JOB_PRIORITY_SYNC_TRIGGER}
    `);

    // CREATE TABLE IF NOT EXISTS only creates the table on its first-ever run for this DO
    // instance; it does not retroactively add a column introduced later (rpd_window_start/
    // rpd_count, added alongside Phase 2's claimDispatchWindow) to a `routes` table an earlier
    // deploy already created. Defensive, cheap, and a no-op on a fresh instance.
    this._migrateJobModelsClustered();
    this._migrateBundlesClustered();
    sql.exec(
      "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_models_job_model ON job_models (job_id, model)"
    );
    this._ensureColumn("routes", "rpd_window_start", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("routes", "rpd_count", "INTEGER NOT NULL DEFAULT 0");
    // Daily quotas reset on the provider's calendar day, not 24h after first use.
    this._ensureColumn("routes", "rpd_day_key", "TEXT NOT NULL DEFAULT ''");
    this._ensureColumn("routes", "payment_required_streak", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("routes", "upstream_capacity_streak", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("routes", "last_failure_class", "TEXT NOT NULL DEFAULT ''");
    // buffer_seconds needs a timestamp to decay against; without one it was a permanent kill
    // switch (see pacing.js's effectiveBufferSeconds). Existing rows get 0, which reads as
    // "fully elapsed" -- deliberately, since those are the routes stuck under the old behaviour.
    this._ensureColumn("routes", "buffer_updated_at", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("jobs", "token_reservation", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("jobs", "purpose", "TEXT NOT NULL DEFAULT ''");
    // Backup-model gating (routes.js's backupModelsActive) reads this alongside `attempts`; an
    // already-provisioned DO's existing rows get 0, same as a freshly-created job.
    this._ensureColumn("jobs", "schema_retry_count", "INTEGER NOT NULL DEFAULT 0");
    // The route's own rpm/rpd/tpm window identity AT CLAIM TIME, so a non-consuming refund
    // (completeBatch) can tell whether the route's current window is still the one this
    // reservation actually counted against, or whether it has since rolled over (see
    // claimDispatchWindow's write site and completeBatch's refund for the full reasoning).
    // Existing leased rows get '' / 0, which never matches a real window identity -- exactly the
    // conservative direction: an in-flight reservation from before this migration simply skips
    // the counter decrement on refund rather than risking a wrong one.
    this._ensureColumn("jobs", "reservation_rpm_window_start", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("jobs", "reservation_rpd_day_key", "TEXT NOT NULL DEFAULT ''");
    this._ensureColumn("jobs", "reservation_tpm_window_start", "INTEGER NOT NULL DEFAULT 0");
    // Retired secondary indexes on jobs (2026-09-23): each cost a billed row on every insert and
    // state change, and no query needs them -- see the idx_jobs_state_updated_id comment. Dropping
    // an index is a schema change, not a row write.
    for (const index of [
      "idx_jobs_state_updated",
      "idx_jobs_state_priority_created",
      "idx_jobs_purpose_state_created",
      // attempts are pruned oldest-first by rowid (insertion order) -- see _pruneTerminalRecords.
      "idx_attempts_created",
    ]) {
      sql.exec(`DROP INDEX IF EXISTS ${index}`);
    }
    this._ensureColumn("scheduler", "ingress_write_units_today", "INTEGER NOT NULL DEFAULT 0");
    // A recurring producer snapshot needs the queue depth, but COUNT(*) over a retained queue
    // makes that diagnostic proportional to backlog. These two columns turn it into a singleton
    // scheduler-row read. Existing DOs initialize exactly once on their next mutating RPC or
    // snapshot; fresh DOs start at zero and explicit deltas (see the note below) maintain it.
    this._ensureColumn("scheduler", "queued_job_count", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("scheduler", "queued_job_count_initialized", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("scheduler", "claim_empty_count_today", "INTEGER NOT NULL DEFAULT 0");
    this._ensureColumn("scheduler", "lease_count_today", "INTEGER NOT NULL DEFAULT 0");
    const addedRowCounter = this._ensureColumn(
      "scheduler",
      "rows_written_today",
      "INTEGER NOT NULL DEFAULT 0"
    );
    this._ensureColumn("scheduler", "claim_reason_counts_json", "TEXT NOT NULL DEFAULT '{}'");
    this._ensureColumn("scheduler", "last_claim_at", "INTEGER");
    this._ensureColumn("scheduler", "last_claim_result", "TEXT NOT NULL DEFAULT ''");
    this._ensureColumn("scheduler", "last_claim_reason", "TEXT NOT NULL DEFAULT ''");
    this._ensureColumn("scheduler", "last_claim_diagnostics_json", "TEXT NOT NULL DEFAULT '{}'");
    // The queued-job counter used to be maintained by three per-row triggers, which cost a billed
    // row on every insert, lease and requeue (~2 per job). It is now maintained explicitly inside
    // scheduler UPDATEs the hot paths already make (enqueue, claim, completion requeues, cancels,
    // schema retries), and recounted exactly once an hour by scheduled cleanup
    // (recountQueuedJobs), which also heals rare paths and direct Data Studio edits. The count is diagnostic only (stats and
    // the empty-claim reason), never an admission input.
    for (const trigger of [
      "trg_jobs_queued_count_insert",
      "trg_jobs_queued_count_delete",
      "trg_jobs_queued_count_state",
    ]) {
      sql.exec(`DROP TRIGGER IF EXISTS ${trigger}`);
    }
    // The job_models_backfill_*/legacy_retryable_recovery_*/migration_*_today columns that used to
    // be retrofitted here were the one-time compatibility migration's own bookkeeping (review/44's
    // "Durable Objects rows-read overage retrospective"). Both migrations completed in production
    // (confirmed 2026-08-28 via a Data Studio query showing both *_complete flags = 1) and their
    // code was retired; the columns themselves are left in place on already-migrated `scheduler`
    // rows rather than dropped -- an unused column on a single-row table costs nothing, and
    // ALTER TABLE ... DROP COLUMN against live production data is an unnecessary risk for zero
    // benefit.

    const today = new Date().toISOString().slice(0, 10);
    const existing = [...sql.exec("SELECT id FROM scheduler WHERE id = 1")];
    if (existing.length === 0) {
      sql.exec(
        `INSERT INTO scheduler (id, utc_day, bundle_count_today, jobs_ingested_today)
         VALUES (1, ?, 0, 0)`,
        today
      );
    } else if (addedRowCounter) {
      this._seedRowCounter(today);
    }
    this._ensureColumn("scheduler", "mistral_latest_migrated", "INTEGER NOT NULL DEFAULT 0");
    this._ensureMigratedJobModels();
  }

  /**
   * One-time rebuild of a pre-2026-09-23 job_models (rowid table, PRIMARY KEY (job_id, model)
   * plus a separate (model, priority, created_at, job_id) index = 3 billed rows per insert) into
   * the clustered WITHOUT ROWID shape (2 rows per insert). job_models only ever holds QUEUED
   * work, so the copy is bounded by the queue (a few thousand rows, ~2 billed rows each, once).
   * Runs inside one transaction; the trigger that references job_models resolves by name at
   * execution time, so it keeps working across the swap.
   */
  _migrateJobModelsClustered() {
    const sql = this._getSql();
    const [row] = [...sql.exec(
      "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'job_models'"
    )];
    if (!row || /WITHOUT\s+ROWID/i.test(String(row.sql))) return;
    this.ctx.storage.transactionSync(() => {
      sql.exec(`
        CREATE TABLE job_models_clustered (
          job_id      TEXT NOT NULL,
          model       TEXT NOT NULL,
          priority    INTEGER NOT NULL,
          created_at  INTEGER NOT NULL,
          PRIMARY KEY (model, priority, created_at, job_id)
        ) WITHOUT ROWID;
        INSERT OR IGNORE INTO job_models_clustered (job_id, model, priority, created_at)
          SELECT job_id, model, priority, created_at FROM job_models;
        DROP TRIGGER IF EXISTS trg_jobs_priority_sync;
        DROP TABLE job_models;
        ALTER TABLE job_models_clustered RENAME TO job_models;
        ${JOB_PRIORITY_SYNC_TRIGGER}
      `);
    });
  }

  /**
   * One-time rebuild of a pre-2026-09-23 rowid `bundles` table into the WITHOUT ROWID shape.
   * Copies only active and expired bundles: a completed bundle is now deleted at completion
   * (completeBatch), and the completed rows still on the old table were retained solely so
   * _pruneTerminalRecords could delete them after BUNDLE_RETENTION_DAYS -- nothing reads them.
   * Dropping them with the old table is cheaper than copying (~2 billed rows each) and then
   * pruning (~3 each) up to seven days of history. The copy is bounded by MAX_ACTIVE_BUNDLES plus
   * the few leases that expired inside the retention window.
   */
  _migrateBundlesClustered() {
    const sql = this._getSql();
    const [row] = [...sql.exec(
      "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'bundles'"
    )];
    if (!row || /WITHOUT\s+ROWID/i.test(String(row.sql))) return;
    this.ctx.storage.transactionSync(() => {
      sql.exec(`
        CREATE TABLE bundles_clustered (
          bundle_id            TEXT PRIMARY KEY,
          execution_token      TEXT NOT NULL,
          state                TEXT NOT NULL CHECK (state IN ('active','completed','expired')),
          lease_expires_at     INTEGER NOT NULL,
          active_call_count    INTEGER NOT NULL DEFAULT 0,
          dispatch_window_end  INTEGER NOT NULL,
          created_at           INTEGER NOT NULL
        ) WITHOUT ROWID;
        INSERT INTO bundles_clustered
          SELECT bundle_id, execution_token, state, lease_expires_at, active_call_count,
                 dispatch_window_end, created_at
          FROM bundles WHERE state IN ('active', 'expired');
        DROP TABLE bundles;
        ALTER TABLE bundles_clustered RENAME TO bundles;
        CREATE INDEX IF NOT EXISTS idx_bundles_state_created ON bundles (state, created_at);
      `);
    });
  }

  _ensureMigratedJobModels() {
    if (this._migratedJobModels) return;
    const sql = this._getSql();
    const schedulerRows = [...sql.exec("SELECT mistral_latest_migrated FROM scheduler WHERE id = 1")];
    if (schedulerRows.length > 0 && schedulerRows[0].mistral_latest_migrated === 1) {
      this._migratedJobModels = true;
      return;
    }
    sql.exec(`
      UPDATE OR IGNORE job_models SET model = 'mistral/mistral-medium-latest'
      WHERE model IN (
        'mistral/mistral-medium-2508',
        'mistral/mistral-medium-2505',
        'mistral/mistral-medium-3-5',
        'mistral-medium-2508',
        'mistral-medium-2505',
        'mistral-medium-3.5',
        'mistral-medium-latest'
      );
      DELETE FROM job_models WHERE model IN (
        'mistral/mistral-medium-2508',
        'mistral/mistral-medium-2505',
        'mistral/mistral-medium-3-5',
        'mistral-medium-2508',
        'mistral-medium-2505',
        'mistral-medium-3.5',
        'mistral-medium-latest'
      );
      UPDATE routes SET buffer_seconds = 0, buffer_updated_at = 0
      WHERE buffer_seconds > 0 AND buffer_updated_at = 0;
      UPDATE scheduler SET mistral_latest_migrated = 1 WHERE id = 1;
    `);
    this._migratedJobModels = true;
  }

  _ensureColumn(table, column, definition) {
    const ALLOWED_TABLES = new Set([
      "routes",
      "jobs",
      "bundles",
      "attempts",
      "estimates",
      "scheduler",
      "ingress_purpose",
      "route_failures",
    ]);
    const ALLOWED_COLUMNS = new Map([
      ["rpd_window_start", "INTEGER NOT NULL DEFAULT 0"],
      ["rpd_count", "INTEGER NOT NULL DEFAULT 0"],
      ["rpd_day_key", "TEXT NOT NULL DEFAULT ''"],
      ["payment_required_streak", "INTEGER NOT NULL DEFAULT 0"],
      ["upstream_capacity_streak", "INTEGER NOT NULL DEFAULT 0"],
      ["last_failure_class", "TEXT NOT NULL DEFAULT ''"],
      ["transient_retry_count", "INTEGER NOT NULL DEFAULT 0"],
      ["schema_retry_count", "INTEGER NOT NULL DEFAULT 0"],
      ["buffer_updated_at", "INTEGER NOT NULL DEFAULT 0"],
      ["token_reservation", "INTEGER NOT NULL DEFAULT 0"],
      ["purpose", "TEXT NOT NULL DEFAULT ''"],
      ["ingress_write_units_today", "INTEGER NOT NULL DEFAULT 0"],
      ["queued_job_count", "INTEGER NOT NULL DEFAULT 0"],
      ["queued_job_count_initialized", "INTEGER NOT NULL DEFAULT 0"],
      ["claim_empty_count_today", "INTEGER NOT NULL DEFAULT 0"],
      ["lease_count_today", "INTEGER NOT NULL DEFAULT 0"],
      ["rows_written_today", "INTEGER NOT NULL DEFAULT 0"],
      ["claim_reason_counts_json", "TEXT NOT NULL DEFAULT '{}'"],
      ["last_claim_at", "INTEGER"],
      ["last_claim_result", "TEXT NOT NULL DEFAULT ''"],
      ["last_claim_reason", "TEXT NOT NULL DEFAULT ''"],
      ["last_claim_diagnostics_json", "TEXT NOT NULL DEFAULT '{}'"],
      ["mistral_latest_migrated", "INTEGER NOT NULL DEFAULT 0"],
      ["reservation_rpm_window_start", "INTEGER NOT NULL DEFAULT 0"],
      ["reservation_rpd_day_key", "TEXT NOT NULL DEFAULT ''"],
      ["reservation_tpm_window_start", "INTEGER NOT NULL DEFAULT 0"],
    ]);
    if (!ALLOWED_TABLES.has(table) || ALLOWED_COLUMNS.get(column) !== definition) {
      throw new Error(`_ensureColumn rejected unallowed schema mutation: ${table}.${column} ${definition}`);
    }
    const sql = this._getSql();
    const columns = [...sql.exec(`PRAGMA table_info(${table})`)];
    if (columns.some((c) => c.name === column)) return false;
    sql.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
    return true;
  }

  /**
   * A deploy that adds the row counter mid-day would otherwise start today's count at 0 after
   * the DO had already written an unknown number of rows, and could let the day run past the
   * platform limit. Seed it from what the scheduler already recorded today, each at its
   * worst-case measured cost (write_budget.js), plus a full cleanup and idle-tick allowance for
   * the minutes elapsed. It over-counts, which only stops enqueues or claims early on that one day.
   */
  _seedRowCounter(today) {
    const sql = this._getSql();
    const sched = [...sql.exec(
      `SELECT utc_day, ingress_write_units_today, lease_count_today, bundle_count_today
         FROM scheduler WHERE id = 1`
    )][0];
    if (!sched || sched.utc_day !== today) return;
    const minutes = Math.floor((Date.now() - Date.parse(`${today}T00:00:00Z`)) / 60_000);
    const cleanupRuns = Math.floor(minutes / Math.max(1, this._envInt("CLEANUP_INTERVAL_MINUTES", 10)));
    const estimate =
      ROWS_PER_INGRESS_WRITE_UNIT * (Number(sched.ingress_write_units_today) || 0) +
      ROWS_PER_BUNDLE * (Number(sched.bundle_count_today) || 0) +
      ROWS_PER_LEASE_WORST * (Number(sched.lease_count_today) || 0) +
      ROWS_PER_CLEANUP_JOB * cleanupRuns * this._envInt("PURGE_BATCH_LIMIT", 15) +
      minutes;
    sql.exec("UPDATE scheduler SET rows_written_today = ? WHERE id = 1", estimate);
  }

  _currentUtcDay(now = Date.now()) {
    return new Date(now).toISOString().slice(0, 10);
  }

  _maxJobsPerUtcDay() {
    const configured = Number(this.env.MAX_JOBS_PER_UTC_DAY);
    return Number.isFinite(configured) && configured > 0 ? configured : 5000;
  }

  _maxIngressWriteUnitsPerUtcDay() {
    const configured = Number(this.env.MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY);
    // Historical deployments set only MAX_JOBS_PER_UTC_DAY.  Preserve that protective bound
    // until the explicit write budget is configured, using four writes/job as its conservative
    // estimate (job row, model pointer, purpose counter, scheduler counter).
    return Number.isFinite(configured) && configured > 0
      ? configured
      : this._maxJobsPerUtcDay() * 4;
  }

  /**
   * The per-purpose ingress budgets, compiled from config/site_config.yml's `llm_lanes` block by
   * scripts/compile_llm_lanes.py.
   *
   * This used to read a hand-maintained INGRESS_PURPOSE_RESERVATIONS env var, and its keys had
   * drifted to name purposes no client ever sends: `topic-tags` and `moments`, against real
   * purposes `topic-tags:tagger`, `topic-tags:prelabeler`, `r6-moments`, and `r6-judge`. Because
   * the admission arithmetic below subtracts every *other* purpose's reservation from the headroom
   * a job may use, those two unreachable keys withheld 10,000 of 30,000 daily write units from
   * every real lane while being unusable by the lanes they were meant to protect. Compiling from
   * the same block the Python client reads means the two halves cannot disagree about which
   * purposes exist.
   *
   * The env var is still honored as an explicit operator override for an incident (a reservation
   * can be widened without a client deploy), but it is no longer the source of truth and an
   * unparseable value is now a hard error rather than a silent `{}` -- an empty map reads as "no
   * lane has a reservation," which is precisely the failure this replaced.
   */
  _ingressPurposeReservations() {
    const raw = this.env.INGRESS_PURPOSE_RESERVATIONS;
    if (!raw) return INGRESS_RESERVATIONS.reservations;
    const parsed = typeof raw === "string" ? JSON.parse(raw) : raw;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("INGRESS_PURPOSE_RESERVATIONS override must be a JSON object");
    }
    return parsed;
  }

  /**
   * Whether `purpose` has a registered lane. An unregistered purpose is rejected at ingress
   * instead of drawing on unreserved shared headroom: silently absorbing a new verb/task is how
   * `topic-tags:prelabeler` came to compete with the production lanes for capacity nobody had
   * budgeted. The remedy is a new `llm_lanes` entry plus a recompile, which the rejection reason
   * names.
   */
  _purposeIsRegistered(purpose) {
    return Object.hasOwn(this._ingressPurposeReservations(), purpose);
  }

  /**
   * Models a job asks for that its own lane never declared, canonicalized on both sides.
   *
   * Registering the purpose is only half the contract. `_modelsForQueuedJob` indexes whatever
   * `allowed_models` the policy carries, so a job stamped with a registered purpose could be
   * claimed on a route that lane's `llm_lanes` entry does not list -- spending a budget sized for
   * one set of routes on another, and making the compiled map's `models` a description of intent
   * rather than of what actually runs. The producer already resolves its routes from
   * `lane_for(...)`; this is the ingress-side half of that, so a hand-run `--models` override or a
   * stale client cannot quietly widen a lane.
   *
   * Returns `[]` (admit) when the lane declares no `models` at all: the env-var override exists to
   * let an operator reshape budgets without a redeploy and need not restate route lists, and an
   * absent list must never be read as "no route is allowed".
   */
  _modelsOutsideLane(job, reservation, dispatchLimits) {
    // backup_models is enforced identically to models -- otherwise a lane could smuggle an
    // unbudgeted/unreviewed model into production via backup_models alone, which this ingress gate
    // would otherwise never see.
    const declared = [
      ...(Array.isArray(reservation?.models) ? reservation.models : []),
      ...(Array.isArray(reservation?.backup_models) ? reservation.backup_models : []),
    ];
    if (declared.length === 0) return [];
    const allowed = new Set(
      declared
        .filter((model) => typeof model === "string" && model.trim() !== "")
        .map((model) => canonicalModelName(model.trim(), dispatchLimits))
    );
    if (allowed.size === 0) return [];
    let policy;
    try {
      policy = typeof job.policy_json === "string" ? JSON.parse(job.policy_json) : job.policy_json;
    } catch {
      return [];
    }
    const requested = [
      ...(Array.isArray(policy?.allowed_models) ? policy.allowed_models : []),
      ...(Array.isArray(policy?.backup_models) ? policy.backup_models : []),
    ];
    const offenders = [];
    for (const rawModel of requested) {
      if (typeof rawModel !== "string" || rawModel.trim() === "") continue;
      if (!allowed.has(canonicalModelName(rawModel.trim(), dispatchLimits))) {
        offenders.push(rawModel.trim());
      }
    }
    return offenders;
  }

  /**
   * A registered purpose whose lane declares `backup_models` still may not activate them earlier
   * than the lane's own configured `backup_after_attempts` -- `_modelsOutsideLane` above only
   * enforces WHICH models a job may name, never WHEN a caller's own `policy_json` says they
   * activate. `validateEnqueueJob` never validates `policy_json` and the coordinator otherwise
   * only checks purpose/model allowlists, so without this a producer could pass a lower threshold
   * than the lane declares and unlock a reviewed/budgeted backup model far sooner than intended.
   * `schema_retry_count`'s independent activation trigger (routes.js's `backupModelsActive`) is
   * untouched -- this only floors the attempts-based threshold.
   *
   * Returns `false` (admit) when the job names no backup models, or when the lane itself declares
   * no `backup_after_attempts` floor to enforce.
   */
  _backupThresholdBelowLaneMinimum(job, reservation) {
    let policy;
    try {
      policy = typeof job.policy_json === "string" ? JSON.parse(job.policy_json) : job.policy_json;
    } catch {
      return false;
    }
    const requestedBackupModels = Array.isArray(policy?.backup_models) ? policy.backup_models : [];
    if (requestedBackupModels.length === 0) return false;
    const laneThreshold = reservation?.backup_after_attempts;
    if (!Number.isInteger(laneThreshold) || laneThreshold <= 0) return false;
    const requestedThreshold = policy?.backup_after_attempts;
    return !Number.isInteger(requestedThreshold) || requestedThreshold < laneThreshold;
  }

  _purposeForJob(job) {
    if (typeof job.purpose === "string" && job.purpose.trim()) return job.purpose.trim();
    try {
      const policy = typeof job.policy_json === "string" ? JSON.parse(job.policy_json) : job.policy_json;
      return typeof policy?.purpose === "string" && policy.purpose.trim()
        ? policy.purpose.trim()
        : "unspecified";
    } catch {
      return "unspecified";
    }
  }

  _ingressWriteUnitsFor(job) {
    // jobs + every model-index row + purpose-ledger + scheduler update. A batch performs only
    // one scheduler update, but charging one per job is the conservative circuit-breaker choice.
    return 3 + this._modelsToIndex(job).length;
  }

  _envInt(name, fallback) {
    const configured = Number(this.env[name]);
    return Number.isFinite(configured) && configured >= 0 ? configured : fallback;
  }

  /** The dispatch window `capacity` in stats() is scored against, matching what the Worker passes
   * to claimDispatchWindow so the reported number is the one the scheduler actually ranks on. */
  _dispatchWindowSecondsForStats() {
    return this._envInt("DISPATCH_WINDOW_SECONDS", 25);
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

  /** How far down one model's queue the claim may look past jobs no available route can take. */
  _candidateLookahead() {
    return this._envInt("MAX_CANDIDATE_LOOKAHEAD", 32);
  }

  /**
   * Whether every route of this model that currently scores capacity carries a size ceiling --
   * i.e. whether some queued jobs may be too large for all of them right now, so the claim should
   * read a bounded lookahead rather than only the queue head.
   */
  _modelIsCeilingBound(modelPlan, dispatchLimits) {
    let live = 0;
    for (const [routeId, score] of modelPlan.routeScores || []) {
      if (!(score > 0)) continue;
      const ceiling = Number(dispatchLimits?.routes_by_id?.[routeId]?.hard_input_ceiling);
      if (!(Number.isFinite(ceiling) && ceiling > 0)) return false;
      live += 1;
    }
    return live > 0;
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

  _maxLeasesPerUtcDay() {
    // A blast-radius backstop only; the daily row thresholds are the working limit.
    return this._envInt("MAX_LEASES_PER_UTC_DAY", 7000);
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

  _upstreamCapacityCooldownSeconds() {
    return this._envInt("UPSTREAM_CAPACITY_COOLDOWN_SECONDS", 15);
  }

  _upstreamCapacityMaxCooldownSeconds() {
    return this._envInt("UPSTREAM_CAPACITY_MAX_COOLDOWN_SECONDS", 300);
  }

  /** Return an exponential route cooldown with randomized jitter for upstream_capacity/
   * gateway_limit -- factored out so authorizeRetry (mid-lease 429) and completeBatch (a final,
   * non-429 upstream_capacity outcome such as an empty 2xx) apply the identical backoff instead
   * of drifting apart. */
  _upstreamCapacityBlockedUntil(streak, now) {
    const baseCooldown = this._upstreamCapacityCooldownSeconds();
    const maxCooldown = this._upstreamCapacityMaxCooldownSeconds();
    const expCooldown = Math.min(maxCooldown, baseCooldown * Math.pow(2, streak - 1));
    return now + Math.ceil(expCooldown * 1000 * (1 + Math.random() * 0.5));
  }

  /** AI Gateway already made its own short retry series, so this is a small durable outer budget. */
  /** Retry budget for failures the provider caused (upstream saturation, gateway limits).
   * Larger than the 5xx budget on purpose: these clear on their own and cost us nothing to wait
   * out, whereas failing the job throws away work that was already admitted and paid for. */
  _maxUpstreamCapacityRetries() {
    return this._envInt("MAX_UPSTREAM_CAPACITY_RETRIES", 8);
  }

  _max5xxRetries() {
    return this._envInt("MAX_5XX_RETRIES", 1);
  }

  /** Maximum route-only cooldown applied after a final post-Gateway 5xx. */
  _max5xxBackoffMs() {
    return this._envInt("MAX_5XX_BACKOFF_SECONDS", 300) * 1000;
  }

  /**
   * Retention for the DO's two append-only bookkeeping tables. Neither `bundles` nor `attempts`
   * has any B2 counterpart or client-side dependency -- they are internal scheduler history, so
   * unlike `jobs` (whose row must outlive the client's result fetch, see purgePendingBatch)
   * they can be aged out entirely inside the DO with no coordination. Kept long enough to stay
   * useful for incident diagnosis, short enough that neither table grows without bound.
   * Since 2026-09-23 a bundle is deleted when its last job settles (completeBatch), so bundle
   * retention now applies only to bundles whose lease expired unreported.
   */
  _bundleRetentionMs() {
    return this._envInt("BUNDLE_RETENTION_DAYS", 7) * 86_400_000;
  }

  _attemptRetentionMs() {
    return this._envInt("ATTEMPT_RETENTION_DAYS", 7) * 86_400_000;
  }

  /** Per-tick delete budget for each table. Deletes are row WRITES, so this bounds the drain of
   * an existing backlog (and steady-state upkeep needs only a row or two per tick). Zero on
   * either is an emergency pause. */
  _maxBundlePrunePerTick() {
    return this._envInt("MAX_BUNDLE_PRUNE_PER_TICK", 50);
  }

  _maxAttemptPrunePerTick() {
    return this._envInt("MAX_ATTEMPT_PRUNE_PER_TICK", 50);
  }

  /** Return an exponential route cooldown with randomized jitter for a final Gateway 5xx. */
  _5xxBlockedUntil(retryCount, now) {
    const baseMs = 60_000;
    const baseDelayMs = Math.min(baseMs * 2 ** Math.max(0, retryCount - 1), this._max5xxBackoffMs());
    const jitter = Math.random() * 0.5;
    return now + Math.round(baseDelayMs * (1.0 + jitter));
  }

  /** How long a route answering 410 Gone (model retired) stays stood down. */
  _routeUnavailableBlockMs() {
    return this._envInt("ROUTE_UNAVAILABLE_BLOCK_SECONDS", 21600) * 1000;
  }

  _maxRouteBufferSeconds() {
    return this._envInt("MAX_ROUTE_BUFFER_SECONDS", 120);
  }

  /** The real compiled catalog, unless a test has injected its own via the (object-valued, so
   * never confusable with a real Cloudflare string env var) env.DISPATCH_LIMITS_OVERRIDE. */
  _dispatchLimits() {
    return this.env.DISPATCH_LIMITS_OVERRIDE || DISPATCH_LIMITS;
  }

  // ---------------------------------------------------------------------------------------------
  // Dispatch pause (POST /v2/dispatch:pause|resume, GET /v2/dispatch:pause-status,
  // POST /v2/dispatch:reserve). Lets an out-of-band caller -- a catalog canary, a rate probe, an
  // incident responder -- stop NEW claims for a whole deployment, one provider, or one route
  // without a redeploy, then wait for that selection's in-flight work to drain. In-flight bundles
  // are never interrupted and their 429 retries are not refused: a refused retry ends the attempt
  // as terminal_error, which would fail the job rather than pause it. The drain signal
  // (leased jobs for the selection) already waits for those retries to finish.
  //
  // Row budget: only pause, resume and reserve write, one or two rows each. A claim tick reads the
  // in-memory copy below, and a globally paused tick returns before any other statement runs.
  // ---------------------------------------------------------------------------------------------

  static PAUSE_MAX_SECONDS = 3600;

  /** `global`, `provider:<name>` or `route:<route_id>`. */
  static pauseScopeKey(scope, target) {
    return scope === "global" ? "global" : `${scope}:${target}`;
  }

  /** Every stored pause row, cached in memory; pause/resume clear the cache after writing. */
  _pauseRows() {
    if (!this._pauseCache) {
      this._pauseCache = [...this._getSql().exec(
        "SELECT scope, paused_until, reason, created_at FROM dispatch_pause"
      )];
    }
    return this._pauseCache;
  }

  _activePauses(now) {
    return this._pauseRows().filter((row) => Number(row.paused_until) > now);
  }

  /** Reject a pause target the compiled catalog does not know: a typo must not pause nothing. */
  _validatePauseTarget(scope, target) {
    if (scope === "global") return null;
    const catalog = this._dispatchLimits();
    // Own properties only: `constructor`/`toString` resolve through Object.prototype and would
    // otherwise pass as a "known" target (and reserve would write a ledger row under that name).
    if (scope === "provider" && !(catalog?.providers && Object.hasOwn(catalog.providers, target))) {
      return `unknown provider '${target}'`;
    }
    if (
      scope === "route" &&
      !(catalog?.routes_by_id && Object.hasOwn(catalog.routes_by_id, target))
    ) {
      return `unknown route '${target}'`;
    }
    return null;
  }

  async pauseDispatch({ scope, target = null, seconds, reason = "" }, now = Date.now()) {
    const invalid = this._validatePauseTarget(scope, target);
    if (invalid) return { ok: false, error: "unknown_target", detail: invalid };
    const key = LLMSchedulerDO.pauseScopeKey(scope, target);
    const pausedUntil = now + Math.min(seconds, LLMSchedulerDO.PAUSE_MAX_SECONDS) * 1000;
    const sql = this._getSql();
    this.ctx.storage.transactionSync(() => {
      // Expired rows are removed lazily here rather than by a scheduled sweep: the table only
      // ever holds a few scopes, and this keeps pausing the only thing that writes it.
      sql.exec("DELETE FROM dispatch_pause WHERE paused_until <= ?", now);
      sql.exec(
        `INSERT INTO dispatch_pause (scope, paused_until, reason, created_at) VALUES (?, ?, ?, ?)
         ON CONFLICT(scope) DO UPDATE SET paused_until = excluded.paused_until,
                                          reason = excluded.reason,
                                          created_at = excluded.created_at`,
        key,
        pausedUntil,
        String(reason || "").slice(0, 200),
        now
      );
    });
    this._pauseCache = null;
    return { ok: true, scope: key, paused_until: pausedUntil };
  }

  async resumeDispatch({ scope, target = null }, now = Date.now()) {
    const key = LLMSchedulerDO.pauseScopeKey(scope, target);
    const sql = this._getSql();
    const existed = this._activePauses(now).some((row) => row.scope === key);
    this.ctx.storage.transactionSync(() => {
      sql.exec("DELETE FROM dispatch_pause WHERE scope = ? OR paused_until <= ?", key, now);
    });
    this._pauseCache = null;
    return { ok: true, scope: key, resumed: existed };
  }

  /**
   * The catalog claimDispatchWindow routes against. With no provider/route pause active this is
   * the compiled catalog itself; otherwise a copy whose model_routes_map omits paused routes, so
   * _rankModelsByCapacity and routesEligibleFor skip them without any pause-specific branches.
   * routes_by_id stays whole: in-flight leases on a paused route must still resolve.
   */
  _claimDispatchLimits(now) {
    const catalog = this._dispatchLimits();
    const pausedProviders = new Set();
    const pausedRoutes = new Set();
    for (const row of this._activePauses(now)) {
      if (row.scope.startsWith("provider:")) pausedProviders.add(row.scope.slice(9));
      else if (row.scope.startsWith("route:")) pausedRoutes.add(row.scope.slice(6));
    }
    if (pausedProviders.size === 0 && pausedRoutes.size === 0) return catalog;
    const isPaused = (routeId) =>
      pausedRoutes.has(routeId) ||
      pausedProviders.has(catalog?.routes_by_id?.[routeId]?.provider);
    const modelRoutesMap = {};
    for (const [model, routeIds] of Object.entries(catalog?.model_routes_map || {})) {
      modelRoutesMap[model] = (routeIds || []).filter((routeId) => !isPaused(routeId));
    }
    return { ...catalog, model_routes_map: modelRoutesMap };
  }

  /**
   * Live leased jobs per route and per provider: the calls that are, or are about to be, in flight.
   * An expired lease is a dead bundle (crash/eviction), not a call -- the claim-time reaper uses
   * the same `lease_expires_at` test -- and a global pause skips that reaper, so counting expired
   * leases would hold the drain signal above zero until the pause itself ran out.
   */
  _inFlightCounts(now, dispatchLimits = this._dispatchLimits()) {
    const byRoute = {};
    const byProvider = {};
    for (const row of this._getSql().exec(
      `SELECT lease_route_id, COUNT(*) AS cnt FROM jobs
        WHERE state = 'leased' AND lease_expires_at > ? GROUP BY lease_route_id`,
      now
    )) {
      if (!row.lease_route_id) continue;
      byRoute[row.lease_route_id] = row.cnt;
      const provider = dispatchLimits?.routes_by_id?.[row.lease_route_id]?.provider;
      if (provider) byProvider[provider] = (byProvider[provider] || 0) + row.cnt;
    }
    return { by_route: byRoute, by_provider: byProvider };
  }

  /**
   * Status for one selection: active pauses, the selection's in-flight count (the drain signal a
   * canary waits on) and, per selected route, how much daily quota is left and when it resets.
   * Read-only.
   */
  async dispatchPauseStatus({ scope = "global", target = null } = {}, now = Date.now()) {
    const invalid = this._validatePauseTarget(scope, target);
    if (invalid) return { ok: false, error: "unknown_target", detail: invalid };
    const catalog = this._dispatchLimits();
    const inFlight = this._inFlightCounts(now, catalog);
    const selectedRouteIds = Object.keys(catalog?.routes_by_id || {}).filter((routeId) => {
      if (scope === "route") return routeId === target;
      if (scope === "provider") return catalog.routes_by_id[routeId]?.provider === target;
      return true;
    });
    let selectionInFlight;
    if (scope === "route") selectionInFlight = inFlight.by_route[target] || 0;
    else if (scope === "provider") selectionInFlight = inFlight.by_provider[target] || 0;
    else selectionInFlight = Object.values(inFlight.by_route).reduce((sum, n) => sum + n, 0);

    // Daily quota per route is reported only for a provider/route selection: a global status
    // would read every routes row for no caller that needs it.
    const routes = {};
    if (scope !== "global") {
      const ledgers = new Map(
        [...this._getSql().exec(
          `SELECT route_id, rpd_count, rpd_day_key FROM routes
            WHERE route_id IN (${selectedRouteIds.map(() => "?").join(",") || "''"})`,
          ...selectedRouteIds
        )].map((row) => [row.route_id, row])
      );
      for (const routeId of selectedRouteIds) {
        const catalogRoute = catalog.routes_by_id[routeId];
        const timeZone = catalog?.providers?.[catalogRoute.provider]?.reset_timezone || "UTC";
        const rpdLimit = configuredLimit(catalogRoute.rpd);
        const ledger = ledgers.get(routeId);
        const usedToday =
          ledger && ledger.rpd_day_key === zonedDateKey(now, timeZone)
            ? Number(ledger.rpd_count) || 0
            : 0;
        routes[routeId] = {
          provider: catalogRoute.provider,
          in_flight: inFlight.by_route[routeId] || 0,
          rpd_limit: rpdLimit,
          rpd_used: usedToday,
          rpd_remaining: rpdLimit === null ? null : Math.max(0, rpdLimit - usedToday),
          rpd_resets_at: nextZonedMidnightMs(now, timeZone),
        };
      }
    }
    return {
      ok: true,
      now,
      selection: LLMSchedulerDO.pauseScopeKey(scope, target),
      in_flight: selectionInFlight,
      pauses: this._activePauses(now),
      routes,
    };
  }

  /**
   * Charge out-of-band calls (a canary or probe made directly against the provider) to a route's
   * rpm/rpd ledger, exactly as claimDispatchWindow charges an admitted job, so production pacing
   * accounts for them. Tokens are not reserved: these calls are a few tokens each.
   */
  async reserveRouteRequests({ route_id: routeId, requests = 1 }, now = Date.now()) {
    const invalid = this._validatePauseTarget("route", routeId);
    if (invalid) return { ok: false, error: "unknown_target", detail: invalid };
    const catalog = this._dispatchLimits();
    const catalogRoute = catalog.routes_by_id[routeId];
    return this.ctx.storage.transactionSync(() => {
      let merged = {
        ...catalogRoute,
        ...this._getOrCreateRouteLedger(routeId, now, catalogRoute),
        route_id: routeId,
        reset_timezone: catalog?.providers?.[catalogRoute.provider]?.reset_timezone || "UTC",
      };
      for (let i = 0; i < requests; i += 1) {
        merged = this._applyProvisionalReservation(merged, 0, now);
      }
      this._writeRouteLedger(merged);
      return {
        ok: true,
        route_id: routeId,
        rpm_count: merged.rpm_count,
        rpd_count: merged.rpd_count,
        rpd_day_key: merged.rpd_day_key,
      };
    });
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
      `SELECT utc_day, bundle_count_today, lease_count_today, jobs_ingested_today,
              ingress_write_units_today, claim_empty_count_today, claim_reason_counts_json,
              rows_written_today, queued_job_count, last_claim_at,
              last_claim_result, last_claim_reason, last_claim_diagnostics_json
       FROM scheduler WHERE id = 1`
    )];
    if (rows.length === 0) {
      sql.exec(
        `INSERT INTO scheduler (
          id, utc_day, bundle_count_today, jobs_ingested_today, ingress_write_units_today,
          claim_empty_count_today, claim_reason_counts_json
        ) VALUES (1, ?, 0, 0, 0, 0, '{}')`,
        today
      );
      return {
        utc_day: today,
        bundle_count_today: 0,
        lease_count_today: 0,
        jobs_ingested_today: 0,
        ingress_write_units_today: 0,
        claim_empty_count_today: 0,
        claim_reason_counts_json: "{}",
        rows_written_today: 0,
        queued_job_count: 0,
      };
    }
    const sched = rows[0];
    if (sched.utc_day !== today) {
      sql.exec(
        `UPDATE scheduler SET utc_day = ?, bundle_count_today = 0, jobs_ingested_today = 0,
         ingress_write_units_today = 0, claim_empty_count_today = 0, lease_count_today = 0,
         rows_written_today = 0, claim_reason_counts_json = '{}' WHERE id = 1`,
        today
      );
      // Writes before midnight belong to yesterday's platform budget, not today's.
      this._rowsUnflushed = 0;
      return {
        ...sched,
        utc_day: today,
        bundle_count_today: 0,
        lease_count_today: 0,
        jobs_ingested_today: 0,
        ingress_write_units_today: 0,
        claim_empty_count_today: 0,
        claim_reason_counts_json: "{}",
        rows_written_today: 0,
      };
    }
    return sched;
  }

  /**
   * Backfill the queue counter once for an already-deployed scheduler, then leave all ordinary
   * mutations to the explicit deltas folded into scheduler UPDATEs (see _initSchema) and the
   * hourly recountQueuedJobs. The one COUNT(*) is an
   * intentional migration cost, not an RPC-path diagnostic: subsequent snapshots read only the
   * scheduler singleton. Must run before a transaction changes any queued-job state.
   */
  _ensureQueuedJobCounter() {
    const sql = this._getSql();
    const scheduler = [...sql.exec(
      "SELECT queued_job_count_initialized FROM scheduler WHERE id = 1"
    )][0];
    if (scheduler?.queued_job_count_initialized === 1) return;
    const queued = [...sql.exec(
      "SELECT COUNT(*) AS n FROM jobs WHERE state = 'queued'"
    )][0]?.n || 0;
    sql.exec(
      `UPDATE scheduler SET queued_job_count = ?, queued_job_count_initialized = 1
       WHERE id = 1`,
      queued
    );
  }

  async enqueueBatch(jobs) {
    const sql = this._getSql();
    const now = Date.now();
    const maxJobsToday = this._maxJobsPerUtcDay();
    const maxIngressWriteUnits = this._maxIngressWriteUnitsPerUtcDay();

    // The whole batch commits or rolls back as one unit -- see review/44 Unit 2 ("one SQLite
    // transaction for the whole batch"). ctx.storage.transactionSync requires its callback to
    // run fully synchronously (no await inside), which this loop already does.
    return this.ctx.storage.transactionSync(() => {
      this._ensureMigratedJobModels();
      this._ensureQueuedJobCounter();
      const accepted = [];
      const rejected = [];

      const sched = this._rollUtcDayIfNeeded(now);
      // Past the enqueue threshold the day's remaining rows are dispatch's: nothing new is
      // admitted (an exact idempotent replay still is -- it writes nothing).
      const rowBudgetClosed = this._rowsWrittenToday(sched) >= this._enqueueRowStop();
      const maxQueued = this._maxQueuedJobs();
      const queuedNow = Number(sched.queued_job_count) || 0;
      let jobsIngestedToday = sched.jobs_ingested_today;
      let ingressWriteUnits = sched.ingress_write_units_today || 0;
      const ingressReservations = this._ingressPurposeReservations();
      // Resolved once for the whole batch: both the lane-model check and the write-unit charge
      // canonicalize against it, and it is a static catalog for the life of the call.
      const dispatchLimits = this._dispatchLimits();
      const purposeDeltas = new Map();
      let queuedAdded = 0;
      const purposeUsage = new Map(
        [...sql.exec(
          "SELECT purpose, jobs_ingested, write_units FROM ingress_purpose WHERE utc_day = ?",
          sched.utc_day
        )].map((row) => [row.purpose, row])
      );

      let newlyInsertedCount = 0;
      let newlyAdmittedWriteUnits = 0;

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
          } else if (rowBudgetClosed) {
            rejected.push({ id: job.id, reason: "daily_row_budget" });
          } else if (row.state === "leased" || row.state === "unknown_attempt") {
            // A genuinely in-flight attempt is the one case superseding cannot safely cover: its
            // lease_token/bundle_id already reference the old payload, and overwriting the row
            // out from under a call that may still be running risks a result racing back against
            // content that no longer matches what was sent. Reject and let the caller retry once
            // this attempt settles.
            rejected.push({ id: job.id, reason: "idempotency_conflict" });
          } else {
            // Same identity (recipe_hash), different content: supersede the stale row rather
            // than reject it.
            //
            // idempotency_key is derived from the job's *recipe*; request_digest hashes the
            // *payload* actually sent. Any change to payload construction -- 2c3b2ab stopped
            // leaking policy-only fields into the literal provider request body -- changes the
            // digest for every job while every key stays identical. Rejecting that as a conflict
            // is indistinguishable from a real double-submission bug, but it produces a
            // permanent one: nothing ever deletes a non-terminal row, so the stale row blocks its
            // own key forever and the caller's real work never gets in. Production hit exactly
            // this on 2026-08-25: 100% of enqueue-batch calls rejected for five days.
            //
            // Safe for every state but the leased one just excluded above:
            //   - queued: nothing has touched this row yet; overwrite outright.
            //   - completed/failed: the old outcome was produced from payload construction we
            //     now know was wrong (the leaked-fields case failed at every provider with
            //     "Extra inputs are not permitted"); superseding is what lets it be tried again
            //     with the corrected request instead of standing as a permanent false failure.
            //   - purge_pending: safe because confirmPurge is itself guarded by
            //     `state = 'purge_pending'` -- if a purge is mid-flight for the OLD payload_key,
            //     supersede's state change here simply makes that confirmPurge a no-op, leaving
            //     this row correctly 'queued' under the NEW payload_key.
            //
            // The old payload_key (and result_key, if terminal) become unreferenced -- this
            // coordinator holds no B2 credentials to delete them (the same split that makes
            // purgePendingBatch two-phase), and they are small, content-addressed JSON blobs, not
            // the append-only audio artifacts the project's storage-reclaim tooling targets. An
            // orphaned KB-scale text blob is an accepted cost of not losing the job.
            const priority = job.priority !== undefined ? job.priority : 1;
            const policyJson = typeof job.policy_json === "string" ? job.policy_json : JSON.stringify(job.policy_json || {});
            const providerIdempotencyKey = job.provider_idempotency_key || null;
            const purpose = this._purposeForJob({ ...job, policy_json: policyJson });
            // The same gate the new-insert path applies below. Superseding consumes no admission
            // budget, which is exactly why it must not be a way around registration: a stale row
            // whose lane has since been removed (or renamed) would otherwise be resurrected into
            // `queued` under a purpose no reservation covers, and dispatched. The caller's remedy
            // is the same either way -- add the `llm_lanes` entry and recompile.
            if (!Object.hasOwn(ingressReservations, purpose)) {
              rejected.push({ id: job.id, reason: "purpose_not_registered", purpose });
              continue;
            }
            const supersedeOffenders = this._modelsOutsideLane(
              { ...job, policy_json: policyJson },
              ingressReservations[purpose],
              dispatchLimits
            );
            if (supersedeOffenders.length > 0) {
              rejected.push({
                id: job.id,
                reason: "model_not_in_lane",
                purpose,
                models: supersedeOffenders,
              });
              continue;
            }
            if (
              this._backupThresholdBelowLaneMinimum(
                { ...job, policy_json: policyJson },
                ingressReservations[purpose]
              )
            ) {
              rejected.push({
                id: job.id,
                reason: "backup_after_attempts_below_lane_minimum",
                purpose,
              });
              continue;
            }
            if (row.state !== "queued" && queuedNow + queuedAdded >= maxQueued) {
              rejected.push({ id: job.id, reason: "queue_full" });
              continue;
            }
            if (row.state !== "queued") queuedAdded += 1;
            sql.exec(
              `UPDATE jobs SET
                 request_digest = ?, provider_idempotency_key = ?, state = 'queued',
                 priority = ?, purpose = ?, policy_json = ?, prompt_family = ?,
                 input_token_estimate = ?, max_output_token_estimate = ?, payload_key = ?,
                 result_key = NULL, lease_token = NULL, lease_route_id = NULL,
                 lease_expires_at = NULL, bundle_id = NULL, attempts = 0,
                 transient_retry_count = 0, updated_at = ?
               WHERE id = ?`,
              job.request_digest,
              providerIdempotencyKey,
              priority,
              purpose,
              policyJson,
              job.prompt_family,
              job.input_token_estimate,
              job.max_output_token_estimate,
              job.payload_key,
              now,
              row.id
            );
            // The allowed-model set can itself have changed between the old and new payload, so
            // rebuild the index rather than trust whatever it already held (or didn't -- a
            // completed/failed row has none, since claiming deletes it).
            sql.exec("DELETE FROM job_models WHERE job_id = ?", row.id);
            this._indexQueuedJobModels(
              { ...job, id: row.id, policy_json: policyJson, priority, created_at: now },
              priority,
              now
            );
            // Superseding replaces a row that already existed; it is not new admission and must
            // not consume today's cap, for the same reason an idempotent replay doesn't.
            accepted.push({ id: row.id, submitted_id: job.id, superseded: true });
          }
          continue;
        }

        if (rowBudgetClosed) {
          rejected.push({ id: job.id, reason: "daily_row_budget" });
          continue;
        }
        // The pending-work cap: a backlog beyond what dispatch can drain in several days only
        // ages, so new work waits in the producer instead of in the DO.
        if (queuedNow + queuedAdded >= maxQueued) {
          rejected.push({ id: job.id, reason: "queue_full" });
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
        const purpose = this._purposeForJob({ ...job, policy_json: policyJson });
        // An unregistered purpose is rejected, never absorbed into unreserved headroom. Falling
        // through was how a new verb/task (a second judge, a new extractor) could quietly start
        // competing with the scheduled production lanes for capacity nobody had budgeted, with no
        // symptom until those lanes started missing their daily quota. The fix is a new
        // `llm_lanes` entry in config/site_config.yml plus `python scripts/compile_llm_lanes.py`.
        if (!Object.hasOwn(ingressReservations, purpose)) {
          rejected.push({ id: job.id, reason: "purpose_not_registered", purpose });
          continue;
        }
        const reservation = ingressReservations[purpose] || {};
        // A registered purpose still may not route outside its own lane -- see
        // `_modelsOutsideLane`. Checked before any budget arithmetic so a job that cannot legally
        // run never consumes the day's admission.
        const offendingModels = this._modelsOutsideLane(
          { ...job, policy_json: policyJson },
          reservation,
          dispatchLimits
        );
        if (offendingModels.length > 0) {
          rejected.push({
            id: job.id,
            reason: "model_not_in_lane",
            purpose,
            models: offendingModels,
          });
          continue;
        }
        if (this._backupThresholdBelowLaneMinimum({ ...job, policy_json: policyJson }, reservation)) {
          rejected.push({
            id: job.id,
            reason: "backup_after_attempts_below_lane_minimum",
            purpose,
          });
          continue;
        }
        const purposeUsageRow = purposeUsage.get(purpose) || {
          jobs_ingested: 0,
          write_units: 0,
        };
        const writeUnits = this._ingressWriteUnitsFor({ ...job, policy_json: policyJson });
        const purposeWriteLimit = Number(reservation.daily_write_units);
        if (
          Number.isFinite(purposeWriteLimit) &&
          purposeWriteLimit >= 0 &&
          purposeUsageRow.write_units + writeUnits > purposeWriteLimit
        ) {
          rejected.push({ id: job.id, reason: "purpose_write_budget_exceeded" });
          continue;
        }
        // A purpose may consume its own reserved write budget plus globally unreserved headroom,
        // but must leave every other configured purpose enough capacity to use its reservation.
        const otherReservations = Object.entries(ingressReservations).reduce(
          (sum, [otherPurpose, config]) => {
            if (otherPurpose === purpose) return sum;
            const reserved = Number(config?.reserved_write_units);
            if (!Number.isFinite(reserved) || reserved <= 0) return sum;
            return sum + Math.max(0, reserved - (purposeUsage.get(otherPurpose)?.write_units || 0));
          },
          0
        );
        if (ingressWriteUnits + newlyAdmittedWriteUnits + writeUnits > maxIngressWriteUnits - otherReservations) {
          rejected.push({ id: job.id, reason: "ingress_write_budget_reserved" });
          continue;
        }

        sql.exec(
          `INSERT INTO jobs (
            id, idempotency_key, request_digest, provider_idempotency_key,
            state, priority, purpose, policy_json, prompt_family,
            input_token_estimate, max_output_token_estimate,
            payload_key, attempts, created_at, updated_at
          ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)`,
          job.id,
          job.idempotency_key,
          job.request_digest,
          providerIdempotencyKey,
          priority,
          purpose,
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
        queuedAdded += 1;
        newlyAdmittedWriteUnits += writeUnits;
        purposeUsage.set(purpose, {
          jobs_ingested: purposeUsageRow.jobs_ingested + 1,
          write_units: purposeUsageRow.write_units + writeUnits,
        });
        const delta = purposeDeltas.get(purpose) || { jobs: 0, units: 0 };
        delta.jobs += 1;
        delta.units += writeUnits;
        purposeDeltas.set(purpose, delta);
        accepted.push({ id: job.id, submitted_id: job.id });
      }

      // One ledger upsert per purpose per batch, not per job: admission above reads the
      // in-memory purposeUsage, so the persisted row only needs the batch's net delta (a billed
      // row saved per admitted job). The whole batch is one transaction either way.
      for (const [purpose, delta] of purposeDeltas) {
        sql.exec(
          `INSERT INTO ingress_purpose (utc_day, purpose, jobs_ingested, write_units)
           VALUES (?, ?, ?, ?)
           ON CONFLICT (utc_day, purpose) DO UPDATE SET
             jobs_ingested = jobs_ingested + excluded.jobs_ingested,
             write_units = write_units + excluded.write_units`,
          sched.utc_day,
          purpose,
          delta.jobs,
          delta.units
        );
      }

      if (newlyInsertedCount > 0 || queuedAdded > 0) {
        // The row counter rides on this write too (see _rowsWrittenToday).
        sql.exec(
          `UPDATE scheduler SET jobs_ingested_today = jobs_ingested_today + ?,
           ingress_write_units_today = ingress_write_units_today + ?,
           queued_job_count = queued_job_count + ?,
           rows_written_today = rows_written_today + ? WHERE id = 1`,
          newlyInsertedCount,
          newlyAdmittedWriteUnits,
          queuedAdded,
          this._takeUnflushedRows()
        );
      }

      return { accepted, rejected };
    });
  }

  /**
   * Clone a completed job into the v2 schema-correction namespace. The caller has already
   * written the corrected payload to B2; this transaction only records the new queue row and
   * copies the source job's routing policy and output budget. Keeping the source row untouched
   * lets the caller retry this RPC idempotently and ack the original only after the clone exists.
   */
  async schemaRetry(sourceId, retry) {
    const sql = this._getSql();
    const now = Date.now();
    const maxJobsToday = this._maxJobsPerUtcDay();
    const maxIngressWriteUnits = this._maxIngressWriteUnitsPerUtcDay();

    return this.ctx.storage.transactionSync(() => {
      // Derive the correction namespace from the source id rather than the source row. That lets
      // a response-loss retry return the existing correction even after cleanup purged the source.
      const idempotencyKey = `schema-correction-v2:${sourceId}`;

      // A repeated request after the first clone was accepted must return that canonical id,
      // even if the source has since been acked and purged by the caller.
      const existing = [...sql.exec(
        "SELECT id, request_digest FROM jobs WHERE idempotency_key = ?",
        idempotencyKey
      )];
      if (existing.length > 0) {
        if (existing[0].request_digest === retry.corrected_request_digest) {
          return {
            status: "accepted",
            id: existing[0].id,
            idempotency_key: idempotencyKey,
          };
        }
        return { status: "conflict" };
      }

      const sourceRows = [...sql.exec("SELECT * FROM jobs WHERE id = ?", sourceId)];
      if (sourceRows.length === 0) return { status: "not_found" };
      const source = sourceRows[0];

      if (source.state !== "completed") {
        return { status: "invalid_state", state: source.state };
      }

      const sched = this._rollUtcDayIfNeeded(now);
      this._ensureQueuedJobCounter();
      // A correction is new work: past the enqueue row threshold it waits for tomorrow, reported
      // as the daily cap the client already treats as "retry later".
      if (
        sched.jobs_ingested_today >= maxJobsToday ||
        this._rowsWrittenToday(sched) >= this._enqueueRowStop()
      ) {
        return { status: "daily_cap_exceeded" };
      }

      const purpose = source.purpose || this._purposeForJob(source);
      const reservations = this._ingressPurposeReservations();
      // Same registration gate as enqueueBatch: a schema-correction retry re-admits a job and so
      // must not become a side door for a purpose with no `llm_lanes` entry.
      if (!Object.hasOwn(reservations, purpose)) {
        return { status: "purpose_not_registered" };
      }
      const reservation = reservations[purpose] || {};
      // ...and the same lane route allowlist, for the same reason. A retry re-admits the job
      // against the lane's budget (it consumes today's job count and write units below), so a
      // source whose lane has since dropped the route it names would spend that budget on a
      // route the lane no longer declares. Checked before any budget arithmetic, matching
      // enqueueBatch: a job that cannot legally run must not consume the day's admission.
      if (this._modelsOutsideLane(source, reservation, this._dispatchLimits()).length > 0) {
        return { status: "model_not_in_lane" };
      }
      if (this._backupThresholdBelowLaneMinimum(source, reservation)) {
        return { status: "backup_after_attempts_below_lane_minimum" };
      }
      const purposeUsage = [...sql.exec(
        "SELECT write_units FROM ingress_purpose WHERE utc_day = ? AND purpose = ?",
        sched.utc_day,
        purpose
      )][0]?.write_units || 0;
      // Computed before the write-unit charge (not alongside the INSERT below) so
      // _ingressWriteUnitsFor -> _modelsToIndex -> backupModelsActive sees the SAME
      // schema_retry_count the clone will actually be created with -- otherwise a correction that
      // activates backup-model indexing (schema_retry_count >= 1) would be charged as if it only
      // indexed the primary model, undercounting against the purpose/global write-unit budgets.
      const nextSchemaRetryCount = (Number(source.schema_retry_count) || 0) + 1;
      const retryJob = {
        ...source,
        input_token_estimate: retry.corrected_input_token_estimate,
        max_output_token_estimate: source.max_output_token_estimate,
        attempts: source.attempts,
        schema_retry_count: nextSchemaRetryCount,
      };
      const writeUnits = this._ingressWriteUnitsFor(retryJob);
      const purposeWriteLimit = Number(reservation.daily_write_units);
      if (Number.isFinite(purposeWriteLimit) && purposeWriteLimit >= 0 &&
          purposeUsage + writeUnits > purposeWriteLimit) {
        return { status: "purpose_write_budget_exceeded" };
      }
      const otherReservations = Object.entries(reservations).reduce((sum, [otherPurpose, config]) => {
        if (otherPurpose === purpose) return sum;
        const reserved = Number(config?.reserved_write_units);
        if (!Number.isFinite(reserved) || reserved <= 0) return sum;
        const used = [...sql.exec(
          "SELECT write_units FROM ingress_purpose WHERE utc_day = ? AND purpose = ?",
          sched.utc_day,
          otherPurpose
        )][0]?.write_units || 0;
        return sum + Math.max(0, reserved - used);
      }, 0);
      if ((sched.ingress_write_units_today || 0) + writeUnits > maxIngressWriteUnits - otherReservations) {
        return { status: "ingress_write_budget_reserved" };
      }

      const id = crypto.randomUUID();
      // Carry `attempts` and `schema_retry_count` forward from `source` rather than resetting to
      // 0: this is what lets a job that needed a JSON-schema correction (or a chain of them) cross
      // the same backup_after_attempts threshold as a job that failed the same number of times on
      // plain dispatch attempts (backupModelsActive, routes.js) -- otherwise a schema-correction
      // clone would always start over at attempts=0 and could never surface a backup model.
      // (nextSchemaRetryCount computed above, before the write-unit charge.)
      sql.exec(
        `INSERT INTO jobs (
          id, idempotency_key, request_digest, provider_idempotency_key,
          state, priority, purpose, policy_json, prompt_family,
          input_token_estimate, max_output_token_estimate,
          payload_key, attempts, schema_retry_count, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        id,
        idempotencyKey,
        retry.corrected_request_digest,
        source.provider_idempotency_key,
        source.priority,
        purpose,
        source.policy_json,
        source.prompt_family,
        retry.corrected_input_token_estimate,
        source.max_output_token_estimate,
        retry.corrected_payload_key,
        source.attempts,
        nextSchemaRetryCount,
        now,
        now
      );
      this._indexQueuedJobModels(
        {
          id,
          policy_json: source.policy_json,
          priority: source.priority,
          input_token_estimate: retry.corrected_input_token_estimate,
          max_output_token_estimate: source.max_output_token_estimate,
          attempts: source.attempts,
          schema_retry_count: nextSchemaRetryCount,
          created_at: now,
        },
        source.priority,
        now
      );
      sql.exec(
        `UPDATE scheduler SET jobs_ingested_today = jobs_ingested_today + 1,
         ingress_write_units_today = ingress_write_units_today + ?,
         queued_job_count = queued_job_count + 1 WHERE id = 1`,
        writeUnits
      );
      sql.exec(
        `INSERT INTO ingress_purpose (utc_day, purpose, jobs_ingested, write_units)
         VALUES (?, ?, 1, ?)
         ON CONFLICT (utc_day, purpose) DO UPDATE SET
           jobs_ingested = jobs_ingested + 1,
           write_units = write_units + excluded.write_units`,
        sched.utc_day,
        purpose,
        writeUnits
      );
      return { status: "accepted", id, idempotency_key: idempotencyKey };
    });
  }

  /**
   * Return the recurring, payload-free scheduler snapshot used by producer workflows. Its SQL
   * reads only bounded singleton/configuration state; it must remain safe to call before and
   * after every LLM producer run, even when queued or retained job history is large.
   */
  async stats(now) {
    const sql = this._getSql();
    return this.ctx.storage.transactionSync(() => {
      this._ensureQueuedJobCounter();
      const one = (query, ...args) => [...sql.exec(query, ...args)][0] || {};
      const scheduler = one(
        `SELECT utc_day, bundle_count_today, lease_count_today, jobs_ingested_today,
                ingress_write_units_today, queued_job_count, rows_written_today,
                next_maintenance_alarm_at, last_claim_at, last_claim_result,
                last_claim_reason, claim_empty_count_today, claim_reason_counts_json
           FROM scheduler WHERE id = 1`
      );
      const claimReasonCounts = parseJsonObject(scheduler.claim_reason_counts_json);
      const activeBundles = [...sql.exec(
        "SELECT active_call_count, lease_expires_at FROM bundles WHERE state = 'active'"
      )];
      const activeCalls = activeBundles.reduce((sum, row) => sum + (row.active_call_count || 0), 0);

      return {
        now,
        jobs: { by_state: { queued: scheduler.queued_job_count || 0 } },
        bundles: {
          active: activeBundles.length,
          active_call_count: activeCalls,
          active_expired: activeBundles.filter((row) => row.lease_expires_at <= now).length,
        },
        // Leased jobs per route/provider (bounded by the in-flight caps) and any operator pause.
        in_flight: this._inFlightCounts(now),
        dispatch_pauses: this._activePauses(now),
        scheduler: {
          utc_day: scheduler.utc_day ?? null,
          bundle_count_today: scheduler.bundle_count_today ?? 0,
          lease_count_today: scheduler.lease_count_today ?? 0,
          ingress_write_units_today: scheduler.ingress_write_units_today ?? 0,
          jobs_ingested_today: scheduler.jobs_ingested_today ?? 0,
          next_maintenance_alarm_at: scheduler.next_maintenance_alarm_at ?? null,
        },
        row_budget: this._rowBudgetSnapshot(scheduler),
        claim: {
          last_at: scheduler.last_claim_at ?? null,
          last_result: scheduler.last_claim_result || null,
          last_reason: scheduler.last_claim_reason || null,
          empty_count_today: scheduler.claim_empty_count_today ?? 0,
          reason_counts_today: claimReasonCounts,
        },
      };
    });
  }

  /**
   * On-demand historical diagnostics for a human operator. This can inspect retained queue
   * history and is intentionally never called by scheduled producer telemetry; use stats()
   * for recurring observation. The HTTP route requires `?detail=1` to reach this method.
   */
  async detailedStats(now, limit = 20) {
    this._ensureQueuedJobCounter();
    const sql = this._getSql();
    const one = (query, ...args) => [...sql.exec(query, ...args)][0] || {};

    const states = {};
    for (const row of sql.exec("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")) {
      states[row.state] = row.n;
    }

    // The stranding check. A queued job with no job_models row can never be selected by
    // claimDispatchWindow, so `queued` above would look healthy while nothing is claimable.
    const unindexed = one(
      `SELECT COUNT(*) AS n FROM jobs WHERE state = 'queued'
         AND NOT EXISTS (SELECT 1 FROM job_models WHERE job_models.job_id = jobs.id)`
    ).n;

    const oldestQueued = one(
      "SELECT MIN(created_at) AS t FROM jobs WHERE state = 'queued'"
    ).t;

    // What the scheduler actually ranks over: queued work grouped by the model it can run on.
    const queuedByModel = [...sql.exec(
      `SELECT m.model AS model, COUNT(*) AS queued
         FROM job_models m JOIN jobs j ON j.id = m.job_id
        WHERE j.state = 'queued'
        GROUP BY m.model ORDER BY queued DESC LIMIT ?`,
      limit
    )];

    // Every route the DO has a ledger row for, with the two independent reasons a route can be
    // unusable AND the capacity score itself.
    //
    // The first cut of this reported only blocked_until, which is why it could not diagnose the
    // 2026-08-30 stall: _capacityFraction also zeroes a route when its 429 buffer covers the
    // dispatch window, and that path sets no block at all. Every route was scoring 0 while this
    // endpoint cheerfully reported an empty `blocked` list. `capacity` below is the number the
    // scheduler actually ranks on, so a route contributing nothing can never again be invisible
    // here regardless of which guard zeroed it.
    //
    // The routes table holds one row per route ever touched -- a small, fixed population, not a
    // traffic-growing table -- so reading all of it is cheap and `limit` only truncates the
    // listing for display.
    const routeRows = [...sql.exec(
      `SELECT route_id, blocked_until, throttle_streak, payment_required_streak,
              last_provider_status, rpd_count, rpd_day_key, buffer_seconds, buffer_updated_at,
              provisional_reservation, settled_usage, rpm_count, tpm_reserved, full_token_budget,
              token_budget_updated_at, tpm_window_start, rpm_window_start, rpd_window_start
         FROM routes`
    )];
    const catalog = this._dispatchLimits();
    const windowSeconds = this._dispatchWindowSecondsForStats();
    const leasedByRoute = new Map();
    for (const row of sql.exec(
      "SELECT lease_route_id, COUNT(*) AS n FROM jobs WHERE state = 'leased' GROUP BY lease_route_id"
    )) {
      if (row.lease_route_id) leasedByRoute.set(row.lease_route_id, row.n);
    }
    const leasedByProvider = new Map();
    for (const [routeId, count] of leasedByRoute) {
      const provider = catalog?.routes_by_id?.[routeId]?.provider;
      if (provider) leasedByProvider.set(provider, (leasedByProvider.get(provider) || 0) + count);
    }
    const routes = routeRows.map((row) => {
      const catalogRoute = catalog?.routes_by_id?.[row.route_id];
      const merged = catalogRoute ? { ...catalogRoute, ...row } : row;
      const providerConfig = catalog?.providers?.[catalogRoute?.provider];
      const routeConcurrency = Number(catalogRoute?.concurrency);
      const providerConcurrency = Number(
        providerConfig?.concurrency ?? catalogRoute?.provider_concurrency
      );
      return {
        route_id: row.route_id,
        capacity: catalogRoute ? this._capacityFraction(merged, now, windowSeconds) : null,
        provider: catalogRoute?.provider || null,
        in_flight: leasedByRoute.get(row.route_id) || 0,
        route_concurrency: Number.isFinite(routeConcurrency) ? routeConcurrency : null,
        provider_in_flight: catalogRoute?.provider
          ? leasedByProvider.get(catalogRoute.provider) || 0
          : 0,
        provider_concurrency: Number.isFinite(providerConcurrency) ? providerConcurrency : null,
        blocked_until: row.blocked_until,
        buffer_seconds: row.buffer_seconds,
        buffer_remaining_seconds: effectiveBufferSeconds(row, now),
        throttle_streak: row.throttle_streak,
        payment_required_streak: row.payment_required_streak,
        last_provider_status: row.last_provider_status,
        rpd_count: row.rpd_count,
        rpd_day_key: row.rpd_day_key,
        provisional_reservation: row.provisional_reservation,
        in_catalog: Boolean(catalogRoute),
      };
    });
    // Surface the ones that cannot currently take work first -- that is what an operator is
    // looking for -- then the rest, so a healthy fleet still reads as healthy.
    const zeroCapacity = routes.filter((r) => r.capacity === 0 || r.capacity === null);
    const blockedRoutes = routes
      .filter((r) => Number(r.blocked_until) > now)
      .sort((a, b) => b.blocked_until - a.blocked_until)
      .slice(0, limit);

    // Which declared accounts this deployment actually holds a key for. Purely informational
    // (see _routeCredentialConfigured on why it must never gate dispatch), but it is the only
    // place an operator can see that a route is configured against a secret that was never set --
    // otherwise those jobs just churn through their retry budget as generic "retryable_error".
    const dispatchLimitsForStats = this._dispatchLimits();
    const unconfiguredAccounts = [];
    for (const [provider, cfg] of Object.entries(dispatchLimitsForStats?.providers || {})) {
      for (const account of cfg?.accounts || []) {
        if (account?.api_key_env && !this.env?.[account.api_key_env]) {
          unconfiguredAccounts.push(`${provider}:${account.id} (${account.api_key_env})`);
        }
      }
    }

    const scheduler = one("SELECT * FROM scheduler WHERE id = 1");
    const claimReasonCounts = parseJsonObject(scheduler.claim_reason_counts_json);
    const lastClaimDiagnostics = parseJsonObject(scheduler.last_claim_diagnostics_json);

    const today = new Date(now).toISOString().slice(0, 10);
    const routeFailures = [...sql.exec(
      `SELECT utc_day, route_id, failure_class, count, last_status, last_seen_at
         FROM route_failures
        WHERE utc_day = ?
        ORDER BY count DESC
        LIMIT ?`,
      today,
      limit
    )];

    return {
      now,
      jobs: {
        by_state: states,
        queued_without_model_index: unindexed,
        oldest_queued_age_ms: oldestQueued == null ? null : now - oldestQueued,
      },
      queued_by_model: queuedByModel,
      unconfigured_accounts: unconfiguredAccounts,
      routes: {
        total: routes.length,
        // A route here contributes nothing to dispatch. If this covers every route, the
        // scheduler has nothing to rank and will claim nothing, however much work is queued.
        zero_capacity_count: zeroCapacity.length,
        zero_capacity: zeroCapacity.slice(0, limit),
        blocked: blockedRoutes,
        all: routes.slice(0, limit),
      },
      route_failures: routeFailures,
      bundles: {
        active: one("SELECT COUNT(*) AS n FROM bundles WHERE state = 'active'").n,
        active_call_count: one(
          "SELECT COALESCE(SUM(active_call_count), 0) AS n FROM bundles WHERE state = 'active'"
        ).n,
        active_expired: one(
          "SELECT COUNT(*) AS n FROM bundles WHERE state = 'active' AND lease_expires_at <= ?",
          now
        ).n,
      },
      scheduler: {
        utc_day: scheduler.utc_day ?? null,
        bundle_count_today: scheduler.bundle_count_today ?? 0,
          lease_count_today: scheduler.lease_count_today ?? 0,
          ingress_write_units_today: scheduler.ingress_write_units_today ?? 0,
        jobs_ingested_today: scheduler.jobs_ingested_today ?? 0,
        next_maintenance_alarm_at: scheduler.next_maintenance_alarm_at ?? null,
      },
      row_budget: this._rowBudgetSnapshot(scheduler),
      claim: {
        last_at: scheduler.last_claim_at ?? null,
        last_result: scheduler.last_claim_result || null,
        last_reason: scheduler.last_claim_reason || null,
        empty_count_today: scheduler.claim_empty_count_today ?? 0,
        reason_counts_today: claimReasonCounts,
        last_diagnostics: lastClaimDiagnostics,
      },
      in_flight: {
        by_route: Object.fromEntries(leasedByRoute),
        by_provider: Object.fromEntries(leasedByProvider),
      },
    };
  }

  async pollBatch(ids) {
    this._ensureMigratedJobModels();
    if (!ids || ids.length === 0) {
      return { statuses: [] };
    }

    const sql = this._getSql();
    // A completed job's `lease_route_id` is left untouched by completeBatch's success UPDATE (it
    // only rewrites `state`/`result_key`/`updated_at`), so it still names the physical route the
    // job actually completed on -- including a backup route a primary-pinned job escalated to.
    // Without returning the model that route serves, the client falls back to whichever model it
    // guessed at enqueue time (JobHandle.model, set from allowed_models[0]), which is always the
    // PRIMARY model regardless of which one actually produced the response.
    const dispatchLimits = this._dispatchLimits();
    const statuses = [];
    for (const chunk of this._chunks(ids)) {
      const placeholders = chunk.map(() => "?").join(",");
      const rows = [...sql.exec(
        `SELECT id, state, result_key, payload_key, attempts, lease_route_id FROM jobs
         WHERE id IN (${placeholders})`,
        ...chunk
      )];
      for (const row of rows) {
        statuses.push({
          id: row.id,
          state: row.state,
          result_key: row.state === "completed" ? row.result_key : null,
          // Returned for completed jobs so a consumer that has durably persisted the result can
          // delete exactly the B2 objects THIS row references before retireConsumed.
          payload_key: row.state === "completed" ? row.payload_key : null,
          error: row.state === "failed" ? "job_failed" : null,
          attempts: row.attempts,
          model:
            row.state === "completed"
              ? modelForRouteId(row.lease_route_id, dispatchLimits)
              : null,
        });
      }
    }

    return { statuses };
  }

  /** Return a bounded, keyset-paginated feed of v2 terminal work.
   *
   * The client owns request payload/result interpretation, while the coordinator is authoritative
   * for lifecycle state.  This feed lets the reaper ask for only rows that became terminal since
   * its last cursor instead of downloading every client-side pending record merely to learn that
   * it is still queued.  `updated_at,id` is a stable keyset cursor; a row updated at the same
   * millisecond cannot be skipped by a timestamp-only cursor.
   */
  async terminalFeed(cursor, limit = 500) {
    const sql = this._getSql();
    const afterUpdatedAt = Number(cursor?.updated_at) || 0;
    const afterId = typeof cursor?.id === "string" ? cursor.id : "";
    // Query completed and failed states with two separate seeks against idx_jobs_state_updated_id
    // using row-value tuple comparison (updated_at, id) > (?, ?). This guarantees a pure index
    // range seek with NO temp B-tree, whereas `state IN ('completed', 'failed') ORDER BY updated_at`
    // forces SQLite to scan and sort the entire terminal history in memory.
    const completedRows = [...sql.exec(
      `SELECT id, state, result_key, updated_at FROM jobs
       WHERE state = 'completed' AND (updated_at, id) > (?, ?)
       ORDER BY state ASC, updated_at ASC, id ASC LIMIT ?`,
      afterUpdatedAt,
      afterId,
      limit
    )];
    const failedRows = [...sql.exec(
      `SELECT id, state, result_key, updated_at FROM jobs
       WHERE state = 'failed' AND (updated_at, id) > (?, ?)
       ORDER BY state ASC, updated_at ASC, id ASC LIMIT ?`,
      afterUpdatedAt,
      afterId,
      limit
    )];
    const merged = mergeSortedRows(
      completedRows,
      failedRows,
      limit,
      (left, right) => left.updated_at - right.updated_at || left.id.localeCompare(right.id)
    );
    const last = merged.at(-1);
    return {
      terminals: merged.map((row) => ({
        id: row.id,
        state: row.state,
        result_key: row.result_key,
        updated_at: row.updated_at,
      })),
      cursor: last
        ? { updated_at: last.updated_at, id: last.id }
        : { updated_at: afterUpdatedAt, id: afterId },
    };
  }

  /** Cancel queued work before it reaches a provider.  Leased/unknown attempts are intentionally
   * left alone: their provider call may already be running and only completeBatch can fence it. */
  async cancelBatch(jobIds) {
    if (!jobIds || jobIds.length === 0) return { cancelled: [], in_flight: [], not_found: [] };
    const sql = this._getSql();
    // Past the optional threshold a cancel is deferred: report every job as still in flight so
    // the caller keeps it and retries after the reset.
    if (this._readRowsWrittenToday() >= this._optionalRowStop()) {
      return { cancelled: [], in_flight: [...jobIds], not_found: [] };
    }
    return this.ctx.storage.transactionSync(() => {
      this._ensureQueuedJobCounter();
      const cancelled = [];
      const inFlight = [];
      const found = new Set();
      let cancelledQueued = 0;
      for (const chunk of this._chunks(jobIds)) {
        const placeholders = chunk.map(() => "?").join(",");
        const rows = [...sql.exec(
          `SELECT id, state FROM jobs WHERE id IN (${placeholders})`,
          ...chunk
        )];
        for (const row of rows) {
          found.add(row.id);
          if (["leased", "unknown_attempt"].includes(row.state)) {
            inFlight.push(row.id);
            continue;
          }
          if (row.state === "purge_pending") {
            cancelled.push(row.id);
            continue;
          }
          sql.exec("DELETE FROM job_models WHERE job_id = ?", row.id);
          sql.exec(
            "UPDATE jobs SET state = 'purge_pending', updated_at = ? WHERE id = ?",
            Date.now(),
            row.id
          );
          if (row.state === "queued") cancelledQueued += 1;
          cancelled.push(row.id);
        }
      }
      if (cancelledQueued > 0) {
        sql.exec(
          "UPDATE scheduler SET queued_job_count = MAX(0, queued_job_count - ?) WHERE id = 1",
          cancelledQueued
        );
      }
      return { cancelled, in_flight: inFlight, not_found: jobIds.filter((id) => !found.has(id)) };
    });
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

  /**
   * Increment bounded per-class failure telemetry (Initiative 20 / review/45 §20.7).
   * Keyed by (utc_day, route_id, failure_class) so it can never grow with traffic.
   */
  _recordRouteFailure(sql, now, routeId, failureClass, lastStatus) {
    if (!routeId || !failureClass) return;
    const utcDay = new Date(now).toISOString().slice(0, 10);
    sql.exec(
      `INSERT INTO route_failures (
         utc_day, route_id, failure_class, count, last_status, last_seen_at
       ) VALUES (?, ?, ?, 1, ?, ?)
       ON CONFLICT(utc_day, route_id, failure_class) DO UPDATE SET
         count = count + 1,
         last_status = excluded.last_status,
         last_seen_at = excluded.last_seen_at`,
      utcDay,
      routeId,
      failureClass,
      lastStatus != null ? Number(lastStatus) : null,
      now
    );
  }

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
        route_id, rpm_window_start, rpm_count, rpd_window_start, rpd_count, rpd_day_key,
        tpm_window_start, tpm_reserved, full_token_budget, token_budget_updated_at,
        provisional_reservation, settled_usage, cost_accumulated,
        throttle_streak, last_provider_status, blocked_until, buffer_seconds,
        payment_required_streak
      ) VALUES (?, 0, 0, 0, 0, '', 0, 0, ?, ?, 0, 0, 0, 0, NULL, NULL, 0, 0)`,
      routeId,
      seedBudget,
      now
    );
    return {
      route_id: routeId,
      rpm_window_start: 0,
      rpm_count: 0,
      rpd_window_start: 0,
      rpd_day_key: "",
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

  /** Read a provider's live ledger row, seeding a fresh one the first time this provider is touched. */
  _getOrCreateProviderLedger(providerName, now, providerCfg) {
    const sql = this._getSql();
    const rows = [...sql.exec("SELECT * FROM providers WHERE provider = ?", providerName)];
    if (rows.length > 0) return rows[0];
    const tpm = Number(providerCfg?.tpm) || 0;
    const seedBudget = tpm * FULL_TOKEN_BUDGET_WINDOWS;
    sql.exec(
      `INSERT INTO providers (
        provider, tpm_window_start, tpm_reserved, full_token_budget, token_budget_updated_at
      ) VALUES (?, 0, 0, ?, ?)`,
      providerName,
      seedBudget,
      now
    );
    return {
      provider: providerName,
      tpm_window_start: 0,
      tpm_reserved: 0,
      full_token_budget: seedBudget,
      token_budget_updated_at: now,
    };
  }

  /**
   * Canonical model groups a job may use. Persist every explicit allowed model, even when it has
   * no configured route yet, so a later catalog addition makes an already-queued job searchable
   * without rewriting it. This follows aliases only: v2 does not expand config/model_routing (that
   * quota-exhaustion overflow map is Python-scheduler-only). It does expand a job's own
   * `policy_json.backup_models` once `modelsForJob`/`backupModelsActive` (routes.js) say the job's
   * `attempts`/`schema_retry_count` warrant it -- that's a per-job failure-count signal carried on
   * the job itself, not a config-driven route substitution.
   *
   * Omits a model whose *every* currently configured route is structurally too small for this
   * job's own token estimates (routesEligibleFor's combined input/output context-limit check,
   * evaluated here independent of any live RPM/RPD/TPM/blocked_until state, which fluctuates and
   * must never exclude a job from the index). Without this, a handful of oversized jobs land at
   * the head of that model's bounded per-claim candidate window (MAX_JOBS_PER_MODEL_CLAIM) and
   * stay there
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
    // modelsForJob folds in policy.backup_models once the job's attempts/schema_retry_count cross
    // its configured threshold (backupModelsActive) -- see routes.js.
    const allowedModels = modelsForJob(job, policy);
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
        return routeFitsContext(route, inputTokens, outputTokens);
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
    let routeFailuresDeleted = 0;

    if (bundleLimit > 0) {
      const cutoff = now - this._bundleRetentionMs();
      // Query each terminal state independently so each statement can use the leading state and
      // created_at columns of idx_bundles_state_created. A combined IN + ORDER BY would make
      // SQLite read both state ranges into a temp B-tree before applying the LIMIT.
      const completed = [...sql.exec(
        `SELECT bundle_id, created_at FROM bundles
         WHERE state = 'completed' AND created_at < ? AND lease_expires_at < ?
         ORDER BY created_at ASC LIMIT ?`,
        cutoff,
        now,
        bundleLimit
      )];
      const expired = [...sql.exec(
        `SELECT bundle_id, created_at FROM bundles
         WHERE state = 'expired' AND created_at < ? AND lease_expires_at < ?
         ORDER BY created_at ASC LIMIT ?`,
        cutoff,
        now,
        bundleLimit
      )];
      const ids = mergeSortedRows(
        completed,
        expired,
        bundleLimit,
        (left, right) => left.created_at - right.created_at
      ).map((row) => row.bundle_id);
      for (const chunk of this._chunks(ids)) {
        const placeholders = chunk.map(() => "?").join(",");
        sql.exec(`DELETE FROM bundles WHERE bundle_id IN (${placeholders})`, ...chunk);
        bundlesDeleted += chunk.length;
      }
    }

    if (attemptLimit > 0) {
      const cutoff = now - this._attemptRetentionMs();
      // Oldest-first by rowid, which is insertion order (created_at is set at insert and never
      // changes). This needs no created_at index -- an index that cost a billed row on every
      // attempt insert. `rowid >= 0` keeps it an INTEGER PRIMARY KEY range seek, read-bounded by
      // the LIMIT; stop at the first row still inside the retention window.
      const ids = [];
      for (const row of sql.exec(
        `SELECT attempt_id, created_at FROM attempts WHERE rowid >= 0 ORDER BY rowid ASC LIMIT ?`,
        attemptLimit
      )) {
        if (!(row.created_at < cutoff)) break;
        ids.push(row.attempt_id);
      }
      for (const chunk of this._chunks(ids)) {
        const placeholders = chunk.map(() => "?").join(",");
        sql.exec(`DELETE FROM attempts WHERE attempt_id IN (${placeholders})`, ...chunk);
        attemptsDeleted += chunk.length;
      }

      // review/45 §20.7: Prune route_failures older than ATTEMPT_RETENTION_DAYS using
      // the existing _maxAttemptPrunePerTick budget idiom.
      const cutoffDay = new Date(cutoff).toISOString().slice(0, 10);
      const staleFailures = [...sql.exec(
        `SELECT utc_day, route_id, failure_class FROM route_failures
         WHERE utc_day < ? ORDER BY utc_day ASC LIMIT ?`,
        cutoffDay,
        attemptLimit
      )];
      for (const row of staleFailures) {
        sql.exec(
          `DELETE FROM route_failures WHERE utc_day = ? AND route_id = ? AND failure_class = ?`,
          row.utc_day,
          row.route_id,
          row.failure_class
        );
        routeFailuresDeleted += 1;
      }
    }

    return { bundlesDeleted, attemptsDeleted, routeFailuresDeleted };
  }

  _freshRouteLedger(catalogRoute, now) {
    const tpm = Number(catalogRoute?.tpm) || 0;
    return {
      rpm_window_start: 0,
      rpm_count: 0,
      rpd_window_start: 0,
      rpd_day_key: "",
      rpd_count: 0,
      tpm_window_start: 0,
      tpm_reserved: 0,
      full_token_budget: tpm * FULL_TOKEN_BUDGET_WINDOWS,
      token_budget_updated_at: now,
      blocked_until: null,
      buffer_seconds: 0,
      buffer_updated_at: 0,
    };
  }

  /**
   * Whether this deployment actually holds the API key the route's account needs.
   *
   * A route whose secret is absent cannot possibly succeed, but it was still ranked and claimed:
   * `resolveProviderCredentials` then threw "missing secret X" inside the executor, which
   * `attemptProviderCall` caught as a generic `retryable_error`, so every job routed there churned
   * through its whole retry budget before failing -- and the operator surface said "retryable
   * error", never "that key is not set".
   *
   * DIAGNOSTIC ONLY -- deliberately NOT used to gate ranking. Gating dispatch on secret presence
   * would mean that if the DO's `env` ever failed to expose provider secrets the way this assumes,
   * every route in the catalog would silently drop out of the ranking with no blocked_until and no
   * error: the exact failure shape as the `rpd: null` coercion this same review had to fix, and
   * not worth re-creating for a convenience. `stats()` reports it so an operator can SEE which
   * accounts are unconfigured, and a missing secret still surfaces per-attempt via
   * resolveProviderCredentials -- it is now merely visible rather than silent.
   */
  _routeCredentialConfigured(catalogRoute, dispatchLimits) {
    const providerCfg = dispatchLimits?.providers?.[catalogRoute?.provider];
    const accounts = providerCfg?.accounts || [];
    if (accounts.length === 0) return true; // nothing declared to check against
    const account = catalogRoute?.account_id
      ? accounts.find((candidate) => candidate.id === catalogRoute.account_id)
      : accounts[0];
    const envName = account?.api_key_env;
    if (!envName) return false;
    return Boolean(this.env?.[envName]);
  }

  _capacityFraction(route, now, windowSeconds) {
    if (Number(route.blocked_until) > now) return 0;
    // Decayed, not stored: a stored buffer never expires on its own, and the only code that
    // cleared it required a success this very check makes unreachable (see effectiveBufferSeconds).
    if (effectiveBufferSeconds(route, now) * 1000 >= windowSeconds * 1000) return 0;

    const windowFraction = (limit, windowStart, count, durationMs) => {
      // `limit === null` means the route declares no limit on this axis -- unlimited, full score.
      // It is NOT the same as an explicit 0. See configuredLimit() on why these must not be
      // collapsed by Number().
      if (limit === null) return 1;
      // rpd: 0 is the repository's explicit "paused/exhausted" convention, not an unlimited
      // route. Other absent limits do not constrain this coarse, route-ranking score.
      if (limit === 0) return 0;
      if (!Number.isFinite(limit) || limit < 0) return 1;
      if (!Number.isFinite(windowStart) || now - windowStart >= durationMs) return 1;
      return Math.max(0, Math.min(1, (limit - Math.max(0, count || 0)) / limit));
    };

    const rpmFraction = windowFraction(
      configuredLimit(route.rpm),
      route.rpm_window_start,
      route.rpm_count,
      rpmWindowDurationMs(route)
    );
    // rpd is keyed on the provider's calendar day: a stale key means the provider already reset,
    // so the route is at full daily capacity regardless of how recently we last used it.
    const rpdLimit = configuredLimit(route.rpd);
    let rpdFraction;
    if (rpdLimit === null) rpdFraction = 1; // no daily limit declared -- unlimited on this axis
    else if (rpdLimit === 0) rpdFraction = 0; // repository convention: paused/exhausted
    else if (!Number.isFinite(rpdLimit) || rpdLimit < 0) rpdFraction = 1;
    else if (route.rpd_day_key !== zonedDateKey(now, routeResetTimezone(route))) rpdFraction = 1;
    else
      rpdFraction = Math.max(
        0,
        Math.min(1, (rpdLimit - Math.max(0, route.rpd_count || 0)) / rpdLimit)
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
              // Daily quotas roll on the provider's clock; the pure pacing helpers only see the
              // route object, so carry the provider's reset timezone onto it.
              reset_timezone:
                dispatchLimits?.providers?.[catalogRoute.provider]?.reset_timezone || "UTC",
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
        //
        // `configuredLimit`, not `Number()` (CodeRabbit, 2026-09-13): `Number(null) === 0`, so a
        // route with no rpd configured -- unlimited on that axis, per _capacityFraction's own
        // treatment of the same field -- was silently weighted zero here instead. Any model whose
        // every route declares no rpd (several NVIDIA/DeepSeek/zai routes have neither rpd nor
        // rpm; deepseek/deepseek-v4-pro and moonshotai/kimi-k3 are two full examples) got
        // `totalWeight === 0`, forcing `score` to the hardcoded 0 branch below and dropping the
        // model out of ranking entirely via the `score > 0` filter -- not merely under-weighted,
        // but invisible to dispatch no matter how available its routes actually were.
        const routeWeight = (route) => {
          const rpd = configuredLimit(route.rpd);
          if (rpd !== null) return Math.max(0, rpd);
          const rpm = configuredLimit(route.rpm);
          if (rpm !== null) return Math.max(0, rpm) * 1440;
          const tpm = configuredLimit(route.tpm);
          if (tpm !== null) return Math.max(0, tpm) * 1440;
          // No rate signal on any axis (11 routes today: several NVIDIA/OpenRouter/zai paid
          // legs). Present but minimal rather than a fabricated "big" number this project has
          // already had to revert once this session for asserting an unmeasured capacity.
          return 1;
        };
        const totalWeight = routes.reduce((sum, entry) => sum + routeWeight(entry.route), 0);
        const score =
          totalWeight === 0
            ? 0
            : routes.reduce((sum, entry) => sum + entry.score * routeWeight(entry.route), 0) /
              totalWeight;
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
   * Returns the updated merged route object so the caller's in-memory lane-sequencing state (and
   * its ledger cache) stays consistent. Pure: the caller persists the final ledger once per claim
   * with _writeRouteLedger -- a bundle that admits several jobs on one route used to rewrite the
   * same routes row once per job (one billed row each).
   */
  _applyProvisionalReservation(mergedRoute, reservation, notBeforeAt) {
    const tpm = Number(mergedRoute.tpm) || 0;

    let rpmWindowStart = mergedRoute.rpm_window_start;
    let rpmCount = mergedRoute.rpm_count;
    if (
      !Number.isFinite(rpmWindowStart) ||
      notBeforeAt - rpmWindowStart >= rpmWindowDurationMs(mergedRoute)
    ) {
      rpmWindowStart = notBeforeAt;
      rpmCount = 1;
    } else {
      rpmCount += 1;
    }

    // Daily quotas roll on the provider's own calendar day, not 24h after our first request.
    // Anchoring on first use held a route exhausted well past the provider's actual reset --
    // ~14 hours for Gemini's midnight-America/Los_Angeles rollover -- and because an exhausted
    // route scores 0 in _capacityFraction, its whole model drops out of the ranking.
    const rpdDayKey = zonedDateKey(notBeforeAt, routeResetTimezone(mergedRoute));
    let rpdWindowStart = mergedRoute.rpd_window_start;
    let rpdCount = mergedRoute.rpd_count;
    if (mergedRoute.rpd_day_key !== rpdDayKey) {
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
      // Uncapped refill (2026-09-13 redesign, see pacing.js) -- this used to duplicate
      // availableTokenBudget()'s math inline with an extra `Math.min(tpm * FULL_TOKEN_BUDGET_WINDOWS,
      // ...)`, which silently re-imposed the retired route-level burst cap on every write even
      // after the pure function itself stopped capping. Route-level size admissibility is
      // `hard_input_ceiling`'s job alone now; this bucket just refills.
      const refilled = availableTokenBudget(mergedRoute, notBeforeAt);
      fullTokenBudget = Math.max(0, refilled - reservation);
      tokenBudgetUpdatedAt = notBeforeAt;
    }

    const provisionalReservation = (mergedRoute.provisional_reservation || 0) + reservation;

    return {
      ...mergedRoute,
      rpm_window_start: rpmWindowStart,
      rpm_count: rpmCount,
      rpd_window_start: rpdWindowStart,
      rpd_day_key: rpdDayKey,
      rpd_count: rpdCount,
      tpm_window_start: tpmWindowStart,
      tpm_reserved: tpmReserved,
      full_token_budget: fullTokenBudget,
      token_budget_updated_at: tokenBudgetUpdatedAt,
      provisional_reservation: provisionalReservation,
    };
  }

  /** Persist a route's claim-side ledger (the fields _applyProvisionalReservation advances). */
  _writeRouteLedger(route) {
    this._getSql().exec(
      `UPDATE routes SET
        rpm_window_start=?, rpm_count=?, rpd_window_start=?, rpd_count=?, rpd_day_key=?,
        tpm_window_start=?, tpm_reserved=?, full_token_budget=?, token_budget_updated_at=?,
        provisional_reservation=?
       WHERE route_id=?`,
      route.rpm_window_start,
      route.rpm_count,
      route.rpd_window_start,
      route.rpd_count,
      route.rpd_day_key,
      route.tpm_window_start,
      route.tpm_reserved,
      route.full_token_budget,
      route.token_budget_updated_at,
      route.provisional_reservation,
      route.route_id
    );
  }

  /** Persist a provider's shared token ledger (the fields _applyProviderProvisionalReservation
   * advances). */
  _writeProviderLedger(providerLedger) {
    this._getSql().exec(
      `UPDATE providers SET
        tpm_window_start=?, tpm_reserved=?, full_token_budget=?, token_budget_updated_at=?
       WHERE provider=?`,
      providerLedger.tpm_window_start,
      providerLedger.tpm_reserved,
      providerLedger.full_token_budget,
      providerLedger.token_budget_updated_at,
      providerLedger.provider
    );
  }

  /** Advance a provider's shared token ledger forward to account for an admitted reservation.
   * Pure, like _applyProvisionalReservation: persisted once per claim by _writeProviderLedger. */
  _applyProviderProvisionalReservation(providerLedger, providerCfg, reservation, notBeforeAt) {
    const tpm = Number(providerCfg?.tpm) || 0;
    if (tpm <= 0) return providerLedger;

    let tpmWindowStart = providerLedger.tpm_window_start;
    let tpmReserved = providerLedger.tpm_reserved;
    if (reservation <= tpm) {
      if (!Number.isFinite(tpmWindowStart) || notBeforeAt - tpmWindowStart >= 60_000) {
        tpmWindowStart = notBeforeAt;
        tpmReserved = reservation;
      } else {
        tpmReserved += reservation;
      }
    }

    let fullTokenBudget = providerLedger.full_token_budget;
    let tokenBudgetUpdatedAt = providerLedger.token_budget_updated_at;
    const elapsedMs = Math.max(0, notBeforeAt - (tokenBudgetUpdatedAt || 0));
    const refilled = Math.min(
      tpm * FULL_TOKEN_BUDGET_WINDOWS,
      (fullTokenBudget || 0) + (elapsedMs * tpm) / 60_000
    );
    fullTokenBudget = Math.max(0, refilled - reservation);
    tokenBudgetUpdatedAt = notBeforeAt;

    return {
      ...providerLedger,
      tpm_window_start: tpmWindowStart,
      tpm_reserved: tpmReserved,
      full_token_budget: fullTokenBudget,
      token_budget_updated_at: tokenBudgetUpdatedAt,
    };
  }

  /** Calibrated input ratio and output forecast for one route/model/prompt family (see
   * calibration.js). `cache` is an optional per-claim Map so a claim that weighs the same route
   * for several jobs of one family reads its estimates row once. */
  _calibration(route, promptFamily, cache = null) {
    // Keyed by the route's PRIMARY model, exactly as _calibrateEstimate writes it: a route
    // serving several pools (also_serves) is one tokenizer and one set of observations, whichever
    // pool a given job reached it through.
    const key = `${route.route_id}:${this._modelForRoute(route.route_id)}:${promptFamily}`;
    if (cache?.has(key)) return cache.get(key);
    const sql = this._getSql();
    const rows = [...sql.exec("SELECT recent_observed_summary FROM estimates WHERE key = ?", key)];
    const result = calibrationFor(route, parseCalibrationSummary(rows[0]?.recent_observed_summary));
    cache?.set(key, result);
    return result;
  }

  _recordClaimOutcome(
    now, result, reason, diagnostics, bundlesClaimed = 0, leasesClaimed = 0, queuedDelta = 0
  ) {
    const sql = this._getSql();
    const row = [...sql.exec(
      "SELECT claim_empty_count_today, claim_reason_counts_json FROM scheduler WHERE id = 1"
    )][0] || {};
    const reasonCounts = parseJsonObject(row.claim_reason_counts_json);
    reasonCounts[reason] = (Number(reasonCounts[reason]) || 0) + 1;
    const emptyCount = Number(row.claim_empty_count_today) || 0;
    sql.exec(
      `UPDATE scheduler SET last_claim_at=?, last_claim_result=?, last_claim_reason=?,
       last_claim_diagnostics_json=?, claim_empty_count_today=?, claim_reason_counts_json=?,
       bundle_count_today = bundle_count_today + ?,
       lease_count_today = lease_count_today + ?,
       queued_job_count = MAX(0, queued_job_count + ?),
       rows_written_today = rows_written_today + ?
       WHERE id=1`,
      now,
      result,
      reason,
      JSON.stringify(diagnostics),
      result === "empty" ? emptyCount + 1 : emptyCount,
      JSON.stringify(reasonCounts),
      bundlesClaimed,
      leasesClaimed,
      queuedDelta,
      this._takeUnflushedRows()
    );
  }

  static EMPTY_CLAIM_RESULT = { bundle_id: null, execution_token: null, jobs: [] };

  /** review/44 Unit 4: fenced, capacity-ranked admission and pacing in one SQLite transaction. */
  async claimDispatchWindow(now, windowSeconds) {
    const sql = this._getSql();
    // A global pause ends the tick before any statement runs -- not even the day roll or the
    // lease reaper -- so a paused deployment writes zero rows per tick. Expired leases are simply
    // reaped by the first tick after the pause ends.
    const globalPause = this._activePauses(now).find((row) => row.scope === "global");
    if (globalPause) {
      return {
        ...LLMSchedulerDO.EMPTY_CLAIM_RESULT,
        claim_result: "empty",
        claim_reason: "dispatch_paused",
        claim_diagnostics: { paused_until: globalPause.paused_until, reason: globalPause.reason },
      };
    }
    // Provider/route pauses: the same catalog minus the paused routes (see _claimDispatchLimits).
    const dispatchLimits = this._claimDispatchLimits(now);

    return this.ctx.storage.transactionSync(() => {
      this._ensureMigratedJobModels();
      this._ensureQueuedJobCounter();
      const maxBundlesPerDay = this._maxBundlesPerUtcDay();
      const maxActiveBundles = this._maxActiveBundles();
      const maxInFlightCalls = this._maxInFlightLlmCalls();
      const maxBundleJobs = this._maxBundleJobs();
      const maxJobsPerModelClaim = this._maxJobsPerModelClaim();
      const candidateLookahead = Math.max(maxJobsPerModelClaim, this._candidateLookahead());
      const maxConcurrentLanes = this._maxConcurrentRouteLanes();
      const maxJobsPerRoutePerBundle = this._maxJobsPerRoutePerBundle();
      const estimateFloor = this._estimateFloor();
      const callDurationCeilingMs = this._callDurationCeilingMs();
      const leaseDurationMs = this._leaseDurationMs();
      const EMPTY = LLMSchedulerDO.EMPTY_CLAIM_RESULT;
      const diagnostics = {
        active_bundles: 0,
        in_flight_calls: 0,
        candidate_jobs: 0,
        chosen_jobs: 0,
        rejections: {
          route_lane_limit: 0,
          route_bundle_limit: 0,
          route_concurrency: 0,
          provider_concurrency: 0,
          route_capacity: 0,
          dispatch_window_capacity: 0,
        },
        routes: {
          route_concurrency: {},
          provider_concurrency: {},
        },
      };
      // Expired leases this tick returns to queued (set below); folded into the claim snapshot's
      // queued_job_count delta so the counter costs no extra billed row.
      let reapedToQueued = 0;
      const recordEmpty = (reason, extra = {}) => {
        const snapshot = { ...diagnostics, ...extra };
        // Leases reaped this tick went back to queued; no new leases.
        this._recordClaimOutcome(now, "empty", reason, snapshot, 0, 0, reapedToQueued);
        return { ...EMPTY, claim_result: "empty", claim_reason: reason, claim_diagnostics: snapshot };
      };
      const recordClaimed = (jobs) => {
        diagnostics.chosen_jobs = jobs;
        // The bundle, lease and queued counters ride on the same scheduler UPDATE: one billed
        // row per claim.
        this._recordClaimOutcome(
          now, "claimed", "claimed", diagnostics, 1, jobs, reapedToQueued - jobs
        );
      };

      const sched = this._rollUtcDayIfNeeded(now);
      const rowsToday = this._rowsWrittenToday(sched);
      // The daily brake. Past the claim threshold no new lease is claimed, and the empty result
      // is not recorded -- every row left belongs to completing what is already in flight. The
      // row counter is still persisted when rows are pending (completions, acks and retires keep
      // writing), since nothing else flushes it past the enqueue threshold and an eviction would
      // otherwise drop them and reopen the optional-write gate. One row, only when more than one
      // is pending, so an idle braked tick writes nothing.
      if (rowsToday >= this._claimRowStop()) {
        if ((this._rowsUnflushed || 0) > 1) {
          sql.exec(
            "UPDATE scheduler SET rows_written_today = rows_written_today + ? WHERE id = 1",
            this._takeUnflushedRows()
          );
        }
        return {
          ...EMPTY,
          claim_result: "empty",
          claim_reason: "daily_row_budget",
          claim_diagnostics: {
            ...diagnostics,
            rows_written_today: rowsToday,
            claim_row_stop: this._claimRowStop(),
          },
        };
      }
      if (sched.bundle_count_today >= maxBundlesPerDay) {
        return recordEmpty("daily_bundle_limit", {
          bundles_today: sched.bundle_count_today,
          max_bundles_per_day: maxBundlesPerDay,
        });
      }
      // The dispatch half of the DO row-write budget (write_budget.js): each lease costs up to
      // ~40 billed rows through retirement. Bundles alone did not bound it -- 1,400 bundles of 5
      // jobs could write ~280k rows against the account's 100,000/day.
      const maxLeasesPerDay = this._maxLeasesPerUtcDay();
      const leasesRemaining = Math.max(0, maxLeasesPerDay - (Number(sched.lease_count_today) || 0));
      if (leasesRemaining <= 0) {
        return recordEmpty("daily_lease_limit", {
          leases_today: Number(sched.lease_count_today) || 0,
          max_leases_per_day: maxLeasesPerDay,
        });
      }
      const bundleJobLimit = Math.min(maxBundleJobs, leasesRemaining);

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
      reapedToQueued = expiredJobs.length;
      if (expiredJobs.length > 0) {
        // Give the route back what these dead leases were holding. completeBatch is the only
        // other place provisional_reservation is ever decremented, and by definition it never
        // ran for these -- so without this every crashed or evicted bundle permanently consumed
        // a slice of its route's token capacity. That leak is silent and cumulative: the route
        // stays unblocked and simply scores lower every time, until it stops being ranked at all.
        for (const job of expiredJobs) {
          if (!job.lease_route_id || !(Number(job.token_reservation) > 0)) continue;
          sql.exec(
            `UPDATE routes SET provisional_reservation = MAX(0, provisional_reservation - ?)
             WHERE route_id = ?`,
            job.token_reservation,
            job.lease_route_id
          );
        }
        sql.exec(
          `UPDATE jobs SET state='queued', lease_token=NULL, lease_route_id=NULL,
                            lease_expires_at=NULL, bundle_id=NULL, token_reservation=0, updated_at=?
           WHERE state='leased' AND lease_expires_at < ?`,
          now,
          now
        );
        for (const job of expiredJobs) this._indexQueuedJobModels(job);
      }

      // Retention runs right after the expire-sweep, so a bundle this tick just marked
      // 'expired' is eligible on a later tick once it ages out. Bounded per tick; see
      // _pruneTerminalRecords.
      // Retention pruning is deferrable: past the enqueue threshold it waits for tomorrow.
      if (rowsToday < this._enqueueRowStop()) this._pruneTerminalRecords(now);

      const activeBundles = [...sql.exec("SELECT active_call_count FROM bundles WHERE state='active'")];
      diagnostics.active_bundles = activeBundles.length;
      if (activeBundles.length >= maxActiveBundles) {
        return recordEmpty("active_bundle_limit", { max_active_bundles: maxActiveBundles });
      }
      const inFlightCalls = activeBundles.reduce((sum, b) => sum + b.active_call_count, 0);
      diagnostics.in_flight_calls = inFlightCalls;
      if (inFlightCalls >= maxInFlightCalls) {
        return recordEmpty("in_flight_call_limit", { max_in_flight_calls: maxInFlightCalls });
      }

      const inFlightByRoute = new Map();
      const inFlightByProvider = new Map();
      const leasedRows = [...sql.exec(
        "SELECT lease_route_id, COUNT(*) as cnt FROM jobs WHERE state='leased' GROUP BY lease_route_id"
      )];
      for (const row of leasedRows) {
        if (!row.lease_route_id) continue;
        inFlightByRoute.set(row.lease_route_id, row.cnt);
        const catRoute = dispatchLimits.routes_by_id?.[row.lease_route_id];
        if (catRoute?.provider) {
          const prev = inFlightByProvider.get(catRoute.provider) || 0;
          inFlightByProvider.set(catRoute.provider, prev + row.cnt);
        }
      }

      const ledgerCache = new Map();
      const getMergedRoute = (route) => {
        if (!ledgerCache.has(route.route_id)) {
          const ledger = this._getOrCreateRouteLedger(route.route_id, now, route);
          ledgerCache.set(route.route_id, {
            ...route,
            ...ledger,
            reset_timezone:
              dispatchLimits?.providers?.[route.provider]?.reset_timezone || "UTC",
          });
        }
        return ledgerCache.get(route.route_id);
      };

      const providerLedgerCache = new Map();
      // Ledgers advanced by this claim, written once each just before the bundle row (P1).
      const dirtyRoutes = new Set();
      const dirtyProviders = new Set();
      const getMergedProvider = (providerName, providerCfg) => {
        if (!providerLedgerCache.has(providerName)) {
          const pLedger = this._getOrCreateProviderLedger(providerName, now, providerCfg);
          providerLedgerCache.set(providerName, pLedger);
        }
        return providerLedgerCache.get(providerName);
      };

      const calibrationCache = new Map();
      const capacityOptions = (route, job) => {
        const providerCfg = dispatchLimits.providers?.[route.provider];
        const providerLedger = route.provider
          ? getMergedProvider(route.provider, providerCfg)
          : null;
        const { inputRatio, outputForecast } = this._calibration(
          route,
          job.prompt_family,
          calibrationCache
        );
        return {
          estimateFloor,
          inputRatio,
          outputForecast,
          callDurationCeilingMs,
          providerConfig: providerCfg,
          providerLedger,
        };
      };

      const chosen = [];
      const chosenJobIds = new Set();
      const seenRoutes = new Set();
      const modelPlans = this._rankModelsByCapacity(now, windowSeconds, dispatchLimits);
      const modelPlansByModel = new Map(modelPlans.map((plan) => [plan.model, plan]));
      for (const modelPlan of modelPlans) {
        if (chosen.length >= bundleJobLimit) break;
        // Head-of-line guard. Reading only the oldest maxJobsPerModelClaim entries let a few jobs
        // too large for every route that currently has capacity (e.g. 10-14k-token Gemma batches
        // while the 10k-ceiling AI Studio routes were the only Gemma legs with headroom) block
        // the whole model every tick, with hundreds of servable jobs queued behind them. When the
        // model's available routes all carry a size ceiling, read a bounded lookahead instead and
        // let the exact, calibrated per-job check below (routeHasCapacityFor) decide each row;
        // maxJobsPerModelClaim then caps jobs ACCEPTED for this model, not rows read. No size
        // filter in SQL: a static-ratio prefilter could disagree with the learned ratio and
        // re-create the stall it exists to prevent.
        const ceilingBound = this._modelIsCeilingBound(modelPlan, dispatchLimits);
        const candidates = [...sql.exec(
          `SELECT jobs.* FROM job_models
           JOIN jobs ON jobs.id = job_models.job_id
           WHERE job_models.model = ? AND jobs.state = 'queued'
           ORDER BY job_models.priority ASC, job_models.created_at ASC, job_models.job_id ASC
           LIMIT ?`,
          modelPlan.model,
          ceilingBound ? candidateLookahead : maxJobsPerModelClaim
        )];
        let acceptedForModel = 0;

        for (const job of candidates) {
          if (chosen.length >= bundleJobLimit) break;
          if (acceptedForModel >= maxJobsPerModelClaim) break;
          if (chosenJobIds.has(job.id)) continue;
          diagnostics.candidate_jobs += 1;
          // The capacity-ranked model index is a bounded way to *find* work, not permission to
          // force that discovery model onto the job. A job is indexed under every explicit
          // alternate; once found, rank all of its eligible routes. Otherwise the first tied
          // model alphabetically (usually Gemini) claims the whole bundle and a later Llama
          // alternate is never considered, even when fewer than maxBundleJobs are available.
          // Stable sort preserves the caller's allowed_models order when live capacity ties.
          const eligibleRoutes = routesEligibleFor(job, dispatchLimits).sort((left, right) => {
            const leftPlan = modelPlansByModel.get(left.model);
            const rightPlan = modelPlansByModel.get(right.model);
            return (
              // Free before paid, ahead of any capacity signal. `allow_paid` is permission to
              // spend when nothing free will do, not a preference for spending: without this
              // term a paid route with more headroom outranks a partly-consumed free one and
              // silently bills for work a free route could have taken. The loop below stops at
              // the first route with capacity, so paid is reached only once every free route is
              // exhausted. (v1's selectRouteForModel additionally waits for a free route to
              // reset unless that would miss the job's deadline; v2 has no deadline concept
              // here, so it elevates as soon as free capacity runs out.)
              Number(Boolean(right.free)) - Number(Boolean(left.free)) ||
              (rightPlan?.score || 0) - (leftPlan?.score || 0) ||
              (rightPlan?.routeScores.get(right.route_id) || 0) -
                (leftPlan?.routeScores.get(left.route_id) || 0)
            );
          });
          const routeOrder = [
            ...eligibleRoutes.filter((route) => !seenRoutes.has(route.route_id)),
            ...eligibleRoutes.filter((route) => seenRoutes.has(route.route_id)),
          ];
          for (const route of routeOrder) {
            const isNewLane = !seenRoutes.has(route.route_id);
            if (isNewLane && seenRoutes.size >= maxConcurrentLanes) {
              diagnostics.rejections.route_lane_limit += 1;
              continue;
            }
            const countInBundle = chosen.filter(
              (entry) => entry.route.route_id === route.route_id
            ).length;
            if (countInBundle >= maxJobsPerRoutePerBundle) {
              diagnostics.rejections.route_bundle_limit += 1;
              continue;
            }

            const routeConcurrency = Number(route.concurrency);
            if (Number.isFinite(routeConcurrency) && routeConcurrency > 0) {
              const curInFlight = (inFlightByRoute.get(route.route_id) || 0) + countInBundle;
              if (curInFlight >= routeConcurrency) {
                diagnostics.rejections.route_concurrency += 1;
                diagnostics.routes.route_concurrency[route.route_id] =
                  (diagnostics.routes.route_concurrency[route.route_id] || 0) + 1;
                continue;
              }
            }

            const providerCfg = dispatchLimits.providers?.[route.provider];
            const providerConcurrency = Number(
              providerCfg?.concurrency ?? route.provider_concurrency
            );
            if (Number.isFinite(providerConcurrency) && providerConcurrency > 0) {
              const curProviderInFlight =
                (inFlightByProvider.get(route.provider) || 0) +
                chosen.filter((entry) => entry.route.provider === route.provider).length;
              if (curProviderInFlight >= providerConcurrency) {
                diagnostics.rejections.provider_concurrency += 1;
                diagnostics.routes.provider_concurrency[route.provider] =
                  (diagnostics.routes.provider_concurrency[route.provider] || 0) + 1;
                continue;
              }
            }

            const merged = getMergedRoute(route);
            if (
              !routeHasCapacityFor(merged, job, now, windowSeconds, capacityOptions(route, job))
            ) {
              diagnostics.rejections.route_capacity += 1;
              continue;
            }
            chosen.push({ job, route });
            chosenJobIds.add(job.id);
            seenRoutes.add(route.route_id);
            acceptedForModel += 1;
            break;
          }
        }
      }

      if (chosen.length === 0) {
        // This branch is taken on nearly every tick. Read the delta-maintained singleton rather
        // than probing queued jobs: the exact classification remains constant-cost even when the
        // backlog is much larger than the 1,000-row cap that previously bounded this query. Leases
        // this tick's sweep just requeued are not in the stored count yet (recordEmpty applies
        // them), so add them here or the tick would report no_queued_work with work queued.
        const storedQueued = [...sql.exec(
          "SELECT queued_job_count FROM scheduler WHERE id = 1"
        )][0]?.queued_job_count || 0;
        const queuedCount = Math.max(0, storedQueued + reapedToQueued);
        const concurrencyRejected =
          diagnostics.rejections.route_concurrency + diagnostics.rejections.provider_concurrency;
        const otherRejected =
          diagnostics.rejections.route_lane_limit +
          diagnostics.rejections.route_bundle_limit +
          diagnostics.rejections.route_capacity;
        const reason =
          queuedCount === 0
            ? "no_queued_work"
            : diagnostics.candidate_jobs === 0
              ? "no_ranked_candidates"
              : concurrencyRejected > 0 && otherRejected === 0
                ? "concurrency_limit"
                : concurrencyRejected > 0
                  ? "mixed_admission_limits"
                  : diagnostics.rejections.route_capacity > 0
                    ? "route_capacity"
                    : "no_eligible_route";
        return recordEmpty(reason, { queued_jobs: queuedCount });
      }

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
        const providerCfg = dispatchLimits.providers?.[route.provider];
        for (const job of routeJobs) {
          const { inputRatio, outputForecast } = this._calibration(
            route,
            job.prompt_family,
            calibrationCache
          );
          const providerLedger = route.provider
            ? getMergedProvider(route.provider, providerCfg)
            : null;
          const waitResult = computeRouteLaneWait(workingRoute, job, laneTime, now, {
            estimateFloor,
            inputRatio,
            outputForecast,
            providerConfig: providerCfg,
            providerLedger,
          });
          if (waitResult === null) {
            diagnostics.rejections.dispatch_window_capacity += 1;
            continue; // exceeds this route's burst capacity outright
          }
          if (waitResult.not_before_at + callDurationCeilingMs > dispatchWindowEnd) {
            diagnostics.rejections.dispatch_window_capacity += 1;
            continue;
          }

          const leaseToken = crypto.randomUUID();

          workingRoute = this._applyProvisionalReservation(
            workingRoute,
            waitResult.reservation,
            waitResult.not_before_at
          );
          ledgerCache.set(route.route_id, workingRoute);
          dirtyRoutes.add(route.route_id);

          sql.exec(
            `UPDATE jobs SET state='leased', lease_token=?, lease_route_id=?, lease_expires_at=?,
                              bundle_id=?, token_reservation=?,
                              reservation_rpm_window_start=?, reservation_rpd_day_key=?,
                              reservation_tpm_window_start=?, updated_at=? WHERE id=?`,
            leaseToken,
            route.route_id,
            leaseExpiresAt,
            bundleId,
            // Persisted so the expire-sweep can release exactly what this lease is holding. The
            // amount previously lived only in the claim response and the completeBatch result, so
            // a bundle that died before reporting leaked its reservation onto the route forever.
            waitResult.reservation,
            // The route's window identity as of THIS claim (post-_applyProvisionalReservation,
            // i.e. what the route row now actually says) -- so a later non-consuming refund in
            // completeBatch can tell whether the route's current window is still this one, or has
            // since rolled over, before decrementing rpm_count/rpd_count/tpm_reserved (CodeRabbit,
            // 2026-09-13: refunding into whatever window happens to be current could otherwise
            // undercount a newer reservation that has nothing to do with this one).
            workingRoute.rpm_window_start,
            workingRoute.rpd_day_key,
            workingRoute.tpm_window_start,
            now,
            job.id
          );
          // Keep the model index queue-only: a completed historical backlog must never make a
          // later model lookup walk terminal rows before it reaches current work.
          sql.exec("DELETE FROM job_models WHERE job_id = ?", job.id);

          if (route.provider && providerCfg?.tpm) {
            let pWorking = getMergedProvider(route.provider, providerCfg);
            pWorking = this._applyProviderProvisionalReservation(
              pWorking,
              providerCfg,
              waitResult.reservation,
              waitResult.not_before_at
            );
            providerLedgerCache.set(route.provider, pWorking);
            dirtyProviders.add(route.provider);
          }

          resultJobs.push({
            id: job.id,
            payload_key: job.payload_key,
            lease_token: leaseToken,
            route_id: route.route_id,
            // For per-lane request shaping at send time (reasoning level, output budget): the
            // lane comes from the job's own policy and costs no extra row reads or writes.
            purpose: this._purposeForJob(job),
            input_token_estimate: Number(job.input_token_estimate || 0),
            token_reservation: waitResult.reservation,
            wait_ms: waitResult.wait_ms,
            not_before_at: waitResult.not_before_at,
            min_inter_request_gap_ms: waitResult.min_inter_request_gap_ms,
          });
          laneTime = waitResult.not_before_at + waitResult.min_inter_request_gap_ms;
        }
      }

      if (resultJobs.length === 0) {
        return recordEmpty("dispatch_window_capacity");
      }

      for (const routeId of dirtyRoutes) this._writeRouteLedger(ledgerCache.get(routeId));
      for (const provider of dirtyProviders) {
        this._writeProviderLedger(providerLedgerCache.get(provider));
      }

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
      recordClaimed(resultJobs.length);

      return {
        bundle_id: bundleId,
        execution_token: executionToken,
        jobs: resultJobs,
        claim_result: "claimed",
        claim_reason: "claimed",
        claim_diagnostics: diagnostics,
      };
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
      // No updated_at bump: it is indexed (idx_jobs_state_updated_id), so bumping it cost two
      // billed rows per attempt, and nothing reads updated_at for a non-terminal job.
      sql.exec("UPDATE jobs SET attempts = attempts + 1 WHERE id = ?", jobId);
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
  async authorizeRetry(jobId, leaseToken, attemptId, now, retryAfterSeconds, failureClass = "unknown_429") {
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

      const routeId = job.lease_route_id;
      this._recordRouteFailure(sql, now, routeId, failureClass, 429);

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

      const ledger = this._getOrCreateRouteLedger(routeId, now, {});
      const dispatchLimits = this._dispatchLimits();
      const route = dispatchLimits?.routes_by_id?.[routeId];

      switch (failureClass) {
        case "own_rpd": {
          const rpd = route?.rpd || 0;
          const tz = routeResetTimezone(route);
          const dayKey = zonedDateKey(now, tz);
          const midnightMs = nextZonedMidnightMs(now, tz);
          sql.exec(
            `UPDATE routes SET rpd_count = ?, rpd_day_key = ?, last_provider_status = 429,
                               last_failure_class = ?, blocked_until = MAX(COALESCE(blocked_until, 0), ?)
             WHERE route_id = ?`,
            rpd,
            dayKey,
            failureClass,
            midnightMs,
            routeId
          );
          // Daily quota exhausted: do not retry in-window; requeue immediately so siblings can serve.
          return { authorized: false, retry_not_before: null };
        }
        case "own_tpm": {
          const tpm = route?.tpm || 0;
          sql.exec(
            `UPDATE routes SET full_token_budget = 0, token_budget_updated_at = ?,
                               tpm_reserved = ?, tpm_window_start = ?,
                               last_provider_status = 429, last_failure_class = ?
             WHERE route_id = ?`,
            now,
            tpm,
            now,
            failureClass,
            routeId
          );
          // Token budget exhausted: do not retry in-window.
          return { authorized: false, retry_not_before: null };
        }
        case "upstream_capacity": {
          const streak = (ledger.upstream_capacity_streak || 0) + 1;
          const blockedUntil = this._upstreamCapacityBlockedUntil(streak, now);
          // Clearing throttle_streak/buffer_seconds here is not housekeeping -- it is the
          // conclusion this classification licenses. The provider just told us its own pool is
          // saturated, which is positive evidence our pacing is NOT the problem, so any own-rate
          // penalty still on the row is stale and must go. Without this, penalties accrued before
          // the classifier existed never clear, because the only other reset path requires a
          // success and these routes rarely get one: observed live 2026-09-09 on
          // openrouter_google_gemma_4_31b_it_free, carrying throttle_streak=465 and
          // buffer_seconds=60 while every one of that day's 19 failures classified
          // upstream_capacity. (That route then succeeded on the 3rd attempt of an endurance
          // probe, confirming it was merely busy, never rate-limited by us.)
          sql.exec(
            `UPDATE routes SET upstream_capacity_streak = ?, last_provider_status = 429,
                               last_failure_class = ?, throttle_streak = 0,
                               buffer_seconds = 0, buffer_updated_at = 0,
                               blocked_until = MAX(COALESCE(blocked_until, 0), ?)
             WHERE route_id = ?`,
            streak,
            failureClass,
            blockedUntil,
            routeId
          );
          return { authorized: false, retry_not_before: null };
        }
        case "gateway_limit": {
          const streak = (ledger.upstream_capacity_streak || 0) + 1;
          const blockedUntil = this._upstreamCapacityBlockedUntil(streak, now);
          const provider = route?.provider;
          if (provider) {
            for (const [otherRouteId, otherRoute] of Object.entries(dispatchLimits?.routes_by_id || {})) {
              if (otherRoute.provider === provider) {
                this._getOrCreateRouteLedger(otherRouteId, now, {});
                sql.exec(
                  `UPDATE routes SET upstream_capacity_streak = ?, last_provider_status = 429,
                                     last_failure_class = ?, blocked_until = MAX(COALESCE(blocked_until, 0), ?)
                   WHERE route_id = ?`,
                  streak,
                  failureClass,
                  blockedUntil,
                  otherRouteId
                );
              }
            }
          } else {
            sql.exec(
              `UPDATE routes SET upstream_capacity_streak = ?, last_provider_status = 429,
                                 last_failure_class = ?, blocked_until = MAX(COALESCE(blocked_until, 0), ?)
               WHERE route_id = ?`,
              streak,
              failureClass,
              blockedUntil,
              routeId
            );
          }
          return { authorized: false, retry_not_before: null };
        }
        case "payment_required": {
          // A billing state (zero-provisioned-limit, insufficient-budget) does not clear on a
          // retry cadence -- it must reach the day -> week -> month cooldown ladder, same as a
          // direct HTTP 402 does in completeBatch, not the short own_rpm-shaped backoff the
          // default branch below would otherwise give it (CodeRabbit, 2026-09-13: these two
          // classification rules can both fire on a 429, and this switch had no case for them).
          const newStreak = (ledger.payment_required_streak || 0) + 1;
          sql.exec(
            `UPDATE routes SET payment_required_streak = ?, blocked_until = ?,
                               last_provider_status = 429, last_failure_class = ?
             WHERE route_id = ?`,
            newStreak,
            paymentRequiredBackoffUntil(newStreak, now),
            failureClass,
            routeId
          );
          return { authorized: false, retry_not_before: null };
        }
        case "request_defect": {
          // A request defect (e.g. OrcaRouter free-tier prompt cap exceeded with no Retry-After)
          // cannot succeed by retrying unchanged. Refuse in-batch retry immediately.
          sql.exec(
            `UPDATE routes SET last_provider_status = 429, last_failure_class = ?
             WHERE route_id = ?`,
            failureClass,
            routeId
          );
          return { authorized: false, retry_not_before: null };
        }
        case "own_rpm":
        case "unknown_429":
        default: {
          const newStreak = (ledger.throttle_streak || 0) + 1;
          const maxBufferSeconds = this._maxRouteBufferSeconds();
          const rawRetryAfterSeconds = Number(retryAfterSeconds);
          // A provider-supplied Retry-After is an authoritative floor, not a backoff suggestion.
          let retryAfterSec =
            Number.isFinite(rawRetryAfterSeconds) && rawRetryAfterSeconds > 0
              ? rawRetryAfterSeconds
              : null;
          if (route?.retry_after_trustworthy === false && route?.observed_recovery_seconds) {
            const observedSec = Number(route.observed_recovery_seconds);
            if (Number.isFinite(observedSec) && observedSec > 0) {
              retryAfterSec =
                retryAfterSec !== null ? Math.max(retryAfterSec, observedSec) : observedSec;
            }
          }
          const addedBufferSeconds = retryAfterSec !== null
            ? Math.min(maxBufferSeconds, retryAfterSec)
            : this._max429BackoffMs() / 1000;
          const newBufferSeconds = Math.min(
            maxBufferSeconds,
            effectiveBufferSeconds(ledger, now) + addedBufferSeconds
          );
          sql.exec(
            `UPDATE routes SET throttle_streak = ?, last_provider_status = 429,
                               last_failure_class = ?, buffer_seconds = ?, buffer_updated_at = ?,
                               blocked_until = MAX(COALESCE(blocked_until, 0), ?)
             WHERE route_id = ?`,
            newStreak,
            failureClass,
            newBufferSeconds,
            now,
            retryAfterSec !== null ? now + Math.ceil(retryAfterSec * 1000) : 0,
            routeId
          );

          const baseBackoffMs = retryAfterSec !== null
            ? retryAfterSec * 1000
            : Math.min(this._max429BackoffMs(), newStreak * 1000);
          const backoffMs = retryAfterSec !== null
            ? Math.ceil(baseBackoffMs + Math.random() * Math.min(1000, baseBackoffMs * 0.1))
            : Math.round(baseBackoffMs * (0.5 + Math.random()));
          const retryNotBefore = now + backoffMs;
          const deadline = Math.min(bundle.dispatch_window_end, bundle.lease_expires_at);
          if (retryNotBefore >= deadline) {
            return { authorized: false, retry_not_before: null };
          }

          return { authorized: true, retry_not_before: retryNotBefore };
        }
      }
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

      this._ensureQueuedJobCounter();
      const now = Date.now();
      let settledCount = 0;
      let requeuedCount = 0;

      // Success settlements, folded per route and written once (P1): a bundle whose jobs share a
      // route used to rewrite that routes row once per success. Any non-success write to a route
      // flushes its pending successes first, so statement order -- and therefore the final
      // backoff state (a later failure's blocked_until must survive an earlier success's reset)
      // -- is exactly what per-job writes produced.
      const pendingSuccess = new Map();
      const flushSuccess = (routeId) => {
        const pending = pendingSuccess.get(routeId);
        if (!pending) return;
        pendingSuccess.delete(routeId);
        // tpm_reserved is adjusted only by settlements whose claim-time window is still the
        // route's current one; each settlement recorded its window, so pick that window's sum.
        const current = [...sql.exec(
          "SELECT tpm_window_start FROM routes WHERE route_id = ?",
          routeId
        )][0];
        const windowDelta = current ? (pending.windowDeltas.get(current.tpm_window_start) ?? 0) : 0;
        sql.exec(
          `UPDATE routes SET
             provisional_reservation = MAX(0, provisional_reservation - ?),
             settled_usage = settled_usage + ?,
             tpm_reserved = MAX(0, tpm_reserved - ?),
             full_token_budget = full_token_budget + ?,
             throttle_streak = 0, buffer_seconds = 0, buffer_updated_at = 0,
             payment_required_streak = 0, upstream_capacity_streak = 0,
             last_failure_class = '', blocked_until = NULL, last_provider_status = ?
           WHERE route_id = ?`,
          pending.reservation,
          pending.settledUsage,
          windowDelta,
          pending.budgetDelta,
          pending.lastStatus,
          routeId
        );
      };

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

        // A job with configured backup_models must actually survive long enough to try them.
        // Every class-specific retry ceiling below (MAX_5XX_RETRIES, MAX_UPSTREAM_CAPACITY_RETRIES)
        // is tuned for a job with no fallback -- confirmed live: a raw 5xx (e.g. NVIDIA's own
        // "Service temporarily overloaded" 503) classifies as `server_error` (classify.js's HTTP
        // 5xx rule runs before any 429/400 check), gated by MAX_5XX_RETRIES=1, i.e. only 2 total
        // attempts before terminal failure -- nowhere near a `backup_after_attempts` in the 5-20
        // range backupModelsActive (routes.js) expects. Without this, backups configured for
        // exactly this failure mode would almost never actually be reached. Once backups are
        // configured, every ceiling below is raised to `backup_after_attempts + <its own normal
        // budget>`: the job survives (at minimum) to backup eligibility on `attempts` alone, then
        // gets its ordinary per-class retry allowance again while a backup model is in play,
        // rather than an untested, unbounded extension.
        const backupPolicy = jobPolicy(job);
        const backupAfterAttempts =
          Array.isArray(backupPolicy?.backup_models) &&
          backupPolicy.backup_models.length > 0 &&
          Number.isInteger(backupPolicy?.backup_after_attempts) &&
          backupPolicy.backup_after_attempts > 0
            ? backupPolicy.backup_after_attempts
            : 0;
        const _retryCeiling = (base) =>
          backupAfterAttempts > 0 ? backupAfterAttempts + base : base;

        // A structured reply that was empty or not JSON (review/48 R10) is also the route's
        // problem, not the job's: same budget, same escalating per-route cooldown (reset by the
        // route's next success), so the job moves to another route while this one stands down.
        const isUpstreamClass =
          result.failure_class === "upstream_capacity" ||
          result.failure_class === "gateway_limit" ||
          result.failure_class === "structured_output_empty" ||
          result.failure_class === "structured_output_invalid";
        // The reply stopped at its output-token limit (finish_reason "length"): the job's own
        // shape -- a budget too small for this model's reasoning -- not the route's health. It
        // retries on the upstream budget (another route or a later attempt may fit) but does NOT
        // cool the route down; it is counted in route_failures so the budget monitor sees it.
        const isOutputBudgetExhausted = result.failure_class === "output_budget_exhausted";
        const isFinal5xx =
          result.outcome === "retryable_error" &&
          Number.isInteger(result.provider_status_code) &&
          result.provider_status_code >= 500 &&
          result.provider_status_code <= 599 &&
          !isUpstreamClass;
        // A 402 requeues rather than failing: the route is blocked below, so the job cannot
        // re-probe it, and it runs on an overflow route or once the cooldown clears. It shares
        // the 5xx retry budget so a route that stays 402 across every cooldown cannot requeue a
        // job forever -- v1 bounds the same case with `attempts < maxAttempts`.
        const isPaymentRequired =
          result.outcome === "retryable_error" &&
          result.provider_status_code === 402 &&
          job.transient_retry_count < _retryCeiling(this._max5xxRetries());
        // A 400 only ever arrives as `retryable_error` when the dispatcher read the body and found
        // the provider blaming its own upstream (see gateway.js's upstreamCapacityFailure). The DO
        // never sees payloads, so the pair (retryable_error, 400) is the whole signal here. The
        // route is unreachable rather than the job defective, so it gets the 5xx treatment: one
        // durable retry from the shared budget, and the route stood down so sibling jobs are not
        // admitted onto a model that would destroy them in turn.
        const isUpstreamCapacityFailure =
          result.outcome === "retryable_error" && result.provider_status_code === 400;
        const isTransientRouteFailure = isFinal5xx || isUpstreamCapacityFailure;
        const nextTransientRetryCount = (job.transient_retry_count || 0) + 1;
        const isRouteInputLimit = result.failure_class === "route_input_limit";
        // The route's model is retired (410 -- classify.js rule 8). Not this job's fault, so it
        // draws on the larger upstream budget and the route itself is stood down for hours.
        const isRouteUnavailable = result.failure_class === "route_unavailable";
        const blockedUntil = isTransientRouteFailure || isRouteInputLimit
          ? this._5xxBlockedUntil(nextTransientRetryCount, now)
          : null;
        // An upstream/gateway failure is by definition not this job's fault and recurs on its own
        // schedule, so it gets its own, larger budget rather than sharing the single 5xx slot.
        // With MAX_5XX_RETRIES=1 shared across every transient cause, two unrelated upstream
        // blips destroyed a job that had done nothing wrong -- 26 such events landed on just two
        // routes on 2026-09-09 alone.
        const isUpstreamRetryable =
          isUpstreamClass &&
          job.transient_retry_count < _retryCeiling(this._maxUpstreamCapacityRetries());
        const isRateLimitTerminal =
          result.provider_status_code === 429 &&
          job.transient_retry_count < _retryCeiling(this._max5xxRetries());
        const shouldRetry5xx =
          isTransientRouteFailure &&
          job.transient_retry_count < _retryCeiling(this._max5xxRetries());
        const shouldRetryRouteInputLimit =
          isRouteInputLimit &&
          job.transient_retry_count < _retryCeiling(this._max5xxRetries());
        const shouldRetryRouteUnavailable =
          isRouteUnavailable &&
          job.transient_retry_count < _retryCeiling(this._maxUpstreamCapacityRetries());
        const shouldRetryOutputBudget =
          isOutputBudgetExhausted &&
          job.transient_retry_count < _retryCeiling(this._maxUpstreamCapacityRetries());
        const shouldRequeue =
          shouldRetryOutputBudget ||
          shouldRetryRouteUnavailable ||
          shouldRetry5xx ||
          shouldRetryRouteInputLimit ||
          isPaymentRequired ||
          isRateLimitTerminal ||
          isUpstreamRetryable;

        if (result.outcome === "retryable_error" || result.outcome === "terminal_error") {
          if (result.provider_status_code !== 429) {
            const failureClass =
              result.failure_class ||
              (result.provider_status_code === 402
                ? "payment_required"
                : isUpstreamCapacityFailure
                  ? "upstream_capacity"
                  : isFinal5xx
                    ? "server_error"
                    : (Number.isInteger(result.provider_status_code) &&
                       result.provider_status_code >= 400 &&
                       result.provider_status_code < 500)
                      ? "request_defect"
                      : "server_error");
            this._recordRouteFailure(
              sql,
              now,
              job.lease_route_id || routeIdForAttempt,
              failureClass,
              result.provider_status_code
            );
          }
        }

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
            newState = shouldRequeue ? "queued" : "failed";
            break;
          case "terminal_error":
            // review/45 §20.6: a job whose terminal outcome carries provider_status_code === 429
            // must requeue under the transient budget instead of failing.
            newState = shouldRequeue ? "queued" : "failed";
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
          requeuedCount += 1;
        } else if (shouldRequeue) {
          // Must go through this branch, not the generic UPDATE below: claiming a job deletes its
          // job_models index rows, so a requeue that only rewrites `state` leaves the job queued
          // with no index row and holding a stale lease -- claimDispatchWindow can never select it
          // again, stranding it silently.
          sql.exec(
            `UPDATE jobs SET state='queued', lease_token=NULL, lease_route_id=NULL,
                             lease_expires_at=NULL, bundle_id=NULL, transient_retry_count=?,
                             updated_at=? WHERE id=?`,
            nextTransientRetryCount,
            now,
            result.job_id
          );
          this._indexQueuedJobModels(job);
          requeuedCount += 1;
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
          // Prefer the amount actually persisted at claim time: it is what was added to
          // provisional_reservation, so releasing anything else drifts the two apart.
          const reservation =
            Number(job.token_reservation) > 0
              ? Number(job.token_reservation)
              : (result.token_reservation ?? observedTotal ?? 0);
          // A request the provider never served consumed none of OUR quota, so the rpm/rpd slot
          // and the token budget it reserved at claim time must be given back -- otherwise a
          // route that is merely upstream-saturated bleeds its own daily allowance one rejected
          // request at a time. Observed live 2026-09-09: openrouter_google_gemma_4_31b_it_free
          // had burned 75 rpd slots against 19 `upstream_capacity` rejections and zero completions.
          //
          // Deliberately excludes own_rpm / own_rpd / own_tpm (those rejections are proof we DID
          // reach our limit, so the counters are correct and must stand) and unknown_429 (we
          // cannot show it was not ours, so we stay conservative and keep the charge).
          const nonConsuming = NON_CONSUMING_FAILURE_CLASSES.has(result.failure_class || "");
          const settledUsage = nonConsuming ? 0 : (observedTotal ?? reservation);
          // A success folds release, settle-to-actual and the backoff reset into ONE routes
          // UPDATE per route per batch (flushSuccess; each statement on the same row is a
          // separate billed row).
          if (result.outcome !== "success") {
            flushSuccess(job.lease_route_id);
            sql.exec(
              `UPDATE routes SET
                 provisional_reservation = MAX(0, provisional_reservation - ?),
                 settled_usage = settled_usage + ?
               WHERE route_id = ?`,
              reservation,
              settledUsage,
              job.lease_route_id
            );
          }
          if (nonConsuming && result.outcome !== "success") {
            const catalogRoute = this._dispatchLimits()?.routes_by_id?.[job.lease_route_id];
            // Uncapped refill (2026-09-13 redesign, see pacing.js) -- refunding a reservation
            // that was never consumed no longer clamps at `tpm * FULL_TOKEN_BUDGET_WINDOWS`; it
            // just gives the tokens back, same as ordinary refill.
            if (Number(catalogRoute?.tpm) > 0) {
              // Refund each windowed counter only when the route's CURRENT window is still the
              // one this reservation actually counted against at claim time -- claimDispatchWindow
              // persists that identity onto the job row. A route can roll its rpm/rpd/tpm window
              // between claim and this refund (a slow provider call spanning a window boundary is
              // enough), and unconditionally decrementing would then undercount a NEWER
              // reservation with nothing to do with this one (CodeRabbit, 2026-09-13). No such
              // risk for full_token_budget: it is a continuously-refilling bucket, not a
              // discrete window-keyed counter, so giving tokens back is always correct regardless
              // of how much time has passed.
              sql.exec(
                `UPDATE routes SET
                   rpm_count = CASE WHEN rpm_window_start = ? THEN MAX(0, rpm_count - 1) ELSE rpm_count END,
                   rpd_count = CASE WHEN rpd_day_key = ? THEN MAX(0, rpd_count - 1) ELSE rpd_count END,
                   tpm_reserved = CASE WHEN tpm_window_start = ? THEN MAX(0, tpm_reserved - ?) ELSE tpm_reserved END,
                   full_token_budget = full_token_budget + ?
                 WHERE route_id = ?`,
                job.reservation_rpm_window_start,
                job.reservation_rpd_day_key,
                job.reservation_tpm_window_start,
                reservation,
                reservation,
                job.lease_route_id
              );
            } else {
              sql.exec(
                `UPDATE routes SET
                   rpm_count = MAX(0, rpm_count - 1),
                   rpd_count = MAX(0, rpd_count - 1)
                 WHERE route_id = ?`,
                job.lease_route_id
              );
            }
          }

          if (result.outcome === "success") {
            // The whole success settlement, accumulated for flushSuccess, in order of meaning:
            //  1. release the claim-time provisional reservation and record settled usage;
            //  2. settle the reservation to what the provider actually counted. The reservation
            //     is a forecast (calibration.js); without this a job that reserved 15k and used 5k
            //     kept the route's token bucket 10k short. A positive delta refunds, a negative one
            //     debits. _applyProvisionalReservation adds a reservation to the rolling per-minute
            //     window only when it fits in one window (reservation <= tpm), so the window is
            //     adjusted only for reservations it holds, and only while it is still that window;
            //  3. a successful call proves the route healthy -- clear every backoff signal.
            const catalogRoute = this._dispatchLimits()?.routes_by_id?.[job.lease_route_id];
            const routeTpm = Number(catalogRoute?.tpm);
            const delta =
              routeTpm > 0 && observedTotal != null ? reservation - observedTotal : 0;
            let pending = pendingSuccess.get(job.lease_route_id);
            if (!pending) {
              pending = { reservation: 0, settledUsage: 0, budgetDelta: 0, windowDeltas: new Map() };
              pendingSuccess.set(job.lease_route_id, pending);
            }
            pending.reservation += reservation;
            pending.settledUsage += settledUsage;
            pending.budgetDelta += delta;
            pending.lastStatus = result.provider_status_code ?? 200;
            if (routeTpm > 0 && reservation <= routeTpm && delta !== 0) {
              const w = job.reservation_tpm_window_start;
              pending.windowDeltas.set(w, (pending.windowDeltas.get(w) ?? 0) + delta);
            }
          } else if (
            result.provider_status_code === 402 ||
            result.failure_class === "payment_required"
          ) {
            // Payment required / provider quota exhausted -- a billing-layer signal no amount of
            // rpm/rpd/tpm pacing fixes, so force the route unavailable via blocked_until (see
            // pacing.js's paymentRequiredBackoffUntil) instead of letting every future tick keep
            // re-attempting and re-failing against it. Also fires for a 429 the classifier reads
            // as payment_required (zero-provisioned-limit, insufficient-budget) -- authorizeRetry
            // already sets this same cooldown on the attempt that discovered it, but a job whose
            // very first attempt already exceeded its 429 retry budget skips authorizeRetry
            // entirely, so this path is the only one that would otherwise catch it (CodeRabbit,
            // 2026-09-13).
            const ledger = this._getOrCreateRouteLedger(job.lease_route_id, now, {});
            const newStreak = (ledger.payment_required_streak || 0) + 1;
            sql.exec(
              `UPDATE routes SET payment_required_streak = ?, blocked_until = ?,
                                  last_provider_status = ? WHERE route_id = ?`,
              newStreak,
              paymentRequiredBackoffUntil(newStreak, now),
              result.provider_status_code ?? 429,
              job.lease_route_id
            );
          } else if (isRouteUnavailable) {
            // A retired model does not come back on a 5xx-style minute scale. Block the route for
            // ROUTE_UNAVAILABLE_BLOCK_SECONDS so every sibling job stops being admitted onto it;
            // once the block lapses a single attempt re-probes it.
            sql.exec(
              "UPDATE routes SET blocked_until = MAX(COALESCE(blocked_until, 0), ?), " +
                "last_provider_status = ?, last_failure_class = ? WHERE route_id = ?",
              now + this._routeUnavailableBlockMs(),
              result.provider_status_code ?? null,
              result.failure_class,
              job.lease_route_id
            );
          } else if (isRouteInputLimit) {
            // Quarantine a route that cannot serve this input shape, letting the requeued job
            // select a longer-context sibling without a durable per-job exclusion list.
            sql.exec(
              "UPDATE routes SET blocked_until = MAX(COALESCE(blocked_until, 0), ?), " +
                "last_provider_status = ?, last_failure_class = ? WHERE route_id = ?",
              blockedUntil,
              result.provider_status_code ?? null,
              result.failure_class,
              job.lease_route_id
            );
          } else if (isTransientRouteFailure && !isUpstreamClass) {
            // The Gateway has already retried this request. Temporarily remove only this route
            // from the capacity ranking so other models/accounts can drain while it recovers.
            sql.exec(
              `UPDATE routes SET blocked_until = MAX(COALESCE(blocked_until, 0), ?),
                                 last_provider_status = ? WHERE route_id = ?`,
              blockedUntil,
              result.provider_status_code,
              job.lease_route_id
            );
          } else if (isUpstreamClass) {
            // The 404 function-not-found shape is upstream capacity too, but uses this branch so
            // it gets the upstream-capacity retry budget/cooldown rather than the shorter 5xx
            // budget. The other shape -- a 2xx carrying no usable completion (c7a1a6c: "a 2xx
            // with no completion is not a success") -- reaches here as well. Without this branch
            // either saturated route could be reselected on the very next tick and burn through
            // the retry budget back to back instead of backing off between attempts.
            const ledger = this._getOrCreateRouteLedger(job.lease_route_id, now, {});
            const streak = (ledger.upstream_capacity_streak || 0) + 1;
            sql.exec(
              `UPDATE routes SET upstream_capacity_streak = ?,
                                 blocked_until = MAX(COALESCE(blocked_until, 0), ?),
                                 last_provider_status = ? WHERE route_id = ?`,
              streak,
              this._upstreamCapacityBlockedUntil(streak, now),
              result.provider_status_code ?? null,
              job.lease_route_id
            );
          } else if (result.provider_status_code != null) {
            sql.exec(
              "UPDATE routes SET last_provider_status = ? WHERE route_id = ?",
              result.provider_status_code,
              job.lease_route_id
            );
          }

          // Unit 7 calibration, now a bounded recent window of input ratios and output sizes
          // (calibration.js) rather than a never-decreasing total.
          if (observedTotal != null) {
            this._calibrateEstimate(job.lease_route_id, job.prompt_family, observedTotal, now, {
              jobId: job.id,
              inputEstimate: job.input_token_estimate,
              observedInput: result.observed_input_tokens,
              observedOutput: result.observed_output_tokens,
            });
          }
        }
      }

      for (const routeId of [...pendingSuccess.keys()]) flushSuccess(routeId);

      if (requeuedCount > 0) {
        // One scheduler write per completeBatch, not per requeued job (see the queued-counter
        // note in _initSchema).
        sql.exec(
          "UPDATE scheduler SET queued_job_count = queued_job_count + ? WHERE id = 1",
          requeuedCount
        );
      }

      if (settledCount > 0) {
        const remainingLeased = [...sql.exec(
          "SELECT COUNT(*) as n FROM jobs WHERE bundle_id = ? AND state = 'leased'",
          bundleId
        )];
        if ((remainingLeased[0]?.n || 0) === 0) {
          // Deleted, not marked 'completed' (P3): nothing reads a finished bundle, and marking
          // then pruning it later cost ~5 billed rows against the delete's 2. A late duplicate
          // completeBatch for it now fails the execution-token check and no-ops, which is what
          // it did in effect before -- every job's lease was already settled.
          sql.exec("DELETE FROM bundles WHERE bundle_id = ?", bundleId);
        }
      }
    });
  }

  /** Unit 7: never decrease an existing margin; insert the configured floor as the first
   * observation for a route:model:prompt_family key. `model` is looked up from the route's own
   * lease, since jobs does not itself store the resolved model name. */
  _calibrateEstimate(routeId, promptFamily, observedTotal, now, sample = {}) {
    const sql = this._getSql();
    const model = this._modelForRoute(routeId);
    const key = `${routeId}:${model}:${promptFamily}`;
    const existing = [...sql.exec(
      "SELECT margin_tokens, recent_observed_summary FROM estimates WHERE key = ?",
      key
    )];
    const parsed = parseCalibrationSummary(existing[0]?.recent_observed_summary);
    // Once the window is full, persist only a deterministic 1-in-CALIBRATION_SAMPLE_EVERY sample
    // (by job id): each estimates UPDATE is a billed row per completion, and a full 32-sample
    // p95 window stays representative when refreshed at a quarter of the rate. The window is
    // filled at full rate first, so new routes/prompt families still converge quickly.
    if (
      existing.length > 0 &&
      parsed.r.length >= CALIBRATION_WINDOW &&
      parsed.o.length >= CALIBRATION_WINDOW &&
      sampleBucket(sample.jobId, CALIBRATION_SAMPLE_EVERY) !== 0
    ) {
      return;
    }
    const summary = JSON.stringify(recordCalibrationSample(parsed, sample));
    // margin_tokens stays a high-water mark for diagnostics only; admission no longer reads it.
    if (existing.length === 0) {
      sql.exec(
        `INSERT INTO estimates (key, margin_tokens, sample_count, recent_observed_summary, updated_at)
         VALUES (?, ?, 1, ?, ?)`,
        key,
        Math.max(observedTotal, this._estimateFloor()),
        summary,
        now
      );
      return;
    }
    sql.exec(
      `UPDATE estimates SET margin_tokens = MAX(margin_tokens, ?), sample_count = sample_count + 1,
                            recent_observed_summary = ?, updated_at = ? WHERE key = ?`,
      observedTotal,
      summary,
      now,
      key
    );
  }

  _modelForRoute(routeId) {
    const catalog = this._dispatchLimits();
    const primary = modelForRouteId(routeId, catalog);
    if (primary) return primary;
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
  /**
   * Exact queued-count recount, called hourly by the scheduled cleanup (index.js): heals any
   * transition the explicit deltas miss and direct Data Studio edits. Deliberately NOT on a
   * per-tick path -- it reads one index entry per queued job, so it scales with queue depth.
   * Writes one row, and only when the stored count has drifted.
   */
  async recountQueuedJobs() {
    const sql = this._getSql();
    if (this._readRowsWrittenToday() >= this._enqueueRowStop()) return { queued: null, skipped: true };
    return this.ctx.storage.transactionSync(() => {
      this._ensureQueuedJobCounter();
      const actual = [...sql.exec("SELECT COUNT(*) AS n FROM jobs WHERE state = 'queued'")][0]?.n || 0;
      sql.exec(
        "UPDATE scheduler SET queued_job_count = ? WHERE id = 1 AND queued_job_count <> ?",
        actual,
        actual
      );
      return { queued: actual };
    });
  }

  async purgePendingBatch(limit) {
    const sql = this._getSql();
    // Cleanup is deferrable: past the enqueue threshold the backlog waits for tomorrow.
    if (this._readRowsWrittenToday() >= this._enqueueRowStop()) return { jobs: [] };
    const retentionDays = this._envInt("COMPLETED_RETENTION_DAYS", 38);
    const cutoff = Date.now() - retentionDays * 86_400_000;
    return this.ctx.storage.transactionSync(() => {
      // Rows ALREADY in purge_pending come first, and are re-listed on every call until
      // confirmPurge actually removes them. Two ways a job gets there without this pass having
      // put it there: ackResults promoted it (the client consumed its result), or an earlier
      // cleanup run marked it and then died before confirmPurge -- a crash this method's own
      // idempotency contract promises to recover from. Selecting only completed/failed, as this
      // did originally, stranded both cases in purge_pending forever with their B2 objects
      // orphaned, because nothing else ever queries that state.
      const carriedOver = [...sql.exec(
        `SELECT id, payload_key, result_key FROM jobs
         WHERE state = 'purge_pending'
         ORDER BY updated_at ASC LIMIT ?`,
        limit
      )];

      const remaining = limit - carriedOver.length;
      let newlyEligible = [];
      if (remaining > 0) {
        // Query each terminal state independently so idx_jobs_state_updated_id can satisfy both
        // the age range and the per-state ordering. The old combined IN + ORDER BY query used a
        // temp B-tree and read the entire terminal history before returning this small batch.
        const completed = [...sql.exec(
          `SELECT id, payload_key, result_key, updated_at FROM jobs
           WHERE state = 'completed' AND updated_at < ?
           ORDER BY updated_at ASC, id ASC LIMIT ?`,
          cutoff,
          remaining
        )];
        const failed = [...sql.exec(
          `SELECT id, payload_key, result_key, updated_at FROM jobs
           WHERE state = 'failed' AND updated_at < ?
           ORDER BY updated_at ASC, id ASC LIMIT ?`,
          cutoff,
          remaining
        )];
        newlyEligible = mergeSortedRows(
          completed,
          failed,
          remaining,
          (left, right) => left.updated_at - right.updated_at || left.id.localeCompare(right.id)
        );
      }

      if (newlyEligible.length > 0) {
        const now = Date.now();
        for (const chunk of this._chunks(newlyEligible.map((r) => r.id))) {
          const placeholders = chunk.map(() => "?").join(",");
          sql.exec(
            `UPDATE jobs SET state='purge_pending', updated_at=? WHERE id IN (${placeholders})`,
            now,
            ...chunk
          );
        }
      }

      const rows = [...carriedOver, ...newlyEligible];
      if (rows.length === 0) return { jobs: [] };
      return {
        jobs: rows.map((r) => ({ id: r.id, payload_key: r.payload_key, result_key: r.result_key })),
      };
    });
  }

  /**
   * Consumption ack: the client has fetched these jobs' results, validated them, and durably
   * written them to the deferred registry (see write_deferred in citypods/compute/llm_deferred.py),
   * so neither the SQLite row nor the B2 payload/result objects are needed any more.
   *
   * Moves them straight to 'purge_pending', short-circuiting the COMPLETED_RETENTION_DAYS timer
   * that would otherwise hold a consumed job for 38 days; the executor's existing cleanup pass
   * then deletes the B2 keys and calls confirmPurge exactly as it does for aged-out jobs. This is
   * the trigger review/44's "Consumption ack" section describes -- retention by age remains only
   * as the backstop for a job that is never acked (a client that died between fetch and ack).
   *
   * Only a job in 'completed' is eligible. A 'failed' job is deliberately NOT ackable: the sweep's
   * schema-correction path still reads it, and a client must never be able to retire a job whose
   * result it could not validate. Unknown/ineligible ids are reported back rather than silently
   * ignored, so a caller can tell an accepted ack from a no-op.
   */
  async ackResults(jobIds) {
    if (!jobIds || jobIds.length === 0) return { acked: [], ignored: [] };
    const sql = this._getSql();
    // Optional: the client already persisted these results; refusing only defers the row's
    // release to retention cleanup.
    if (this._readRowsWrittenToday() >= this._optionalRowStop()) {
      return { acked: [], ignored: [...jobIds] };
    }
    return this.ctx.storage.transactionSync(() => {
      const acked = [];
      for (const chunk of this._chunks(jobIds)) {
        const placeholders = chunk.map(() => "?").join(",");
        const eligible = [...sql.exec(
          `SELECT id FROM jobs WHERE id IN (${placeholders}) AND +state = 'completed'`,
          ...chunk
        )].map((row) => row.id);
        if (eligible.length === 0) continue;
        for (const eligibleChunk of this._chunks(eligible)) {
          const marks = eligibleChunk.map(() => "?").join(",");
          sql.exec(
            `UPDATE jobs SET state='purge_pending', updated_at=? WHERE id IN (${marks})`,
            Date.now(),
            ...eligibleChunk
          );
          acked.push(...eligibleChunk);
        }
      }
      const ackedSet = new Set(acked);
      return { acked, ignored: jobIds.filter((id) => !ackedSet.has(id)) };
    });
  }

  /**
   * Consumption-based retirement (tier 3): delete the rows of COMPLETED jobs whose result the
   * caller has durably persisted AND whose B2 payload/result objects it has already deleted.
   * One billed row per job, versus ack (state change, 2) + cleanup's confirmPurge (1) plus two
   * Worker-side B2 delete subrequests on the plain-ack path.
   *
   * Safety is by consumption, never by age: a row is removed only when it is still `completed`
   * AND its result_key equals the one the caller consumed. A job superseded by an idempotent
   * replay after the caller's poll (queued again, or re-completed with a new result_key) is left
   * untouched, so a late retire can never delete work nobody has read. Jobs that are queued,
   * leased, failed, or unknown are ignored.
   */
  async retireConsumed(items) {
    if (!Array.isArray(items) || items.length === 0) return { retired: [], ignored: [] };
    const sql = this._getSql();
    if (this._readRowsWrittenToday() >= this._optionalRowStop()) {
      return { retired: [], ignored: items.map((item) => item.id) };
    }
    return this.ctx.storage.transactionSync(() => {
      const retired = [];
      const ignored = [];
      const wanted = new Map(items.map((item) => [item.id, item.result_key]));
      for (const chunk of this._chunks([...wanted.keys()])) {
        const placeholders = chunk.map(() => "?").join(",");
        const rows = [...sql.exec(
          `SELECT id, result_key FROM jobs WHERE id IN (${placeholders}) AND +state = 'completed'`,
          ...chunk
        )];
        const matched = rows
          .filter((row) => row.result_key && row.result_key === wanted.get(row.id))
          .map((row) => row.id);
        for (const deleteChunk of this._chunks(matched)) {
          const marks = deleteChunk.map(() => "?").join(",");
          sql.exec(
            `DELETE FROM jobs WHERE id IN (${marks}) AND +state = 'completed'`,
            ...deleteChunk
          );
          retired.push(...deleteChunk);
        }
      }
      const retiredSet = new Set(retired);
      for (const id of wanted.keys()) if (!retiredSet.has(id)) ignored.push(id);
      return { retired, ignored };
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
          `DELETE FROM jobs WHERE id IN (${placeholders}) AND +state = 'purge_pending'`,
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

// Every public async RPC tallies the rows its statements wrote once it finishes (see _getSql and
// _drainRowCount). Wrapping on the prototype keeps these real class methods -- Workers RPC only
// exposes prototype methods -- and covers any RPC added later without a per-method call.
for (const name of Object.getOwnPropertyNames(LLMSchedulerDO.prototype)) {
  if (name === "constructor" || name.startsWith("_")) continue;
  const descriptor = Object.getOwnPropertyDescriptor(LLMSchedulerDO.prototype, name);
  const original = descriptor?.value;
  if (typeof original !== "function" || original.constructor?.name !== "AsyncFunction") continue;
  Object.defineProperty(LLMSchedulerDO.prototype, name, {
    ...descriptor,
    value: {
      async [name](...args) {
        try {
          return await original.apply(this, args);
        } finally {
          this._drainRowCount();
        }
      },
    }[name],
  });
}
