import test from "node:test";
import assert from "node:assert/strict";
import worker, { LLMSchedulerDO, validateConfig } from "../src/index.js";
import { createMockSqlStorage } from "./helpers.js";

function createMockEnv(overrides = {}) {
  const { storage } = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage }, { MAX_JOBS_PER_UTC_DAY: "100" });

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
    MAX_JOBS_PER_UTC_DAY: "5000",
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

  // MAX_JOBS_PER_MODEL_CLAIM > MAX_BUNDLE_JOBS
  assert.throws(() =>
    validateConfig(
      createMockEnv({
        MAX_BUNDLE_JOBS: "4",
        MAX_JOBS_PER_MODEL_CLAIM: "5",
      })
    )
  );

  // MAX_JOBS_PER_UTC_DAY > 5,000 (model-index write headroom)
  assert.throws(() =>
    validateConfig(
      createMockEnv({
        MAX_JOBS_PER_UTC_DAY: "5001",
      })
    )
  );

  assert.throws(() => validateConfig(createMockEnv({ MAX_5XX_RETRIES: "3" })));
  assert.throws(() => validateConfig(createMockEnv({ MAX_5XX_BACKOFF_SECONDS: "0" })));
  assert.doesNotThrow(() =>
    validateConfig(
      createMockEnv({
        MAX_QUEUED_JOB_MODEL_BACKFILL_PER_CLAIM: "0",
        MAX_LEGACY_RETRYABLE_RECOVERY_PER_CLAIM: "0",
        MAX_MIGRATION_WRITE_UNITS_PER_UTC_DAY: "0",
        MAX_MIGRATION_ROWS_SCANNED_PER_UTC_DAY: "0",
      })
    )
  );
  assert.throws(() =>
    validateConfig(createMockEnv({ MAX_QUEUED_JOB_MODEL_BACKFILL_PER_CLAIM: "-1" }))
  );
  assert.throws(() =>
    validateConfig(createMockEnv({ MAX_LEGACY_RETRYABLE_RECOVERY_PER_CLAIM: "1.5" }))
  );
  assert.throws(() =>
    validateConfig(createMockEnv({ MAX_MIGRATION_WRITE_UNITS_PER_UTC_DAY: "-1" }))
  );
  assert.throws(() =>
    validateConfig(createMockEnv({ MAX_MIGRATION_WRITE_UNITS_PER_UTC_DAY: "4" }))
  );
  assert.throws(() =>
    validateConfig(createMockEnv({ MAX_MIGRATION_ROWS_SCANNED_PER_UTC_DAY: "1.5" }))
  );

  // ESTIMATED_CALL_DURATION_CEILING_SECONDS >= DISPATCH_WINDOW_SECONDS
  assert.throws(() =>
    validateConfig(
      createMockEnv({
        DISPATCH_WINDOW_SECONDS: "25",
        ESTIMATED_CALL_DURATION_CEILING_SECONDS: "25",
      })
    )
  );

  // BEARER_TOKEN unset must fail closed at startup, not silently disable auth per-request.
  const noTokenEnv = createMockEnv();
  delete noTokenEnv.BEARER_TOKEN;
  assert.throws(() => validateConfig(noTokenEnv));
});

test("validateConfig rejects a CLEANUP_INTERVAL_MINUTES that does not evenly divide 60", () => {
  // 7 fires at :00, :07, ..., :56, then wraps to :00 -- a 4-minute gap, not the claimed 7-minute
  // cadence. Only divisors of 60 repeat an identical, evenly-spaced pattern every hour.
  assert.throws(() => validateConfig(createMockEnv({ CLEANUP_INTERVAL_MINUTES: "7" })));
  assert.doesNotThrow(() => validateConfig(createMockEnv({ CLEANUP_INTERVAL_MINUTES: "20" })));
  assert.doesNotThrow(() => validateConfig(createMockEnv({ CLEANUP_INTERVAL_MINUTES: "1" })));
});

test("validateConfig rejects a PURGE_BATCH_LIMIT that would exceed the 50-subrequest Free ceiling", () => {
  // Each purged job costs up to 2 B2 deletes (payload + result); dispatch in the same invocation
  // costs up to MAX_BUNDLE_JOBS * 2. Both must fit under 50 with headroom.
  assert.throws(() => validateConfig(createMockEnv({ PURGE_BATCH_LIMIT: "50" })));
  assert.throws(() =>
    validateConfig(createMockEnv({ PURGE_BATCH_LIMIT: "15", MAX_BUNDLE_JOBS: "20" }))
  );
  assert.doesNotThrow(() => validateConfig(createMockEnv({ PURGE_BATCH_LIMIT: "15" })));
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
  assert.deepEqual(data.accepted, [{ id: "j1", submitted_id: "j1" }]);
});

test("POST /v2/jobs:enqueue-batch fails closed when no auth token is configured", async () => {
  const env = createMockEnv();
  delete env.BEARER_TOKEN;

  const req = new Request("http://localhost/v2/jobs:enqueue-batch", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jobs: [] }),
  });
  // validateConfig() (called by the fetch handler) also rejects a token-less deployment
  // outright -- see the dedicated validateConfig assertion below -- but this exercises the
  // full request path.
  const res = await worker.fetch(req, env);
  assert.equal(res.status, 500);
  const data = await res.json();
  assert.equal(data.error, "configuration_error");
});

test("POST /v2/jobs:enqueue-batch surfaces the real error message, not a generic string", async () => {
  // Regression test for the 2026-08-18 incident: console.error("enqueueBatch failed", err)'s
  // second argument (the actual Error) never appeared in Cloudflare's exported Workers Logs --
  // only the literal call-site string did, even with a custom Logs field added in the
  // dashboard. Diagnosing a real coordinator_error required guessing at the cause blind. Both
  // the console.error call and the HTTP response body must now carry the real message/stack as
  // a plain string, which Workers Logs does reliably capture.
  const env = createMockEnv({
    LLM_SCHEDULER: {
      getByName: () => ({
        async enqueueBatch() {
          throw new Error("SQLITE_CONSTRAINT: distinctive test failure detail");
        },
      }),
    },
  });

  const req = new Request("http://localhost/v2/jobs:enqueue-batch", {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer secret-token" },
    body: JSON.stringify({
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
    }),
  });

  const res = await worker.fetch(req, env);
  assert.equal(res.status, 500);
  const data = await res.json();
  assert.equal(data.error, "coordinator_error");
  assert.match(data.detail, /SQLITE_CONSTRAINT: distinctive test failure detail/);
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

test("POST /v2/jobs/{id}:schema-retry reports not_implemented rather than a fake success", async () => {
  // schema-retry semantics land with Phase 2's dispatch machinery -- until then this endpoint
  // must not return a 200 with an id nothing was ever written for (a caller polling that id
  // would wait forever). See the note in index.js's schema-retry handler.
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
  assert.equal(res.status, 501);
  const data = await res.json();
  assert.equal(data.error, "not_implemented");
});
