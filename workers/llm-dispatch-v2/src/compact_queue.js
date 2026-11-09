/** Compact model-set queue used when the coordinator's durable authority selector is enabled. */

// SQLite BINARY compares UTF-8 bytes; JavaScript's string ordering compares UTF-16 code units.
const encoder = new TextEncoder();

/** Preflight a one-shot compact queue build without changing either representation.
 * The caller must keep the preflight and build in one synchronous transaction. Limits protect
 * against an unexpectedly large source; malformed or divergent legacy state requires repair.
 * maxWrites covers only the two compact tables; the caller must separately reserve and meter
 * all other migration, accounting and concurrent work. */
export function planCompactMigration(sql, { maxJobs, maxModelRows, maxGroups, maxWrites }) {
  for (const n of [maxJobs, maxModelRows, maxGroups, maxWrites]) {
    if (!Number.isSafeInteger(n) || n < 1 || n === Number.MAX_SAFE_INTEGER) {
      throw new Error("invalid compact migration limit");
    }
  }
  const jobs = [...sql.exec(
    "SELECT id, priority, created_at, queue_models FROM jobs WHERE state = 'queued' LIMIT ?",
    maxJobs + 1
  )];
  if (jobs.length > maxJobs) throw new Error("compact migration job limit exceeded");
  const legacy = [...sql.exec(
    "SELECT job_id, model, priority, created_at FROM job_models LIMIT ?", maxModelRows + 1
  )];
  if (legacy.length > maxModelRows) throw new Error("compact migration model row limit exceeded");
  const expected = new Set(), groups = new Map();
  for (const job of jobs) {
    let models;
    try { models = JSON.parse(job.queue_models); } catch { /* rejected below */ }
    if (!Array.isArray(models) || models.length === 0 ||
        models.some(model => typeof model !== "string" || !model) ||
        new Set(models).size !== models.length || JSON.stringify(models) !== job.queue_models) {
      throw new Error("compact migration requires a valid recorded model set");
    }
    for (const model of models) {
      expected.add(JSON.stringify([job.id, model, job.priority, job.created_at]));
    }
    groups.set(job.queue_models, models);
  }
  for (const row of legacy) {
    if (!expected.delete(JSON.stringify([row.job_id, row.model, row.priority, row.created_at]))) {
      throw new Error("compact migration legacy index mismatch");
    }
  }
  if (expected.size) throw new Error("compact migration legacy index mismatch");
  const groupRows = [...groups.values()].reduce((sum, models) => sum + models.length, 0);
  if (groupRows > maxGroups) throw new Error("compact migration group limit exceeded");
  const expectedWrites = jobs.length + groupRows;
  if (expectedWrites > maxWrites) throw new Error("compact migration write reserve exceeded");
  return { jobs, groups, expectedWrites };
}

/** Build only inside the same transaction as preflight, authority switch and accounting. */
export function buildCompactMigration(sql, plan) {
  sql.exec(`CREATE TABLE compact_queue (
    model_set TEXT NOT NULL, priority INTEGER NOT NULL, created_at INTEGER NOT NULL,
    job_id TEXT NOT NULL, PRIMARY KEY (model_set, priority, created_at, job_id)
  ) WITHOUT ROWID`);
  sql.exec(`CREATE TABLE compact_groups (
    model TEXT NOT NULL, model_set TEXT NOT NULL, PRIMARY KEY (model, model_set)
  ) WITHOUT ROWID`);
  for (const job of plan.jobs) {
    sql.exec("INSERT INTO compact_queue VALUES (?, ?, ?, ?)",
      job.queue_models, job.priority, job.created_at, job.id);
  }
  for (const [set, models] of plan.groups) {
    for (const model of models) sql.exec("INSERT INTO compact_groups VALUES (?, ?)", model, set);
  }
  sql.exec(COMPACT_PRIORITY_SYNC_TRIGGER);
}

