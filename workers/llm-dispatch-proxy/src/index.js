import DISPATCH_LIMITS from "./dispatch_limits.json" with { type: "json" };

const REQUEST_PREFIX = "requests/";
const PENDING_PREFIX = "pending/";
const CRON_LOCK_KEY = "locks/cron.json";
const DISPATCH_BUDGET_KEY = "state/dispatch_budget.json";
// Chapter-tag batches can legitimately carry several large, source-backed chapter windows. Keep
// an explicit cap (the Worker parses JSON in memory) but avoid rejecting ordinary long meetings.
const DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024;
const DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const DEFAULT_MAX_QUEUE_SCAN = 1000;
const DEFAULT_MAX_ATTEMPTS = 5;
const DEFAULT_PROCESSING_TIMEOUT_SECONDS = 20 * 60;
const DEFAULT_RETRY_BASE_SECONDS = 60;
const DEFAULT_RETRY_MAX_SECONDS = 60 * 60;
// Large-context & reasoning LLM calls (DeepSeek 1M, Gemini 1M, Mistral 256k) can take several minutes.
// Wall-clock timeouts are scaled for multi-minute generation.  Fast requests use a short lane so
// scheduled invocations drain backlog first; long-context requests get the larger bounded lane.
const DEFAULT_LEASE_DURATION_SECONDS = 14 * 60; // 840s
const DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 12 * 60; // 720s
const DEFAULT_FAST_UPSTREAM_TIMEOUT_SECONDS = 90;
// Keep enough wall-clock margin to persist terminal records and release reservations while still
// allowing the 12-minute long lane inside the 13m40s invocation deadline.
const DEFAULT_FINALIZATION_RESERVE_SECONDS = 20;
const DEFAULT_MAX_EXECUTION_SECONDS = 13 * 60 + 40; // 820s
const DEFAULT_BATCH_CONCURRENCY = 4;
const DEFAULT_MAX_TOTAL_REQUESTS = 16;

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

function parseTime(value) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : 0;
}

function positiveNumber(value, fallback, { integer = false } = {}) {
  if (value === undefined || value === null || value === "") {
    return fallback;
  }
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

function canonicalModelName(model, dispatchLimits = DISPATCH_LIMITS) {
  let current = model;
  const seen = new Set();
  const aliases = dispatchLimits?.model_aliases || {};
  while (typeof aliases[current] === "string" && !seen.has(current)) {
    seen.add(current);
    current = aliases[current];
  }
  return current;
}

function modelName(model) {
  return model.split("/").pop() || model;
}

function config(env) {
  const provider = requiredString(env.PROVIDER_NAME, "PROVIDER_NAME");
  const upstreamModel = requiredString(env.UPSTREAM_MODEL, "UPSTREAM_MODEL");
  const model = String(env.MODEL_ID || `${provider}/${modelName(upstreamModel)}`).trim();

  const result = {
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
    maxExecutionSeconds: positiveNumber(
      env.MAX_EXECUTION_SECONDS,
      DEFAULT_MAX_EXECUTION_SECONDS,
    ),
    batchConcurrency: positiveNumber(
      env.BATCH_CONCURRENCY,
      DEFAULT_BATCH_CONCURRENCY,
      { integer: true },
    ),
    maxTotalRequests: positiveNumber(
      env.MAX_TOTAL_REQUESTS,
      DEFAULT_MAX_TOTAL_REQUESTS,
      { integer: true },
    ),
    ...leaseAndTimeoutConfig(env),
  };
  if (result.maxExecutionSeconds >= result.leaseDurationSeconds) {
    throw new Error(
      `MAX_EXECUTION_SECONDS (${result.maxExecutionSeconds}) must be less than ` +
        `LEASE_DURATION_SECONDS (${result.leaseDurationSeconds})`,
    );
  }
  for (const [name, timeoutSeconds] of [
    ["UPSTREAM_TIMEOUT_SECONDS", result.upstreamTimeoutSeconds],
    ["FAST_UPSTREAM_TIMEOUT_SECONDS", result.fastUpstreamTimeoutSeconds],
  ]) {
    if (timeoutSeconds + result.finalizationReserveSeconds > result.maxExecutionSeconds) {
      throw new Error(
        `${name} plus FINALIZATION_RESERVE_SECONDS must fit within ` +
          `MAX_EXECUTION_SECONDS (${result.maxExecutionSeconds})`,
      );
    }
  }
  return result;
}

function leaseAndTimeoutConfig(env) {
  const leaseDurationSeconds = positiveNumber(
    env.LEASE_DURATION_SECONDS,
    DEFAULT_LEASE_DURATION_SECONDS,
  );
  const upstreamTimeoutSeconds = positiveNumber(
    env.UPSTREAM_TIMEOUT_SECONDS,
    DEFAULT_UPSTREAM_TIMEOUT_SECONDS,
  );
  const fastUpstreamTimeoutSeconds = positiveNumber(
    env.FAST_UPSTREAM_TIMEOUT_SECONDS,
    DEFAULT_FAST_UPSTREAM_TIMEOUT_SECONDS,
  );
  const finalizationReserveSeconds = positiveNumber(
    env.FINALIZATION_RESERVE_SECONDS,
    DEFAULT_FINALIZATION_RESERVE_SECONDS,
  );
  if (upstreamTimeoutSeconds >= leaseDurationSeconds) {
    throw new Error(
      `UPSTREAM_TIMEOUT_SECONDS (${upstreamTimeoutSeconds}) must be less than ` +
        `LEASE_DURATION_SECONDS (${leaseDurationSeconds})`,
    );
  }
  if (fastUpstreamTimeoutSeconds >= upstreamTimeoutSeconds) {
    throw new Error(
      `FAST_UPSTREAM_TIMEOUT_SECONDS (${fastUpstreamTimeoutSeconds}) must be less than ` +
        `UPSTREAM_TIMEOUT_SECONDS (${upstreamTimeoutSeconds})`,
    );
  }
  return {
    leaseDurationSeconds,
    upstreamTimeoutSeconds,
    fastUpstreamTimeoutSeconds,
    finalizationReserveSeconds,
  };
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

function pendingKey(id) {
  return `${PENDING_PREFIX}${id}.json`;
}

async function markPending(bucket, id) {
  // A compact pending-only index keeps terminal request history from hiding live work behind
  // MAX_QUEUE_SCAN's bounded legacy object scan. The request object remains canonical.
  await putJson(bucket, pendingKey(id), { id }, { onlyIf: { etagDoesNotMatch: "*" } });
}

async function unmarkPending(bucket, id) {
  await bucket.delete(pendingKey(id));
}

async function getJson(bucket, key) {
  const object = await bucket.get(key);
  if (!object) {
    return null;
  }
  return { object, etag: object.etag, value: await object.json() };
}

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
  if (etagMatches) {
    options.onlyIf = { etagMatches };
  } else if (onlyIf) {
    options.onlyIf = onlyIf;
  }
  return bucket.put(key, body, options);
}

function normalizeChatRequest(body, cfg, dispatchLimits = DISPATCH_LIMITS) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new HttpError(400, "request body must be an object");
  }
  if (body.stream) {
    throw new HttpError(400, "stream mode is not supported by the asynchronous dispatch queue");
  }

  const requestedModel =
    typeof body.model === "string" && body.model.trim() ? body.model.trim() : cfg.model;
  let canonicalModel = canonicalModelName(requestedModel, dispatchLimits);
  if (!dispatchLimits.model_routes_map?.[canonicalModel]) {
    const matchingRoute = (dispatchLimits.routes || []).find(
      (r) =>
        r.upstream_model === requestedModel || r.model === `${cfg.provider}/${requestedModel}`,
    );
    if (matchingRoute) {
      canonicalModel = canonicalModelName(matchingRoute.model, dispatchLimits);
    } else if (requestedModel === cfg.upstreamModel) {
      canonicalModel = canonicalModelName(cfg.model, dispatchLimits);
    }
  }

  const configuredModels = Object.keys(dispatchLimits.model_routes_map || {});
  if (configuredModels.length > 0 && !configuredModels.includes(canonicalModel)) {
    throw new HttpError(400, `unknown model: ${requestedModel}`);
  }

  const rawMessages = body.messages;
  if (!Array.isArray(rawMessages) || rawMessages.length === 0) {
    throw new HttpError(400, "messages must be a non-empty array");
  }

  const messages = rawMessages.map((msg, index) => {
    if (!msg || typeof msg !== "object" || Array.isArray(msg)) {
      throw new HttpError(400, `message at index ${index} must be an object`);
    }
    const role = String(msg.role || "").trim();
    if (!role) {
      throw new HttpError(400, `message at index ${index} must have a role`);
    }
    const normalizedMsg = { role };
    if (typeof msg.content === "string") {
      normalizedMsg.content = msg.content;
    } else if (Array.isArray(msg.content)) {
      normalizedMsg.content = msg.content;
    } else if (msg.content === null || msg.content === undefined) {
      normalizedMsg.content = "";
    } else {
      throw new HttpError(400, `message at index ${index} content must be a string or array`);
    }
    if (msg.name) {
      normalizedMsg.name = String(msg.name);
    }
    return normalizedMsg;
  });

  const request = {
    model: canonicalModel,
    messages,
    stream: false,
  };
  for (const field of COPY_FIELDS) {
    if (body[field] !== undefined) {
      request[field] = body[field];
    }
  }

  const policy = {};
  if (body.allow_paid !== undefined) policy.allow_paid = Boolean(body.allow_paid);
  if (body.allow_batch !== undefined) policy.allow_batch = Boolean(body.allow_batch);
  if (body.submit_next !== undefined) policy.submit_next = Boolean(body.submit_next);
  if (typeof body.deadline_at === "string") policy.deadline_at = body.deadline_at;
  if (body.timeout_class === "long" || body.timeout_class === "fast") {
    policy.timeout_class = body.timeout_class;
  }
  if (typeof body.estimated_tokens === "number" && body.estimated_tokens > 0) {
    policy.estimated_tokens = Math.floor(body.estimated_tokens);
  }
  for (const field of ["input_tokens_estimate", "output_token_budget"]) {
    if (body[field] === undefined) continue;
    if (typeof body[field] !== "number" || !Number.isFinite(body[field]) || body[field] < 0) {
      throw new HttpError(400, `${field} must be a non-negative number`);
    }
    policy[field] = Math.floor(body[field]);
  }
  if (policy.input_tokens_estimate === undefined && policy.estimated_tokens !== undefined) {
    policy.input_tokens_estimate = policy.estimated_tokens;
  }
  if (body.allowed_models !== undefined) {
    if (!Array.isArray(body.allowed_models) || body.allowed_models.length === 0) {
      throw new HttpError(400, "allowed_models must be a non-empty array when provided");
    }
    const allowedModels = [...new Set(body.allowed_models.map((candidate) => {
      if (typeof candidate !== "string" || !candidate.trim()) {
        throw new HttpError(400, "allowed_models entries must be non-empty strings");
      }
      const canonical = canonicalModelName(candidate.trim(), dispatchLimits);
      if (!configuredModels.includes(canonical)) {
        throw new HttpError(400, `unknown allowed model: ${candidate}`);
      }
      return canonical;
    }))];
    if (!allowedModels.includes(canonicalModel)) allowedModels.unshift(canonicalModel);
    policy.allowed_models = allowedModels;
  }

  return {
    model: canonicalModel,
    request,
    policy,
  };
}

