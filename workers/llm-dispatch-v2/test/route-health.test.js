import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import TUNING from "../src/dispatch_tuning.json" with { type: "json" };
import {
  ROUTE_HEALTH_DEFAULTS, parseRouteHealth, recordRouteHealthFailure, recordRouteHealthSuccess,
  routeHealthConfig, routeHealthGate,
} from "../src/route_health.js";
import { createMockSqlStorage, createRecordingSqlStorage, withTestReservations } from "./helpers.js";

// Two models: one served by two routes on different providers (a sibling to move to), one by a
// single route (nowhere to move: its jobs must wait queued).
const CATALOG = {
  model_aliases: {},
  model_routes_map: {
    "acme/pair": ["acme-a", "beta-a"],
    "solo/one": ["solo-a"],
  },
  routes_by_id: Object.fromEntries([
    ["acme-a", "acme", "acme/pair"], ["beta-a", "beta", "acme/pair"], ["solo-a", "solo", "solo/one"],
  ].map(([routeId, provider, model]) => [routeId, {
    model, provider, upstream_model: routeId, rpm: 1000, rpd: 100000, free: true,
    input_context_limit: 64000, output_context_limit: 4096,
  }])),
};

const BASE_ENV = {
  MAX_JOBS_PER_UTC_DAY: "10000",
  MAX_BUNDLE_JOBS: "4",
  MAX_JOBS_PER_ROUTE_PER_BUNDLE: "4",
  MAX_CONCURRENT_ROUTE_LANES: "5",
  MAX_ACTIVE_BUNDLES: "4",
  MAX_IN_FLIGHT_LLM_CALLS: "8",
  MAX_BUNDLES_PER_UTC_DAY: "1000",
  MAX_QUEUE_WAIT_SECONDS: "86400",
  LEASE_DURATION_SECONDS: "840",
  MAX_429_RETRIES: "1",
  MAX_429_BACKOFF_SECONDS: "5",
  ESTIMATED_CALL_DURATION_CEILING_SECONDS: "5",
};

function make(t, env = {}, shared = createMockSqlStorage()) {
  const clock = { value: Date.UTC(2026, 10, 12, 12) };
  t.mock.method(Date, "now", () => clock.value);
  const fullEnv = withTestReservations({ ...BASE_ENV, DISPATCH_LIMITS_OVERRIDE: CATALOG, ...env });
  const coordinator = new LLMSchedulerDO({ storage: shared.storage }, fullEnv);
  const logs = [];
  for (const level of ["info", "warn"]) {
    t.mock.method(console, level, (line) => {
      if (typeof line === "string" && line.includes('"route_health"')) logs.push(JSON.parse(line));
    });
  }
  return { coordinator, clock, logs, ...shared, remake: () => new LLMSchedulerDO({ storage: shared.storage }, fullEnv) };
}

const job = (id, model) => ({
  id, idempotency_key: `key-${id}`, request_digest: `digest-${id}`,
  policy_json: JSON.stringify({ allowed_models: [model] }), prompt_family: "tags",
  input_token_estimate: 500, max_output_token_estimate: 200, payload_key: `payloads/${id}/request.json`,
});

const health = (sql) => JSON.parse(
  [...sql.exec("SELECT route_health_json FROM scheduler WHERE id = 1")][0].route_health_json
);
const blockedUntil = (sql, routeId) =>
  [...sql.exec("SELECT blocked_until FROM routes WHERE route_id = ?", routeId)][0]?.blocked_until ?? null;
const jobState = (sql, id) => [...sql.exec("SELECT state FROM jobs WHERE id = ?", id)][0]?.state;
const seedHealth = (sql, map) =>
  sql.exec("UPDATE scheduler SET route_health_json = ? WHERE id = 1", JSON.stringify(map));

const TIMEOUT = { outcome: "retryable_error", failure_class: "provider_response_timeout", provider_status_code: null };
const CAPACITY = { outcome: "retryable_error", failure_class: "upstream_capacity", provider_status_code: 503 };
const DEFECT = { outcome: "terminal_error", failure_class: "request_defect", provider_status_code: 400 };
const SUCCESS = { outcome: "success", provider_status_code: 200 };

/** Claim at the clock, then settle every claimed job with `result` (a result or index -> result). */
async function claimAndSettle(f, result, { advance = 0 } = {}) {
  const plan = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  f.clock.value += advance;
  if (plan.jobs.length) await settle(f, plan, result);
  return plan;
}

