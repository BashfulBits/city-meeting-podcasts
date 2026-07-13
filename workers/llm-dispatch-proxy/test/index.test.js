import assert from "node:assert/strict";
import test from "node:test";

import { dispatchOne, handleRequest } from "../src/index.js";

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
}

const ENV = {
  DISPATCH_AUTH_TOKEN: "dispatch-secret",
  PROVIDER_NAME: "mistral",
  UPSTREAM_BASE_URL: "https://api.mistral.example",
  UPSTREAM_CHAT_PATH: "/v1/chat/completions",
  UPSTREAM_MODEL: "mistral-large-3",
  UPSTREAM_API_KEY: "upstream-secret",
  DISPATCH_INTERVAL_SECONDS: "60",
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

function chatRequest(body, idempotencyKey) {
  const headers = idempotencyKey ? { "idempotency-key": idempotencyKey } : {};
  return request("https://dispatch.example/v1/chat/completions", {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: "mistral/mistral-large-3",
      messages: body || [{ role: "user", content: "Summarize this meeting." }],
    }),
  });
}

function isolatedEnv() {
  return { ...ENV, LLM_QUEUE: new FakeBucket() };
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

test("queues an OpenAI-shaped request and reuses an idempotency key", async () => {
  const env = isolatedEnv();
  const first = await handleRequest(chatRequest(undefined, "meeting-1"), env);
  assert.equal(first.status, 202);
  const firstBody = await first.json();
  assert.match(firstBody.id, /^chatcmpl-[a-f0-9]{32}$/);
  assert.equal(first.headers.get("location"), `https://dispatch.example/v1/requests/${firstBody.id}`);

  const repeated = await handleRequest(chatRequest(undefined, "meeting-1"), env);
  assert.equal(repeated.status, 202);
  assert.equal((await repeated.json()).id, firstBody.id);
  assert.equal(env.LLM_QUEUE.objects.size, 1);

  const conflict = await handleRequest(
    chatRequest([{ role: "user", content: "different" }], "meeting-1"),
    env,
  );
  assert.equal(conflict.status, 409);
});

test("accepts the configured provider route but rejects a different provider", async () => {
  const env = isolatedEnv();
  const accepted = await handleRequest(
    chatRequest(undefined, "provider-qualified"),
    env,
  );
  assert.equal(accepted.status, 202);

  const rejected = await handleRequest(
    request("https://dispatch.example/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify({
        model: "gemini/gemini-3-flash",
        messages: [{ role: "user", content: "wrong route" }],
      }),
    }),
    env,
  );
  assert.equal(rejected.status, 400);
});

test("Cron claims one request, forwards only the normalized upstream payload, and stores the result", async () => {
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

  const result = await dispatchOne(env, upstream, new Date(Date.now() + 61_000));
  assert.equal(result.status, "completed");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.mistral.example/v1/chat/completions");
  assert.equal(calls[0].init.headers.authorization, "Bearer upstream-secret");
  assert.equal(calls[0].body.model, "mistral-large-3");
  assert.equal(calls[0].body.stream, false);

  const poll = await handleRequest(
    request(`https://dispatch.example/v1/requests/${queuedBody.id}`, { method: "GET" }),
    env,
  );
  assert.equal(poll.status, 200);
  assert.equal((await poll.json()).choices[0].message.content, "Done.");
});

test("can preserve a provider-qualified model when the upstream is a LiteLLM Proxy", async () => {
  const env = {
    ...isolatedEnv(),
    UPSTREAM_BASE_URL: "https://litellm.example",
    MODEL_ID: "mistral/mistral-large-3",
    UPSTREAM_REQUEST_MODEL: "mistral/mistral-large-3",
  };
  await handleRequest(chatRequest(undefined, "litellm-route"), env);
  const calls = [];
  const upstream = async (_url, init) => {
    calls.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ id: "proxy-completion", choices: [] }), { status: 200 });
  };

  assert.equal((await dispatchOne(env, upstream, new Date(Date.now() + 61_000))).status, "completed");
  assert.equal(calls[0].model, "mistral/mistral-large-3");
});

test("conditional dispatch pacing prevents a second upstream call before the interval", async () => {
  const env = isolatedEnv();
  await handleRequest(chatRequest(undefined, "first"), env);
  await handleRequest(chatRequest(undefined, "second"), env);
  let calls = 0;
  const upstream = async () => {
    calls += 1;
    return new Response(JSON.stringify({ id: `completion-${calls}`, choices: [] }), { status: 200 });
  };

  const firstAt = new Date(Date.now() + 61_000);
  assert.equal((await dispatchOne(env, upstream, firstAt)).status, "completed");
  assert.equal((await dispatchOne(env, upstream, new Date(firstAt.getTime() + 1_000))).status, "rate_limited");
  assert.equal(calls, 1);
});

test("429 responses retry with bounded backoff and do not expose the provider body", async () => {
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

  const firstAt = new Date(Date.now() + 61_000);
  assert.equal((await dispatchOne(env, upstream, firstAt)).status, "retrying");
  const stored = await env.LLM_QUEUE.get(`requests/${queuedBody.id}.json`);
  const retryRecord = await stored.json();
  assert.equal(retryRecord.status, "pending");
  assert.equal(retryRecord.last_error.status, 429);
  assert.equal(retryRecord.error, undefined);
  assert.equal((await dispatchOne(env, upstream, new Date(firstAt.getTime() + 61_000))).status, "idle");
  assert.equal((await dispatchOne(env, upstream, new Date(firstAt.getTime() + 121_000))).status, "completed");
  assert.equal(calls, 2);
});

test("streaming requests and oversized request bodies are rejected", async () => {
  const env = isolatedEnv();
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
