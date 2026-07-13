const REQUEST_PREFIX = "requests/";
const DISPATCH_CONTROL_KEY = "control/dispatch.json";
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

function config(env) {
  const base = requiredString(env.UPSTREAM_BASE_URL, "UPSTREAM_BASE_URL").replace(/\/+$/, "");
  let parsed;
  try {
    parsed = new URL(base);
  } catch {
    throw new Error("UPSTREAM_BASE_URL is not a valid URL");
  }
  if (parsed.protocol !== "https:") {
    throw new Error("UPSTREAM_BASE_URL must use HTTPS");
  }

  const provider = requiredString(env.PROVIDER_NAME, "PROVIDER_NAME");
  const upstreamModel = requiredString(env.UPSTREAM_MODEL, "UPSTREAM_MODEL");
  const model = String(env.MODEL_ID || `${provider}/${modelName(upstreamModel)}`).trim();
  const upstreamRequestModel = String(
    env.UPSTREAM_REQUEST_MODEL || modelName(upstreamModel),
  ).trim();
  if (!model || !upstreamRequestModel || !model.startsWith(`${provider}/`)) {
    throw new Error("MODEL_ID must be a provider-qualified model for PROVIDER_NAME");
  }

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
    maxMessages: positiveNumber(env.MAX_MESSAGES, 256, { integer: true }),
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

function modelName(value) {
  return String(value).split("/").pop();
}

function modelMatches(value, cfg) {
  const raw = String(value);
  const slash = raw.indexOf("/");
  if (slash >= 0 && raw.slice(0, slash) !== cfg.provider) {
    return false;
  }
  const candidate = modelName(raw);
  return candidate === modelName(cfg.model) || candidate === modelName(cfg.upstreamModel);
}

function normalizeChatRequest(payload, cfg) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new HttpError(400, "request body must be a JSON object");
  }
  if (payload.stream === true) {
    throw new HttpError(400, "streaming responses are not supported; poll the queued request");
  }
  if (!Array.isArray(payload.messages) || payload.messages.length === 0) {
    throw new HttpError(400, "messages must be a non-empty array");
  }
  if (payload.messages.length > cfg.maxMessages) {
    throw new HttpError(413, "too many messages");
  }
  for (const message of payload.messages) {
    if (!message || typeof message !== "object" || typeof message.role !== "string") {
      throw new HttpError(400, "each message must contain a role");
    }
    if (!("content" in message) && !("tool_calls" in message)) {
      throw new HttpError(400, "each message must contain content or tool_calls");
    }
  }

  if (payload.model !== undefined && !modelMatches(payload.model, cfg)) {
    throw new HttpError(400, `this Worker only dispatches model ${cfg.model}`);
  }

  const normalized = {
    model: cfg.upstreamRequestModel,
    messages: payload.messages,
    stream: false,
  };
  for (const field of COPY_FIELDS) {
    if (payload[field] !== undefined) {
      normalized[field] = payload[field];
    }
  }
  return normalized;
}

function requestKey(id) {
  return `${REQUEST_PREFIX}${id}.json`;
}

function recordMetadata(record) {
  return {
    status: String(record.status || "unknown"),
    created_at: String(record.created_at || ""),
    updated_at: String(record.updated_at || ""),
    available_at: String(record.available_at || ""),
    attempts: String(record.attempts || 0),
  };
}

async function putJson(bucket, key, value, onlyIf) {
  const options = {
    customMetadata: recordMetadata(value),
    httpMetadata: { contentType: "application/json", cacheControl: "no-store" },
  };
  if (onlyIf) {
    options.onlyIf = onlyIf;
  }
  return bucket.put(key, JSON.stringify(value), options);
}

async function getJson(bucket, key) {
  const object = await bucket.get(key);
  if (!object) {
    return null;
  }
  return { object, value: await object.json() };
}

function parseTime(value, fallback = 0) {
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : fallback;
}

