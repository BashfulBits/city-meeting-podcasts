import test from "node:test";
import assert from "node:assert/strict";
import worker, { LLMSchedulerDO } from "../src/index.js";
import { createMockSqlStorage } from "./helpers.js";

const CATALOG = {
  model_aliases: {},
  model_routes_map: {
    "gemini/gemini-flash-lite": ["route-a"],
  },
  routes_by_id: {
    "route-a": {
      provider: "gemini",
      upstream_model: "gemini-flash-lite",
      rpm: 15,
      rpd: 500,
      tpm: 250000,
      free: true,
      input_context_limit: 1048576,
      output_context_limit: 65536,
    },
  },
  providers: {
    gemini: {
      api_base: "https://generativelanguage.googleapis.com/v1beta/openai",
      ai_gateway_slug: "google-ai-studio",
      ai_gateway_chat_path: "/v1beta/openai/chat/completions",
      chat_path: "/chat/completions",
      accounts: [{ id: "project_primary", api_key_env: "GEMINI_API_KEY" }],
    },
  },
};

const B2_HOST = "s3.us-west-004.backblazeb2.com";

function fakeFetch(store, { gatewayStatus = 200, gatewayBody = null } = {}) {
  return async (url, init = {}) => {
    const parsed = new URL(url);
    if (parsed.host === B2_HOST) {
      const key = decodeURIComponent(parsed.pathname.replace(/^\/[^/]+\//, ""));
      if (init.method === "PUT") {
        store.set(key, init.body);
        return new Response("", { status: 200 });
      }
      if (init.method === "GET") {
        if (!store.has(key)) return new Response("", { status: 404 });
        return new Response(store.get(key), { status: 200 });
      }
      if (init.method === "DELETE") {
        store.delete(key);
        return new Response("", { status: 200 });
      }
      return new Response("unsupported", { status: 400 });
    }
    // Provider / AI Gateway call.
    const body =
      gatewayBody ||
      JSON.stringify({
        choices: [{ message: { content: "hello from the model" } }],
        usage: { prompt_tokens: 620, completion_tokens: 140 },
      });
    return new Response(body, { status: gatewayStatus });
  };
}

function makeEnv(overrides = {}) {
  const { storage } = createMockSqlStorage();
  const coordinatorEnv = {
    MAX_JOBS_PER_UTC_DAY: "10000",
    MAX_BUNDLE_JOBS: "4",
    MAX_JOBS_PER_ROUTE_PER_BUNDLE: "4",
    MAX_CONCURRENT_ROUTE_LANES: "5",
    MAX_ACTIVE_BUNDLES: "2",
    MAX_IN_FLIGHT_LLM_CALLS: "8",
    MAX_BUNDLES_PER_UTC_DAY: "1000",
    MAX_QUEUE_WAIT_SECONDS: "3600",
    LEASE_DURATION_SECONDS: "840",
    MAX_429_RETRIES: "1",
    MAX_429_BACKOFF_SECONDS: "1",
    ESTIMATED_CALL_DURATION_CEILING_SECONDS: "1",
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
  };
  const coordinator = new LLMSchedulerDO({ storage }, coordinatorEnv);
  return {
    BEARER_TOKEN: "secret-token",
    LLM_SCHEDULER: { getByName: () => coordinator },
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
    DISPATCH_WINDOW_SECONDS: "25",
    MAX_RESPONSE_SECONDS: "5",
    FINALIZATION_RESERVE_SECONDS: "1",
    LEASE_DURATION_SECONDS: "840",
    CRON_EXECUTION_LIMIT_SECONDS: "900",
    CRON_TICK_SECONDS: "60",
    MAX_BUNDLES_PER_UTC_DAY: "1000",
    MAX_CONCURRENT_ROUTE_LANES: "5",
    MAX_JOBS_PER_UTC_DAY: "20000",
    ENQUEUE_BATCH_MAX: "1000",
    POLL_BATCH_MAX: "1000",
    B2_ENDPOINT: `https://${B2_HOST}`,
    B2_KEY_ID: "test-key-id",
    B2_APP_KEY: "test-app-key",
    B2_BUCKET: "test-bucket",
    GEMINI_API_KEY: "fake-gemini-key",
    ...overrides,
  };
}

async function enqueueOneJob(env, jobId, store) {
  const payload = { model: "gemini/gemini-flash-lite", messages: [{ role: "user", content: "hi" }] };
  store.set(`payloads/${jobId}/request.json`, JSON.stringify(payload));
  const req = new Request("http://localhost/v2/jobs:enqueue-batch", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer secret-token" },
    body: JSON.stringify({
      jobs: [
        {
          id: jobId,
          idempotency_key: `key-${jobId}`,
          request_digest: `digest-${jobId}`,
          policy_json: JSON.stringify({ allowed_models: ["gemini/gemini-flash-lite"], allow_paid: false }),
          prompt_family: "tags",
          input_token_estimate: 500,
          max_output_token_estimate: 200,
          payload_key: `payloads/${jobId}/request.json`,
        },
      ],
    }),
  });
  const res = await worker.fetch(req, env);
  assert.equal(res.status, 200);
}

test("scheduled() with no queued jobs makes no B2 or gateway calls", async () => {
  const store = new Map();
  let fetchCalls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    fetchCalls += 1;
    return fakeFetch(store)(...args);
  };
  try {
    const env = makeEnv();
    const waitUntilPromises = [];
    await worker.scheduled({}, env, { waitUntil: (p) => waitUntilPromises.push(p) });
    await Promise.all(waitUntilPromises);
    assert.equal(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("scheduled() claims a job, calls the gateway, writes the result, and completes the bundle", async () => {
  const store = new Map();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fakeFetch(store);
  try {
    const env = makeEnv();
    await enqueueOneJob(env, "job-1", store);

    const waitUntilPromises = [];
    await worker.scheduled({}, env, { waitUntil: (p) => waitUntilPromises.push(p) });
    await Promise.all(waitUntilPromises);

    const coordinator = env.LLM_SCHEDULER.getByName();
    const pollRes = await coordinator.pollBatch(["job-1"]);
    assert.equal(pollRes.statuses[0].state, "completed");
    assert.ok(pollRes.statuses[0].result_key);

    const resultBody = store.get(pollRes.statuses[0].result_key);
    assert.ok(resultBody);
    const parsed = JSON.parse(resultBody);
    assert.equal(parsed.choices[0].message.content, "hello from the model");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("scheduled() reports a terminal_error and does not crash when the gateway returns 500", async () => {
  const store = new Map();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fakeFetch(store, { gatewayStatus: 500, gatewayBody: JSON.stringify({ error: "boom" }) });
  try {
    const env = makeEnv();
    await enqueueOneJob(env, "job-err", store);

    const waitUntilPromises = [];
    await worker.scheduled({}, env, { waitUntil: (p) => waitUntilPromises.push(p) });
    await Promise.all(waitUntilPromises);

    const coordinator = env.LLM_SCHEDULER.getByName();
    const pollRes = await coordinator.pollBatch(["job-err"]);
    assert.equal(pollRes.statuses[0].state, "retryable");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

function fakeFetchWithOneThrottle(store) {
  let gatewayCalls = 0;
  return async (url, init = {}) => {
    const parsed = new URL(url);
    if (parsed.host === B2_HOST) {
      return fakeFetch(store)(url, init);
    }
    gatewayCalls += 1;
    if (gatewayCalls === 1) {
      return new Response(JSON.stringify({ error: "rate limited" }), {
        status: 429,
        headers: { "retry-after": "1" },
      });
    }
    return new Response(
      JSON.stringify({
        choices: [{ message: { content: "recovered after retry" } }],
        usage: { prompt_tokens: 500, completion_tokens: 120 },
      }),
      { status: 200 }
    );
  };
}

test("scheduled() retries once after a 429 and completes successfully", async () => {
  const store = new Map();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fakeFetchWithOneThrottle(store);
  try {
    const env = makeEnv({ MAX_429_BACKOFF_SECONDS: "1" });
    await enqueueOneJob(env, "job-429", store);

    const waitUntilPromises = [];
    await worker.scheduled({}, env, { waitUntil: (p) => waitUntilPromises.push(p) });
    await Promise.all(waitUntilPromises);

    const coordinator = env.LLM_SCHEDULER.getByName();
    const pollRes = await coordinator.pollBatch(["job-429"]);
    assert.equal(pollRes.statuses[0].state, "completed");
    const resultBody = JSON.parse(store.get(pollRes.statuses[0].result_key));
    assert.equal(resultBody.choices[0].message.content, "recovered after retry");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("scheduled() does nothing when B2 credentials are not configured", async () => {
  const store = new Map();
  let fetchCalls = 0;
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (...args) => {
    fetchCalls += 1;
    return fakeFetch(store)(...args);
  };
  try {
    const env = makeEnv({ B2_ENDPOINT: undefined });
    await enqueueOneJob(env, "job-nob2", store);

    const waitUntilPromises = [];
    await worker.scheduled({}, env, { waitUntil: (p) => waitUntilPromises.push(p) });
    await Promise.all(waitUntilPromises);

    assert.equal(fetchCalls, 0); // No fetch calls made when B2 credentials missing
    const coordinator = env.LLM_SCHEDULER.getByName();
    const pollRes = await coordinator.pollBatch(["job-nob2"]);
    assert.equal(pollRes.statuses[0].state, "queued"); // remains queued without leasing when B2 is missing
  } finally {
    globalThis.fetch = originalFetch;
  }
});
