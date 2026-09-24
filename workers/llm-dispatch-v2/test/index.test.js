import test from "node:test";
import assert from "node:assert/strict";
import worker, { LLMSchedulerDO, validateConfig } from "../src/index.js";
import { createMockSqlStorage, withTestReservations } from "./helpers.js";

function createMockEnv(overrides = {}) {
  const { storage } = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage }, withTestReservations({ MAX_JOBS_PER_UTC_DAY: "100" }));

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
    MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY: "18000",
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

  // The raw job cap still protects a runaway client even though write units are now the primary
  // admission budget.
  assert.throws(() =>
    validateConfig(
      createMockEnv({
        MAX_JOBS_PER_UTC_DAY: "20001",
      })
    )
  );

  assert.throws(() => validateConfig(createMockEnv({ MAX_5XX_RETRIES: "3" })));
  assert.throws(() => validateConfig(createMockEnv({ MAX_5XX_BACKOFF_SECONDS: "0" })));

  // ESTIMATED_CALL_DURATION_CEILING_SECONDS >= DISPATCH_WINDOW_SECONDS
  assert.throws(() =>
    validateConfig(
      createMockEnv({
        DISPATCH_WINDOW_SECONDS: "25",
        ESTIMATED_CALL_DURATION_CEILING_SECONDS: "25",
      })
    )
  );

  assert.throws(() => validateConfig(createMockEnv({ MAX_CANDIDATE_LOOKAHEAD: "2" })));
  assert.throws(() => validateConfig(createMockEnv({ MAX_CANDIDATE_LOOKAHEAD: "1000" })));

  // BEARER_TOKEN unset must fail closed at startup, not silently disable auth per-request.
  const noTokenEnv = createMockEnv();
  delete noTokenEnv.BEARER_TOKEN;
  assert.throws(() => validateConfig(noTokenEnv));
});

test("validateConfig rejects a CLEANUP_INTERVAL_MINUTES that does not evenly divide 60", () => {
  // 7 fires at :00, :07, ..., :56, then wraps to :00 -- a 4-minute gap, not the claimed 7-minute
  // cadence. Only divisors of 60 repeat an identical, evenly-spaced pattern every hour.
  assert.throws(() => validateConfig(createMockEnv({ CLEANUP_INTERVAL_MINUTES: "7" })));
  // Divisors are accepted: 20 -> 1,080 jobs/day, 12 -> 1,800, 10 -> 2,160.
  assert.doesNotThrow(() =>
    validateConfig(createMockEnv({ CLEANUP_INTERVAL_MINUTES: "20", MAX_LEASES_PER_UTC_DAY: "1000" }))
  );
  assert.doesNotThrow(() =>
    validateConfig(createMockEnv({ CLEANUP_INTERVAL_MINUTES: "12", MAX_LEASES_PER_UTC_DAY: "1800" }))
  );
  assert.doesNotThrow(() => validateConfig(createMockEnv({ CLEANUP_INTERVAL_MINUTES: "10" })));
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

test("POST /v2/jobs:terminal-feed returns terminal items and keyset cursor", async () => {
  const env = createMockEnv();
  const req = new Request("http://localhost/v2/jobs:terminal-feed", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: "Bearer secret-token",
    },
    body: JSON.stringify({ cursor: { updated_at: 0, id: "" }, limit: 10 }),
  });

  const res = await worker.fetch(req, env);
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.ok(Array.isArray(data.terminals));
  assert.ok(data.cursor && typeof data.cursor.updated_at === "number");
});

test("POST /v2/jobs/{id}:schema-retry persists a corrected queued job", async () => {
  const env = createMockEnv();
  const { getByName } = env.LLM_SCHEDULER;
  const coordinator = getByName();
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
  coordinator.sql.exec(
    "UPDATE jobs SET state='completed', result_key='results/j1.json' WHERE id='j1'"
  );
  const req = new Request("http://localhost/v2/jobs/j1:schema-retry", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: "Bearer secret-token",
    },
    body: JSON.stringify({
      corrected_payload_key: "payloads/retry-j1/request.json",
      corrected_request_digest: "d2",
      corrected_input_token_estimate: 123,
    }),
  });

  const res = await worker.fetch(req, env);
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.match(data.id, /^[0-9a-f-]{36}$/);
  assert.equal(data.idempotency_key, "schema-correction-v2:j1");
  const row = [...coordinator.sql.exec("SELECT * FROM jobs WHERE id = ?", data.id)][0];
  assert.equal(row.state, "queued");
  assert.equal(row.payload_key, "payloads/retry-j1/request.json");
  assert.equal(row.request_digest, "d2");
  assert.equal(row.input_token_estimate, 123);
  assert.equal(row.max_output_token_estimate, 50);
});

