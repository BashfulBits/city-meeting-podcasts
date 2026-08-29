import DISPATCH_LIMITS from "./dispatch_limits.json" with { type: "json" };

const REQUEST_PREFIX = "requests/";
// Ready markers are ordered by their eligibility timestamp. R2 lists keys lexicographically, so
// finding a bounded set of candidate requests is one fixed-size list, irrespective of historical
// request count. `pending/` is retained only for the external one-time migration.
const READY_PREFIX = "ready/";
// Unified coordination object combining the single-runner cron lease and rate ledger.
const DISPATCH_COORDINATOR_KEY = "state/dispatch_coordinator.json";
// Legacy key aliases retained for fixture and migration compatibility.
const CRON_LOCK_KEY = "locks/cron.json";
const DISPATCH_BUDGET_KEY = "state/dispatch_budget.json";
// Chapter-tag batches can legitimately carry several large, source-backed chapter windows. Keep
// an explicit cap (the Worker parses JSON in memory) but avoid rejecting ordinary long meetings.
const DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024;
const DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const DEFAULT_MAX_UPSTREAM_ERROR_BYTES = 8 * 1024;
const MAX_UPSTREAM_ERROR_MESSAGE_CHARS = 2048;
const MAX_UPSTREAM_ERROR_KEYS = 32;
const MAX_UPSTREAM_ERROR_KEY_CHARS = 64;
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
// Cron triggers on the Workers Free plan have a 10ms CPU allowance.  Ready markers keep queue
// inspection bounded; upstream wait time is not CPU time, so one provider call can make progress
// without returning to the old prompt-scanning design.
const DEFAULT_BATCH_CONCURRENCY = 1;
const DEFAULT_MAX_TOTAL_REQUESTS = 1;
const DEFAULT_READY_LOOKAHEAD = 500;
// Maximum delay an admitted candidate is allowed to wait in memory before initiating its
// upstream HTTP/TCP connection. Kept small (20s) so the connection is established well within
// Cloudflare's 30-second idle/execution limit while enabling high-RPM models (e.g. Gemma 4 at 30 RPM = 2s)
// to run concurrently in a single batch.
const DEFAULT_MAX_IN_BATCH_STAGGER_SECONDS = 20;
// A head whose route is merely pacing will be eligible again shortly.  Rewriting its canonical
// record and moving its marker costs four R2 operations -- more than dispatching a request -- so
// short waits are skipped in memory and the marker is left where it is.  Only a wait longer than
// this is worth relocating the marker out of the lookahead window.
const DEFAULT_DEFER_IN_PLACE_SECONDS = 10 * 60;
const DEFAULT_PROFILE_SAMPLE_RATE = 0.1;
// Every timezone comes from the compiled route catalog, so the key space is bounded by
// configuration; the cap is defence in depth only.
const MAX_DATE_FORMATTER_CACHE = 64;
const DATE_FORMATTER_CACHE = new Map();
// Reused across every decode below; `new TextDecoder()` is not free and the options never vary.
const TEXT_DECODER = new TextDecoder();
const ACTIVE_PRICING_CACHE = new WeakMap();

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

function sleep(ms) {
  if (ms <= 0) return Promise.resolve();
  return new Promise((resolve) => setTimeout(resolve, ms));
}

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

function nonNegativeNumber(value, fallback, { integer = false } = {}) {
  if (value === undefined || value === null || value === "") {
    return fallback;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) {
    return fallback;
  }
  return integer ? Math.max(0, Math.floor(parsed)) : parsed;
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

function sampleRate(value, fallback = DEFAULT_PROFILE_SAMPLE_RATE) {
  if (value === undefined || value === null || value === "") return fallback;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.min(1, Math.max(0, parsed)) : fallback;
}

function requiredString(value, name) {
  const result = String(value || "").trim();
  if (!result) {
    throw new Error(`${name} is required`);
  }
  return result;
}

function stripSchemaKeys(value, keys) {
  if (Array.isArray(value)) return value.map((item) => stripSchemaKeys(item, keys));
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([key]) => !keys.has(key))
      .map(([key, item]) => [key, stripSchemaKeys(item, keys)]),
  );
}