async function settle(f, plan, result, jobs = plan.jobs) {
  const now = f.clock.value;
  await f.coordinator.completeBatch(plan.bundle_id, plan.execution_token, jobs.map((planned, i) => ({
    job_id: planned.id, lease_token: planned.lease_token, attempt_id: `att-${planned.id}-${now}`,
    planned_at: planned.not_before_at, actual_start_at: now, actual_end_at: now + 500,
    observed_input_tokens: 400, observed_output_tokens: 50, result_key: `results/${planned.id}.json`,
    ...(typeof result === "function" ? result(planned, i) : result),
  })));
}

// ---- Pure ladder -------------------------------------------------------------------------------

test("ladder config: compiled defaults, disable switch, rungs stop at the first zero", () => {
  const read = (overrides) => routeHealthConfig((name, fallback) => overrides[name] ?? fallback);
  assert.deepEqual(read({}), {
    enabled: true, thresholds: { timeout: 1, capacity: 2, defect: 3 },
    rungsMs: [60_000, 300_000, 900_000, 3_600_000],
  });
  assert.equal(read({ ROUTE_HEALTH_LADDER_ENABLED: 0 }).enabled, false);
  assert.deepEqual(read({ ROUTE_HEALTH_RUNG_3_SECONDS: 0 }).rungsMs, [60_000, 300_000]);
  assert.equal(read({ ROUTE_HEALTH_RUNG_1_SECONDS: 0 }).enabled, false, "no rung, no ladder");
});

test("ladder escalates per class at its threshold, climbs on a failed probe and caps at the top", () => {
  const config = routeHealthConfig((_, fallback) => fallback);
  const now = 1_000_000;
  for (const [cls, threshold] of [["timeout", 1], ["capacity", 2], ["defect", 3]]) {
    const map = {};
    for (let i = 1; i < threshold; i++) {
      assert.equal(recordRouteHealthFailure(map, "r", cls, now, config).escalated, false, `${cls} #${i}`);
    }
    const { entry, escalated } = recordRouteHealthFailure(map, "r", cls, now, config);
    assert.ok(escalated, `${cls} escalates at ${threshold}`);
    assert.deepEqual([entry.rung, entry.until, entry.class], [1, now + 60_000, cls]);
  }

  const map = {};
  recordRouteHealthFailure(map, "r", "timeout", now, config);
  // A call already in flight when the cooldown began fails while it cools: counted, no climb.
  const inFlight = recordRouteHealthFailure(map, "r", "capacity", now + 10_000, config);
  assert.deepEqual([inFlight.escalated, map.r.rung, map.r.capacity], [false, 1, 1]);
  // A call that started while the route cooled but finishes after the cooldown is not a probe.
  const late = recordRouteHealthFailure(map, "r", "timeout", map.r.until + 5000, config, now + 30_000);
  assert.deepEqual([late.escalated, map.r.rung], [false, 1]);
  // Each failed half-open probe moves one rung up; the top rung re-arms.
  let at = now;
  for (const [rung, seconds] of [[2, 300], [3, 900], [4, 3600], [4, 3600]]) {
    at = map.r.until;
    const step = recordRouteHealthFailure(map, "r", "timeout", at, config);
    assert.deepEqual([step.escalated, map.r.rung, map.r.until], [true, rung, at + seconds * 1000]);
  }
  assert.deepEqual(recordRouteHealthSuccess(map, "r").rung, 4);
  assert.deepEqual(map, {});
  assert.equal(recordRouteHealthSuccess(map, "r"), null, "a healthy route's success is a no-op");
});

test("ladder gate: cooling, then one probe at a time; a class threshold of 0 exempts it", () => {
  const entry = { timeout: 1, capacity: 0, defect: 0, rung: 1, until: 2000, class: "timeout" };
  assert.equal(routeHealthGate(entry, 1999, 0), "cooling");
  assert.equal(routeHealthGate(entry, 2000, 0), null);
  assert.equal(routeHealthGate(entry, 2000, 1), "probe_in_flight");
  assert.equal(routeHealthGate({ ...entry, rung: 0 }, 0, 5), null);
  assert.equal(routeHealthGate(undefined, 0, 5), null);
  const config = routeHealthConfig((name, fallback) =>
    name === "ROUTE_HEALTH_DEFECT_THRESHOLD" ? 0 : fallback);
  const map = {};
  assert.equal(recordRouteHealthFailure(map, "r", "defect", 0, config).changed, false);
  assert.deepEqual(map, {});
});

