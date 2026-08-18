/**
 * Durable Object Coordinator for LLM Dispatch v2.
 * Backed by SQLite inside Cloudflare Workers.
 */

export class LLMSchedulerDO {
  constructor(ctx, env) {
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

  _currentUtcDay() {
    return new Date().toISOString().slice(0, 10);
  }

  _maxJobsPerUtcDay() {
    const configured = Number(this.env.MAX_JOBS_PER_UTC_DAY);
    return Number.isFinite(configured) && configured > 0 ? configured : 20000;
  }

  async enqueueBatch(jobs) {
    const sql = this._getSql();
    const now = Date.now();
    const today = this._currentUtcDay();
    const maxJobsToday = this._maxJobsPerUtcDay();

    const accepted = [];
    const rejected = [];

    // Check & roll UTC day if changed
    const schedRows = [...sql.exec("SELECT utc_day, jobs_ingested_today, bundle_count_today FROM scheduler WHERE id = 1")];
    let jobsIngestedToday = 0;
    if (schedRows.length > 0) {
      const sched = schedRows[0];
      if (sched.utc_day !== today) {
        sql.exec(
          "UPDATE scheduler SET utc_day = ?, bundle_count_today = 0, jobs_ingested_today = 0 WHERE id = 1",
          today
        );
        jobsIngestedToday = 0;
      } else {
        jobsIngestedToday = sched.jobs_ingested_today;
      }
    } else {
      sql.exec(
        "INSERT INTO scheduler (id, utc_day, bundle_count_today, jobs_ingested_today) VALUES (1, ?, 0, 0)",
        today
      );
      jobsIngestedToday = 0;
    }

    let newlyInsertedCount = 0;

    for (const job of jobs) {
      // Check daily cap for newly ingested jobs
      if (jobsIngestedToday + newlyInsertedCount >= maxJobsToday) {
        rejected.push({ id: job.id, reason: "daily_cap_exceeded" });
        continue;
      }

      // Check idempotency_key
      const existing = [...sql.exec(
        "SELECT id, request_digest, state FROM jobs WHERE idempotency_key = ?",
        job.idempotency_key
      )];

      if (existing.length > 0) {
        const row = existing[0];
        if (row.request_digest === job.request_digest) {
          // Idempotent replay: return existing ID
          accepted.push({ id: row.id });
        } else {
          // Reused key with different payload: fail loudly
          rejected.push({ id: job.id, reason: "idempotency_conflict" });
        }
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
      accepted.push({ id: job.id });
    }

    if (newlyInsertedCount > 0) {
      sql.exec(
        "UPDATE scheduler SET jobs_ingested_today = jobs_ingested_today + ? WHERE id = 1",
        newlyInsertedCount
      );
    }

    return { accepted, rejected };
  }

  async pollBatch(ids) {
    if (!ids || ids.length === 0) {
      return { statuses: [] };
    }

    const sql = this._getSql();
    const placeholders = ids.map(() => "?").join(",");
    const rows = [...sql.exec(
      `SELECT id, state, result_key, attempts FROM jobs WHERE id IN (${placeholders})`,
      ...ids
    )];

    const statuses = rows.map((row) => ({
      id: row.id,
      state: row.state,
      result_key: row.state === "completed" ? row.result_key : null,
      error: row.state === "failed" ? "job_failed" : null,
      attempts: row.attempts,
    }));

    return { statuses };
  }

  async resolveUnknownBatch(attemptIds) {
    if (!attemptIds || attemptIds.length === 0) {
      return { resolved: [], not_found: [] };
    }
    const sql = this._getSql();
    const resolved = [];
    const not_found = [];
    for (const attemptId of attemptIds) {
      const rows = [...sql.exec("SELECT attempt_id FROM attempts WHERE attempt_id = ?", attemptId)];
      if (rows.length > 0) {
        resolved.push(attemptId);
      } else {
        not_found.push(attemptId);
      }
    }
    return { resolved, not_found };
  }
}
