import assert from "node:assert/strict";
import test from "node:test";

import DISPATCH_LIMITS from "../src/dispatch_limits.json" with { type: "json" };

import {
  acquireCronLease,
  config,
  dispatchBatch,
  dispatchOne,
  handleRequest,
  localDateTimeToUTC,
  nextCapacityRetryAt,
  nextLocalMidnightUTC,
  nextRouteReset,
  rankRoutes,
  releaseCronLease,
  readyKey,
  readyMarker,
  readyMarkerMetadata,
  renewCronLease,
  resolveProviderCredentials,
  routeAvailable,
  runScheduled,
  selectRoute,
} from "../src/index.js";

class FakeBucket {
  constructor() {
    this.objects = new Map();
    this.sequence = 0;
  }

  async put(key, value, options = {}) {
    const current = this.objects.get(key);
    const condition = options.onlyIf || {};
    if (condition.etagMatches && (!current || current.etag !== condition.etagMatches)) {
      return null;
    }
    if (condition.etagDoesNotMatch === "*" && current) {
      return null;
    }
    const etag = `etag-${++this.sequence}`;
    const text = typeof value === "string" ? value : await new Response(value).text();
    this.objects.set(key, {
      key,
      etag,
      value: text,
      customMetadata: options.customMetadata || {},
      uploaded: new Date(),
    });
    return { key, etag, httpEtag: `"${etag}"` };
  }

  async get(key) {
    const stored = this.objects.get(key);
    if (!stored) {
      return null;
    }
    return {
      key: stored.key,
      etag: stored.etag,
      httpEtag: `"${stored.etag}"`,
      customMetadata: stored.customMetadata,
      async json() {
        return JSON.parse(stored.value);
      },
      async text() {
        return stored.value;
      },
    };
  }

  async head(key) {
    const stored = this.objects.get(key);
    if (!stored) return null;
    return {
      key: stored.key,
      etag: stored.etag,
      httpEtag: `"${stored.etag}"`,
      customMetadata: stored.customMetadata,
      uploaded: stored.uploaded,
    };
  }

  async list(options = {}) {
    const prefix = options.prefix || "";
    const all = [...this.objects.values()]
      .filter((object) => object.key.startsWith(prefix))
      .sort((left, right) => left.key.localeCompare(right.key));
    const start = options.cursor ? Number(options.cursor) : 0;
    const limit = options.limit || 1000;
    const page = all.slice(start, start + limit);
    const next = start + page.length;
    return {
      objects: page.map((object) => ({
        key: object.key,
        etag: object.etag,
        customMetadata: object.customMetadata,
        uploaded: object.uploaded,
      })),
      truncated: next < all.length,
      cursor: next < all.length ? String(next) : undefined,
    };
  }

  async delete(key) {
    for (const one of Array.isArray(key) ? key : [key]) this.objects.delete(one);
  }
}

// Real secrets for every account declared in config/provider_limits.yml -- resolveProviderCredentials
// reads these by the `api_key_env` name compiled into dispatch_limits.json, so tests exercise the
// actual multi-provider credential/URL resolution rather than a mocked-out stand-in for it.
const ENV = {
  DISPATCH_AUTH_TOKEN: "dispatch-secret",
  PROVIDER_NAME: "mistral",
  UPSTREAM_MODEL: "mistral-large-2512",
  MISTRAL_API_KEY: "mistral-secret",
  GEMINI_API_KEY: "gemini-primary-secret",
  GEMINI_API_KEY_SECONDARY: "gemini-secondary-secret",
  DEEPSEEK_API_KEY: "deepseek-secret",
  SILICONFLOW_API_KEY: "siliconflow-secret",
  GROQ_API_KEY: "groq-secret",
  SAMBANOVA_API_KEY: "sambanova-secret",
  ZAI_API_KEY: "zai-secret",
  OPENROUTER_API_KEY: "openrouter-secret",
  KILO_API_KEY: "kilo-secret",
  OPENCODE_API_KEY: "opencode-secret",
  RETRY_BASE_SECONDS: "60",
  RETRY_MAX_SECONDS: "3600",
  LLM_QUEUE: new FakeBucket(),
};

function request(url, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("authorization", "Bearer dispatch-secret");
  headers.set("content-type", "application/json");
  return new Request(url, { ...options, headers });
}

function chatRequest(body, idempotencyKey, model, policy = {}) {
  const headers = idempotencyKey ? { "idempotency-key": idempotencyKey } : {};
  return request("https://dispatch.example/v1/chat/completions", {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: model || "mistral/mistral-large-2512",
      messages: body || [{ role: "user", content: "Summarize this meeting." }],
      ...policy,
    }),
  });
}

function routeIdsForModel(model) {
  const ids = {};
  for (const routeId of DISPATCH_LIMITS.model_routes_map?.[model] || []) {
    if (DISPATCH_LIMITS.routes_by_id?.[routeId]) ids[routeId] = true;
  }
  return ids;
}

function isolatedEnv() {
  return { ...ENV, LLM_QUEUE: new FakeBucket() };
}

function okUpstream(id = "completion") {
  return async () => new Response(JSON.stringify({ id, choices: [] }), { status: 200 });
}

test("health is public, while API endpoints require a bearer token", async () => {
  const env = isolatedEnv();
  const health = await handleRequest(new Request("https://dispatch.example/healthz"), env);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { ok: true });

  const unauthorized = await handleRequest(
    new Request("https://dispatch.example/v1/models"),
    env,
  );
  assert.equal(unauthorized.status, 401);
  assert.equal(unauthorized.headers.get("www-authenticate"), "Bearer");
});

test("queues an OpenAI-shaped request, reuses an idempotency key, and rejects a payload mismatch", async () => {
  const env = isolatedEnv();
  const first = await handleRequest(chatRequest(undefined, "meeting-1"), env);
  assert.equal(first.status, 202);
  const firstBody = await first.json();
  assert.match(firstBody.id, /^chatcmpl-[a-f0-9]{32}$/);
  assert.equal(first.headers.get("location"), `https://dispatch.example/v1/requests/${firstBody.id}`);

  const repeated = await handleRequest(chatRequest(undefined, "meeting-1"), env);
  assert.equal(repeated.status, 202);
  assert.equal((await repeated.json()).id, firstBody.id);
  assert.equal(env.LLM_QUEUE.objects.size, 2); // canonical request + pending-only index

  const conflict = await handleRequest(
    chatRequest([{ role: "user", content: "different" }], "meeting-1"),
    env,
  );
  assert.equal(conflict.status, 409);
});

test("enqueue writes its ready marker before the canonical pending record", async () => {
  class MarkerFirstBucket extends FakeBucket {
    async put(key, value, options = {}) {
      if (key.startsWith("requests/")) {
        assert.ok(
          [...this.objects.keys()].some((candidate) => candidate.startsWith("ready/")),
          "a request write must have a recoverable ready marker already",
        );
      }
      return super.put(key, value, options);
    }
  }
  const env = { ...ENV, LLM_QUEUE: new MarkerFirstBucket() };
  const response = await handleRequest(chatRequest(undefined, "marker-first"), env);
  assert.equal(response.status, 202);
  const marker = [...env.LLM_QUEUE.objects.values()].find((object) => object.key.startsWith("ready/"));
  assert.equal(marker.customMetadata.ready_version, "1");
  assert.equal(marker.customMetadata.status, "pending");
});

test("schema retry clones only a completed request and appends one corrective instruction", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(
    chatRequest([{ role: "user", content: "Extract tags." }], "schema-original"),
    env,
  );
  const { id } = await queued.json();
  const tooEarly = await handleRequest(
    request(`https://dispatch.example/v1/requests/${id}/schema-retry`, {
      method: "POST",
      headers: { "idempotency-key": "schema-correction" },
      body: "{}",
    }),
    env,
  );
  assert.equal(tooEarly.status, 409);
  await dispatchOne(env, okUpstream("malformed"), new Date());

  const retry = await handleRequest(
    request(`https://dispatch.example/v1/requests/${id}/schema-retry`, {
      method: "POST",
      headers: { "idempotency-key": "schema-correction" },
      body: "{}",
    }),
    env,
  );
  assert.equal(retry.status, 202);
  const body = await retry.json();
  assert.notEqual(body.id, id);
  const replacement = await (await env.LLM_QUEUE.get(`requests/${body.id}.json`)).json();
  assert.equal(replacement.status, "pending");
  assert.equal(replacement.request.messages.length, 2);
  assert.match(replacement.request.messages[1].content, /failed local schema validation/);

  const repeated = await handleRequest(
    request(`https://dispatch.example/v1/requests/${id}/schema-retry`, {
      method: "POST",
      headers: { "idempotency-key": "schema-correction" },
      body: "{}",
    }),
    env,
  );
  assert.equal(repeated.status, 202);
  assert.equal((await repeated.json()).id, body.id);

  const noPrompt = await handleRequest(chatRequest(undefined, "schema-no-prompt"), env);
  const { id: noPromptId } = await noPrompt.json();
  const stored = await (await env.LLM_QUEUE.get(`requests/${noPromptId}.json`)).json();
  stored.status = "completed";
  stored.request.messages = [];
  await env.LLM_QUEUE.put(`requests/${noPromptId}.json`, JSON.stringify(stored));
  const noPromptRetry = await handleRequest(
    request(`https://dispatch.example/v1/requests/${noPromptId}/schema-retry`, {
      method: "POST",
      headers: { "idempotency-key": "schema-no-prompt-correction" },
      body: "{}",
    }),
    env,
  );
  assert.equal(noPromptRetry.status, 409);
});

test("the stored record's model is the canonical requested model, not an upstream-shaped string", async () => {
  // CodeRabbit / review/41: `normalizeChatRequest` used to persist a provider/cfg-shaped model
  // string, which was only harmless because `dispatchOne` overwrote it before use -- it would
  // have gone straight to the (broken) credential-resolution path once that bug was fixed.
  const env = isolatedEnv();
  const queued = await handleRequest(
    chatRequest(undefined, "canonical-check", "gemini/gemini-3-flash-preview"),
    env,
  );
  const body = await queued.json();
  const stored = await env.LLM_QUEUE.get(`requests/${body.id}.json`);
  const record = await stored.json();
  assert.equal(record.model, "gemini/gemini-3-flash-preview");
  assert.equal(record.request.model, "gemini/gemini-3-flash-preview");
});

test("accepts the configured default route but rejects an unrecognized model", async () => {
  const env = isolatedEnv();
  const accepted = await handleRequest(chatRequest(undefined, "provider-qualified"), env);
  assert.equal(accepted.status, 202);

  const rejected = await handleRequest(
    request("https://dispatch.example/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify({
        model: "unknown-provider/no-such-model",
        messages: [{ role: "user", content: "wrong route" }],
      }),
    }),
    env,
  );
  assert.equal(rejected.status, 400);
});

test("cron claims one request, resolves the route's own provider credentials, and stores the result", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(chatRequest(), env);
  const queuedBody = await queued.json();
  const calls = [];
  const upstream = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    return new Response(
      JSON.stringify({
        id: "upstream-completion",
        object: "chat.completion",
        choices: [{ index: 0, message: { role: "assistant", content: "Done." } }],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };

  const result = await dispatchOne(env, upstream, new Date());
  assert.equal(result.status, "completed");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.mistral.ai/v1/chat/completions");
  assert.equal(calls[0].init.headers.authorization, "Bearer mistral-secret");
  assert.equal(calls[0].body.model, "mistral-large-2512");
  assert.equal(calls[0].body.stream, false);
  assert.ok(calls[0].init.signal instanceof AbortSignal);

  const poll = await handleRequest(
    request(`https://dispatch.example/v1/requests/${queuedBody.id}`, { method: "GET" }),
    env,
  );
  assert.equal(poll.status, 200);
  assert.equal((await poll.json()).choices[0].message.content, "Done.");
});

test("a retired request is terminal to pollers and is never dispatched from a stale ready marker", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(chatRequest(undefined, "retired-request"), env);
  const { id } = await queued.json();
  const key = `requests/${id}.json`;
  const stored = await env.LLM_QUEUE.get(key);
  const record = await stored.json();
  record.status = "retired";
  await env.LLM_QUEUE.put(key, JSON.stringify(record), { onlyIf: { etagMatches: stored.etag } });

  const poll = await handleRequest(
    request(`https://dispatch.example/v1/requests/${id}`, { method: "GET" }),
    env,
  );
  assert.equal(poll.status, 410);
  assert.equal((await poll.json()).error.code, "retired");

  let dispatched = false;
  await dispatchOne(env, async () => {
    dispatched = true;
    return new Response();
  });
  assert.equal(dispatched, false);
});

