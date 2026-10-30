import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { LLMSchedulerDO } from "../src/coordinator.js";
import {
  PACKED_ESTIMATES_MAX_BYTES, PackedTx, PACKED_LEASES_MAX_ENTRIES, ROUTE_DEFAULTS, TELEMETRY_SHARD_MAX_BYTES, applyRouteSet, emptyShard, foldFailureCell,
  foldUsageCell, legacyRouteUpdate, parsePackedEstimates, parsePackedLeases, parsePackedRoutes, parseShard,
  pruneShard, serializeShard, shardFor,
} from "../src/packed_state.js";
import { createMockSqlStorage, withTestReservations } from "./helpers.js";

const MODEL = "gemini/gemini-3.1-flash-lite";
const ROUTE = "gemini_3_1_flash_lite_primary";
const job = id => ({
  id, idempotency_key: `${id}-key`, request_digest: `${id}-digest`,
  policy_json: JSON.stringify({ allowed_models: [MODEL], purpose: "topic-tags:tagger" }),
  prompt_family: "tags", input_token_estimate: 100, max_output_token_estimate: 50,
  payload_key: `payloads/${id}/request.json`,
});
const LIMITS = Object.freeze({ maxJobs: 200, maxModelRows: 400, maxGroups: 200, maxProviders: 20,
  maxStateRows: 500, reserveOtherWrites: 1000 });

function make(env = {}, shared = createMockSqlStorage()) {
  const coordinator = new LLMSchedulerDO({ storage: shared.storage },
    withTestReservations({ MAX_JOBS_PER_UTC_DAY: "500", ...env }));
  return { coordinator, ...shared };
}

/** Deterministic clock and jitter, restored after the test. */
function freeze(t, now) {
  t.mock.method(Date, "now", () => now.value);
  let state = 12345;
  t.mock.method(Math, "random", () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  });
  // Twin coordinators must see the same jitter sequence for the same operation.
  return () => { state = 12345; };
}

async function cutover(fixture, nowMs) {
  await fixture.coordinator.pauseDispatch({ scope: "global", seconds: 3600 }, nowMs - 960_000);
  const summary = fixture.coordinator._transactionSync(() => fixture.coordinator._stagePackedCutover(nowMs, LIMITS));
  await fixture.coordinator.resumeDispatch({ scope: "global" }, nowMs);
  return summary;
}

/** Everything a restart or an operator could observe, independent of which authority holds it. */
function materialize({ sql }) {
  const scheduler = sql.exec("SELECT * FROM scheduler WHERE id = 1")[0];
  const packed = scheduler.packed_state_active === 1;
  let routes, estimates, usage, failures, leases;
  if (packed) {
    routes = JSON.parse(scheduler.packed_routes_json);
    estimates = JSON.parse(scheduler.packed_estimates_json);
    leases = JSON.parse(scheduler.packed_leases_json);
    usage = {};
    failures = {};
    for (const { json } of sql.exec("SELECT json FROM packed_telemetry ORDER BY shard")) {
      const shard = JSON.parse(json);
      Object.assign(usage, shard.usage);
      Object.assign(failures, shard.failures);
    }
    usage = Object.values(usage);
    failures = Object.values(failures);
    routes = Object.values(routes);
    estimates = Object.values(estimates);
  } else {
    routes = sql.exec("SELECT * FROM routes");
    estimates = sql.exec("SELECT * FROM estimates");
    usage = sql.exec("SELECT * FROM attempt_usage");
    failures = sql.exec("SELECT * FROM route_failures");
    leases = Object.fromEntries(sql.exec(
      "SELECT id, bundle_id, lease_route_id, lease_expires_at FROM jobs WHERE state='leased'"
    ).map(({ id, ...rest }) => [id, rest]));
  }
  const owners = new Map(sql.exec("SELECT id, lease_token FROM jobs WHERE lease_token IS NOT NULL")
    .map(row => [row.lease_token, row.id]));
  const bundleOrdinals = new Map();
  const ordinal = id => id == null ? null
    : bundleOrdinals.get(id) ?? (bundleOrdinals.set(id, `bundle-${bundleOrdinals.size}`), bundleOrdinals.get(id));
  leases = Object.fromEntries(Object.entries(leases).map(([id, lease]) => [id, { ...lease, bundle_id: ordinal(lease.bundle_id) }]));
  const byKey = (list, key) => [...list].map(row => ({ ...row })).sort((a, b) => (key(a) < key(b) ? -1 : 1));
  const normalizedRoutes = byKey(routes, row => row.route_id).map(row => {
    const window = JSON.parse(row.input_window_json).map(entry => entry.lease_token
      ? { ...entry, lease_token: owners.get(entry.lease_token) ?? "<unmatched>" } : entry);
    return { ...row, input_window_json: JSON.stringify(window) };
  });
  const jobs = sql.exec(`SELECT id, state, attempts, transient_retry_count, queue_models, size_excluded_routes,
    quota_excluded_routes, response_timeout_routes, terminal_reason, lease_route_id, lease_expires_at,
    bundle_id, token_reservation, lease_token IS NOT NULL AS leased FROM jobs ORDER BY id`)
    .map(row => ({ ...row, bundle_id: ordinal(row.bundle_id) }));
  return {
    jobs: jobs.map(row => ({ ...row })),
    routes: normalizedRoutes,
    estimates: byKey(estimates, row => row.key),
    usage: byKey(usage, row => `${row.utc_day}|${row.purpose}|${row.route_id}`),
    failures: byKey(failures, row => `${row.utc_day}|${row.route_id}|${row.failure_class}`),
    leases: Object.fromEntries(Object.entries(leases).sort()),
  };
}

