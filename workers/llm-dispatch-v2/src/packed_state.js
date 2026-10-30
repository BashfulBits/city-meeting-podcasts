/**
 * Packed scheduler state: the hot, small ledgers live as JSON on the scheduler singleton row and
 * ride the one accounting UPDATE every transaction already issues. Per-day telemetry that grows
 * with retention (attempt usage, failure counters) lives in a few route-hashed shard rows instead.
 *
 *   scheduler.packed_routes_json     route_id  -> routes row            (bounded by the catalog)
 *   scheduler.packed_estimates_json  key       -> estimates row         (bounded by routes x families)
 *   scheduler.packed_leases_json     job id    -> {bundle_id, lease_route_id, lease_expires_at}
 *   packed_telemetry (shard, json)   usage and failure cells, by hash of route_id
 *
 * There is exactly one authority per committed transaction (scheduler.packed_state_active). A
 * telemetry shard that outgrows its cap evicts its oldest days rather than failing a completion:
 * those counters are diagnostics, never admission inputs. Ledgers, estimates and leases fail
 * closed instead of dropping data.
 */

export const TELEMETRY_SHARDS = 8;
export const TELEMETRY_SHARD_MAX_BYTES = 256 * 1024;
export const PACKED_ROUTES_MAX_BYTES = 512 * 1024;
export const PACKED_ESTIMATES_MAX_BYTES = 512 * 1024;
export const PACKED_LEASES_MAX_BYTES = 128 * 1024;
export const PACKED_LEASES_MAX_ENTRIES = 1024;
/** Combined JSON the scheduler row may carry before cutover or a flush refuses to continue. */
export const SCHEDULER_ROW_MAX_BYTES = 1536 * 1024;

const encoder = new TextEncoder();
export const byteLength = text => encoder.encode(text).length;

const nullProto = () => Object.create(null);

/** Every column of the legacy `routes` table, with the value a freshly inserted row holds. */
export const ROUTE_DEFAULTS = Object.freeze({
  rpm_window_start: 0, rpm_count: 0, rpd_window_start: 0, rpd_count: 0, rpd_day_key: "",
  tpm_window_start: 0, tpm_reserved: 0, full_token_budget: 0, token_budget_updated_at: 0,
  provisional_reservation: 0, settled_usage: 0, cost_accumulated: 0, throttle_streak: 0,
  last_provider_status: null, blocked_until: null, buffer_seconds: 0, buffer_updated_at: 0,
  payment_required_streak: 0, upstream_capacity_streak: 0, last_failure_class: "",
  tpd_updated_at: 0, tpd_used: 0, prompt_cap_estimate: 0, input_window_json: "[]",
  input_accepted_max: 0, input_accepted_at: 0, input_rejected_min: 0, input_rejected_at: 0,
});
export const ROUTE_COLUMNS = Object.freeze(["route_id", ...Object.keys(ROUTE_DEFAULTS)]);

export function newRouteRow(routeId, seedBudget, now) {
  return { route_id: routeId, ...ROUTE_DEFAULTS, full_token_budget: seedBudget, token_budget_updated_at: now };
}

/**
 * One route-row change. Plain entries assign a column. `maxBlockedUntil` is the legacy
 * `blocked_until = MAX(COALESCE(blocked_until, 0), ?)`, and `releaseReservation` the legacy
 * `provisional_reservation = MAX(0, provisional_reservation - ?)`.
 */
export function applyRouteSet(row, set) {
  for (const [key, value] of Object.entries(set)) {
    if (key === "maxBlockedUntil") row.blocked_until = Math.max(row.blocked_until ?? 0, value);
    else if (key === "releaseReservation") {
      row.provisional_reservation = Math.max(0, (row.provisional_reservation ?? 0) - value);
    } else {
      if (!Object.hasOwn(ROUTE_DEFAULTS, key)) throw new Error(`unknown route column ${key}`);
      row[key] = value;
    }
  }
  return row;
}