test("a non-Mistral route resolves its own provider's URL and API key, not Mistral's", async () => {
  // This is the credential-disclosure bug CodeRabbit flagged as Critical: before the fix, `cfg`
  // (always Mistral-shaped) won every fallback in `resolveProviderCredentials`, so a Gemini route's
  // key was sent to api.mistral.ai with a Mistral model string.
  const env = isolatedEnv();
  await handleRequest(chatRequest(undefined, "gemini-route", "gemini/gemini-3-flash-preview"), env);
  const calls = [];
  const upstream = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ id: "gemini-completion", choices: [] }), { status: 200 });
  };

  const result = await dispatchOne(env, upstream, new Date());
  assert.equal(result.status, "completed");
  assert.equal(calls[0].url, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions");
  assert.equal(calls[0].init.headers.authorization, "Bearer gemini-primary-secret");
  assert.equal(calls[0].body.model, "gemini-3-flash-preview");
});

test("dispatch applies a route's relaxed structured-output profile only to the upstream copy", async () => {
  const env = isolatedEnv();
  const responseFormat = {
    type: "json_schema",
    json_schema: {
      name: "Response",
      schema: {
        type: "object",
        properties: {
          answer: { type: "string", minLength: 1, maxLength: 100 },
          confidence: { type: "number", minimum: 0, maximum: 1 },
          items: { type: "array", minItems: 1, maxItems: 10 },
        },
      },
    },
  };
  const queued = await handleRequest(
    chatRequest(
      [{ role: "user", content: "structured output" }],
      "gemma-relaxed-schema",
      "google/gemma-4-31b-it",
      { response_format: responseFormat },
    ),
    env,
  );
  const { id } = await queued.json();
  const calls = [];
  const result = await dispatchOne(env, async (_url, init) => {
    calls.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ id: "gemma-result", choices: [] }), { status: 200 });
  }, new Date());

  assert.equal(result.status, "completed");
  const sentSchema = calls[0].response_format.json_schema.schema;
  const sentSchemaText = JSON.stringify(sentSchema);
  for (const key of ["minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"]) {
    assert.equal(sentSchemaText.includes(`\"${key}\"`), false, key);
  }

  const stored = await env.LLM_QUEUE.get(`requests/${id}.json`);
  const storedRecord = await stored.json();
  assert.equal(storedRecord.request.response_format.json_schema.schema.properties.answer.minLength, 1);
  assert.equal(storedRecord.status, "completed");
});

test("non-2xx upstream responses retain bounded structured diagnostics in the private record", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(
    chatRequest(undefined, "provider-400", "gemini/gemini-3-flash-preview"),
    env,
  );
  const { id } = await queued.json();
  const result = await dispatchOne(
    env,
    async () =>
      new Response(
        JSON.stringify({
          error: {
            code: 400,
            status: "INVALID_ARGUMENT",
            message: "response_schema contains an unsupported keyword",
          },
        }),
        { status: 400, headers: { "content-type": "application/json" } },
      ),
    new Date(),
  );

  assert.equal(result.status, "failed");
  assert.equal(result.upstreamStatus, 400);
  assert.equal(result.providerCode, 400);
  assert.equal(result.providerStatus, "INVALID_ARGUMENT");

  const stored = await env.LLM_QUEUE.get(`requests/${id}.json`);
  const record = await stored.json();
  assert.equal(record.error.provider_error.format, "json");
  assert.equal(record.error.provider_error.content_type, "application/json");
  assert.equal(record.error.provider_error.json_type, "object");
  assert.deepEqual(record.error.provider_error.top_level_keys, ["error"]);
  assert.deepEqual(record.error.provider_error.error_keys, ["code", "status", "message"]);
  assert.equal(record.error.provider_error.diagnostic_path, "root.error");
  assert.equal(record.error.provider_error.code, 400);
  assert.equal(record.error.provider_error.status, "INVALID_ARGUMENT");
  assert.equal(record.error.provider_error.message, "response_schema contains an unsupported keyword");
  assert.match(record.error.provider_error.body_preview, /response_schema contains/);

  const budget = await (await env.LLM_QUEUE.get("state/dispatch_budget.json")).json();
  const entry = Object.values(budget.routes)[0];
  assert.equal(entry.requests_minute, 1, "a provider attempt remains rate-accounted on failure");
  assert.deepEqual(entry.inflight, {});

  const poll = await handleRequest(
    request(`https://dispatch.example/v1/requests/${id}`, { method: "GET" }),
    env,
  );
  const pollBody = await poll.json();
  assert.equal(poll.status, 502);
  assert.equal(pollBody.error.message, "upstream LLM dispatch failed");
  assert.equal(pollBody.error.provider_error, undefined);
});

test("RPM is a continuous per-route pace, not a burstable minute bucket", async (t) => {
  // Mistral Large's physical route is configured at 4 RPM, so its next request is eligible
  // 15 seconds after the previous reservation. Its provider has only one configured account, so
  // a second immediate request must remain pending rather than rotating around the route pace.
  //
  // The clock is frozen for the whole test (CodeRabbit, review/41): `enqueue()` stamps its own
  // `new Date()` independently of what the test passes to `dispatchOne`, so an unmocked clock
  // could straddle a real minute boundary across eleven awaited round-trips, reset
  // `requests_minute`, and make this flake on primary capacity that was never actually exhausted.
  t.mock.timers.enable({ apis: ["Date"], now: new Date("2026-08-06T12:00:00.000Z") });

  const env = isolatedEnv();
  const calls = [];
  const upstream = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ id: `c-${calls.length}`, choices: [] }), { status: 200 });
  };

  await handleRequest(
    chatRequest(undefined, "mistral-paced-1"),
    env,
  );
  assert.equal((await dispatchOne(env, upstream, new Date())).status, "completed");
  await handleRequest(
    chatRequest(undefined, "mistral-paced-2"),
    env,
  );
  const blocked = await dispatchOne(env, upstream, new Date());
  assert.equal(blocked.status, "no_capacity");
  assert.equal((await (await env.LLM_QUEUE.get(`requests/${blocked.requestId}.json`)).json()).status, "pending");
  assert.equal(
    (await dispatchOne(env, upstream, new Date("2026-08-06T12:00:15.000Z"))).status,
    "completed",
  );
  assert.equal(calls.length, 2);
});

test("a route with no capacity anywhere is requeued (no_capacity), not permanently failed", async (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: new Date("2026-08-06T12:00:00.000Z") });
  const env = isolatedEnv();
  for (let i = 0; i < 4; i += 1) {
    await handleRequest(chatRequest(undefined, `req-${i}`), env);
  }
  await handleRequest(chatRequest(undefined, "fifth"), env);
  for (const object of (await env.LLM_QUEUE.list({ prefix: "requests/" })).objects) {
    const record = await (await env.LLM_QUEUE.get(object.key)).json();
    record.attempts = 3;
    await env.LLM_QUEUE.put(object.key, JSON.stringify(record));
  }
  const now = new Date("2026-08-06T12:00:00.000Z");

  for (let i = 0; i < 4; i += 1) {
    const res = await dispatchOne(env, okUpstream(), new Date(now.getTime() + i * 15_000));
    assert.equal(res.status, "completed");
  }
  const exhausted = await dispatchOne(env, okUpstream(), new Date(now.getTime() + 45_000));
  assert.equal(exhausted.status, "no_capacity");
  const stored = await env.LLM_QUEUE.get(`requests/${exhausted.requestId}.json`);
  const record = await stored.json();
  assert.equal(record.status, "pending");
  assert.equal(record.attempts, 3); // capacity deferral is not a real attempt

  // The next paced slot and minute boundary free Mistral's capacity back up.
  const nextMinute = new Date(now.getTime() + 60_000);
  const recovered = await dispatchOne(env, okUpstream(), nextMinute);
  assert.equal(recovered.status, "completed");
});

test("provider RPM paces different models through one shared schedule", async (t) => {
  t.mock.timers.enable({ apis: ["Date"], now: new Date("2026-08-06T12:00:00.000Z") });
  const env = isolatedEnv();
  const calls = [];
  const upstream = async (_url, init) => {
    calls.push(JSON.parse(init.body).model);
    return new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 });
  };

  await handleRequest(chatRequest(undefined, "mistral-codestral", "mistral/codestral-2508"), env);
  assert.equal((await dispatchOne(env, upstream, new Date())).status, "completed");

  // The route-level intervals differ, but Mistral's provider-wide 60 RPM cap is shared by both.
  await handleRequest(chatRequest(undefined, "mistral-small", "mistral/mistral-small-2603"), env);
  const blocked = await dispatchOne(env, upstream, new Date());
  assert.equal(blocked.status, "no_capacity");
  assert.equal(
    (await dispatchOne(env, upstream, new Date("2026-08-06T12:00:01.000Z"))).status,
    "completed",
  );
  assert.deepEqual(calls, ["codestral-2508", "mistral-small-2603"]);
});

test("legacy DeepSeek aliases use the unified free candidate pool", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(
    chatRequest(undefined, "deepseek-alias", "opencode/deepseek-v4-flash-free"),
    env,
  );
  const body = await queued.json();

  const calls = [];
  const upstream = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return new Response(JSON.stringify({ id: "deepseek-free", choices: [] }), { status: 200 });
  };

  const result = await dispatchOne(env, upstream, new Date());
  assert.equal(result.status, "completed");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://opencode.ai/zen/v1/chat/completions");
  assert.equal(calls[0].body.model, "deepseek-v4-flash-free");
  const stored = await env.LLM_QUEUE.get(`requests/${body.id}.json`);
  const record = await stored.json();
  assert.equal(record.status, "completed");
  assert.equal(record.model, "deepseek/deepseek-v4-flash");
});

test("a request for a canonical model with no configured route fails permanently", async () => {
  const env = isolatedEnv();
  const now = new Date();
  const record = {
    schema: 1,
    id: "chatcmpl-unrouted",
    status: "pending",
    provider: "nowhere",
    model: "nowhere/no-such-route",
    request: { model: "nowhere/no-such-route", messages: [{ role: "user", content: "x" }], stream: false },
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
    available_at: now.toISOString(),
    attempts: 0,
    policy: { estimated_tokens: 100, allow_paid: true },
  };
  await env.LLM_QUEUE.put(`requests/${record.id}.json`, JSON.stringify(record));
  await env.LLM_QUEUE.put(readyKey(record), JSON.stringify(readyMarker(record)));

  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "no_configured_route");
});

test("ordered ready index drains work without reading terminal request history", async () => {
  const env = isolatedEnv();
  const now = new Date();
  const timestamp = now.toISOString();
  const terminal = {
    id: "chatcmpl-terminal", status: "completed", model: "mistral/mistral-large-2512",
    created_at: timestamp, updated_at: timestamp, available_at: timestamp, response: { choices: [] },
  };
  const pending = {
    id: "chatcmpl-ready", status: "pending", model: "mistral/mistral-large-2512",
    request: { model: "mistral/mistral-large-2512", messages: [{ role: "user", content: "ready" }], stream: false },
    created_at: timestamp, updated_at: timestamp, available_at: timestamp, attempts: 0, policy: {},
  };
  await env.LLM_QUEUE.put("requests/000-terminal.json", JSON.stringify(terminal));
  await env.LLM_QUEUE.put("requests/chatcmpl-ready.json", JSON.stringify(pending));
  await env.LLM_QUEUE.put(readyKey(pending), JSON.stringify(readyMarker(pending)));

  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "completed");
  assert.equal(result.requestId, pending.id);
  assert.equal(await env.LLM_QUEUE.get(readyKey(pending)), null);
});