async function enqueue(bucket, normalized, cfg, now = new Date(), idempotencyKey = "") {
  let id;
  if (idempotencyKey) {
    const rawHash = await sha256Hex(idempotencyKey);
    id = `chatcmpl-${rawHash.slice(0, 32)}`;
  } else {
    id = `chatcmpl-${crypto.randomUUID()}`;
  }

  const key = requestKey(id);
  const existing = await getJson(bucket, key);
  if (existing && existing.value) {
    const stored = existing.value;
    const sameModel = stored.model === normalized.model;
    const sameRequest = JSON.stringify(stored.request) === JSON.stringify(normalized.request);
    const samePolicy = JSON.stringify(stored.policy || {}) === JSON.stringify(normalized.policy || {});
    if (!sameModel || !sameRequest || !samePolicy) {
      throw new HttpError(409, "idempotency key collision with different payload or policy");
    }
    if (stored.status === "pending") await markPending(bucket, stored.id);
    return stored;
  }

  const record = {
    id,
    object: "chat.completion.queued",
    status: "pending",
    model: normalized.model,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
    available_at: now.toISOString(),
    attempts: 0,
    request: normalized.request,
    policy: normalized.policy,
  };

  const putResult = await putJson(bucket, key, record, {
    onlyIf: { etagDoesNotMatch: "*" },
    customMetadata: requestMetadata(record),
  });

  if (!putResult) {
    const fresh = await getJson(bucket, key);
    if (fresh && fresh.value) {
      const stored = fresh.value;
      const sameModel = stored.model === normalized.model;
      const sameRequest = JSON.stringify(stored.request) === JSON.stringify(normalized.request);
      const samePolicy = JSON.stringify(stored.policy || {}) === JSON.stringify(normalized.policy || {});
      if (!sameModel || !sameRequest || !samePolicy) {
        throw new HttpError(409, "idempotency key collision with different payload or policy");
      }
      if (stored.status === "pending") await markPending(bucket, stored.id);
      return stored;
    }
    throw new HttpError(503, "could not persist queued request");
  }

  await markPending(bucket, record.id);

  return record;
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

async function saveRetry(bucket, claimed, response, cfg, now, errorDetail = null) {
  const delay = retryDelaySeconds(response, claimed.record.attempts, cfg);
  const availableAt = new Date(now.getTime() + delay * 1000).toISOString();
  const lastError = errorDetail || {
    status: response ? response.status : 0,
    timestamp: now.toISOString(),
  };
  const updated = {
    ...claimed.record,
    status: "pending",
    updated_at: now.toISOString(),
    available_at: availableAt,
    processing_started_at: undefined,
    last_error: lastError,
  };
  await putJson(bucket, claimed.key, updated, {
    etagMatches: claimed.object.etag,
    customMetadata: requestMetadata(updated),
  });
  await markPending(bucket, updated.id);
}

async function saveFailure(bucket, claimed, status, now, code = "upstream_error", errorDetail = null) {
  const errObj = errorDetail || {
    code,
    status: status || 502,
    timestamp: now.toISOString(),
  };
  const updated = {
    ...claimed.record,
    status: "failed",
    updated_at: now.toISOString(),
    completed_at: now.toISOString(),
    error: errObj,
  };
  const saved = await putJson(bucket, claimed.key, updated, {
    etagMatches: claimed.object.etag,
    customMetadata: requestMetadata(updated),
  });
  // A lost conditional write means another worker owns the newer canonical state.  Its pending
  // marker is still needed until that state is observed and finalized.
  if (saved) await unmarkPending(bucket, updated.id);
}

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
  await markPending(bucket, released.id);
}

