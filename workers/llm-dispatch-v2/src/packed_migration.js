/**
 * Packed-state cutover and rollback. Both run inside ONE synchronous transaction while the global
 * dispatch pause is drained (see Coordinator._stagePackedCutover): the packed rows and the legacy
 * tables are never both authoritative, and a throw anywhere rolls the whole move back.
 *
 * Cutover moves routes, estimates, attempt usage, failure counters and the lease registry; rollback
 * rebuilds exactly those legacy tables from the packed rows. The compact queue and provider ledger
 * switch in the same transaction (coordinator).
 */
import {
  PACKED_LEASES_MAX_ENTRIES, ROUTE_COLUMNS, SCHEDULER_ROW_MAX_BYTES, TELEMETRY_SHARDS,
  TELEMETRY_SHARD_MAX_BYTES, byteLength, emptyShard, failureKey, parsePackedEstimates, parsePackedLeases,
  parsePackedRoutes, parseShard, serializeBounded, shardFor, usageKey,
  PACKED_ESTIMATES_MAX_BYTES, PACKED_LEASES_MAX_BYTES, PACKED_ROUTES_MAX_BYTES,
} from "./packed_state.js";

const ESTIMATE_COLUMNS = ["key", "margin_tokens", "sample_count", "recent_observed_summary", "updated_at"];
const USAGE_COLUMNS = ["utc_day", "purpose", "route_id", "calls", "measured_calls", "measured_input_calls",
  "reserved_sum", "over_reservation_calls", "slow_calls", "max_duration_ms", "outputs_json", "input_ratios_json"];
const FAILURE_COLUMNS = ["utc_day", "route_id", "failure_class", "count", "last_status", "last_seen_at",
  "last_max_tokens_sent", "last_output_tokens", "max_output_tokens"];

export const LEGACY_STATE_TABLES = Object.freeze({
  routes: ROUTE_COLUMNS,
  estimates: ESTIMATE_COLUMNS,
  attempt_usage: USAGE_COLUMNS,
  route_failures: FAILURE_COLUMNS,
});

const PACKED_TELEMETRY_DDL = "CREATE TABLE packed_telemetry (shard INTEGER PRIMARY KEY, json TEXT NOT NULL)";

function pick(row, columns) {
  const out = {};
  for (const column of columns) out[column] = row[column] === undefined ? null : row[column];
  return out;
}

/** Billed rows to write or delete one row of `table`: the row plus every index entry. */
export function rowWriteMultiplier(sql, table) {
  const [{ sql: ddl }] = [...sql.exec("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", table)];
  const indexes = [...sql.exec(`PRAGMA index_list(${table})`)];
  if (/WITHOUT\s+ROWID/i.test(String(ddl))) return 1 + indexes.filter(index => index.origin !== "pk").length;
  return 1 + indexes.length;
}

/** Legacy state tables must have exactly the expected columns and no triggers. */
export function assertLegacyStateSchema(sql) {
  const triggers = [...sql.exec(
    `SELECT name FROM sqlite_master WHERE type='trigger'
       AND tbl_name IN ('routes', 'estimates', 'attempt_usage', 'route_failures', 'packed_telemetry') LIMIT 1`
  )];
  if (triggers.length) throw new Error("unsupported trigger on packed-state source table");
  for (const [table, columns] of Object.entries(LEGACY_STATE_TABLES)) {
    const actual = [...sql.exec(`PRAGMA table_info(${table})`)].map(row => row.name).sort();
    if (JSON.stringify(actual) !== JSON.stringify([...columns].sort())) {
      throw new Error(`unsupported ${table} schema for packed-state migration`);
    }
  }
}

function readAll(sql, table, limit) {
  const rows = [...sql.exec(`SELECT * FROM ${table} LIMIT ?`, limit + 1)];
  if (rows.length > limit) throw new Error(`packed-state ${table} row limit exceeded`);
  return rows;
}