test("POST /v2/jobs/{id}:schema-retry rejects a non-completed source", async () => {
  const env = createMockEnv();
  const req = new Request("http://localhost/v2/jobs/missing:schema-retry", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: "Bearer secret-token",
    },
    body: JSON.stringify({
      corrected_payload_key: "payloads/retry/request.json",
      corrected_request_digest: "d2",
      corrected_input_token_estimate: 1,
    }),
  });

  const res = await worker.fetch(req, env);
  assert.equal(res.status, 404);
  assert.equal((await res.json()).error, "not_found");
});

test("GET /v2/stats requires auth and returns the bounded snapshot by default", async () => {
  const env = createMockEnv();

  // Queue depths and route health are operational detail, not public.
  const unauthRes = await worker.fetch(new Request("http://localhost/v2/stats"), env);
  assert.equal(unauthRes.status, 401);

  const res = await worker.fetch(
    new Request("http://localhost/v2/stats", {
      headers: { authorization: "Bearer secret-token" },
    }),
    env
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.ok(Number.isFinite(body.now));
  assert.ok(body.jobs && typeof body.jobs.by_state === "object");
  assert.ok(body.bundles && body.scheduler);
  assert.equal(body.queued_by_model, undefined);
});

test("GET /v2/ingress-status requires auth and reports the admission preflight", async () => {
  const env = createMockEnv();
  const unauthRes = await worker.fetch(
    new Request("http://localhost/v2/ingress-status?purpose=chapter-agenda"),
    env
  );
  assert.equal(unauthRes.status, 401);

  const res = await worker.fetch(
    new Request("http://localhost/v2/ingress-status?purpose=chapter-agenda", {
      headers: { authorization: "Bearer secret-token" },
    }),
    env
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.purpose, "chapter-agenda");
  assert.equal(typeof body.open, "boolean");
  assert.ok(Array.isArray(body.reasons));
  assert.ok(body.row_budget && Number.isFinite(body.row_budget.rows_written_today));
});

test("GET /v2/stats makes historical diagnostics explicit and clamps their limit", async () => {
  const env = createMockEnv();
  const call = async (qs) =>
    (await worker.fetch(
      new Request(`http://localhost/v2/stats?detail=1&${qs}`, {
        headers: { authorization: "Bearer secret-token" },
      }),
      env
    )).status;

  assert.equal(await call("limit=99999"), 200);
  assert.equal(await call("limit=0"), 200);
  assert.equal(await call("limit=notanumber"), 200);
});

test("the committed wrangler.jsonc vars pass validateConfig", async () => {
  const { readFile } = await import("node:fs/promises");
  const raw = await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8");
  // Strip // line comments that are not inside a string, then parse as JSON.
  const stripped = raw
    .split("\n")
    .map((line) => line.replace(/^(\s*)\/\/.*$/, "$1").replace(/("(?:[^"\\]|\\.)*"\s*[,:]?\s*)\/\/.*$/, "$1"))
    .join("\n");
  const { vars } = JSON.parse(stripped);
  assert.equal(vars.DISPATCH_WINDOW_SECONDS, "30");
  // The daily row thresholds rise enqueue <= claim <= optional, under the platform limit, and a
  // full day of admitted ingress fits under the enqueue threshold on its own.
  const { DO_ROWS_WRITTEN_PLATFORM_LIMIT, ROWS_PER_INGRESS_WRITE_UNIT } = await import(
    "../src/write_budget.js"
  );
  // The thresholds run at the coordinator's code defaults and are not declared as vars (Workers
  // Free counts every var and secret against a 64-variable limit), so check the EFFECTIVE value:
  // the declared one if a deployment overrides it, the default otherwise.
  const { LLMSchedulerDO } = await import("../src/coordinator.js");
  const { createMockSqlStorage } = await import("./helpers.js");
  const effective = new LLMSchedulerDO({ storage: createMockSqlStorage().storage }, { ...vars });
  assert.equal(effective._enqueueRowStop(), 90000);
  assert.equal(effective._claimRowStop(), 97000);
  assert.equal(effective._optionalRowStop(), 99000);
  assert.equal(effective._maxQueuedJobs(), 20000);
  assert.ok(effective._optionalRowStop() < DO_ROWS_WRITTEN_PLATFORM_LIMIT);
  assert.ok(
    ROWS_PER_INGRESS_WRITE_UNIT * Number(vars.MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY) <=
      effective._enqueueRowStop()
  );
  assert.equal(vars.ESTIMATED_CALL_DURATION_CEILING_SECONDS, "2");
  assert.doesNotThrow(() => validateConfig(createMockEnv({ ...vars })));
});


test("validateConfig requires ordered daily row thresholds under the platform limit", () => {
  assert.doesNotThrow(() => validateConfig(createMockEnv()));
  // Claims must not stop before enqueues, nor optional writes before claims.
  assert.throws(
    () => validateConfig(createMockEnv({ DO_ROWS_ENQUEUE_STOP: "98000", DO_ROWS_CLAIM_STOP: "97000" })),
    /DO_ROWS_ENQUEUE_STOP/
  );
  assert.throws(
    () => validateConfig(createMockEnv({ DO_ROWS_CLAIM_STOP: "99500", DO_ROWS_OPTIONAL_STOP: "99000" })),
    /DO_ROWS_ENQUEUE_STOP/
  );
  // Nothing may be allowed to reach the platform's own cutoff.
  assert.throws(
    () => validateConfig(createMockEnv({ DO_ROWS_OPTIONAL_STOP: "100000" })),
    /below the platform/
  );
  assert.throws(() => validateConfig(createMockEnv({ DO_ROWS_CLAIM_STOP: "abc" })));
});

test("validateConfig keeps a full day of ingress under the enqueue threshold", () => {
  // 2 rows per unit: 40,000 units could write 80,000 rows, past a 70,000 enqueue stop.
  assert.throws(
    () =>
      validateConfig(
        createMockEnv({ MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY: "40000", DO_ROWS_ENQUEUE_STOP: "70000" })
      ),
    /past DO_ROWS_ENQUEUE_STOP/
  );
  assert.doesNotThrow(() =>
    validateConfig(createMockEnv({ MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY: "18000" }))
  );
});

test("validateConfig bounds MAX_QUEUED_JOBS", () => {
  assert.throws(() => validateConfig(createMockEnv({ MAX_QUEUED_JOBS: "0" })));
  assert.throws(() => validateConfig(createMockEnv({ MAX_QUEUED_JOBS: "200000" })));
  assert.doesNotThrow(() => validateConfig(createMockEnv({ MAX_QUEUED_JOBS: "20000" })));
});

test("dispatch pause endpoints: auth, validation, pause, status and resume", async () => {
  const env = createMockEnv();
  const call = (method, path, body, token = "secret-token") =>
    worker.fetch(
      new Request(`https://example.test${path}`, {
        method,
        headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
      }),
      env
    );

  assert.equal((await call("POST", "/v2/dispatch:pause", { scope: "global", seconds: 60 }, "wrong")).status, 401);
  assert.equal((await call("POST", "/v2/dispatch:pause", { scope: "global" })).status, 400);
  assert.equal((await call("POST", "/v2/dispatch:pause", { scope: "global", seconds: 3601 })).status, 400);
  assert.equal((await call("POST", "/v2/dispatch:pause", { scope: "provider", seconds: 60 })).status, 400);
  const unknown = await call("POST", "/v2/dispatch:pause", { scope: "provider", target: "nope", seconds: 60 });
  assert.equal(unknown.status, 400);
  assert.equal((await unknown.json()).error, "unknown_target");

  const paused = await call("POST", "/v2/dispatch:pause", {
    scope: "provider",
    target: "gemini",
    seconds: 120,
    reason: "catalog canary",
  });
  assert.equal(paused.status, 200);
  assert.equal((await paused.json()).scope, "provider:gemini");

  const status = await call("GET", "/v2/dispatch:pause-status?scope=provider&target=gemini");
  assert.equal(status.status, 200);
  const statusBody = await status.json();
  assert.equal(statusBody.in_flight, 0);
  assert.equal(statusBody.pauses[0].scope, "provider:gemini");
  assert.ok(Object.keys(statusBody.routes).length > 0);
  assert.equal((await call("GET", "/v2/dispatch:pause-status?scope=provider")).status, 400);

  const reserve = await call("POST", "/v2/dispatch:reserve", {
    route_id: "gemini_3_1_flash_lite_primary",
    requests: 1,
  });
  assert.equal(reserve.status, 200);
  assert.equal((await call("POST", "/v2/dispatch:reserve", { route_id: "x", requests: 6 })).status, 400);

  const resumed = await call("POST", "/v2/dispatch:resume", { scope: "provider", target: "gemini" });
  assert.equal((await resumed.json()).resumed, true);
  const statsBody = await (await call("GET", "/v2/stats")).json();
  assert.deepEqual(statsBody.dispatch_pauses, []);
});
