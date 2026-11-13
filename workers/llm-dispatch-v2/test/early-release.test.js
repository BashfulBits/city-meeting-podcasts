import test from "node:test";
import assert from "node:assert/strict";
import worker, { LLMSchedulerDO } from "../src/index.js";
import TUNING from "../src/dispatch_tuning.json" with { type: "json" };
import {
  EARLY_RELEASE_DEFAULTS, createBundleSettler, earlyReleaseConfig, earlyReleaseConfigFromEnv,
  dailyBudgetAllows, partialRefusal, rowPaceAllows,
} from "../src/early_release.js";
import { activeBundles, createMockSqlStorage, withTestReservations } from "./helpers.js";

// review/58 WP4: early slot release. One model served by two routes on two providers, each route
// one call at a time, and an in-flight cap of two: a two-job bundle fills both, so the next claim
// can only proceed once a slot is released.
const CATALOG = {
  model_aliases: {},
  model_routes_map: { "acme/pair": ["acme-a", "beta-a"] },
  routes_by_id: Object.fromEntries([["acme-a", "acme"], ["beta-a", "beta"]].map(([id, provider]) => [
    id, {
      model: "acme/pair", provider, upstream_model: id, rpm: 1000, rpd: 100000, free: true,
      concurrency: 1, input_context_limit: 64000, output_context_limit: 4096,
    },
  ])),
};

const BASE_ENV = {
  MAX_JOBS_PER_UTC_DAY: "10000",
  MAX_BUNDLE_JOBS: "4",
  MAX_JOBS_PER_ROUTE_PER_BUNDLE: "1",
  MAX_CONCURRENT_ROUTE_LANES: "5",
  MAX_ACTIVE_BUNDLES: "4",
  MAX_IN_FLIGHT_LLM_CALLS: "2",
  MAX_BUNDLES_PER_UTC_DAY: "1000",
  LEASE_DURATION_SECONDS: "840",
  ESTIMATED_CALL_DURATION_CEILING_SECONDS: "5",
  // Two partials allowed, so retried and repeated partials reach the dedupe (default is 2 commits).
  EARLY_SLOT_RELEASE_MAX_COMMITS: "3",
};

function make(t, env = {}) {
  const shared = createMockSqlStorage();
  const clock = { value: Date.UTC(2026, 10, 12, 12) };
  t.mock.method(Date, "now", () => clock.value);
  const fullEnv = withTestReservations({ ...BASE_ENV, DISPATCH_LIMITS_OVERRIDE: CATALOG, ...env });
  const coordinator = new LLMSchedulerDO({ storage: shared.storage }, fullEnv);
  const ladder = [];
  for (const level of ["info", "warn"]) {
    t.mock.method(console, level, (line) => {
      if (typeof line === "string" && line.includes('"route_health"')) ladder.push(JSON.parse(line));
    });
  }
  return { coordinator, clock, ladder, ...shared };
}

const job = (id) => ({
  id, idempotency_key: `key-${id}`, request_digest: `digest-${id}`,
  policy_json: JSON.stringify({ allowed_models: ["acme/pair"] }), prompt_family: "tags",
  input_token_estimate: 500, max_output_token_estimate: 200, payload_key: `payloads/${id}/request.json`,
});

const SUCCESS = { outcome: "success", provider_status_code: 200 };
const TIMEOUT = { outcome: "retryable_error", failure_class: "provider_response_timeout", provider_status_code: null };

function resultFor(f, planned, outcome = SUCCESS) {
  const now = f.clock.value;
  return {
    job_id: planned.id, lease_token: planned.lease_token, attempt_id: `att-${planned.id}`,
    planned_at: planned.not_before_at, actual_start_at: now - 3000, actual_end_at: now,
    observed_input_tokens: 400, observed_output_tokens: 50, result_key: `results/${planned.id}.json`,
    ...outcome,
  };
}

const jobRow = (sql, id) => [...sql.exec("SELECT state, lease_token FROM jobs WHERE id = ?", id)][0];
const usageCalls = async (f) => (await f.coordinator.detailedStats(f.clock.value, 5)).usage_today
  .reduce((sum, cell) => sum + cell.calls, 0);
const rows = (sql) => [...sql.exec("SELECT rows_written_today FROM scheduler WHERE id = 1")][0]
  .rows_written_today;

