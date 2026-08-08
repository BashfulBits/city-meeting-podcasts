import DISPATCH_LIMITS from "./dispatch_limits.json" with { type: "json" };

const REQUEST_PREFIX = "requests/";
const CRON_LOCK_KEY = "locks/cron.json";
const DISPATCH_BUDGET_KEY = "state/dispatch_budget.json";
const DEFAULT_MAX_REQUEST_BYTES = 512 * 1024;
const DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const DEFAULT_MAX_QUEUE_SCAN = 1000;
const DEFAULT_MAX_ATTEMPTS = 5;
const DEFAULT_PROCESSING_TIMEOUT_SECONDS = 15 * 60;
const DEFAULT_RETRY_BASE_SECONDS = 60;
const DEFAULT_RETRY_MAX_SECONDS = 60 * 60;
// The cron lease bounds how long one invocation holds exclusive dispatch rights before a dead/
// stuck invocation stops blocking new attempts. The upstream fetch timeout must stay comfortably
// under this, or a normal-speed call that simply runs long can have its lease stolen mid-flight by
// the next tick while it's still in-flight upstream -- the same claimed-but-still-pending record
// then gets claimed and dispatched a second time (review/41 / CodeRabbit).
const DEFAULT_LEASE_DURATION_SECONDS = 90;
const DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 60;

const COPY_FIELDS = [
  "temperature",
  "top_p",
  "max_tokens",
  "max_completion_tokens",
  "response_format",
  "tools",
  "tool_choice",
  "seed",
  "presence_penalty",
  "frequency_penalty",
];

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

class BodyTooLargeError extends Error {}

function jsonResponse(value, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "application/json; charset=utf-8",
      "x-content-type-options": "nosniff",
      ...extraHeaders,
    },
  });
}

function plain(status, message, extraHeaders = {}) {
  return new Response(`${message}\n`, {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "text/plain; charset=utf-8",
      "x-content-type-options": "nosniff",
      ...extraHeaders,
    },
  });
}

function positiveNumber(value, fallback, { integer = false } = {}) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return integer ? Math.max(1, Math.floor(parsed)) : parsed;
}

function requiredString(value, name) {
  const result = String(value || "").trim();
  if (!result) {
    throw new Error(`${name} is required`);
  }
  return result;
}

function modelName(model) {
  return model.split("/").pop() || model;
}

// `PROVIDER_NAME`/`UPSTREAM_MODEL`/`MODEL_ID` describe the Worker's own advertised default route
// (used by `GET /v1/models` and as the fallback when a request omits `model`). They are no longer
// used to resolve *credentials* or an upstream URL for a real dispatch -- that is entirely
// `DISPATCH_LIMITS.providers[route.provider]`'s job now (`resolveProviderCredentials` below), keyed
// by the route the ranked-selection step in `dispatchOne` actually chose. Deliberately no
// fallback default here: a missing var fails the deploy instead of silently resolving to whatever
// provider happened to be configured last (the credential-disclosure bug this replaced -- a Gemini
// request's key reaching `api.mistral.ai`, review/41).
function config(env) {
  const provider = requiredString(env.PROVIDER_NAME, "PROVIDER_NAME");
  const upstreamModel = requiredString(env.UPSTREAM_MODEL, "UPSTREAM_MODEL");
  const model = String(env.MODEL_ID || `${provider}/${modelName(upstreamModel)}`).trim();

  return {
    provider,
    model,
    upstreamModel,
    maxRequestBytes: positiveNumber(env.MAX_REQUEST_BYTES, DEFAULT_MAX_REQUEST_BYTES, {
      integer: true,
    }),
    maxResponseBytes: positiveNumber(env.MAX_RESPONSE_BYTES, DEFAULT_MAX_RESPONSE_BYTES, {
      integer: true,
    }),
    maxQueueScan: Math.min(
      1000,
      positiveNumber(env.MAX_QUEUE_SCAN, DEFAULT_MAX_QUEUE_SCAN, { integer: true }),
    ),
    maxAttempts: positiveNumber(env.MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS, { integer: true }),
    processingTimeoutSeconds: positiveNumber(
      env.PROCESSING_TIMEOUT_SECONDS,
      DEFAULT_PROCESSING_TIMEOUT_SECONDS,
    ),
    retryBaseSeconds: positiveNumber(env.RETRY_BASE_SECONDS, DEFAULT_RETRY_BASE_SECONDS),
    retryMaxSeconds: positiveNumber(env.RETRY_MAX_SECONDS, DEFAULT_RETRY_MAX_SECONDS),
    ...leaseAndTimeoutConfig(env),
  };
}

/** `upstreamTimeoutSeconds` must stay below `leaseDurationSeconds`, or a normal-speed call that
 * simply runs long can have its lease stolen mid-flight by the next tick and get dispatched a
 * second time for the same still-pending record. Both are independently overridable via env, so
 * this was previously only a comment -- CodeRabbit correctly flagged that as unenforced (review/41):
 * one env tweak could silently reinstate the exact double-dispatch failure mode it warns about. */
function leaseAndTimeoutConfig(env) {
  const leaseDurationSeconds = positiveNumber(
    env.LEASE_DURATION_SECONDS,
    DEFAULT_LEASE_DURATION_SECONDS,
  );
  const upstreamTimeoutSeconds = positiveNumber(
    env.UPSTREAM_TIMEOUT_SECONDS,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
  );
  if (upstreamTimeoutSeconds >= leaseDurationSeconds) {
    throw new Error(
      `UPSTREAM_TIMEOUT_SECONDS (${upstreamTimeoutSeconds}) must be less than ` +
        `LEASE_DURATION_SECONDS (${leaseDurationSeconds})`,
    );
  }
  return { leaseDurationSeconds, upstreamTimeoutSeconds };
}