// ---------------------------------------------------------------------------------------------
// Cron lease -- single-runner guarantee for dispatch.
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
    if (current?.value?.owner === owner) {
      // R2's delete has no ETag precondition.  Deleting after a read would leave a TOCTOU window
      // in which an expired lease is acquired by another invocation and then removed by this
      // stale owner.  A CAS-written expired tombstone is immediately acquirable and cannot erase
      // a newer lease if the object changed after our read.
      await putJson(
        bucket,
        CRON_LOCK_KEY,
        {
          ...current.value,
          released_at: new Date().toISOString(),
          expires_at: new Date(0).toISOString(),
        },
        { etagMatches: current.etag },
      );
    }
  } catch {
    // Best-effort release; the lease's own expiry is the real backstop.
  }
}

async function renewCronLease(bucket, now, owner, leaseDurationSeconds) {
  const current = await getJson(bucket, CRON_LOCK_KEY);
  if (
    !current ||
    current.value?.owner !== owner ||
    parseTime(current.value?.expires_at) <= now.getTime()
  ) {
    return false;
  }
  const renewed = {
    ...current.value,
    renewed_at: now.toISOString(),
    expires_at: new Date(now.getTime() + leaseDurationSeconds * 1000).toISOString(),
  };
  try {
    return Boolean(
      await putJson(bucket, CRON_LOCK_KEY, renewed, { etagMatches: current.etag }),
    );
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------------------------
// Per-route/per-account ledger (`state/dispatch_budget.json`)
// ---------------------------------------------------------------------------------------------

function minuteKey(date) {
  return date.toISOString().slice(0, 16); // "YYYY-MM-DDTHH:MM", UTC
}

function zonedDateKey(date, timeZone = "UTC") {
  try {
    const formatter = new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    const parts = formatter.formatToParts(date);
    const y = parts.find((p) => p.type === "year")?.value;
    const m = parts.find((p) => p.type === "month")?.value;
    const d = parts.find((p) => p.type === "day")?.value;
    if (y && m && d) {
      return `${y}-${m}-${d}`;
    }
  } catch {
    // Fall back to UTC if the timezone string is invalid.
  }
  return date.toISOString().slice(0, 10);
}

function routeResetTimezone(route, dispatchLimits = DISPATCH_LIMITS) {
  return (
    route?.reset_timezone ||
    dispatchLimits?.providers?.[route?.provider]?.reset_timezone ||
    "UTC"
  );
}

function nextLocalMidnightUTC(date, timeZone = "UTC") {
  const currentKey = zonedDateKey(date, timeZone);
  let low = date.getTime();
  let high = low + 36 * 3600 * 1000;
  let best = high;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const cand = new Date(mid);
    if (zonedDateKey(cand, timeZone) !== currentKey) {
      best = mid;
      high = mid - 1000;
    } else {
      low = mid + 1000;
    }
  }
  const res = new Date(best);
  res.setUTCSeconds(0, 0);
  return res;
}

function ledgerEntry(budget, routeId) {
  if (!budget.routes) {
    budget.routes = {};
  }
  if (!budget.routes[routeId]) {
    budget.routes[routeId] = {
      requests_minute: 0,
      tokens_minute: 0,
      requests_available_at: "",
      tokens_available_at: "",
      requests_minute_key: "",
      requests_day: 0,
      requests_day_key: "",
      blocked_until: null,
      cost_used: 0,
      cost_cycle_key: "",
      cost_day_used: 0,
      cost_day_key: "",
      inflight: {},
    };
  } else if (!budget.routes[routeId].inflight) {
    // Preserve ledgers written by older Worker versions and by the Python CAS ledger.
    budget.routes[routeId].inflight = {};
  }
  return budget.routes[routeId];
}

function rollLedgerWindows(entry, route, now) {
  const mk = minuteKey(now);
  if (entry.requests_minute_key !== mk) {
    entry.requests_minute = 0;
    // `tokens_minute` is retained as legacy telemetry; TPM admission uses the continuous
    // `tokens_available_at` schedule below and must not reset at a wall-clock minute boundary.
    if (!entry.tokens_available_at) entry.tokens_minute = 0;
    entry.requests_minute_key = mk;
  }
  if (route.rpd != null) {
    const dk = zonedDateKey(now, routeResetTimezone(route));
    if (entry.requests_day_key !== dk) {
      entry.requests_day = 0;
      entry.requests_day_key = dk;
    }
  }
  if (
    route.tpm != null &&
    entry.tokens_available_at &&
    parseTime(entry.tokens_available_at) <= now.getTime()
  ) {
    // An oversized request's token debt has drained. Start a fresh burst budget.
    entry.tokens_available_at = "";
    entry.tokens_minute = 0;
  }
}

function reapExpiredInflight(entry, now) {
  for (const [owner, reservation] of Object.entries(entry.inflight || {})) {
    if (reservation?.expires_at && parseTime(reservation.expires_at) <= now.getTime()) {
      delete entry.inflight[owner];
    }
  }
}

function routeAvailable(entry, route, { requests, tokens }, now) {
  reapExpiredInflight(entry, now);
  if (entry.blocked_until && parseTime(entry.blocked_until) > now.getTime()) {
    return false;
  }
  if (route.rpm != null) {
    const nextRequestAt = entry.requests_available_at
      ? parseTime(entry.requests_available_at)
      : null;
    // New reservations use a continuous pacing schedule. The minute counter remains as a
    // compatibility fallback for ledgers written by older Worker versions.
    if (
      (nextRequestAt != null && nextRequestAt > now.getTime()) ||
      (nextRequestAt == null && entry.requests_minute + requests > route.rpm)
    ) {
      return false;
    }
  }
  if (route.rpd != null && entry.requests_day + requests > route.rpd) {
    return false;
  }
  if (
    route.tpm != null &&
    entry.tokens_available_at &&
    parseTime(entry.tokens_available_at) > now.getTime()
  ) {
    return false;
  }
  if (
    route.concurrency != null &&
    Object.keys(entry.inflight || {}).length + requests > route.concurrency
  ) {
    return false;
  }
  return true;
}

function reserveRouteCapacity(
  entry,
  route,
  { requests, tokens },
  { owner = null, reservedAt = null, expiresAt = null } = {},
) {
  const tokensMinuteBefore = entry.tokens_minute;
  const requestScheduleBefore = entry.requests_available_at || "";
  entry.requests_minute += requests;
  entry.tokens_minute += tokens;
  let requestScheduleAfter = requestScheduleBefore;
  if (route.rpm != null) {
    const intervalMs = 60_000 / route.rpm;
    const reservedMs = Date.parse(reservedAt || new Date().toISOString());
    const readyMs = requestScheduleBefore ? parseTime(requestScheduleBefore) : reservedMs;
    requestScheduleAfter = new Date(
      Math.max(reservedMs, readyMs) + intervalMs * requests,
    ).toISOString();
    entry.requests_available_at = requestScheduleAfter;
  }
  const tokenScheduleBefore = entry.tokens_available_at || "";
  const totalTokens = tokensMinuteBefore + tokens;
  const tokenScheduleAfter =
    route.tpm != null && !tokenScheduleBefore && totalTokens > route.tpm
      ? new Date(
          Date.parse(reservedAt || new Date().toISOString()) +
            (totalTokens * 60_000) / route.tpm,
        ).toISOString()
      : tokenScheduleBefore;
  if (tokenScheduleAfter) entry.tokens_available_at = tokenScheduleAfter;
  if (route.rpd != null) {
    entry.requests_day += requests;
  }
  if (owner) {
    entry.inflight[owner] = {
      requests,
      tokens,
      cost: 0,
      requests_available_at_before: requestScheduleBefore,
      requests_available_at_after: requestScheduleAfter,
      tokens_available_at_before: tokenScheduleBefore,
      tokens_available_at_after: tokenScheduleAfter,
      tokens_minute_before: tokensMinuteBefore,
      reserved_at: reservedAt,
      expires_at: expiresAt,
    };
  }
}

function providerRpm(route, dispatchLimits = DISPATCH_LIMITS) {
  const raw = dispatchLimits.providers?.[route?.provider]?.rpm;
  if (raw == null) return null;
  const rpm = Number(raw);
  if (!Number.isFinite(rpm) || rpm <= 0) {
    throw new Error(`provider ${route.provider} rpm must be a positive number`);
  }
  return rpm;
}

function providerLedgerEntry(budget, provider) {
  if (!budget.providers) budget.providers = {};
  if (!budget.providers[provider]) {
    budget.providers[provider] = { requests_available_at: "" };
  }
  return budget.providers[provider];
}

function providerAvailable(budget, route, now, dispatchLimits = DISPATCH_LIMITS) {
  if (providerRpm(route, dispatchLimits) == null) return true;
  const entry = budget.providers?.[route.provider];
  const nextRequestAt = entry?.requests_available_at
    ? parseTime(entry.requests_available_at)
    : null;
  return nextRequestAt == null || nextRequestAt <= now.getTime();
}

function reserveProviderCapacity(budget, route, requests, now, dispatchLimits = DISPATCH_LIMITS) {
  const rpm = providerRpm(route, dispatchLimits);
  if (rpm == null) return;
  const entry = providerLedgerEntry(budget, route.provider);
  const intervalMs = 60_000 / rpm;
  const nowMs = now.getTime();
  const readyMs = entry.requests_available_at
    ? parseTime(entry.requests_available_at)
    : nowMs;
  entry.requests_available_at = new Date(
    Math.max(nowMs, readyMs) + intervalMs * requests,
  ).toISOString();
}

async function releaseRouteReservation(bucket, routeId, owner) {
  if (!owner) return true;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const loaded = await getJson(bucket, DISPATCH_BUDGET_KEY);
    if (!loaded?.value) return true;
    const budget = loaded.value;
    const entry = ledgerEntry(budget, routeId);
    if (!entry.inflight?.[owner]) return true;
    delete entry.inflight[owner];
    try {
      if (
        await putJson(bucket, DISPATCH_BUDGET_KEY, budget, { etagMatches: loaded.etag })
      ) {
        return true;
      }
    } catch {
      // Retry after a sibling's CAS update.
    }
  }
  return false;
}

