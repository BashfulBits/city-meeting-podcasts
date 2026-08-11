import assert from "node:assert/strict";
import test from "node:test";

import {
  acquireCronLease,
  config,
  dispatchBatch,
  dispatchOne,
  handleRequest,
  nextLocalMidnightUTC,
  nextRouteReset,
  rankRoutes,
  releaseCronLease,
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
    this.objects.delete(key);
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
  assert.equal(env.LLM_QUEUE.objects.size, 1);

  const conflict = await handleRequest(
    chatRequest([{ role: "user", content: "different" }], "meeting-1"),
    env,
  );
  assert.equal(conflict.status, 409);
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
  assert.equal(record.attempts, 0); // not counted as a real attempt

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

test("a paid-only model with allow_paid=false fails permanently instead of dispatching anyway", async () => {
  // CodeRabbit index.js:541 -- the exact bug: every route for deepseek-v4-flash is paid, so a
  // caller that disallowed paid must get a terminal failure, not a silent fallback dispatch.
  const env = isolatedEnv();
  const queued = await handleRequest(
    chatRequest(undefined, "paid-disallowed", "deepseek/deepseek-v4-flash"),
    env,
  );
  const body = await queued.json();

  const posted = { count: 0 };
  const upstream = async () => {
    posted.count += 1;
    return new Response(JSON.stringify({ id: "should-not-happen", choices: [] }), { status: 200 });
  };

  const result = await dispatchOne(env, upstream, new Date());
  assert.equal(result.status, "failed");
  assert.equal(posted.count, 0);
  const stored = await env.LLM_QUEUE.get(`requests/${body.id}.json`);
  const record = await stored.json();
  assert.equal(record.status, "failed");
  assert.equal(record.error.code, "no_eligible_route");
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
  await env.LLM_QUEUE.put("requests/unrouted.json", JSON.stringify(record));

  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "failed");
  assert.equal(result.reason, "no_configured_route");
});

test("queue scan limit counts every scanned object, including terminal records", async () => {
  // CodeRabbit -- `scanned` was never incremented, so `maxQueueScan` didn't bound anything.
  const env = isolatedEnv();
  env.MAX_QUEUE_SCAN = "1";
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
  const pending = {
    schema: 1,
    id: "chatcmpl-ready",
    status: "pending",
    provider: "mistral",
    model: "mistral/mistral-large-2512",
    request: { model: "mistral/mistral-large-2512", messages: [{ role: "user", content: "ready" }], stream: false },
    created_at: timestamp,
    updated_at: timestamp,
    available_at: timestamp,
    attempts: 0,
    policy: { estimated_tokens: 100 },
  };
  await env.LLM_QUEUE.put("requests/000-terminal.json", JSON.stringify(terminal));
  await env.LLM_QUEUE.put("requests/999-ready.json", JSON.stringify(pending));

  // Scanning is capped at 1 object, and the terminal record sorts first -- so the ready record is
  // never even reached this tick.
  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "idle");
});

test("a terminal record's customMetadata lets the scan skip it without ever fetching its body", async () => {
  // CodeRabbit's perf finding: both queue scans used to read every listed object's full body just
  // to check `status`. `putJson` now mirrors status/model/available_at into R2 customMetadata
  // (returned by `bucket.list()` already), so a completed/failed record must be skippable from the
  // listing alone. Proven here by making `.get()` throw for the terminal key -- the scan must
  // never call it.
  class NoBodyFetchForTerminalBucket extends FakeBucket {
    async get(key) {
      if (key === "requests/000-terminal.json") {
        throw new Error("must not fetch a terminal record's body from customMetadata alone");
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
  await env.LLM_QUEUE.put("requests/000-terminal.json", JSON.stringify(terminal), {
    customMetadata: { status: "completed", model: terminal.model, available_at: timestamp },
  });

  const result = await dispatchOne(env, okUpstream(), now);
  assert.equal(result.status, "completed");
  assert.equal(result.requestId, queuedBody.id);
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

  const firstAt = new Date();
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

test("GET /v1/queue/estimate reports backlog_count for pending records of the requested model only", async () => {
  const env = isolatedEnv();
  const now = new Date();
  const timestamp = now.toISOString();
  const record = (id, model, status) => ({
    schema: 1,
    id,
    status,
    provider: model.split("/")[0],
    model,
    request: { model, messages: [{ role: "user", content: "x" }], stream: false },
    created_at: timestamp,
    updated_at: timestamp,
    available_at: timestamp,
    attempts: status === "completed" ? 1 : 0,
    ...(status === "completed" ? { response: { id: "done", choices: [] } } : {}),
  });
  // Two pending + one already-completed Mistral record, plus one pending Gemini record --
  // backlog_count for Mistral must be 2 (pending only), not 3 (all records) or 1 (undercounting).
  await env.LLM_QUEUE.put(
    "requests/mistral-pending-1.json",
    JSON.stringify(record("chatcmpl-m1", "mistral/mistral-large-2512", "pending")),
  );
  await env.LLM_QUEUE.put(
    "requests/mistral-pending-2.json",
    JSON.stringify(record("chatcmpl-m2", "mistral/mistral-large-2512", "pending")),
  );
  await env.LLM_QUEUE.put(
    "requests/mistral-done.json",
    JSON.stringify(record("chatcmpl-m3", "mistral/mistral-large-2512", "completed")),
  );
  await env.LLM_QUEUE.put(
    "requests/gemini-pending.json",
    JSON.stringify(record("chatcmpl-g1", "gemini/gemini-3-flash-preview", "pending")),
  );

  const response = await handleRequest(
    request("https://dispatch.example/v1/queue/estimate?model=mistral%2Fmistral-large-2512", {
      method: "GET",
    }),
    env,
  );
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.model, "mistral/mistral-large-2512");
  assert.equal(body.backlog_count, 2);
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
  entry.inflight = Object.fromEntries(
    Array.from({ length: 5 }, (_, index) => [`existing-${index}`, { requests: 1, tokens: 10 }]),
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

test("a batch retains sibling success when one task rejects during finalization", async () => {
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
  assert.deepEqual(
    result.results.map((item) => item.status).sort(),
    ["completed", "failed"],
  );
  const failed = await bucket.get(bucket.failKey);
  assert.equal((await failed.json()).status, "failed");
  const ledger = await bucket.get("state/dispatch_budget.json");
  for (const entry of Object.values((await ledger.json()).routes || {})) {
    assert.deepEqual(entry.inflight || {}, {});
  }
});

test("a fresh invocation gives one long request a batch slot despite a fast backlog", async () => {
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
  assert.ok(
    completed.some(
      ({ record }) => record.policy?.timeout_class === "long" && record.status === "completed",
    ),
  );
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