async function enqueue(bucket, payload, cfg, now, idempotencyKey) {
  const requestJson = JSON.stringify(payload);
  const requestHash = await sha256Hex(requestJson);
  const idempotencyHash = idempotencyKey ? await sha256Hex(idempotencyKey) : null;
  const id = idempotencyHash ? `chatcmpl-${idempotencyHash.slice(0, 32)}` : `chatcmpl-${crypto.randomUUID()}`;
  const key = requestKey(id);
  const existing = await getJson(bucket, key);
  if (existing) {
    if (existing.value.request_hash !== requestHash) {
      throw new HttpError(409, "Idempotency-Key was already used for a different request");
    }
    return existing.value;
  }

  const createdAt = now.toISOString();
  const record = {
    schema: 1,
    id,
    status: "pending",
    provider: cfg.provider,
    model: cfg.model,
    request: payload,
    request_hash: requestHash,
    idempotency_hash: idempotencyHash,
    created_at: createdAt,
    updated_at: createdAt,
    available_at: createdAt,
    attempts: 0,
  };
  // The conditional create prevents two concurrent retries with the same idempotency key from
  // replacing each other's request. R2 returns null when the precondition loses the race.
  const stored = await putJson(bucket, key, record, { etagDoesNotMatch: "*" });
  if (stored) {
    return record;
  }
  const raced = await getJson(bucket, key);
  if (!raced) {
    throw new Error("request could not be persisted");
  }
  if (raced.value.request_hash !== requestHash) {
    throw new HttpError(409, "Idempotency-Key was already used for a different request");
  }
  return raced.value;
}

async function listRequestObjects(bucket, limit) {
  const objects = [];
  let cursor;
  do {
    const listed = await bucket.list({
      prefix: REQUEST_PREFIX,
      limit: Math.min(1000, limit - objects.length),
      cursor,
      include: ["customMetadata"],
    });
    objects.push(...listed.objects);
    cursor = listed.truncated ? listed.cursor : undefined;
  } while (cursor && objects.length < limit);
  return objects;
}

async function claimNext(bucket, cfg, now) {
  const objects = await listRequestObjects(bucket, cfg.maxQueueScan);
  const candidates = [];
  for (const object of objects) {
    const loaded = await getJson(bucket, object.key);
    if (!loaded || !loaded.value || typeof loaded.value !== "object") {
      continue;
    }
    const record = loaded.value;
    const staleProcessing =
      record.status === "processing" &&
      now.getTime() - parseTime(record.processing_started_at || record.updated_at) >=
        cfg.processingTimeoutSeconds * 1000;
    const readyPending = record.status === "pending" && parseTime(record.available_at) <= now.getTime();
    if (readyPending || staleProcessing) {
      candidates.push({ loaded, createdAt: parseTime(record.created_at), key: object.key });
    }
  }
  candidates.sort((left, right) => left.createdAt - right.createdAt || left.key.localeCompare(right.key));

  for (const candidate of candidates) {
    const record = candidate.loaded.value;
    const claimed = {
      ...record,
      status: "processing",
      attempts: Number(record.attempts || 0) + 1,
      updated_at: now.toISOString(),
      processing_started_at: now.toISOString(),
    };
    const stored = await putJson(bucket, candidate.key, claimed, {
      etagMatches: candidate.loaded.object.etag,
    });
    if (stored) {
      return { key: candidate.key, record: claimed, object: stored };
    }
  }
  return null;
}

async function reserveDispatchSlot(bucket, cfg, now, requestId) {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const current = await getJson(bucket, DISPATCH_CONTROL_KEY);
    if (current) {
      const lastDispatch = parseTime(current.value.last_dispatch_at);
      if (now.getTime() - lastDispatch < cfg.dispatchIntervalSeconds * 1000) {
        return false;
      }
      const next = {
        schema: 1,
        last_dispatch_at: now.toISOString(),
        request_id: requestId,
      };
      const stored = await putJson(bucket, DISPATCH_CONTROL_KEY, next, {
        etagMatches: current.object.etag,
      });
      if (stored) {
        return true;
      }
      continue;
    }

    const first = {
      schema: 1,
      last_dispatch_at: now.toISOString(),
      request_id: requestId,
    };
    const stored = await putJson(bucket, DISPATCH_CONTROL_KEY, first, { etagDoesNotMatch: "*" });
    if (stored) {
      return true;
    }
  }
  return false;
}

function retryDelaySeconds(response, record, cfg, now) {
  const retryAfter = response?.headers.get("retry-after");
  if (retryAfter) {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds) && seconds >= 0) {
      return Math.min(cfg.retryMaxSeconds, seconds);
    }
    const date = Date.parse(retryAfter);
    if (Number.isFinite(date)) {
      return Math.min(cfg.retryMaxSeconds, Math.max(0, (date - now.getTime()) / 1000));
    }
  }
  return Math.min(
    cfg.retryMaxSeconds,
    cfg.retryBaseSeconds * 2 ** Math.max(0, Number(record.attempts || 1) - 1),
  );
}

function retryableStatus(status) {
  return status === 408 || status === 409 || status === 425 || status === 429 || status >= 500;
}