test("old or unreadable ladder state reads as every route healthy", () => {
  for (const text of [undefined, null, "", "{", "[]", "null", "42"]) {
    assert.deepEqual({ ...parseRouteHealth(text) }, {}, String(text));
  }
  const parsed = parseRouteHealth(JSON.stringify({
    good: { timeout: 1, capacity: 0, defect: 0, rung: 1, until: 5, class: "timeout" },
    negative: { timeout: -1, capacity: 0, defect: 0, rung: 1, until: 5 },
    partial: { timeout: 1 },
    junk: "x",
  }));
  assert.deepEqual(Object.keys(parsed), ["good"]);
});

test("tuning lockstep: the compiled dispatch tuning carries the ladder's code defaults", () => {
  for (const [name, value] of Object.entries(ROUTE_HEALTH_DEFAULTS)) {
    assert.equal(TUNING.values[name], value, name);
  }
});

// ---- Coordinator --------------------------------------------------------------------------------

test("a response timeout cools its route at once and the next job moves to the sibling route", async t => {
  const f = make(t);
  await f.coordinator.enqueueBatch([job("t1", "acme/pair")]);
  const first = await claimAndSettle(f, TIMEOUT);
  const failedRoute = first.jobs[0].route_id;
  const sibling = failedRoute === "acme-a" ? "beta-a" : "acme-a";
  const start = f.clock.value;

  assert.deepEqual(health(f.sql)[failedRoute],
    { timeout: 1, capacity: 0, defect: 0, rung: 1, until: start + 60_000, class: "timeout" });
  assert.ok(blockedUntil(f.sql, failedRoute) >= start + 60_000);
  assert.equal(jobState(f.sql, "t1"), "queued", "the ladder never fails a job");
  const [event] = f.logs;
  assert.deepEqual([event.action, event.route_id, event.health_class, event.rung, event.previous_rung,
    event.cooldown_seconds], ["escalate", failedRoute, "timeout", 1, 0, 60]);

  await f.coordinator.enqueueBatch([job("t2", "acme/pair")]);
  f.clock.value += 1000;
  const second = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.ok(second.jobs.length > 0);
  assert.ok(second.jobs.every(planned => planned.route_id === sibling), "work flows to the sibling");
  // Cooled through blocked_until, the route drops out of the ranking; diagnostics still name it.
  assert.equal(second.claim_diagnostics.routes.route_health[failedRoute], "cooling");
});

test("upstream capacity enters the ladder on the second consecutive failure, success resets it", async t => {
  const f = make(t);
  await f.coordinator.enqueueBatch([job("c1", "solo/one")]);
  await claimAndSettle(f, CAPACITY);
  assert.deepEqual([health(f.sql)["solo-a"].capacity, health(f.sql)["solo-a"].rung], [1, 0]);
  assert.equal(f.logs.length, 0, "below the threshold nothing escalates");
  assert.equal(jobState(f.sql, "c1"), "queued");

  f.clock.value = Math.max(f.clock.value, blockedUntil(f.sql, "solo-a") ?? 0) + 1000;
  await claimAndSettle(f, CAPACITY);
  const entry = health(f.sql)["solo-a"];
  assert.deepEqual([entry.capacity, entry.rung, entry.class], [2, 1, "capacity"]);
  assert.equal(entry.until, f.clock.value + 60_000);
  assert.ok(blockedUntil(f.sql, "solo-a") >= entry.until);

  f.clock.value = blockedUntil(f.sql, "solo-a") + 1000;
  const probe = await claimAndSettle(f, SUCCESS);
  assert.equal(probe.jobs.length, 1);
  assert.deepEqual(health(f.sql), {}, "the first success clears the ladder");
  assert.ok(!(blockedUntil(f.sql, "solo-a") > f.clock.value), "and the cooldown with it");
  const reset = f.logs.at(-1);
  assert.deepEqual([reset.action, reset.route_id, reset.rung, reset.previous_rung], ["reset", "solo-a", 0, 1]);
});