async function hasValidBearer(request, env) {
  const expected = String(env.DISPATCH_AUTH_TOKEN || "");
  const authorization = request.headers.get("authorization") || "";
  if (!expected || !authorization.startsWith("Bearer ")) {
    return false;
  }

  const encoder = new TextEncoder();
  const supplied = authorization.slice("Bearer ".length);
  const [expectedHash, suppliedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
    crypto.subtle.digest("SHA-256", encoder.encode(supplied)),
  ]);
  const left = new Uint8Array(expectedHash);
  const right = new Uint8Array(suppliedHash);
  let difference = left.length ^ right.length;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ (right[index] || 0);
  }
  return difference === 0;
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function readTextLimited(stream, limit) {
  if (!stream) {
    return "";
  }
  const reader = stream.getReader();
  const chunks = [];
  let bytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      bytes += chunk.byteLength;
      if (bytes > limit) {
        await reader.cancel();
        throw new BodyTooLargeError();
      }
      chunks.push(chunk);
    }
  } finally {
    reader.releaseLock();
  }

  const result = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(result);
}

async function readJsonBody(request, limit) {
  const contentLength = Number(request.headers.get("content-length"));
  if (Number.isFinite(contentLength) && contentLength > limit) {
    throw new BodyTooLargeError();
  }
  const text = await readTextLimited(request.body, limit);
  try {
    return JSON.parse(text);
  } catch {
    throw new HttpError(400, "request body must be valid JSON");
  }
}

function requestKey(id) {
  return `requests/${id}.json`;
}

async function getJson(bucket, key) {
  const object = await bucket.get(key);
  if (!object) {
    return null;
  }
  return { object, etag: object.etag, value: await object.json() };
}

/** `status`/`model`/`available_at` mirrored into R2 `customMetadata` for every request record --
 * `bucket.list()` already returns `customMetadata` per object, so both the cron claim scan and
 * `/v1/queue/estimate` can filter from the listing alone and reserve a real `getJson` body fetch
 * for the one record they actually go on to process, instead of fetching every listed object's
 * body just to inspect its status (CodeRabbit, review/41). */
function requestMetadata(record) {
  return {
    status: String(record.status || ""),
    model: String(record.model || ""),
    available_at: String(record.available_at || ""),
  };
}

async function putJson(bucket, key, value, { etagMatches, onlyIf, customMetadata } = {}) {
  const body = JSON.stringify(value);
  const options = {};
  if (customMetadata) {
    options.customMetadata = customMetadata;
  }
  if (onlyIf) {
    options.onlyIf = onlyIf;
  } else if (etagMatches !== undefined) {
    options.onlyIf = { etagMatches };
  }
  return bucket.put(key, body, options);
}

function normalizeChatRequest(body, cfg) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new HttpError(400, "request body must be a JSON object");
  }
  const rawModel = String(body.model || cfg?.model || "").trim();
  if (!rawModel) {
    throw new HttpError(400, "model is required");
  }
  if (body.stream === true) {
    throw new HttpError(400, "streaming is not supported");
  }

  // A request naming the Worker's own bare `UPSTREAM_MODEL` string (e.g. "mistral-large-2512",
  // not the canonical "mistral/mistral-large-2512") is accepted as a convenience alias for the
  // Worker's default route -- but it must be *canonicalized* to that route's model here, not
  // stored as-is: the bare string has no entry in `model_routes_map`, so a record persisted under
  // it would always resolve to "no_configured_route" at dispatch time and fail permanently no
  // matter how many times it's retried (CodeRabbit, review/41).
  const requestedModel = cfg && rawModel === cfg.upstreamModel ? cfg.model : rawModel;
  if (cfg && requestedModel !== cfg.model) {
    const allowedModels = DISPATCH_LIMITS.model_routes_map || {};
    if (!allowedModels[requestedModel]) {
      throw new HttpError(400, `request model must match ${cfg.model}`);
    }
  }

  const messages = body.messages;
  if (!Array.isArray(messages) || messages.length === 0) {
    throw new HttpError(400, "messages must be a non-empty array");
  }

  // Store the *canonical* requested model, never a provider-specific upstream string -- which
  // physical route/account actually serves this request isn't decided until `dispatchOne` ranks
  // candidates against live ledger state, so nothing at enqueue time can know it yet. `dispatchOne`
  // substitutes the resolved route's own `upstream_model` when it builds the real upstream payload.
  // Storing an upstream-shaped value here was the CodeRabbit-flagged bug: it happened to be
  // harmless only because `dispatchOne` already overwrote it before this fix, and would have
  // become load-bearing (and wrong) the moment the credential-routing fix landed (review/41).
  const payload = {
    model: requestedModel,
    messages,
    stream: false,
  };

  for (const field of COPY_FIELDS) {
    if (body[field] !== undefined) {
      payload[field] = body[field];
    }
  }

  return {
    model: requestedModel,
    payload,
    estimated_tokens: positiveNumber(body.estimated_tokens, 1024, { integer: true }),
    allow_paid: Boolean(body.allow_paid),
    allow_batch: body.allow_batch !== false,
    deadline_at: body.deadline_at ? String(body.deadline_at) : null,
    submit_next: Boolean(body.submit_next),
  };
}

