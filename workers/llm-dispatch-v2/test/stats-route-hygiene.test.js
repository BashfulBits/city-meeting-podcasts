// /v2/stats lists only what can be scheduled: claim diagnostics, scan cursors and model aliases
// leave out routes that are stood down (`rpd: 0`), deleted or paused, and a route that has not been
// called today reports no request count from a previous day.
import assert from "node:assert/strict";
import test from "node:test";

import { LLMSchedulerDO } from "../src/coordinator.js";
import { zonedDateKey } from "../src/pacing.js";
import { createMockSqlStorage, withTestReservations } from "./helpers.js";

const BASE = {
  free: true, rpm: 20, tpm: 100000, input_context_limit: 10000, output_context_limit: 10000,
};
const CATALOG = {
  providers: { pa: {}, pb: {}, pc: {} },
  // `old-held` resolves to a model whose only route is stood down; `older-live` reaches `live`
  // through a second alias.
  model_aliases: { "old-held": "held", "old-live": "live", "older-live": "old-live" },
  model_routes_map: {
    live: ["live-route"],
    held: ["held-route"],
    shared: ["shared-down", "shared-up"],
  },
  routes_by_id: {
    "live-route": { ...BASE, model: "live", provider: "pa", rpd: 1000, upstream_model: "live-up" },
    "held-route": { ...BASE, model: "held", provider: "pb", rpd: 0, upstream_model: "held-up" },
    "shared-down": { ...BASE, model: "shared", provider: "pc", rpd: 0, upstream_model: "shared-up-name" },
    "shared-up": { ...BASE, model: "shared", provider: "pc", rpd: 500, upstream_model: "shared-up-name" },
  },
};

function makeCoordinator() {
  const { sql, storage } = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO(
    { storage },
    withTestReservations({ MAX_JOBS_PER_UTC_DAY: "100", DISPATCH_LIMITS_OVERRIDE: CATALOG })
  );
  return { coordinator, sql };
}

const key = (createdAt) => ({ priority: 5, created_at: createdAt, job_id: `job-${createdAt}` });
const STORED_DIAGNOSTICS = {
  at: 1,
  rejections: { route_capacity: 4 },
  candidate_frontiers: { live: key(1), held: key(1794446835884), "deleted-model": key(2) },
  routes: {
    route_concurrency: { "live-route": 1, "held-route": 2 },
    route_capacity: { "live-route": { gates: {}, max_wait_ms: 0 }, "held-route": { gates: {}, max_wait_ms: 0 } },
    route_health: { "live-route": "cooling", "held-route": "cooling", "gone-route": "cooling" },
    provider_concurrency: { pa: 1, pb: 1 },
  },
};

function seedDiagnostics(sql, diagnostics = STORED_DIAGNOSTICS) {
  const json = JSON.stringify(diagnostics);
  sql.exec(
    "UPDATE scheduler SET last_empty_claim_diagnostics_json = ?, last_claim_diagnostics_json = ? WHERE id = 1",
    json, json
  );
}

for (const [label, options] of [["plain", {}], ["pending=1", { includePending: true }]]) {
  test(`/v2/stats (${label}) reports diagnostics only for routes that can be scheduled`, async () => {
    const { coordinator, sql } = makeCoordinator();
    seedDiagnostics(sql);
    const now = Date.now();

    const diag = (await coordinator.stats(now, options)).claim.last_empty_diagnostics;
    assert.deepEqual(Object.keys(diag.candidate_frontiers), ["live"], "stood-down and deleted models");
    assert.deepEqual(Object.keys(diag.routes.route_concurrency), ["live-route"]);
    assert.deepEqual(Object.keys(diag.routes.route_capacity), ["live-route"]);
    assert.deepEqual(Object.keys(diag.routes.route_health), ["live-route"], "stood-down and deleted routes");
    assert.deepEqual(Object.keys(diag.routes.provider_concurrency), ["pa"]);
    // Aggregate counts name no route and are kept as recorded.
    assert.deepEqual(diag.rejections, { route_capacity: 4 });
  });
}