test("a cooling route holds its only model's jobs queued, then takes one half-open probe", async t => {
  const f = make(t);
  const t0 = f.clock.value;
  seedHealth(f.sql, { "solo-a": { timeout: 0, capacity: 2, defect: 0, rung: 1, until: t0 + 60_000, class: "capacity" } });
  await f.coordinator.enqueueBatch(["h1", "h2", "h3"].map(id => job(id, "solo/one")));

  f.clock.value = t0 + 1000;
  const cooling = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.equal(cooling.jobs.length, 0);
  assert.equal(cooling.claim_diagnostics.routes.route_health["solo-a"], "cooling");
  assert.deepEqual(["h1", "h2", "h3"].map(id => jobState(f.sql, id)), ["queued", "queued", "queued"]);

  f.clock.value = t0 + 61_000;
  const probe = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.equal(probe.jobs.length, 1, "half-open: one call, not a bundle's worth");
  f.clock.value += 1000;
  const held = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.equal(held.jobs.length, 0);
  assert.equal(held.claim_diagnostics.routes.route_health["solo-a"], "probe_in_flight");

  // The probe fails: one rung up (300 s), and the remaining jobs are still queued, not failed.
  await settle(f, probe, CAPACITY);
  const entry = health(f.sql)["solo-a"];
  assert.deepEqual([entry.rung, entry.until], [2, f.clock.value + 300_000]);
  assert.equal(f.logs.at(-1).previous_rung, 1);
  assert.equal(["h1", "h2", "h3"].filter(id => jobState(f.sql, id) === "failed").length, 0);
});

test("a cooling route's jobs go to its sibling route; admissible capacity excludes it", async t => {
  const f = make(t);
  const t0 = f.clock.value;
  seedHealth(f.sql, { "acme-a": { timeout: 1, capacity: 0, defect: 0, rung: 1, until: t0 + 60_000, class: "timeout" } });
  await f.coordinator.enqueueBatch(["s1", "s2"].map(id => job(id, "acme/pair")));
  const plan = await f.coordinator.claimDispatchWindow(t0, 25);
  assert.deepEqual(plan.jobs.map(planned => planned.route_id), ["beta-a", "beta-a"]);
  assert.equal(typeof plan.claim_diagnostics.admissible_calls, "number");

  // With nothing laddered every route counts toward admissible capacity.
  const g = make(t);
  await g.coordinator.enqueueBatch([job("s3", "acme/pair")]);
  const open = await g.coordinator.claimDispatchWindow(g.clock.value, 25);
  assert.ok(open.claim_diagnostics.admissible_calls >= 1);
});

test("one request defect fails its job without cooling the route; three in a row ladder it", async t => {
  const f = make(t);
  await f.coordinator.enqueueBatch([job("d1", "solo/one")]);
  await claimAndSettle(f, DEFECT);
  assert.equal(jobState(f.sql, "d1"), "failed", "a request defect stays job-terminal");
  assert.deepEqual([health(f.sql)["solo-a"].defect, health(f.sql)["solo-a"].rung], [1, 0]);
  assert.ok(!(blockedUntil(f.sql, "solo-a") > f.clock.value), "one defect does not cool the route");

  // A success between defects resets the count: defects must be consecutive.
  await f.coordinator.enqueueBatch([job("ok", "solo/one")]);
  f.clock.value += 1000;
  await claimAndSettle(f, SUCCESS);
  assert.deepEqual(health(f.sql), {});

  for (const id of ["d2", "d3", "d4"]) {
    await f.coordinator.enqueueBatch([job(id, "solo/one")]);
    f.clock.value += 1000;
    await claimAndSettle(f, DEFECT);
    assert.equal(jobState(f.sql, id), "failed");
  }
  const entry = health(f.sql)["solo-a"];
  assert.deepEqual([entry.defect, entry.rung, entry.class], [3, 1, "defect"]);
  assert.ok(blockedUntil(f.sql, "solo-a") >= f.clock.value + 60_000);

  // A held job waits: queued while the route cools.
  await f.coordinator.enqueueBatch([job("d5", "solo/one")]);
  f.clock.value += 1000;
  assert.equal((await f.coordinator.claimDispatchWindow(f.clock.value, 25)).jobs.length, 0);
  assert.equal(jobState(f.sql, "d5"), "queued");
});