function upstreamRequestForRoute(record, route, upstreamModel) {
  const payload = {
    ...record.request,
    model: upstreamModel,
    stream: false,
  };
  const responseFormat = payload.response_format;
  const stripKeys = new Set(route?.structured_output_schema_strip_keys || []);
  const schema = responseFormat?.json_schema?.schema;
  if (responseFormat?.type === "json_schema" && schema && stripKeys.size > 0) {
    payload.response_format = {
      ...responseFormat,
      json_schema: {
        ...responseFormat.json_schema,
        schema: stripSchemaKeys(schema, stripKeys),
      },
    };
  }
  return payload;
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

// The catalog carries each route once, keyed by ID, with the canonical model string implied by
// which model_routes_map entry pointed at it rather than repeated on the route itself -- attach it
// here so the returned route matches what selection and dispatch expect to read.
function routeFromCatalog(routeId, dispatchLimits = DISPATCH_LIMITS, model = undefined) {
  const stored = dispatchLimits?.routes_by_id?.[routeId];
  return stored ? { ...stored, model } : null;
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
    deferInPlaceSeconds: positiveNumber(
      env.DEFER_IN_PLACE_SECONDS,
      DEFAULT_DEFER_IN_PLACE_SECONDS,
    ),
    maxInBatchStaggerSeconds: nonNegativeNumber(
      env.MAX_IN_BATCH_STAGGER_SECONDS,
      DEFAULT_MAX_IN_BATCH_STAGGER_SECONDS,
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
    profileSampleRate: sampleRate(env.PROFILE_SAMPLE_RATE),
    ...leaseAndTimeoutConfig(env),
  };
  if (result.maxExecutionSeconds >= result.leaseDurationSeconds) {
    throw new Error(
      `MAX_EXECUTION_SECONDS (${result.maxExecutionSeconds}) must be less than ` +
        `LEASE_DURATION_SECONDS (${result.leaseDurationSeconds})`,
    );
  }
  if (result.maxInBatchStaggerSeconds >= result.maxExecutionSeconds) {
    throw new Error(
      `MAX_IN_BATCH_STAGGER_SECONDS (${result.maxInBatchStaggerSeconds}) must be less than ` +
        `MAX_EXECUTION_SECONDS (${result.maxExecutionSeconds})`,
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

function profileNow() {
  return typeof performance !== "undefined" && typeof performance.now === "function"
    ? performance.now()
    : Date.now();
}

function profileElapsed(startedAt) {
  return Math.max(0, Math.round((profileNow() - startedAt) * 100) / 100);
}

function finishDispatchProfile(profile, startedAt) {
  return { ...profile, total_ms: profileElapsed(startedAt) };
}

function profiledResult(result, profile, startedAt) {
  return { ...result, profile: finishDispatchProfile(profile, startedAt) };
}

async function readTextLimited(stream, limit) {
  return (await readTextLimitedWithMetadata(stream, limit)).text;
}

async function readTextLimitedWithMetadata(stream, limit) {
  if (!stream) {
    return { text: "", bytes: 0 };
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

  // A single-chunk body is the common case for provider responses; decoding it directly avoids
  // allocating and copying a second buffer the same size as the response.
  if (chunks.length === 1) {
    return { text: TEXT_DECODER.decode(chunks[0]), bytes };
  }
  const result = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { text: TEXT_DECODER.decode(result), bytes };
}

async function readDiagnosticTextLimited(stream, limit) {
  if (!stream) {
    return { text: "", bytes: 0, truncated: false };
  }
  const reader = stream.getReader();
  const chunks = [];
  let bytes = 0;
  let truncated = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      const previousBytes = bytes;
      bytes += chunk.byteLength;
      if (bytes > limit) {
        const allowed = Math.max(0, limit - previousBytes);
        if (allowed > 0) chunks.push(chunk.slice(0, allowed));
        truncated = true;
        try {
          await reader.cancel();
        } catch {
          // The prefix is still useful if cancellation races stream closure.
        }
        break;
      }
      chunks.push(chunk);
    }
  } finally {
    reader.releaseLock();
  }

  const result = new Uint8Array(Math.min(bytes, limit));
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { text: TEXT_DECODER.decode(result), bytes, truncated };
}

function boundedDiagnosticMessage(value) {
  return String(value)
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_UPSTREAM_ERROR_MESSAGE_CHARS);
}

function boundedDiagnosticContentType(value) {
  if (typeof value !== "string") return undefined;
  const contentType = value.split(";", 1)[0].trim().toLowerCase();
  return contentType ? contentType.slice(0, 128) : undefined;
}

function diagnosticKeys(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  return Object.keys(value)
    .slice(0, MAX_UPSTREAM_ERROR_KEYS)
    .map((key) =>
      /^[A-Za-z0-9_.:-]{1,64}$/.test(key)
        ? key.slice(0, MAX_UPSTREAM_ERROR_KEY_CHARS)
        : "<redacted>",
    );
}

function addDiagnosticShape(result, prefix, value) {
  if (!value || typeof value !== "object") return;
  if (Array.isArray(value)) {
    result[`${prefix}_type`] = "array";
    return;
  }
  result[`${prefix}_keys`] = diagnosticKeys(value);
  if (Object.keys(value).length > MAX_UPSTREAM_ERROR_KEYS) {
    result[`${prefix}_keys_truncated`] = true;
  }
}

const DIAGNOSTIC_FIELD_NAMES = [
  "code",
  "status",
  "message",
  "detail",
  "error_message",
  "errorMessage",
  "type",
  "reason",
];

function hasDiagnosticField(value) {
  return (
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    DIAGNOSTIC_FIELD_NAMES.some((field) => field in value)
  );
}

function findDiagnosticSource(value, path = "root", depth = 0) {
  if (typeof value === "string" && path !== "root") {
    return { source: { message: value }, path };
  }
  if (!value || typeof value !== "object") return null;
  if (hasDiagnosticField(value)) return { source: value, path };
  if (depth >= 3) return null;

  for (const key of ["error", "details", "cause", "response"]) {
    const child = value[key];
    const childPath = `${path}.${key}`;
    if (Array.isArray(child)) {
      for (const [index, item] of child.slice(0, 4).entries()) {
        const found = findDiagnosticSource(item, `${childPath}[${index}]`, depth + 1);
        if (found) return found;
      }
    } else {
      const found = findDiagnosticSource(child, childPath, depth + 1);
      if (found) return found;
    }
  }
  return null;
}

function boundedDiagnosticBody(value) {
  return String(value)
    .replace(/[\u0000\u0008\u000b\u000c\u000e-\u001f\u007f]/g, " ")
    .slice(0, DEFAULT_MAX_UPSTREAM_ERROR_BYTES);
}

function parseUpstreamError(text, metadata = {}, { persistBodyPreview = true } = {}) {
  const result = {
    ...metadata,
    format: text ? "json" : "empty",
  };
  if (persistBodyPreview && text) {
    result.body_preview = boundedDiagnosticBody(text);
  }
  if (!text) return result;

  let body;
  try {
    body = JSON.parse(text);
  } catch {
    return {
      ...metadata,
      format: "non_json",
      ...(persistBodyPreview ? { body_preview: boundedDiagnosticBody(text) } : {}),
    };
  }

  result.json_type = Array.isArray(body) ? "array" : body === null ? "null" : typeof body;
  if (body && typeof body === "object" && !Array.isArray(body)) {
    addDiagnosticShape(result, "top_level", body);
    if (body.error && typeof body.error === "object") {
      addDiagnosticShape(result, "error", body.error);
    }
  }

  const found = findDiagnosticSource(body);
  if (!found) return result;
  if (found.path !== "root") result.diagnostic_path = found.path;

  const source = found.source;
  if (!source || typeof source !== "object" || Array.isArray(source)) return result;

  if (typeof source.code === "string") {
    result.code = boundedDiagnosticMessage(source.code);
  } else if (Number.isFinite(source.code)) {
    result.code = source.code;
  }
  if (typeof source.status === "string") {
    result.status = boundedDiagnosticMessage(source.status);
  }
  for (const key of ["message", "detail", "error_message", "errorMessage"]) {
    if (typeof source[key] === "string") {
      result.message = boundedDiagnosticMessage(source[key]);
      break;
    }
  }
  if (typeof source.type === "string") {
    result.provider_type = boundedDiagnosticMessage(source.type);
  }
  if (typeof source.reason === "string") {
    result.provider_reason = boundedDiagnosticMessage(source.reason);
  }
  return result;
}

async function readUpstreamError(response, options = {}) {
  const contentType = boundedDiagnosticContentType(response.headers.get("content-type"));
  const metadata = contentType ? { content_type: contentType } : {};
  try {
    const { text, bytes, truncated } = await readDiagnosticTextLimited(
      response.body,
      DEFAULT_MAX_UPSTREAM_ERROR_BYTES,
    );
    if (truncated) {
      return {
        ...metadata,
        bytes,
        format: "too_large",
        truncated: true,
        ...(options.persistBodyPreview ? { body_preview: boundedDiagnosticBody(text) } : {}),
      };
    }
    return parseUpstreamError(text, { ...metadata, bytes }, options);
  } catch (error) {
    return {
      ...metadata,
      format: error instanceof BodyTooLargeError ? "too_large" : "unreadable",
    };
  }
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

function readyTimeKey(value, fallback = 0) {
  const milliseconds = parseTime(value) || fallback;
  return String(Math.max(0, milliseconds)).padStart(15, "0");
}

function readyPriority(record) {
  if (record.policy?.submit_next) return "0-urgent";
  return record.policy?.timeout_class === "long" ? "2-long" : "1-fast";
}

function readyKey(record) {
  const createdAt = parseTime(record.created_at);
  return (
    `${READY_PREFIX}${readyTimeKey(record.available_at, createdAt)}-` +
    `${readyPriority(record)}-${readyTimeKey(record.created_at)}-${record.id}.json`
  );
}

function readyMarker(record) {
  // Do not put the prompt in the index.  The marker contains just enough routing policy to select
  // a route before the scheduler reads the potentially multi-megabyte canonical request object.
  return {
    version: 1,
    id: record.id,
    model: record.model,
    created_at: record.created_at,
    available_at: record.available_at,
    policy: record.policy || {},
  };
}

function readyMarkerMetadata(record) {
  const marker = readyMarker(record);
  return {
    id: String(marker.id),
    status: "pending",
    ready_version: String(marker.version),
    model: String(marker.model || ""),
    created_at: String(marker.created_at || ""),
    available_at: String(marker.available_at || ""),
    policy: JSON.stringify(marker.policy),
  };
}

// The bounded lookahead lists several markers but usually dispatches the first one.  `policy` is
// the only field that costs a JSON parse, and eligibility is decided from `available_at` alone, so
// it is parsed on first access instead of for every listed marker.  An unparseable policy reads as
// `null` via `readyMarkerPolicyValid`, matching the previous whole-marker rejection.
function readyMarkerFromMetadata(metadata) {
  if (
    !metadata ||
    metadata.ready_version !== "1" ||
    typeof metadata.id !== "string" ||
    typeof metadata.model !== "string" ||
    typeof metadata.available_at !== "string"
  ) {
    return null;
  }
  const raw = metadata.policy;
  let policy;
  let parsed = false;
  return {
    version: 1,
    id: metadata.id,
    model: metadata.model,
    created_at: metadata.created_at || "",
    available_at: metadata.available_at,
    get policy() {
      if (!parsed) {
        parsed = true;
        try {
          const value = raw ? JSON.parse(raw) : {};
          policy = value && typeof value === "object" && !Array.isArray(value) ? value : null;
        } catch {
          policy = null;
        }
      }
      return policy;
    },
  };
}

function readyMarkerPolicyValid(record) {
  return record?.policy != null;
}

async function markReady(bucket, record) {
  const key = readyKey(record);
  await putJson(bucket, key, readyMarker(record), {
    customMetadata: readyMarkerMetadata(record),
  });
  return key;
}

async function unmarkReady(bucket, key) {
  if (key) await bucket.delete(key);
}

// R2's delete accepts a key list, so a batch removes every finished marker in one operation
// instead of one per request.  A marker that outlives its terminal record is self-healing --
// `loadReadyClaim` drops any marker whose canonical record is no longer pending -- so collecting
// these until the batch ends is safe even if the invocation dies before the delete lands.
async function unmarkReadyBatch(bucket, keys) {
  const unique = [...new Set((keys || []).filter(Boolean))];
  if (unique.length === 0) return;
  if (unique.length === 1) {
    await bucket.delete(unique[0]);
    return;
  }
  await bucket.delete(unique);
}

async function replaceReadyMarker(bucket, record, oldKey = null, pendingDeletes = null) {
  const newKey = await markReady(bucket, record);
  if (oldKey && oldKey !== newKey) {
    if (pendingDeletes) {
      pendingDeletes.push(oldKey);
    } else {
      await unmarkReady(bucket, oldKey);
    }
  }
  return newKey;
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
    // Legacy upstream-model and provider-qualified selectors are folded into `model_aliases` by
    // the compiler's `_worker_legacy_model_map`, so `canonicalModelName` above has already
    // resolved them. (A `legacy_model_map` lookup used to sit here; the compiler emits no such
    // key, so it was unreachable.) Only the Worker's own configured default is left to map.
    if (requestedModel === cfg.upstreamModel) {
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

  // `model_routing` (compiled from config/provider_limits.yml's `model_routing`) is a general,
  // config-driven overflow map: every model eligible for this request -- the request's own model
  // plus any explicit `allowed_models` the caller passed -- becomes eligible for its own
  // configured target model(s) too. `citypods.compute.llm_scheduler.select_route` expands every
  // entry in `policy.allowed_models` the same way, so this must too, not just `canonicalModel`.
  // `selectRoute` below already tries `policy.allowed_models` in order and falls through past a
  // merely-full earlier model, so appending here is enough -- no other call site needs to know.
  const allowedModelsBase = policy.allowed_models || [canonicalModel];
  const expandedAllowedModels = expandModelsWithRouting(
    allowedModelsBase,
    dispatchLimits,
    configuredModels,
  );
  if (expandedAllowedModels.length > allowedModelsBase.length) {
    policy.allowed_models = [...new Set(expandedAllowedModels)];
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
    if (stored.status === "pending") await markReady(bucket, stored);
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

  // Write the index first. A crash before the canonical write leaves a harmless marker that the
  // scheduler removes; the reverse order would strand an otherwise-valid pending request forever.
  const markerKey = await markReady(bucket, record);
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
        // A concurrent writer may have created the canonical record after this invocation wrote
        // its marker. Restore that writer's marker before rejecting this idempotency collision.
        if (stored.status === "pending") {
          const storedMarkerKey = await markReady(bucket, stored);
          if (storedMarkerKey !== markerKey) await unmarkReady(bucket, markerKey);
        } else {
          await unmarkReady(bucket, markerKey);
        }
        throw new HttpError(409, "idempotency key collision with different payload or policy");
      }
      if (stored.status === "pending") {
        const storedMarkerKey = await markReady(bucket, stored);
        if (storedMarkerKey !== markerKey) await unmarkReady(bucket, markerKey);
      } else {
        await unmarkReady(bucket, markerKey);
      }
      return stored;
    }
    throw new HttpError(503, "could not persist queued request");
  }

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

async function saveRetry(
  bucket, claimed, response, cfg, now, errorDetail = null, pendingMarkerDeletes = null,
) {
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
  const saved = await putJson(bucket, claimed.key, updated, {
    etagMatches: claimed.object.etag,
    customMetadata: requestMetadata(updated),
  });
  if (saved) await replaceReadyMarker(bucket, updated, claimed.readyKey, pendingMarkerDeletes);
}

async function saveFailure(
  bucket,
  claimed,
  status,
  now,
  code = "upstream_error",
  errorDetail = null,
  pendingMarkerDeletes = null,
) {
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
  if (!saved) return;
  const markerKey = claimed.readyKey || readyKey(claimed.record);
  if (pendingMarkerDeletes) {
    pendingMarkerDeletes.push(markerKey);
    return;
  }
  await unmarkReady(bucket, markerKey);
}

async function requeue(
  bucket,
  claimed,
  now,
  availableAt = now.toISOString(),
  { releaseAttempt = false } = {},
) {
  const released = {
    ...claimed.record,
    status: "pending",
    attempts: releaseAttempt
      ? Math.max(0, (claimed.record.attempts || 0) - 1)
      : claimed.record.attempts || 0,
    updated_at: now.toISOString(),
    available_at: availableAt,
    processing_started_at: undefined,
  };
  const saved = await putJson(bucket, claimed.key, released, {
    etagMatches: claimed.object.etag,
    customMetadata: requestMetadata(released),
  });
  if (saved) await replaceReadyMarker(bucket, released, claimed.readyKey);
}

// ---------------------------------------------------------------------------------------------
// Cron lease -- single-runner guarantee for dispatch.
// ---------------------------------------------------------------------------------------------

// ---------------------------------------------------------------------------------------------
// Dispatch coordinator & lease (`state/dispatch_coordinator.json`)
// ---------------------------------------------------------------------------------------------

function coordinatorLeaseMetadata(lease) {
  return {
    lease_version: "1",
    owner: String(lease?.owner || ""),
    acquired_at: String(lease?.acquired_at || ""),
    renewed_at: String(lease?.renewed_at || ""),
    released_at: String(lease?.released_at || ""),
    expires_at: String(lease?.expires_at || ""),
  };
}

function cronLeaseMetadata(lease) {
  return coordinatorLeaseMetadata(lease);
}

async function getCoordinatorLease(bucket) {
  if (typeof bucket.head === "function") {
    const head = await bucket.head(DISPATCH_COORDINATOR_KEY);
    const metadata = head?.customMetadata;
    if (head && metadata?.lease_version === "1" && metadata.owner && metadata.expires_at) {
      return {
        object: head,
        etag: head.etag,
        value: {
          owner: metadata.owner,
          acquired_at: metadata.acquired_at || "",
          renewed_at: metadata.renewed_at || "",
          released_at: metadata.released_at || "",
          expires_at: metadata.expires_at,
        },
      };
    }
  }
  const loaded = await getJson(bucket, DISPATCH_COORDINATOR_KEY);
  if (!loaded?.value) return null;
  return {
    object: loaded.object,
    etag: loaded.etag,
    value: loaded.value.lease || null,
    coordinator: loaded.value,
  };
}

async function getCronLease(bucket) {
  return getCoordinatorLease(bucket);
}

async function acquireCoordinator(bucket, now, owner, leaseDurationSeconds, handle = null) {
  const loaded = await getJson(bucket, DISPATCH_COORDINATOR_KEY);
  const coordinator = (loaded && loaded.value) || {
    version: 1,
    lease: null,
    routes: {},
    providers: {},
  };
  if (!coordinator.routes) coordinator.routes = {};
  if (!coordinator.providers) coordinator.providers = {};

  if (coordinator.lease?.expires_at) {
    if (
      parseTime(coordinator.lease.expires_at) > now.getTime() &&
      coordinator.lease.owner !== owner
    ) {
      return { acquired: false, status: "lease_busy" };
    }
  }

  const lease = {
    owner,
    acquired_at: now.toISOString(),
    renewed_at: now.toISOString(),
    released_at: "",
    expires_at: new Date(now.getTime() + leaseDurationSeconds * 1000).toISOString(),
  };
  coordinator.lease = lease;

  const putRes = await putJson(bucket, DISPATCH_COORDINATOR_KEY, coordinator, {
    onlyIf: loaded ? { etagMatches: loaded.etag } : { etagDoesNotMatch: "*" },
    customMetadata: coordinatorLeaseMetadata(lease),
  });
  if (!putRes) {
    return { acquired: false, status: "lease_busy" };
  }

  if (handle) {
    handle.etag = putRes.etag;
    handle.coordinator = coordinator;
    handle.owner = owner;
  }

  return {
    acquired: true,
    coordinator,
    etag: putRes.etag,
  };
}

async function acquireCronLease(bucket, now, owner, leaseDurationSeconds, handle = null) {
  const coordHandle = { etag: null, coordinator: null, owner };
  const res = await acquireCoordinator(bucket, now, owner, leaseDurationSeconds, coordHandle);
  if (!res.acquired) return false;
  if (handle) {
    handle.etag = coordHandle.etag;
    handle.lease = coordHandle.coordinator.lease;
    handle.coordinator = coordHandle.coordinator;
    handle.owner = owner;
  }
  return true;
}

async function releaseCoordinator(bucket, owner, handle = null) {
  try {
    const tombstoneLease = (lease) => ({
      ...(lease || {}),
      owner,
      released_at: new Date().toISOString(),
      expires_at: new Date(0).toISOString(),
    });

    if (handle?.etag && handle.coordinator?.lease?.owner === owner) {
      const releasedLease = tombstoneLease(handle.coordinator.lease);
      handle.coordinator.lease = releasedLease;
      await putJson(bucket, DISPATCH_COORDINATOR_KEY, handle.coordinator, {
        etagMatches: handle.etag,
        customMetadata: coordinatorLeaseMetadata(releasedLease),
      });
      return;
    }

    const current = await getJson(bucket, DISPATCH_COORDINATOR_KEY);
    if (current?.value?.lease?.owner === owner) {
      const releasedLease = tombstoneLease(current.value.lease);
      current.value.lease = releasedLease;
      await putJson(bucket, DISPATCH_COORDINATOR_KEY, current.value, {
        etagMatches: current.etag,
        customMetadata: coordinatorLeaseMetadata(releasedLease),
      });
    }
  } catch {
    // Best-effort release; expiry is the backstop.
  }
}

async function releaseCronLease(bucket, owner, handle = null) {
  return releaseCoordinator(bucket, owner, handle);
}

async function renewCoordinator(bucket, now, owner, leaseDurationSeconds, handle = null) {
  const renewalFrom = (lease) => ({
    ...(lease || {}),
    owner,
    renewed_at: now.toISOString(),
    expires_at: new Date(now.getTime() + leaseDurationSeconds * 1000).toISOString(),
  });

  if (handle?.etag && handle.coordinator?.lease?.owner === owner) {
    if (parseTime(handle.coordinator.lease.expires_at) > now.getTime()) {
      const renewedLease = renewalFrom(handle.coordinator.lease);
      handle.coordinator.lease = renewedLease;
      try {
        const putRes = await putJson(bucket, DISPATCH_COORDINATOR_KEY, handle.coordinator, {
          etagMatches: handle.etag,
          customMetadata: coordinatorLeaseMetadata(renewedLease),
        });
        if (putRes) {
          handle.etag = putRes.etag;
          return true;
        }
      } catch {
        // Fall through to authoritative read below.
      }
    }
    handle.etag = null;
  }

  const current = await getJson(bucket, DISPATCH_COORDINATOR_KEY);
  if (
    !current ||
    current.value?.lease?.owner !== owner ||
    parseTime(current.value?.lease?.expires_at) <= now.getTime()
  ) {
    return false;
  }

  const renewedLease = renewalFrom(current.value.lease);
  current.value.lease = renewedLease;
  try {
    const putRes = await putJson(bucket, DISPATCH_COORDINATOR_KEY, current.value, {
      etagMatches: current.etag,
      customMetadata: coordinatorLeaseMetadata(renewedLease),
    });
    if (putRes && handle) {
      handle.etag = putRes.etag;
      handle.coordinator = current.value;
    }
    return Boolean(putRes);
  } catch {
    return false;
  }
}

async function renewCronLease(bucket, now, owner, leaseDurationSeconds, handle = null) {
  return renewCoordinator(bucket, now, owner, leaseDurationSeconds, handle);
}

// ---------------------------------------------------------------------------------------------
// Per-route/per-account ledger (`state/dispatch_budget.json`)
// ---------------------------------------------------------------------------------------------

function minuteKey(date) {
  return date.toISOString().slice(0, 16); // "YYYY-MM-DDTHH:MM", UTC
}

function dateFormatter(timeZone, withTime) {
  const key = `${withTime ? "local" : "date"}:${timeZone}`;
  const cached = DATE_FORMATTER_CACHE.get(key);
  // `undefined` means "not looked up yet"; a cached `null` means this zone already failed and must
  // not pay the ICU rejection cost again.
  if (cached !== undefined) return cached;
  try {
    const formatter = new Intl.DateTimeFormat("en-US", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      ...(withTime ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } : {}),
    });
    if (DATE_FORMATTER_CACHE.size < MAX_DATE_FORMATTER_CACHE) {
      DATE_FORMATTER_CACHE.set(key, formatter);
    }
    return formatter;
  } catch {
    if (DATE_FORMATTER_CACHE.size < MAX_DATE_FORMATTER_CACHE) {
      DATE_FORMATTER_CACHE.set(key, null);
    }
    return null;
  }
}

function zonedDateKey(date, timeZone = "UTC") {
  try {
    const formatter = dateFormatter(timeZone, false);
    if (!formatter) return date.toISOString().slice(0, 10);
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

/**
 * Escalating cooldown for a route whose most recent attempt came back HTTP 402 (payment
 * required / provider-side quota exhausted at the billing layer) -- a distinct signal from 429:
 * 429 means "too fast," already handled by requests_available_at/Retry-After above; 402 means
 * "no budget left until some future reset," which no amount of pacing fixes. Returns epoch ms;
 * callers store it as an ISO string in entry.blocked_until, same as every other ledger timestamp
 * here (see parseTime). Mirrors workers/llm-dispatch-v2/src/pacing.js's function of the same
 * name exactly -- kept as a duplicate, not a shared import, per this repo's existing convention
 * of each Worker directory being self-contained (review/44 Phase 1).
 *
 * `streak` is the count AFTER this occurrence; a success resets it to 0 elsewhere (ledgerEntry
 * callers, on the completed-response path), so streak only grows across consecutive 402s with no
 * successful call on that route between them. First occurrence assumes a same-day quota reset
 * might simply fix it, escalating through a week and then a month for a streak that keeps
 * recurring after each cooldown expires -- i.e., a real billing problem -- and stays pinned to
 * "start of next month" for any further streak past that, rather than escalating indefinitely.
 */
function paymentRequiredBackoffUntil(streak, now) {
  const d = new Date(now);
  if (streak <= 1) {
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
  }
  if (streak === 2) {
    const daysUntilNextMonday = ((8 - d.getUTCDay()) % 7) || 7;
    return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + daysUntilNextMonday);
  }
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
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
      payment_required_streak: 0,
    };
  } else {
    if (!budget.routes[routeId].inflight) {
      // Preserve ledgers written by older Worker versions and by the Python CAS ledger.
      budget.routes[routeId].inflight = {};
    }
    if (budget.routes[routeId].payment_required_streak === undefined) {
      budget.routes[routeId].payment_required_streak = 0;
    }
  }
  return budget.routes[routeId];
}

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
  let reaped = false;
  for (const [owner, reservation] of Object.entries(entry.inflight || {})) {
    // No current writer creates an `inflight` entry at all -- durable usage is committed up front
    // and the ceiling is tracked in memory -- so anything here was left by an older Worker
    // version or a crashed predecessor. An entry with no `expires_at` must still be reaped:
    // `routeAvailable` counts every entry toward `route.concurrency`, so skipping it would let a
    // malformed record consume a slot permanently with nothing able to clear it.
    const expiresAt = reservation?.expires_at;
    if (!expiresAt || parseTime(expiresAt) <= now.getTime()) {
      delete entry.inflight[owner];
      reaped = true;
    }
  }
  return reaped;
}