test("an operator pause hides a route's diagnostics while it lasts, and resuming restores them", async () => {
  const { coordinator, sql } = makeCoordinator();
  seedDiagnostics(sql);
  const now = Date.now();
  await coordinator.pauseDispatch({ scope: "route", target: "live-route", seconds: 600 }, now);

  let diag = (await coordinator.stats(now)).claim.last_empty_diagnostics;
  assert.deepEqual(diag.candidate_frontiers, {});
  assert.deepEqual(diag.routes.route_capacity, {});
  assert.deepEqual(diag.routes.provider_concurrency, {});

  await coordinator.resumeDispatch({ scope: "route", target: "live-route" }, now);
  diag = (await coordinator.stats(now)).claim.last_empty_diagnostics;
  assert.deepEqual(Object.keys(diag.candidate_frontiers), ["live"]);
});

test("a claim drops the scan cursor of a model with nothing schedulable", async () => {
  const { coordinator, sql } = makeCoordinator();
  seedDiagnostics(sql);
  const now = Date.now();

  await coordinator.claimDispatchWindow(now, 30);
  assert.deepEqual([...coordinator._claimFrontier.keys()], ["live"], "held (stood down) is not tracked");
  const stored = JSON.parse(
    [...sql.exec("SELECT last_empty_claim_diagnostics_json AS j FROM scheduler WHERE id = 1")][0].j
  );
  assert.deepEqual(Object.keys(stored.candidate_frontiers), ["live"], "and is no longer persisted");

  // Pausing the live route leaves that model with nothing schedulable too.
  await coordinator.pauseDispatch({ scope: "provider", target: "pa", seconds: 600 }, now);
  await coordinator.claimDispatchWindow(now + 1, 30);
  assert.deepEqual([...coordinator._claimFrontier.keys()], []);
});

test("model_aliases lists only names that lead to a schedulable route", async () => {
  const { coordinator } = makeCoordinator();
  const now = Date.now();
  const aliases = (await coordinator.stats(now)).model_aliases;

  assert.equal(aliases["old-live"], "live");
  assert.equal(aliases["older-live"], "old-live", "an alias chain that ends at a live model stays");
  assert.equal(aliases["live-route"], "live");
  assert.equal(aliases["live-up"], "live");
  // The stood-down route's own names are gone, and so is the declared alias for its model.
  for (const name of ["old-held", "held-route", "held-up"]) {
    assert.equal(name in aliases, false, name);
  }
  // A model that still has a live route keeps the names it shares, and loses the dead route's id.
  assert.equal(aliases["shared-up"], "shared");
  assert.equal(aliases["shared-up-name"], "shared");
  assert.equal("shared-down" in aliases, false);

  // A probe pause is transient: the list follows the configuration, not the pause.
  await coordinator.pauseDispatch({ scope: "route", target: "live-route", seconds: 600 }, now);
  assert.deepEqual((await coordinator.stats(now)).model_aliases, aliases);
});

test("routes.all reports no request count from a previous day", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const today = zonedDateKey(now, "UTC");
  const add = (routeId, count, dayKey) => sql.exec(
    "INSERT INTO routes (route_id, rpd_count, rpd_day_key) VALUES (?, ?, ?)", routeId, count, dayKey
  );
  add("live-route", 7, today);
  add("held-route", 40, "2026-11-11"); // stood down since: never called today
  add("old-route-not-in-catalog", 12, "2026-11-01");

  for (const stats of [
    await coordinator.routeInventoryStats(now, 20),
    await coordinator.detailedStats(now, 20),
  ]) {
    const byId = Object.fromEntries(stats.routes.all.map((row) => [row.route_id, row]));
    assert.equal(byId["live-route"].rpd_count, 7, "today's count is reported as is");
    assert.equal(byId["held-route"].rpd_count, 0, "yesterday's count is not today's");
    assert.equal(byId["held-route"].rpd_day_key, "2026-11-11", "the ledger's own day key is kept");
    assert.equal(byId["old-route-not-in-catalog"].rpd_count, 0);
    assert.equal(byId["old-route-not-in-catalog"].in_catalog, false);
  }
});