/** The legacy SQL for the same change, with identical semantics to applyRouteSet. */
export function legacyRouteUpdate(routeId, set) {
  const assignments = [];
  const params = [];
  for (const [key, value] of Object.entries(set)) {
    if (key === "maxBlockedUntil") {
      assignments.push("blocked_until = MAX(COALESCE(blocked_until, 0), ?)");
    } else if (key === "releaseReservation") {
      assignments.push("provisional_reservation = MAX(0, provisional_reservation - ?)");
    } else {
      if (!Object.hasOwn(ROUTE_DEFAULTS, key)) throw new Error(`unknown route column ${key}`);
      assignments.push(`${key} = ?`);
    }
    params.push(value);
  }
  return { sql: `UPDATE routes SET ${assignments.join(", ")} WHERE route_id = ?`, params: [...params, routeId] };
}

function parseObject(text, label, maxBytes) {
  if (typeof text !== "string" || byteLength(text) > maxBytes) throw new Error(`invalid packed ${label} size`);
  const data = JSON.parse(text);
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(`invalid packed ${label}`);
  const out = nullProto();
  for (const [key, value] of Object.entries(data)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`invalid packed ${label} entry`);
    out[key] = value;
  }
  return out;
}

export function parsePackedRoutes(text) {
  const routes = parseObject(text, "routes", PACKED_ROUTES_MAX_BYTES);
  for (const [id, row] of Object.entries(routes)) {
    if (row.route_id !== id) throw new Error("invalid packed route ledger");
    for (const [key, fallback] of Object.entries(ROUTE_DEFAULTS)) {
      if (!Object.hasOwn(row, key)) throw new Error("invalid packed route ledger");
      const value = row[key];
      const ok = typeof fallback === "number" ? typeof value === "number" && Number.isFinite(value)
        : typeof fallback === "string" ? typeof value === "string"
          : value === null || (typeof value === "number" && Number.isFinite(value));
      if (!ok) throw new Error("invalid packed route ledger");
    }
  }
  return routes;
}

export function parsePackedEstimates(text) {
  const estimates = parseObject(text, "estimates", PACKED_ESTIMATES_MAX_BYTES);
  for (const [key, row] of Object.entries(estimates)) {
    if (row.key !== key || typeof row.margin_tokens !== "number" || typeof row.updated_at !== "number" ||
        typeof row.sample_count !== "number" ||
        (row.recent_observed_summary !== null && typeof row.recent_observed_summary !== "string")) {
      throw new Error("invalid packed estimate");
    }
  }
  return estimates;
}

export function parsePackedLeases(text) {
  const leases = parseObject(text, "leases", PACKED_LEASES_MAX_BYTES);
  if (Object.keys(leases).length > PACKED_LEASES_MAX_ENTRIES) throw new Error("packed lease registry too large");
  for (const lease of Object.values(leases)) {
    if (typeof lease.lease_route_id !== "string" || !Number.isFinite(lease.lease_expires_at) ||
        (lease.bundle_id !== null && typeof lease.bundle_id !== "string")) {
      throw new Error("invalid packed lease");
    }
  }
  return leases;
}

export function serializeBounded(value, label, maxBytes) {
  const text = JSON.stringify(value);
  if (byteLength(text) > maxBytes) throw new Error(`packed ${label} exceed safety limit`);
  return text;
}

/** Lease registry entry for a leased jobs row. */
export const leaseEntry = job => ({
  bundle_id: job.bundle_id ?? null, lease_route_id: job.lease_route_id, lease_expires_at: job.lease_expires_at,
});

// ---------------------------------------------------------------------------------------------
// Telemetry shards: attempt usage and failure counters.
// ---------------------------------------------------------------------------------------------

export const emptyShard = () => ({ usage: nullProto(), failures: nullProto() });
/** Stable FNV-1a bucket of a route id: the telemetry shard that holds its usage and failure cells. */
export function shardFor(routeId) {
  let hash = 2166136261;
  for (const byte of encoder.encode(String(routeId))) hash = Math.imul(hash ^ byte, 16777619) >>> 0;
  return hash % TELEMETRY_SHARDS;
}
export const usageKey = (day, purpose, routeId) => JSON.stringify([day, purpose, routeId]);
export const failureKey = (day, routeId, failureClass) => JSON.stringify([day, routeId, failureClass]);

