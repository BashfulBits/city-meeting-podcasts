import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import worker, { LLMSchedulerDO, validateConfig } from "../src/index.js";

function createMockSqlStorage() {
  const db = new DatabaseSync(":memory:");

  return {
    exec(query, ...params) {
      const trimmed = query.trim();
      if (trimmed.startsWith("SELECT") || trimmed.startsWith("select")) {
        const stmt = db.prepare(query);
        return stmt.all(...params);
      }
      if (params.length > 0) {
        db.prepare(query).run(...params);
      } else {
        db.exec(query);
      }
      return [];
    },
  };
}

function createMockEnv(overrides = {}) {
  const sql = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage: { sql } }, { MAX_JOBS_PER_UTC_DAY: "100" });

  return {
    BEARER_TOKEN: "secret-token",
    LLM_SCHEDULER: {
      getByName: () => coordinator,
      idFromName: () => "mock-id",
      get: () => coordinator,
    },
    DISPATCH_WINDOW_SECONDS: "25",
    MAX_RESPONSE_SECONDS: "720",
    FINALIZATION_RESERVE_SECONDS: "90",
    LEASE_DURATION_SECONDS: "840",
    CRON_EXECUTION_LIMIT_SECONDS: "900",
    CRON_TICK_SECONDS: "60",
    MAX_BUNDLES_PER_UTC_DAY: "1000",
    MAX_CONCURRENT_ROUTE_LANES: "5",
    MAX_JOBS_PER_UTC_DAY: "20000",
    ENQUEUE_BATCH_MAX: "1000",
    POLL_BATCH_MAX: "1000",
    ...overrides,
  };
}

test("validateConfig accepts valid configuration and rejects invalid", () => {
  const validEnv = createMockEnv();
  assert.doesNotThrow(() => validateConfig(validEnv));

  // Lease duration < window + response + reserve
  assert.throws(() =>
    validateConfig(
      createMockEnv({
        LEASE_DURATION_SECONDS: "800", // 25 + 720 + 90 = 835 > 800
      })
    )
  );

  // MAX_CONCURRENT_ROUTE_LANES > 5
  assert.throws(() =>
    validateConfig(
      createMockEnv({
        MAX_CONCURRENT_ROUTE_LANES: "6",
      })
    )
  );

  // MAX_JOBS_PER_UTC_DAY > 75,000 (75% limit)
  assert.throws(() =>
    validateConfig(
      createMockEnv({
        MAX_JOBS_PER_UTC_DAY: "80000",
      })
    )
  );
});

test("GET and HEAD /healthz return 200 without auth", async () => {
  const env = createMockEnv();
  const getReq = new Request("http://localhost/healthz", { method: "GET" });
  const getRes = await worker.fetch(getReq, env);
  assert.equal(getRes.status, 200);
  const data = await getRes.json();
  assert.deepEqual(data, { ok: true });

  const headReq = new Request("http://localhost/healthz", { method: "HEAD" });
  const headRes = await worker.fetch(headReq, env);
  assert.equal(headRes.status, 200);
});

test("POST /v2/jobs:enqueue-batch enforces Bearer auth and enqueues jobs", async () => {
  const env = createMockEnv();

  const unauthReq = new Request("http://localhost/v2/jobs:enqueue-batch", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jobs: [] }),
  });
  const unauthRes = await worker.fetch(unauthReq, env);
  assert.equal(unauthRes.status, 401);

  const payload = {
    jobs: [
      {
        id: "j1",
        idempotency_key: "k1",
        request_digest: "d1",
        prompt_family: "tags",
        input_token_estimate: 100,
        max_output_token_estimate: 50,
        payload_key: "payloads/j1/request.json",
      },
    ],
  };

  const req = new Request("http://localhost/v2/jobs:enqueue-batch", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: "Bearer secret-token",
    },
    body: JSON.stringify(payload),
  });

  const res = await worker.fetch(req, env);
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.deepEqual(data.accepted, [{ id: "j1" }]);
});

test("POST /v2/jobs:poll-batch polls coordinator and returns result_key without inlining", async () => {
  const env = createMockEnv();

  // Enqueue job j1
  const coordinator = env.LLM_SCHEDULER.getByName();
  await coordinator.enqueueBatch([
    {
      id: "j1",
      idempotency_key: "k1",
      request_digest: "d1",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/request.json",
    },
  ]);

  const req = new Request("http://localhost/v2/jobs:poll-batch", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: "Bearer secret-token",
    },
    body: JSON.stringify({ ids: ["j1"] }),
  });

  const res = await worker.fetch(req, env);
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.equal(data.statuses.length, 1);
  assert.equal(data.statuses[0].id, "j1");
  assert.equal(data.statuses[0].state, "queued");
  assert.equal(data.statuses[0].result_key, null);
});

test("POST /v2/jobs/{id}:schema-retry creates new retry identity", async () => {
  const env = createMockEnv();
  const req = new Request("http://localhost/v2/jobs/j1:schema-retry", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: "Bearer secret-token",
    },
    body: JSON.stringify({ corrected_payload_key: "payloads/j1/retry.json" }),
  });

  const res = await worker.fetch(req, env);
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.ok(data.id);
  assert.ok(data.idempotency_key.startsWith("j1:schema-retry:"));
});