function timeoutSecondsForRecord(record, cfg) {
  return record?.policy?.timeout_class === "long"
    ? cfg.upstreamTimeoutSeconds
    : cfg.fastUpstreamTimeoutSeconds;
}

function nextRouteReset(entry, route, now, dispatchLimits = DISPATCH_LIMITS, budget = null) {
  const candidates = [];
  if (entry.blocked_until) {
    const blockedMs = parseTime(entry.blocked_until);
    if (blockedMs > now.getTime()) {
      candidates.push(new Date(blockedMs));
    }
  }
  const nextMinuteMs = Math.floor(now.getTime() / 60000) * 60000 + 60000;
  if (route.rpm != null) {
    if (entry.requests_available_at) {
      const requestReadyMs = parseTime(entry.requests_available_at);
      if (requestReadyMs > now.getTime()) candidates.push(new Date(requestReadyMs));
    } else if (entry.requests_minute >= route.rpm) {
      candidates.push(new Date(nextMinuteMs));
    }
  }
  if (providerRpm(route, dispatchLimits) != null) {
    const providerReady = budget?.providers?.[route.provider]?.requests_available_at;
    if (providerReady) {
      const providerReadyMs = parseTime(providerReady);
      if (providerReadyMs > now.getTime()) candidates.push(new Date(providerReadyMs));
    }
  }
  if (route.tpm != null && entry.tokens_available_at) {
    const tokenReadyMs = parseTime(entry.tokens_available_at);
    if (tokenReadyMs > now.getTime()) candidates.push(new Date(tokenReadyMs));
  }
  if (route.rpd != null && entry.requests_day >= route.rpd) {
    candidates.push(nextLocalMidnightUTC(now, routeResetTimezone(route)));
  }
  if (candidates.length === 0) {
    return new Date(nextMinuteMs);
  }
  // Every candidate is a necessary gate for this route. Returning the earliest one can cause an
  // immediate retry while another axis (for example RPD) remains exhausted; wait for all of them.
  return new Date(Math.max(...candidates.map((d) => d.getTime())));
}

function activePricing(route, now = new Date()) {
  const base = {
    input_per_token: Number(route.input_per_token || 0),
    output_per_token: Number(route.output_per_token || 0),
    windows: [],
  };
  const periods = Array.isArray(route.pricing?.periods) ? route.pricing.periods : [];
  const active = periods
    .filter((period) => Number.isFinite(Date.parse(period.effective_at || "")))
    .filter((period) => Date.parse(period.effective_at) <= now.getTime())
    .sort((left, right) => Date.parse(right.effective_at) - Date.parse(left.effective_at))[0];
  if (active) {
    if (active.input_per_token != null) base.input_per_token = Number(active.input_per_token);
    if (active.output_per_token != null) base.output_per_token = Number(active.output_per_token);
    base.windows = Array.isArray(active.windows) ? active.windows : [];
  } else if (Array.isArray(route.pricing?.windows)) {
    base.windows = route.pricing.windows;
  }
  return base;
}

function windowIsActive(window, now) {
  const bounds = windowBounds(window, now);
  return Boolean(bounds && bounds.start <= now && now < bounds.end);
}

function localTimeParts(now, timeZone) {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(now);
    const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return {
      year: Number(value.year),
      month: Number(value.month),
      day: Number(value.day),
      hour: Number(value.hour),
      minute: Number(value.minute),
    };
  } catch {
    return null;
  }
}

function shiftLocalDate(parts, days) {
  const date = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + days));
  return { year: date.getUTCFullYear(), month: date.getUTCMonth() + 1, day: date.getUTCDate() };
}

function localDateTimeToUTC(date, hour, minute, timeZone) {
  let candidate = Date.UTC(date.year, date.month - 1, date.day, hour, minute);
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const actual = localTimeParts(new Date(candidate), timeZone);
    if (!actual) return null;
    const desiredAsUTC = Date.UTC(date.year, date.month - 1, date.day, hour, minute);
    const actualAsUTC = Date.UTC(actual.year, actual.month - 1, actual.day, actual.hour, actual.minute);
    candidate += desiredAsUTC - actualAsUTC;
  }
  return new Date(candidate);
}

function windowBounds(window, now) {
  const timeZone = String(window?.tz || "UTC");
  const local = localTimeParts(now, timeZone);
  const start = String(window?.start || "").split(":").map(Number);
  const end = String(window?.end || "").split(":").map(Number);
  if (!local || ![...start, ...end].every(Number.isFinite)) return null;
  const currentMinutes = local.hour * 60 + local.minute;
  const startMinutes = start[0] * 60 + start[1];
  const endMinutes = end[0] * 60 + end[1];
  let startDate = { year: local.year, month: local.month, day: local.day };
  let endDate = startDate;
  if (startMinutes < endMinutes) {
    if (currentMinutes >= endMinutes) {
      startDate = shiftLocalDate(startDate, 1);
      endDate = startDate;
    }
  } else if (currentMinutes < endMinutes) {
    startDate = shiftLocalDate(startDate, -1);
  } else {
    endDate = shiftLocalDate(endDate, 1);
  }
  return {
    start: localDateTimeToUTC(startDate, start[0], start[1], timeZone),
    end: localDateTimeToUTC(endDate, end[0], end[1], timeZone),
  };
}