export function parseShard(text) {
  if (typeof text !== "string" || byteLength(text) > TELEMETRY_SHARD_MAX_BYTES * 2) {
    throw new Error("invalid packed telemetry shard size");
  }
  const data = JSON.parse(text);
  if (!data || typeof data !== "object" || !data.usage || !data.failures ||
      typeof data.usage !== "object" || typeof data.failures !== "object") {
    throw new Error("invalid packed telemetry shard");
  }
  return { usage: Object.assign(nullProto(), data.usage), failures: Object.assign(nullProto(), data.failures) };
}

/** Fold a failure into one route/day/class cell using the legacy SQLite UPSERT semantics. */
export function foldFailureCell(previous, { now, routeId, failureClass, status, detail, count = 1, maxOutput }) {
  const day = new Date(now).toISOString().slice(0, 10);
  const sent = Number(detail?.sentMaxTokens);
  const output = Number(detail?.outputTokens);
  const nextSent = Number.isFinite(sent) && sent > 0 ? sent : null;
  const nextOutput = detail?.outputTokens != null && Number.isFinite(output) && output >= 0 ? output : null;
  const nextMax = maxOutput !== undefined ? maxOutput : nextOutput;
  return {
    utc_day: day, route_id: routeId, failure_class: failureClass,
    count: (previous?.count ?? 0) + count,
    last_status: status != null ? Number(status) : null,
    last_seen_at: now,
    last_max_tokens_sent: nextSent ?? previous?.last_max_tokens_sent ?? null,
    last_output_tokens: nextOutput ?? previous?.last_output_tokens ?? null,
    max_output_tokens: previous ? Math.max(previous.max_output_tokens ?? 0, nextMax ?? 0) : nextMax,
  };
}

/** Merge one completion's usage cell into the stored day cell (attempt_usage upsert semantics). */
export function foldUsageCell(previous, cell, day, sampleCap) {
  const append = (stored, added) => stored.concat(added.slice(0, Math.max(0, sampleCap - stored.length)));
  return {
    utc_day: day, purpose: cell.purpose, route_id: cell.route_id,
    calls: (previous?.calls || 0) + cell.calls,
    measured_calls: (previous?.measured_calls || 0) + cell.measured_calls,
    measured_input_calls: (previous?.measured_input_calls || 0) + cell.measured_input_calls,
    reserved_sum: (previous?.reserved_sum || 0) + cell.reserved_sum,
    over_reservation_calls: (previous?.over_reservation_calls || 0) + cell.over_reservation_calls,
    slow_calls: (previous?.slow_calls || 0) + cell.slow_calls,
    max_duration_ms: Math.max(previous?.max_duration_ms || 0, cell.max_duration_ms),
    outputs_json: JSON.stringify(append(JSON.parse(previous?.outputs_json || "[]"), cell.outputs)),
    input_ratios_json: JSON.stringify(append(JSON.parse(previous?.input_ratios_json || "[]"), cell.input_ratios)),
  };
}

/** Delete cells older than `cutoffDay`, oldest first, at most `limit` of them. Returns the counts. */
export function pruneShard(shard, cutoffDay, limit) {
  const result = { usage: 0, failures: 0 };
  const stale = [];
  for (const [kind, cells] of [["usage", shard.usage], ["failures", shard.failures]]) {
    for (const [key, cell] of Object.entries(cells)) if (cell.utc_day < cutoffDay) stale.push([cell.utc_day, kind, key]);
  }
  stale.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  for (const [, kind, key] of stale.slice(0, limit)) {
    delete shard[kind][key];
    result[kind] += 1;
  }
  return result;
}

/**
 * Keep a shard under its byte cap. Failure cells are dropped before usage cells, oldest day
 * first; today's cells are never evicted while older ones remain. Returns the serialized shard
 * and how many cells were evicted. If a single day alone exceeds the cap, the cap is enforced by
 * dropping that day's cells too: counters are diagnostics and must never abort a completion.
 */