test("malformed ready markers are removed instead of blocking the queue head", async () => {
  const env = isolatedEnv();
  const key = "ready/000000000000000-1-fast-000000000000000-malformed.json";
  await env.LLM_QUEUE.put(key, "not JSON");

  const result = await dispatchOne(env, okUpstream(), new Date());
  assert.equal(result.status, "index_repaired");
  assert.equal(await env.LLM_QUEUE.get(key), null);
});

test("ready-marker lookahead skips a blocked provider and dispatches a later provider", async () => {
  const env = isolatedEnv();
  const first = await handleRequest(
    chatRequest([{ role: "user", content: "blocked provider" }], "aaa-mistral-blocked"),
    env,
  );
  const second = await handleRequest(
    chatRequest(
      [{ role: "user", content: "independent provider" }],
      "bbb-gemini-ready",
      "gemini/gemini-3-flash-preview",
    ),
    env,
  );
  const firstId = (await first.json()).id;
  const secondId = (await second.json()).id;
  const now = new Date();
  await env.LLM_QUEUE.put(
    "state/dispatch_budget.json",
    JSON.stringify({
      version: 1,
      routes: {
        mistral_large_2512_primary: {
          requests_minute: 1,
          requests_day: 1,
          tokens_minute: 0,
          requests_available_at: new Date(now.getTime() + 60_000).toISOString(),
        },
      },
    }),
  );

  const calls = [];
  const result = await dispatchBatch(
    env,
    async (url, init) => {
      calls.push({ url, body: JSON.parse(init.body) });
      return new Response(JSON.stringify({ id: "gemini-result", choices: [] }), { status: 200 });
    },
    now,
    4,
  );

  assert.equal(result.status, "completed");
  assert.equal(result.count, 1);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /generativelanguage\.googleapis\.com/);
  assert.equal(calls[0].body.model, "gemini-3-flash-preview");
  assert.equal((await (await env.LLM_QUEUE.get(`requests/${firstId}.json`)).json()).status, "pending");
  assert.equal((await (await env.LLM_QUEUE.get(`requests/${secondId}.json`)).json()).status, "completed");
});

test("ready keys order eligibility before priority and priority within a tie", () => {
  const at = "2026-08-13T19:20:00.000Z";
  const records = [
    { id: "long-early", created_at: at, available_at: "2026-08-13T19:19:00.000Z", policy: { timeout_class: "long" } },
    { id: "long", created_at: at, available_at: at, policy: { timeout_class: "long" } },
    { id: "fast", created_at: at, available_at: at, policy: {} },
    { id: "urgent", created_at: at, available_at: at, policy: { submit_next: true } },
  ];
  const orderedIds = [...records]
    .sort((left, right) => readyKey(left).localeCompare(readyKey(right)))
    .map((record) => record.id);
  assert.deepEqual(orderedIds, ["long-early", "urgent", "fast", "long"]);
});

test("an aliased ready marker dispatches without an index-repair delay", async () => {
  const env = isolatedEnv();
  const now = new Date("2026-08-13T19:20:00.000Z");
  const record = {
    id: "chatcmpl-aliased-ready",
    status: "pending",
    model: "opencode/deepseek-v4-flash-free",
    request: {
      model: "opencode/deepseek-v4-flash-free",
      messages: [{ role: "user", content: "x" }],
      stream: false,
    },
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
    available_at: now.toISOString(),
    attempts: 0,
    policy: {},
  };
  await env.LLM_QUEUE.put(`requests/${record.id}.json`, JSON.stringify(record));
  await env.LLM_QUEUE.put(readyKey(record), JSON.stringify(readyMarker(record)));

  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "completed");
});

test("queue reindex endpoint is authenticated but intentionally delegated to the offline migrator", async () => {
  const env = isolatedEnv();
  const missingAuth = await handleRequest(
    new Request("https://dispatch.example/v1/queue/reindex", { method: "POST" }),
    env,
  );
  assert.equal(missingAuth.status, 401);
  const invalidAuth = await handleRequest(
    new Request("https://dispatch.example/v1/queue/reindex", {
      method: "POST",
      headers: { authorization: "Bearer wrong" },
    }),
    env,
  );
  assert.equal(invalidAuth.status, 401);

  const response = await handleRequest(
    request("https://dispatch.example/v1/queue/reindex", { method: "POST" }),
    env,
  );
  assert.equal(response.status, 410);
  assert.match(await response.text(), /reindex_llm_dispatch_queue/);
});

test("a lost terminal write preserves the pending marker for a later retry", async () => {
  class RejectTerminalWriteBucket extends FakeBucket {
    async put(key, value, options = {}) {
      if (key.startsWith("requests/") && options.onlyIf?.etagMatches) {
        const record = JSON.parse(typeof value === "string" ? value : await new Response(value).text());
        if (record.status === "completed") return null;
      }
      return super.put(key, value, options);
    }
  }

  const env = { ...ENV, LLM_QUEUE: new RejectTerminalWriteBucket() };
  const queued = await handleRequest(chatRequest(undefined, "lost-terminal-write"), env);
  const { id } = await queued.json();
  await dispatchOne(env, okUpstream(), new Date());

  const record = await (await env.LLM_QUEUE.get(`requests/${id}.json`)).json();
  assert.ok(await env.LLM_QUEUE.get(readyKey(record)));
  assert.equal((await (await env.LLM_QUEUE.get(`requests/${id}.json`)).json()).status, "pending");
});

test("ready-index lookup ignores a large terminal request history", async () => {
  class NoBodyFetchForTerminalBucket extends FakeBucket {
    async get(key) {
      if (key.startsWith("requests/terminal-")) {
        throw new Error("the scheduler must not fetch terminal history");
      }
      return super.get(key);
    }
  }
  const env = { ...ENV, LLM_QUEUE: new NoBodyFetchForTerminalBucket() };
  const queued = await handleRequest(chatRequest(undefined, "second-in-scan"), env);
  const queuedBody = await queued.json();
  const now = new Date();
  const timestamp = now.toISOString();
  const terminal = {
    schema: 1,
    id: "chatcmpl-terminal",
    status: "completed",
    provider: "mistral",
    model: "mistral/mistral-large-2512",
    created_at: timestamp,
    updated_at: timestamp,
    available_at: timestamp,
    attempts: 1,
    response: { id: "already-complete", choices: [] },
  };
  await env.LLM_QUEUE.put("requests/terminal-000.json", JSON.stringify(terminal), {
    customMetadata: { status: "completed", model: terminal.model, available_at: timestamp },
  });

  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "completed");
  assert.equal(result.requestId, queuedBody.id);
});

test("429 responses retry with bounded diagnostics without storing a non-JSON provider body", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(chatRequest(undefined, "retry-me"), env);
  const queuedBody = await queued.json();
  let calls = 0;
  const upstream = async () => {
    calls += 1;
    if (calls === 1) {
      return new Response("provider prompt and secret must not be stored", {
        status: 429,
        headers: { "retry-after": "120" },
      });
    }
    return new Response(JSON.stringify({ id: "recovered", choices: [] }), { status: 200 });
  };

  const firstAt = new Date();
  assert.equal((await dispatchOne(env, upstream, firstAt)).status, "retrying");
  const stored = await env.LLM_QUEUE.get(`requests/${queuedBody.id}.json`);
  const retryRecord = await stored.json();
  assert.equal(retryRecord.status, "pending");
  assert.equal(retryRecord.last_error.status, 429);
  assert.deepEqual(retryRecord.last_error.provider_error, {
    content_type: "text/plain",
    bytes: 45,
    format: "non_json",
  });
  assert.equal(retryRecord.last_error.provider_error.body_preview, undefined);
  assert.doesNotMatch(JSON.stringify(retryRecord), /provider prompt and secret/);
  assert.equal(retryRecord.error, undefined);
  assert.equal((await dispatchOne(env, upstream, new Date(firstAt.getTime() + 61_000))).status, "idle");
  assert.equal((await dispatchOne(env, upstream, new Date(firstAt.getTime() + 121_000))).status, "completed");
  assert.equal(calls, 2);
});

test("atypical JSON errors retain safe shape metadata and nested details", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(
    chatRequest(undefined, "nested-provider-400", "gemini/gemini-3-flash-preview"),
    env,
  );
  const { id } = await queued.json();
  const result = await dispatchOne(
    env,
    async () =>
      new Response(
        JSON.stringify({
          error: {
            details: [{ reason: "unsupported_schema", detail: "schema rejected" }],
          },
        }),
        { status: 400, headers: { "content-type": "application/problem+json; charset=utf-8" } },
      ),
    new Date(),
  );

  assert.equal(result.status, "failed");
  const stored = await env.LLM_QUEUE.get(`requests/${id}.json`);
  const providerError = (await stored.json()).error.provider_error;
  assert.equal(providerError.content_type, "application/problem+json");
  assert.equal(providerError.json_type, "object");
  assert.deepEqual(providerError.top_level_keys, ["error"]);
  assert.deepEqual(providerError.error_keys, ["details"]);
  assert.equal(providerError.diagnostic_path, "root.error.details[0]");
  assert.equal(providerError.message, "schema rejected");
  assert.equal(providerError.reason, undefined);
  assert.equal(providerError.provider_reason, "unsupported_schema");
  assert.match(providerError.body_preview, /unsupported_schema/);
});

test("oversized terminal upstream errors retain a bounded preview and truncation metadata", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(
    chatRequest(undefined, "oversized-provider-400", "gemini/gemini-3-flash-preview"),
    env,
  );
  const { id } = await queued.json();
  const oversizedBody = `{"error":{"message":"${"x".repeat(9_000)}"}}`;
  const result = await dispatchOne(
    env,
    async () =>
      new Response(oversizedBody, {
        status: 400,
        headers: { "content-type": "application/json; charset=utf-8" },
      }),
    new Date(),
  );

  assert.equal(result.status, "failed");
  const stored = await env.LLM_QUEUE.get(`requests/${id}.json`);
  const providerError = (await stored.json()).error.provider_error;
  assert.equal(providerError.format, "too_large");
  assert.equal(providerError.content_type, "application/json");
  assert.equal(providerError.truncated, true);
  assert.equal(providerError.bytes, new TextEncoder().encode(oversizedBody).byteLength);
  assert.equal(providerError.body_preview.length, 8 * 1024);
  assert.equal(providerError.body_preview, oversizedBody.slice(0, 8 * 1024));
});

test("streaming requests and oversized request bodies are rejected", async () => {
  const env = isolatedEnv();
  assert.equal(config(env).maxRequestBytes, 8 * 1024 * 1024);
  const streaming = await handleRequest(
    request("https://dispatch.example/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify({ messages: [{ role: "user", content: "x" }], stream: true }),
    }),
    env,
  );
  assert.equal(streaming.status, 400);

  const oversizedEnv = { ...env, MAX_REQUEST_BYTES: "8" };
  const oversized = await handleRequest(
    request("https://dispatch.example/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify({ messages: [{ role: "user", content: "too large" }] }),
    }),
    oversizedEnv,
  );
  assert.equal(oversized.status, 413);
});

test("an unexpired cron lease rejects a concurrent dispatchOne invocation", async () => {
  const env = isolatedEnv();
  await handleRequest(chatRequest(undefined, "leased"), env);
  const now = new Date();
  await env.LLM_QUEUE.put(
    "locks/cron.json",
    JSON.stringify({
      owner: "some-other-invocation",
      acquired_at: now.toISOString(),
      expires_at: new Date(now.getTime() + 90_000).toISOString(),
    }),
    { onlyIf: { etagDoesNotMatch: "*" } },
  );

  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "lease_busy");
});

test("releasing the cron lease only deletes it if the owner still matches", async () => {
  // Simulate invocation A's lease expiring and being replaced by invocation B's, then A finally
  // getting around to releasing what it thinks is still its own lease -- A's release must not
  // delete B's still-live lease (CodeRabbit index.js:434).
  const bucket = new FakeBucket();
  const now = new Date();
  await acquireCronLease(bucket, now, "invocation-a", 30);
  // A's lease has now "expired" from B's point of view; B acquires a fresh one.
  const later = new Date(now.getTime() + 31_000);
  const acquiredByB = await acquireCronLease(bucket, later, "invocation-b", 90);
  assert.equal(acquiredByB, true);

  await releaseCronLease(bucket, "invocation-a"); // A's stale, delayed release
  const stillThere = await bucket.get("locks/cron.json");
  assert.ok(stillThere, "B's lease must survive A's stale release");
  assert.equal((await stillThere.json()).owner, "invocation-b");

  await releaseCronLease(bucket, "invocation-b"); // B's own, matching release
  const released = await bucket.get("locks/cron.json");
  assert.equal((await released.json()).owner, "invocation-b");
  assert.equal(Date.parse((await released.json()).expires_at) <= Date.now(), true);
});