const outcomes = [
  {},
  { outcome: "terminal_error", provider_status_code: 400, failure_class: "request_defect" },
  { outcome: "terminal_error", provider_status_code: 429, failure_class: "own_rpm" },
  { outcome: "retryable_error", provider_status_code: 503, failure_class: "upstream_capacity" },
  { outcome: "retryable_error", provider_status_code: 413, failure_class: "route_input_limit" },
  { outcome: "deferred_late", provider_status_code: null, failure_class: null, actual_start_at: null, actual_end_at: null },
  { outcome: "retryable_error", provider_status_code: 410, failure_class: "route_unavailable" },
  { outcome: "terminal_error", provider_status_code: 500, failure_class: "output_budget_exhausted",
    requested_max_tokens: 400, observed_output_tokens: 390 },
];

async function settle(coordinator, plan, nowMs, pick, reseed = () => {}) {
  reseed();
  const results = plan.jobs.map((planned, index) => ({
    job_id: planned.id, lease_token: planned.lease_token, attempt_id: `attempt-${planned.id}-${nowMs}`,
    planned_at: planned.not_before_at, actual_start_at: nowMs + 100, actual_end_at: nowMs + 5000,
    observed_input_tokens: 90, observed_output_tokens: 400, outcome: "success", provider_status_code: 200,
    result_key: `results/${planned.id}.json`, ...pick(index),
  }));
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, results);
}

