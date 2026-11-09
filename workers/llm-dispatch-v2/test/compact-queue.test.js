import test from "node:test";
import assert from "node:assert/strict";
import { COMPACT_PRIORITY_SYNC_TRIGGER, buildCompactMigration, buildCompactRollback, indexCompactJob,
  planCompactMigration, planCompactRollback, pruneEmptyCompactGroups, readCompactCandidates,
  unindexCompactJob } from "../src/compact_queue.js";
import { createMockSqlStorage } from "./helpers.js";

test("compact model-set lookup preserves SQLite order, limits and frontier", () => {
  const { sql } = createMockSqlStorage();
  sql.exec("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT, priority INTEGER, created_at INTEGER)");
  sql.exec("CREATE TABLE job_models (model TEXT, priority INTEGER, created_at INTEGER, job_id TEXT, " +
    "PRIMARY KEY(model, priority, created_at, job_id)) WITHOUT ROWID");
  sql.exec("CREATE TABLE compact_groups (model TEXT, model_set TEXT, PRIMARY KEY(model, model_set)) WITHOUT ROWID");
  sql.exec("CREATE TABLE compact_queue (model_set TEXT, priority INTEGER, created_at INTEGER, job_id TEXT, " +
    "PRIMARY KEY(model_set, priority, created_at, job_id)) WITHOUT ROWID");
  const fixtures = [
    { id: "a", priority: 0, time: 10, models: ["m", "x"] },
    { id: "\u{10000}", priority: 0, time: 11, models: ["m"] },
    { id: "\uE000", priority: 0, time: 11, models: ["m", "z"] },
    { id: "d", priority: 1, time: 1, models: ["m", "x"] },
    { id: "e", priority: 1, time: 2, models: ["x"] },
  ];
  for (const job of fixtures) {
    const set = JSON.stringify(job.models);
    sql.exec("INSERT INTO jobs VALUES (?, 'queued', ?, ?)", job.id, job.priority, job.time);
    sql.exec("INSERT INTO compact_queue VALUES (?, ?, ?, ?)", set, job.priority, job.time, job.id);
    for (const model of job.models) {
      sql.exec("INSERT OR IGNORE INTO compact_groups VALUES (?, ?)", model, set);
      sql.exec("INSERT INTO job_models VALUES (?, ?, ?, ?)", model, job.priority, job.time, job.id);
    }
  }
  // A stale queue row must not cause a leased job to be selected.
  sql.exec("UPDATE jobs SET state = 'leased' WHERE id = ?", "d");
  for (const [model, limit, after] of [["m", 2, null], ["m", 4, null], ["m", 4, [0, 10, "a"]],
    ["x", 3, null], ["absent", 3, null]]) {
    const expected = [...sql.exec(
      `SELECT jobs.id FROM job_models JOIN jobs ON jobs.id=job_models.job_id
       WHERE job_models.model=? AND jobs.state='queued'
       ${after ? "AND (job_models.priority, job_models.created_at, job_models.job_id) > (?, ?, ?)" : ""}
       ORDER BY job_models.priority, job_models.created_at, job_models.job_id LIMIT ?`,
      model, ...(after || []), limit
    )].map(row => row.id);
    const actual = readCompactCandidates(sql, model, limit, after).map(row => row.id);
    assert.deepEqual(actual, expected);
  }
});


test("compact queue indexing is idempotent and removes only the recorded job", () => {
  const { sql } = createMockSqlStorage();
  sql.exec("CREATE TABLE compact_groups (model TEXT, model_set TEXT, PRIMARY KEY(model, model_set)) WITHOUT ROWID");
  sql.exec("CREATE TABLE compact_queue (model_set TEXT, priority INTEGER, created_at INTEGER, job_id TEXT, " +
    "PRIMARY KEY(model_set, priority, created_at, job_id)) WITHOUT ROWID");
  const models = ["m", "x"], key = JSON.stringify(models);
  const first = { id: "a", priority: 1, created_at: 10, queue_models: key };
  const second = { id: "b", priority: 1, created_at: 10, queue_models: key };
  indexCompactJob(sql, first, models);
  indexCompactJob(sql, first, models);
  indexCompactJob(sql, second, models);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM compact_queue")[0].n, 2);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM compact_groups")[0].n, 2);
  unindexCompactJob(sql, first);
  assert.deepEqual(sql.exec("SELECT job_id FROM compact_queue").map(row => row.job_id), ["b"]);
  assert.throws(() => unindexCompactJob(sql, { ...second, queue_models: null }), /missing/);
});