test("renewing the cron lease is owner-checked and CAS-safe", async () => {
  const bucket = new FakeBucket();
  const first = new Date("2026-08-06T12:00:00Z");
  assert.equal(await acquireCronLease(bucket, first, "invocation-a", 30), true);

  const renewed = new Date(first.getTime() + 10_000);
  assert.equal(await renewCronLease(bucket, renewed, "invocation-a", 30), true);
  const current = await bucket.get("locks/cron.json");
  const lease = await current.json();
  assert.equal(lease.owner, "invocation-a");
  assert.equal(lease.renewed_at, renewed.toISOString());
  assert.equal(lease.expires_at, new Date(renewed.getTime() + 30_000).toISOString());

  assert.equal(await renewCronLease(bucket, renewed, "wrong-owner", 30), false);
  const later = new Date(renewed.getTime() + 31_000);
  assert.equal(
    await renewCronLease(bucket, later, "invocation-a", 30),
    false,
    "the original owner must not resurrect an expired lease",
  );
  assert.equal(await acquireCronLease(bucket, later, "invocation-b", 30), true);
  assert.equal(await renewCronLease(bucket, later, "invocation-a", 30), false);
});

test("ready lookup uses listed marker metadata with 10,000 queued requests", async () => {
  class CountingBucket extends FakeBucket {
    constructor() {
      super();
      this.listCalls = 0;
      this.getCalls = [];
    }
    async list(options) {
      this.listCalls += 1;
      return super.list(options);
    }
    async get(key) {
      this.getCalls.push(key);
      return super.get(key);
    }
  }
  const bucket = new CountingBucket();
  const env = { ...ENV, LLM_QUEUE: bucket };
  const now = new Date("2026-08-13T19:20:00Z");
  for (let index = 0; index < 10_000; index += 1) {
    const record = {
      id: `chatcmpl-${String(index).padStart(5, "0")}`,
      status: "pending",
      model: "mistral/mistral-large-2512",
      request: { model: "mistral/mistral-large-2512", messages: [{ role: "user", content: "x" }], stream: false },
      created_at: now.toISOString(), updated_at: now.toISOString(), available_at: now.toISOString(),
      attempts: 0, policy: {},
    };
    await bucket.put(`requests/${record.id}.json`, JSON.stringify(record));
    await bucket.put(readyKey(record), JSON.stringify(readyMarker(record)), {
      customMetadata: readyMarkerMetadata(record),
    });
  }
  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "completed");
  assert.equal(bucket.listCalls, 1);
  assert.equal(bucket.getCalls.filter((key) => key.startsWith("requests/")).length, 1);
  assert.equal(bucket.getCalls.filter((key) => key.startsWith("ready/")).length, 0);
});

test("queue estimate endpoint is retired rather than scanning the whole R2 history", async () => {
  const response = await handleRequest(
    request("https://dispatch.example/v1/queue/estimate", { method: "GET" }),
    isolatedEnv(),
  );
  assert.equal(response.status, 410);
  assert.match(await response.text(), /bounded/);
});

test("deadline-based paid elevation only fires when waiting for free capacity would miss the deadline", async () => {
  const dispatchLimits = {
    providers: { free_co: { reset_timezone: "UTC" }, paid_co: {} },
    model_routes_map: { "svc/model": ["free_route", "paid_route"] },
    routes_by_id: {
      free_route: { route_id: "free_route", provider: "free_co", free: true, rpm: 1 },
      paid_route: {
        route_id: "paid_route",
        provider: "paid_co",
        free: false,
        rpm: 10,
        input_per_token: 0.001,
        output_per_token: 0.002,
      },
    },
  };
  const now = new Date("2026-08-06T12:00:00Z");
  const exhaustedBudget = () => ({
    routes: {
      free_route: { requests_minute: 1, requests_minute_key: now.toISOString().slice(0, 16), tokens_minute: 0 },
    },
  });

  // No deadline at all, allow_paid=true -- the free route will reset within the minute, so
  // paid must not be used yet.
  const noDeadline = selectRoute(
    exhaustedBudget(),
    "svc/model",
    { allow_paid: true, estimated_tokens: 100 },
    now,
    dispatchLimits,
  );
  assert.equal(noDeadline.reason, "no_capacity");

  // A deadline far in the future -- still plenty of time for the free route's next-minute
  // reset, so paid still must not be used.
  const farDeadline = selectRoute(
    exhaustedBudget(),
    "svc/model",
    { allow_paid: true, estimated_tokens: 100, deadline_at: new Date(now.getTime() + 3_600_000).toISOString() },
    now,
    dispatchLimits,
  );
  assert.equal(farDeadline.reason, "no_capacity");

  // A deadline before the free route's next reset -- must elevate to paid now.
  const nearDeadline = selectRoute(
    exhaustedBudget(),
    "svc/model",
    { allow_paid: true, estimated_tokens: 100, deadline_at: new Date(now.getTime() + 5_000).toISOString() },
    now,
    dispatchLimits,
  );
  assert.equal(nearDeadline.chosenRoute?.route_id, "paid_route");

  // allow_paid=false must never elevate, no matter how close the deadline is.
  const disallowedPaid = selectRoute(
    exhaustedBudget(),
    "svc/model",
    { allow_paid: false, estimated_tokens: 100, deadline_at: new Date(now.getTime() + 1_000).toISOString() },
    now,
    dispatchLimits,
  );
  assert.equal(disallowedPaid.reason, "no_capacity");
});

test("capacity retry does not wake for a paid route before paid elevation is allowed", () => {
  const dispatchLimits = {
    providers: { free_co: { reset_timezone: "UTC" }, paid_co: {} },
    model_routes_map: { "svc/model": ["free_route", "paid_route"] },
    routes_by_id: {
      free_route: { route_id: "free_route", provider: "free_co", free: true, rpd: 1 },
      paid_route: { route_id: "paid_route", provider: "paid_co", free: false },
    },
  };
  const now = new Date("2026-08-06T12:00:00Z");
  const budget = {
    routes: {
      free_route: { requests_day: 1, requests_day_key: "2026-08-06" },
      paid_route: { blocked_until: "2026-08-06T12:00:10Z" },
    },
  };
  const policy = {
    allow_paid: true,
    deadline_at: "2026-08-07T01:00:00Z", // after the free route's midnight reset
  };
  assert.equal(selectRoute(budget, "svc/model", policy, now, dispatchLimits).reason, "no_capacity");
  assert.equal(
    nextCapacityRetryAt(budget, "svc/model", policy, now, dispatchLimits).toISOString(),
    "2026-08-07T00:00:00.000Z",
  );
});

test("selectRoute ranks free before paid and cheapest paid first, deterministically", () => {
  const dispatchLimits = {
    providers: { a: {}, b: {}, c: {} },
    model_routes_map: { "svc/model": ["pricey", "free_b", "cheap"] },
    routes_by_id: {
      pricey: { route_id: "pricey", provider: "a", free: false, input_per_token: 0.01, output_per_token: 0.01 },
      free_b: { route_id: "free_b", provider: "b", free: true },
      cheap: { route_id: "cheap", provider: "c", free: false, input_per_token: 0.001, output_per_token: 0.001 },
    },
  };
  const now = new Date("2026-08-06T12:00:00Z");
  const result = selectRoute({ routes: {} }, "svc/model", { allow_paid: false }, now, dispatchLimits);
  assert.equal(result.chosenRoute.route_id, "free_b");

  const ranked = rankRoutes(Object.values(dispatchLimits.routes_by_id));
  assert.deepEqual(
    ranked.map((route) => route.route_id),
    ["free_b", "cheap", "pricey"],
  );
});

test("selectRoute ranks an effective peak price card instead of stale base rates", () => {
  const dispatchLimits = {
    providers: { scheduled: {}, steady: {} },
    model_routes_map: { "svc/model": ["scheduled", "steady"] },
    routes_by_id: {
      scheduled: {
        route_id: "scheduled",
        provider: "scheduled",
        free: false,
        input_per_token: 0.1,
        output_per_token: 0.1,
        pricing: {
          periods: [
            {
              effective_at: "2026-08-16T16:00:00Z",
              input_per_token: 1,
              output_per_token: 1,
              windows: [{ tz: "UTC", start: "01:00", end: "04:00", multiplier: 2 }],
            },
          ],
        },
      },
      steady: {
        route_id: "steady",
        provider: "steady",
        free: false,
        input_per_token: 0.3,
        output_per_token: 0.3,
      },
    },
  };
  const before = selectRoute(
    { routes: {} },
    "svc/model",
    { allow_paid: true },
    new Date("2026-08-16T15:00:00Z"),
    dispatchLimits,
  );
  const peak = selectRoute(
    { routes: {} },
    "svc/model",
    { allow_paid: true },
    new Date("2026-08-17T02:00:00Z"),
    dispatchLimits,
  );
  assert.equal(before.chosenRoute.route_id, "scheduled");
  assert.equal(peak.chosenRoute.route_id, "steady");
});

test("selectRoute defers a flexible request until the route's cheapest pricing window", () => {
  const dispatchLimits = {
    providers: { scheduled: {} },
    model_routes_map: { "svc/model": ["scheduled"] },
    routes_by_id: {
      scheduled: {
        route_id: "scheduled",
        provider: "scheduled",
        free: false,
        input_per_token: 0.1,
        output_per_token: 0.1,
        pricing: {
          periods: [
            {
              effective_at: "2026-08-16T16:00:00Z",
              input_per_token: 1,
              output_per_token: 1,
              windows: [{ tz: "UTC", start: "01:00", end: "04:00", multiplier: 2 }],
            },
          ],
        },
      },
    },
  };
  const peak = new Date("2026-08-17T02:00:00Z");
  const deferred = selectRoute(
    { routes: {} },
    "svc/model",
    { allow_paid: true },
    peak,
    dispatchLimits,
  );
  assert.equal(deferred.chosenRoute, null);
  assert.equal(deferred.reason, "no_capacity");
  assert.equal(
    nextCapacityRetryAt(
      { routes: {} },
      "svc/model",
      { allow_paid: true },
      peak,
      dispatchLimits,
    ).toISOString(),
    "2026-08-17T04:00:00.000Z",
  );

  const urgent = selectRoute(
    { routes: {} },
    "svc/model",
    { allow_paid: true, deadline_at: "2026-08-17T02:30:00Z" },
    peak,
    dispatchLimits,
  );
  assert.equal(urgent.chosenRoute.route_id, "scheduled");

  const offPeak = selectRoute(
    { routes: {} },
    "svc/model",
    { allow_paid: true },
    new Date("2026-08-17T04:00:00Z"),
    dispatchLimits,
  );
  assert.equal(offPeak.chosenRoute.route_id, "scheduled");
});

test("price-window retry rechecks at an earlier effective rate-card transition", () => {
  const dispatchLimits = {
    providers: { scheduled: {} },
    model_routes_map: { "svc/model": ["scheduled"] },
    routes_by_id: {
      scheduled: {
        route_id: "scheduled",
        provider: "scheduled",
        free: false,
        pricing: {
          periods: [
            {
              effective_at: "1970-01-01T00:00:00Z",
              input_per_token: 1,
              windows: [{ tz: "UTC", start: "01:00", end: "04:00", multiplier: 2 }],
            },
            { effective_at: "2026-08-17T03:00:00Z", input_per_token: 0.5 },
          ],
        },
      },
    },
  };
  const now = new Date("2026-08-17T02:00:00Z");
  assert.equal(
    nextCapacityRetryAt({ routes: {} }, "svc/model", { allow_paid: true }, now, dispatchLimits).toISOString(),
    "2026-08-17T03:00:00.000Z",
  );
});