async function claimTwo(f) {
  await f.coordinator.enqueueBatch(["j1", "j2", "j3"].map(job));
  const plan = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.equal(plan.jobs.length, 2, "one job per route");
  assert.deepEqual(new Set(plan.jobs.map(j => j.route_id)), new Set(["acme-a", "beta-a"]));
  return plan;
}

// ---- Coordinator ---------------------------------------------------------------------------------

test("a partial frees the settled call's slot and route lease while its sibling still runs", async t => {
  const f = make(t);
  const plan = await claimTwo(f);
  const [fast, slow] = plan.jobs;
  f.clock.value += 5000;
  const reply = await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, fast)]);
  assert.deepEqual(reply, { accepted: true, settled: 1, duplicates: 0, bundle_closed: false });
  assert.equal(jobRow(f.sql, fast.id).state, "completed");
  assert.equal(jobRow(f.sql, slow.id).state, "leased");
  const bundle = activeBundles(f.sql)[plan.bundle_id];
  assert.equal(bundle.active_call_count, 1);
  assert.deepEqual(bundle.settled_job_ids, [fast.id]);
  assert.equal(bundle.partial_flushes, 1);
  assert.deepEqual(f.coordinator._leaseCountsByRoute().map(r => r.lease_route_id), [slow.route_id]);

  // The next tick claims the queued job onto the freed route, while the slow call still runs.
  f.clock.value += 60_000;
  const next = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.equal(next.claim_result, "claimed");
  assert.deepEqual(next.jobs.map(j => [j.id, j.route_id]), [["j3", fast.route_id]]);
  assert.equal(next.claim_diagnostics.in_flight_calls, 1);

  // The final commit settles the rest and closes the bundle as before.
  const done = await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token,
    [resultFor(f, slow)]);
  assert.equal(done.bundle_closed, true);
  assert.equal(activeBundles(f.sql)[plan.bundle_id], undefined);
  assert.equal(jobRow(f.sql, slow.id).state, "completed");
});

test("without a partial the same claim is refused at the in-flight cap (today's behaviour)", async t => {
  const f = make(t);
  await claimTwo(f);
  f.clock.value += 60_000;
  const next = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.equal(next.claim_reason, "in_flight_call_limit");
});

test("a final that repeats a partial's job, and a retried partial, settle and count it once", async t => {
  const f = make(t);
  const plan = await claimTwo(f);
  const [fast, slow] = plan.jobs;
  const partialResults = [resultFor(f, fast)];
  await f.coordinator.completePartial(plan.bundle_id, plan.execution_token, partialResults);
  const afterPartial = rows(f.sql);
  assert.equal(await usageCalls(f), 1);

  // A retried partial (its reply was lost) is accepted, settles nothing and writes no row.
  const retried = await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    partialResults);
  assert.deepEqual(retried, { accepted: true, settled: 0, duplicates: 1, bundle_closed: false });
  assert.equal(rows(f.sql), afterPartial);
  assert.equal(activeBundles(f.sql)[plan.bundle_id].partial_flushes, 1);

  // The final repeats the partial's job (it was in flight when the final was sent).
  const done = await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token,
    [...partialResults, resultFor(f, slow)]);
  assert.deepEqual(done, { accepted: true, settled: 1, duplicates: 1, bundle_closed: true });
  assert.equal(await usageCalls(f), 2, "one usage call per job");
  const counters = (await f.coordinator.stats(f.clock.value)).scheduler;
  assert.equal(counters.completed_today, 2);
});

test("a final that arrives before an in-flight partial closes the bundle; the partial is a no-op", async t => {
  const f = make(t);
  const plan = await claimTwo(f);
  const all = plan.jobs.map(planned => resultFor(f, planned));
  await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token, all);
  const before = rows(f.sql);
  const late = await f.coordinator.completePartial(plan.bundle_id, plan.execution_token, [all[0]]);
  assert.deepEqual(late, { accepted: false, reason: "stale_bundle" });
  assert.equal(rows(f.sql), before);
  assert.equal(await usageCalls(f), 2);
});