function nextCheapestPricingAt(route, now = new Date()) {
  const pricing = activePricing(route, now);
  const windows = pricing.windows.filter((window) => Number.isFinite(Number(window?.multiplier)));
  if (windows.length === 0) return null;
  const activeWindow = windows.find((window) => windowIsActive(window, now));
  const activeMultiplier = Number(activeWindow?.multiplier ?? 1);
  const cheapest = Math.min(1, ...windows.map((window) => Number(window.multiplier)));
  if (activeMultiplier <= cheapest) return null;
  if (cheapest === 1 && activeWindow) {
    return windowBounds(activeWindow, now)?.end || null;
  }
  const starts = windows
    .filter((window) => Number(window.multiplier) === cheapest)
    .map((window) => windowBounds(window, now)?.start)
    .filter((start) => start && start.getTime() > now.getTime());
  starts.sort((left, right) => left.getTime() - right.getTime());
  return starts[0] || null;
}

function routeCost(route, now = new Date()) {
  const pricing = activePricing(route, now);
  const window = pricing.windows.find((candidate) => windowIsActive(candidate, now));
  const multiplier = Number(window?.multiplier || 1);
  return (pricing.input_per_token + pricing.output_per_token) * multiplier;
}

function rankRoutes(routes, now = new Date()) {
  return [...routes].sort((a, b) => {
    if (Boolean(a.free) !== Boolean(b.free)) {
      return a.free ? -1 : 1;
    }
    const costA = routeCost(a, now);
    const costB = routeCost(b, now);
    if (costA !== costB) {
      return costA - costB;
    }
    return String(a.route_id || "").localeCompare(String(b.route_id || ""));
  });
}