test("pricing windows preserve a zero multiplier and respect a non-UTC zone", () => {
  const dispatchLimits = {
    providers: { discounted: {}, steady: {} },
    model_routes_map: { "svc/model": ["discounted", "steady"] },
    routes_by_id: {
      discounted: {
        route_id: "discounted",
        provider: "discounted",
        free: false,
        input_per_token: 1,
        pricing: {
          windows: [{ tz: "America/Los_Angeles", start: "18:00", end: "20:00", multiplier: 0 }],
        },
      },
      steady: {
        route_id: "steady",
        provider: "steady",
        free: false,
        input_per_token: 0.5,
        rpd: 0,
      },
    },
  };
  const peak = new Date("2026-08-17T02:00:00Z"); // 19:00 PDT
  assert.equal(
    selectRoute({ routes: {} }, "svc/model", { allow_paid: true }, peak, dispatchLimits).chosenRoute.route_id,
    "discounted",
  );
  assert.equal(
    nextCapacityRetryAt(
      { routes: {} },
      "svc/model",
      { allow_paid: true },
      new Date("2026-08-17T00:00:00Z"), // 17:00 PDT
      dispatchLimits,
    ).toISOString(),
    "2026-08-17T01:00:00.000Z",
  );
});

test("localDateTimeToUTC rejects a nonexistent DST wall-clock time", () => {
  assert.equal(
    localDateTimeToUTC({ year: 2026, month: 3, day: 8 }, 2, 30, "America/Los_Angeles"),
    null,
  );
});

test("selectRoute spills a durable request to its next allowed model when the primary is full", () => {
  const dispatchLimits = {
    providers: { primary: {}, backup: {} },
    model_routes_map: { "svc/primary": ["primary"], "svc/backup": ["backup"] },
    routes_by_id: {
      primary: { route_id: "primary", provider: "primary", free: true, rpm: 1 },
      backup: { route_id: "backup", provider: "backup", free: true, rpm: 1 },
    },
  };
  const now = new Date("2026-08-06T12:00:00Z");
  const result = selectRoute(
    { routes: { primary: { requests_available_at: "2026-08-06T12:01:00Z" } } },
    ["svc/primary", "svc/backup"],
    { allow_paid: false, estimated_tokens: 100 },
    now,
    dispatchLimits,
  );
  assert.equal(result.chosenRoute?.route_id, "backup");

});

test("selectRoute skips allowed routes whose input or output context cannot fit the request", () => {
  const dispatchLimits = {
    providers: { primary: {}, backup: {} },
    model_routes_map: { "svc/primary": ["primary"], "svc/backup": ["backup"] },
    routes_by_id: {
      primary: {
        route_id: "primary", provider: "primary", free: true,
        input_context_limit: 10_000, output_context_limit: 1_024,
      },
      backup: {
        route_id: "backup", provider: "backup", free: true,
        input_context_limit: 100_000, output_context_limit: 8_192,
      },
    },
  };
  const result = selectRoute(
    { routes: {} },
    ["svc/primary", "svc/backup"],
    { allow_paid: false, input_tokens_estimate: 20_000, output_token_budget: 2_048 },
    new Date("2026-08-06T12:00:00Z"),
    dispatchLimits,
  );
  assert.equal(result.chosenRoute?.route_id, "backup");

  const outputOnly = selectRoute(
    { routes: {} },
    ["svc/primary", "svc/backup"],
    { allow_paid: false, input_tokens_estimate: 2_000, output_token_budget: 2_048 },
    new Date("2026-08-06T12:00:00Z"),
    dispatchLimits,
  );
  assert.equal(outputOnly.chosenRoute?.route_id, "backup");
});

test("selectRoute preserves a temporarily full allowed route over later context rejection", () => {
  const dispatchLimits = {
    providers: { primary: {}, small: {} },
    model_routes_map: { "svc/primary": ["primary"], "svc/small": ["small"] },
    routes_by_id: {
      primary: {
        route_id: "primary", provider: "primary", free: true, rpm: 1,
        input_context_limit: 100_000, output_context_limit: 8_192,
      },
      small: {
        route_id: "small", provider: "small", free: true,
        input_context_limit: 1_000, output_context_limit: 1_024,
      },
    },
  };
  const result = selectRoute(
    { routes: { primary: { requests_available_at: "2026-08-06T12:01:00Z" } } },
    ["svc/primary", "svc/small"],
    { allow_paid: false, input_tokens_estimate: 2_000, output_token_budget: 1_024 },
    new Date("2026-08-06T12:00:00Z"),
    dispatchLimits,
  );
  assert.equal(result.reason, "no_capacity");
});

test("routeAvailable respects rpm/rpd/tpm/blocked_until independently", () => {
  const now = new Date("2026-08-06T12:00:30Z");
  const route = { rpm: 2, rpd: 5, tpm: 100 };
  assert.equal(
    routeAvailable({ requests_minute: 1, requests_day: 1, tokens_minute: 0 }, route, { requests: 1, tokens: 10 }, now),
    true,
  );
  assert.equal(
    routeAvailable({ requests_minute: 2, requests_day: 1, tokens_minute: 0 }, route, { requests: 1, tokens: 10 }, now),
    false,
  );
  assert.equal(
    routeAvailable({ requests_minute: 0, requests_day: 5, tokens_minute: 0 }, route, { requests: 1, tokens: 10 }, now),
    false,
  );
  assert.equal(
    routeAvailable(
      { requests_minute: 0, requests_day: 0, tokens_minute: 0, tokens_available_at: "2026-08-06T12:01:00Z" },
      route,
      { requests: 1, tokens: 10 },
      now,
    ),
    false,
  );
  assert.equal(
    routeAvailable({ requests_minute: 0, requests_day: 0, tokens_minute: 0 }, route, { requests: 1, tokens: 1000 }, now),
    true,
  );
  assert.equal(
    routeAvailable(
      { requests_minute: 0, requests_day: 0, tokens_minute: 0, blocked_until: "2026-08-06T12:01:00Z" },
      route,
      { requests: 1, tokens: 10 },
      now,
    ),
    false,
  );
});

test("nextLocalMidnightUTC rolls to the correct UTC instant across a negative offset", () => {
  // America/Los_Angeles is UTC-7 in August (PDT) -- local midnight is 07:00 UTC.
  const now = new Date("2026-08-06T15:00:00Z"); // 08:00 PDT
  const next = nextLocalMidnightUTC(now, "America/Los_Angeles");
  assert.equal(next.toISOString(), "2026-08-07T07:00:00.000Z");
});

test("nextRouteReset prefers a live blocked_until over the plain minute/day rollover", () => {
  const now = new Date("2026-08-06T12:00:30Z");
  const route = { rpm: 1, rpd: 10, reset_timezone: "UTC" };
  const blockedUntil = "2026-08-06T12:30:00.000Z";
  const reset = nextRouteReset({ blocked_until: blockedUntil }, route, now);
  assert.equal(reset.toISOString(), blockedUntil);
});

test("nextRouteReset waits for the latest blocking axis", () => {
  const now = new Date("2026-08-06T12:00:30Z");
  const route = { rpm: 60, rpd: 10, reset_timezone: "UTC" };
  const reset = nextRouteReset(
    {
      requests_available_at: "2026-08-06T12:00:35.000Z",
      requests_day: 10,
      blocked_until: "2026-08-06T12:00:40.000Z",
    },
    route,
    now,
  );
  assert.equal(reset.toISOString(), "2026-08-07T00:00:00.000Z");
});

test("resolveProviderCredentials rejects an http:// api_base and a route with no matching account", () => {
  const dispatchLimits = {
    providers: {
      insecure: { api_base: "http://insecure.example", chat_path: "/v1/chat/completions", accounts: [{ id: "primary", api_key_env: "X" }] },
      noaccount: { api_base: "https://noaccount.example", accounts: [] },
    },
  };
  assert.throws(
    () =>
      resolveProviderCredentials(
        { X: "secret" },
        { provider: "insecure", account_id: "primary" },
        dispatchLimits,
      ),
    /HTTPS/,
  );
  assert.throws(
    () =>
      resolveProviderCredentials(
        {},
        { provider: "noaccount", account_id: "primary" },
        dispatchLimits,
      ),
    /no account configured/,
  );
});

test("config() fails fast on a missing required var instead of defaulting to Mistral", () => {
  const { PROVIDER_NAME, ...withoutProvider } = isolatedEnv();
  assert.throws(() => config(withoutProvider), /PROVIDER_NAME is required/);
});

test("Free-plan dispatch defaults allow one request per scheduled batch and run", () => {
  const settings = config(isolatedEnv());
  assert.equal(settings.batchConcurrency, 1);
  assert.equal(settings.maxTotalRequests, 1);
});

test("config() rejects an upstream timeout that is not comfortably under the lease duration", () => {
  const env = { ...isolatedEnv(), UPSTREAM_TIMEOUT_SECONDS: "90", LEASE_DURATION_SECONDS: "90" };
  assert.throws(() => config(env), /must be less than/);
});

test("config() rejects a lane that cannot finish before the run deadline", () => {
  const env = { ...isolatedEnv(), FINALIZATION_RESERVE_SECONDS: "101" };
  assert.throws(() => config(env), /must fit within MAX_EXECUTION_SECONDS/);
});

test("resolveProviderCredentials fails closed on a route naming an unknown account_id", () => {
  // CodeRabbit, review/41: falling back to accounts[0] for a *named-but-unmatched* account_id
  // would reserve the intended (e.g. secondary) route's ledger entry while silently sending the
  // primary account's key -- a real credential-confusion bug for a hand-authored YAML typo.
  const dispatchLimits = {
    providers: {
      gemini: {
        api_base: "https://generativelanguage.googleapis.com/v1beta/openai",
        chat_path: "/chat/completions",
        accounts: [{ id: "project_primary", api_key_env: "GEMINI_API_KEY" }],
      },
    },
  };
  assert.throws(
    () =>
      resolveProviderCredentials(
        { GEMINI_API_KEY: "primary-secret" },
        { provider: "gemini", account_id: "project_typo" },
        dispatchLimits,
      ),
    /no account configured/,
  );
});

test("a bare UPSTREAM_MODEL request is canonicalized before it can ever reach dispatch", async () => {
  // CodeRabbit, review/41: the bare upstream string (e.g. "mistral-large-2512") was accepted at
  // enqueue time but has no entry in model_routes_map, so a record stored under it would always
  // fail permanently at dispatch with no way to retry into success.
  const env = isolatedEnv();
  const bareModelRequest = request("https://dispatch.example/v1/chat/completions", {
    method: "POST",
    headers: { "idempotency-key": "bare-model" },
    body: JSON.stringify({
      model: "mistral-large-2512",
      messages: [{ role: "user", content: "hi" }],
    }),
  });
  const queued = await handleRequest(bareModelRequest, env);
  assert.equal(queued.status, 202);
  const body = await queued.json();
  const stored = await env.LLM_QUEUE.get(`requests/${body.id}.json`);
  const record = await stored.json();
  assert.equal(record.model, "mistral/mistral-large-2512");

  const result = await dispatchOne(env, okUpstream(), new Date());
  assert.equal(result.status, "completed");
});

test("credential-resolution failure never touches the ledger, and a persistent ledger-write failure requeues instead of dispatching unreserved", async () => {
  const env = isolatedEnv();
  await handleRequest(chatRequest(undefined, "cred-then-ledger-failure"), env);

  // First: a missing secret must fail the record without ever writing to the ledger.
  const { MISTRAL_API_KEY, ...withoutMistralKey } = env;
  const credFailure = await dispatchOne(withoutMistralKey, okUpstream(), new Date());
  assert.equal(credFailure.status, "failed");
  assert.equal(await env.LLM_QUEUE.get("state/dispatch_budget.json"), null);

  // A fresh record + a bucket whose ledger writes always lose the CAS race: proves a
  // never-successful reservation requeues the record instead of dispatching with no durable
  // reservation (CodeRabbit, review/41: "do not dispatch after a failed ledger CAS write").
  class WriteBlockedBucket extends FakeBucket {
    async put(key, value, options = {}) {
      if (key === "state/dispatch_budget.json") {
        return null; // simulate every conditional write losing the CAS race
      }
      return super.put(key, value, options);
    }
  }
  const blockedEnv = { ...env, LLM_QUEUE: new WriteBlockedBucket() };
  await handleRequest(chatRequest(undefined, "ledger-write-always-fails"), blockedEnv);
  const posted = { count: 0 };
  const countingUpstream = async () => {
    posted.count += 1;
    return new Response(JSON.stringify({ id: "should-not-happen", choices: [] }), { status: 200 });
  };
  const result = await dispatchOne(blockedEnv, countingUpstream, new Date());
  assert.equal(result.status, "no_capacity");
  assert.equal(posted.count, 0);
});