test("a partial for a reaped bundle, or with a wrong execution token, changes nothing", async t => {
  const f = make(t);
  const plan = await claimTwo(f);
  const before = rows(f.sql);
  assert.deepEqual(
    await f.coordinator.completePartial(plan.bundle_id, "not-the-token", [resultFor(f, plan.jobs[0])]),
    { accepted: false, reason: "stale_bundle" }
  );
  assert.equal(rows(f.sql), before);

  // The lease expires and the next claim reaps the bundle and requeues its jobs.
  f.clock.value += 841_000;
  await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.equal(activeBundles(f.sql)[plan.bundle_id], undefined);
  const late = await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[0])]);
  assert.deepEqual(late, { accepted: false, reason: "stale_bundle" });
  assert.notEqual(jobRow(f.sql, plan.jobs[0].id).state, "completed");
});

test("a partial for a job whose lease was already settled elsewhere settles nothing", async t => {
  const f = make(t);
  const plan = await claimTwo(f);
  const stale = { ...resultFor(f, plan.jobs[0]), lease_token: "an-older-lease" };
  const reply = await f.coordinator.completePartial(plan.bundle_id, plan.execution_token, [stale]);
  assert.equal(reply.settled, 0);
  assert.equal(jobRow(f.sql, plan.jobs[0].id).state, "leased");
  assert.equal(activeBundles(f.sql)[plan.bundle_id].active_call_count, 2);
});

test("the DO caps partials per bundle at MAX_COMMITS - 1", async t => {
  const f = make(t, { MAX_IN_FLIGHT_LLM_CALLS: "4", EARLY_SLOT_RELEASE_MAX_COMMITS: "2" });
  const plan = await claimTwo(f);
  assert.equal((await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[0])])).accepted, true);
  const before = rows(f.sql);
  assert.deepEqual(await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[1])]), { accepted: false, reason: "commit_cap" });
  assert.equal(rows(f.sql), before);
  assert.equal(jobRow(f.sql, plan.jobs[1].id).state, "leased");
  // The final is never capped.
  assert.equal((await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[1])])).bundle_closed, true);
});

test("kill switch: the DO refuses partials and completeBatch behaves exactly as before", async t => {
  const f = make(t, { EARLY_SLOT_RELEASE_ENABLED: "0" });
  const plan = await claimTwo(f);
  const before = rows(f.sql);
  assert.deepEqual(await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[0])]), { accepted: false, reason: "disabled" });
  assert.equal(rows(f.sql), before);
  assert.equal(jobRow(f.sql, plan.jobs[0].id).state, "leased");
  // A final that leaves a job unreported keeps the bundle entry as it always did.
  await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token, [resultFor(f, plan.jobs[0])]);
  const entry = activeBundles(f.sql)[plan.bundle_id];
  assert.equal(entry.active_call_count, 2);
  assert.equal(entry.settled_job_ids, undefined);
});

test("a timeout settled by a partial escalates the route health ladder once, not twice", async t => {
  const f = make(t);
  const plan = await claimTwo(f);
  const [failed, other] = plan.jobs;
  const timeout = resultFor(f, failed, TIMEOUT);
  await f.coordinator.completePartial(plan.bundle_id, plan.execution_token, [timeout]);
  await f.coordinator.completePartial(plan.bundle_id, plan.execution_token, [timeout]); // retried
  await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token,
    [timeout, resultFor(f, other)]);
  const escalations = f.ladder.filter(e => e.action === "escalate" && e.route_id === failed.route_id);
  assert.equal(escalations.length, 1);
  const map = JSON.parse([...f.sql.exec("SELECT route_health_json FROM scheduler WHERE id = 1")][0]
    .route_health_json);
  assert.equal(map[failed.route_id].rung, 1);
  assert.equal(map[failed.route_id].timeout, 1);
  assert.equal(jobRow(f.sql, failed.id).state, "queued", "requeued onto the sibling route");
});

test("row pace: a partial is refused, writing nothing, while the day is ahead of the pace line", async t => {
  const f = make(t);
  const plan = await claimTwo(f);
  // 12:00 UTC: the 85,000/day line is at 42,500 rows.
  f.sql.exec("UPDATE scheduler SET rows_written_today = 42499 WHERE id = 1");
  const before = rows(f.sql);
  assert.deepEqual(await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[0])]), { accepted: false, reason: "row_pace" });
  assert.equal(rows(f.sql), before);
  assert.equal(jobRow(f.sql, plan.jobs[0].id).state, "leased");
  // Back under the line (or later in the day) it is accepted; the final is never paced.
  f.sql.exec("UPDATE scheduler SET rows_written_today = 42000 WHERE id = 1");
  assert.equal((await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[0])])).accepted, true);
  f.sql.exec("UPDATE scheduler SET rows_written_today = 80000 WHERE id = 1");
  assert.equal((await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[1])])).bundle_closed, true);
});