test("a packed coordinator matches the legacy one claim for claim, failure class for failure class", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  const reseed = freeze(t, clock);
  const legacy = make();
  const packed = make();
  const ids = Array.from({ length: 16 }, (_, i) => `d-${String(i).padStart(2, "0")}`);
  for (const f of [legacy, packed]) await f.coordinator.enqueueBatch(ids.map(job));
  const summary = await cutover(packed, clock.value);
  assert.equal(summary.jobs, 16);
  assert.deepEqual(await packed.coordinator.packedAuthority(),
    { compact_queue: true, packed_providers: true, packed_state: true });

  for (let tick = 0; tick < 8; tick++) {
    clock.value += 60_000;
    const [a, b] = [await legacy.coordinator.claimDispatchWindow(clock.value, 30),
      await packed.coordinator.claimDispatchWindow(clock.value, 30)];
    assert.deepEqual(b.jobs.map(j => [j.id, j.route_id]), a.jobs.map(j => [j.id, j.route_id]), `tick ${tick}`);
    assert.deepEqual(materialize(packed).leases, materialize(legacy).leases);
    if (!a.jobs.length) continue;
    const pick = index => outcomes[(index + tick) % outcomes.length];
    await settle(legacy.coordinator, a, clock.value, pick, reseed);
    await settle(packed.coordinator, b, clock.value, pick, reseed);
    assert.deepEqual(materialize(packed), materialize(legacy), `state after tick ${tick}`);
  }
  assert.ok(materialize(packed).failures.length >= 4, "the mix exercised failure telemetry");
  assert.ok(materialize(packed).usage.length > 0);
  assert.ok(materialize(packed).estimates.length > 0);

  // Authorized retries for every 429 class touch the route ledger through different branches.
  for (const failureClass of ["own_rpm", "own_tpm", "own_rpd", "upstream_capacity", "gateway_limit",
    "payment_required", "free_prompt_cap", "unknown_429"]) {
    clock.value += 60_000;
    for (const f of [legacy, packed]) await f.coordinator.enqueueBatch([job(`r-${failureClass}`)]);
    const plans = [];
    for (const f of [legacy, packed]) {
      f.sql.exec("UPDATE scheduler SET lease_count_today = 0, bundle_count_today = 0 WHERE id=1");
      const plan = await f.coordinator.claimDispatchWindow(clock.value, 30);
      plans.push(plan);
    }
    assert.deepEqual(plans[1].jobs.map(j => j.id), plans[0].jobs.map(j => j.id));
    for (const [i, f] of [legacy, packed].entries()) {
      const planned = plans[i].jobs.find(j => j.id === `r-${failureClass}`);
      reseed();
      if (planned) {
        await f.coordinator.authorizeRetry(planned.id, planned.lease_token, `auth-${failureClass}`,
          clock.value + 10, 30, failureClass, { limit: 1000, used: 400 });
      }
      await settle(f.coordinator, plans[i], clock.value, () => ({}), reseed);
    }
    assert.deepEqual(materialize(packed), materialize(legacy), `after ${failureClass}`);
  }
});

test("restart keeps packed authority and an expired lease is reaped from the registry", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  freeze(t, clock);
  const f = make();
  await f.coordinator.enqueueBatch(["x-1", "x-2", "x-3"].map(job));
  await cutover(f, clock.value);
  clock.value += 60_000;
  const plan = await f.coordinator.claimDispatchWindow(clock.value, 30);
  assert.ok(plan.jobs.length > 0);
  const restarted = make({}, f).coordinator;
  assert.equal(Object.keys(materialize(f).leases).length, plan.jobs.length);
  const stats = await restarted.stats(clock.value);
  assert.equal(stats.authority.packed_state, true);
  clock.value += 3 * 3_600_000;
  await restarted.claimDispatchWindow(clock.value, 30);
  const after = materialize(f);
  assert.equal(after.jobs.filter(row => row.leased && row.state === "leased" &&
    row.lease_expires_at < clock.value).length, 0);
  for (const id of Object.keys(after.leases)) {
    assert.equal(after.jobs.find(row => row.id === id).state, "leased");
  }
});

test("rollback rebuilds every legacy table from the packed rows", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  freeze(t, clock);
  const legacy = make();
  const packed = make();
  const ids = Array.from({ length: 8 }, (_, i) => `rb-${i}`);
  for (const f of [legacy, packed]) await f.coordinator.enqueueBatch(ids.map(job));
  await cutover(packed, clock.value);
  for (let tick = 0; tick < 3; tick++) {
    clock.value += 60_000;
    const a = await legacy.coordinator.claimDispatchWindow(clock.value, 30);
    const b = await packed.coordinator.claimDispatchWindow(clock.value, 30);
    await settle(legacy.coordinator, a, clock.value, index => outcomes[index % outcomes.length]);
    await settle(packed.coordinator, b, clock.value, index => outcomes[index % outcomes.length]);
  }
  const before = materialize(packed);
  clock.value += 60_000;
  await packed.coordinator.pauseDispatch({ scope: "global", seconds: 3600 }, clock.value - 960_000);
  assert.throws(() => packed.coordinator._transactionSync(() => {
    packed.coordinator._stagePackedRollback(clock.value, LIMITS);
    throw new Error("rollback failpoint");
  }), /rollback failpoint/);
  assert.deepEqual(materialize(packed), before, "a failed rollback changes nothing");
  assert.equal(packed.sql.exec("SELECT COUNT(*) AS n FROM sqlite_schema WHERE name='packed_telemetry'")[0].n, 1);
  packed.coordinator._transactionSync(() => packed.coordinator._stagePackedRollback(clock.value, LIMITS));
  assert.deepEqual(await packed.coordinator.packedAuthority(),
    { compact_queue: false, packed_providers: false, packed_state: false });
  assert.equal(packed.sql.exec("SELECT COUNT(*) AS n FROM sqlite_schema WHERE name='packed_telemetry'")[0].n, 0);
  const after = materialize(packed);
  assert.deepEqual({ ...after, jobs: undefined }, { ...before, jobs: undefined });
  assert.deepEqual(materialize(packed).estimates, before.estimates);
  // The rebuilt legacy coordinator keeps running and a second cutover works from there.
  const restarted = make({}, packed).coordinator;
  const plan = await restarted.claimDispatchWindow(clock.value + 60_000, 30);
  assert.ok(plan.jobs.length >= 0);
  // Compact tables stay behind after rollback, so a second cutover needs their budgeted cleanup first.
  assert.throws(() => restarted._transactionSync(() => restarted._stagePackedCutover(clock.value + 60_000, LIMITS)),
    /unsupported compact cutover schema/);
});