test("routeAvailable supports concurrency-only routes and enforces the slot", () => {
  const now = new Date("2026-08-06T12:00:00Z");
  // A paid route declaring only concurrency: no rpm, rpd, or tpm.
  const paidConcurrencyOnly = { free: false, concurrency: 5 };
  const entry = { requests_minute: 0, requests_day: 0, tokens_minute: 0, blocked_until: "" };
  assert.equal(routeAvailable(entry, paidConcurrencyOnly, { requests: 1, tokens: 10 }, now), true);
  // A real occupied slot always carries an expiry -- reserveRouteCapacity set one on every
  // reservation it ever wrote. An entry without one is treated as malformed and reaped, so the
  // fixture must be well-formed for this to test the ceiling rather than the reaper.
  const stillHeld = new Date(now.getTime() + 600_000).toISOString();
  entry.inflight = Object.fromEntries(
    Array.from({ length: 5 }, (_, index) => [
      `existing-${index}`,
      { requests: 1, tokens: 10, expires_at: stillHeld },
    ]),
  );
  assert.equal(
    routeAvailable(entry, paidConcurrencyOnly, { requests: 1, tokens: 10 }, now),
    false,
    "a concurrency-only route must reject once all slots are occupied",
  );

  // A free route with no rpm/rpd/tpm should still pass when its slots are empty.
  const freeConcurrencyOnly = { free: true, concurrency: 5 };
  entry.inflight = {};
  assert.equal(
    routeAvailable(entry, freeConcurrencyOnly, { requests: 1, tokens: 10 }, now),
    true,
    "free route with no rpm/rpd/tpm should pass",
  );

  // A paid route with rpm should be checked normally, not fail-closed.
  const paidWithRpm = { free: false, rpm: 10 };
  assert.equal(
    routeAvailable(entry, paidWithRpm, { requests: 1, tokens: 10 }, now),
    true,
    "paid route with rpm should be checked normally",
  );
});

test("idempotency collision detects policy field differences, not just payload", async () => {
  const env = isolatedEnv();

  // First request with allow_paid: true
  const firstReq = request("https://dispatch.example/v1/chat/completions", {
    method: "POST",
    headers: { "idempotency-key": "policy-test-1" },
    body: JSON.stringify({
      model: "mistral/mistral-large-2512",
      messages: [{ role: "user", content: "hello" }],
      allow_paid: true,
    }),
  });
  const first = await handleRequest(firstReq, env);
  assert.equal(first.status, 202);

  // Same payload, same key, but different allow_paid
  const conflictReq = request("https://dispatch.example/v1/chat/completions", {
    method: "POST",
    headers: { "idempotency-key": "policy-test-1" },
    body: JSON.stringify({
      model: "mistral/mistral-large-2512",
      messages: [{ role: "user", content: "hello" }],
      allow_paid: false,
    }),
  });
  const conflict = await handleRequest(conflictReq, env);
  assert.equal(conflict.status, 409, "different allow_paid with same idem key must 409");
});

test("dispatchBatch admits only one same-route request until its RPM interval elapses", async () => {
  const env = isolatedEnv();
  for (let i = 0; i < 4; i += 1) {
    await handleRequest(
      chatRequest([{ role: "user", content: `batch item ${i}` }], `item-${i}`, "mistral/mistral-large-2512"),
      env,
    );
  }

  const calls = [];
  const parallelUpstream = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, body });
    return new Response(
      JSON.stringify({
        id: `completion-${calls.length}`,
        choices: [{ message: { role: "assistant", content: `Response ${calls.length}` } }],
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };

  const now = new Date();
  const batchResult = await dispatchBatch(env, parallelUpstream, now, 4);
  assert.equal(batchResult.status, "completed");
  assert.equal(batchResult.count, 1);
  assert.equal(batchResult.completedCount, 1);
  assert.equal(calls.length, 1);

  const listRes = await env.LLM_QUEUE.list({ prefix: "requests/" });
  const completedObjects = listRes.objects.filter((o) => o.customMetadata?.status === "completed");
  assert.equal(completedObjects.length, 1);
});

test("dispatchBatch runs four independently paced routes concurrently", async () => {
  const env = isolatedEnv();
  const models = [
    "mistral/mistral-large-2512",
    "gemini/gemini-3-flash-preview",
    "meta-llama/llama-3.3-70b-instruct",
    "deepseek/deepseek-v4-flash",
  ];
  for (const [index, model] of models.entries()) {
    await handleRequest(
      chatRequest([{ role: "user", content: `independent route ${index}` }], `route-${index}`, model),
      env,
    );
  }

  const calls = [];
  const result = await dispatchBatch(
    env,
    async (_url, init) => {
      calls.push(JSON.parse(init.body).model);
      return new Response(JSON.stringify({ id: `completion-${calls.length}`, choices: [] }), { status: 200 });
    },
    new Date(),
    4,
  );

  assert.equal(result.status, "completed");
  assert.equal(result.count, 4);
  assert.equal(result.completedCount, 4);
  assert.equal(calls.length, 4);
  assert.equal(new Set(calls).size, 4);
  assert.equal(typeof result.profile.ready_heads_ms, "number");
  assert.equal(typeof result.profile.candidate_prepare_ms, "number");
  assert.equal(typeof result.profile.ledger_write_ms, "number");
  assert.equal(typeof result.profile.marker_delete_ms, "number");
  assert.equal(result.results.every((item) => typeof item.profile.total_ms === "number"), true);
  assert.equal(result.results.every((item) => typeof item.profile.upstream_ms === "number"), true);
});

test("a concurrent batch commits usage up front and needs no release CAS", async () => {
  class BudgetCountingBucket extends FakeBucket {
    constructor() {
      super();
      this.budgetGets = 0;
      this.budgetPuts = 0;
    }

    async get(key) {
      if (key === "state/dispatch_budget.json") this.budgetGets += 1;
      return super.get(key);
    }

    async put(key, value, options = {}) {
      if (key === "state/dispatch_budget.json") this.budgetPuts += 1;
      return super.put(key, value, options);
    }
  }

  const bucket = new BudgetCountingBucket();
  const env = { ...ENV, LLM_QUEUE: bucket };
  const models = ["mistral/mistral-large-2512", "gemini/gemini-3-flash-preview"];
  for (const [index, model] of models.entries()) {
    await handleRequest(
      chatRequest([{ role: "user", content: `batched release ${index}` }], `batched-release-${index}`, model),
      env,
    );
  }

  const result = await dispatchBatch(
    env,
    async () => new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 }),
    new Date(),
    2,
  );

  assert.equal(result.completedCount, 2);
  assert.equal(bucket.budgetGets, 1, "one ledger read for the whole batch");
  assert.equal(
    bucket.budgetPuts,
    1,
    "usage is committed once before the upstream calls; there is no reservation to clean up",
  );
  const budget = await (await bucket.get("state/dispatch_budget.json")).json();
  for (const entry of Object.values(budget.routes || {})) {
    assert.deepEqual(entry.inflight || {}, {}, "no durable reservation should ever be written");
  }
  // Durable pacing must still be committed for every dispatched request.
  const used = Object.values(budget.routes).filter((e) => (e.requests_minute || 0) > 0);
  assert.equal(used.length, 2, "both routes must record their durable request usage");
});

test("serial dispatch commits durable usage without an inflight cleanup CAS", async () => {
  class BudgetCountingBucket extends FakeBucket {
    constructor() {
      super();
      this.budgetGets = 0;
      this.budgetPuts = 0;
    }

    async get(key) {
      if (key === "state/dispatch_budget.json") this.budgetGets += 1;
      return super.get(key);
    }

    async put(key, value, options = {}) {
      if (key === "state/dispatch_budget.json") this.budgetPuts += 1;
      return super.put(key, value, options);
    }
  }

  const bucket = new BudgetCountingBucket();
  const env = { ...ENV, LLM_QUEUE: bucket, BATCH_CONCURRENCY: "1", MAX_TOTAL_REQUESTS: "1" };
  await handleRequest(chatRequest(undefined, "serial-ledger"), env);

  const result = await dispatchOne(
    env,
    async () => new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 }),
    new Date(),
  );

  assert.equal(result.status, "completed");
  assert.equal(bucket.budgetGets, 1);
  assert.equal(bucket.budgetPuts, 1);
  const budget = await (await bucket.get("state/dispatch_budget.json")).json();
  const entry = Object.values(budget.routes)[0];
  assert.equal(entry.requests_minute, 1);
  assert.deepEqual(entry.inflight, {});
});

test("a finalization failure records a terminal failure and releases its reservation", async () => {
  class FinalizationFailureBucket extends FakeBucket {
    constructor() {
      super();
      this.failKey = null;
    }

    async put(key, value, options = {}) {
      if (
        key === this.failKey &&
        typeof value === "string" &&
        value.includes('"status":"completed"')
      ) {
        throw new Error("injected finalization failure");
      }
      return super.put(key, value, options);
    }
  }

  const bucket = new FinalizationFailureBucket();
  const env = { ...ENV, LLM_QUEUE: bucket };
  await handleRequest(
    chatRequest([{ role: "user", content: "mistral sibling" }], "sibling-mistral"),
    env,
  );
  await handleRequest(
    chatRequest(
      [{ role: "user", content: "gemini sibling" }],
      "sibling-gemini",
      "gemini/gemini-3-flash-preview",
    ),
    env,
  );
  bucket.failKey = [...bucket.objects.keys()].find((key) => key.startsWith("requests/"));

  const result = await dispatchBatch(
    env,
    async () => new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 }),
    new Date(),
    2,
  );
  assert.equal(result.status, "completed");
  assert.deepEqual(result.results.map((item) => item.status), ["failed", "completed"]);
  const failed = await bucket.get(bucket.failKey);
  assert.equal((await failed.json()).status, "failed");
  const ledger = await bucket.get("state/dispatch_budget.json");
  for (const entry of Object.values((await ledger.json()).routes || {})) {
    assert.deepEqual(entry.inflight || {}, {});
  }
});

test("the Free-plan scheduler prioritizes fast work before long work", async () => {
  const env = isolatedEnv();
  for (let index = 0; index < 4; index += 1) {
    await handleRequest(chatRequest(undefined, `fast-${index}`), env);
  }
  await handleRequest(
    chatRequest(undefined, "long", undefined, { timeout_class: "long" }),
    env,
  );

  const selected = [];
  const now = new Date();
  await dispatchBatch(
    env,
    async (_url, init) => {
      selected.push(JSON.parse(init.body).messages[0].content);
      return new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 });
    },
    now,
    4,
    null,
    now.getTime() + 820_000,
  );
  assert.equal(selected.length, 1);
  const records = await env.LLM_QUEUE.list({ prefix: "requests/" });
  const completed = await Promise.all(
    records.objects.map(async (object) => ({ key: object.key, record: await (await env.LLM_QUEUE.get(object.key)).json() })),
  );
  assert.ok(completed.some(({ record }) => !record.policy?.timeout_class && record.status === "completed"));
  assert.ok(completed.some(({ record }) => record.policy?.timeout_class === "long" && record.status === "pending"));
});