test("row pace line: prorated by UTC time of day, worst partial cost included", () => {
  const day = Date.UTC(2026, 10, 12);
  assert.equal(rowPaceAllows(0, 85_000, day), false, "midnight: no headroom yet");
  assert.equal(rowPaceAllows(21_248, 85_000, day + 6 * 3_600_000), true);
  assert.equal(rowPaceAllows(21_249, 85_000, day + 6 * 3_600_000), false);
  assert.equal(rowPaceAllows(84_999, 85_000, day + 86_399_000), false);
  assert.equal(rowPaceAllows(10, 0, day + 43_200_000), false, "pace 0 refuses every partial");
  // The daily budget is prorated the same way and checked first.
  assert.equal(dailyBudgetAllows(498, 1000, day + 43_200_000), true);
  assert.equal(dailyBudgetAllows(499, 1000, day + 43_200_000), false);
  assert.equal(partialRefusal({ rowsToday: 0, partialRowsToday: 600,
    config: { dailyRows: 1000, rowPace: 85_000 }, nowMs: day + 43_200_000 }), "daily_rows");
  assert.equal(partialRefusal({ rowsToday: 50_000, partialRowsToday: 0,
    config: { dailyRows: 1000, rowPace: 85_000 }, nowMs: day + 43_200_000 }), "row_pace");
  assert.equal(partialRefusal({ rowsToday: 30_000, partialRowsToday: 100,
    config: { dailyRows: 1000, rowPace: 85_000 }, nowMs: day + 43_200_000 }), null);
});

test("daily budget: each partial that writes charges 2 rows to today's ledger; a full budget refuses", async t => {
  const f = make(t, { EARLY_SLOT_RELEASE_DAILY_ROWS: "1000" });
  const plan = await claimTwo(f);
  const ledger = () => JSON.parse([...f.sql.exec("SELECT early_release_json FROM scheduler WHERE id = 1")][0]
    .early_release_json);
  // Yesterday's ledger counts for nothing today.
  f.sql.exec("UPDATE scheduler SET early_release_json = ? WHERE id = 1",
    JSON.stringify({ utc_day: "2026-11-11", partial_rows: 999 }));
  const partialResults = [resultFor(f, plan.jobs[0])];
  assert.equal((await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    partialResults)).accepted, true);
  assert.deepEqual(ledger(), { utc_day: "2026-11-12", partial_rows: 2 });
  // A retried partial writes nothing and is not charged.
  await f.coordinator.completePartial(plan.bundle_id, plan.execution_token, partialResults);
  assert.deepEqual(ledger(), { utc_day: "2026-11-12", partial_rows: 2 });
  // At 12:00 the prorated budget is 500 rows.
  f.sql.exec("UPDATE scheduler SET early_release_json = ? WHERE id = 1",
    JSON.stringify({ utc_day: "2026-11-12", partial_rows: 499 }));
  const before = rows(f.sql);
  assert.deepEqual(await f.coordinator.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[1])]), { accepted: false, reason: "daily_rows" });
  assert.equal(rows(f.sql), before);
  // The final is never refused, and a final does not charge the ledger.
  assert.equal((await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[1])])).bundle_closed, true);
  assert.deepEqual(ledger(), { utc_day: "2026-11-12", partial_rows: 499 });
});

test("old state: the ledger column is added additively and a corrupt ledger counts as empty", async t => {
  const f = make(t);
  f.sql.exec("ALTER TABLE scheduler DROP COLUMN early_release_json");
  const g = new LLMSchedulerDO({ storage: f.storage }, withTestReservations({
    ...BASE_ENV, DISPATCH_LIMITS_OVERRIDE: CATALOG,
  }));
  const columns = [...f.sql.exec("PRAGMA table_info(scheduler)")].map(c => c.name);
  assert.ok(columns.includes("early_release_json"));
  f.sql.exec("UPDATE scheduler SET early_release_json = '{not json' WHERE id = 1");
  await g.enqueueBatch(["k1", "k2"].map(job));
  const plan = await g.claimDispatchWindow(f.clock.value, 25);
  assert.equal((await g.completePartial(plan.bundle_id, plan.execution_token,
    [resultFor(f, plan.jobs[0])])).accepted, true);
});

