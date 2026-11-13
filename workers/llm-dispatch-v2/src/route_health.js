/**
 * Route health ladder (review/58 B3, WP3b).
 *
 * A route that keeps failing in the same way is stood down for longer each time, instead of being
 * re-picked on every tick. Three failure classes are tracked per route, each as a count of
 * consecutive failures with no intervening success on that route:
 *
 *   timeout   our response deadline, or a gateway 504/524 (provider_response_timeout)
 *   capacity  the provider's pool is full or its upstream failed (upstream_capacity, final 5xx)
 *   defect    request_defect, including a model the provider withdrew from the key (HTTP 400)
 *
 * A route enters the ladder when one class reaches its threshold. Each rung is a cooldown,
 * written as the route's existing `blocked_until`, so the claim skips it exactly as it skips any
 * other cooled-down route and jobs flow to other routes of their model or wait queued. When the
 * cooldown ends the route is half-open: it takes ONE call (no claim admits a second while that
 * call holds a lease). Success resets the ladder; any tracked failure on a half-open route moves
 * it one rung up; failures of calls already in flight while it cools do not climb further.
 *
 * Nothing here fails a job. A request defect still fails its own job (unchanged); the ladder only
 * stops further jobs being spent on a route whose defects are evidently the route's.
 *
 * State lives in one small JSON map on the scheduler row (`scheduler.route_health_json`), keyed by
 * route id, holding only routes that have failed since their last success. It is written by the
 * accounting UPDATE every completion already issues, so it adds no billed rows, and it is read by
 * the scheduler-authority SELECT every claim and completion already make, so it adds no reads.
 */

/** Code defaults; config/dispatch_tuning.yml carries the same values (tuning lockstep test). */
export const ROUTE_HEALTH_DEFAULTS = Object.freeze({
  ROUTE_HEALTH_LADDER_ENABLED: 1,
  ROUTE_HEALTH_TIMEOUT_THRESHOLD: 1,
  ROUTE_HEALTH_CAPACITY_THRESHOLD: 2,
  ROUTE_HEALTH_DEFECT_THRESHOLD: 3,
  ROUTE_HEALTH_RUNG_1_SECONDS: 60,
  ROUTE_HEALTH_RUNG_2_SECONDS: 300,
  ROUTE_HEALTH_RUNG_3_SECONDS: 900,
  ROUTE_HEALTH_RUNG_4_SECONDS: 3600,
});

export const HEALTH_CLASSES = Object.freeze(["timeout", "capacity", "defect"]);
const RUNG_NAMES = Object.freeze([
  "ROUTE_HEALTH_RUNG_1_SECONDS", "ROUTE_HEALTH_RUNG_2_SECONDS",
  "ROUTE_HEALTH_RUNG_3_SECONDS", "ROUTE_HEALTH_RUNG_4_SECONDS",
]);
/** The map holds failing routes only, bounded by the catalog; this is a corruption backstop. */
export const ROUTE_HEALTH_MAX_ENTRIES = 256;

/**
 * The ladder as configured. `envInt(name, fallback)` is the coordinator's tuning reader. Rungs are
 * read in order and stop at the first zero. No rung, or ENABLED 0, switches the ladder off; a
 * class threshold of 0 stops that class from entering the ladder.
 */
export function routeHealthConfig(envInt) {
  const read = name => envInt(name, ROUTE_HEALTH_DEFAULTS[name]);
  const rungsMs = [];
  for (const name of RUNG_NAMES) {
    const seconds = read(name);
    if (!(seconds > 0)) break;
    rungsMs.push(Math.floor(seconds) * 1000);
  }
  return {
    enabled: read("ROUTE_HEALTH_LADDER_ENABLED") > 0 && rungsMs.length > 0,
    thresholds: {
      timeout: Math.floor(read("ROUTE_HEALTH_TIMEOUT_THRESHOLD")),
      capacity: Math.floor(read("ROUTE_HEALTH_CAPACITY_THRESHOLD")),
      defect: Math.floor(read("ROUTE_HEALTH_DEFECT_THRESHOLD")),
    },
    rungsMs,
  };
}

const isCount = value => Number.isSafeInteger(value) && value >= 0;

/**
 * Parse the stored map. Absent, empty or unreadable state reads as "every route healthy": the
 * ladder is advisory, so a bad value must never stop dispatch (the route cooldowns it wrote stay
 * on the route rows and expire on their own). Malformed entries are dropped individually.
 */
