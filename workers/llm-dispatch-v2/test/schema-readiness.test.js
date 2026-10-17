import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import { createRecordingSqlStorage, withTestReservations } from "./helpers.js";

const quotaColumns = ["tpd_updated_at", "tpd_used", "prompt_cap_estimate", "input_window_json"];
const model = "nvidia/nemotron-3-ultra-550b-a55b:free";
const job = (id) => ({
  id, idempotency_key: id, request_digest: id,
  policy_json: JSON.stringify({ allowed_models: [model], purpose: "chapter-agenda" }),
  prompt_family: "agenda-item-extract", input_token_estimate: 100,
  max_output_token_estimate: 50, payload_key: `payloads/${id}`, priority: 1,
});

function fixture() {
  const f = createRecordingSqlStorage();
  f.env = withTestReservations({});
  f.make = () => new LLMSchedulerDO({ storage: f.storage }, f.env);
  f.coordinator = f.make();
  return f;
}

function mutations(recorder) {
  return recorder.statements.filter(({ query }) => !/^(SELECT|PRAGMA|EXPLAIN)\b/i.test(query));
}

test("readiness covers every column the initializer can add", () => {
  const f = createRecordingSqlStorage();
  class CaptureColumns extends LLMSchedulerDO {
    _ensureColumn(table, column, definition) {
      (this.addedColumns ||= []).push({ table, column, definition });
      return super._ensureColumn(table, column, definition);
    }
  }
  const coordinator = new CaptureColumns({ storage: f.storage }, withTestReservations({}));
  for (const { table, column, definition } of coordinator.addedColumns) {
    // SQLite refuses to drop a column a trigger body reads; set the trigger aside meanwhile.
    const triggers = f.sql.exec(
      "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ? AND sql LIKE ?",
      table,
      `%${column}%`
    ).map((row) => ({ ...row }));
    for (const { name } of triggers) f.sql.exec(`DROP TRIGGER ${name}`);
    f.sql.exec(`ALTER TABLE ${table} DROP COLUMN ${column}`);
    assert.ok(
      coordinator._inspectCurrentSchema().missing.includes(`column:${table}.${column}`),
      `startup must check ${table}.${column}`
    );
    f.sql.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
    for (const trigger of triggers) f.sql.exec(trigger.sql);
    if (column === "mistral_latest_migrated") {
      f.sql.exec("UPDATE scheduler SET mistral_latest_migrated = 1 WHERE id = 1");
    }
  }
});

test("quota-only upgrade changes only missing columns; later startups issue no writes", () => {
  const f = fixture();
  f.sql.exec("INSERT INTO routes (route_id, rpd_count) VALUES (?, ?)", "preserved", 123);
  for (const column of quotaColumns) f.sql.exec(`ALTER TABLE routes DROP COLUMN ${column}`);
  f.recorder.start();
  const upgraded = f.make();
  assert.equal(upgraded._inspectCurrentSchema().current, true);
  assert.deepEqual(mutations(f.recorder).map(({ query }) => query), [
    "ALTER TABLE routes ADD COLUMN tpd_updated_at INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE routes ADD COLUMN tpd_used INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE routes ADD COLUMN prompt_cap_estimate INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE routes ADD COLUMN input_window_json TEXT NOT NULL DEFAULT '[]'",
  ]);
  assert.equal(
    f.sql.exec("SELECT rpd_count FROM routes WHERE route_id = 'preserved'")[0].rpd_count, 123
  );
  f.recorder.reset();
  for (let i = 0; i < 5; i++) f.make();
  assert.deepEqual(mutations(f.recorder), []);
});

test("partial quota upgrade resumes without replaying completed columns or job writes", () => {
  const f = fixture();
  for (const column of quotaColumns) f.sql.exec(`ALTER TABLE routes DROP COLUMN ${column}`);
  const raw = f.sql.exec.bind(f.sql);
  let unavailable = true;
  f.sql.exec = (query, ...args) => {
    if (unavailable && query.startsWith("ALTER TABLE routes ADD COLUMN tpd_used ")) {
      throw new Error("migration unavailable");
    }
    return raw(query, ...args);
  };
  f.recorder.start();
  assert.throws(f.make, /migration unavailable/);
  f.recorder.reset();
  for (let i = 0; i < 3; i++) assert.throws(f.make, /migration unavailable/);
  assert.deepEqual(mutations(f.recorder), []);
  unavailable = false;
  const repaired = f.make();
  assert.equal(repaired._inspectCurrentSchema().current, true);
  assert.equal(mutations(f.recorder).length, 3);
  assert.ok(mutations(f.recorder).every(({ query }) => query.startsWith("ALTER TABLE routes ADD")));
});

test("an initializer that leaves columns absent cannot activate the coordinator", () => {
  const f = fixture();
  f.sql.exec("ALTER TABLE routes DROP COLUMN tpd_used");
  class SkippedMigration extends LLMSchedulerDO {
    _ensureColumn() { return false; }
  }
  f.recorder.start();
  assert.throws(
    () => new SkippedMigration({ storage: f.storage }, f.env),
    /schema not ready: column:routes.tpd_used/
  );
  assert.deepEqual(mutations(f.recorder), []);
});

test("missing quota columns stop expired-lease claims and repeated RPCs before writes", async () => {
  const f = fixture();
  const now = Date.now();
  await f.coordinator.enqueueBatch(Array.from({ length: 12 }, (_, i) => job(`j${i}`)));
  const plan = await f.coordinator.claimDispatchWindow(now, 30);
  assert.ok(plan.jobs.length > 0);
  const before = f.sql.exec("SELECT * FROM jobs ORDER BY id");
  f.sql.exec("ALTER TABLE routes DROP COLUMN tpd_used");
  f.recorder.start();
  for (let i = 0; i < 4; i++) {
    await assert.rejects(
      f.coordinator.claimDispatchWindow(now + 3_600_000 + i * 60_000, 30), /schema not ready/
    );
    await assert.rejects(f.coordinator.enqueueBatch([job(`later-${i}`)]), /schema not ready/);
  }
  assert.deepEqual(mutations(f.recorder), []);
  assert.deepEqual(f.sql.exec("SELECT * FROM jobs ORDER BY id"), before);
  const repaired = f.make();
  const next = await repaired.claimDispatchWindow(now + 3_600_000, 30);
  assert.ok(next.jobs.length > 0, "recreation migrates and resumes the expired queue");
});

test("a transient preflight read failure does not latch the schema breaker", async () => {
  const f = fixture();
  const raw = f.sql.exec.bind(f.sql);
  let fail = true;
  f.sql.exec = (query, ...args) => {
    if (fail && query.startsWith("SELECT tpd_updated_at, tpd_used,")) {
      fail = false;
      throw new Error("temporary read failure");
    }
    return raw(query, ...args);
  };
  f.recorder.start();
  await assert.rejects(f.coordinator.enqueueBatch([job("retry")]), /temporary read failure/);
  assert.deepEqual(mutations(f.recorder), []);
  const result = await f.coordinator.enqueueBatch([job("retry")]);
  assert.equal(result.accepted.length, 1);
});