test("a bundle entry written by a previous version (no WP4 fields) is completed as before", async t => {
  const f = make(t);
  const plan = await claimTwo(f);
  const entry = activeBundles(f.sql)[plan.bundle_id];
  assert.equal(entry.settled_job_ids, undefined);
  assert.equal(entry.partial_flushes, undefined);
  const reply = await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token,
    plan.jobs.map(planned => resultFor(f, planned)));
  assert.equal(reply.bundle_closed, true);
});

// ---- Settler (executor side) ---------------------------------------------------------------------

function fakeClock() {
  const timers = [];
  const clock = {
    value: 0,
    now: () => clock.value,
    setTimer: (fn, ms) => {
      const timer = { at: clock.value + ms, fn };
      timers.push(timer);
      return timer;
    },
    clearTimer: (timer) => {
      const index = timers.indexOf(timer);
      if (index >= 0) timers.splice(index, 1);
    },
    async advance(ms) {
      clock.value += ms;
      for (;;) {
        timers.sort((a, b) => a.at - b.at);
        if (!timers.length || timers[0].at > clock.value) break;
        timers.shift().fn();
        await settleMicrotasks();
      }
      await settleMicrotasks();
    },
  };
  return clock;
}

const settleMicrotasks = () => new Promise(resolve => setImmediate(resolve));

function fakeCoordinator({ partial = async () => ({ accepted: true }) } = {}) {
  const calls = [];
  return {
    calls,
    completePartial: async (bundleId, token, results) => {
      calls.push({ kind: "partial", ids: results.map(r => r.job_id) });
      return partial(results);
    },
    completeBatch: async (bundleId, token, results) => {
      calls.push({ kind: "final", ids: results.map(r => r.job_id) });
      return { accepted: true };
    },
  };
}

const config = (overrides = {}) => ({ enabled: true, debounceMs: 20_000, maxCommits: 3, ...overrides });

function settler(coordinator, clock, overrides = {}) {
  return createBundleSettler({
    coordinator, bundleId: "b", executionToken: "tok", totalJobs: 4, laneCount: 4, startAt: 0,
    config: config(), now: clock.now, setTimer: clock.setTimer, clearTimer: clock.clearTimer,
    ...overrides,
  });
}

const r = id => ({ job_id: id, lease_token: `lease-${id}` });

test("settler: debounce from bundle start, at most MAX_COMMITS - 1 partials, rest in the final", async () => {
  const clock = fakeClock();
  const coordinator = fakeCoordinator();
  const s = settler(coordinator, clock);
  await clock.advance(3000);
  s.settle(r("a"));
  s.laneClosed();
  await settleMicrotasks();
  assert.deepEqual(coordinator.calls, [], "a 3 s call waits for the 20 s debounce");
  await clock.advance(17_000);
  assert.deepEqual(coordinator.calls, [{ kind: "partial", ids: ["a"] }]);
  await clock.advance(5000);
  s.settle(r("b"));
  s.laneClosed();
  await settleMicrotasks();
  assert.equal(coordinator.calls.length, 1, "next partial waits 20 s after the previous one");
  await clock.advance(15_000);
  assert.deepEqual(coordinator.calls[1], { kind: "partial", ids: ["b"] });
  await clock.advance(30_000);
  s.settle(r("c"));
  s.laneClosed();
  await clock.advance(60_000);
  assert.equal(coordinator.calls.length, 2, "the commit cap leaves only the final");
  s.settle(r("d"));
  const out = await s.finish();
  assert.deepEqual(coordinator.calls[2], { kind: "final", ids: ["c", "d"] });
  assert.equal(out.partialFlushes, 2);
  assert.equal(out.commits, 3);
  assert.deepEqual(out.releases.get("a"), { releasedAt: 20_000, releasedBy: "partial" });
  assert.deepEqual(out.releases.get("b"), { releasedAt: 40_000, releasedBy: "partial" });
  assert.equal(out.releases.get("c").releasedBy, "final");
});

test("settler: no partial once every call has settled; a fast bundle commits once", async () => {
  const clock = fakeClock();
  const coordinator = fakeCoordinator();
  const s = settler(coordinator, clock, { totalJobs: 2, laneCount: 2 });
  await clock.advance(3000);
  s.settle(r("a")); // debounce timer armed for t=20 s
  await clock.advance(10_000);
  s.settle(r("b")); // every call settled before the timer fires
  await clock.advance(60_000);
  const out = await s.finish();
  assert.deepEqual(coordinator.calls, [{ kind: "final", ids: ["a", "b"] }]);
  assert.equal(out.commits, 1);
});

