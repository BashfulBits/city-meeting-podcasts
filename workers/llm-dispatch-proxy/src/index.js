import DISPATCH_LIMITS from "./dispatch_limits.json" with { type: "json" };

const REQUEST_PREFIX = "requests/";
const DISPATCH_CONTROL_KEY = "control/dispatch.json";
const CRON_LOCK_KEY = "locks/cron.json";
const DEFAULT_MAX_REQUEST_BYTES = 512 * 1024;
const DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const DEFAULT_MAX_QUEUE_SCAN = 1000;
const DEFAULT_MAX_ATTEMPTS = 5;
const DEFAULT_PROCESSING_TIMEOUT_SECONDS = 15 * 60;
const DEFAULT_RETRY_BASE_SECONDS = 60;
const DEFAULT_RETRY_MAX_SECONDS = 60 * 60;
const DEFAULT_DISPATCH_INTERVAL_SECONDS = 60;

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

function config(env) {
  const provider = requiredString(env.PROVIDER_NAME || "mistral", "PROVIDER_NAME");
  const upstreamModel = requiredString(env.UPSTREAM_MODEL || "mistral-large-2512", "UPSTREAM_MODEL");
  const model = String(env.MODEL_ID || `${provider}/${modelName(upstreamModel)}`).trim();
  const base = requiredString(env.UPSTREAM_BASE_URL || "https://api.mistral.ai", "UPSTREAM_BASE_URL").replace(/\/+$/, "");

  let parsed;
  try {
    parsed = new URL(base);
  } catch {
    throw new Error("UPSTREAM_BASE_URL is not a valid URL");
  }
  if (parsed.protocol !== "https:") {
    throw new Error("UPSTREAM_BASE_URL must use HTTPS");
  }

  const upstreamRequestModel = String(
    env.UPSTREAM_REQUEST_MODEL || modelName(upstreamModel),
  ).trim();

  return {
    upstreamUrl: `${base}${String(env.UPSTREAM_CHAT_PATH || "/v1/chat/completions")}`,
    upstreamApiKey: String(env.UPSTREAM_API_KEY || ""),
    provider,
    model,
    upstreamModel,
    upstreamRequestModel,
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
    dispatchIntervalSeconds: positiveNumber(
      env.DISPATCH_INTERVAL_SECONDS,
      DEFAULT_DISPATCH_INTERVAL_SECONDS,
    ),
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

async function getJson(bucket, key) {
  const object = await bucket.get(key);
  if (!object) {
    return null;
  }
  return { object, etag: object.etag, value: await object.json() };
}

async function putJson(bucket, key, value, { etagMatches, onlyIf } = {}) {
  const body = JSON.stringify(value);
  const options = {};
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
  const requestedModel = String(body.model || cfg?.model || "").trim();
  if (!requestedModel) {
    throw new HttpError(400, "model is required");
  }
  if (body.stream === true) {
    throw new HttpError(400, "streaming is not supported");
  }

  if (cfg) {
    if (requestedModel !== cfg.model && requestedModel !== cfg.upstreamModel) {
      const allowedModels = DISPATCH_LIMITS.model_routes_map || {};
      if (!allowedModels[requestedModel]) {
        throw new HttpError(400, `request model must match ${cfg.model}`);
      }
    }
  }

  const messages = body.messages;
  if (!Array.isArray(messages) || messages.length === 0) {
    throw new HttpError(400, "messages must be a non-empty array");
  }

  const payloadModel = cfg ? cfg.upstreamRequestModel : modelName(requestedModel);
  const payload = {
    model: payloadModel,
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
    if (JSON.stringify(existingReq) !== JSON.stringify(normalized.payload)) {
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

  const saved = await putJson(bucket, key, record, { onlyIf: { etagDoesNotMatch: "*" } });
  if (!saved) {
    const reloaded = await getJson(bucket, key);
    if (reloaded) {
      return reloaded.value;
    }
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

async function reserveDispatchSlot(bucket, cfg, now, requestId) {
  const intervalMs = cfg.dispatchIntervalSeconds * 1000;
  const existingControl = await getJson(bucket, DISPATCH_CONTROL_KEY);

  let lastDispatchedMs = 0;
  if (existingControl && existingControl.value && existingControl.value.last_dispatched_at) {
    lastDispatchedMs = parseTime(existingControl.value.last_dispatched_at);
  }

  if (lastDispatchedMs > 0 && now.getTime() - lastDispatchedMs < intervalMs) {
    return false;
  }

  const updatedControl = {
    schema: 1,
    updated_at: now.toISOString(),
    last_dispatched_at: now.toISOString(),
    last_dispatched_request_id: requestId,
  };

  const saved = await putJson(bucket, DISPATCH_CONTROL_KEY, updatedControl, {
    onlyIf: existingControl ? { etagMatches: existingControl.etag } : { etagDoesNotMatch: "*" },
  });

  return Boolean(saved);
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
  await putJson(bucket, claimed.key, updated, { etagMatches: claimed.object.etag });
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
  await putJson(bucket, claimed.key, updated, { etagMatches: claimed.object.etag });
}

async function acquireCronLease(bucket, now) {
  const existing = await getJson(bucket, CRON_LOCK_KEY);
  if (existing && existing.value && existing.value.expires_at) {
    if (parseTime(existing.value.expires_at) > now.getTime()) {
      return false;
    }
  }
  const lease = {
    acquired_at: now.toISOString(),
    expires_at: new Date(now.getTime() + 30000).toISOString(),
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

async function releaseCronLease(bucket) {
  try {
    if (bucket && typeof bucket.delete === "function") {
      await bucket.delete(CRON_LOCK_KEY);
    }
  } catch {
    // Best-effort release
  }
}

function resolveProviderCredentials(env, route, cfg) {
  const provider = route ? route.provider : cfg.provider;
  const accounts = (DISPATCH_LIMITS.providers[provider]?.accounts) || [];
  const account = (route ? accounts.find((a) => a.id === route.account_id) : null) || accounts[0];

  const envKeyName = account?.api_key_env || `${provider.toUpperCase()}_API_KEY`;
  const apiKey = env[envKeyName] || cfg?.upstreamApiKey || env.UPSTREAM_API_KEY || "";

  let url = cfg?.upstreamUrl;
  if (!url) {
    let baseUrl = env[`${provider.toUpperCase()}_BASE_URL`] || env.UPSTREAM_BASE_URL || "https://api.mistral.ai";
    baseUrl = baseUrl.replace(/\/+$/, "");
    let chatPath = env[`${provider.toUpperCase()}_CHAT_PATH`] || env.UPSTREAM_CHAT_PATH || "/v1/chat/completions";
    url = `${baseUrl}${chatPath}`;
  }

  const upstreamModel = cfg?.upstreamRequestModel || (route ? route.upstream_model : cfg.upstreamModel);
  return { apiKey, url, upstreamModel };
}

export async function dispatchOne(env, fetchImpl = fetch, now = new Date()) {
  const bucket = env.LLM_QUEUE;
  if (!bucket) {
    return { status: "no_storage" };
  }

  const cfg = config(env);
  const acquiredLock = await acquireCronLease(bucket, now);
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

    if (!(await reserveDispatchSlot(bucket, cfg, now, claimed.record.id))) {
      const released = {
        ...claimed.record,
        status: "pending",
        attempts: Math.max(0, claimed.record.attempts - 1),
        updated_at: now.toISOString(),
        available_at: now.toISOString(),
        processing_started_at: undefined,
      };
      await putJson(bucket, claimed.key, released, { etagMatches: claimed.object.etag });
      return { status: "rate_limited", requestId: claimed.record.id };
    }

    const canonicalModel = claimed.record.model;
    const routeIds = DISPATCH_LIMITS.model_routes_map[canonicalModel] || [];
    let chosenRoute = null;
    for (const rId of routeIds) {
      const r = DISPATCH_LIMITS.routes_by_id[rId];
      if (r) {
        if (!r.free && !claimed.record.policy?.allow_paid) continue;
        chosenRoute = r;
        break;
      }
    }

    const creds = resolveProviderCredentials(env, chosenRoute, cfg);
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
    await putJson(bucket, claimed.key, completed, { etagMatches: claimed.object.etag });
    return { status: "completed", requestId: claimed.record.id, upstreamStatus: response.status };
  } finally {
    await releaseCronLease(bucket);
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
    const listRes = await env.LLM_QUEUE.list({ prefix: REQUEST_PREFIX, limit: 1000 });
    const objects = listRes ? listRes.objects || [] : [];
    let count = 0;
    for (const obj of objects) {
      const loaded = await getJson(env.LLM_QUEUE, obj.key);
      if (loaded && loaded.value && loaded.value.status === "pending") {
        if (!model || loaded.value.model === model) {
          count += 1;
        }
      }
    }
    const estSeconds = Math.ceil(count * cfg.dispatchIntervalSeconds);
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

export { config, normalizeChatRequest, requestKey };
