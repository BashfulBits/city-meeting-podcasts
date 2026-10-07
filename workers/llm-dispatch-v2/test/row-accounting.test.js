import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import { createMockSqlStorage, withTestReservations } from "./helpers.js";

function fixture() {
  const { storage, sql } = createMockSqlStorage();
  const env = withTestReservations({ MAX_JOBS_PER_UTC_DAY: "100" });
  const make = () => new LLMSchedulerDO({ storage }, env);
  const coordinator = make();
  coordinator._transactionSync(() => coordinator._ensureQueuedJobCounter());
  const read = () => [...sql.exec("SELECT rows_written_today FROM scheduler WHERE id = 1")][0]
    .rows_written_today;
  return { storage, sql, coordinator, make, read };
}

const job = (id) => ({
  id, idempotency_key: id, request_digest: id, policy_json: "{}", prompt_family: "tags",
  input_token_estimate: 100, max_output_token_estimate: 50, payload_key: `payloads/${id}`, priority: 1,
});

test("each enqueue survives recreation and includes its own accounting write exactly once", async () => {
  const f = fixture();
  const baseline = f.read();
  let written = 0;
  const raw = f.sql.exec.bind(f.sql);
  f.sql.exec = (...args) => {
    const cursor = raw(...args);
    written += Number(cursor.rowsWritten) || 0;
    return cursor;
  };
  for (let i = 0; i < 10; i++) {
    await f.make().enqueueBatch([job(`j${i}`)]);
    assert.equal(f.read(), baseline + written);
    assert.equal((await f.make().stats(Date.now())).row_budget.rows_written_today, f.read());
  }
});

test("pause and resume accounting survives recreation without a later claim or enqueue", async () => {
  const f = fixture();
  const before = f.read();
  await f.make().pauseDispatch({ scope: "global", seconds: 60, reason: "test" });
  const paused = f.read();
  assert.ok(paused > before);
  await f.make().resumeDispatch({ scope: "global" });
  assert.ok(f.read() > paused);
  assert.equal((await f.make().stats(Date.now())).row_budget.rows_written_today, f.read());
});

test("accounting failure rolls back the business writes and restores the memory tally", async () => {
  const f = fixture();
  const before = f.read();
  const raw = f.sql.exec.bind(f.sql);
  let fail = true;
  f.sql.exec = (query, ...args) => {
    if (fail && query === "UPDATE scheduler SET rows_written_today = rows_written_today + ? WHERE id = 1") {
      throw new Error("accounting write failed");
    }
    return raw(query, ...args);
  };
  await assert.rejects(f.coordinator.enqueueBatch([job("j1")]), /accounting write failed/);
  assert.equal(f.read(), before);
  assert.equal([...raw("SELECT COUNT(*) AS n FROM jobs")][0].n, 0);
  assert.equal(f.coordinator._rowsUnflushed, 0);
  fail = false;
  assert.equal((await f.coordinator.enqueueBatch([job("j1")])).accepted.length, 1);
  assert.equal((await f.make().stats(Date.now())).row_budget.rows_written_today, f.read());
});

test("read-only snapshots and rejected replays do not add accounting writes", async () => {
  const f = fixture();
  await f.coordinator.enqueueBatch([job("j1")]);
  const before = f.read();
  await f.make().stats(Date.now());
  await f.make().ingressStatus("unspecified");
  await f.make().enqueueBatch([job("j1")]);
  assert.equal(f.read(), before);
});

test("a first writing RPC after midnight counts its writes and the day rollover", async () => {
  const f = fixture();
  f.sql.exec("UPDATE scheduler SET utc_day = ?, rows_written_today = 99999 WHERE id = 1", "2000-01-01");
  await f.make().pauseDispatch({ scope: "global", seconds: 60, reason: "new day" });
  assert.ok(f.read() > 0 && f.read() < 100);
  assert.equal((await f.make().stats(Date.now())).row_budget.rows_written_today, f.read());
});