test("settler: switched off, or MAX_COMMITS 1, it never sends a partial", async () => {
  for (const cfg of [config({ enabled: false }), earlyReleaseConfig((name, fallback) =>
    name === "EARLY_SLOT_RELEASE_MAX_COMMITS" ? 1 : fallback)]) {
    const clock = fakeClock();
    const coordinator = fakeCoordinator();
    const s = settler(coordinator, clock, { config: cfg });
    await clock.advance(30_000);
    s.settle(r("a"));
    await clock.advance(60_000);
    s.settle(r("b"));
    await s.finish();
    assert.deepEqual(coordinator.calls, [{ kind: "final", ids: ["a", "b"] }]);
  }
});

test("settler: a failed or refused partial goes into the final and stops further partials", async () => {
  for (const partial of [
    async () => { throw new Error("completePartial is not a method (older coordinator)"); },
    async () => ({ accepted: false, reason: "disabled" }),
  ]) {
    const clock = fakeClock();
    const coordinator = fakeCoordinator({ partial });
    const errors = [];
    const s = settler(coordinator, clock, { onError: err => errors.push(err) });
    await clock.advance(25_000);
    s.settle(r("a"));
    await settleMicrotasks();
    await clock.advance(25_000);
    s.settle(r("b"));
    await clock.advance(25_000);
    s.settle(r("c"));
    s.settle(r("d"));
    const out = await s.finish();
    assert.deepEqual(coordinator.calls, [
      { kind: "partial", ids: ["a"] }, { kind: "final", ids: ["a", "b", "c", "d"] },
    ]);
    assert.equal(errors.length, 1);
    assert.equal(out.releases.get("a").releasedBy, "final");
  }
});

test("settler: the final never waits for a partial still in flight, and carries its results", async () => {
  const clock = fakeClock();
  let releasePartial;
  const coordinator = fakeCoordinator({
    partial: () => new Promise(resolve => { releasePartial = () => resolve({ accepted: true }); }),
  });
  const s = settler(coordinator, clock, { totalJobs: 2, laneCount: 2 });
  await clock.advance(25_000);
  s.settle(r("a"));
  await settleMicrotasks();
  s.settle(r("b"));
  const finishing = s.finish();
  await settleMicrotasks();
  assert.deepEqual(coordinator.calls, [
    { kind: "partial", ids: ["a"] }, { kind: "final", ids: ["a", "b"] },
  ], "the final was sent while the partial was still open");
  releasePartial();
  const out = await finishing;
  assert.equal(out.releases.get("a").releasedBy, "final", "the first confirmed commit wins");
});

test("settler: a partial opens a connection only when lanes leave room beside the shared slot", async () => {
  const clock = fakeClock();
  const coordinator = fakeCoordinator();
  const s = settler(coordinator, clock, { totalJobs: 6, laneCount: 6 });
  await clock.advance(25_000);
  s.settle(r("a"));
  await clock.advance(5000);
  assert.deepEqual(coordinator.calls, [], "six lanes open: no seventh connection");
  s.laneClosed();
  await settleMicrotasks();
  assert.deepEqual(coordinator.calls, [], "five lanes plus the probe/cleanup slot fill six");
  s.laneClosed();
  await settleMicrotasks();
  assert.deepEqual(coordinator.calls, [{ kind: "partial", ids: ["a"] }]);
});

// ---- Config --------------------------------------------------------------------------------------

test("config: compiled defaults, the switch, and the three-commit ceiling", () => {
  assert.deepEqual(earlyReleaseConfigFromEnv({}),
    { enabled: true, debounceMs: 60_000, maxCommits: 2, rowPace: 85_000, dailyRows: 1000 });
  assert.equal(earlyReleaseConfigFromEnv({ EARLY_SLOT_RELEASE_ENABLED: "0" }).enabled, false);
  assert.equal(earlyReleaseConfigFromEnv({ EARLY_SLOT_RELEASE_MAX_COMMITS: "1" }).enabled, false);
  assert.equal(earlyReleaseConfigFromEnv({ EARLY_SLOT_RELEASE_MAX_COMMITS: "9" }).maxCommits, 3);
  assert.equal(earlyReleaseConfigFromEnv({ EARLY_SLOT_RELEASE_DEBOUNCE_SECONDS: "x" }).debounceMs,
    60_000);
});