async function saveFailure(bucket, claimed, status, now, reason = "upstream_error") {
  const failed = {
    ...claimed.record,
    status: "failed",
    updated_at: now.toISOString(),
    completed_at: now.toISOString(),
    error: { type: reason, status: status || null },
  };
  return putJson(bucket, claimed.key, failed, { etagMatches: claimed.object.etag });
}

async function saveRetry(bucket, claimed, response, cfg, now) {
  const retry = {
    ...claimed.record,
    status: "pending",
    updated_at: now.toISOString(),
    available_at: new Date(
      now.getTime() + retryDelaySeconds(response, claimed.record, cfg, now) * 1000,
    ).toISOString(),
    last_error: { type: "upstream_retry", status: response?.status || null },
  };
  return putJson(bucket, claimed.key, retry, { etagMatches: claimed.object.etag });
}

export async function dispatchOne(env, fetchImpl = fetch, now = new Date()) {
  const cfg = config(env);
  if (!env.LLM_QUEUE) {
    throw new Error("LLM_QUEUE R2 binding is not configured");
  }

  const claimed = await claimNext(env.LLM_QUEUE, cfg, now);
  if (!claimed) {
    return { status: "idle" };
  }

  if (!(await reserveDispatchSlot(env.LLM_QUEUE, cfg, now, claimed.record.id))) {
    const released = {
      ...claimed.record,
      status: "pending",
      updated_at: now.toISOString(),
      available_at: now.toISOString(),
      processing_started_at: undefined,
    };
    await putJson(env.LLM_QUEUE, claimed.key, released, { etagMatches: claimed.object.etag });
    return { status: "rate_limited", requestId: claimed.record.id };
  }

  let response;
  try {
    const headers = { accept: "application/json", "content-type": "application/json" };
    if (cfg.upstreamApiKey) {
      headers.authorization = `Bearer ${cfg.upstreamApiKey}`;
    }
    response = await fetchImpl(cfg.upstreamUrl, {
      method: "POST",
      headers,
      body: JSON.stringify(claimed.record.request),
      redirect: "manual",
    });
  } catch {
    if (claimed.record.attempts < cfg.maxAttempts) {
      await saveRetry(env.LLM_QUEUE, claimed, null, cfg, now);
      return { status: "retrying", requestId: claimed.record.id };
    }
    await saveFailure(env.LLM_QUEUE, claimed, null, now, "upstream_unreachable");
    return { status: "failed", requestId: claimed.record.id };
  }

  if (!response.ok) {
    if (retryableStatus(response.status) && claimed.record.attempts < cfg.maxAttempts) {
      await response.body?.cancel();
      await saveRetry(env.LLM_QUEUE, claimed, response, cfg, now);
      return { status: "retrying", requestId: claimed.record.id, upstreamStatus: response.status };
    }
    await response.body?.cancel();
    await saveFailure(env.LLM_QUEUE, claimed, response.status, now);
    return { status: "failed", requestId: claimed.record.id, upstreamStatus: response.status };
  }

  let responseJson;
  try {
    const responseText = await readTextLimited(response.body, cfg.maxResponseBytes);
    responseJson = JSON.parse(responseText);
  } catch (error) {
    await saveFailure(
      env.LLM_QUEUE,
      claimed,
      response.status,
      now,
      error instanceof BodyTooLargeError ? "upstream_response_too_large" : "invalid_upstream_json",
    );
    return { status: "failed", requestId: claimed.record.id, upstreamStatus: response.status };
  }

  if (!responseJson || typeof responseJson !== "object" || Array.isArray(responseJson)) {
    await saveFailure(env.LLM_QUEUE, claimed, response.status, now, "invalid_upstream_json");
    return { status: "failed", requestId: claimed.record.id, upstreamStatus: response.status };
  }
  const completed = {
    ...claimed.record,
    status: "completed",
    updated_at: now.toISOString(),
    completed_at: now.toISOString(),
    response: responseJson,
  };
  await putJson(env.LLM_QUEUE, claimed.key, completed, { etagMatches: claimed.object.etag });
  return { status: "completed", requestId: claimed.record.id, upstreamStatus: response.status };
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
  let body;
  try {
    body = await readJsonBody(request, cfg.maxRequestBytes);
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

  if (url.pathname === "/v1/models" && request.method === "GET") {
    const cfg = config(env);
    return jsonResponse({
      object: "list",
      data: [{ id: cfg.model, object: "model", owned_by: cfg.provider }],
    });
  }
  if (url.pathname === "/v1/chat/completions" && request.method === "POST") {
    return handleChatCompletion(request, env, config(env));
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
