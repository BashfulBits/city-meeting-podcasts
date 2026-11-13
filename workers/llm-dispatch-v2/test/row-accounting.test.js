import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import {
  EARLY_RELEASE_DEFAULTS, earlyReleaseConfigFromEnv, partialRefusal,
} from "../src/early_release.js";
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

test("rescue stats survive recreation without writes or queued-job scans", async () => {
  const f = fixture();
  const cursor = JSON.stringify({ after: [10, "a"], through: [20, "b"] });
  f.sql.exec("UPDATE scheduler SET catalog_digest=?, catalog_rescue_cursor=?, catalog_rescue_complete=0 WHERE id=1",
    "catalog", cursor);
  const before = f.read();
  let written = 0;
  const queries = [];
  const raw = f.sql.exec.bind(f.sql);
  f.sql.exec = (...args) => {
    queries.push(args[0]);
    const result = raw(...args);
    written += Number(result.rowsWritten) || 0;
    return result;
  };
  const recreated = f.make();
  queries.length = 0; // Existing constructor migrations are outside the stats read.
  assert.deepEqual((await recreated.stats(Date.now())).catalog_rescue,
    { digest: "catalog", cursor, complete: false });
  assert.equal(written, 0);
  assert.equal(f.read(), before);
  const jobReads = queries.filter(query => /FROM\s+jobs\b/i.test(query));
  assert.ok(jobReads.every(query => /state\s*=\s*'leased'/i.test(query)));
  assert.equal(queries.filter(query => /SELECT[\s\S]*catalog_digest/i.test(query)).length, 1);
  f.sql.exec("UPDATE scheduler SET catalog_rescue_complete=1 WHERE id=1");
  written = 0;
  assert.equal((await f.make().stats(Date.now())).catalog_rescue.complete, true);
  assert.equal(written, 0);
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

test("idle recovery statistics and claim counts share one durable write per sweep", async () => {
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

  // Each sweep writes recovery telemetry on the scheduler's accounting row.
  for (let i = 1; i <= 9; i++) await f.coordinator.claimDispatchWindow(t0 + i * minute, 30);
  assert.equal(f.read(), first + 9);
  assert.equal(scheduler().last_claim_at, t0 + 9 * minute);
  assert.equal(scheduler().claim_empty_count_today, 10);
  // Stats reports the same durable counts.
  const live = (await f.coordinator.stats(t0 + 9 * minute)).claim;
  assert.equal(live.empty_count_today, 10);
  assert.equal(live.reason_counts_today.no_queued_work, 10);
  assert.equal(live.last_at, t0 + 9 * minute);
  // The detailed view agrees with the ordinary one.
  const detailed = (await f.coordinator.detailedStats(t0 + 9 * minute, 5)).claim;
  assert.equal(detailed.empty_count_today, 10);
  assert.equal(detailed.reason_counts_today.no_queued_work, 10);
  assert.equal(detailed.last_at, t0 + 9 * minute);

  // The next tick also uses a single accounting row.
  await f.coordinator.claimDispatchWindow(t0 + 10 * minute, 30);
  assert.equal(f.read(), first + 10);
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


test("rate failure snapshot uses bounded primary-key reads and survives exhausted write budget", async () => {
  const f = fixture();
  f.coordinator._dispatchLimits = () => ({ routes_by_id: { "route-a": {} } });
  const day = "2026-10-09";
  for (const [date, route, cls] of [[day, "route-a", "own_rpm"],
    ["2026-10-08", "route-a", "own_rpm"], [day, "retired", "own_rpm"],
    [day, "route-a", "auth"]]) {
    f.sql.exec(
      "INSERT INTO route_failures (utc_day, route_id, failure_class, count, last_status, last_seen_at) VALUES (?,?,?,?,?,?)",
      date, route, cls, 3, 429, 1
    );
  }
  f.coordinator = f.make(); // Persisted counters survive object recreation.
  f.coordinator._dispatchLimits = () => ({ routes_by_id: { "route-a": {} } });
  const raw = f.sql.exec.bind(f.sql);
  const queries = [];
  f.sql.exec = (query, ...args) => {
    queries.push(query);
    if (!/^\s*SELECT/i.test(query)) throw new Error("row writes exceeded");
    return raw(query, ...args);
  };
  f.coordinator._rowsSinceLog = 0; // Constructor telemetry is outside the measured RPC.
  const result = await f.coordinator.rateFailureStats(Date.parse(day));
  assert.deepEqual(result.route_failures.map(row => ({ ...row })), [{ utc_day: day, route_id: "route-a",
    failure_class: "own_rpm", count: 3 }]);
  // One authority read, then one bounded primary-key read per tracked class.
  assert.equal(queries.length, 4);
  assert.match(queries[0], /packed_state_active/);
  assert.ok(queries.slice(1).every(q => /WHERE utc_day = \? AND route_id = \? AND failure_class = \?/.test(q)));
  assert.equal((await f.coordinator.rateFailureStats(Date.parse("2026-10-10"))).route_failures.length, 0);
  f.coordinator._dispatchLimits = () => ({ routes_by_id: Object.fromEntries(
    Array.from({ length: 129 }, (_, i) => [String(i), {}])) });
  queries.length = 0;
  assert.equal((await f.coordinator.rateFailureStats(Date.parse(day))).truncated, true);
  assert.equal(queries.length, 0);
  f.coordinator._dispatchLimits = () => ({ routes_by_id: { "route-a": {} } });
  f.sql.exec = () => { throw new Error("storage unavailable"); };
  await assert.rejects(f.coordinator.rateFailureStats(Date.parse(day)), /storage unavailable/);
});

// ---- review/58 WP4: what an early-release partial flush costs in DO rows ----------------------------

// r-a, r-b, r-c, r-d hash to four different telemetry shards; r-a and r-i share shard 3.
const wp4Catalog = routes => ({
  model_aliases: {},
  model_routes_map: { "acme/m": routes },
  routes_by_id: Object.fromEntries(routes.map((id, i) => [id, {
    model: "acme/m", provider: `p${i}`, upstream_model: id, rpm: 1000, rpd: 100000, free: true,
    concurrency: 1, input_context_limit: 64000, output_context_limit: 4096,
  }])),
});
const wp4Job = id => ({
  id, idempotency_key: `k-${id}`, request_digest: `d-${id}`,
  policy_json: JSON.stringify({ allowed_models: ["acme/m"] }), prompt_family: "tags",
  input_token_estimate: 500, max_output_token_estimate: 200, payload_key: `payloads/${id}`,
});

/**
 * Rows the completion side of ONE bundle writes (partials + final), in steady state (the second
 * bundle of the day, so first-of-day usage cells already exist), for a bundle of `size` calls on
 * distinct routes and providers settled in 1, 2 or 3 commits.
 */
async function completionRows(t, { packed, size, partials, routes = ["r-a", "r-b", "r-c", "r-d"] }) {
  const clock = { value: Date.UTC(2026, 10, 12, 12) };
  t.mock.method(Date, "now", () => clock.value);
  const shared = createMockSqlStorage();
  const c = new LLMSchedulerDO({ storage: shared.storage }, withTestReservations({
    MAX_JOBS_PER_UTC_DAY: "500", DISPATCH_LIMITS_OVERRIDE: wp4Catalog(routes), MAX_BUNDLE_JOBS: String(size),
    MAX_JOBS_PER_ROUTE_PER_BUNDLE: "1", MAX_IN_FLIGHT_LLM_CALLS: "20",
    EARLY_SLOT_RELEASE_MAX_COMMITS: "3",
  }));
  await c.enqueueBatch(Array.from({ length: 2 * size }, (_, i) => wp4Job(`j${i}`)));
  if (packed) {
    await c.pauseDispatch({ scope: "global", seconds: 3600 }, clock.value - 960_000);
    c._transactionSync(() => c._stagePackedCutover(clock.value, { maxJobs: 200, maxModelRows: 400,
      maxGroups: 200, maxProviders: 20, maxStateRows: 500, reserveOtherWrites: 1000 }));
    await c.resumeDispatch({ scope: "global" }, clock.value);
  }
  const rows = () => [...shared.sql.exec("SELECT rows_written_today FROM scheduler WHERE id=1")][0]
    .rows_written_today;
  let measured = null;
  for (let round = 0; round < 2; round++) {
    clock.value += 60_000;
    const plan = await c.claimDispatchWindow(clock.value, 25);
    assert.equal(plan.jobs.length, size);
    const result = p => ({
      job_id: p.id, lease_token: p.lease_token, attempt_id: `a-${p.id}`, planned_at: p.not_before_at,
      actual_start_at: clock.value, actual_end_at: clock.value + 3000, observed_input_tokens: 400,
      observed_output_tokens: 50, result_key: `r/${p.id}`, outcome: "success", provider_status_code: 200,
    });
    // Partials settle in catalog route order (so r-a goes first in the shard-collision case).
    const ordered = [...plan.jobs].sort((a, b) => routes.indexOf(a.route_id) - routes.indexOf(b.route_id));
    const groups = [...ordered.slice(0, partials).map(p => [p]), ordered.slice(partials)];
    const before = rows();
    for (const [i, group] of groups.entries()) {
      clock.value += 1000;
      const commit = i === groups.length - 1 ? c.completeBatch : c.completePartial;
      const reply = await commit.call(c, plan.bundle_id, plan.execution_token, group.map(result));
      assert.equal(reply.accepted, true);
    }
    measured = rows() - before;
  }
  return measured;
}

test("WP4 row cost: a partial flush adds one row, two in packed mode when it shares a shard with the final", async t => {
  for (const level of ["log", "info", "warn"]) t.mock.method(console, level, () => {});
  const table = [];
  for (const packed of [false, true]) {
    for (const size of [3, 4]) {
      const cost = [];
      for (const partials of [0, 1, 2]) cost.push(await completionRows(t, { packed, size, partials }));
      table.push({ mode: packed ? "packed" : "legacy", size, rows: cost });
    }
  }
  t.diagnostic(`completion rows per bundle [0,1,2 partials]: ${JSON.stringify(table)}`);
  for (const { mode, size, rows } of table) {
    assert.equal(rows[1] - rows[0], 1, `${mode} size ${size}: 1 partial`);
    assert.equal(rows[2] - rows[0], 2, `${mode} size ${size}: 2 partials`);
  }
  // Worst case: the partial's route and a route left for the final hash to the same shard, so the
  // shard row is written by both commits instead of once.
  const collide = ["r-a", "r-i", "r-b"];
  const legacy = [];
  const packed = [];
  for (const partials of [0, 1]) {
    legacy.push(await completionRows(t, { packed: false, size: 3, partials, routes: collide }));
    packed.push(await completionRows(t, { packed: true, size: 3, partials, routes: collide }));
  }
  t.diagnostic(`shard collision, size 3 [0,1 partials]: legacy ${legacy}, packed ${packed}`);
  assert.equal(legacy[1] - legacy[0], 1);
  assert.equal(packed[1] - packed[0], 2);
});

test("WP4 projection at 6,000 results/day: review/58's 20 s / 3 commits passes 85,000; the defaults fit", t => {
  // review/58 section 8: 10.7 rows per claim x 1.3 claims per result = 13.9 (rounded to 14) rows
  // per result, so about 84,000 rows/day at 6,000 results before WP4; mean 3.3 jobs per bundle.
  const base = 6000 * 14;
  const bundles = Math.round((6000 * 1.3) / 3.3);
  const ungated = (partials, rowsEach) => base + bundles * partials * rowsEach;
  assert.equal(bundles, 2364);
  assert.equal(ungated(2, 1), 88_728, "20 s / 3 commits, every bundle flushes twice, 1 row each");
  assert.equal(ungated(2, 2), 93_456, "worst case: every partial shares a shard with the final");
  assert.equal(ungated(1, 1), 86_364, "even one partial per bundle passes 85,000");
  // Only (85,000 - 84,000) = 1,000 rows of headroom: that is EARLY_SLOT_RELEASE_DAILY_ROWS.
  assert.equal(EARLY_RELEASE_DEFAULTS.EARLY_SLOT_RELEASE_DAILY_ROWS, 85_000 - base);

  // A day of 1,440 ticks, the other work spread by `weight`, every bundle asking for two partials
  // at the worst two rows each, the DO's gate (budget, then pace) deciding each one.
  const config = earlyReleaseConfigFromEnv({});
  const simulate = (weight) => {
    const day = Date.UTC(2026, 10, 12);
    const total = Array.from({ length: 1440 }, (_, m) => weight(m)).reduce((a, b) => a + b, 0);
    let rows = 0;
    let partialRows = 0;
    let asks = 0;
    for (let minute = 0; minute < 1440; minute++) {
      const share = weight(minute) / total;
      rows += base * share;
      asks += bundles * share * 2;
      for (; asks >= 1; asks -= 1) {
        const nowMs = day + minute * 60_000 + 30_000;
        if (partialRefusal({ rowsToday: rows, partialRowsToday: partialRows, config, nowMs })) continue;
        rows += 2;
        partialRows += 2;
      }
    }
    return { rows: Math.round(rows), partialRows };
  };
  const days = {};
  for (const [name, weight] of Object.entries({
    uniform: () => 1,
    back_loaded_1_25: minute => (minute < 720 ? 1 : 1.25),
    back_loaded_3: minute => (minute < 720 ? 1 : 3),
    front_loaded_3: minute => (minute < 720 ? 3 : 1),
  })) {
    days[name] = simulate(weight);
    assert.ok(days[name].rows <= 85_000, `${name} day ${days[name].rows}`);
  }
  // A lighter day (today's volume) still gets the whole budget.
  t.diagnostic(`rows/day at 6,000 results with the default gate: ${JSON.stringify(days)}`);
  assert.ok(days.uniform.partialRows >= 900);
});