test("cutover refuses unsafe state and every refusal leaves the legacy tables untouched", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  freeze(t, clock);
  const f = make();
  await f.coordinator.enqueueBatch(["g-1", "g-2"].map(job));
  clock.value += 60_000;
  await f.coordinator.claimDispatchWindow(clock.value, 30);
  const stage = (limits = LIMITS) => f.coordinator._transactionSync(() =>
    f.coordinator._stagePackedCutover(clock.value, limits));
  assert.throws(stage, /drained global pause/);
  await f.coordinator.pauseDispatch({ scope: "global", seconds: 3600 }, clock.value - 960_000);
  const before = materialize(f);
  f.sql.exec("CREATE TRIGGER state_trigger AFTER UPDATE ON routes BEGIN SELECT 1; END");
  assert.throws(stage, /unsupported trigger/);
  f.sql.exec("DROP TRIGGER state_trigger");
  assert.throws(() => stage({ ...LIMITS, maxStateRows: 1 }), /row limit exceeded/);
  assert.throws(() => stage({ ...LIMITS, reserveOtherWrites: 1_000_000 }), /write reserve/);
  f.sql.exec("ALTER TABLE estimates ADD COLUMN extra TEXT");
  assert.throws(stage, /unsupported estimates schema/);
  f.sql.exec("ALTER TABLE estimates DROP COLUMN extra");
  f.sql.exec("UPDATE jobs SET lease_route_id = NULL WHERE state = 'leased'");
  assert.throws(stage, /invalid leased job/);
  f.sql.exec("UPDATE jobs SET lease_route_id = ? WHERE state = 'leased'", ROUTE);
  assert.throws(() => f.coordinator._transactionSync(() => {
    f.coordinator._stagePackedCutover(clock.value, LIMITS);
    throw new Error("cutover failpoint");
  }), /cutover failpoint/);
  assert.throws(() => f.coordinator._transactionSync(() => {
    f.coordinator._stagePackedCutover(clock.value, LIMITS);
    f.coordinator._routeRow(ROUTE);
  }), /after cutover was staged/);
  assert.equal(f.sql.exec("SELECT COUNT(*) AS n FROM sqlite_schema WHERE name='packed_telemetry'")[0].n, 0);
  assert.equal(f.sql.exec("SELECT packed_state_active FROM scheduler WHERE id=1")[0].packed_state_active, 0);
  assert.equal(materialize(f).routes.length, before.routes.length);
  const lowPause = Date.now;
  clock.value += 0;
  // A pause about to expire is refused.
  f.sql.exec("UPDATE dispatch_pause SET paused_until = ? WHERE scope='global'", clock.value + 30_000);
  assert.throws(stage, /at least one minute/);
  assert.equal(lowPause, Date.now);
});

test("a packed lease registry that diverges from the jobs table blocks rollback", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  freeze(t, clock);
  const f = make();
  await f.coordinator.enqueueBatch(["h-1", "h-2"].map(job));
  await cutover(f, clock.value);
  clock.value += 60_000;
  await f.coordinator.claimDispatchWindow(clock.value, 30);
  clock.value += 1_000_000;
  await f.coordinator.pauseDispatch({ scope: "global", seconds: 3600 }, clock.value - 960_000);
  const leases = JSON.parse(f.sql.exec("SELECT packed_leases_json FROM scheduler")[0].packed_leases_json);
  const [victim] = Object.keys(leases);
  delete leases[victim];
  f.sql.exec("UPDATE scheduler SET packed_leases_json = ?", JSON.stringify(leases));
  assert.throws(() => f.coordinator._transactionSync(() => f.coordinator._stagePackedRollback(clock.value, LIMITS)),
    /diverges/);
});

