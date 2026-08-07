import assert from "node:assert/strict";
import test from "node:test";

import {
  acquireCronLease,
  config,
  dispatchOne,
  handleRequest,
  nextLocalMidnightUTC,
  nextRouteReset,
  rankRoutes,
  releaseCronLease,
  resolveProviderCredentials,
  routeAvailable,
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

function chatRequest(body, idempotencyKey, model) {
  const headers = idempotencyKey ? { "idempotency-key": idempotencyKey } : {};
  return request("https://dispatch.example/v1/chat/completions", {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: model || "mistral/mistral-large-2512",
      messages: body || [{ role: "user", content: "Summarize this meeting." }],
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

test("per-route ledger exhaustion rotates onto a second account instead of blocking the model", async () => {
  // Mistral has one account (rpm=1) -- a second immediate request stays pending, never rotates
  // (there's nothing to rotate onto). Gemini has two accounts (project_primary/project_secondary);
  // once primary's rpm=10 window is spent, a further Gemini request must resolve to the secondary
  // account's own key -- this is what "multi-account key rotation" actually requires (review/41).
  const env = isolatedEnv();
  const calls = [];
  const upstream = async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ id: `c-${calls.length}`, choices: [] }), { status: 200 });
  };

  for (let i = 0; i < 10; i += 1) {
    await handleRequest(
      chatRequest(undefined, `gemini-burst-${i}`, "gemini/gemini-3-flash-preview"),
      env,
    );
    // Fresh `new Date()` each call, same as the enqueue side -- a shared, stale timestamp can
    // drift behind real wall-clock time across ten awaited round-trips and make a freshly
    // enqueued record's `available_at` look like it's still in the future.
    const result = await dispatchOne(env, upstream, new Date());
    assert.equal(result.status, "completed");
  }
  assert.equal(calls.length, 10);
  assert.ok(calls.every((call) => call.init.headers.authorization === "Bearer gemini-primary-secret"));

  // The 11th request within the same minute window must roll onto the secondary account.
  await handleRequest(chatRequest(undefined, "gemini-burst-11", "gemini/gemini-3-flash-preview"), env);
  const eleventh = await dispatchOne(env, upstream, new Date());
  assert.equal(eleventh.status, "completed");
  assert.equal(calls[10].init.headers.authorization, "Bearer gemini-secondary-secret");
});

test("a route with no capacity anywhere is requeued (no_capacity), not permanently failed", async () => {
  const env = isolatedEnv();
  await handleRequest(chatRequest(undefined, "first"), env);
  await handleRequest(chatRequest(undefined, "second"), env);
  const now = new Date();

  const first = await dispatchOne(env, okUpstream(), now);
  assert.equal(first.status, "completed");
  const exhausted = await dispatchOne(env, okUpstream(), now); // Mistral rpm=1, same minute
  assert.equal(exhausted.status, "no_capacity");
  const stored = await env.LLM_QUEUE.get(`requests/${exhausted.requestId}.json`);
  const record = await stored.json();
  assert.equal(record.status, "pending");
  assert.equal(record.attempts, 0); // not counted as a real attempt

  // The next minute's window frees Mistral's single slot back up.
  const nextMinute = new Date(now.getTime() + 61_000);
  const recovered = await dispatchOne(env, okUpstream(), nextMinute);
  assert.equal(recovered.status, "completed");
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
  assert.equal(await bucket.get("locks/cron.json"), null);
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
    routeAvailable({ requests_minute: 0, requests_day: 0, tokens_minute: 95 }, route, { requests: 1, tokens: 10 }, now),
    false,
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