test("operator observations only raise the current-day floor, survive recreation and are idempotent", async () => {
  const f = fixture();
  const now = Date.now();
  const observation = {
    utc_day: new Date(now).toISOString().slice(0, 10), minimum_rows_written: 65000,
    observed_through_at: now, reason: "Cloudflare UTC-day aggregate plus reporting-gap reserve",
  };
  const repaired = await f.coordinator.reconcileRowBudget(observation, now);
  assert.equal(repaired.ok, true);
  assert.equal(repaired.rows_written_today, 65002); // Floor write + accounting write.
  assert.equal((await f.make().stats(now)).row_budget.rows_written_today, 65002);
  await f.make().reconcileRowBudget(observation, now);
  await f.make().reconcileRowBudget({ ...observation, minimum_rows_written: 5 }, now);
  assert.equal(f.read(), 65002);
  const before = f.read();
  for (const changed of [
    { utc_day: "2000-01-01" }, { minimum_rows_written: -1 }, { minimum_rows_written: 100001 },
    { minimum_rows_written: 1.5 }, { observed_through_at: now + 1 }, { observed_through_at: 0 },
    { reason: "" }, { reason: "x".repeat(501) },
  ]) {
    const result = await f.coordinator.reconcileRowBudget({ ...observation, ...changed }, now);
    assert.equal(result.ok, false);
  }
  assert.equal(f.read(), before);
});

test("legacy counter seeding includes both idle-tick writes", (t) => {
  const now = Date.parse(`${new Date().toISOString().slice(0, 10)}T12:00:00Z`);
  t.mock.method(Date, "now", () => now);
  const { storage, sql } = createMockSqlStorage();
  const env = withTestReservations({ PURGE_BATCH_LIMIT: "0" });
  new LLMSchedulerDO({ storage }, env);
  sql.exec("ALTER TABLE scheduler DROP COLUMN rows_written_today");
  sql.exec(
    `UPDATE scheduler SET ingress_write_units_today = 0, lease_count_today = 0,
       bundle_count_today = 0 WHERE id = 1`
  );
  new LLMSchedulerDO({ storage }, env);
  const [row] = [...sql.exec("SELECT rows_written_today FROM scheduler")];
  assert.ok(row.rows_written_today >= 2 * 12 * 60, `seeded ${row.rows_written_today}`);
});

test("an unchanged idle claim writes nothing until the refresh interval, then folds its count", async () => {
  const f = fixture();
  const t0 = Date.UTC(2026, 9, 7, 12, 0, 0);
  const minute = 60_000;
  const scheduler = () => [...f.sql.exec(
    "SELECT last_claim_at, last_claim_reason, claim_empty_count_today, claim_reason_counts_json FROM scheduler WHERE id = 1"
  )][0];
  await f.coordinator.claimDispatchWindow(t0, 30);
  const first = f.read();
  assert.equal(scheduler().last_claim_reason, "no_queued_work");
  assert.equal(scheduler().claim_empty_count_today, 1);

  // Nine more idle ticks inside the interval: no billed row at all.
  for (let i = 1; i <= 9; i++) await f.coordinator.claimDispatchWindow(t0 + i * minute, 30);
  assert.equal(f.read(), first);
  assert.equal(scheduler().last_claim_at, t0);
  // Stats still reports every tick this instance saw.
  const live = (await f.coordinator.stats(t0 + 9 * minute)).claim;
  assert.equal(live.empty_count_today, 10);
  assert.equal(live.reason_counts_today.no_queued_work, 10);
  assert.equal(live.last_at, t0 + 9 * minute);

  // The tick at the interval persists, carrying the skipped ticks' counts.
  await f.coordinator.claimDispatchWindow(t0 + 10 * minute, 30);
  assert.equal(f.read(), first + 2);
  assert.equal(scheduler().last_claim_at, t0 + 10 * minute);
  assert.equal(scheduler().claim_empty_count_today, 11);
  assert.deepEqual(JSON.parse(scheduler().claim_reason_counts_json), { no_queued_work: 11 });
});

test("an idle claim whose reason changes is persisted at once", async () => {
  const f = fixture();
  const t0 = Date.UTC(2026, 9, 7, 12, 0, 0);
  await f.coordinator.claimDispatchWindow(t0, 30);
  // Exhaust today's lease cap directly: the next tick's empty reason becomes daily_lease_limit.
  f.sql.exec("UPDATE scheduler SET lease_count_today = 1000000 WHERE id = 1");
  const before = f.read();
  await f.coordinator.claimDispatchWindow(t0 + 60_000, 30);
  const reason = [...f.sql.exec("SELECT last_claim_reason FROM scheduler WHERE id = 1")][0]
    .last_claim_reason;
  assert.equal(reason, "daily_lease_limit");
  assert.ok(f.read() > before, "a changed reason is a persisted outcome");
});