/** Validate compact authority and size a legacy-index rebuild before changing either table. */
export function planCompactRollback(sql, { maxJobs, maxModelRows, maxGroups, maxWrites }) {
  for (const n of [maxJobs, maxModelRows, maxGroups, maxWrites]) {
    if (!Number.isSafeInteger(n) || n < 1 || n === Number.MAX_SAFE_INTEGER) {
      throw new Error("invalid compact rollback limit");
    }
  }
  const jobs = [...sql.exec(
    "SELECT id, priority, created_at, queue_models FROM jobs WHERE state = 'queued' LIMIT ?",
    maxJobs + 1
  )];
  const queue = [...sql.exec("SELECT model_set, priority, created_at, job_id FROM compact_queue LIMIT ?", maxJobs + 1)];
  const groups = [...sql.exec("SELECT model, model_set FROM compact_groups LIMIT ?", maxGroups + 1)];
  const legacy = [...sql.exec("SELECT job_id FROM job_models LIMIT ?", maxModelRows + 1)];
  if (jobs.length > maxJobs || queue.length > maxJobs || groups.length > maxGroups ||
      legacy.length > maxModelRows) throw new Error("compact rollback source limit exceeded");
  const remaining = new Set(queue.map(row => JSON.stringify(
    [row.model_set, row.priority, row.created_at, row.job_id]
  )));
  const groupKeys = new Set(groups.map(row => JSON.stringify([row.model, row.model_set])));
  const rows = [];
  for (const job of jobs) {
    let models;
    try { models = JSON.parse(job.queue_models); } catch { /* rejected below */ }
    if (!Array.isArray(models) || models.length === 0 ||
        models.some(model => typeof model !== "string" || !model) ||
        new Set(models).size !== models.length || JSON.stringify(models) !== job.queue_models ||
        !remaining.delete(JSON.stringify([job.queue_models, job.priority, job.created_at, job.id]))) {
      throw new Error("compact rollback queue mismatch");
    }
    for (const model of models) {
      if (!groupKeys.has(JSON.stringify([model, job.queue_models]))) {
        throw new Error("compact rollback group mismatch");
      }
      rows.push({ job_id: job.id, model, priority: job.priority, created_at: job.created_at });
      if (rows.length > maxModelRows) throw new Error("compact rollback model limit exceeded");
    }
  }
  if (remaining.size) throw new Error("compact rollback queue mismatch");
  const expectedWrites = legacy.length + rows.length;
  if (expectedWrites > maxWrites) throw new Error("compact rollback write reserve exceeded");
  return { jobs: jobs.length, rows, oldRows: legacy.length, expectedWrites };
}

/** Rebuild the legacy index inside the caller's transaction after successful preflight. */
export function buildCompactRollback(sql, plan) {
  sql.exec("DELETE FROM job_models");
  for (const row of plan.rows) {
    sql.exec("INSERT INTO job_models (job_id, model, priority, created_at) VALUES (?, ?, ?, ?)",
      row.job_id, row.model, row.priority, row.created_at);
  }
}

function sqliteBinaryCompare(left, right) {
  const a = encoder.encode(left), b = encoder.encode(right);
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}

/** Return the same bounded ordered prefix as the per-model index for the provided frontier.
 * Group enumeration is not bounded and is included in the returned cursor observations. */
export function readCompactCandidates(sql, model, limit, after = null, observe = () => {}) {
  const collect = (query, ...args) => {
    const cursor = sql.exec(query, ...args);
    const rows = [...cursor];
    observe(cursor, query);
    return rows;
  };
  const groupQuery = "SELECT model_set FROM compact_groups WHERE model = ?";
  const groupCursor = sql.exec(groupQuery, model);
  const compare = (a, b) => a.jm_priority - b.jm_priority || a.jm_created_at - b.jm_created_at ||
    sqliteBinaryCompare(a.id, b.id);
  let candidates = [];
  for (const group of groupCursor) {
    const next = collect(
      `SELECT jobs.*, compact_queue.priority AS jm_priority, compact_queue.created_at AS jm_created_at
       FROM compact_queue JOIN jobs ON jobs.id = compact_queue.job_id
       WHERE compact_queue.model_set = ? AND jobs.state = 'queued'
       ${after ? "AND (compact_queue.priority, compact_queue.created_at, compact_queue.job_id) > (?, ?, ?)" : ""}
       ORDER BY compact_queue.priority, compact_queue.created_at, compact_queue.job_id LIMIT ?`,
      group.model_set, ...(after || []), limit
    );
    const merged = [];
    let left = 0, right = 0;
    while (merged.length < limit && (left < candidates.length || right < next.length)) {
      if (right >= next.length || (left < candidates.length && compare(candidates[left], next[right]) <= 0)) {
        merged.push(candidates[left++]);
      } else {
        merged.push(next[right++]);
      }
    }
    candidates = merged;
  }
  observe(groupCursor, groupQuery);
  return candidates;
}