function buildShards(usage, failures) {
  const shards = Array.from({ length: TELEMETRY_SHARDS }, emptyShard);
  for (const row of usage) {
    shards[shardFor(row.route_id)].usage[usageKey(row.utc_day, row.purpose, row.route_id)] = pick(row, USAGE_COLUMNS);
  }
  for (const row of failures) {
    shards[shardFor(row.route_id)].failures[failureKey(row.utc_day, row.route_id, row.failure_class)] =
      pick(row, FAILURE_COLUMNS);
  }
  return shards.map(shard => {
    const text = JSON.stringify(shard);
    if (byteLength(text) > TELEMETRY_SHARD_MAX_BYTES) throw new Error("telemetry shard exceeds cutover limit");
    return text;
  });
}

/**
 * Read the legacy state and plan the move. `limits` = { maxRows, schedulerBytes }:
 * maxRows bounds each legacy table; schedulerBytes is the current scheduler row size excluding
 * the packed columns, so the combined row can be checked before anything is written.
 */
export function planPackedMigration(sql, { maxRows, schedulerBytes, maxWrites }) {
  if (!Number.isSafeInteger(maxRows) || maxRows < 1) throw new Error("invalid packed-state limits");
  assertLegacyStateSchema(sql);
  const routeRows = readAll(sql, "routes", maxRows);
  const estimateRows = readAll(sql, "estimates", maxRows);
  const usageRows = readAll(sql, "attempt_usage", maxRows);
  const failureRows = readAll(sql, "route_failures", maxRows);
  const leaseRows = [...sql.exec(
    "SELECT id, bundle_id, lease_route_id, lease_expires_at FROM jobs WHERE state = 'leased' LIMIT ?",
    PACKED_LEASES_MAX_ENTRIES + 1
  )];
  if (leaseRows.length > PACKED_LEASES_MAX_ENTRIES) throw new Error("too many active leases for packed migration");

  const routes = Object.create(null);
  for (const row of routeRows) routes[row.route_id] = pick(row, ROUTE_COLUMNS);
  const estimates = Object.create(null);
  for (const row of estimateRows) estimates[row.key] = pick(row, ESTIMATE_COLUMNS);
  const leases = Object.create(null);
  for (const row of leaseRows) {
    if (typeof row.lease_route_id !== "string" || !Number.isFinite(row.lease_expires_at)) {
      throw new Error("invalid leased job for packed migration");
    }
    leases[row.id] = { bundle_id: row.bundle_id ?? null, lease_route_id: row.lease_route_id,
      lease_expires_at: row.lease_expires_at };
  }
  const texts = {
    routes: serializeBounded(routes, "routes", PACKED_ROUTES_MAX_BYTES),
    estimates: serializeBounded(estimates, "estimates", PACKED_ESTIMATES_MAX_BYTES),
    leases: serializeBounded(leases, "leases", PACKED_LEASES_MAX_BYTES),
  };
  // Parse back through the same validators the coordinator uses at runtime.
  parsePackedRoutes(texts.routes);
  parsePackedEstimates(texts.estimates);
  parsePackedLeases(texts.leases);
  if (schedulerBytes + byteLength(texts.routes) + byteLength(texts.estimates) + byteLength(texts.leases) >
      SCHEDULER_ROW_MAX_BYTES) {
    throw new Error("scheduler row exceeds packed cutover safety limit");
  }
  const shards = buildShards(usageRows, failureRows);
  shards.forEach(parseShard);

  const counts = { routes: routeRows.length, estimates: estimateRows.length,
    attempt_usage: usageRows.length, route_failures: failureRows.length };
  let deleteWrites = 0;
  for (const [table, count] of Object.entries(counts)) {
    if (count > 0) deleteWrites += count * rowWriteMultiplier(sql, table);
  }
  // Table creation, one row per shard, and the legacy deletes. The scheduler flag rides the
  // accounting write the coordinator already reserves.
  const expectedWrites = 4 + TELEMETRY_SHARDS + deleteWrites;
  if (expectedWrites > maxWrites) throw new Error("packed-state write reserve unavailable");
  return { texts, shards, counts, leases: leaseRows.length, expectedWrites };
}