// `changes` collects the durable side effects of an otherwise read-only admission check.  Rolling
// a minute/day window is derived from `now` and is recomputed on every load, so it never needs to
function routePacingDelayMs(entry, route, nowMs) {
  if (entry.blocked_until && parseTime(entry.blocked_until) > nowMs) {
    return Infinity;
  }
  let delayMs = 0;
  if (route.rpm != null && entry.requests_available_at) {
    const readyMs = parseTime(entry.requests_available_at);
    if (readyMs > nowMs) {
      delayMs = Math.max(delayMs, readyMs - nowMs);
    }
  }
  if (route.tpm != null && entry.tokens_available_at) {
    const tokenReadyMs = parseTime(entry.tokens_available_at);
    if (tokenReadyMs > nowMs) {
      delayMs = Math.max(delayMs, tokenReadyMs - nowMs);
    }
  }
  return delayMs;
}

function providerPacingDelayMs(budget, route, nowMs, dispatchLimits = DISPATCH_LIMITS) {
  if (providerRpm(route, dispatchLimits) == null) return 0;
  const entry = budget.providers?.[route.provider];
  const nextRequestAt = entry?.requests_available_at
    ? parseTime(entry.requests_available_at)
    : null;
  if (nextRequestAt != null && nextRequestAt > nowMs) {
    return nextRequestAt - nowMs;
  }
  return 0;
}