/** Record one queued job and its model-set memberships using the caller's transaction. */
export function indexCompactJob(sql, job, models) {
  const modelSet = JSON.stringify(models);
  sql.exec("INSERT OR IGNORE INTO compact_queue VALUES (?, ?, ?, ?)",
    modelSet, job.priority, job.created_at, job.id);
  for (const model of models) {
    sql.exec("INSERT OR IGNORE INTO compact_groups VALUES (?, ?)", model, modelSet);
  }
}

/** Remove one queue entry using the key recorded on the job. */
export function unindexCompactJob(sql, job) {
  if (typeof job.queue_models !== "string") throw new Error("compact queue key is missing");
  sql.exec("DELETE FROM compact_queue WHERE model_set = ? AND priority = ? AND created_at = ? AND job_id = ?",
    job.queue_models, job.priority, job.created_at, job.id);
}

/** Scan one bounded keyset page and remove only groups with no queued compact entries.
 * The caller must run the scan, deletes and cursor persistence in the same transaction. */
export function pruneEmptyCompactGroups(sql, limit, cursor = null) {
  const validKey = key => Array.isArray(key) && key.length === 2 &&
    key.every(value => typeof value === "string");
  if (!Number.isInteger(limit) || limit < 1 || limit > 64 ||
      (cursor !== null && (!cursor || typeof cursor !== "object" ||
        !validKey(cursor.after) || !validKey(cursor.through)))) {
    throw new Error("invalid compact group maintenance cursor or limit");
  }
  let through = cursor?.through;
  if (!through) {
    const [last] = [...sql.exec("SELECT model, model_set FROM compact_groups ORDER BY model DESC, model_set DESC LIMIT 1")];
    if (!last) return { scanned: 0, removed: 0, next: null };
    through = [last.model, last.model_set];
  }
  const rows = [...sql.exec(
    `SELECT model, model_set FROM compact_groups
     WHERE (model, model_set) <= (?, ?) ${cursor ? "AND (model, model_set) > (?, ?)" : ""}
     ORDER BY model, model_set LIMIT ?`, ...through, ...(cursor?.after || []), limit
  )];
  let removed = 0;
  for (const row of rows) {
    const present = [...sql.exec("SELECT 1 FROM compact_queue WHERE model_set = ? LIMIT 1", row.model_set)];
    if (present.length) continue;
    sql.exec("DELETE FROM compact_groups WHERE model = ? AND model_set = ?", row.model, row.model_set);
    removed += 1;
  }
  const last = rows[rows.length - 1];
  const atEnd = !last || (last.model === through[0] && last.model_set === through[1]);
  return { scanned: rows.length, removed,
    next: rows.length === limit && !atEnd ? { after: [last.model, last.model_set], through } : null };
}

/** Priority edits must move the existing compact key in the same SQLite transaction. */
export const COMPACT_PRIORITY_SYNC_TRIGGER = `
  CREATE TRIGGER IF NOT EXISTS trg_compact_priority_sync
  AFTER UPDATE OF priority ON jobs
  WHEN NEW.priority IS NOT OLD.priority
  BEGIN
    UPDATE compact_queue SET priority = NEW.priority
     WHERE model_set = OLD.queue_models AND priority = OLD.priority
       AND created_at = OLD.created_at AND job_id = OLD.id;
  END`;