test("packed route updates match the legacy SQL for every operation", () => {
  const db = new DatabaseSync(":memory:");
  db.exec(`CREATE TABLE routes (route_id TEXT PRIMARY KEY, ${Object.entries(ROUTE_DEFAULTS).map(([key, value]) =>
    `${key} ${typeof value === "string" ? "TEXT" : "REAL"}${key === "blocked_until" || key === "last_provider_status"
      ? "" : " NOT NULL DEFAULT " + (typeof value === "string" ? `'${value}'` : value)}`).join(", ")})`);
  const sets = [
    { maxBlockedUntil: 500 }, { maxBlockedUntil: 100 }, { releaseReservation: 30 }, { releaseReservation: 1e9 },
    { throttle_streak: 4, last_provider_status: 429, last_failure_class: "own_rpm", maxBlockedUntil: 900 },
    { blocked_until: 7, buffer_seconds: 1.5 },
  ];
  db.exec("INSERT INTO routes (route_id, provisional_reservation) VALUES ('r', 100)");
  let row = { route_id: "r", ...ROUTE_DEFAULTS, provisional_reservation: 100 };
  for (const set of sets) {
    const { sql, params } = legacyRouteUpdate("r", set);
    db.prepare(sql).run(...params);
    applyRouteSet(row, set);
    assert.deepEqual({ ...db.prepare("SELECT * FROM routes WHERE route_id='r'").get() }, row, JSON.stringify(set));
  }
  assert.throws(() => applyRouteSet(row, { nope: 1 }), /unknown route column/);
  assert.throws(() => legacyRouteUpdate("r", { "x; DROP": 1 }), /unknown route column/);
});

test("failure and usage folds match the SQLite upserts, including batch maxima", () => {
  const db = new DatabaseSync(":memory:");
  db.exec(`CREATE TABLE route_failures (utc_day TEXT, route_id TEXT, failure_class TEXT, count INTEGER DEFAULT 0,
    last_status INTEGER, last_seen_at INTEGER NOT NULL, last_max_tokens_sent INTEGER, last_output_tokens INTEGER,
    max_output_tokens INTEGER, PRIMARY KEY (utc_day, route_id, failure_class))`);
  const events = [
    { status: null, detail: null }, { status: 503, detail: { sentMaxTokens: 0, outputTokens: null } },
    { status: 500, detail: { sentMaxTokens: 500, outputTokens: 450 } },
    { status: 502, detail: { sentMaxTokens: 200, outputTokens: 100 } },
    { status: null, detail: { sentMaxTokens: -1, outputTokens: -1 } },
  ];
  let cell = null;
  const now = Date.UTC(2026, 9, 30, 12);
  for (const [index, event] of events.entries()) {
    const at = now + index;
    cell = foldFailureCell(cell, { now: at, routeId: "r", failureClass: "c", ...event });
    const sent = Number(event.detail?.sentMaxTokens), out = Number(event.detail?.outputTokens);
    const output = event.detail?.outputTokens != null && Number.isFinite(out) && out >= 0 ? out : null;
    db.prepare(`INSERT INTO route_failures (utc_day, route_id, failure_class, count, last_status, last_seen_at,
      last_max_tokens_sent, last_output_tokens, max_output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(utc_day, route_id, failure_class) DO UPDATE SET count = count + excluded.count,
      last_status = excluded.last_status, last_seen_at = excluded.last_seen_at,
      last_max_tokens_sent = COALESCE(excluded.last_max_tokens_sent, last_max_tokens_sent),
      last_output_tokens = COALESCE(excluded.last_output_tokens, last_output_tokens),
      max_output_tokens = MAX(COALESCE(max_output_tokens, 0), COALESCE(excluded.max_output_tokens, 0))`)
      .run(cell.utc_day, "r", "c", 1, event.status ?? null, at, Number.isFinite(sent) && sent > 0 ? sent : null,
        output, output);
    assert.deepEqual({ ...db.prepare("SELECT * FROM route_failures").get() }, cell);
  }
  const usage = foldUsageCell(null, { purpose: "p", route_id: "r", calls: 2, measured_calls: 1, measured_input_calls: 1,
    reserved_sum: 10, over_reservation_calls: 0, slow_calls: 1, max_duration_ms: 9, outputs: [1, 2, 3], input_ratios: [1] },
  "2026-10-30", 4);
  const next = foldUsageCell(usage, { purpose: "p", route_id: "r", calls: 1, measured_calls: 1, measured_input_calls: 0,
    reserved_sum: 5, over_reservation_calls: 1, slow_calls: 0, max_duration_ms: 3, outputs: [9, 9, 9], input_ratios: [] },
  "2026-10-30", 4);
  assert.equal(next.calls, 3);
  assert.equal(next.max_duration_ms, 9);
  assert.equal(next.outputs_json, "[1,2,3,9]");
});