test("pacing 429s, route pauses, rpd 0 and yields_to are untouched by the ladder", async t => {
  const f = make(t);
  await f.coordinator.enqueueBatch([job("p1", "solo/one")]);
  await claimAndSettle(f, { outcome: "retryable_error", failure_class: "own_rpm", provider_status_code: 429 });
  assert.deepEqual(health(f.sql), {}, "a pacing 429 is not route health");

  // A success clears the ladder but never a pause.
  seedHealth(f.sql, { "solo-a": { timeout: 1, capacity: 0, defect: 0, rung: 1, until: 0, class: "timeout" } });
  await f.coordinator.pauseDispatch({ scope: "route", target: "solo-a", seconds: 600 }, f.clock.value);
  f.clock.value += 120_000;
  const paused = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
  assert.equal(paused.jobs.length, 0, "a paused route stays paused");
  assert.equal([...f.sql.exec("SELECT COUNT(*) AS n FROM dispatch_pause WHERE scope = 'route:solo-a'")][0].n, 1);

  // rpd 0 and yields_to: the same catalog shapes as dispatch.test.js, with the ladder seeded on the
  // route being yielded to. The ladder neither revives an rpd-0 route nor lifts a yield.
  const catalog = {
    model_aliases: {},
    model_routes_map: { "x/jev": ["jev"], "x/pro": ["pro"], "x/off": ["off"] },
    routes_by_id: {
      jev: { model: "x/jev", provider: "bx", upstream_model: "jev", rpm: 1000, rpd: 100000, free: true,
        input_context_limit: 64000, output_context_limit: 4096 },
      pro: { model: "x/pro", provider: "bx", upstream_model: "pro", rpm: 1000, rpd: 100000, free: true,
        tier: "backup", yields_to: ["jev"], input_context_limit: 64000, output_context_limit: 4096 },
      off: { model: "x/off", provider: "ox", upstream_model: "off", rpm: 1000, rpd: 0, free: true,
        input_context_limit: 64000, output_context_limit: 4096 },
    },
  };
  for (const enabled of ["1", "0"]) {
    const g = make(t, { DISPATCH_LIMITS_OVERRIDE: catalog, ROUTE_HEALTH_LADDER_ENABLED: enabled });
    seedHealth(g.sql, { jev: { timeout: 1, capacity: 0, defect: 0, rung: 1, until: g.clock.value + 60_000, class: "timeout" } });
    await g.coordinator.enqueueBatch([job("pro-1", "x/pro"), job("jev-1", "x/jev"), job("off-1", "x/off")]);
    const plan = await g.coordinator.claimDispatchWindow(g.clock.value, 25);
    const routes = plan.jobs.map(planned => planned.route_id);
    assert.ok(!routes.includes("off"), `rpd 0 stays off (ladder ${enabled})`);
    assert.ok(!routes.includes("pro"), `the yield still holds (ladder ${enabled})`);
    assert.equal(routes.includes("jev"), enabled === "0", "only the ladder holds the cooling route");
    assert.ok(["pro-1", "off-1"].every(id => g.sql.exec("SELECT state FROM jobs WHERE id = ?", id)[0].state !== "failed"));
  }
});

test("switched off, the ladder records nothing and ignores state it wrote before", async t => {
  const f = make(t, { ROUTE_HEALTH_LADDER_ENABLED: "0" });
  seedHealth(f.sql, { "beta-a": { timeout: 1, capacity: 0, defect: 0, rung: 1, until: f.clock.value + 60_000, class: "timeout" } });
  await f.coordinator.enqueueBatch([job("o1", "acme/pair")]);
  const plan = await claimAndSettle(f, TIMEOUT);
  assert.equal(plan.claim_diagnostics.rejections.route_health, 0);
  assert.equal(Object.keys(health(f.sql)).length, 1, "unchanged: only the seeded entry");
  assert.equal(f.logs.length, 0);
});