export function serializeShard(shard, today, maxBytes = TELEMETRY_SHARD_MAX_BYTES) {
  let text = JSON.stringify(shard);
  let evicted = 0;
  if (byteLength(text) <= maxBytes) return { text, evicted };
  const order = [];
  for (const [kind, cells] of [["failures", shard.failures], ["usage", shard.usage]]) {
    for (const [key, cell] of Object.entries(cells)) {
      order.push({ kind, key, today: cell.utc_day === today ? 1 : 0, day: cell.utc_day, rank: kind === "failures" ? 0 : 1 });
    }
  }
  order.sort((a, b) => a.today - b.today || (a.day < b.day ? -1 : a.day > b.day ? 1 : 0) || a.rank - b.rank);
  for (const victim of order) {
    delete shard[victim.kind][victim.key];
    evicted += 1;
    text = JSON.stringify(shard);
    if (byteLength(text) <= maxBytes) break;
  }
  return { text, evicted };
}

// ---------------------------------------------------------------------------------------------
// Per-transaction view
// ---------------------------------------------------------------------------------------------

const SCHEDULER_NAMES = ["routes", "estimates", "leases"];
const PARSERS = { routes: parsePackedRoutes, estimates: parsePackedEstimates, leases: parsePackedLeases };
const LIMITS = { routes: PACKED_ROUTES_MAX_BYTES, estimates: PACKED_ESTIMATES_MAX_BYTES, leases: PACKED_LEASES_MAX_BYTES };

/**
 * The packed authority as one transaction sees it. Scheduler-row sections parse lazily on first
 * use and serialize only when changed; telemetry shards load per touched route hash.
 */
export class PackedTx {
  constructor(row, loadShardText) {
    this.raw = {
      routes: row.packed_routes_json, estimates: row.packed_estimates_json, leases: row.packed_leases_json,
    };
    this.parsed = {};
    this.dirty = new Set();
    this.shards = new Map();
    this.shardDirty = new Set();
    this.loadShardText = loadShardText;
  }

  section(name) {
    return (this.parsed[name] ??= PARSERS[name](this.raw[name]));
  }

  get routes() { return this.section("routes"); }
  get estimates() { return this.section("estimates"); }
  get leases() { return this.section("leases"); }

  markDirty(name) { this.dirty.add(name); }

  shard(index) {
    let shard = this.shards.get(index);
    if (!shard) {
      const text = this.loadShardText(index);
      shard = text == null ? emptyShard() : parseShard(text);
      this.shards.set(index, shard);
    }
    return shard;
  }

  markShardDirty(index) { this.shardDirty.add(index); }

  evictEstimates() {
    const estimates = this.parsed.estimates;
    if (byteLength(JSON.stringify(estimates)) <= PACKED_ESTIMATES_MAX_BYTES) return 0;
    let evicted = 0;
    const oldest = Object.values(estimates).sort((a, b) => a.updated_at - b.updated_at);
    for (const row of oldest) {
      delete estimates[row.key];
      evicted += 1;
      if (byteLength(JSON.stringify(estimates)) <= PACKED_ESTIMATES_MAX_BYTES) break;
    }
    this.estimatesEvicted = (this.estimatesEvicted || 0) + evicted;
    return evicted;
  }

  /** Scheduler-row assignments for every changed section, bounded against the row-size limit. */
  schedulerSets() {
    const sets = [];
    let total = 0;
    for (const name of SCHEDULER_NAMES) {
      if (this.dirty.has(name)) {
        // Estimates are calibration only: past the cap the stalest are dropped (the route falls
        // back to its prior) rather than failing the completion that happened to cross it.
        if (name === "estimates") this.evictEstimates();
        const text = serializeBounded(this.parsed[name], name, LIMITS[name]);
        this.raw[name] = text;
        sets.push({ assignment: `packed_${name}_json = ?`, params: [text] });
      }
      total += byteLength(this.raw[name]);
    }
    if (total > SCHEDULER_ROW_MAX_BYTES) throw new Error("packed scheduler state exceeds row safety limit");
    return sets;
  }

  /** [index, text] for each dirty telemetry shard, evicting old cells that exceed the cap. */
  shardWrites(today) {
    let evicted = 0;
    const writes = [...this.shardDirty].sort((a, b) => a - b).map(index => {
      const out = serializeShard(this.shards.get(index), today);
      evicted += out.evicted;
      return [index, out.text];
    });
    return { writes, evicted };
  }
}