test("compact lookup merges many sets without changing the global first page", () => {
  const { sql } = createMockSqlStorage();
  sql.exec("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT, priority INTEGER, created_at INTEGER)");
  sql.exec("CREATE TABLE compact_groups (model TEXT, model_set TEXT, PRIMARY KEY(model, model_set)) WITHOUT ROWID");
  sql.exec("CREATE TABLE compact_queue (model_set TEXT, priority INTEGER, created_at INTEGER, job_id TEXT, " +
    "PRIMARY KEY(model_set, priority, created_at, job_id)) WITHOUT ROWID");
  for (let i = 0; i < 180; i++) {
    const id = `job-${String(i).padStart(3, "0")}`;
    const models = ["m", `secondary-${i}`];
    const job = { id, priority: i % 2, created_at: 180 - i };
    sql.exec("INSERT INTO jobs VALUES (?, 'queued', ?, ?)", job.id, job.priority, job.created_at);
    indexCompactJob(sql, job, models);
  }
  const expected = [...sql.exec("SELECT id FROM jobs WHERE state='queued' ORDER BY priority, created_at, id LIMIT 6")]
    .map(row => row.id);
  const actual = readCompactCandidates(sql, "m", 6).map(row => row.id);
  assert.deepEqual(actual, expected);
});


test("priority trigger moves the compact key and rolls back with the job", () => {
  const { sql, storage } = createMockSqlStorage();
  sql.exec("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT, priority INTEGER, created_at INTEGER, queue_models TEXT)");
  sql.exec("CREATE TABLE compact_groups (model TEXT, model_set TEXT, PRIMARY KEY(model, model_set)) WITHOUT ROWID");
  sql.exec("CREATE TABLE compact_queue (model_set TEXT, priority INTEGER, created_at INTEGER, job_id TEXT, " +
    "PRIMARY KEY(model_set, priority, created_at, job_id)) WITHOUT ROWID");
  sql.exec(COMPACT_PRIORITY_SYNC_TRIGGER);
  const models = ["m"], key = JSON.stringify(models);
  const job = { id: "a", priority: 1, created_at: 10, queue_models: key };
  sql.exec("INSERT INTO jobs VALUES (?, 'queued', ?, ?, ?)", job.id, job.priority, job.created_at, key);
  indexCompactJob(sql, job, models);
  assert.throws(() => storage.transactionSync(() => {
    sql.exec("UPDATE jobs SET priority=0 WHERE id=?", job.id);
    throw new Error("rollback");
  }), /rollback/);
  assert.equal(sql.exec("SELECT priority FROM compact_queue")[0].priority, 1);
  sql.exec("UPDATE jobs SET priority=0 WHERE id=?", job.id);
  assert.equal(sql.exec("SELECT priority FROM compact_queue")[0].priority, 0);
  assert.deepEqual(readCompactCandidates(sql, "m", 1).map(row => row.id), ["a"]);
});