// Check whether a route can take one more candidate immediately or within maxStaggerMs.
function routeAvailable(entry, route, { requests, tokens }, now, batch = null, maxStaggerMs = 0) {
  if (reapExpiredInflight(entry, now) && batch) {
    batch.reaped = true;
  }
  if (entry.blocked_until && parseTime(entry.blocked_until) > now.getTime()) {
    return false;
  }
  if (route.rpm != null) {
    const nextRequestAt = entry.requests_available_at
      ? parseTime(entry.requests_available_at)
      : null;
    // Pacing allows delays within maxStaggerMs. The minute counter remains as a
    // compatibility fallback for ledgers written by older Worker versions.
    if (
      (nextRequestAt != null && nextRequestAt - now.getTime() > maxStaggerMs) ||
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
    parseTime(entry.tokens_available_at) - now.getTime() > maxStaggerMs
  ) {
    return false;
  }
  if (route.concurrency != null) {
    // The cron lease guarantees this is the only invocation dispatching, so a route's concurrency
    // ceiling is the durable inflight entries left by a crashed predecessor plus the candidates
    // this batch has already admitted.  The latter is tracked in memory: writing it to R2 and
    // deleting it again costs a round trip that nothing outside this invocation can observe.
    const admittedHere = batch?.admitted?.get(route.route_id) || 0;
    if (Object.keys(entry.inflight || {}).length + admittedHere + requests > route.concurrency) {
      return false;
    }
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
  let tokenScheduleAfter = tokenScheduleBefore;
  if (route.tpm != null) {
    const tokenIntervalMs = (tokens * 60_000) / route.tpm;
    const reservedMs = Date.parse(reservedAt || new Date().toISOString());
    const tokenReadyMs = tokenScheduleBefore ? parseTime(tokenScheduleBefore) : reservedMs;
    tokenScheduleAfter = new Date(
      Math.max(reservedMs, tokenReadyMs) + tokenIntervalMs,
    ).toISOString();
    entry.tokens_available_at = tokenScheduleAfter;
  }
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

function providerAvailable(budget, route, now, dispatchLimits = DISPATCH_LIMITS, maxStaggerMs = 0) {
  if (providerRpm(route, dispatchLimits) == null) return true;
  const entry = budget.providers?.[route.provider];
  const nextRequestAt = entry?.requests_available_at
    ? parseTime(entry.requests_available_at)
    : null;
  return nextRequestAt == null || nextRequestAt - now.getTime() <= maxStaggerMs;
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
  const cached = ACTIVE_PRICING_CACHE.get(route);
  const nowMs = now.getTime();
  if (cached?.nowMs === nowMs) return cached.value;
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
  ACTIVE_PRICING_CACHE.set(route, { nowMs, value: base });
  return base;
}

function windowIsActive(window, now) {
  const bounds = windowBounds(window, now);
  return Boolean(bounds && bounds.start <= now && now < bounds.end);
}

function localTimeParts(now, timeZone) {
  try {
    const formatter = dateFormatter(timeZone, true);
    if (!formatter) return null;
    const parts = formatter.formatToParts(now);
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
  const actual = localTimeParts(new Date(candidate), timeZone);
  if (!actual || actual.year !== date.year || actual.month !== date.month || actual.day !== date.day ||
      actual.hour !== hour || actual.minute !== minute) {
    return null;
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
  const periods = Array.isArray(route.pricing?.periods) ? route.pricing.periods : [];
  const nextPeriodAt = periods
    .map((period) => Date.parse(period?.effective_at || ""))
    .filter((effectiveAt) => Number.isFinite(effectiveAt) && effectiveAt > now.getTime())
    .sort((left, right) => left - right)[0];
  let cheapestAt = null;
  if (cheapest === 1 && activeWindow) {
    cheapestAt = windowBounds(activeWindow, now)?.end || null;
  } else {
    const starts = windows
      .filter((window) => Number(window.multiplier) === cheapest)
      .map((window) => windowBounds(window, now)?.start)
      .filter((start) => start && start.getTime() > now.getTime());
    starts.sort((left, right) => left.getTime() - right.getTime());
    cheapestAt = starts[0] || null;
  }
  const candidates = [cheapestAt, Number.isFinite(nextPeriodAt) ? new Date(nextPeriodAt) : null]
    .filter(Boolean);
  candidates.sort((left, right) => left.getTime() - right.getTime());
  return candidates[0] || null;
}

function routeCost(route, now = new Date()) {
  const pricing = activePricing(route, now);
  const window = pricing.windows.find((candidate) => windowIsActive(candidate, now));
  const multiplier = Number(window?.multiplier ?? 1);
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
  const directUrlString = `${apiBase}${chatPath}`;

  let parsedDirectUrl;
  try {
    parsedDirectUrl = new URL(directUrlString);
  } catch {
    throw new Error(`provider ${route.provider} api_base/chat_path is not a valid URL`);
  }
  if (parsedDirectUrl.protocol !== "https:") {
    throw new Error(`provider ${route.provider} api_base must use HTTPS`);
  }

  let url = directUrlString;
  let aiGatewayBase = String(env?.AI_GATEWAY_BASE_URL || "").trim().replace(/\/+$/, "");
  if (!aiGatewayBase && env?.CLOUDFLARE_ACCOUNT_ID && env?.AI_GATEWAY_ID) {
    const accountId = String(env.CLOUDFLARE_ACCOUNT_ID).trim();
    const gatewayId = String(env.AI_GATEWAY_ID).trim();
    if (accountId && gatewayId) {
      aiGatewayBase = `https://gateway.ai.cloudflare.com/v1/${encodeURIComponent(accountId)}/${encodeURIComponent(gatewayId)}`;
    }
  }

  if (aiGatewayBase) {
    let parsedGateway;
    try {
      parsedGateway = new URL(aiGatewayBase);
    } catch {
      throw new Error("AI_GATEWAY_BASE_URL is not a valid URL");
    }
    if (parsedGateway.protocol !== "https:") {
      throw new Error("AI_GATEWAY_BASE_URL must use HTTPS");
    }
    const gatewaySlug = providerCfg.ai_gateway_slug || route.provider;
    const gatewayPath = providerCfg.ai_gateway_chat_path
      ? `${providerCfg.ai_gateway_chat_path}${parsedDirectUrl.search}`
      : `${parsedDirectUrl.pathname}${parsedDirectUrl.search}`;
    url = `${aiGatewayBase}/${gatewaySlug}${gatewayPath}`;
  }

  return { apiKey, url, upstreamModel: route.upstream_model, usesGateway: Boolean(aiGatewayBase) };
}

function eligibleRoutesForModel(canonicalModel, policy, now, dispatchLimits = DISPATCH_LIMITS) {
  const logicalModel = canonicalModelName(canonicalModel, dispatchLimits);
  const routeIds = dispatchLimits.model_routes_map?.[logicalModel] || [];
  const candidates = routeIds
    .map((routeId) => routeFromCatalog(routeId, dispatchLimits, logicalModel))
    .filter(Boolean);
  const inputTokens = policy?.input_tokens_estimate ?? policy?.estimated_tokens ?? 1024;
  const outputTokens = policy?.output_token_budget ?? 0;
  const contextCompatible = candidates.filter(
    (route) =>
      inputTokens <= (route.input_context_limit || 32768) &&
      outputTokens <= (route.output_context_limit || 1024),
  );
  return {
    candidates,
    contextCompatible,
    freeRanked: rankRoutes(contextCompatible.filter((route) => route.free), now),
    paidRanked: rankRoutes(contextCompatible.filter((route) => !route.free), now),
  };
}

function expandModelsWithRouting(models, dispatchLimits, configuredModels = null) {
  const configured = configuredModels || Object.keys(dispatchLimits.model_routes_map || {});
  const requested = models.map((model) => canonicalModelName(model, dispatchLimits));
  const overflow = requested.flatMap((model) =>
    (dispatchLimits.model_routing?.[model] || [])
      .map((target) => canonicalModelName(target, dispatchLimits))
      .filter((target) => configured.includes(target)),
  );
  // Explicit alternates are peer choices supplied by the caller. Keep all of them ahead of
  // config-injected overflow routes, which are only fallbacks after every requested model is
  // unavailable. Interleaving each source with its overflow made Mistral -> Gemini hide a later
  // explicit Llama alternate indefinitely whenever Gemini retained capacity.
  return [...new Set([...requested, ...overflow])];
}

function selectRouteForModel(
  budget,
  canonicalModel,
  policy,
  now,
  dispatchLimits = DISPATCH_LIMITS,
  batch = null,
  maxStaggerMs = 0,
) {
  const { candidates, contextCompatible, freeRanked, paidRanked } = eligibleRoutesForModel(
    canonicalModel,
    policy,
    now,
    dispatchLimits,
  );
  const allowPaid = Boolean(policy?.allow_paid);
  const deadlineAt = policy?.deadline_at ? parseTime(policy.deadline_at) : null;
  const tokens = policy?.estimated_tokens || 1024;
  const reservationSize = { requests: 1, tokens };
  const nowMs = now.getTime();
  // In-batch stagger is only permitted when candidates have already been admitted in this batch.
  // The first candidate in a batch must be immediately ready (delayMs === 0) at the current time.
  const hasAdmittedCandidates = Boolean(
    batch?.admitted && [...batch.admitted.values()].some((c) => c > 0),
  );
  const effectiveMaxStaggerMs = hasAdmittedCandidates ? maxStaggerMs : 0;

  const tryRoutes = (ranked) => {
    for (const route of ranked) {
      const priceReadyAt = nextCheapestPricingAt(route, now);
      if (priceReadyAt && (deadlineAt == null || priceReadyAt <= deadlineAt)) {
        continue;
      }
      const entry = ledgerEntry(budget, route.route_id);
      rollLedgerWindows(entry, route, now);
      if (providerAvailable(budget, route, now, dispatchLimits, effectiveMaxStaggerMs)) {
        if (routeAvailable(entry, route, reservationSize, now, batch, effectiveMaxStaggerMs)) {
          const routeDelay = routePacingDelayMs(entry, route, nowMs);
          const provDelay = providerPacingDelayMs(budget, route, nowMs, dispatchLimits);
          const delayMs = Math.max(0, routeDelay, provDelay);
          const dispatchAtMs = nowMs + delayMs;
          return { chosenRoute: route, entry, dispatchAtMs, delayMs };
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

function selectRoute(
  budget,
  canonicalModels,
  policy,
  now,
  dispatchLimits = DISPATCH_LIMITS,
  batch = null,
  maxStaggerMs = 0,
) {
  const baseModels = Array.isArray(canonicalModels) ? canonicalModels : [canonicalModels];
  const configuredModels = Object.keys(dispatchLimits.model_routes_map || {});
  // Dynamically expand model_routing (compiled from config/provider_limits.yml's `model_routing`)
  // so existing resident R2 records and unexpanded policy.allowed_models can drain onto overflow
  // routes (e.g. Mistral Medium -> Gemini 3.5 Flash Lite) without requiring canonical record migration.
  const models = expandModelsWithRouting(baseModels, dispatchLimits, configuredModels);
  let lastSelection = { chosenRoute: null, reason: "no_configured_route" };
  let retryableSelection = null;
  for (const model of models) {
    const selection = selectRouteForModel(
      budget,
      model,
      policy,
      now,
      dispatchLimits,
      batch,
      maxStaggerMs,
    );
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

function nextCapacityRetryAt(budget, canonicalModels, policy, now, dispatchLimits = DISPATCH_LIMITS) {
  const baseModels = Array.isArray(canonicalModels) ? canonicalModels : [canonicalModels];
  const configuredModels = Object.keys(dispatchLimits.model_routes_map || {});
  const models = expandModelsWithRouting(baseModels, dispatchLimits, configuredModels);
  const allowPaid = Boolean(policy?.allow_paid);
  const deadlineAt = policy?.deadline_at ? parseTime(policy.deadline_at) : null;
  const resets = [];
  const nextEligibleAt = (route) => {
    const capacityAt = nextRouteReset(
      ledgerEntry(budget, route.route_id),
      route,
      now,
      dispatchLimits,
      budget,
    ).getTime();
    const priceReadyAt = nextCheapestPricingAt(route, now);
    const priceAt =
      priceReadyAt && (deadlineAt == null || priceReadyAt.getTime() <= deadlineAt)
        ? priceReadyAt.getTime()
        : now.getTime();
    return Math.max(capacityAt, priceAt);
  };
  for (const model of [...new Set(models)]) {
    const { freeRanked, paidRanked } = eligibleRoutesForModel(
      model,
      policy,
      now,
      dispatchLimits,
    );
    const freeResets = freeRanked.map(nextEligibleAt);
    resets.push(...freeResets);

    // This must mirror selectRouteForModel: paid routes become relevant only when no free route
    // exists, or when waiting for every free route would miss the caller's deadline.
    const canUsePaid =
      allowPaid &&
      (freeRanked.length === 0 ||
        (deadlineAt != null && Math.min(...freeResets) >= deadlineAt));
    if (!canUsePaid) continue;
    for (const route of paidRanked) {
      resets.push(nextEligibleAt(route));
    }
  }
  return new Date(resets.length > 0 ? Math.min(...resets) : now.getTime() + 60_000);
}

async function loadReadyHeads(bucket, now, limit = DEFAULT_READY_LOOKAHEAD) {
  const listed = await bucket.list({
    prefix: READY_PREFIX,
    limit,
    include: ["customMetadata"],
  });
  const heads = [];
  let invalidCount = 0;
  let future = false;
  for (const object of listed?.objects || []) {
    let record = readyMarkerFromMetadata(object.customMetadata);
    if (!record) {
      let marker;
      try {
        marker = await getJson(bucket, object.key);
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
        await unmarkReady(bucket, object.key);
        invalidCount += 1;
        continue;
      }
      record = marker?.value;
      // A marker read from its JSON body carries `policy` as a plain property.  Normalize the
      // omitted case so the shared validity check does not mistake a legacy marker for a corrupt
      // one; metadata-backed markers already normalize this inside their accessor.
      if (record && record.policy === undefined) {
        record.policy = {};
      }
    }
    if (!record || record.version !== 1 || typeof record.id !== "string") {
      await unmarkReady(bucket, object.key);
      invalidCount += 1;
      continue;
    }
    // readyKey() orders by available_at, so a future marker means every later marker is future
    // too. Stop before inspecting more markers, while still allowing malformed/stale markers
    // before it to be repaired in this pass. New markers carry this routing data in list metadata;
    // legacy markers fall back to reading their JSON body above.
    if (parseTime(record.available_at) > now.getTime()) {
      future = true;
      break;
    }
    heads.push({ key: object.key, record });
  }
  return { heads, future, invalidCount };
}

async function loadReadyClaim(bucket, head, now) {
  const loaded = await getJson(bucket, requestKey(head.record.id));
  if (!loaded?.value || loaded.value.status !== "pending") {
    await unmarkReady(bucket, head.key);
    return null;
  }
  const canonical = loaded.value;
  if (canonical.available_at && parseTime(canonical.available_at) > now.getTime()) {
    await replaceReadyMarker(bucket, canonical, head.key);
    return null;
  }
  const logicalModel = canonicalModelName(canonical.model, DISPATCH_LIMITS);
  if (logicalModel !== canonical.model) {
    canonical.model = logicalModel;
    if (canonical.request && typeof canonical.request === "object") {
      canonical.request.model = logicalModel;
    }
  }
  // A moved marker can survive a crash between creating the replacement and deleting the old
  // key.  Never dispatch based on stale routing policy or a stale eligibility timestamp.
  if (
    head.key !== readyKey(canonical) ||
    canonicalModelName(head.record.model, DISPATCH_LIMITS) !== logicalModel ||
    JSON.stringify(head.record.policy || {}) !== JSON.stringify(canonical.policy || {})
  ) {
    await replaceReadyMarker(bucket, canonical, head.key);
    return null;
  }
  return { key: requestKey(canonical.id), object: loaded.object, record: canonical, readyKey: head.key };
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
  externalCoordinatorHandle = null,
  sleepImpl = sleep,
  nowMs = Date.now,
) {
  const batchStartedAt = profileNow();
  const batchProfile = {};
  const bucket = env.LLM_QUEUE;
  if (!bucket) {
    return {
      status: "no_storage",
      count: 0,
      profile: finishDispatchProfile(batchProfile, batchStartedAt),
    };
  }

  const cfg = config(env);
  let leaseOwner = externalLeaseOwner || externalCoordinatorHandle?.owner;
  let acquiredLock = false;
  const coordinatorHandle = externalCoordinatorHandle || {
    etag: null,
    coordinator: null,
    owner: null,
  };
  if (!leaseOwner || !coordinatorHandle.coordinator) {
    leaseOwner = crypto.randomUUID();
    coordinatorHandle.owner = leaseOwner;
    const leaseStartedAt = profileNow();
    const acquireRes = await acquireCoordinator(
      bucket,
      now,
      leaseOwner,
      cfg.leaseDurationSeconds,
      coordinatorHandle,
    );
    batchProfile.lease_acquire_ms = profileElapsed(leaseStartedAt);
    if (!acquireRes.acquired) {
      return {
        status: "lease_busy",
        count: 0,
        profile: finishDispatchProfile(batchProfile, batchStartedAt),
      };
    }
    acquiredLock = true;
  }

  try {
    // List a small, fixed number of compact markers.  Route selection happens from marker
    // metadata first, so a provider/model at capacity does not force a full prompt read or stop
    // the batch at the queue head.  Canonical requests are read only for candidates that can be
    // dispatched or must be requeued.
    const readyStartedAt = profileNow();
    const ready = await loadReadyHeads(bucket, now);
    batchProfile.ready_heads_ms = profileElapsed(readyStartedAt);
    if (ready.heads.length === 0) {
      return {
        status: ready.invalidCount > 0 ? "index_repaired" : "idle",
        count: 0,
        profile: finishDispatchProfile(batchProfile, batchStartedAt),
      };
    }

    let coordinator = coordinatorHandle.coordinator;
    let coordinatorLoadedEtag = coordinatorHandle.etag;
    batchProfile.budget_load_ms = 0;

    // Select candidates up to maxBatch with stacked in-memory coordinator reservations
    const candidatesToDispatch = [];
    const batchFailures = [];
    let lastRequeuedId = null;
    let deadlineGuarded = false;
    let repairedCount = 0;
    // Heads left in place because their route frees up soon; used only to guarantee forward
    // progress if the whole lookahead window turns out to be blocked.
    const deferredInPlace = [];
    // `reaped` records durable ledger side effects observed while deciding admission, used to
    // decide whether a batch that dispatched nothing still owes R2 a write.  `admitted` counts the
    // candidates this batch has taken per route, so a route's concurrency ceiling is enforced
    // without a durable reservation.
    const batchState = { reaped: false, admitted: new Map() };
    const maxStaggerMs = cfg.maxInBatchStaggerSeconds * 1000;
    // Accumulated across the whole batch: marker deletes from the candidate-prep loop
    // (permanent failures) and the finalize closures (completed/failed dispatches). A single
    // unmarkReadyBatch call flushes them all in one R2 DELETE instead of N individual deletes.
    const pendingMarkerDeletes = [];

    const candidatePrepareStartedAt = profileNow();
    for (const head of ready.heads) {
      if (candidatesToDispatch.length >= maxBatch) break;

      // First touch of `policy` parses it.  A marker whose policy is unreadable carries no usable
      // routing state, so it is repaired out of the index exactly as a malformed marker body is.
      if (!readyMarkerPolicyValid(head.record)) {
        await unmarkReady(bucket, head.key);
        repairedCount += 1;
        continue;
      }

      const candidateProfile = {};
      const candidateStartedAt = profileNow();
      const routeSelectStartedAt = profileNow();
      const selection = selectRoute(
        coordinator,
        head.record.policy?.allowed_models || head.record.model,
        head.record.policy,
        now,
        DISPATCH_LIMITS,
        batchState,
        maxStaggerMs,
      );
      candidateProfile.route_select_ms = profileElapsed(routeSelectStartedAt);
      let claimed;
      if (!selection.chosenRoute) {
        if (selection.reason === "no_capacity") {
          // Leave it alone and look at the next head.  This marker was already returned by the
          // one `list` above, so re-examining it next minute is free. If no candidate can be
          // dispatched, the batch-level fallback relocates at most one head at the end,
          // preventing an unbounded burst of R2 operations that breaches the Free CPU limit.
          deferredInPlace.push(head);
          continue;
        }
        const claimStartedAt = profileNow();
        claimed = await loadReadyClaim(bucket, head, now);
        candidateProfile.claim_ms = profileElapsed(claimStartedAt);
        if (!claimed) continue;
        {
          await saveFailure(
            bucket, claimed, 400, now, selection.reason, null, pendingMarkerDeletes,
          );
          batchFailures.push({
            status: "failed",
            requestId: claimed.record.id,
            reason: selection.reason,
          });
        }
        continue;
      }

      const claimStartedAt = profileNow();
      claimed = await loadReadyClaim(bucket, head, now);
      candidateProfile.claim_ms = profileElapsed(claimStartedAt);
      if (!claimed) continue;

      const { chosenRoute, dispatchAtMs = now.getTime(), delayMs = 0 } = selection;
      const timeoutSeconds = timeoutSecondsForRecord(claimed.record, cfg);
      if (
        runDeadlineMs != null &&
        now.getTime() + delayMs + (timeoutSeconds + cfg.finalizationReserveSeconds) * 1000 > runDeadlineMs
      ) {
        await requeue(bucket, claimed, now, new Date(now.getTime() + 60_000).toISOString());
        lastRequeuedId = claimed.record.id;
        deadlineGuarded = true;
        continue;
      }
      let creds;
      try {
        const credentialsStartedAt = profileNow();
        creds = resolveProviderCredentials(env, chosenRoute);
        candidateProfile.credentials_ms = profileElapsed(credentialsStartedAt);
      } catch (error) {
        await saveFailure(
          bucket, claimed, 500, now, "credential_resolution_failed", null,
          pendingMarkerDeletes,
        );
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

      const entry = ledgerEntry(coordinator, chosenRoute.route_id);
      rollLedgerWindows(entry, chosenRoute, now);
      if (!providerAvailable(coordinator, chosenRoute, now, DISPATCH_LIMITS, maxStaggerMs)) {
        await requeue(
          bucket,
          claimed,
          now,
          nextRouteReset(
            ledgerEntry(coordinator, chosenRoute.route_id),
            chosenRoute,
            now,
            DISPATCH_LIMITS,
            coordinator,
          ).toISOString(),
        );
        lastRequeuedId = claimed.record.id;
        continue;
      }
      if (!routeAvailable(entry, chosenRoute, reservationSize, now, batchState, maxStaggerMs)) {
        await requeue(
          bucket,
          claimed,
          now,
          nextRouteReset(entry, chosenRoute, now, DISPATCH_LIMITS, coordinator).toISOString(),
        );
        lastRequeuedId = claimed.record.id;
        continue;
      }

      const reservationOwner = claimed.record.id;
      reserveProviderCapacity(
        coordinator,
        chosenRoute,
        reservationSize.requests,
        new Date(dispatchAtMs),
      );
      reserveRouteCapacity(entry, chosenRoute, reservationSize, {
        reservedAt: new Date(dispatchAtMs).toISOString(),
      });
      batchState.admitted.set(
        chosenRoute.route_id,
        (batchState.admitted.get(chosenRoute.route_id) || 0) + reservationSize.requests,
      );
      claimed.record.attempts = (claimed.record.attempts || 0) + 1;
      claimed.record.processing_started_at = now.toISOString();

      candidateProfile.stagger_delay_ms = delayMs;
      candidatesToDispatch.push({
        claimed,
        chosenRoute,
        creds,
        reservationSize,
        reservationOwner,
        timeoutSeconds,
        dispatchAtMs,
        delayMs,
        profile: candidateProfile,
        dispatchStartedAt: candidateStartedAt,
      });
    }
    batchProfile.candidate_prepare_ms = profileElapsed(candidatePrepareStartedAt);

    if (candidatesToDispatch.length === 0) {
      if (coordinatorLoadedEtag && batchState.reaped) {
        try {
          const putRes = await putJson(bucket, DISPATCH_COORDINATOR_KEY, coordinator, {
            onlyIf: { etagMatches: coordinatorLoadedEtag },
            customMetadata: coordinatorLeaseMetadata(coordinator.lease),
          });
          if (putRes) {
            coordinatorHandle.etag = putRes.etag;
            coordinatorHandle.coordinator = coordinator;
          }
        } catch {
          // A sibling may have committed fresher rollover state; the request remains requeued.
        }
      }
      // Flush any marker deletes collected during candidate preparation (permanent failures)
      // before any early return so they are not silently dropped.
      if (pendingMarkerDeletes.length > 0) {
        try {
          await unmarkReadyBatch(bucket, pendingMarkerDeletes);
        } catch {
          // Best-effort; self-healing will clean stale markers on the next cron tick.
        }
      }
      if (batchFailures.length > 0) {
        return {
          status: "failed",
          count: 0,
          requestId: batchFailures[0].requestId,
          reason: batchFailures[0].reason,
          results: batchFailures,
          profile: finishDispatchProfile(batchProfile, batchStartedAt),
        };
      }
      if (deferredInPlace.length > 0 && batchFailures.length === 0) {
        // Every head was waiting on capacity.  Relocating exactly one keeps the window advancing
        // without paying the four-operation requeue for the whole backlog.
        const head = deferredInPlace[0];
        const claimed = await loadReadyClaim(bucket, head, now);
        if (claimed) {
          await requeue(
            bucket,
            claimed,
            now,
            nextCapacityRetryAt(
              coordinator,
              claimed.record.policy?.allowed_models || claimed.record.model,
              claimed.record.policy,
              now,
            ).toISOString(),
          );
          lastRequeuedId = claimed.record.id;
        }
      }
      if (deadlineGuarded) {
        return {
          status: "deadline_guard",
          count: 0,
          requestId: lastRequeuedId,
          profile: finishDispatchProfile(batchProfile, batchStartedAt),
        };
      }
      return {
        // Markers dropped for an unreadable policy are index repairs, not capacity pressure.
        status: repairedCount > 0 && lastRequeuedId === null ? "index_repaired" : "no_capacity",
        count: 0,
        requestId: lastRequeuedId,
        profile: finishDispatchProfile(batchProfile, batchStartedAt),
      };
    }

    // Atomic CAS write of the combined coordinator (lease + rate ledger)
    let ledgerSaved = false;
    let freshCapacityFailed = false;
    const ledgerWriteStartedAt = profileNow();
    for (let attempt = 0; attempt < 3 && !ledgerSaved; attempt += 1) {
      if (attempt > 0) {
        const freshLoaded = await getJson(bucket, DISPATCH_COORDINATOR_KEY);
        const freshCoordinator = (freshLoaded && freshLoaded.value) || {
          version: 1,
          lease: null,
          routes: {},
          providers: {},
        };
        coordinatorLoadedEtag = freshLoaded ? freshLoaded.etag : null;
        if (!freshCoordinator.routes) freshCoordinator.routes = {};
        if (!freshCoordinator.providers) freshCoordinator.providers = {};
        freshCoordinator.lease = coordinator.lease;

        // Re-admit the same candidates against the fresher ledger, counting them as this batch
        // goes so a route's concurrency ceiling still holds on the retry.
        const retryState = { reaped: false, admitted: new Map() };
        for (const item of candidatesToDispatch) {
          if (
            !providerAvailable(
              freshCoordinator,
              item.chosenRoute,
              now,
              DISPATCH_LIMITS,
              maxStaggerMs,
            )
          ) {
            freshCapacityFailed = true;
            break;
          }
          const entry = ledgerEntry(freshCoordinator, item.chosenRoute.route_id);
          rollLedgerWindows(entry, item.chosenRoute, now);
          if (
            !routeAvailable(
              entry,
              item.chosenRoute,
              item.reservationSize,
              now,
              retryState,
              maxStaggerMs,
            )
          ) {
            freshCapacityFailed = true;
            break;
          }
          const routeDelay = routePacingDelayMs(entry, item.chosenRoute, now.getTime());
          const provDelay = providerPacingDelayMs(
            freshCoordinator,
            item.chosenRoute,
            now.getTime(),
            DISPATCH_LIMITS,
          );
          item.delayMs = Math.max(0, routeDelay, provDelay);
          item.dispatchAtMs = now.getTime() + item.delayMs;
          reserveProviderCapacity(
            freshCoordinator,
            item.chosenRoute,
            item.reservationSize.requests,
            new Date(item.dispatchAtMs),
          );
          reserveRouteCapacity(entry, item.chosenRoute, item.reservationSize, {
            reservedAt: new Date(item.dispatchAtMs).toISOString(),
          });
          retryState.admitted.set(
            item.chosenRoute.route_id,
            (retryState.admitted.get(item.chosenRoute.route_id) || 0) +
              item.reservationSize.requests,
          );
        }
        if (freshCapacityFailed) break;
        coordinator = freshCoordinator;
      }
      const ledgerPut = await putJson(bucket, DISPATCH_COORDINATOR_KEY, coordinator, {
        onlyIf: coordinatorLoadedEtag
          ? { etagMatches: coordinatorLoadedEtag }
          : { etagDoesNotMatch: "*" },
        customMetadata: coordinatorLeaseMetadata(coordinator.lease),
      });
      ledgerSaved = Boolean(ledgerPut);
      if (ledgerSaved) {
        // Reservations are released against this exact version, so the release path can CAS
        // straight onto it instead of re-reading the ledger it just wrote.
        coordinatorHandle.etag = ledgerPut.etag;
        coordinatorHandle.coordinator = coordinator;
      }
    }
    batchProfile.ledger_write_ms = profileElapsed(ledgerWriteStartedAt);

    if (!ledgerSaved) {
      console.error(
        JSON.stringify({
          event: "llm_dispatch_ledger_conflict",
          batchSize: candidatesToDispatch.length,
        }),
      );
      for (const item of candidatesToDispatch) {
        await requeue(bucket, item.claimed, now, now.toISOString(), { releaseAttempt: true });
      }
      if (pendingMarkerDeletes.length > 0) {
        try {
          await unmarkReadyBatch(bucket, pendingMarkerDeletes);
        } catch {
          // Best-effort; self-healing will clean stale markers on the next cron tick.
        }
      }
      return {
        status: "no_capacity",
        count: 0,
        requestId: candidatesToDispatch[0].claimed.record.id,
        profile: finishDispatchProfile(batchProfile, batchStartedAt),
      };
    }

    // Set by a 402/success response below (see paymentRequiredBackoffUntil) mutating `coordinator`
    // in place after this batch's admission reservations were already committed to R2 -- those two
    // writes happen at genuinely different times (admission before dispatch, response after), so a
    // 402 needs its own best-effort flush once every task below has finished; see its use after
    // the `Promise.all` further down.
    let ledgerMutatedAfterDispatch = false;

    // Execute batch concurrently with in-batch stagger pacing, retaining every sibling outcome.
    const upstreamTasks = candidatesToDispatch.map(
      async ({
        claimed,
        chosenRoute,
        creds,
        timeoutSeconds,
        delayMs,
        dispatchAtMs,
        profile,
        dispatchStartedAt,
      }) => {
        // Compute remaining stagger time relative to now; CAS-commit latency should reduce
        // the wait, not add to it.  dispatchAtMs is the absolute target wall-clock instant.
        if (delayMs > 0) {
          const currentMs = typeof nowMs === "function" ? nowMs() : Date.now();
          const remainingMs = Math.max(0, dispatchAtMs - currentMs);
          if (remainingMs > 0) await sleepImpl(remainingMs);
        }
        const upstreamPayload = upstreamRequestForRoute(
          claimed.record,
          chosenRoute,
          creds.upstreamModel,
        );

        let response;
        const requestStartMs = Date.now();
        const upstreamStartedAt = profileNow();
        try {
          const headers = { accept: "application/json", "content-type": "application/json" };
          if (creds.apiKey) {
            headers.authorization = `Bearer ${creds.apiKey}`;
          }
          // Cloudflare AI Gateway's own "Authenticated Gateway" setting (independent of the
          // upstream provider's API key above) requires this header on every proxied request, or
          // the edge rejects with a non-retryable 401 before the request ever reaches the
          // provider. Only attach it when this route is actually going through the gateway and a
          // token is configured -- a direct-to-provider call has no gateway edge to authenticate.
          if (creds.usesGateway && env.AI_GATEWAY_AUTH_TOKEN) {
            headers["cf-aig-authorization"] = `Bearer ${env.AI_GATEWAY_AUTH_TOKEN}`;
          }
          response = await fetchImpl(creds.url, {
            method: "POST",
            headers,
            body: JSON.stringify(upstreamPayload),
            redirect: "manual",
            signal: AbortSignal.timeout(timeoutSeconds * 1000),
          });
          profile.upstream_ms = profileElapsed(upstreamStartedAt);
        } catch (err) {
          profile.upstream_ms = profileElapsed(upstreamStartedAt);
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
            dispatch_profile: finishDispatchProfile(profile, dispatchStartedAt),
          };
          if (claimed.record.attempts < cfg.maxAttempts) {
            return {
              finalize: async () => {
                const persistStartedAt = profileNow();
                await saveRetry(bucket, claimed, null, cfg, now, errorDetail, pendingMarkerDeletes);
                profile.persist_ms = profileElapsed(persistStartedAt);
                return profiledResult(
                  {
                    status: "retrying",
                    requestId: claimed.record.id,
                    routeId: chosenRoute.route_id,
                    error: code,
                  },
                  profile,
                  dispatchStartedAt,
                );
              },
            };
          }
          return {
            finalize: async () => {
              const persistStartedAt = profileNow();
              await saveFailure(
                bucket,
                claimed,
                null,
                now,
                code,
                errorDetail,
                pendingMarkerDeletes,
              );
              profile.persist_ms = profileElapsed(persistStartedAt);
              return profiledResult(
                {
                  status: "failed",
                  requestId: claimed.record.id,
                  routeId: chosenRoute.route_id,
                  reason: code,
                },
                profile,
                dispatchStartedAt,
              );
            },
          };
        }

        if (!response.ok) {
          if (response.status === 402) {
            // Payment required / provider quota exhausted -- a billing-layer signal no amount of
            // rpm/rpd/tpm pacing fixes, so force the whole route unavailable via blocked_until
            // instead of letting every future tick keep re-admitting jobs onto it and repeating
            // this same failure. The individual job below still fails normally (402 is not in
            // retryableStatus); this only stops OTHER jobs from being admitted onto this route
            // until the cooldown passes.
            const entry = ledgerEntry(coordinator, chosenRoute.route_id);
            entry.payment_required_streak = (entry.payment_required_streak || 0) + 1;
            entry.blocked_until = new Date(
              paymentRequiredBackoffUntil(entry.payment_required_streak, now.getTime()),
            ).toISOString();
            // Stamped so a slower sibling that STARTED before this 402 cannot clear the block it
            // knows nothing about. Without a concurrency cap a batch runs several requests on one
            // route, and an in-flight success landing afterwards would otherwise reopen an
            // exhausted route -- now that a 402 requeues its job rather than failing it, that job
            // would come straight back and burn another attempt.
            entry.payment_required_at = now.toISOString();
            ledgerMutatedAfterDispatch = true;
          }
          const elapsedSec = Math.max(1, Math.round((Date.now() - requestStartMs) / 1000));
          // A 402 is a route-level budget signal, not a defect in this job. The branch above has
          // already blocked the whole route, so the job that happened to discover the exhausted
          // budget is collateral -- failing it burns one recoverable job per cooldown lapse, per
          // route (observed 2026-08-26: exactly 2 per route, matching payment_required_streak=2).
          // Requeue it instead: admission keeps it off the now-blocked route, so it runs on an
          // overflow route or once the cooldown clears. `attempts` still bounds it, so a route
          // that stays 402 across every cooldown cannot loop the job forever.
          const willRetry =
            (retryableStatus(response.status) || response.status === 402) &&
            claimed.record.attempts < cfg.maxAttempts;
          const errorReadStartedAt = profileNow();
          const providerError = await readUpstreamError(response, {
            persistBodyPreview: !willRetry,
          });
          profile.error_read_ms = profileElapsed(errorReadStartedAt);
          const errorDetail = {
            code: "upstream_error",
            status: response.status,
            model: chosenRoute.model,
            route_id: chosenRoute.route_id,
            duration_seconds: elapsedSec,
            timestamp: new Date().toISOString(),
            provider_error: providerError,
            dispatch_profile: finishDispatchProfile(profile, dispatchStartedAt),
          };
          if (willRetry) {
            return {
              finalize: async () => {
                const persistStartedAt = profileNow();
                await saveRetry(
                  bucket, claimed, response, cfg, now, errorDetail, pendingMarkerDeletes,
                );
                profile.persist_ms = profileElapsed(persistStartedAt);
                return profiledResult(
                  {
                    status: "retrying",
                    requestId: claimed.record.id,
                    routeId: chosenRoute.route_id,
                    upstreamStatus: response.status,
                  },
                  profile,
                  dispatchStartedAt,
                );
              },
            };
          }
          return {
            finalize: async () => {
              const persistStartedAt = profileNow();
              await saveFailure(
                bucket,
                claimed,
                response.status,
                now,
                "upstream_error",
                errorDetail,
                pendingMarkerDeletes,
              );
              profile.persist_ms = profileElapsed(persistStartedAt);
              return profiledResult(
                {
                  status: "failed",
                  requestId: claimed.record.id,
                  routeId: chosenRoute.route_id,
                  reason: "upstream_error",
                  upstreamStatus: response.status,
                  providerCode: providerError?.code,
                  providerStatus: providerError?.status,
                },
                profile,
                dispatchStartedAt,
              );
            },
          };
        }

        let responseJson;
        const responseReadStartedAt = profileNow();
        try {
          const responseText = await readTextLimited(response.body, cfg.maxResponseBytes);
          responseJson = JSON.parse(responseText);
          profile.response_read_ms = profileElapsed(responseReadStartedAt);
        } catch (error) {
          profile.response_read_ms = profileElapsed(responseReadStartedAt);
          const failureProfile = finishDispatchProfile(profile, dispatchStartedAt);
          return {
            finalize: async () => {
              const persistStartedAt = profileNow();
              await saveFailure(
                bucket,
                claimed,
                response.status,
                now,
                error instanceof BodyTooLargeError
                  ? "upstream_response_too_large"
                  : "invalid_upstream_json",
                {
                  code:
                    error instanceof BodyTooLargeError
                      ? "upstream_response_too_large"
                      : "invalid_upstream_json",
                  status: response.status,
                  model: chosenRoute.model,
                  route_id: chosenRoute.route_id,
                  timestamp: new Date().toISOString(),
                  dispatch_profile: failureProfile,
                },
                pendingMarkerDeletes,
              );
              profile.persist_ms = profileElapsed(persistStartedAt);
              return profiledResult(
                {
                  status: "failed",
                  requestId: claimed.record.id,
                  routeId: chosenRoute.route_id,
                  upstreamStatus: response.status,
                },
                profile,
                dispatchStartedAt,
              );
            },
          };
        }

        if (!responseJson || typeof responseJson !== "object" || Array.isArray(responseJson)) {
          const failureProfile = finishDispatchProfile(profile, dispatchStartedAt);
          return {
            finalize: async () => {
              const persistStartedAt = profileNow();
              await saveFailure(
                bucket,
                claimed,
                response.status,
                now,
                "invalid_upstream_json",
                {
                  code: "invalid_upstream_json",
                  status: response.status,
                  model: chosenRoute.model,
                  route_id: chosenRoute.route_id,
                  timestamp: new Date().toISOString(),
                  dispatch_profile: failureProfile,
                },
                pendingMarkerDeletes,
              );
              profile.persist_ms = profileElapsed(persistStartedAt);
              return profiledResult(
                {
                  status: "failed",
                  requestId: claimed.record.id,
                  routeId: chosenRoute.route_id,
                  upstreamStatus: response.status,
                },
                profile,
                dispatchStartedAt,
              );
            },
          };
        }

        profile.response_read_ms = profileElapsed(responseReadStartedAt);

        // A successful call proves the route is healthy again -- clear any 402 backoff state,
        // matching the branch above that sets it.
        const successEntry = ledgerEntry(coordinator, chosenRoute.route_id);
        const blockedAtMs = successEntry.payment_required_at
          ? parseTime(successEntry.payment_required_at)
          : null;
        // Only a call that began AFTER the most recent 402 proves the route recovered. One that
        // was already in flight when the 402 landed proves nothing about the account's balance.
        const provesRecovery = blockedAtMs == null || requestStartMs > blockedAtMs;
        if (
          provesRecovery &&
          (successEntry.payment_required_streak ||
            successEntry.blocked_until ||
            successEntry.payment_required_at)
        ) {
          successEntry.payment_required_streak = 0;
          successEntry.blocked_until = null;
          successEntry.payment_required_at = null;
          ledgerMutatedAfterDispatch = true;
        }

        const completed = {
          ...claimed.record,
          status: "completed",
          updated_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
          dispatch_profile: finishDispatchProfile(profile, dispatchStartedAt),
          response: responseJson,
        };
        return {
          finalize: async () => {
            const persistStartedAt = profileNow();
            const saved = await putJson(bucket, claimed.key, completed, {
              etagMatches: claimed.object.etag,
              customMetadata: requestMetadata(completed),
            });
            if (saved && claimed.readyKey) pendingMarkerDeletes.push(claimed.readyKey);
            profile.persist_ms = profileElapsed(persistStartedAt);
            return profiledResult(
              {
                status: "completed",
                requestId: claimed.record.id,
                routeId: chosenRoute.route_id,
                upstreamStatus: response.status,
              },
              profile,
              dispatchStartedAt,
            );
          },
        };
      },
    );

    const finalized = await Promise.all(
      upstreamTasks.map(async (task, index) => {
        const item = candidatesToDispatch[index];
        try {
          const outcome = await task;
          return await outcome.finalize();
        } catch (error) {
          console.error(
            JSON.stringify({
              event: "llm_dispatch_task_error",
              request_id: item.claimed.record.id,
              error: error?.name || "Error",
            }),
          );
          try {
            await saveFailure(
              bucket,
              item.claimed,
              500,
              now,
              "worker_task_failed",
              null,
              pendingMarkerDeletes,
            );
          } catch {
            // Recoverable by processing-timeout reaper if write races.
          }
          return {
            status: "failed",
            requestId: item.claimed.record.id,
            reason: "worker_task_failed",
          };
        }
      }),
    );

    // Best-effort flush of any 402/success ledger mutations made above (see
    // paymentRequiredBackoffUntil): those happened after this batch's admission reservations were
    // already durably committed to R2, so they need their own write. Single CAS attempt, silently
    // dropped on conflict -- same risk tolerance as the pre-dispatch "reaped" flush above: a lost
    // write here just means blocked_until doesn't take effect until this same branch is reached
    // again on a future tick, not a correctness hazard.
    if (ledgerMutatedAfterDispatch && coordinatorHandle.etag) {
      try {
        const putRes = await putJson(bucket, DISPATCH_COORDINATOR_KEY, coordinator, {
          onlyIf: { etagMatches: coordinatorHandle.etag },
          customMetadata: coordinatorLeaseMetadata(coordinator.lease),
        });
        if (putRes) {
          coordinatorHandle.etag = putRes.etag;
          coordinatorHandle.coordinator = coordinator;
        }
      } catch {
        // A sibling may have committed fresher ledger state; this route's 402 backoff is picked
        // up again the next time this branch runs, same as the pre-dispatch reaped flush above.
      }
    }

    const markerDeleteStartedAt = profileNow();
    try {
      await unmarkReadyBatch(bucket, pendingMarkerDeletes);
    } catch {
      console.error(
        JSON.stringify({
          event: "llm_dispatch_marker_cleanup_failed",
          batch_size: candidatesToDispatch.length,
        }),
      );
    }
    batchProfile.marker_delete_ms = profileElapsed(markerDeleteStartedAt);
    const completedCount = finalized.filter((r) => r.status === "completed").length;
    return {
      status: "completed",
      count: candidatesToDispatch.length,
      completedCount,
      results: finalized,
      profile: finishDispatchProfile(batchProfile, batchStartedAt),
    };
  } finally {
    if (acquiredLock) {
      await releaseCoordinator(bucket, leaseOwner, coordinatorHandle);
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

function summarizeBatchResults(results = [], includeSuccessProfiles = false) {
  return results.map((result) => ({
    request_id: result.requestId,
    status: result.status,
    route_id: result.routeId,
    upstream_status: result.upstreamStatus,
    provider_code: result.providerCode,
    provider_status: result.providerStatus,
    reason: result.reason || result.error,
    profile:
      includeSuccessProfiles || result.status !== "completed" ? result.profile : undefined,
  }));
}

function requestLocation(request, id) {
  return new URL(`/v1/requests/${encodeURIComponent(id)}`, request.url).toString();
}

function responseForRecord(request, record) {
  if (record.status === "completed") {
    return jsonResponse(record.response, 200, { "x-request-id": record.id });
  }
  if (record.status === "retired") {
    return jsonResponse(
      { error: { code: "retired", message: "request was retired by an operator" } },
      410,
      { "x-request-id": record.id },
    );
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

  if (url.pathname === "/v1/queue/reindex" && request.method === "POST") {
    return plain(410, "queue reindex moved to scripts/reindex_llm_dispatch_queue.py");
  }

  if (url.pathname === "/v1/queue/estimate" && request.method === "GET") {
    return plain(410, "exact queue estimates were removed to keep Worker reads bounded");
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

async function runScheduled(
  env,
  { fetchImpl = fetch, nowMs = Date.now, sleepImpl = sleep } = {},
) {
  const bucket = env.LLM_QUEUE;
  if (!bucket) return { status: "no_storage", totalDispatched: 0 };
  const cfg = config(env);
  const startMs = nowMs();
  // `config()` guarantees the execution budget is shorter than the lease. The lease is acquired
  // after `startMs`, so its actual expiry is later than this fixed run deadline. Renewals protect
  // ownership between batches; they never extend the invocation deadline.
  const deadlineMs = startMs + cfg.maxExecutionSeconds * 1000;
  const leaseOwner = crypto.randomUUID();
  const coordinatorHandle = { etag: null, coordinator: null, owner: leaseOwner };
  const acquireResult = await acquireCoordinator(
    bucket,
    new Date(nowMs()),
    leaseOwner,
    cfg.leaseDurationSeconds,
    coordinatorHandle,
  );
  if (!acquireResult.acquired) {
    console.log(JSON.stringify({ event: "llm_dispatch", status: "lease_busy" }));
    return { status: "lease_busy", totalDispatched: 0 };
  }

  // The lease cannot be taken over before it expires, so renewing is only meaningful once a run
  // has consumed a real share of the lease.  Renewing on the first pass -- microseconds after
  // acquiring an 840-second lease -- spent an R2 round trip to re-prove ownership this invocation
  // had just established.
  let renewDueAtMs = nowMs() + (cfg.leaseDurationSeconds * 1000) / 2;

  let totalDispatched = 0;
  let status = "idle";
  const includeSuccessProfiles = Math.random() < cfg.profileSampleRate;
  try {
    while (nowMs() < deadlineMs && totalDispatched < cfg.maxTotalRequests) {
      if (
        deadlineMs - nowMs() <
        (cfg.fastUpstreamTimeoutSeconds + cfg.finalizationReserveSeconds) * 1000
      ) {
        status = "deadline_guard";
        break;
      }
      if (nowMs() >= renewDueAtMs) {
        const renewed = await renewCoordinator(
          bucket,
          new Date(nowMs()),
          leaseOwner,
          cfg.leaseDurationSeconds,
          coordinatorHandle,
        );
        if (!renewed) {
          status = "lease_lost";
          console.error(JSON.stringify({ event: "llm_dispatch", status }));
          break;
        }
        renewDueAtMs = nowMs() + (cfg.leaseDurationSeconds * 1000) / 2;
      }
      const now = new Date(nowMs());
      const batchResult = await dispatchBatch(
        env,
        fetchImpl,
        now,
        Math.min(cfg.batchConcurrency, cfg.maxTotalRequests - totalDispatched),
        leaseOwner,
        deadlineMs,
        coordinatorHandle,
        sleepImpl,
        nowMs,
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
            results: summarizeBatchResults(batchResult.results, includeSuccessProfiles),
            profile:
              includeSuccessProfiles ||
              (batchResult.results || []).some((r) => r.status !== "completed")
                ? batchResult.profile
                : undefined,
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
          completed: batchResult.completedCount || 0,
          failed: (batchResult.results || []).filter((result) => result.status === "failed")
            .length,
          retrying: (batchResult.results || []).filter((result) => result.status === "retrying")
            .length,
          profile:
            includeSuccessProfiles ||
            (batchResult.results || []).some((r) => r.status !== "completed")
              ? batchResult.profile
              : undefined,
          results: summarizeBatchResults(batchResult.results, includeSuccessProfiles),
        }),
      );
    }
  } catch (error) {
    status = "error";
    console.error(JSON.stringify({ event: "llm_dispatch", status, error: error?.name || "Error" }));
  } finally {
    await releaseCoordinator(bucket, leaseOwner, coordinatorHandle);
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
  DISPATCH_COORDINATOR_KEY,
  acquireCoordinator,
  acquireCronLease,
  config,
  dispatchBatch,
  dispatchOne,
  getCoordinatorLease,
  handleRequest,
  localDateTimeToUTC,
  nextCapacityRetryAt,
  nextLocalMidnightUTC,
  nextRouteReset,
  ledgerEntry,
  normalizeChatRequest,
  paymentRequiredBackoffUntil,
  rankRoutes,
  releaseCoordinator,
  releaseCronLease,
  readyKey,
  readyMarker,
  readyMarkerMetadata,
  renewCoordinator,
  renewCronLease,
  runScheduled,
  requestKey,
  requestMetadata,
  resolveProviderCredentials,
  routeAvailable,
  selectRoute,
  sleep,
};