async function enqueue(bucket, normalized, cfg, now, idempotencyKey) {
  let requestId;
  if (idempotencyKey) {
    const hash = await sha256Hex(idempotencyKey);
    requestId = `chatcmpl-${hash.slice(0, 32)}`;
  } else {
    const raw = crypto.randomUUID().replace(/-/g, "");
    requestId = `chatcmpl-${raw}`;
  }

  const key = requestKey(requestId);
  const existing = await getJson(bucket, key);
  if (existing) {
    const existingReq = existing.value.request;
    const existingPolicy = existing.value.policy || {};
    const incomingPolicy = {
      estimated_tokens: normalized.estimated_tokens,
      allow_paid: normalized.allow_paid,
      allow_batch: normalized.allow_batch,
      deadline_at: normalized.deadline_at,
      submit_next: normalized.submit_next,
    };
    if (
      JSON.stringify(existingReq) !== JSON.stringify(normalized.payload) ||
      JSON.stringify(existingPolicy) !== JSON.stringify(incomingPolicy)
    ) {
      throw new HttpError(409, "Idempotency key collision with different request payload");
    }
    return existing.value;
  }

  const record = {
    schema: 1,
    id: requestId,
    status: "pending",
    provider: cfg ? cfg.provider : normalized.model.split("/")[0],
    model: normalized.model,
    request: normalized.payload,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
    available_at: now.toISOString(),
    attempts: 0,
    policy: {
      estimated_tokens: normalized.estimated_tokens,
      allow_paid: normalized.allow_paid,
      allow_batch: normalized.allow_batch,
      deadline_at: normalized.deadline_at,
      submit_next: normalized.submit_next,
    },
  };

  const saved = await putJson(bucket, key, record, {
    onlyIf: { etagDoesNotMatch: "*" },
    customMetadata: requestMetadata(record),
  });
  if (!saved) {
    const reloaded = await getJson(bucket, key);
    if (!reloaded) {
      // The conditional create lost the race (someone else is writing this exact idempotency
      // key right now) *and* the reload came up empty -- returning the in-memory `record` here
      // would answer 202 with a request id that was never actually persisted; the caller would
      // poll it forever and get 404 (CodeRabbit, review/41). Ask the caller to retry instead of
      // silently losing the request.
      throw new HttpError(503, "request could not be queued, retry");
    }
    return reloaded.value;
  }
  return record;
}

function parseTime(isoString) {
  return new Date(isoString).getTime();
}

function retryableStatus(status) {
  return status === 429 || status === 500 || status === 502 || status === 503 || status === 504;
}

function retryDelaySeconds(response, attempts, cfg) {
  const retryAfter = response ? Number(response.headers?.get?.("retry-after") || response.headers?.["retry-after"]) : NaN;
  if (Number.isFinite(retryAfter) && retryAfter > 0) {
    return Math.min(retryAfter, cfg.retryMaxSeconds);
  }
  const exponential = cfg.retryBaseSeconds * Math.pow(2, Math.max(0, attempts - 1));
  return Math.min(exponential, cfg.retryMaxSeconds);
}

async function saveRetry(bucket, claimed, response, cfg, now) {
  const delay = retryDelaySeconds(response, claimed.record.attempts, cfg);
  const availableAt = new Date(now.getTime() + delay * 1000).toISOString();
  const updated = {
    ...claimed.record,
    status: "pending",
    updated_at: now.toISOString(),
    available_at: availableAt,
    processing_started_at: undefined,
    last_error: {
      status: response ? response.status : 0,
      timestamp: now.toISOString(),
    },
  };
  await putJson(bucket, claimed.key, updated, {
    etagMatches: claimed.object.etag,
    customMetadata: requestMetadata(updated),
  });
}

async function saveFailure(bucket, claimed, status, now, code = "upstream_error") {
  const updated = {
    ...claimed.record,
    status: "failed",
    updated_at: now.toISOString(),
    completed_at: now.toISOString(),
    error: {
      code,
      status: status || 502,
      timestamp: now.toISOString(),
    },
  };
  await putJson(bucket, claimed.key, updated, {
    etagMatches: claimed.object.etag,
    customMetadata: requestMetadata(updated),
  });
}

/** Return a claimed-but-not-dispatched record to `pending` without counting it as an attempt --
 * used both when a concurrent tick beat us to the dispatch slot and when no route currently has
 * capacity for this record's model (temporary, not a failure -- a later tick retries it). */
async function requeue(bucket, claimed, now) {
  const released = {
    ...claimed.record,
    status: "pending",
    attempts: Math.max(0, claimed.record.attempts - 1),
    updated_at: now.toISOString(),
    available_at: now.toISOString(),
    processing_started_at: undefined,
  };
  await putJson(bucket, claimed.key, released, {
    etagMatches: claimed.object.etag,
    customMetadata: requestMetadata(released),
  });
}

// ---------------------------------------------------------------------------------------------
// Cron lease -- single-runner guarantee for dispatchOne. `owner` (new) closes the CodeRabbit-
// flagged unconditional-release race: without it, an invocation whose own upstream call outlives
// `leaseDurationSeconds` can delete a *different*, later invocation's lease out from under it,
// letting a third tick acquire immediately while the second is still running unprotected.
// ---------------------------------------------------------------------------------------------

async function acquireCronLease(bucket, now, owner, leaseDurationSeconds) {
  const existing = await getJson(bucket, CRON_LOCK_KEY);
  if (existing && existing.value && existing.value.expires_at) {
    if (parseTime(existing.value.expires_at) > now.getTime()) {
      return false;
    }
  }
  const lease = {
    owner,
    acquired_at: now.toISOString(),
    expires_at: new Date(now.getTime() + leaseDurationSeconds * 1000).toISOString(),
  };
  try {
    const putRes = await putJson(bucket, CRON_LOCK_KEY, lease, {
      onlyIf: existing ? { etagMatches: existing.etag } : { etagDoesNotMatch: "*" },
    });
    return Boolean(putRes);
  } catch {
    return false;
  }
}