test("scheduled dispatch stops at MAX_TOTAL_REQUESTS after acquiring and renewing its lease", async () => {
  const env = { ...isolatedEnv(), MAX_TOTAL_REQUESTS: "1", BATCH_CONCURRENCY: "1" };
  const ids = [];
  for (let index = 0; index < 2; index += 1) {
    const queued = await handleRequest(chatRequest(undefined, `scheduled-${index}`), env);
    ids.push((await queued.json()).id);
  }

  const clock = Date.now();
  const result = await runScheduled(env, { fetchImpl: okUpstream(), nowMs: () => clock });
  assert.equal(result.status, "dispatched");
  assert.equal(result.totalDispatched, 1);
  const records = await Promise.all(
    ids.map(async (id) => (await env.LLM_QUEUE.get(`requests/${id}.json`)).json()),
  );
  assert.equal(records.filter((record) => record.status === "completed").length, 1);
  assert.equal(records.filter((record) => record.status === "pending").length, 1);
});

test("scheduled dispatch does not start a batch after its execution deadline", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(chatRequest(undefined, "scheduled-deadline"), env);
  const { id } = await queued.json();
  const ticks = [0, 0, 820_000];
  let upstreamCalls = 0;
  const result = await runScheduled(env, {
    fetchImpl: async () => {
      upstreamCalls += 1;
      return new Response(JSON.stringify({ id: "unexpected", choices: [] }), { status: 200 });
    },
    nowMs: () => ticks.shift() ?? 820_000,
  });
  assert.equal(result.status, "idle");
  assert.equal(result.totalDispatched, 0);
  assert.equal(upstreamCalls, 0);
  assert.equal((await (await env.LLM_QUEUE.get(`requests/${id}.json`)).json()).status, "pending");
});

test("deadline guard requeues work that cannot finish with finalization reserve", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(chatRequest(undefined, "deadline-guard"), env);
  const body = await queued.json();
  const calls = [];
  const now = new Date();
  const result = await dispatchBatch(
    env,
    async () => {
      calls.push(true);
      return new Response(JSON.stringify({ choices: [] }), { status: 200 });
    },
    now,
    1,
    null,
    now.getTime() + 100_000,
  );
  assert.equal(result.status, "deadline_guard");
  assert.equal(calls.length, 0);
  const stored = await env.LLM_QUEUE.get(`requests/${body.id}.json`);
  assert.equal((await stored.json()).status, "pending");
});

test("upstream LLM timeout tags last_error with upstream_timeout, duration, and route metadata", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(chatRequest(undefined, "timeout-check"), env);
  const queuedBody = await queued.json();

  const timingOutUpstream = async () => {
    const error = new Error("The operation was aborted due to timeout");
    error.name = "TimeoutError";
    throw error;
  };

  const now = new Date();
  const result = await dispatchOne(env, timingOutUpstream, now);
  assert.equal(result.status, "retrying");
  assert.equal(result.error, "upstream_timeout");

  const stored = await env.LLM_QUEUE.get(`requests/${queuedBody.id}.json`);
  const record = await stored.json();
  assert.equal(record.status, "pending");
  assert.equal(record.attempts, 1);
  assert.equal(record.last_error?.code, "upstream_timeout");
  assert.equal(record.last_error?.model, "mistral/mistral-large-2512");
  assert.equal(record.last_error?.route_id, "mistral_large_2512_primary");
  assert.ok(record.last_error?.duration_seconds >= 0);
  assert.match(record.last_error?.message, /timed out/);
});

test("exhausting retry attempts on timeout fails permanently with upstream_timeout", async () => {
  const env = isolatedEnv();
  const queued = await handleRequest(chatRequest(undefined, "terminal-timeout-check"), env);
  const queuedBody = await queued.json();

  // Pre-seed attempts to 4 so 5th attempt triggers terminal failure
  const key = `requests/${queuedBody.id}.json`;
  const existing = await env.LLM_QUEUE.get(key);
  const data = await existing.json();
  data.attempts = 4;
  await env.LLM_QUEUE.put(key, JSON.stringify(data));

  const timingOutUpstream = async () => {
    const error = new Error("The operation was aborted due to timeout");
    error.name = "TimeoutError";
    throw error;
  };

  const now = new Date();
  const result = await dispatchOne(env, timingOutUpstream, now);
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "upstream_timeout");

  const stored = await env.LLM_QUEUE.get(key);
  const record = await stored.json();
  assert.equal(record.status, "failed");
  assert.equal(record.attempts, 5);
  assert.equal(record.error?.code, "upstream_timeout");
  assert.equal(record.error?.route_id, "mistral_large_2512_primary");
  assert.match(record.error?.message, /timed out/);
});

// ---------------------------------------------------------------------------------------------
// CPU-cost regressions.
//
// Worker CPU on the Free-plan cron is dominated by how many R2 operations a dispatch performs and
// how many bytes cross that boundary, not by the time spent waiting on them.  These tests pin the
// operation shape so a future change cannot quietly reintroduce a redundant round trip.
// ---------------------------------------------------------------------------------------------

class RecordingBucket extends FakeBucket {
  constructor() {
    super();
    this.ops = [];
    this.recording = false;
  }
  #note(op, key, bytes = 0) {
    if (this.recording) this.ops.push({ op, key, bytes });
  }
  async put(key, value, options) {
    const result = await super.put(key, value, options);
    this.#note("put", key, typeof value === "string" ? value.length : 0);
    return result;
  }
  async get(key) {
    this.#note("get", key);
    return super.get(key);
  }
  async list(options) {
    this.#note("list", options?.prefix || "");
    return super.list(options);
  }
  async delete(key) {
    this.#note("delete", key);
    return super.delete(key);
  }
  countFor(key) {
    return this.ops.filter((op) => op.key === key).length;
  }
}

test("a scheduled dispatch touches the cron lock three times, not six", async () => {
  const env = { ...ENV, LLM_QUEUE: new RecordingBucket(), MAX_TOTAL_REQUESTS: "1" };
  await handleRequest(chatRequest(undefined, "lease-ops"), env);

  env.LLM_QUEUE.recording = true;
  const clock = Date.now();
  const result = await runScheduled(env, { fetchImpl: okUpstream(), nowMs: () => clock });
  assert.equal(result.status, "dispatched");

  // acquire (get + put) and release (a single CAS put on the ETag acquire returned).  The lease
  // was just taken for its full duration, so re-proving ownership before the first batch cannot
  // discover anything and is skipped.
  assert.deepEqual(
    env.LLM_QUEUE.ops.filter((op) => op.key === "locks/cron.json").map((op) => op.op),
    ["get", "put", "put"],
  );
});

test("the lease is still renewed once a run has consumed half of it", async () => {
  const env = {
    ...ENV,
    LLM_QUEUE: new RecordingBucket(),
    MAX_TOTAL_REQUESTS: "2",
    LEASE_DURATION_SECONDS: "840",
  };
  await handleRequest(chatRequest(undefined, "renew-a"), env);
  await handleRequest(chatRequest(undefined, "renew-b"), env);

  env.LLM_QUEUE.recording = true;
  const start = Date.now();
  // The run's clock jumps past half of the 840s lease, so ownership must be re-proved before the
  // Worker keeps dispatching against a lease another invocation could by then have taken.
  const ticks = [start, start, start, start];
  let index = 0;
  const result = await runScheduled(env, {
    fetchImpl: okUpstream(),
    nowMs: () => (index < ticks.length ? ticks[index++] : start + 500_000),
  });
  assert.equal(result.totalDispatched, 1);
  const lock = await env.LLM_QUEUE.get("locks/cron.json");
  assert.ok((await lock.json()).renewed_at, "a long-running pass must still renew the lease");
});

test("a stale owner's handle cannot release a lease another invocation now holds", async () => {
  const bucket = new FakeBucket();
  const now = new Date();
  const handleA = { etag: null, lease: null };
  assert.equal(await acquireCronLease(bucket, now, "invocation-a", 30, handleA), true);
  assert.ok(handleA.etag, "acquire must hand back the ETag it wrote");

  const later = new Date(now.getTime() + 31_000);
  assert.equal(await acquireCronLease(bucket, later, "invocation-b", 90), true);

  // A releases using its cached handle; the CAS must fail against B's newer lease.
  await releaseCronLease(bucket, "invocation-a", handleA);
  const stillThere = await bucket.get("locks/cron.json");
  assert.equal((await stillThere.json()).owner, "invocation-b");
  assert.equal(
    Date.parse((await stillThere.json()).expires_at) > Date.now(),
    true,
    "B's lease must remain live",
  );
});

test("renewing through a stale handle falls back to the authoritative read", async () => {
  const bucket = new FakeBucket();
  const now = new Date();
  const handle = { etag: null, lease: null };
  assert.equal(await acquireCronLease(bucket, now, "invocation-a", 600, handle), true);

  // Another writer bumps the object, invalidating the cached ETag but not the ownership.
  const current = await bucket.get("locks/cron.json");
  await bucket.put(
    "locks/cron.json",
    JSON.stringify({ ...(await current.json()), note: "touched" }),
    { onlyIf: { etagMatches: current.etag } },
  );

  const later = new Date(now.getTime() + 10_000);
  assert.equal(await renewCronLease(bucket, later, "invocation-a", 600, handle), true);
  assert.equal((await (await bucket.get("locks/cron.json")).json()).renewed_at, later.toISOString());

  // A handle whose owner no longer holds the lease must not renew it.
  const stolen = new Date(now.getTime() + 700_000);
  assert.equal(await acquireCronLease(bucket, stolen, "invocation-b", 600), true);
  assert.equal(await renewCronLease(bucket, stolen, "invocation-a", 600, handle), false);
});

test("a batch that dispatches nothing persists a reaped reservation but not a window roll", async () => {
  // The guard on this write used to compare `JSON.stringify(budget)` with a second stringify of
  // the same object, so it could never fire.  Making it fire correctly also means being precise
  // about what is worth a write: window keys are recomputed from `now` on every load, while an
  // abandoned reservation is durable state nothing else will clear.
  const route = "mistral_large_2512_primary";
  // Ahead of the enqueues below, so their markers are already eligible when the batch runs.
  const now = new Date(Date.now() + 1_000);

  const ledger = (inflight) => ({
    version: 1,
    routes: {
      [route]: {
        requests_minute: 0,
        tokens_minute: 0,
        requests_available_at: "",
        tokens_available_at: "",
        requests_minute_key: "1999-01-01T00:00",
        requests_day: 0,
        requests_day_key: "1999-01-01",
        blocked_until: new Date(now.getTime() + 3_600_000).toISOString(),
        inflight,
      },
    },
  });

  // A stale window key alone is not worth an R2 write.
  const rollOnly = { ...isolatedEnv(), MAX_TOTAL_REQUESTS: "1" };
  await handleRequest(chatRequest(undefined, "roll-only"), rollOnly);
  await rollOnly.LLM_QUEUE.put("state/dispatch_budget.json", JSON.stringify(ledger({})), {});
  const beforeEtag = (await rollOnly.LLM_QUEUE.get("state/dispatch_budget.json")).etag;
  assert.equal((await dispatchOne(rollOnly, okUpstream(), now)).status, "no_capacity");
  assert.equal(
    (await rollOnly.LLM_QUEUE.get("state/dispatch_budget.json")).etag,
    beforeEtag,
    "recomputable window state must not cost an R2 write on every idle minute",
  );

  // An expired reservation is durable state, so it must be written back.
  const reapEnv = { ...isolatedEnv(), MAX_TOTAL_REQUESTS: "1" };
  await handleRequest(chatRequest(undefined, "reap"), reapEnv);
  await reapEnv.LLM_QUEUE.put(
    "state/dispatch_budget.json",
    JSON.stringify(
      ledger({
        "chatcmpl-abandoned": {
          requests: 1,
          tokens: 1024,
          expires_at: new Date(now.getTime() - 60_000).toISOString(),
        },
      }),
    ),
    {},
  );
  assert.equal((await dispatchOne(reapEnv, okUpstream(), now)).status, "no_capacity");
  const stored = await (await reapEnv.LLM_QUEUE.get("state/dispatch_budget.json")).json();
  assert.deepEqual(
    stored.routes[route].inflight,
    {},
    "an abandoned reservation must be cleared from durable storage",
  );
});