test("old state: the column is added additively, and a corrupt value dispatches as healthy", async t => {
  const fixture = createRecordingSqlStorage();
  const f = make(t, {}, fixture);
  f.sql.exec("ALTER TABLE scheduler DROP COLUMN route_health_json");
  fixture.recorder.start();
  const g = f.remake();
  assert.deepEqual(fixture.recorder.statements.filter(({ query }) =>
    !/^(SELECT|PRAGMA|EXPLAIN)\b/i.test(query)).map(({ query }) => query),
  ["ALTER TABLE scheduler ADD COLUMN route_health_json TEXT NOT NULL DEFAULT '{}'"]);
  assert.deepEqual(health(f.sql), {});

  f.sql.exec("UPDATE scheduler SET route_health_json = '{not json' WHERE id = 1");
  await g.enqueueBatch([job("x1", "solo/one")]);
  const plan = await g.claimDispatchWindow(f.clock.value, 25);
  assert.equal(plan.jobs.length, 1);
  assert.equal(plan.claim_diagnostics.rejections.route_health, 0);
});

test("route stats show the rung from the authority read, with no extra statement", async t => {
  const fixture = createRecordingSqlStorage();
  const f = make(t, {}, fixture);
  await f.coordinator.enqueueBatch([job("st1", "solo/one")]);
  await claimAndSettle(f, TIMEOUT); // gives solo-a its route row
  const until = health(f.sql)["solo-a"].until;
  // A fresh instance each time, so the view's first (uncached) read is what is measured.
  const view = async () => {
    fixture.recorder.reset();
    fixture.recorder.start();
    const stats = await f.remake().routeInventoryStats(f.clock.value, 50);
    fixture.recorder.recording = false;
    return { stats, statements: [...fixture.recorder.statements] };
  };
  const laddered = await view();
  assert.equal(laddered.statements.filter(({ query }) => /route_health_json/.test(query)).length, 1,
    "the ladder rides the one authority SELECT");
  const solo = laddered.stats.routes.all.find(route => route.route_id === "solo-a");
  assert.deepEqual([solo.health_rung, solo.health_class, solo.health_until], [1, "timeout", until]);
  // With nothing laddered the view runs exactly the same statements.
  seedHealth(f.sql, {});
  const healthy = await view();
  assert.deepEqual(healthy.statements.map(({ query }) => query), laddered.statements.map(({ query }) => query));
  assert.equal(healthy.stats.routes.all.find(route => route.route_id === "solo-a").health_rung, 0);
});

test("row accounting: a healthy success costs nothing extra; an escalation one legacy row, zero packed", async t => {
  async function measure(enabled, result, packed = false) {
    const fixture = createRecordingSqlStorage();
    const f = make(t, { ROUTE_HEALTH_LADDER_ENABLED: enabled }, fixture);
    await f.coordinator.enqueueBatch([job("r1", "solo/one")]);
    if (packed) {
      // review/57 cutover, as packed-state.test.js does it: the production authority.
      await f.coordinator.pauseDispatch({ scope: "global", seconds: 3600 }, f.clock.value - 960_000);
      f.coordinator._transactionSync(() => f.coordinator._stagePackedCutover(f.clock.value, {
        maxJobs: 200, maxModelRows: 400, maxGroups: 200, maxProviders: 20, maxStateRows: 500,
        reserveOtherWrites: 1000,
      }));
      await f.coordinator.resumeDispatch({ scope: "global" }, f.clock.value);
      assert.equal((await f.coordinator.packedAuthority()).packed_state, true);
    }
    const plan = await f.coordinator.claimDispatchWindow(f.clock.value, 25);
    let written = 0;
    const exec = f.sql.exec;
    f.sql.exec = (...args) => {
      const cursor = exec(...args);
      written += Number(cursor.rowsWritten) || 0;
      return cursor;
    };
    await settle(f, plan, result);
    f.sql.exec = exec;
    return written;
  }
  assert.equal(await measure("1", SUCCESS), await measure("0", SUCCESS), "healthy success: +0 rows");
  const capacityDelta = await measure("1", CAPACITY) - await measure("0", CAPACITY);
  assert.equal(capacityDelta, 0, "a counted failure below its threshold rides the accounting row");
  const timeoutDelta = await measure("1", TIMEOUT) - await measure("0", TIMEOUT);
  assert.ok(timeoutDelta >= 0 && timeoutDelta <= 1, `an escalation adds at most one row (${timeoutDelta})`);
  // Legacy tables: the escalation's routes UPDATE is the one row. Packed (production): none.
  assert.equal(timeoutDelta, 1);
  assert.equal(await measure("1", SUCCESS, true), await measure("0", SUCCESS, true));
  assert.equal(await measure("1", TIMEOUT, true) - await measure("0", TIMEOUT, true), 0,
    "packed: an escalation rides the accounting row too");
});