// ---- Scheduled handler (index.js) ----------------------------------------------------------------

const B2_HOST = "s3.us-west-004.backblazeb2.com";
const TICK_CATALOG = {
  model_aliases: {},
  model_routes_map: { "gemini/fast": ["route-fast"], "gemini/slow": ["route-slow"] },
  routes_by_id: Object.fromEntries(["fast", "slow"].map(name => [`route-${name}`, {
    provider: "gemini", upstream_model: name, rpm: 15, rpd: 500, tpm: 250000, free: true,
    input_context_limit: 1048576, output_context_limit: 65536,
  }])),
  providers: {
    gemini: {
      api_base: "https://generativelanguage.googleapis.com/v1beta/openai",
      ai_gateway_slug: "google-ai-studio", ai_gateway_chat_path: "/v1beta/openai/chat/completions",
      chat_path: "/chat/completions", accounts: [{ id: "project_primary", api_key_env: "GEMINI_API_KEY" }],
    },
  },
};

function tickEnv(workerOverrides = {}, coordinatorOverrides = {}) {
  const { storage, sql } = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage }, withTestReservations({
    MAX_JOBS_PER_UTC_DAY: "5000", MAX_BUNDLE_JOBS: "4", MAX_JOBS_PER_MODEL_CLAIM: "4",
    MAX_JOBS_PER_ROUTE_PER_BUNDLE: "1", MAX_CONCURRENT_ROUTE_LANES: "5", MAX_ACTIVE_BUNDLES: "2",
    MAX_IN_FLIGHT_LLM_CALLS: "8", MAX_BUNDLES_PER_UTC_DAY: "1000", LEASE_DURATION_SECONDS: "840",
    ESTIMATED_CALL_DURATION_CEILING_SECONDS: "1", DISPATCH_LIMITS_OVERRIDE: TICK_CATALOG,
    ...coordinatorOverrides,
  }));
  return {
    sql, coordinator,
    env: {
      BEARER_TOKEN: "secret-token", LLM_SCHEDULER: { getByName: () => coordinator },
      DISPATCH_LIMITS_OVERRIDE: TICK_CATALOG, DISPATCH_WINDOW_SECONDS: "25", MAX_RESPONSE_SECONDS: "5",
      FINALIZATION_RESERVE_SECONDS: "1", LEASE_DURATION_SECONDS: "840", CRON_EXECUTION_LIMIT_SECONDS: "900",
      CRON_TICK_SECONDS: "60", MAX_BUNDLES_PER_UTC_DAY: "1000", MAX_BUNDLE_JOBS: "4",
      MAX_JOBS_PER_MODEL_CLAIM: "4", MAX_LEASES_PER_UTC_DAY: "4000", MAX_CONCURRENT_ROUTE_LANES: "5",
      MAX_JOBS_PER_UTC_DAY: "5000", ENQUEUE_BATCH_MAX: "1000", POLL_BATCH_MAX: "1000",
      B2_ENDPOINT: `https://${B2_HOST}`, B2_KEY_ID: "id", B2_APP_KEY: "key", B2_BUCKET: "bucket",
      GEMINI_API_KEY: "fake-gemini-key", EARLY_SLOT_RELEASE_DEBOUNCE_SECONDS: "0",
      ...workerOverrides,
    },
  };
}