test("a ready marker with an unreadable policy is repaired out of the index", async () => {
  const env = isolatedEnv();
  await handleRequest(chatRequest(undefined, "bad-policy"), env);
  const [markerKey] = [...env.LLM_QUEUE.objects.keys()].filter((key) => key.startsWith("ready/"));
  const marker = env.LLM_QUEUE.objects.get(markerKey);
  marker.customMetadata = { ...marker.customMetadata, policy: "{not json" };

  const result = await dispatchOne(env, okUpstream(), new Date());
  assert.equal(result.status, "index_repaired");
  assert.equal(env.LLM_QUEUE.objects.has(markerKey), false);
});

test("repeated zoned-date work reuses one Intl formatter per timezone", async () => {
  // Constructing an Intl.DateTimeFormat costs ~20x what reusing one does, and route selection
  // computes a zoned day key for every rate-limited route it considers.
  const RealDateTimeFormat = Intl.DateTimeFormat;
  let constructions = 0;
  class CountingDateTimeFormat extends RealDateTimeFormat {
    constructor(...args) {
      super(...args);
      constructions += 1;
    }
  }
  // nextRouteReset only reaches the zoned-midnight path (and therefore a formatter) when the
  // route's daily quota is actually exhausted. An earlier version of this test used
  // requests_day: 0 against rpd: 500, so it never constructed a formatter whether the cache
  // worked or not -- it asserted 0 against a path it never took.
  const route = { rpd: 500, provider: "gemini", reset_timezone: "America/Los_Angeles" };
  const exhausted = () => ({ requests_day: 500, requests_day_key: "", inflight: {} });

  // Warm the module-level cache first so this proves reuse rather than depending on whichever
  // earlier test happened to populate it.
  nextRouteReset(exhausted(), route, new Date());

  Intl.DateTimeFormat = CountingDateTimeFormat;
  try {
    for (let index = 0; index < 200; index += 1) {
      nextRouteReset(exhausted(), route, new Date(Date.now() + index * 1000));
    }
  } finally {
    Intl.DateTimeFormat = RealDateTimeFormat;
  }
  assert.equal(constructions, 0, "the timezone formatter must already be cached module-wide");
});

test("a serial dispatch reads the rate ledger once and writes it once", async () => {
  const env = { ...ENV, LLM_QUEUE: new RecordingBucket(), MAX_TOTAL_REQUESTS: "1" };
  await handleRequest(chatRequest(undefined, "ledger-ops"), env);

  env.LLM_QUEUE.recording = true;
  const clock = Date.now();
  const result = await runScheduled(env, { fetchImpl: okUpstream(), nowMs: () => clock });
  assert.equal(result.status, "dispatched");

  // At the 1/1 configuration the cron lease is the serialization guarantee, so durable usage is
  // committed before the upstream call and no reservation is created to release afterwards.
  assert.deepEqual(
    env.LLM_QUEUE.ops
      .filter((op) => op.key === "state/dispatch_budget.json")
      .map((op) => op.op),
    ["get", "put"],
  );
});

test("a released reservation is really gone from the persisted ledger", async () => {
  const env = { ...isolatedEnv(), MAX_TOTAL_REQUESTS: "1" };
  const queued = await handleRequest(chatRequest(undefined, "release-durable"), env);
  const { id } = await queued.json();

  const result = await runScheduled(env, { fetchImpl: okUpstream(), nowMs: () => Date.now() });
  assert.equal(result.status, "dispatched");

  const budget = await (await env.LLM_QUEUE.get("state/dispatch_budget.json")).json();
  const entry = budget.routes.mistral_large_2512_primary;
  assert.deepEqual(entry.inflight, {}, "the reservation must not survive the run");
  assert.equal(entry.requests_minute, 1, "durable rate usage must survive the release");
  assert.ok(entry.requests_available_at, "request pacing must survive the release");
  assert.equal((await (await env.LLM_QUEUE.get(`requests/${id}.json`)).json()).status, "completed");
});

test("a route's concurrency ceiling still holds without a durable reservation", async () => {
  // The in-memory `batchState.admitted` counter replaced the durable `inflight` reservation, so
  // this is the only thing standing between a batch and over-dispatching a concurrency-capped
  // route. Testing it requires a route where the ceiling is actually the binding limit:
  //
  //   * an earlier revision named two route IDs that do not exist, so it starved nothing; and
  //   * its replacement used a route with `rpm: 10`, where RPM pacing already caps the route at
  //     one request per batch (reserveRouteCapacity pushes requests_available_at a full interval
  //     ahead on the first reservation), so the assertion held whether or not the ceiling worked.
  //
  // deepseek_v4_flash_primary has `rpm: null` and `concurrency: 5`, so the ceiling is the only
  // limit in play and a broken counter is observable.
  const ceilingRoute = "deepseek_v4_flash_primary";
  const model = "deepseek/deepseek-v4-flash";
  const catalogRoutes = Object.keys(routeIdsForModel(model));
  assert.ok(catalogRoutes.includes(ceilingRoute), "the route under test must exist in the catalog");

  const env = { ...ENV, LLM_QUEUE: new FakeBucket() };
  // deepseek_v4_flash_primary is a paid route, and selectRouteForModel only elevates past free
  // routes when waiting for every free route would miss the caller's deadline. The free
  // alternative is starved for an hour below, so a near-term deadline is what makes the paid
  // route reachable at all.
  const policy = {
    allow_paid: true,
    deadline_at: new Date(Date.now() + 60_000).toISOString(),
  };
  for (let index = 0; index < 8; index += 1) {
    await handleRequest(chatRequest(undefined, `ceiling-${index}`, model, policy), env);
  }

  // Starve every real alternative for this model, resolved from the compiled catalog rather than
  // hard-coded, so the ceiling route is the only one selection can pick.
  const blocked = new Date(Date.now() + 3_600_000).toISOString();
  const alternatives = catalogRoutes.filter((id) => id !== ceilingRoute);
  assert.ok(alternatives.length > 0, "the fixture must starve real catalog routes");
  const budget = { version: 1, routes: {}, providers: {} };
  for (const id of alternatives) {
    budget.routes[id] = { requests_minute: 0, inflight: {}, blocked_until: blocked };
  }
  await env.LLM_QUEUE.put("state/dispatch_budget.json", JSON.stringify(budget), {});

  const result = await dispatchBatch(
    env,
    async () => new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 }),
    new Date(),
    8,
  );

  // Exactly the ceiling: eight requests were queued against a concurrency-5 route with no other
  // limit, so a working counter admits precisely five. Asserting `<= 5` would also pass if the
  // route were never selected, which is how the earlier revisions hid their bugs.
  const onCeilingRoute = (result.results || []).filter((r) => r.routeId === ceilingRoute);
  assert.equal(
    onCeilingRoute.length,
    5,
    `concurrency-5 route admitted ${onCeilingRoute.length} candidates in one batch`,
  );
});

test("a concurrency ceiling still counts reservations left by a crashed predecessor", async () => {
  const route = "openrouter_google_gemma_4_31b_it_free";
  const entry = {
    requests_minute: 0,
    inflight: {
      // Not yet expired: an older Worker version, or a crashed invocation, still holds this slot.
      "chatcmpl-crashed": {
        requests: 1,
        expires_at: new Date(Date.now() + 300_000).toISOString(),
      },
    },
  };
  assert.equal(
    routeAvailable(entry, { route_id: route, concurrency: 1 }, { requests: 1, tokens: 0 }, new Date()),
    false,
    "a live inflight entry from a previous invocation must still block admission",
  );
});

test("a batch removes every finished marker in one R2 delete", async () => {
  class DeleteCountingBucket extends FakeBucket {
    constructor() {
      super();
      this.deleteCalls = 0;
      this.deletedKeys = 0;
    }
    async delete(key) {
      this.deleteCalls += 1;
      this.deletedKeys += Array.isArray(key) ? key.length : 1;
      return super.delete(key);
    }
  }
  const bucket = new DeleteCountingBucket();
  const env = { ...ENV, LLM_QUEUE: bucket };
  const models = ["mistral/mistral-large-2512", "gemini/gemini-3-flash-preview"];
  for (const [index, model] of models.entries()) {
    await handleRequest(chatRequest(undefined, `marker-batch-${index}`, model), env);
  }
  bucket.deleteCalls = 0;
  bucket.deletedKeys = 0;

  const result = await dispatchBatch(
    env,
    async () => new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 }),
    new Date(),
    2,
  );
  assert.equal(result.completedCount, 2);
  assert.equal(bucket.deletedKeys, 2, "both markers must be removed");
  assert.equal(bucket.deleteCalls, 1, "in a single R2 delete call");
  assert.equal(
    [...bucket.objects.keys()].filter((k) => k.startsWith("ready/")).length,
    0,
    "no ready marker may survive a completed batch",
  );
});

test("heads waiting on short route pacing are skipped without touching R2", async () => {
  // The scenario that matters for throughput: a low-rate route is cooling down at the head of the
  // queue while higher-rate work sits behind it. Rewriting each blocked head cost four R2
  // operations -- more than dispatching a request -- so a short wait must cost nothing.
  class CountingBucket extends FakeBucket {
    constructor() {
      super();
      this.ops = 0;
      this.counting = false;
    }
    async put(k, v, o) { if (this.counting) this.ops += 1; return super.put(k, v, o); }
    async get(k) { if (this.counting) this.ops += 1; return super.get(k); }
    async head(k) { if (this.counting) this.ops += 1; return super.head(k); }
    async list(o) { if (this.counting) this.ops += 1; return super.list(o); }
    async delete(k) { if (this.counting) this.ops += 1; return super.delete(k); }
  }

  const run = async (blockedCount) => {
    const bucket = new CountingBucket();
    const env = { ...ENV, LLM_QUEUE: bucket, BATCH_CONCURRENCY: "2", MAX_TOTAL_REQUESTS: "2" };
    for (let i = 0; i < blockedCount; i += 1) {
      await handleRequest(chatRequest(undefined, `slow-${i}`, "gemini/gemini-3.5-flash-lite"), env);
    }
    await handleRequest(chatRequest(undefined, "fast-0", "mistral/mistral-large-2512"), env);

    // Every route for the slow model is pacing, and becomes eligible again in three minutes.
    const soon = new Date(Date.now() + 180_000).toISOString();
    const routes = {};
    for (const id of Object.keys(routeIdsForModel("gemini/gemini-3.5-flash-lite"))) {
      routes[id] = { requests_minute: 0, inflight: {}, blocked_until: soon };
    }
    await bucket.put("state/dispatch_budget.json", JSON.stringify({ version: 1, routes }), {});

    bucket.counting = true;
    await dispatchBatch(
      env,
      async () => new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 }),
      new Date(),
      2,
    );
    return bucket.ops;
  };

  const none = await run(0);
  const many = await run(8);
  assert.equal(
    many,
    none,
    `eight heads waiting on short pacing cost ${many - none} extra R2 operations`,
  );
});

test("structured-output schema stripping works against the real compiled catalog, not just a fixture route object", async () => {
  // Historical note: the catalog previously stored each route as a fixed-position array
  // (COMPACT_ROUTE_FIELDS/routeFromCatalog), and structured_output_schema_strip_keys was read by
  // upstreamRequestForRoute without ever being added to that field list -- silently undefined, no
  // error, on every configured route -- while a hand-built full route object (see the "relaxed
  // structured-output profile" test above) still passed. The catalog is a named object keyed by
  // route ID now, which can't misplace a field this way, but this test still earns its keep: it
  // exercises the real compiled dispatch_limits.json end to end rather than a hand-built fixture,
  // so a future field-name mismatch between the compiler and the Worker still gets caught here.
  const env = isolatedEnv();
  const responseFormat = {
    type: "json_schema",
    json_schema: { name: "Response", schema: { type: "object", properties: {
      answer: { type: "string", minLength: 1, maxLength: 100 },
    } } },
  };
  await handleRequest(
    chatRequest(
      [{ role: "user", content: "structured output" }],
      "catalog-strip-keys",
      "gemini/gemini-3.5-flash-lite",
      { response_format: responseFormat },
    ),
    env,
  );
  const calls = [];
  const result = await dispatchOne(env, async (_url, init) => {
    calls.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ id: "ok", choices: [] }), { status: 200 });
  }, new Date());
  assert.equal(result.status, "completed");
  const sentSchemaText = JSON.stringify(calls[0].response_format.json_schema.schema);
  assert.equal(sentSchemaText.includes("minLength"), false);
});