async function releaseCronLease(bucket, owner) {
  try {
    const current = await getJson(bucket, CRON_LOCK_KEY);
    if (current?.value?.owner === owner && typeof bucket.delete === "function") {
      await bucket.delete(CRON_LOCK_KEY);
    }
  } catch {
    // Best-effort release; the lease's own expiry is the real backstop.
  }
}

// ---------------------------------------------------------------------------------------------
// Per-route/per-account ledger (`state/dispatch_budget.json`) -- mirrors
// `citypods/compute/llm_budget.py`'s `RouteLedger` shape (minute/day window keys, `blocked_until`),
// keyed by `route_id` rather than canonical model. Keying by `route_id` is what makes multi-account
// rotation real: `gemini_3_flash_preview_primary` and `..._secondary` are separate route_ids with
// independent ledger entries, so once primary's window is full, ranking naturally falls through to
// secondary's still-fresh one (review/41). Single-writer by construction: only the cron-lease
// holder ever mutates this object, so a conditional put with no CAS-retry loop is sufficient --
// unlike the Python-side ledger, which is written by many concurrent GitHub Actions runners.
// ---------------------------------------------------------------------------------------------

function minuteKey(date) {
  return date.toISOString().slice(0, 16); // "YYYY-MM-DDTHH:MM", UTC
}

/** The local calendar date (YYYY-MM-DD) `date` falls on in the given IANA zone. */
function zonedDateKey(date, tzName) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: tzName,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

/** This zone's current UTC offset in milliseconds, derived by round-tripping `date`'s wall-clock
 * numbers through the zone (there is no direct zone->UTC-offset API in the Workers runtime).
 * Advisory precision only -- accurate to the minute away from a DST transition instant, which is
 * acceptable here (this only ever feeds a soft scheduling preference, review/33 §8's own
 * "advisory, provider's real response is the backstop" precedent, never a billing or quota
 * enforcement decision -- `routeAvailable` below enforces off stored counters, not this offset). */
function zoneOffsetMs(date, tzName) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: tzName,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })
      .formatToParts(date)
      .map((part) => [part.type, part.value]),
  );
  const hour = parts.hour === "24" ? "00" : parts.hour;
  const asUtc = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(hour),
    Number(parts.minute),
    Number(parts.second),
  );
  return asUtc - date.getTime();
}

/** The next local midnight in `tzName`, as a UTC instant. */
function nextLocalMidnightUTC(date, tzName) {
  const offsetMs = zoneOffsetMs(date, tzName);
  const key = zonedDateKey(date, tzName);
  const [year, month, day] = key.split("-").map(Number);
  const tomorrowWallClockAsUtc = Date.UTC(year, month - 1, day + 1, 0, 0, 0);
  return new Date(tomorrowWallClockAsUtc - offsetMs);
}

function nextMinuteBoundaryUTC(date) {
  const next = new Date(date);
  next.setUTCSeconds(0, 0);
  next.setUTCMinutes(next.getUTCMinutes() + 1);
  return next;
}

function ledgerEntry(budget, routeId) {
  if (!budget.routes) {
    budget.routes = {};
  }
  if (!budget.routes[routeId]) {
    budget.routes[routeId] = {
      requests_minute: 0,
      requests_minute_key: "",
      requests_day: 0,
      requests_day_key: "",
      tokens_minute: 0,
      blocked_until: "",
    };
  }
  return budget.routes[routeId];
}

function routeResetTimezone(route) {
  const providerCfg = DISPATCH_LIMITS.providers?.[route.provider];
  return route.reset_timezone || providerCfg?.reset_timezone || "UTC";
}

/** Roll each window independently against its own key -- rolling the minute window must never
 * also zero the day counter or vice versa (mirrors `LLMBudget._ledger` in `llm_budget.py`). */
function rollLedgerWindows(entry, route, now) {
  const mk = minuteKey(now);
  if (entry.requests_minute_key !== mk) {
    entry.requests_minute = 0;
    entry.tokens_minute = 0;
    entry.requests_minute_key = mk;
  }
  if (route.rpd != null) {
    const dk = zonedDateKey(now, routeResetTimezone(route));
    if (entry.requests_day_key !== dk) {
      entry.requests_day = 0;
      entry.requests_day_key = dk;
    }
  }
}

function routeAvailable(entry, route, { requests, tokens }, now) {
  if (entry.blocked_until && parseTime(entry.blocked_until) > now.getTime()) {
    return false;
  }
  // A paid route that declares no enforceable per-window limit (only `concurrency`, which this
  // ledger does not model -- see `nextRouteReset`) must not be treated as unlimited: it would
  // bypass the sole cost-control gate for the only paid provider in the compiled set (DeepSeek).
  // Fail closed until a real concurrency enforcement path is implemented (CodeRabbit, review/41).
  if (!route.free && route.rpm == null && route.rpd == null && route.tpm == null) {
    return false;
  }
  if (route.rpm != null && entry.requests_minute + requests > route.rpm) {
    return false;
  }
  if (route.rpd != null && entry.requests_day + requests > route.rpd) {
    return false;
  }
  if (route.tpm != null && entry.tokens_minute + tokens > route.tpm) {
    return false;
  }
  return true;
}