test("compact migration validates the legacy index and rolls back the build atomically", () => {
  const { sql, storage } = createMockSqlStorage();
  sql.exec("CREATE TABLE jobs (id TEXT PRIMARY KEY, state TEXT, priority INTEGER, created_at INTEGER, queue_models TEXT)");
  sql.exec("CREATE TABLE job_models (job_id TEXT, model TEXT, priority INTEGER, created_at INTEGER, " +
    "PRIMARY KEY(model, priority, created_at, job_id)) WITHOUT ROWID");
  const set = JSON.stringify(["m", "x"]);
  sql.exec("INSERT INTO jobs VALUES ('a', 'queued', 1, 10, ?)", set);
  sql.exec("INSERT INTO job_models VALUES ('a', 'm', 1, 10)");
  sql.exec("INSERT INTO job_models VALUES ('a', 'x', 1, 10)");
  const limits = { maxJobs: 2, maxModelRows: 2, maxGroups: 2, maxWrites: 3 };
  assert.throws(() => storage.transactionSync(() => {
    const plan = planCompactMigration(sql, limits);
    assert.equal(plan.expectedWrites, 3);
    buildCompactMigration(sql, plan);
    throw new Error("rollback migration");
  }), /rollback migration/);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM sqlite_schema WHERE name='compact_queue'")[0].n, 0);
  storage.transactionSync(() => buildCompactMigration(sql, planCompactMigration(sql, limits)));
  assert.deepEqual(readCompactCandidates(sql, "m", 2).map(row => row.id), ["a"]);
  const rollbackLimits = { ...limits, maxWrites: 4 };
  const rollback = planCompactRollback(sql, rollbackLimits);
  assert.equal(rollback.expectedWrites, 4);
  assert.throws(() => planCompactRollback(sql, { ...rollbackLimits, maxWrites: 3 }), /write reserve/);
  assert.throws(() => storage.transactionSync(() => {
    buildCompactRollback(sql, rollback);
    throw new Error("rollback rebuild");
  }), /rollback rebuild/);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM job_models")[0].n, 2);
  sql.exec("DELETE FROM compact_groups WHERE model='x'");
  assert.throws(() => planCompactRollback(sql, rollbackLimits), /group mismatch/);
  sql.exec("INSERT INTO compact_groups VALUES ('x', ?)", set);
  sql.exec("INSERT INTO compact_queue VALUES (?, 1, 10, 'stale')", set);
  assert.throws(() => planCompactRollback(sql, rollbackLimits), /queue mismatch/);
  sql.exec("DELETE FROM compact_queue WHERE job_id='stale'");
  sql.exec("DELETE FROM job_models WHERE model='x'");
  assert.throws(() => planCompactMigration(sql, limits), /index mismatch/);
  sql.exec("UPDATE jobs SET queue_models=NULL WHERE id='a'");
  assert.throws(() => planCompactMigration(sql, limits), /valid recorded model set/);
  sql.exec("UPDATE jobs SET queue_models=? WHERE id='a'", set);
  assert.throws(() => planCompactMigration(sql, { ...limits, maxModelRows: 0 }), /invalid/);
  sql.exec("INSERT INTO job_models VALUES ('a', 'x', 1, 10)");
  assert.throws(() => planCompactMigration(sql, { ...limits, maxGroups: 1 }), /group limit/);
  assert.throws(() => planCompactMigration(sql, { ...limits, maxWrites: 2 }), /write reserve/);
});

test("compact group maintenance advances in bounded pages and retains active groups", () => {
  const { sql, storage } = createMockSqlStorage();
  sql.exec("CREATE TABLE compact_groups (model TEXT, model_set TEXT, PRIMARY KEY(model, model_set)) WITHOUT ROWID");
  sql.exec("CREATE TABLE compact_queue (model_set TEXT, priority INTEGER, created_at INTEGER, job_id TEXT, " +
    "PRIMARY KEY(model_set, priority, created_at, job_id)) WITHOUT ROWID");
  for (let i = 0; i < 130; i++) {
    const model = `m-${String(i).padStart(3, "0")}`, set = JSON.stringify([model]);
    sql.exec("INSERT INTO compact_groups VALUES (?, ?)", model, set);
  }
  const active = { id: "active", priority: 1, created_at: 1 };
  indexCompactJob(sql, active, ["m-100"]);
  let cursor = null, scanned = 0, removed = 0;
  let firstPage = true;
  do {
    const result = storage.transactionSync(() => pruneEmptyCompactGroups(sql, 64, cursor));
    assert.ok(result.scanned <= 64);
    scanned += result.scanned;
    removed += result.removed;
    cursor = result.next;
    if (firstPage) {
      sql.exec("INSERT INTO compact_groups VALUES (?, ?)", "z-late", '["z-late"]');
      firstPage = false;
    }
  } while (cursor);
  assert.equal(scanned, 130);
  assert.equal(removed, 129);
  assert.deepEqual(sql.exec("SELECT model FROM compact_groups").map(row => row.model), ["m-100", "z-late"]);
  assert.equal(storage.transactionSync(() => pruneEmptyCompactGroups(sql, 64)).removed, 1);
  indexCompactJob(sql, { id: "new", priority: 1, created_at: 2 }, ["m-001"]);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM compact_groups WHERE model='m-001'")[0].n, 1);
  assert.throws(() => pruneEmptyCompactGroups(sql, 65), /invalid/);
  assert.throws(() => pruneEmptyCompactGroups(sql, 1, ["bad"]), /invalid/);
});