export function parseRouteHealth(text) {
  const out = Object.create(null);
  if (typeof text !== "string" || text === "") return out;
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return out;
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return out;
  for (const [routeId, entry] of Object.entries(data).slice(0, ROUTE_HEALTH_MAX_ENTRIES)) {
    if (!entry || typeof entry !== "object") continue;
    if (!HEALTH_CLASSES.every(cls => isCount(entry[cls])) || !isCount(entry.rung) ||
        !isCount(entry.until)) continue;
    out[routeId] = {
      timeout: entry.timeout, capacity: entry.capacity, defect: entry.defect,
      rung: entry.rung, until: entry.until,
      class: HEALTH_CLASSES.includes(entry.class) ? entry.class : "",
    };
  }
  return out;
}

export function serializeRouteHealth(map) {
  return JSON.stringify(map);
}

/**
 * The ladder class of one settled provider attempt, or null when the ladder does not track it.
 * `flags` are the coordinator's own classifications of the result (see completeBatch).
 */
export function healthClassFor({ isResponseTimeout, isCapacity, isDefect }) {
  if (isResponseTimeout) return "timeout";
  if (isCapacity) return "capacity";
  if (isDefect) return "defect";
  return null;
}

const blankEntry = () => ({ timeout: 0, capacity: 0, defect: 0, rung: 0, until: 0, class: "" });

/**
 * Record one failure of class `cls` on `routeId`. Mutates `map`. Returns `{ entry, escalated }`;
 * `escalated` is true when the route moved up a rung (or re-armed the top rung), and `entry.until`
 * is then its new cooldown end. Escalates when a healthy route's class count reaches its
 * threshold, or when a route on the ladder fails a call that started after its cooldown ended
 * (its half-open probe). A call that started before then -- in flight when the cooldown began,
 * however late it finishes -- only counts. `startedAt` is the attempt's start (defaults to `now`).
 */
export function recordRouteHealthFailure(map, routeId, cls, now, config, startedAt = now) {
  if (!config.enabled || !HEALTH_CLASSES.includes(cls) || !(config.thresholds[cls] > 0)) {
    return { entry: map[routeId] || null, escalated: false, changed: false };
  }
  if (!map[routeId] && Object.keys(map).length >= ROUTE_HEALTH_MAX_ENTRIES) {
    return { entry: null, escalated: false, changed: false };
  }
  const entry = { ...blankEntry(), ...(map[routeId] || {}) };
  const previousRung = entry.rung;
  entry[cls] += 1;
  const started = Number.isFinite(startedAt) ? Math.min(startedAt, now) : now;
  const escalated = entry.rung > 0
    ? started >= entry.until
    : entry[cls] >= config.thresholds[cls];
  if (escalated) {
    entry.rung = Math.min(entry.rung + 1, config.rungsMs.length);
    entry.until = now + config.rungsMs[entry.rung - 1];
    entry.class = cls;
  }
  map[routeId] = entry;
  return { entry, escalated, changed: true, previousRung };
}

/** Record a success on `routeId`: forget its ladder state. Returns the entry it cleared, or null. */
export function recordRouteHealthSuccess(map, routeId) {
  const entry = map[routeId];
  if (!entry) return null;
  delete map[routeId];
  return entry;
}

/**
 * Why the claim must skip this route now, or null. "cooling" while a ladder cooldown runs (the
 * route's blocked_until says the same; this keeps the ladder self-contained), "probe_in_flight"
 * once it has ended but the route already holds a call -- the half-open state admits one.
 */
export function routeHealthGate(entry, now, inFlight) {
  if (!entry || !(entry.rung > 0)) return null;
  if (now < entry.until) return "cooling";
  return inFlight > 0 ? "probe_in_flight" : null;
}

/** Structured log line for a rung change or a reset (Workers Logs; no DO rows). */
export function routeHealthEvent(action, routeId, entry, now, extra = {}) {
  return {
    event: "route_health",
    action,
    route_id: routeId,
    health_class: entry.class || null,
    rung: action === "reset" ? 0 : entry.rung,
    // The rung before this change; an escalation overrides it (a capped route stays on its rung).
    previous_rung: entry.rung,
    cooldown_seconds: action === "reset" ? 0 : Math.max(0, Math.round((entry.until - now) / 1000)),
    cooldown_until: action === "reset" ? null : entry.until,
    consecutive_failures: action === "reset" ? 0 : entry[entry.class] ?? 0,
    consecutive: { timeout: entry.timeout, capacity: entry.capacity, defect: entry.defect },
    ...extra,
  };
}