function resolveProviderCredentials(env, route, dispatchLimits = DISPATCH_LIMITS) {
  const providerCfg = dispatchLimits.providers?.[route.provider];
  if (!providerCfg) {
    throw new Error(`no provider config compiled for provider ${route.provider}`);
  }
  const accounts = providerCfg.accounts || [];
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

function selectRouteForModel(budget, canonicalModel, policy, now, dispatchLimits = DISPATCH_LIMITS) {
  const logicalModel = canonicalModelName(canonicalModel, dispatchLimits);
  const routeIds = dispatchLimits.model_routes_map?.[logicalModel] || [];
  const candidates = routeIds.map((id) => dispatchLimits.routes_by_id[id]).filter(Boolean);
  const inputTokens = policy?.input_tokens_estimate ?? policy?.estimated_tokens ?? 1024;
  const outputTokens = policy?.output_token_budget ?? 0;
  const contextCompatible = candidates.filter(
    (route) =>
      inputTokens <= (route.input_context_limit || 32768) &&
      outputTokens <= (route.output_context_limit || 1024),
  );
  const freeRanked = rankRoutes(contextCompatible.filter((route) => route.free), now);
  const paidRanked = rankRoutes(contextCompatible.filter((route) => !route.free), now);
  const allowPaid = Boolean(policy?.allow_paid);
  const deadlineAt = policy?.deadline_at ? parseTime(policy.deadline_at) : null;
  const tokens = policy?.estimated_tokens || 1024;
  const reservationSize = { requests: 1, tokens };

  const tryRoutes = (ranked) => {
    for (const route of ranked) {
      const priceReadyAt = nextCheapestPricingAt(route, now);
      if (priceReadyAt && (deadlineAt == null || priceReadyAt <= deadlineAt)) {
        continue;
      }
      const entry = ledgerEntry(budget, route.route_id);
      rollLedgerWindows(entry, route, now);
      if (providerAvailable(budget, route, now, dispatchLimits)) {
        if (routeAvailable(entry, route, reservationSize, now)) {
          return { chosenRoute: route, entry };
        }
      }
    }
    return null;
  };

  const freePick = tryRoutes(freeRanked);
  if (freePick) {
    return freePick;
  }

  if (candidates.length > 0 && contextCompatible.length === 0) {
    return { chosenRoute: null, reason: "context_limit" };
  }

  if (freeRanked.length === 0 && paidRanked.length === 0) {
    return { chosenRoute: null, reason: "no_configured_route" };
  }

  if (freeRanked.length === 0) {
    if (!allowPaid) {
      return { chosenRoute: null, reason: "no_eligible_route" };
    }
    const paidPick = tryRoutes(paidRanked);
    return paidPick || { chosenRoute: null, reason: "no_capacity" };
  }

  if (!allowPaid) {
    return { chosenRoute: null, reason: "no_capacity" };
  }

  const freeResets = freeRanked.map((route) =>
    nextRouteReset(ledgerEntry(budget, route.route_id), route, now, dispatchLimits, budget).getTime(),
  );
  const earliestFreeReset = Math.min(...freeResets);

  const shouldElevateNow = deadlineAt != null && earliestFreeReset >= deadlineAt;
  if (!shouldElevateNow) {
    return { chosenRoute: null, reason: "no_capacity" };
  }

  const paidPick = tryRoutes(paidRanked);
  return paidPick || { chosenRoute: null, reason: "no_capacity" };
}

function selectRoute(budget, canonicalModels, policy, now, dispatchLimits = DISPATCH_LIMITS) {
  const models = Array.isArray(canonicalModels) ? canonicalModels : [canonicalModels];
  let lastSelection = { chosenRoute: null, reason: "no_configured_route" };
  let retryableSelection = null;
  for (const model of [...new Set(models)]) {
    const selection = selectRouteForModel(budget, model, policy, now, dispatchLimits);
    if (selection.chosenRoute) return selection;
    // Preserve "no capacity" when an earlier, valid model is merely full rather than allowing
    // a later unknown model to misclassify the durable request as permanently invalid.
    if (selection.reason === "no_capacity") {
      retryableSelection = selection;
    } else if (selection.reason !== "no_configured_route") {
      lastSelection = selection;
    }
  }
  return retryableSelection || lastSelection;
}

async function dispatchOne(env, fetchImpl = fetch, now = new Date()) {
  const result = await dispatchBatch(env, fetchImpl, now, 1);
  if (result.results && result.results.length > 0) {
    return result.results[0];
  }
  return { status: result.status, requestId: result.requestId, reason: result.reason };
}

async function dispatchBatch(
  env,
  fetchImpl = fetch,
  now = new Date(),
  maxBatch = DEFAULT_BATCH_CONCURRENCY,
  externalLeaseOwner = null,
  runDeadlineMs = null,
) {
  const bucket = env.LLM_QUEUE;
  if (!bucket) {
    return { status: "no_storage", count: 0 };
  }

  const cfg = config(env);
  let leaseOwner = externalLeaseOwner;
  let acquiredLock = false;
  if (!leaseOwner) {
    leaseOwner = crypto.randomUUID();
    acquiredLock = await acquireCronLease(bucket, now, leaseOwner, cfg.leaseDurationSeconds);
    if (!acquiredLock) {
      return { status: "lease_busy", count: 0 };
    }
  }

  try {
    let cursor = undefined;
    let claimable = [];
    let scanned = 0;
    let scanPrefix = PENDING_PREFIX;
    let legacyScan = false;

    while (scanned < cfg.maxQueueScan) {
      const batchSize = Math.min(100, cfg.maxQueueScan - scanned);
      let listResult = await bucket.list({ prefix: scanPrefix, cursor, limit: batchSize });
      const objects = listResult ? listResult.objects || [] : [];
      // Backward-compatible bridge for requests written before the pending-only index shipped.
      // Operators reindex once after deploy; new queues never take this bounded legacy path.
      if (objects.length === 0 && scanned === 0 && scanPrefix === PENDING_PREFIX) {
        scanPrefix = REQUEST_PREFIX;
        legacyScan = true;
        cursor = undefined;
        listResult = await bucket.list({ prefix: scanPrefix, cursor, limit: batchSize });
        objects.push(...(listResult ? listResult.objects || [] : []));
      }
      if (objects.length === 0) {
        break;
      }

      for (const obj of objects) {
        if (claimable.length >= cfg.maxQueueScan) break;
        scanned += 1;
        const id = obj.key.slice(scanPrefix.length).replace(/\.json$/, "");
        if (!id) continue;
        const meta = obj.customMetadata || {};
        if (legacyScan && meta.status && meta.status !== "pending") continue;
        if (
          legacyScan &&
          meta.status === "pending" &&
          meta.available_at &&
          parseTime(meta.available_at) > now.getTime()
        ) {
          continue;
        }
        const loaded = await getJson(bucket, requestKey(id));
        if (!loaded || !loaded.value) continue;
        const rec = loaded.value;
        if (rec.status !== "pending") {
          if (!legacyScan) await unmarkPending(bucket, id);
          continue;
        }
        if (rec.available_at && parseTime(rec.available_at) > now.getTime()) continue;
        const logicalModel = canonicalModelName(rec.model, DISPATCH_LIMITS);
        if (logicalModel !== rec.model) {
          rec.model = logicalModel;
          if (rec.request && typeof rec.request === "object") {
            rec.request.model = logicalModel;
          }
        }
        if (legacyScan) await markPending(bucket, id);
        claimable.push({ key: requestKey(id), object: loaded.object, record: rec });
      }

      if (claimable.length >= cfg.maxQueueScan || !listResult.truncated || !listResult.cursor) {
        break;
      }
      cursor = listResult.cursor;
    }

    if (claimable.length === 0) {
      return { status: "idle", count: 0 };
    }

    claimable.sort((a, b) => {
      const pA = a.record.policy?.submit_next ? 0 : 1;
      const pB = b.record.policy?.submit_next ? 0 : 1;
      if (pA !== pB) return pA - pB;
      const tA = a.record.policy?.timeout_class === "long" ? 1 : 0;
      const tB = b.record.policy?.timeout_class === "long" ? 1 : 0;
      if (tA !== tB) return tA - tB;
      return parseTime(a.record.created_at) - parseTime(b.record.created_at);
    });

    // Fast work normally drains first.  At the start of a fresh run, however, reserve one slot
    // for a long request when it can still complete inside this invocation's deadline.  Without
    // this escape hatch, a permanent fast backlog fills the only 10-second long-lane start
    // window and leaves every long request pending forever.
    if (
      runDeadlineMs != null &&
      Date.now() + (cfg.upstreamTimeoutSeconds + cfg.finalizationReserveSeconds) * 1000 <=
        runDeadlineMs
    ) {
      const firstLong = claimable.find((item) => item.record.policy?.timeout_class === "long");
      if (firstLong) {
        claimable = [firstLong, ...claimable.filter((item) => item !== firstLong)];
      }
    }

    let budgetLoaded = await getJson(bucket, DISPATCH_BUDGET_KEY);
    let budget = (budgetLoaded && budgetLoaded.value) || { version: 1, routes: {} };

    // Select candidates up to maxBatch with stacked in-memory ledger reservations
    const candidatesToDispatch = [];
    const batchFailures = [];
    let lastRequeuedId = null;
    let deadlineGuarded = false;

    for (const claimed of claimable) {
      if (candidatesToDispatch.length >= maxBatch) break;

      const selection = selectRoute(
        budget,
        claimed.record.policy?.allowed_models || claimed.record.model,
        claimed.record.policy,
        now,
      );
      if (!selection.chosenRoute) {
        if (selection.reason === "no_capacity") {
          await requeue(bucket, claimed, now);
          lastRequeuedId = claimed.record.id;
        } else {
          await saveFailure(bucket, claimed, 400, now, selection.reason);
          batchFailures.push({
            status: "failed",
            requestId: claimed.record.id,
            reason: selection.reason,
          });
        }
        continue;
      }

      const { chosenRoute } = selection;
      const timeoutSeconds = timeoutSecondsForRecord(claimed.record, cfg);
      if (
        runDeadlineMs != null &&
        Date.now() + (timeoutSeconds + cfg.finalizationReserveSeconds) * 1000 > runDeadlineMs
      ) {
        await requeue(bucket, claimed, now);
        lastRequeuedId = claimed.record.id;
        deadlineGuarded = true;
        continue;
      }
      let creds;
      try {
        creds = resolveProviderCredentials(env, chosenRoute);
      } catch (error) {
        await saveFailure(bucket, claimed, 500, now, "credential_resolution_failed");
        console.error(
          JSON.stringify({ event: "llm_dispatch_credentials_error", error: error?.message }),
        );
        batchFailures.push({
          status: "failed",
          requestId: claimed.record.id,
          reason: "credential_resolution_failed",
        });
        continue;
      }

      const reservationSize = {
        requests: 1,
        tokens: claimed.record.policy?.estimated_tokens || 1024,
      };

      const entry = ledgerEntry(budget, chosenRoute.route_id);
      rollLedgerWindows(entry, chosenRoute, now);
      if (!providerAvailable(budget, chosenRoute, now)) {
        await requeue(bucket, claimed, now);
        lastRequeuedId = claimed.record.id;
        continue;
      }
      if (!routeAvailable(entry, chosenRoute, reservationSize, now)) {
        await requeue(bucket, claimed, now);
        lastRequeuedId = claimed.record.id;
        continue;
      }

      const reservationOwner = claimed.record.id;
      reserveProviderCapacity(budget, chosenRoute, reservationSize.requests, now);
      reserveRouteCapacity(entry, chosenRoute, reservationSize, {
        owner: reservationOwner,
        reservedAt: now.toISOString(),
        expiresAt: new Date(
          runDeadlineMs || now.getTime() + timeoutSeconds * 1000,
        ).toISOString(),
      });
      claimed.record.attempts = (claimed.record.attempts || 0) + 1;
      claimed.record.processing_started_at = now.toISOString();

      candidatesToDispatch.push({
        claimed,
        chosenRoute,
        creds,
        reservationSize,
        reservationOwner,
        timeoutSeconds,
      });
    }

    if (candidatesToDispatch.length === 0) {
      // Route selection rolls minute/day/token windows in memory even when every request is
      // requeued. Persist those bookkeeping changes so the coordination object does not retain
      // stale quota keys until a later successful reservation.
      const before = JSON.stringify((budgetLoaded && budgetLoaded.value) || { version: 1, routes: {} });
      const after = JSON.stringify(budget);
      if (budgetLoaded && before !== after) {
        try {
          await putJson(bucket, DISPATCH_BUDGET_KEY, budget, {
            onlyIf: budgetLoaded ? { etagMatches: budgetLoaded.etag } : { etagDoesNotMatch: "*" },
          });
        } catch {
          // A sibling may have committed fresher rollover state; the request remains requeued.
        }
      }
      if (batchFailures.length > 0) {
        return {
          status: "failed",
          count: 0,
          requestId: batchFailures[0].requestId,
          reason: batchFailures[0].reason,
          results: batchFailures,
        };
      }
      return {
        status: deadlineGuarded ? "deadline_guard" : "no_capacity",
        count: 0,
        requestId: lastRequeuedId,
      };
    }

    // Atomic CAS write of the combined budget reservations
    let ledgerSaved = false;
    let freshCapacityFailed = false;
    for (let attempt = 0; attempt < 3 && !ledgerSaved; attempt += 1) {
      if (attempt > 0) {
        budgetLoaded = await getJson(bucket, DISPATCH_BUDGET_KEY);
        const freshBudget = (budgetLoaded && budgetLoaded.value) || { version: 1, routes: {} };
        for (const item of candidatesToDispatch) {
          if (!providerAvailable(freshBudget, item.chosenRoute, now)) {
            freshCapacityFailed = true;
            break;
          }
          const entry = ledgerEntry(freshBudget, item.chosenRoute.route_id);
          rollLedgerWindows(entry, item.chosenRoute, now);
          if (!routeAvailable(entry, item.chosenRoute, item.reservationSize, now)) {
            freshCapacityFailed = true;
            break;
          }
          reserveProviderCapacity(
            freshBudget,
            item.chosenRoute,
            item.reservationSize.requests,
            now,
          );
          reserveRouteCapacity(entry, item.chosenRoute, item.reservationSize, {
            owner: item.reservationOwner,
            reservedAt: now.toISOString(),
            expiresAt: new Date(
              runDeadlineMs || now.getTime() + item.timeoutSeconds * 1000,
            ).toISOString(),
          });
        }
        if (freshCapacityFailed) break;
        budget = freshBudget;
      }
      ledgerSaved = Boolean(
        await putJson(bucket, DISPATCH_BUDGET_KEY, budget, {
          onlyIf: budgetLoaded ? { etagMatches: budgetLoaded.etag } : { etagDoesNotMatch: "*" },
        }),
      );
    }

    if (!ledgerSaved) {
      console.error(
        JSON.stringify({
          event: "llm_dispatch_ledger_conflict",
          batchSize: candidatesToDispatch.length,
        }),
      );
      for (const item of candidatesToDispatch) {
        await requeue(bucket, item.claimed, now);
      }
      return {
        status: "no_capacity",
        count: 0,
        requestId: candidatesToDispatch[0].claimed.record.id,
      };
    }

    // Execute batch concurrently, retaining every sibling outcome.  One unexpected task/write
    // rejection must not discard successful results or leave its sibling reservations inflight.
    const settled = await Promise.allSettled(
      candidatesToDispatch.map(async ({ claimed, chosenRoute, creds, timeoutSeconds }) => {
        const upstreamPayload = {
          ...claimed.record.request,
          model: creds.upstreamModel,
          stream: false,
        };

        let response;
        const requestStartMs = Date.now();
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
            signal: AbortSignal.timeout(timeoutSeconds * 1000),
          });
        } catch (err) {
          const elapsedSec = Math.max(1, Math.round((Date.now() - requestStartMs) / 1000));
          const isTimeout =
            err?.name === "TimeoutError" ||
            err?.name === "AbortError" ||
            elapsedSec >= timeoutSeconds;
          const code = isTimeout ? "upstream_timeout" : "upstream_unreachable";
          const errorDetail = {
            code,
            message: isTimeout
              ? `Upstream LLM provider timed out after ${timeoutSeconds}s without completing response`
              : "upstream request failed before a response was received",
            model: chosenRoute.model,
            route_id: chosenRoute.route_id,
            duration_seconds: elapsedSec,
            timestamp: new Date().toISOString(),
          };
          if (claimed.record.attempts < cfg.maxAttempts) {
            await saveRetry(bucket, claimed, null, cfg, now, errorDetail);
            return { status: "retrying", requestId: claimed.record.id, error: code };
          }
          await saveFailure(bucket, claimed, null, now, code, errorDetail);
          return { status: "failed", requestId: claimed.record.id, reason: code };
        }

        if (!response.ok) {
          const elapsedSec = Math.max(1, Math.round((Date.now() - requestStartMs) / 1000));
          const errorDetail = {
            code: "upstream_error",
            status: response.status,
            model: chosenRoute.model,
            route_id: chosenRoute.route_id,
            duration_seconds: elapsedSec,
            timestamp: new Date().toISOString(),
          };
          if (retryableStatus(response.status) && claimed.record.attempts < cfg.maxAttempts) {
            await response.body?.cancel();
            await saveRetry(bucket, claimed, response, cfg, now, errorDetail);
            return {
              status: "retrying",
              requestId: claimed.record.id,
              upstreamStatus: response.status,
            };
          }
          await response.body?.cancel();
          await saveFailure(bucket, claimed, response.status, now, "upstream_error", errorDetail);
          return {
            status: "failed",
            requestId: claimed.record.id,
            upstreamStatus: response.status,
          };
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
            error instanceof BodyTooLargeError
              ? "upstream_response_too_large"
              : "invalid_upstream_json",
          );
          return {
            status: "failed",
            requestId: claimed.record.id,
            upstreamStatus: response.status,
          };
        }

        if (!responseJson || typeof responseJson !== "object" || Array.isArray(responseJson)) {
          await saveFailure(bucket, claimed, response.status, now, "invalid_upstream_json");
          return {
            status: "failed",
            requestId: claimed.record.id,
            upstreamStatus: response.status,
          };
        }

        const completed = {
          ...claimed.record,
          status: "completed",
          updated_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
          response: responseJson,
        };
        const saved = await putJson(bucket, claimed.key, completed, {
          etagMatches: claimed.object.etag,
          customMetadata: requestMetadata(completed),
        });
        if (saved) await unmarkPending(bucket, completed.id);
        return {
          status: "completed",
          requestId: claimed.record.id,
          upstreamStatus: response.status,
        };
      }),
    );

    const results = [];
    for (let index = 0; index < settled.length; index += 1) {
      const outcome = settled[index];
      const item = candidatesToDispatch[index];
      const released = await releaseRouteReservation(
        bucket,
        item.chosenRoute.route_id,
        item.reservationOwner,
      );
      if (!released) {
        console.error(
          JSON.stringify({
            event: "llm_dispatch_reservation_release_failed",
            request_id: item.claimed.record.id,
            route_id: item.chosenRoute.route_id,
          }),
        );
      }
      if (outcome.status === "fulfilled") {
        results.push(outcome.value);
        continue;
      }
      console.error(
        JSON.stringify({
          event: "llm_dispatch_task_error",
          request_id: item.claimed.record.id,
          error: outcome.reason?.name || "Error",
        }),
      );
      try {
        await saveFailure(bucket, item.claimed, 500, now, "worker_task_failed");
      } catch {
        // The request remains recoverable by the processing-timeout reaper if this write races.
      }
      results.push({
        status: "failed",
        requestId: item.claimed.record.id,
        reason: "worker_task_failed",
      });
    }

    const completedCount = results.filter((r) => r.status === "completed").length;
    return {
      status: "completed",
      count: candidatesToDispatch.length,
      completedCount,
      results,
    };
  } finally {
    if (acquiredLock) {
      await releaseCronLease(bucket, leaseOwner);
    }
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
    attempts: record.attempts || 0,
    available_at: record.available_at,
    last_error: record.last_error || null,
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
    const errCode = record.error?.code || "dispatch_failed";
    const errMessage = record.error?.message || "upstream LLM dispatch failed";
    return jsonResponse(
      {
        error: {
          message: errMessage,
          type: "upstream_error",
          code: errCode,
          route_id: record.error?.route_id,
          duration_seconds: record.error?.duration_seconds,
          attempts: record.attempts,
        },
      },
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

async function handleSchemaRetry(request, env, cfg, id) {
  if (!env.LLM_QUEUE) return plain(503, "dispatch storage is not configured");
  const loaded = await getJson(env.LLM_QUEUE, requestKey(id));
  const record = loaded?.value;
  if (!record) return plain(404, "request not found");
  if (record.status !== "completed") {
    return plain(409, "only a completed request can receive a schema correction");
  }
  const messages = record.request?.messages;
  if (!Array.isArray(messages) || messages.length === 0) {
    return plain(409, "completed request does not retain a retryable prompt");
  }
  // Pydantic remains authoritative in Python. The Worker only appends a generic correction,
  // avoiding transcript/model-output disclosure in the request-management API while preserving
  // the original response schema and all provider-routing policy fields.
  const correction = {
    role: "user",
    content: (
      "Retry this task. Your previous response failed local schema validation. " +
      "Return only one JSON object that exactly matches the requested response schema."
    ),
  };
  const normalized = normalizeChatRequest(
    {
      ...record.request,
      ...record.policy,
      model: record.model,
      messages: [...messages, correction],
    },
    cfg,
  );
  const idempotencyKey = request.headers.get("idempotency-key") || "";
  if (idempotencyKey.length > 256) throw new HttpError(400, "Idempotency-Key is too long");
  const replacement = await enqueue(env.LLM_QUEUE, normalized, cfg, new Date(), idempotencyKey);
  return responseForRecord(request, replacement);
}

async function reindexPendingRequests(bucket) {
  let cursor = undefined;
  let scanned = 0;
  let pending = 0;
  do {
    const listed = await bucket.list({ prefix: REQUEST_PREFIX, cursor, limit: 1000 });
    for (const obj of listed?.objects || []) {
      scanned += 1;
      const id = obj.key.slice(REQUEST_PREFIX.length).replace(/\.json$/, "");
      const meta = obj.customMetadata || {};
      if (meta.status && meta.status !== "pending") continue;
      const loaded = await getJson(bucket, obj.key);
      if (loaded?.value?.status === "pending") {
        await markPending(bucket, id);
        pending += 1;
      }
    }
    cursor = listed?.truncated ? listed.cursor : undefined;
  } while (cursor);
  return { scanned, pending };
}

async function handleRequest(request, env) {
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
    const requestedModel = url.searchParams.get("model") || cfg.model;
    const model = canonicalModelName(requestedModel, DISPATCH_LIMITS);
    let count = 0;
    let cursor = undefined;
    do {
      const listRes = await env.LLM_QUEUE.list({ prefix: REQUEST_PREFIX, cursor, limit: 1000 });
      const objects = listRes ? listRes.objects || [] : [];
      for (const obj of objects) {
        const meta = obj.customMetadata || {};
        if (meta.status) {
          if (
            meta.status === "pending" &&
            (!model || canonicalModelName(meta.model, DISPATCH_LIMITS) === model)
          ) {
            count += 1;
          }
          continue;
        }
        const loaded = await getJson(env.LLM_QUEUE, obj.key);
        if (loaded && loaded.value && loaded.value.status === "pending") {
          if (!model || canonicalModelName(loaded.value.model, DISPATCH_LIMITS) === model) {
            count += 1;
          }
        }
      }
      cursor = listRes && listRes.truncated ? listRes.cursor : undefined;
    } while (cursor);
    const routeIds = DISPATCH_LIMITS.model_routes_map?.[model] || [];
    const fastestRpm = routeIds
      .map((id) => {
        const route = DISPATCH_LIMITS.routes_by_id[id];
        const limits = [
          route?.rpm,
          DISPATCH_LIMITS.providers?.[route?.provider]?.rpm,
        ].filter((rpm) => typeof rpm === "number" && rpm > 0);
        return limits.length > 0 ? Math.min(...limits) : null;
      })
      .filter((rpm) => rpm != null);
    const perRequestSeconds = fastestRpm.length > 0 ? 60 / Math.max(...fastestRpm) : 60;
    const estSeconds = Math.ceil(count * perRequestSeconds);
    return jsonResponse({
      model,
      backlog_count: count,
      estimated_wait_seconds: estSeconds,
    });
  }

  if (url.pathname === "/v1/queue/reindex" && request.method === "POST") {
    if (!env.LLM_QUEUE) return plain(503, "dispatch storage is not configured");
    return jsonResponse(await reindexPendingRequests(env.LLM_QUEUE));
  }

  if (url.pathname === "/v1/chat/completions" && request.method === "POST") {
    return handleChatCompletion(request, env, cfg);
  }

  const match = url.pathname.match(/^\/v1\/requests\/(chatcmpl-[A-Za-z0-9-]{8,96})$/);
  if (match && request.method === "GET") {
    return handlePoll(request, env, match[1]);
  }

  const schemaRetry = url.pathname.match(
    /^\/v1\/requests\/(chatcmpl-[A-Za-z0-9-]{8,96})\/schema-retry$/,
  );
  if (schemaRetry && request.method === "POST") {
    try {
      return await handleSchemaRetry(request, env, cfg, schemaRetry[1]);
    } catch (error) {
      if (error instanceof HttpError) return plain(error.status, error.message);
      return plain(503, "schema correction could not be queued");
    }
  }

  if (url.pathname.startsWith("/v1/")) {
    return plain(405, "method or endpoint not allowed");
  }

  return plain(404, "not found");
}

async function runScheduled(env, { fetchImpl = fetch, nowMs = Date.now } = {}) {
  const bucket = env.LLM_QUEUE;
  if (!bucket) return { status: "no_storage", totalDispatched: 0 };
  const cfg = config(env);
  const startMs = nowMs();
  // `config()` guarantees the execution budget is shorter than the lease. The lease is acquired
  // after `startMs`, so its actual expiry is later than this fixed run deadline. Renewals protect
  // ownership between batches; they never extend the invocation deadline.
  const deadlineMs = startMs + cfg.maxExecutionSeconds * 1000;
  const leaseOwner = crypto.randomUUID();
  const acquiredLock = await acquireCronLease(
    bucket,
    new Date(nowMs()),
    leaseOwner,
    cfg.leaseDurationSeconds,
  );
  if (!acquiredLock) {
    console.log(JSON.stringify({ event: "llm_dispatch", status: "lease_busy" }));
    return { status: "lease_busy", totalDispatched: 0 };
  }

  let totalDispatched = 0;
  let status = "idle";
  try {
    while (nowMs() < deadlineMs && totalDispatched < cfg.maxTotalRequests) {
      if (
        deadlineMs - nowMs() <
        (cfg.fastUpstreamTimeoutSeconds + cfg.finalizationReserveSeconds) * 1000
      ) {
        status = "deadline_guard";
        break;
      }
      const renewed = await renewCronLease(
        bucket,
        new Date(nowMs()),
        leaseOwner,
        cfg.leaseDurationSeconds,
      );
      if (!renewed) {
        status = "lease_lost";
        console.error(JSON.stringify({ event: "llm_dispatch", status }));
        break;
      }
      const now = new Date(nowMs());
      const batchResult = await dispatchBatch(
        env,
        fetchImpl,
        now,
        cfg.batchConcurrency,
        leaseOwner,
        deadlineMs,
      );
      status = batchResult.status;
      if (
        batchResult.status === "idle" ||
        batchResult.status === "no_capacity" ||
        batchResult.status === "deadline_guard" ||
        batchResult.count === 0
      ) {
        console.log(
          JSON.stringify({
            event: "llm_dispatch_batch",
            status: batchResult.status,
            count: 0,
          }),
        );
        break;
      }
      totalDispatched += batchResult.count;
      status = "dispatched";
      console.log(
        JSON.stringify({
          event: "llm_dispatch_batch",
          status,
          count: batchResult.count,
          totalDispatched,
        }),
      );
    }
  } catch (error) {
    status = "error";
    console.error(JSON.stringify({ event: "llm_dispatch", status, error: error?.name || "Error" }));
  } finally {
    await releaseCronLease(bucket, leaseOwner);
  }
  return { status, totalDispatched };
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },

  scheduled(_controller, env) {
    return runScheduled(env);
  },
};

export {
  acquireCronLease,
  config,
  dispatchBatch,
  dispatchOne,
  handleRequest,
  nextLocalMidnightUTC,
  nextRouteReset,
  normalizeChatRequest,
  rankRoutes,
  releaseCronLease,
  renewCronLease,
  runScheduled,
  requestKey,
  resolveProviderCredentials,
  routeAvailable,
  selectRoute,
};