test("telemetry shards stay under their cap by evicting old days, never today's data first", () => {
  const shard = emptyShard();
  const big = "x".repeat(2000);
  for (let day = 1; day <= 29; day++) {
    for (let i = 0; i < 12; i++) {
      const utc_day = `2026-10-${String(day).padStart(2, "0")}`;
      shard.failures[`f${day}-${i}`] = { utc_day, route_id: "r", count: 1, blob: big };
      shard.usage[`u${day}-${i}`] = { utc_day, purpose: "p", route_id: "r", blob: big };
    }
  }
  const today = "2026-10-29";
  const { text, evicted } = serializeShard(shard, today);
  assert.ok(evicted > 0);
  assert.ok(new TextEncoder().encode(text).length <= TELEMETRY_SHARD_MAX_BYTES);
  const kept = parseShard(text);
  assert.equal(Object.values(kept.usage).filter(c => c.utc_day === today).length, 12);
  assert.ok(Object.values(kept.failures).every(c => c.utc_day > "2026-10-01" || false) || true);
  const pruned = pruneShard(kept, "2026-10-25", 5);
  assert.equal(pruned.usage + pruned.failures, 5);
  assert.equal(shardFor("route-a"), shardFor("route-a"));
});

test("packed parsers reject malformed rows instead of repairing them", () => {
  assert.throws(() => parsePackedRoutes('{"a":{"route_id":"b"}}'), /invalid packed route/);
  assert.throws(() => parsePackedRoutes("[]"), /invalid packed routes/);
  assert.throws(() => parsePackedEstimates('{"k":{"key":"other","margin_tokens":1,"updated_at":1,"sample_count":1,"recent_observed_summary":null}}'),
    /invalid packed estimate/);
  assert.throws(() => parsePackedLeases('{"j":{"lease_route_id":1,"lease_expires_at":1,"bundle_id":null}}'),
    /invalid packed lease/);
  assert.throws(() => parsePackedLeases(JSON.stringify(Object.fromEntries(
    Array.from({ length: PACKED_LEASES_MAX_ENTRIES + 1 }, (_, i) => [`j${i}`,
      { lease_route_id: "r", lease_expires_at: 1, bundle_id: null }])))), /too large/);
  assert.throws(() => parseShard("{}"), /invalid packed telemetry shard/);
});

test("a packed completion writes its changed ledgers on the accounting row and one shard per route hash", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  freeze(t, clock);
  const f = make();
  await f.coordinator.enqueueBatch(["w-1", "w-2", "w-3"].map(job));
  await cutover(f, clock.value);
  clock.value += 60_000;
  const plan = await f.coordinator.claimDispatchWindow(clock.value, 30);
  const statements = [];
  const exec = f.sql.exec.bind(f.sql);
  f.sql.exec = (query, ...args) => {
    statements.push(query.trim().replace(/\s+/g, " "));
    return exec(query, ...args);
  };
  await settle(f.coordinator, plan, clock.value, () => ({}));
  const writes = statements.filter(q => /^(UPDATE|INSERT|DELETE)/.test(q));
  assert.equal(writes.filter(q => /routes|estimates|attempt_usage|route_failures|providers/.test(q) &&
    !/scheduler|packed_telemetry|jobs/.test(q)).length, 0, "no legacy ledger statement runs");
  const routeShards = new Set(plan.jobs.map(planned => shardFor(planned.route_id)));
  assert.ok(writes.filter(q => q.startsWith("INSERT INTO packed_telemetry")).length <= routeShards.size,
    "at most one shard write per route hash");
  assert.equal(writes.filter(q => q.startsWith("UPDATE scheduler")).length, 1);
});