function reserveRouteCapacity(entry, route, { requests, tokens }) {
  entry.requests_minute += requests;
  entry.tokens_minute += tokens;
  if (route.rpd != null) {
    entry.requests_day += requests;
  }
}

/** The soonest UTC instant this route is predicted to free up, given only the axis(es) actually
 * keeping it unavailable right now -- mirrors `_next_quota_reset` in
 * `citypods/compute/llm_scheduler.py`, including that function's own documented fix: only offer
 * "next minute" as a candidate when the per-minute window is the *actual* binding constraint
 * (not unconditionally whenever the route merely declares an `rpm`/`tpm`), and likewise for
 * "next local midnight" against `rpd`. Unconditionally including every configured axis was a real
 * bug caught while porting this from the Python reference: a route blocked by a real 429 until
 * 12:30 but merely one request away from its per-minute cap would otherwise report "free at
 * 12:01" -- `min()` picking the bogus near-immediate minute rollover over the real, later block
 * -- exactly the busy-retry failure mode the Python docstring warns about. `blocked_until` is
 * always a candidate on its own (it only ever moves later, `LLMBudget.block`'s semantics) since it
 * holds the route back regardless of what any window counter says. Precision is advisory-level,
 * not exact -- see `zoneOffsetMs`'s docstring. */
function nextRouteReset(entry, route, now, { requests = 1, tokens = 0 } = {}) {
  const candidates = [];
  if (entry.blocked_until && parseTime(entry.blocked_until) > now.getTime()) {
    candidates.push(new Date(entry.blocked_until));
  }
  const perMinuteBinding =
    (route.rpm != null && (entry.requests_minute || 0) + requests > route.rpm) ||
    (route.tpm != null && (entry.tokens_minute || 0) + tokens > route.tpm);
  if (perMinuteBinding) {
    candidates.push(nextMinuteBoundaryUTC(now));
  }
  if (route.rpd != null && (entry.requests_day || 0) + requests > route.rpd) {
    candidates.push(nextLocalMidnightUTC(now, routeResetTimezone(route)));
  }
  if (candidates.length === 0) {
    // `routeAvailable` rejected this route on an axis this function doesn't model a precise
    // reset for (e.g. a future `concurrency` field) -- fall back to the one-minute guess rather
    // than `now`, matching the Python reference's identical fallback.
    return nextMinuteBoundaryUTC(now);
  }
  return new Date(Math.min(...candidates.map((d) => d.getTime())));
}

/** Free before paid, then lowest declared per-token cost, then route_id for determinism --
 * mirrors `select_route`'s ranking in `citypods/compute/llm_scheduler.py` (§5 gate 6). */
function rankRoutes(routes) {
  return routes.slice().sort((a, b) => {
    if (Boolean(a.free) !== Boolean(b.free)) {
      return a.free ? -1 : 1;
    }
    const costA = (a.input_per_token || 0) + (a.output_per_token || 0);
    const costB = (b.input_per_token || 0) + (b.output_per_token || 0);
    if (costA !== costB) {
      return costA - costB;
    }
    return String(a.route_id).localeCompare(String(b.route_id));
  });
}

function resolveProviderCredentials(env, route, dispatchLimits = DISPATCH_LIMITS) {
  const providerCfg = dispatchLimits.providers?.[route.provider];
  if (!providerCfg) {
    throw new Error(`no provider config compiled for provider ${route.provider}`);
  }
  const accounts = providerCfg.accounts || [];
  // Fail closed on a malformed route: `accounts[0]` is only a fallback for a route that
  // intentionally omits `account_id` altogether. A route that *names* an account_id must match it
  // exactly -- falling back to the first configured account otherwise would reserve capacity
  // against the intended (e.g. secondary) route's ledger entry while silently sending the
  // *primary* account's key, a real account-confusion bug for a hand-authored YAML typo
  // (CodeRabbit, review/41).
  const account = route.account_id
    ? accounts.find((candidate) => candidate.id === route.account_id)
    : accounts[0];
  if (!account?.api_key_env) {
    throw new Error(`no account configured for provider ${route.provider} route ${route.route_id}`);
  }
  const apiKey = env[account.api_key_env];
  if (!apiKey) {
    throw new Error(`missing secret ${account.api_key_env} for provider ${route.provider}`);
  }

  const apiBase = String(providerCfg.api_base || "").replace(/\/+$/, "");
  if (!apiBase) {
    throw new Error(`no api_base configured for provider ${route.provider}`);
  }
  const chatPath = providerCfg.chat_path || "/v1/chat/completions";
  const url = `${apiBase}${chatPath}`;

  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`provider ${route.provider} api_base/chat_path is not a valid URL`);
  }
  if (parsed.protocol !== "https:") {
    throw new Error(`provider ${route.provider} api_base must use HTTPS`);
  }

  return { apiKey, url, upstreamModel: route.upstream_model };
}

/** Rank candidate routes for `canonicalModel`, reserve the first one with real capacity right now
 * (free before paid), and elevate to paid only when free capacity genuinely can't help -- either no
 * free route exists for this model at all, or the soonest free route would reset at/after the
 * record's `deadline_at`. Returns `{chosenRoute, entry}` or a `{reason}` explaining why nothing was
 * selected: `"no_configured_route"`/`"no_eligible_route"` are permanent (the caller should fail the
 * record -- it will never resolve), `"no_capacity"` is temporary (leave it pending for a later
 * tick). This is the piece that makes the compiled `rpm`/`rpd`/`tpm`/`account_id` data in
 * `dispatch_limits.json` actually enforce anything, superseding the single global
 * one-request-per-interval gate this replaced (review/41). */