/** B2 in memory; the slow model's call is held until `releaseSlow()`. */
function tickFetch(store) {
  let releaseSlow;
  const slowHeld = new Promise(resolve => { releaseSlow = resolve; });
  let slowStarted = false;
  const fetch = async (url, init = {}) => {
    const parsed = new URL(url);
    if (parsed.host === B2_HOST) {
      const key = decodeURIComponent(parsed.pathname.replace(/^\/[^/]+\//, ""));
      if (init.method === "PUT") { store.set(key, init.body); return new Response("", { status: 200 }); }
      if (init.method === "GET") {
        return store.has(key) ? new Response(store.get(key), { status: 200 }) : new Response("", { status: 404 });
      }
      return new Response("", { status: 200 });
    }
    if (String(init.body).includes('"slow"')) {
      slowStarted = true;
      await slowHeld;
    }
    return new Response(JSON.stringify({
      choices: [{ message: { content: "ok" } }], usage: { prompt_tokens: 620, completion_tokens: 140 },
    }), { status: 200 });
  };
  return { fetch, releaseSlow: () => releaseSlow(), slowStarted: () => slowStarted };
}

async function enqueueTickJob(env, store, id, model) {
  store.set(`payloads/${id}/request.json`, JSON.stringify({ model, messages: [{ role: "user", content: "hi" }] }));
  const res = await worker.fetch(new Request("http://localhost/v2/jobs:enqueue-batch", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer secret-token" },
    body: JSON.stringify({ jobs: [{ id, idempotency_key: `key-${id}`, request_digest: `digest-${id}`,
      prompt_family: "tags", input_token_estimate: 500, max_output_token_estimate: 200,
      payload_key: `payloads/${id}/request.json`,
      policy_json: JSON.stringify({ allowed_models: [model], allow_paid: false }) }] }),
  }), env);
  assert.equal(res.status, 200);
}

async function runHeldTick(t, workerOverrides = {}) {
  const store = new Map();
  const { env, coordinator, sql } = tickEnv(workerOverrides);
  const events = [];
  const net = tickFetch(store);
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = net.fetch;
  t.mock.method(console, "info", line => { try { events.push(JSON.parse(line)); } catch {} });
  t.mock.method(console, "warn", () => {});
  t.mock.method(console, "error", () => {});
  const partials = [];
  const realPartial = coordinator.completePartial.bind(coordinator);
  coordinator.completePartial = async (...args) => {
    const reply = await realPartial(...args);
    partials.push(reply);
    return reply;
  };
  await enqueueTickJob(env, store, "fast-1", "gemini/fast");
  await enqueueTickJob(env, store, "slow-1", "gemini/slow");
  const waits = [];
  await worker.scheduled({}, env, { waitUntil: p => waits.push(p) });
  const state = id => [...sql.exec("SELECT state FROM jobs WHERE id = ?", id)][0].state;
  const expectPartial = workerOverrides.EARLY_SLOT_RELEASE_ENABLED !== "0";
  for (let i = 0; i < 400 && !(net.slowStarted() && state("fast-1") === "completed"); i++) {
    if (!expectPartial && net.slowStarted() && i >= 20) break; // nothing is coming
    await new Promise(r => setTimeout(r, 5));
  }
  const whileSlowRuns = {
    fast: state("fast-1"), slow: state("slow-1"), partials: partials.length,
    activeCalls: Object.values(activeBundles(sql))[0]?.active_call_count,
  };
  net.releaseSlow();
  await Promise.all(waits);
  return { events, whileSlowRuns, partials, state };
}

test("scheduled(): a fast call is committed by a partial while its slow sibling still runs", async t => {
  const { events, whileSlowRuns, partials, state } = await runHeldTick(t);
  assert.deepEqual(whileSlowRuns, { fast: "completed", slow: "leased", partials: 1, activeCalls: 1 });
  assert.equal(partials[0].accepted, true);
  assert.equal(state("slow-1"), "completed");
  const done = events.find(e => e.event === "bundle_completed");
  assert.equal(done.commit_ok, true);
  assert.equal(done.partial_flushes, 1);
  assert.equal(done.commits, 2);
  const byRoute = Object.fromEntries(done.calls.map(c => [c.route_id, c]));
  assert.equal(byRoute["route-fast"].released_by, "partial");
  assert.equal(byRoute["route-slow"].released_by, "final");
  assert.ok(byRoute["route-fast"].hold_ms >= 0 && byRoute["route-fast"].hold_ms <= done.bundle_duration_ms);
});

test("scheduled(): with the switch off the bundle commits once, exactly as before", async t => {
  const { events, whileSlowRuns, partials } = await runHeldTick(t, { EARLY_SLOT_RELEASE_ENABLED: "0" });
  assert.equal(partials.length, 0);
  assert.equal(whileSlowRuns.fast, "leased");
  const done = events.find(e => e.event === "bundle_completed");
  assert.equal(done.partial_flushes, 0);
  assert.equal(done.commits, 1);
  assert.ok(done.calls.every(c => c.released_by === "final"));
});

test("tuning lockstep: the compiled dispatch tuning carries early release's code defaults", () => {
  for (const [name, value] of Object.entries(EARLY_RELEASE_DEFAULTS)) {
    assert.equal(TUNING.values[name], value, name);
  }
});