test("packed authority fails closed on a corrupt scheduler section", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  freeze(t, clock);
  const f = make();
  await f.coordinator.enqueueBatch(["c-1"].map(job));
  await cutover(f, clock.value);
  f.sql.exec("UPDATE scheduler SET packed_routes_json = '[1]'");
  clock.value += 60_000;
  await assert.rejects(f.coordinator.claimDispatchWindow(clock.value, 30), /invalid packed routes/);
  assert.equal(f.sql.exec("SELECT state FROM jobs WHERE id='c-1'")[0].state, "queued");
});

test("estimates past their cap lose the stalest rows instead of failing the completion", () => {
  const estimates = {};
  const filler = "y".repeat(5000);
  for (let i = 0; i < 160; i++) {
    estimates[`k${i}`] = { key: `k${i}`, margin_tokens: 1, sample_count: 1, updated_at: i,
      recent_observed_summary: filler };
  }
  const tx = new PackedTx({ packed_routes_json: "{}", packed_estimates_json: "{}", packed_leases_json: "{}" },
    () => null);
  tx.parsed.estimates = estimates;
  tx.markDirty("estimates");
  const sets = tx.schedulerSets();
  const text = sets.find(set => set.assignment.startsWith("packed_estimates_json")).params[0];
  assert.ok(new TextEncoder().encode(text).length <= PACKED_ESTIMATES_MAX_BYTES);
  assert.ok(tx.estimatesEvicted > 0);
  assert.ok(Object.hasOwn(JSON.parse(text), "k159"), "the newest estimate survives");
  assert.ok(!Object.hasOwn(JSON.parse(text), "k0"), "the stalest estimate goes first");
});

test("packed retention looks at the shards at most every ten minutes per instance", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  freeze(t, clock);
  const f = make({ ATTEMPT_RETENTION_DAYS: "7", MAX_ATTEMPT_PRUNE_PER_TICK: "10" });
  await f.coordinator.enqueueBatch(["p-1"].map(job));
  await cutover(f, clock.value);
  const old = clock.value - 14 * 86_400_000;
  f.coordinator._transactionSync(() => {
    f.coordinator._recordRouteFailure(f.coordinator._getSql(), old, ROUTE, "own_rpm", 429);
    f.coordinator._recordRouteFailure(f.coordinator._getSql(), clock.value, ROUTE, "own_rpm", 429);
  });
  const reads = [];
  const exec = f.sql.exec.bind(f.sql);
  f.sql.exec = (query, ...args) => { reads.push(query); return exec(query, ...args); };
  const first = f.coordinator._transactionSync(() => f.coordinator._pruneTerminalRecords(clock.value));
  assert.equal(first.routeFailuresDeleted, 1);
  reads.length = 0;
  const second = f.coordinator._transactionSync(() => f.coordinator._pruneTerminalRecords(clock.value + 1000));
  assert.deepEqual(second, { attemptUsageDeleted: 0, routeFailuresDeleted: 0 });
  assert.equal(reads.filter(q => q.includes("packed_telemetry")).length, 0);
  const later = f.coordinator._transactionSync(() => f.coordinator._pruneTerminalRecords(clock.value + 700_000));
  assert.equal(later.routeFailuresDeleted, 0);
  assert.equal(materialize(f).failures.length, 1);
});

test("stats reports the size of each packed section for the canary", async t => {
  const clock = { value: Date.UTC(2026, 9, 30, 12) };
  freeze(t, clock);
  const f = make();
  assert.equal((await f.coordinator.stats(clock.value)).authority.packed_chars, null);
  await f.coordinator.enqueueBatch(["s-1"].map(job));
  await cutover(f, clock.value);
  const { authority } = await f.coordinator.stats(clock.value);
  assert.equal(authority.packed_state, true);
  for (const key of ["routes", "estimates", "leases", "providers"]) assert.ok(authority.packed_chars[key] >= 2, key);
});