function selectRoute(budget, canonicalModel, policy, now, dispatchLimits = DISPATCH_LIMITS) {
  const routeIds = dispatchLimits.model_routes_map?.[canonicalModel] || [];
  const candidates = routeIds.map((id) => dispatchLimits.routes_by_id[id]).filter(Boolean);
  const freeRanked = rankRoutes(candidates.filter((route) => route.free));
  const paidRanked = rankRoutes(candidates.filter((route) => !route.free));
  const allowPaid = Boolean(policy?.allow_paid);
  const deadlineAt = policy?.deadline_at ? parseTime(policy.deadline_at) : null;
  const tokens = policy?.estimated_tokens || 1024;
  const reservationSize = { requests: 1, tokens };

  const tryRoutes = (ranked) => {
    for (const route of ranked) {
      const entry = ledgerEntry(budget, route.route_id);
      rollLedgerWindows(entry, route, now);
      if (routeAvailable(entry, route, reservationSize, now)) {
        return { chosenRoute: route, entry };
      }
    }
    return null;
  };

  const freeHit = tryRoutes(freeRanked);
  if (freeHit) {
    return freeHit;
  }

  if (candidates.length === 0) {
    return { reason: "no_configured_route" };
  }
  if (freeRanked.length === 0 && !allowPaid) {
    return { reason: "no_eligible_route" };
  }
  if (!allowPaid) {
    return { reason: "no_capacity" };
  }

  const earliestFreeReset =
    freeRanked.length > 0
      ? Math.min(
          ...freeRanked.map((route) => {
            const entry = ledgerEntry(budget, route.route_id);
            rollLedgerWindows(entry, route, now);
            return nextRouteReset(entry, route, now, reservationSize).getTime();
          }),
        )
      : null;
  const shouldElevateNow =
    earliestFreeReset === null || (deadlineAt !== null && earliestFreeReset >= deadlineAt);

  if (shouldElevateNow) {
    const paidHit = tryRoutes(paidRanked);
    if (paidHit) {
      return paidHit;
    }
  }
  return { reason: "no_capacity" };
}