export function buildPackedMigration(sql, plan) {
  sql.exec(PACKED_TELEMETRY_DDL);
  plan.shards.forEach((text, index) => {
    sql.exec("INSERT INTO packed_telemetry (shard, json) VALUES (?, ?)", index, text);
  });
  for (const table of Object.keys(LEGACY_STATE_TABLES)) {
    if (plan.counts[table] > 0) sql.exec(`DELETE FROM ${table}`);
  }
}

/** Scheduler assignment that makes the packed rows authoritative. */
export function packedActivationSets(plan) {
  return {
    assignment: "packed_state_active = 1, packed_routes_json = ?, packed_estimates_json = ?, packed_leases_json = ?",
    params: [plan.texts.routes, plan.texts.estimates, plan.texts.leases],
  };
}

/** Rebuild the legacy tables from the packed rows. `scheduler` is the current scheduler row. */
export function planPackedRollback(sql, scheduler, { maxRows, maxWrites }) {
  if (!Number.isSafeInteger(maxRows) || maxRows < 1) throw new Error("invalid packed-state limits");
  assertLegacyStateSchema(sql);
  const routes = parsePackedRoutes(scheduler.packed_routes_json);
  const estimates = parsePackedEstimates(scheduler.packed_estimates_json);
  parsePackedLeases(scheduler.packed_leases_json);
  // Every active lease the packed registry holds must still match a leased job, or rollback would
  // silently change which calls the legacy queries believe are in flight.
  const registry = JSON.parse(scheduler.packed_leases_json);
  const leased = new Map([...sql.exec(
    "SELECT id, bundle_id, lease_route_id, lease_expires_at FROM jobs WHERE state = 'leased' LIMIT ?",
    PACKED_LEASES_MAX_ENTRIES + 1
  )].map(row => [row.id, row]));
  if (leased.size !== Object.keys(registry).length ||
      Object.entries(registry).some(([id, lease]) => {
        const job = leased.get(id);
        return !job || (job.bundle_id ?? null) !== lease.bundle_id || job.lease_route_id !== lease.lease_route_id ||
          job.lease_expires_at !== lease.lease_expires_at;
      })) {
    throw new Error("packed lease registry diverges from leased jobs");
  }
  const [{ n }] = [...sql.exec("SELECT COUNT(*) AS n FROM packed_telemetry")];
  if (n !== TELEMETRY_SHARDS) throw new Error("packed telemetry shards are incomplete");
  const usage = [];
  const failures = [];
  for (const { json } of sql.exec("SELECT json FROM packed_telemetry ORDER BY shard")) {
    const shard = parseShard(json);
    usage.push(...Object.values(shard.usage));
    failures.push(...Object.values(shard.failures));
  }
  const rows = { routes: Object.values(routes), estimates: Object.values(estimates),
    attempt_usage: usage, route_failures: failures };
  for (const [table, list] of Object.entries(rows)) {
    if (list.length > maxRows) throw new Error(`packed-state ${table} row limit exceeded`);
  }
  let expectedWrites = 4; // DROP TABLE and schema bookkeeping
  for (const [table, list] of Object.entries(rows)) {
    const legacyCount = [...sql.exec(`SELECT COUNT(*) AS n FROM ${table}`)][0].n;
    expectedWrites += (legacyCount + list.length) * rowWriteMultiplier(sql, table);
  }
  if (expectedWrites > maxWrites) throw new Error("packed-state rollback write reserve unavailable");
  return { rows, expectedWrites };
}

export function buildPackedRollback(sql, plan) {
  for (const [table, columns] of Object.entries(LEGACY_STATE_TABLES)) {
    sql.exec(`DELETE FROM ${table}`);
    const statement = `INSERT INTO ${table} (${columns.join(", ")}) VALUES (${columns.map(() => "?").join(", ")})`;
    for (const row of plan.rows[table]) sql.exec(statement, ...columns.map(column => row[column] ?? null));
  }
  sql.exec("DROP TABLE packed_telemetry");
}

export const PACKED_ROLLBACK_SETS = {
  assignment: "packed_state_active = 0, packed_routes_json = '{}', packed_estimates_json = '{}', packed_leases_json = '{}'",
  params: [],
};