export async function dispatchOne(env, fetchImpl = fetch, now = new Date()) {
  const bucket = env.LLM_QUEUE;
  if (!bucket) {
    return { status: "no_storage" };
  }

  const cfg = config(env);
  const leaseOwner = crypto.randomUUID();
  const acquiredLock = await acquireCronLease(bucket, now, leaseOwner, cfg.leaseDurationSeconds);
  if (!acquiredLock) {
    return { status: "lease_busy" };
  }

  try {
    let cursor = undefined;
    let claimable = [];
    let scanned = 0;

    while (scanned < cfg.maxQueueScan) {
      const batchSize = Math.min(100, cfg.maxQueueScan - scanned);
      const listResult = await bucket.list({ prefix: REQUEST_PREFIX, cursor, limit: batchSize });
      const objects = listResult ? listResult.objects || [] : [];
      if (objects.length === 0) {
        break;
      }

      for (const obj of objects) {
        if (claimable.length >= cfg.maxQueueScan) break;
        scanned += 1;
        // `bucket.list()` already returns `customMetadata` per object -- skip a terminal
        // (completed/failed) or still-delayed record straight from the listing, with no body
        // fetch at all. A record with no `customMetadata` (written before this optimization
        // shipped) falls through to the real body fetch below, same as before.
        const meta = obj.customMetadata || {};
        if (meta.status && meta.status !== "pending") continue;
        if (
          meta.status === "pending" &&
          meta.available_at &&
          parseTime(meta.available_at) > now.getTime()
        ) {
          continue;
        }
        const loaded = await getJson(bucket, obj.key);
        if (!loaded || !loaded.value) continue;
        const rec = loaded.value;
        if (rec.status !== "pending") continue;
        if (rec.available_at && parseTime(rec.available_at) > now.getTime()) continue;
        claimable.push({ key: obj.key, object: loaded.object, record: rec });
      }

      if (claimable.length >= cfg.maxQueueScan || !listResult.truncated || !listResult.cursor) {
        break;
      }
      cursor = listResult.cursor;
    }

    if (claimable.length === 0) {
      return { status: "idle" };
    }

    claimable.sort((a, b) => {
      const pA = a.record.policy?.submit_next ? 0 : 1;
      const pB = b.record.policy?.submit_next ? 0 : 1;
      if (pA !== pB) return pA - pB;
      return parseTime(a.record.created_at) - parseTime(b.record.created_at);
    });

    const claimed = claimable[0];
    claimed.record.attempts = (claimed.record.attempts || 0) + 1;
    claimed.record.processing_started_at = now.toISOString();

    let budgetLoaded = await getJson(bucket, DISPATCH_BUDGET_KEY);
    let budget = (budgetLoaded && budgetLoaded.value) || { routes: {} };
    const selection = selectRoute(budget, claimed.record.model, claimed.record.policy, now);

    if (!selection.chosenRoute) {
      if (selection.reason === "no_capacity") {
        await requeue(bucket, claimed, now);
        return { status: "no_capacity", requestId: claimed.record.id };
      }
      // "no_configured_route" (nothing in dispatch_limits.json serves this canonical model at
      // all) or "no_eligible_route" (only paid routes exist and the caller disallowed paid) --
      // both permanent: no future tick will make this resolve differently.
      await saveFailure(bucket, claimed, 400, now, selection.reason);
      return { status: "failed", requestId: claimed.record.id, reason: selection.reason };
    }

    const { chosenRoute } = selection;

    // Resolve credentials *before* touching the ledger: a missing secret or malformed provider
    // config must never spend a route's RPM/RPD/TPM window, or a persistent config error would
    // consume real capacity every tick until the window resets, potentially starving a healthy
    // account (CodeRabbit, review/41). Nothing here has side effects yet, so simply failing the
    // record and returning is enough -- there is no reservation to release.
    let creds;
    try {
      creds = resolveProviderCredentials(env, chosenRoute);
    } catch (error) {
      await saveFailure(bucket, claimed, 500, now, "credential_resolution_failed");
      console.error(
        JSON.stringify({ event: "llm_dispatch_credentials_error", error: error?.message }),
      );
      return { status: "failed", requestId: claimed.record.id };
    }

    // Reserve the chosen route's capacity durably before dispatching. The cron lease makes this
    // invocation the ledger's sole writer under normal operation, so a conflict here means a
    // sibling write raced us anyway (should not happen, but must not be trusted blindly) -- retry
    // a bounded number of times against a freshly reloaded ledger rather than dispatching without
    // ever durably recording the reservation, which would let a later tick spend the same capacity
    // again (CodeRabbit, review/41: "do not dispatch after a failed ledger CAS write").
    let ledgerSaved = false;
    const reservationSize = {
      requests: 1,
      tokens: claimed.record.policy?.estimated_tokens || 1024,
    };
    for (let attempt = 0; attempt < 3 && !ledgerSaved; attempt += 1) {
      if (attempt > 0) {
        budgetLoaded = await getJson(bucket, DISPATCH_BUDGET_KEY);
        budget = (budgetLoaded && budgetLoaded.value) || { routes: {} };
      }
      const entry = ledgerEntry(budget, chosenRoute.route_id);
      rollLedgerWindows(entry, chosenRoute, now);
      // Re-check availability against the (potentially reloaded) ledger before reserving: a
      // concurrent write may have exhausted this route's capacity, and reserving unconditionally
      // would oversubscribe it.
      if (!routeAvailable(entry, chosenRoute, reservationSize, now)) {
        break;
      }
      reserveRouteCapacity(entry, chosenRoute, reservationSize);
      ledgerSaved = Boolean(
        await putJson(bucket, DISPATCH_BUDGET_KEY, budget, {
          onlyIf: budgetLoaded ? { etagMatches: budgetLoaded.etag } : { etagDoesNotMatch: "*" },
        }),
      );
    }
    if (!ledgerSaved) {
      // Exhausted retries -- leave the record claimable again rather than dispatch with no
      // durable reservation. `attempts` is decremented by `requeue` (mirrors `no_capacity`), since
      // nothing was actually sent to a provider.
      console.error(
        JSON.stringify({ event: "llm_dispatch_ledger_conflict", route_id: chosenRoute.route_id }),
      );
      await requeue(bucket, claimed, now);
      return { status: "no_capacity", requestId: claimed.record.id };
    }

    const upstreamPayload = {
      ...claimed.record.request,
      model: creds.upstreamModel,
    };

    let response;
    try {
      const headers = { accept: "application/json", "content-type": "application/json" };
      if (creds.apiKey) {
        headers.authorization = `Bearer ${creds.apiKey}`;
      }
      response = await fetchImpl(creds.url, {
        method: "POST",
        headers,
        body: JSON.stringify(upstreamPayload),
        redirect: "manual",
        signal: AbortSignal.timeout(cfg.upstreamTimeoutSeconds * 1000),
      });
    } catch {
      if (claimed.record.attempts < cfg.maxAttempts) {
        await saveRetry(bucket, claimed, null, cfg, now);
        return { status: "retrying", requestId: claimed.record.id };
      }
      await saveFailure(bucket, claimed, null, now, "upstream_unreachable");
      return { status: "failed", requestId: claimed.record.id };
    }

    if (!response.ok) {
      if (retryableStatus(response.status) && claimed.record.attempts < cfg.maxAttempts) {
        await response.body?.cancel();
        await saveRetry(bucket, claimed, response, cfg, now);
        return { status: "retrying", requestId: claimed.record.id, upstreamStatus: response.status };
      }
      await response.body?.cancel();
      await saveFailure(bucket, claimed, response.status, now);
      return { status: "failed", requestId: claimed.record.id, upstreamStatus: response.status };
    }

    let responseJson;
    try {
      const responseText = await readTextLimited(response.body, cfg.maxResponseBytes);
      responseJson = JSON.parse(responseText);
    } catch (error) {
      await saveFailure(
        bucket,
        claimed,
        response.status,
        now,
        error instanceof BodyTooLargeError ? "upstream_response_too_large" : "invalid_upstream_json",
      );
      return { status: "failed", requestId: claimed.record.id, upstreamStatus: response.status };
    }

    if (!responseJson || typeof responseJson !== "object" || Array.isArray(responseJson)) {
      await saveFailure(bucket, claimed, response.status, now, "invalid_upstream_json");
      return { status: "failed", requestId: claimed.record.id, upstreamStatus: response.status };
    }

    const completed = {
      ...claimed.record,
      status: "completed",
      updated_at: now.toISOString(),
      completed_at: now.toISOString(),
      response: responseJson,
    };
    await putJson(bucket, claimed.key, completed, {
      etagMatches: claimed.object.etag,
      customMetadata: requestMetadata(completed),
    });
    return { status: "completed", requestId: claimed.record.id, upstreamStatus: response.status };
  } finally {
    await releaseCronLease(bucket, leaseOwner);
  }
}

function pollBody(record) {
  return {
    id: record.id,
    object: "chat.completion",
    status: record.status,
    request_id: record.id,
    created: Math.floor(parseTime(record.created_at) / 1000),
    model: record.model,
  };
}

function requestLocation(request, id) {
  return new URL(`/v1/requests/${encodeURIComponent(id)}`, request.url).toString();
}

function responseForRecord(request, record) {
  if (record.status === "completed") {
    return jsonResponse(record.response, 200, { "x-request-id": record.id });
  }
  if (record.status === "failed") {
    return jsonResponse(
      { error: { message: "upstream LLM dispatch failed", type: "upstream_error", code: "dispatch_failed" } },
      502,
      { "x-request-id": record.id },
    );
  }
  return jsonResponse(pollBody(record), 202, {
    location: requestLocation(request, record.id),
    "retry-after": "60",
    "x-request-id": record.id,
  });
}

async function handleChatCompletion(request, env, cfg) {
  if (!env.LLM_QUEUE) {
    return plain(503, "dispatch storage is not configured");
  }
  try {
    const body = await readJsonBody(request, cfg.maxRequestBytes);
    const normalized = normalizeChatRequest(body, cfg);
    const idempotencyKey = request.headers.get("idempotency-key") || "";
    if (idempotencyKey.length > 256) {
      throw new HttpError(400, "Idempotency-Key is too long");
    }
    const record = await enqueue(env.LLM_QUEUE, normalized, cfg, new Date(), idempotencyKey);
    return responseForRecord(request, record);
  } catch (error) {
    if (error instanceof BodyTooLargeError) {
      return plain(413, "request body is too large");
    }
    if (error instanceof HttpError) {
      return plain(error.status, error.message);
    }
    return plain(503, "request could not be queued");
  }
}

async function handlePoll(request, env, id) {
  if (!env.LLM_QUEUE) {
    return plain(503, "dispatch storage is not configured");
  }
  const loaded = await getJson(env.LLM_QUEUE, requestKey(id));
  if (!loaded) {
    return plain(404, "request not found");
  }
  return responseForRecord(request, loaded.value);
}

export async function handleRequest(request, env) {
  const url = new URL(request.url);
  if (url.pathname === "/healthz" && request.method === "GET") {
    return jsonResponse({ ok: true });
  }

  if (!(await hasValidBearer(request, env))) {
    return plain(401, "unauthorized", { "www-authenticate": "Bearer" });
  }

  const cfg = config(env);

  if (url.pathname === "/v1/models" && request.method === "GET") {
    return jsonResponse({
      object: "list",
      data: [{ id: cfg.model, object: "model", owned_by: cfg.provider }],
    });
  }

  if (url.pathname === "/v1/queue/estimate" && request.method === "GET") {
    const model = url.searchParams.get("model") || cfg.model;
    // Counts straight from `customMetadata` (no body fetch) whenever it's present; paginates the
    // full bucket via the list cursor rather than stopping at a fixed `limit: 1000`, so backlog
    // accuracy doesn't quietly degrade once the queue -- including terminal records, which are
    // never pruned -- grows past one page (CodeRabbit, review/41).
    let count = 0;
    let cursor = undefined;
    do {
      const listRes = await env.LLM_QUEUE.list({ prefix: REQUEST_PREFIX, cursor, limit: 1000 });
      const objects = listRes ? listRes.objects || [] : [];
      for (const obj of objects) {
        const meta = obj.customMetadata || {};
        if (meta.status) {
          if (meta.status === "pending" && (!model || meta.model === model)) {
            count += 1;
          }
          continue;
        }
        const loaded = await getJson(env.LLM_QUEUE, obj.key);
        if (loaded && loaded.value && loaded.value.status === "pending") {
          if (!model || loaded.value.model === model) {
            count += 1;
          }
        }
      }
      cursor = listRes && listRes.truncated ? listRes.cursor : undefined;
    } while (cursor);
    const routeIds = DISPATCH_LIMITS.model_routes_map?.[model] || [];
    const fastestRpm = routeIds
      .map((id) => DISPATCH_LIMITS.routes_by_id[id]?.rpm)
      .filter((rpm) => typeof rpm === "number" && rpm > 0);
    const perRequestSeconds = fastestRpm.length > 0 ? 60 / Math.max(...fastestRpm) : 60;
    const estSeconds = Math.ceil(count * perRequestSeconds);
    return jsonResponse({
      model,
      backlog_count: count,
      estimated_wait_seconds: estSeconds,
    });
  }

  if (url.pathname === "/v1/chat/completions" && request.method === "POST") {
    return handleChatCompletion(request, env, cfg);
  }

  const match = url.pathname.match(/^\/v1\/requests\/(chatcmpl-[A-Za-z0-9-]{8,96})$/);
  if (match && request.method === "GET") {
    return handlePoll(request, env, match[1]);
  }

  if (url.pathname.startsWith("/v1/")) {
    return plain(405, "method or endpoint not allowed");
  }

  return plain(404, "not found");
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },

  async scheduled(_controller, env) {
    try {
      const result = await dispatchOne(env);
      console.log(JSON.stringify({ event: "llm_dispatch", status: result.status, request_id: result.requestId || null }));
    } catch (error) {
      console.error(
        JSON.stringify({ event: "llm_dispatch", status: "error", error: error?.name || "Error" }),
      );
    }
  },
};

export {
  acquireCronLease,
  config,
  nextLocalMidnightUTC,
  nextRouteReset,
  normalizeChatRequest,
  rankRoutes,
  releaseCronLease,
  requestKey,
  resolveProviderCredentials,
  routeAvailable,
  selectRoute,
};
