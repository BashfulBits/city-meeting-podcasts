import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import { createMockSqlStorage, withTestReservations } from "./helpers.js";

const TEST_CATALOG = {
  model_aliases: {},
  model_routes_map: {
    "gemini/gemini-flash-lite": ["route-a", "route-b"],
    "mistral/mistral-small": ["route-c"],
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
    "route-b": {
      provider: "gemini",
      upstream_model: "gemini-flash-lite",
      rpm: 15,
      rpd: 500,
      tpm: 250000,
      free: true,
      input_context_limit: 1048576,
      output_context_limit: 65536,
    },
    "route-c": {
      provider: "mistral",
      upstream_model: "mistral-small",
      rpm: 60,
      rpd: 10000,
      tpm: 500000,
      free: true,
      input_context_limit: 32000,
      output_context_limit: 8000,
    },
  },
};

function makeCoordinator(envOverrides = {}) {
  const { sql, storage } = createMockSqlStorage();
  const env = {
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
    MAX_429_BACKOFF_SECONDS: "5",
    ESTIMATED_CALL_DURATION_CEILING_SECONDS: "5",
    DISPATCH_LIMITS_OVERRIDE: TEST_CATALOG,
    ...envOverrides,
  };
  return { coordinator: new LLMSchedulerDO({ storage }, withTestReservations(env)), sql };
}

function makeJob(id, overrides = {}) {
  return {
    id,
    idempotency_key: `key-${id}`,
    request_digest: `digest-${id}`,
    policy_json: JSON.stringify({ allowed_models: ["gemini/gemini-flash-lite"], allow_paid: false }),
    prompt_family: "tags",
    input_token_estimate: 500,
    max_output_token_estimate: 200,
    payload_key: `payloads/${id}/request.json`,
    ...overrides,
  };
}

test("claimDispatchWindow returns an empty plan when nothing is queued", async () => {
  const { coordinator } = makeCoordinator();
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(plan.bundle_id, null);
  assert.deepEqual(plan.jobs, []);
  assert.equal(plan.claim_reason, "no_queued_work");
  assert.equal(plan.claim_diagnostics.queued_jobs, 0);
});

test("claimDispatchWindow claims a queued job and leases it", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1")]);

  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);
  assert.ok(plan.bundle_id);
  assert.ok(plan.execution_token);
  assert.equal(plan.jobs.length, 1);
  const claimed = plan.jobs[0];
  assert.equal(claimed.id, "j1");
  assert.equal(claimed.payload_key, "payloads/j1/request.json");
  assert.ok(claimed.lease_token);
  assert.ok(["route-a", "route-b"].includes(claimed.route_id));
  assert.equal(claimed.wait_ms, 0); // fresh route, no prior usage
  assert.ok(claimed.token_reservation >= 700);

  const rows = [...sql.exec("SELECT state, lease_token, bundle_id FROM jobs WHERE id = 'j1'")];
  assert.equal(rows[0].state, "leased");
  assert.equal(rows[0].lease_token, claimed.lease_token);
  assert.equal(rows[0].bundle_id, plan.bundle_id);
});

test("claimDispatchWindow respects MAX_BUNDLE_JOBS", async () => {
  const { coordinator } = makeCoordinator({ MAX_BUNDLE_JOBS: "2" });
  await coordinator.enqueueBatch([
    makeJob("j1"),
    makeJob("j2"),
    makeJob("j3"),
    makeJob("j4"),
  ]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(plan.jobs.length, 2);
});

test("claimDispatchWindow prefers priority=0 jobs ahead of priority=1", async () => {
  const { coordinator } = makeCoordinator({ MAX_BUNDLE_JOBS: "1" });
  await coordinator.enqueueBatch([
    makeJob("low", { priority: 1 }),
    makeJob("high", { priority: 0 }),
  ]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(plan.jobs.length, 1);
  assert.equal(plan.jobs[0].id, "high");
});

test("claimDispatchWindow paces same-route jobs by the RPM inter-request gap", async () => {
  // Force both jobs onto the same route by using a model with only one eligible route.
  const { coordinator } = makeCoordinator({ MAX_BUNDLE_JOBS: "4" });
  await coordinator.enqueueBatch([
    makeJob("j1", {
      policy_json: JSON.stringify({ allowed_models: ["mistral/mistral-small"], allow_paid: false }),
    }),
    makeJob("j2", {
      policy_json: JSON.stringify({ allowed_models: ["mistral/mistral-small"], allow_paid: false }),
    }),
  ]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);
  assert.equal(plan.jobs.length, 2);
  assert.equal(plan.jobs[0].route_id, "route-c");
  assert.equal(plan.jobs[1].route_id, "route-c");
  // route-c has rpm=60 -> 1000ms min gap; second job must not start before the first's slot + gap.
  assert.equal(plan.jobs[1].min_inter_request_gap_ms, 1000);
  assert.ok(plan.jobs[1].not_before_at >= plan.jobs[0].not_before_at + 1000);
});

test(
  "claimDispatchWindow preserves a route's request-start safety margin across bundles",
  async () => {
    const catalog = structuredClone(TEST_CATALOG);
    Object.assign(catalog.routes_by_id["route-c"], {
      rpm: 1,
      request_start_margin_seconds: 2,
    });
    const { coordinator } = makeCoordinator({
      DISPATCH_LIMITS_OVERRIDE: catalog,
      MAX_BUNDLE_JOBS: "1",
    });
    await coordinator.enqueueBatch([
      makeJob("first", {
        policy_json: JSON.stringify({
          allowed_models: ["mistral/mistral-small"],
          allow_paid: false,
        }),
      }),
      makeJob("second", {
        policy_json: JSON.stringify({
          allowed_models: ["mistral/mistral-small"],
          allow_paid: false,
        }),
      }),
    ]);

    const now = Date.now();
    const first = await coordinator.claimDispatchWindow(now, 25);
    assert.equal(first.jobs.length, 1);
    assert.equal((await coordinator.claimDispatchWindow(now + 60_000, 25)).jobs.length, 0);
    assert.equal((await coordinator.claimDispatchWindow(now + 62_000, 25)).jobs.length, 1);
  }
);

test("claimDispatchWindow never opens more than MAX_CONCURRENT_ROUTE_LANES distinct lanes", async () => {
  const { coordinator } = makeCoordinator({ MAX_CONCURRENT_ROUTE_LANES: "1", MAX_BUNDLE_JOBS: "4" });
  await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2"), makeJob("j3")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  const distinctRoutes = new Set(plan.jobs.map((j) => j.route_id));
  assert.equal(distinctRoutes.size, 1);
});

test("claimDispatchWindow returns empty once MAX_ACTIVE_BUNDLES is reached", async () => {
  const { coordinator } = makeCoordinator({ MAX_ACTIVE_BUNDLES: "1", MAX_BUNDLE_JOBS: "1" });
  await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2")]);
  const now = Date.now();
  const first = await coordinator.claimDispatchWindow(now, 25);
  assert.equal(first.jobs.length, 1);
  const second = await coordinator.claimDispatchWindow(now, 25);
  assert.equal(second.bundle_id, null); // one active (uncompleted) bundle already outstanding
  assert.equal(second.claim_reason, "active_bundle_limit");
});

test("claimDispatchWindow reaps a bundle whose lease expired without completeBatch, freeing its MAX_ACTIVE_BUNDLES slot", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_ACTIVE_BUNDLES: "1",
    MAX_BUNDLE_JOBS: "1",
    LEASE_DURATION_SECONDS: "1",
  });
  await coordinator.enqueueBatch([makeJob("j1")]);

  const start = Date.now();
  const stuck = await coordinator.claimDispatchWindow(start, 25);
  assert.equal(stuck.jobs.length, 1);
  // Simulate an executor that never called completeBatch (crash, eviction, uncaught error):
  // the bundle stays 'active' and its job stays 'leased' with nothing else to move either.

  // Before the lease expires, the stuck bundle still correctly blocks new claims.
  const tooSoon = await coordinator.claimDispatchWindow(start + 500, 25);
  assert.equal(tooSoon.bundle_id, null);

  // Once its lease has expired, the next call must reap the stuck bundle and its leased job
  // instead of returning empty forever.
  const after = start + 2000;
  const recovered = await coordinator.claimDispatchWindow(after, 25);
  assert.ok(recovered.bundle_id);
  assert.notEqual(recovered.bundle_id, stuck.bundle_id);
  assert.equal(recovered.jobs.length, 1);
  assert.equal(recovered.jobs[0].id, "j1");

  const bundleRows = [...sql.exec("SELECT state FROM bundles WHERE bundle_id = ?", stuck.bundle_id)];
  assert.equal(bundleRows[0].state, "expired");
});

test("attemptStarted fences on a matching lease and rejects a stale one", async () => {
  const { coordinator } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  const job = plan.jobs[0];

  const ok = await coordinator.attemptStarted(job.id, job.lease_token, "attempt-1", Date.now());
  assert.equal(ok.fenced, true);

  const stale = await coordinator.attemptStarted(job.id, "wrong-lease-token", "attempt-2", Date.now());
  assert.equal(stale.fenced, false);
});

test("authorizeRetry authorizes a first 429 and declines a second on the same job", async () => {
  const { coordinator } = makeCoordinator({ MAX_429_RETRIES: "1", MAX_429_BACKOFF_SECONDS: "2" });
  await coordinator.enqueueBatch([makeJob("j1")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);
  const job = plan.jobs[0];

  await coordinator.attemptStarted(job.id, job.lease_token, "attempt-1", now);
  const first = await coordinator.authorizeRetry(job.id, job.lease_token, "attempt-1", now);
  assert.equal(first.authorized, true);
  assert.ok(first.retry_not_before > now);

  await coordinator.attemptStarted(job.id, job.lease_token, "attempt-2", now);
  const second = await coordinator.authorizeRetry(job.id, job.lease_token, "attempt-2", now);
  assert.equal(second.authorized, false);
});

test("authorizeRetry declines a retry that would not fit before the bundle deadline", async () => {
  // Backoff scales with the route's throttle_streak, capped by MAX_429_BACKOFF_SECONDS -- set the
  // cap high (so it isn't the limiting factor) and force the streak high directly on the ledger,
  // so the computed retry time clears the (default, 25s) dispatch window on its own.
  const { coordinator, sql } = makeCoordinator({ MAX_429_BACKOFF_SECONDS: "3600" });
  await coordinator.enqueueBatch([makeJob("j1")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);
  const job = plan.jobs[0];
  sql.exec("UPDATE routes SET throttle_streak = 100 WHERE route_id = ?", job.route_id);

  await coordinator.attemptStarted(job.id, job.lease_token, "attempt-1", now);
  const auth = await coordinator.authorizeRetry(job.id, job.lease_token, "attempt-1", now);
  assert.equal(auth.authorized, false);
});

test("completeBatch settles a successful job and is a no-op for a stale execution_token", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);
  const job = plan.jobs[0];

  await coordinator.completeBatch("not-the-real-bundle-id", "wrong-token", [
    { job_id: job.id, lease_token: job.lease_token, attempt_id: "a1", outcome: "success" },
  ]);
  let rows = [...sql.exec("SELECT state FROM jobs WHERE id='j1'")];
  assert.equal(rows[0].state, "leased"); // untouched by the stale call

  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: job.id,
      lease_token: job.lease_token,
      attempt_id: "a1",
      planned_at: job.not_before_at,
      actual_start_at: now,
      actual_end_at: now + 500,
      observed_input_tokens: 400,
      observed_output_tokens: 150,
      outcome: "success",
      provider_status_code: 200,
      gateway_correlation_id: "gw-1",
      result_key: "results/j1/lt1.json",
    },
  ]);
  rows = [...sql.exec("SELECT state, result_key FROM jobs WHERE id='j1'")];
  assert.equal(rows[0].state, "completed");
  assert.equal(rows[0].result_key, "results/j1/lt1.json");

  // Every leased job in the bundle settled, so the bundle row is deleted rather than kept.
  const bundleRows = [...sql.exec("SELECT state FROM bundles WHERE bundle_id=?", plan.bundle_id)];
  assert.equal(bundleRows.length, 0);
});

test("completeBatch requeues a deferred_late job without touching its attempt count", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  const job = plan.jobs[0];

  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: job.id,
      lease_token: job.lease_token,
      attempt_id: "a1",
      planned_at: job.not_before_at,
      outcome: "deferred_late",
    },
  ]);
  const rows = [...sql.exec("SELECT state, lease_token, bundle_id, attempts FROM jobs WHERE id='j1'")];
  assert.equal(rows[0].state, "queued");
  assert.equal(rows[0].lease_token, null);
  assert.equal(rows[0].bundle_id, null);
  assert.equal(rows[0].attempts, 0);
});

test("completeBatch requeues one final 5xx after Gateway retries, then fails the next one", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_5XX_RETRIES: "1" });
  await coordinator.enqueueBatch([makeJob("j1")]);
  const now = Date.now();
  const firstPlan = await coordinator.claimDispatchWindow(now, 25);
  const first = firstPlan.jobs[0];

  await coordinator.completeBatch(firstPlan.bundle_id, firstPlan.execution_token, [
    {
      job_id: first.id,
      lease_token: first.lease_token,
      attempt_id: "first-503",
      planned_at: first.not_before_at,
      outcome: "retryable_error",
      provider_status_code: 503,
    },
  ]);

  let row = [...sql.exec("SELECT state, transient_retry_count FROM jobs WHERE id='j1'")][0];
  assert.equal(row.state, "queued");
  assert.equal(row.transient_retry_count, 1);
  const route = [...sql.exec("SELECT blocked_until FROM routes WHERE route_id=?", first.route_id)][0];
  assert.ok(route.blocked_until >= now + 60_000);
  const alternatePlan = await coordinator.claimDispatchWindow(now + 1_000, 25);
  assert.equal(alternatePlan.jobs[0].id, "j1");
  assert.notEqual(alternatePlan.jobs[0].route_id, first.route_id);

  await coordinator.completeBatch(alternatePlan.bundle_id, alternatePlan.execution_token, [
    {
      job_id: "j1",
      lease_token: alternatePlan.jobs[0].lease_token,
      attempt_id: "second-503",
      planned_at: alternatePlan.jobs[0].not_before_at,
      outcome: "retryable_error",
      provider_status_code: 503,
    },
  ]);
  row = [...sql.exec("SELECT state FROM jobs WHERE id='j1'")][0];
  assert.equal(row.state, "failed");
});

test("a job with backup_models survives past the ordinary 5xx ceiling to reach backup eligibility", async () => {
  // Without the retry-ceiling extension, this job would terminally fail on its SECOND attempt
  // (MAX_5XX_RETRIES=1 allows exactly one retry) -- long before attempts could ever reach
  // backup_after_attempts=3, which sits well inside the 5-20 range backupModelsActive (routes.js)
  // expects callers to use. A raw 503 classifies as `server_error` (classify.js's HTTP-5xx rule
  // runs before any 429/400 check), so this is the realistic dominant failure mode for a
  // best-effort free route, not an edge case.
  const { coordinator, sql } = makeCoordinator({ MAX_5XX_RETRIES: "1" });
  await coordinator.enqueueBatch([
    makeJob("j1", {
      policy_json: JSON.stringify({
        allowed_models: ["gemini/gemini-flash-lite"],
        backup_models: ["mistral/mistral-small"],
        backup_after_attempts: 3,
        allow_paid: false,
      }),
    }),
  ]);

  let now = Date.now();
  let attemptNumber = 0;
  let state = "queued";
  // Bounded loop: the extended ceiling is backup_after_attempts(3) + MAX_5XX_RETRIES(1) = 4, so
  // this must terminally fail well before 10 iterations if the extension is bounded correctly.
  while (state === "queued" && attemptNumber < 10) {
    attemptNumber += 1;
    const plan = await coordinator.claimDispatchWindow(now, 25);
    assert.equal(plan.jobs.length, 1, `expected a claimable job on attempt ${attemptNumber}`);
    const claimed = plan.jobs[0];

    if (attemptNumber === 2) {
      // The exact point the OLD (unextended) ceiling would have already failed the job: attempt 1
      // failed and requeued (transient_retry_count=1), and the old code checked
      // `1 < MAX_5XX_RETRIES(1)` -> false -> failed, on THIS attempt's own completion. Assert the
      // job is claimable at all, which it could not be if it had already failed after attempt 1.
      assert.ok(claimed, "job must still be claimable past the ordinary 5xx ceiling");
    }

    await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
      {
        job_id: "j1",
        lease_token: claimed.lease_token,
        attempt_id: `attempt-${attemptNumber}`,
        planned_at: claimed.not_before_at,
        outcome: "retryable_error",
        provider_status_code: 503,
      },
    ]);
    const row = [...sql.exec("SELECT state, attempts FROM jobs WHERE id='j1'")][0];
    state = row.state;
    if (row.attempts >= 3) {
      // Once attempts crosses backup_after_attempts, the backup model must be indexed alongside
      // the primary -- confirming eligibility actually activated, not just that the job survived.
      const models = [...sql.exec(
        "SELECT model FROM job_models WHERE job_id='j1' ORDER BY model"
      )].map((r) => r.model);
      assert.deepEqual(models, ["gemini/gemini-flash-lite", "mistral/mistral-small"]);
    }
    // Comfortably above _max5xxBackoffMs()'s 300s cap so a route blocked by a prior 503 clears
    // its cooldown before the next claim -- otherwise, with only 2 gemini routes and backups not
    // yet eligible early on, both can be simultaneously blocked and nothing is claimable at all,
    // independent of the fix under test.
    now += 400_000;
  }

  assert.equal(state, "failed", "must still terminally fail eventually, not retry forever");
  assert.ok(attemptNumber > 2, "must survive past the ordinary (unextended) 5xx ceiling");
  // Whether route-c actually wins the ranking once eligible is a separate concern (capacity
  // score, free-before-paid) from reachability, which is what this test and the fix are about --
  // the job_models assertion above is the direct proof that eligibility itself activated.
});

test("completeBatch escalates blocked_until on consecutive 402s and clears it on the next success", async () => {
  const { coordinator, sql } = makeCoordinator();
  // Single-route model (see the next test's comment) so every claim below lands on route-c,
  // never a sibling route masking the block.
  const policy = JSON.stringify({ allowed_models: ["mistral/mistral-small"], allow_paid: false });

  // Pinned to a Wednesday. The escalation is calendar-based -- streak 1 blocks until tomorrow,
  // streak 2 until next UTC Monday -- so on a Sunday those two land on the SAME instant and the
  // strictly-greater assertion below fails through no fault of the code. Using the wall clock made
  // this test fail every Sunday; it surfaced on 2026-08-30.
  const FIXED_NOW = Date.parse("2026-08-26T12:00:00Z"); // Wednesday

  async function claimAndComplete(jobId, outcome, providerStatusCode) {
    await coordinator.enqueueBatch([makeJob(jobId, { policy_json: policy })]);
    const plan = await coordinator.claimDispatchWindow(FIXED_NOW, 25);
    const job = plan.jobs[0];
    await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
      {
        job_id: job.id,
        lease_token: job.lease_token,
        attempt_id: `attempt-${jobId}`,
        planned_at: job.not_before_at,
        outcome,
        provider_status_code: providerStatusCode,
      },
    ]);
    return job.route_id;
  }

  const routeId = await claimAndComplete("p1", "terminal_error", 402);
  assert.equal(routeId, "route-c");
  let row = [...sql.exec("SELECT payment_required_streak, blocked_until FROM routes WHERE route_id=?", routeId)][0];
  assert.equal(row.payment_required_streak, 1);
  const afterFirst = row.blocked_until;
  assert.ok(afterFirst > FIXED_NOW); // blocked into the future

  // Real time obviously can't advance a day inside a test; clear the block directly to simulate
  // it having already expired, exactly as it would in production once `now` passes blocked_until
  // -- this test is about what completeBatch does with the streak across separate 402s, not
  // about re-proving claimDispatchWindow's own blocked_until enforcement (see the next test).
  sql.exec("UPDATE routes SET blocked_until = 0 WHERE route_id = ?", routeId);

  await claimAndComplete("p2", "terminal_error", 402);
  row = [...sql.exec("SELECT payment_required_streak, blocked_until FROM routes WHERE route_id=?", routeId)][0];
  assert.equal(row.payment_required_streak, 2);
  assert.ok(row.blocked_until > afterFirst); // escalated further out (day -> week)

  sql.exec("UPDATE routes SET blocked_until = 0 WHERE route_id = ?", routeId);
  await claimAndComplete("p3", "success", 200);
  row = [...sql.exec("SELECT payment_required_streak, blocked_until FROM routes WHERE route_id=?", routeId)][0];
  assert.equal(row.payment_required_streak, 0);
  assert.equal(row.blocked_until, null);
});

test("claimDispatchWindow will not select a route still inside its 402 blocked_until window", async () => {
  const { coordinator } = makeCoordinator();
  // mistral/mistral-small has exactly one eligible route (route-c) in TEST_CATALOG, so blocking
  // it can't be masked by a sibling route picking up the second job instead.
  const policy = JSON.stringify({ allowed_models: ["mistral/mistral-small"], allow_paid: false });

  await coordinator.enqueueBatch([makeJob("b1", { policy_json: policy })]);
  const first = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(first.jobs[0].route_id, "route-c");
  await coordinator.completeBatch(first.bundle_id, first.execution_token, [
    {
      job_id: first.jobs[0].id,
      lease_token: first.jobs[0].lease_token,
      attempt_id: "a-b1",
      planned_at: first.jobs[0].not_before_at,
      outcome: "terminal_error",
      provider_status_code: 402,
    },
  ]);

  await coordinator.enqueueBatch([makeJob("b2", { policy_json: policy })]);
  const second = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(second.jobs.length, 0); // route-c is blocked until tomorrow; no other route serves this model
});

test("claimDispatchWindow skips a 402-blocked model without reading its queued prefix", async () => {
  const { coordinator } = makeCoordinator();
  const mistralPolicy = JSON.stringify({
    allowed_models: ["mistral/mistral-small"],
    allow_paid: false,
  });

  // Create route-c's durable ledger, then give it the same 402 block the production incident
  // writes. Twenty Mistral jobs precede Gemini work, but the model score is now zero.
  await coordinator.enqueueBatch([makeJob("seed", { policy_json: mistralPolicy })]);
  const seedPlan = await coordinator.claimDispatchWindow(Date.now(), 25);
  await coordinator.completeBatch(seedPlan.bundle_id, seedPlan.execution_token, [
    {
      job_id: seedPlan.jobs[0].id,
      lease_token: seedPlan.jobs[0].lease_token,
      attempt_id: "seed-402",
      planned_at: seedPlan.jobs[0].not_before_at,
      outcome: "terminal_error",
      provider_status_code: 402,
    },
  ]);

  await coordinator.enqueueBatch([
    ...Array.from({ length: 20 }, (_, index) =>
      makeJob(`blocked-${index}`, { policy_json: mistralPolicy })
    ),
    ...Array.from({ length: 4 }, (_, index) => makeJob(`gemini-${index}`)),
  ]);

  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.deepEqual(
    new Set(plan.jobs.map((job) => job.id)),
    new Set(["gemini-0", "gemini-1", "gemini-2", "gemini-3"])
  );
  assert.ok(plan.jobs.every((job) => ["route-a", "route-b"].includes(job.route_id)));
});

test("claimDispatchWindow skips a model whose live RPM capacity is exhausted", async () => {
  const { coordinator, sql } = makeCoordinator();
  const mistralPolicy = JSON.stringify({
    allowed_models: ["mistral/mistral-small"],
    allow_paid: false,
  });

  // Seed route-c, complete its bundle, then make its RPM window full. This is intentionally not
  // a 402: capacity-based deferrals must not hide later Gemini work either.
  await coordinator.enqueueBatch([makeJob("seed", { policy_json: mistralPolicy })]);
  const seedPlan = await coordinator.claimDispatchWindow(Date.now(), 25);
  await coordinator.completeBatch(seedPlan.bundle_id, seedPlan.execution_token, [
    {
      job_id: seedPlan.jobs[0].id,
      lease_token: seedPlan.jobs[0].lease_token,
      attempt_id: "seed-success",
      planned_at: seedPlan.jobs[0].not_before_at,
      outcome: "success",
      provider_status_code: 200,
    },
  ]);
  sql.exec("UPDATE routes SET rpm_window_start=?, rpm_count=60 WHERE route_id='route-c'", Date.now());

  await coordinator.enqueueBatch([
    ...Array.from({ length: 20 }, (_, index) =>
      makeJob(`limited-${index}`, { policy_json: mistralPolicy })
    ),
    ...Array.from({ length: 4 }, (_, index) => makeJob(`gemini-${index}`)),
  ]);

  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.deepEqual(
    new Set(plan.jobs.map((job) => job.id)),
    new Set(["gemini-0", "gemini-1", "gemini-2", "gemini-3"])
  );
});

test("claimDispatchWindow prefers a higher-capacity model over older queued work", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_BUNDLE_JOBS: "1" });
  const mistralPolicy = JSON.stringify({
    allowed_models: ["mistral/mistral-small"],
    allow_paid: false,
  });

  // Establish a live Mistral ledger, then leave it with only 1% of its daily capacity. Gemini's
  // routes remain at 100%, so this must outrank an older Mistral job without a global queue scan.
  await coordinator.enqueueBatch([makeJob("seed", { policy_json: mistralPolicy })]);
  const seedPlan = await coordinator.claimDispatchWindow(Date.now(), 25);
  await coordinator.completeBatch(seedPlan.bundle_id, seedPlan.execution_token, [
    {
      job_id: seedPlan.jobs[0].id,
      lease_token: seedPlan.jobs[0].lease_token,
      attempt_id: "seed-success",
      planned_at: seedPlan.jobs[0].not_before_at,
      outcome: "success",
      provider_status_code: 200,
    },
  ]);
  sql.exec("UPDATE routes SET rpd_window_start=?, rpd_count=9900 WHERE route_id='route-c'", Date.now());

  await coordinator.enqueueBatch([
    makeJob("mistral-first", { policy_json: mistralPolicy }),
    makeJob("gemini-later"),
  ]);

  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(plan.jobs[0].id, "gemini-later");
  assert.ok(["route-a", "route-b"].includes(plan.jobs[0].route_id));
});

test("claimDispatchWindow does not let oversized jobs permanently block a fit job behind them", async () => {
  const { coordinator } = makeCoordinator();
  const mistralPolicy = JSON.stringify({
    allowed_models: ["mistral/mistral-small"],
    allow_paid: false,
  });

  // Four jobs whose input_token_estimate exceeds route-c's own configured 32,000
  // input_context_limit -- no currently configured Mistral route could ever serve them --
  // followed by one ordinary job for the same model. Before the fix, indexing the oversized jobs
  // under "mistral/mistral-small" meant the bounded MAX_JOBS_PER_MODEL_CLAIM=4 read returned the
  // same four ineligible rows on every claim, and the fifth (perfectly dispatchable) job was
  // never even read, let alone claimed.
  await coordinator.enqueueBatch([
    ...Array.from({ length: 4 }, (_, i) =>
      makeJob(`hog-${i}`, { policy_json: mistralPolicy, input_token_estimate: 40000 })
    ),
    makeJob("zzz-fits", { policy_json: mistralPolicy }),
  ]);

  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.deepEqual(plan.jobs.map((job) => job.id), ["zzz-fits"]);
  assert.equal(plan.jobs[0].route_id, "route-c");

  // The oversized jobs themselves stay queued -- nothing configured could ever serve them -- but
  // are never wrongly claimed, on this tick or a later one.
  const second = await coordinator.claimDispatchWindow(Date.now() + 1000, 25);
  assert.deepEqual(second.jobs, []);
});

test("claimDispatchWindow honors a direct jobs.priority recovery edit on an already-queued job", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_BUNDLE_JOBS: "1" });

  // Both default to priority=1; "old-normal" is enqueued first, so plain FIFO would pick it.
  await coordinator.enqueueBatch([makeJob("old-normal")]);
  await coordinator.enqueueBatch([makeJob("urgent-but-late")]);

  // review/44's documented recovery/testing path: an operator promotes an already-queued job with
  // a direct SQLite edit (Cloudflare's dashboard Data Studio, or wrangler dev's Local Explorer SQL
  // Studio) rather than through enqueueBatch. The trg_jobs_priority_sync trigger must propagate
  // this into job_models.priority, or admission order silently never reflects the promotion.
  sql.exec("UPDATE jobs SET priority = 0 WHERE id = 'urgent-but-late'");

  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(plan.jobs[0]?.id, "urgent-but-late");
});

test("completeBatch calibration only ever raises margin_tokens, never lowers it", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);

  // First observation: 900 tokens, above the reservation -- establishes the margin.
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: plan.jobs[0].id,
      lease_token: plan.jobs[0].lease_token,
      attempt_id: "a1",
      planned_at: plan.jobs[0].not_before_at,
      observed_input_tokens: 700,
      observed_output_tokens: 200,
      outcome: "success",
      result_key: "results/j1/lt1.json",
    },
  ]);
  let rows = [...sql.exec("SELECT margin_tokens, sample_count FROM estimates")];
  assert.equal(rows.length, 1);
  assert.equal(rows[0].margin_tokens, 900);
  assert.equal(rows[0].sample_count, 1);

  // Second observation, LOWER than the first: must not decrease the recorded margin.
  const plan2 = await coordinator.claimDispatchWindow(now + 1, 25);
  if (plan2.jobs.length > 0) {
    await coordinator.completeBatch(plan2.bundle_id, plan2.execution_token, [
      {
        job_id: plan2.jobs[0].id,
        lease_token: plan2.jobs[0].lease_token,
        attempt_id: "a2",
        planned_at: plan2.jobs[0].not_before_at,
        observed_input_tokens: 300,
        observed_output_tokens: 100,
        outcome: "success",
        result_key: "results/j2/lt2.json",
      },
    ]);
    rows = [...sql.exec("SELECT margin_tokens, sample_count FROM estimates")];
    assert.equal(rows[0].margin_tokens, 900); // unchanged
    assert.equal(rows[0].sample_count, 2); // still recorded as an observation
  }
});

const CEILING_CATALOG = {
  model_aliases: {},
  model_routes_map: { "google/gemma-4-31b-it": ["gemma-ai-studio"] },
  routes_by_id: {
    "gemma-ai-studio": {
      provider: "gemini",
      upstream_model: "gemma-4-31b-it",
      rpm: 30,
      rpd: 14400,
      tpm: 14400,
      hard_input_ceiling: 10000,
      input_token_ratio: 1.2,
      free: true,
      input_context_limit: 262144,
      output_context_limit: 32768,
    },
  },
};

function gemmaJob(id, input, overrides = {}) {
  return makeJob(id, {
    policy_json: JSON.stringify({ allowed_models: ["google/gemma-4-31b-it"], allow_paid: false }),
    input_token_estimate: input,
    max_output_token_estimate: 1000,
    ...overrides,
  });
}

async function succeed(coordinator, plan, job, observedInput, observedOutput, attemptId) {
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: job.id,
      lease_token: job.lease_token,
      attempt_id: attemptId,
      planned_at: job.not_before_at,
      observed_input_tokens: observedInput,
      observed_output_tokens: observedOutput,
      outcome: "success",
      result_key: `results/${job.id}/${job.lease_token}.json`,
    },
  ]);
}

test("claim reserves input in the route's tokenizer units via its input_token_ratio prior", async () => {
  const { coordinator } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: CEILING_CATALOG });
  await coordinator.enqueueBatch([gemmaJob("g1", 5000)]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(plan.jobs.length, 1);
  // ceil(5000 * 1.2) input + the full 1000 max_tokens before any calibration samples exist.
  assert.equal(plan.jobs[0].token_reservation, 7000);
});

test("calibration follows recent output sizes instead of ratcheting on one large job", async () => {
  const { coordinator, sql } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: CEILING_CATALOG });
  const key = "gemma-ai-studio:google/gemma-4-31b-it:tags";
  // A window of 16 recent completions: ratio 1.0, outputs of 200 tokens -- plus a stale
  // high-water margin that the old calibration would have reserved for every later job.
  sql.exec(
    `INSERT INTO estimates (key, margin_tokens, sample_count, recent_observed_summary, updated_at)
     VALUES (?, 15000, 16, ?, 0)`,
    key,
    JSON.stringify({ r: Array(16).fill(1.0), o: Array(16).fill(200) })
  );
  await coordinator.enqueueBatch([gemmaJob("g1", 5000)]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  // 5000 * 1.0 learned ratio + ceil(200 * 1.25) forecast; the 15000 margin no longer applies.
  assert.equal(plan.jobs[0].token_reservation, 5250);

  await succeed(coordinator, plan, plan.jobs[0], 5100, 180, "a1");
  const row = [...sql.exec("SELECT margin_tokens, recent_observed_summary FROM estimates WHERE key = ?", key)][0];
  assert.equal(row.margin_tokens, 15000); // diagnostic high-water only
  const summary = JSON.parse(row.recent_observed_summary);
  assert.equal(summary.r.at(-1), 1.02);
  assert.equal(summary.o.at(-1), 180);
});

test("a successful completion settles the token bucket to actual usage", async () => {
  const { coordinator, sql } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: CEILING_CATALOG });
  await coordinator.enqueueBatch([gemmaJob("g1", 5000)]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  const reservation = plan.jobs[0].token_reservation;
  const read = () =>
    [...sql.exec(
      "SELECT full_token_budget, tpm_reserved FROM routes WHERE route_id = 'gemma-ai-studio'"
    )][0];
  const before = read();
  await succeed(coordinator, plan, plan.jobs[0], 4000, 300, "a1");
  const after = read();
  const refund = reservation - 4300;
  assert.ok(refund > 0);
  assert.equal(after.full_token_budget, before.full_token_budget + refund);
  assert.equal(after.tpm_reserved, Math.max(0, before.tpm_reserved - refund));
});

test("settlement leaves the per-minute window alone for a reservation larger than tpm", async () => {
  const { coordinator, sql } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: CEILING_CATALOG });
  // 8,000 raw x 1.2 = 9,600 input + 8,000 max_tokens = 17,600 > 14,400 tpm: bucket-gated only.
  await coordinator.enqueueBatch([
    gemmaJob("big-reservation", 8000, { max_output_token_estimate: 8000 }),
  ]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  const job = plan.jobs[0];
  assert.ok(job.token_reservation > 14400);
  // Another job's tokens already sit in the current window.
  sql.exec("UPDATE routes SET tpm_reserved = 5000 WHERE route_id = 'gemma-ai-studio'");
  const before = [...sql.exec(
    "SELECT full_token_budget FROM routes WHERE route_id = 'gemma-ai-studio'"
  )][0];
  await succeed(coordinator, plan, job, 9000, 300, "a1");
  const after = [...sql.exec(
    "SELECT full_token_budget, tpm_reserved FROM routes WHERE route_id = 'gemma-ai-studio'"
  )][0];
  assert.equal(after.tpm_reserved, 5000, "the window holds none of this reservation");
  assert.equal(after.full_token_budget, before.full_token_budget + (job.token_reservation - 9300));
});

test("claim looks past queue-head jobs too large for every route with capacity", async () => {
  // The production shape: large Gemma batches are indexed because an uncapped leg (SambaNova)
  // can take them, but that leg is out of capacity, leaving only the 10k-ceiling AI Studio route.
  const catalog = {
    ...CEILING_CATALOG,
    model_routes_map: { "google/gemma-4-31b-it": ["gemma-ai-studio", "gemma-uncapped"] },
    routes_by_id: {
      ...CEILING_CATALOG.routes_by_id,
      "gemma-uncapped": {
        provider: "sambanova",
        upstream_model: "gemma-4-31b-it",
        rpm: 20,
        rpd: 20,
        tpm: 90000,
        input_token_ratio: 1.2,
        concurrency: 1,
        free: true,
        input_context_limit: 131072,
        output_context_limit: 32768,
      },
    },
  };
  const { coordinator, sql } = makeCoordinator({
    DISPATCH_LIMITS_OVERRIDE: catalog,
    MAX_BUNDLE_JOBS: "2",
    MAX_JOBS_PER_MODEL_CLAIM: "2",
  });
  const big = Array.from({ length: 6 }, (_, i) => gemmaJob(`big-${i}`, 13000));
  await coordinator.enqueueBatch(big);
  await coordinator.enqueueBatch([gemmaJob("small", 6000)]);
  const indexed = [...sql.exec(
    "SELECT COUNT(*) AS n FROM job_models WHERE model = 'google/gemma-4-31b-it'"
  )][0].n;
  assert.equal(indexed, 7, "the large jobs are queued under the model via the uncapped leg");
  // The uncapped leg is out of capacity (blocked) for this tick.
  await coordinator.claimDispatchWindow(Date.now(), 30); // creates both route ledgers
  sql.exec("UPDATE jobs SET state = 'queued', lease_token = NULL WHERE state = 'leased'");
  sql.exec("DELETE FROM bundles");
  sql.exec("UPDATE routes SET blocked_until = ? WHERE route_id = 'gemma-uncapped'", Date.now() + 3_600_000);
  sql.exec("DELETE FROM job_models");
  for (const row of sql.exec("SELECT id, priority, created_at FROM jobs WHERE state = 'queued'")) {
    sql.exec(
      "INSERT INTO job_models (job_id, model, priority, created_at) VALUES (?, 'google/gemma-4-31b-it', ?, ?)",
      row.id,
      row.priority,
      row.created_at
    );
  }
  const plan = await coordinator.claimDispatchWindow(Date.now() + 120_000, 30);
  assert.deepEqual(plan.jobs.map((job) => job.id), ["small"]);
});

test("lookahead uses the calibrated ratio, not the static prior, to skip unservable jobs", async () => {
  const { coordinator, sql } = makeCoordinator({
    DISPATCH_LIMITS_OVERRIDE: CEILING_CATALOG,
    MAX_BUNDLE_JOBS: "2",
    MAX_JOBS_PER_MODEL_CLAIM: "2",
  });
  // Learned ratio 1.4 is above the route's 1.2 prior.
  sql.exec(
    `INSERT INTO estimates (key, margin_tokens, sample_count, recent_observed_summary, updated_at)
     VALUES (?, 0, 16, ?, 0)`,
    "gemma-ai-studio:google/gemma-4-31b-it:tags",
    JSON.stringify({ r: Array(16).fill(1.4), o: Array(16).fill(200) })
  );
  // 7,500 raw fits the ceiling at the 1.2 prior (9,000) but not at the learned 1.4 (10,500).
  // A static-ratio SQL prefilter would return only these and stall the model.
  const headOfLine = Array.from({ length: 4 }, (_, i) => gemmaJob(`mid-${i}`, 7500));
  await coordinator.enqueueBatch(headOfLine);
  await coordinator.enqueueBatch([gemmaJob("fits", 6000)]); // 8,400 at 1.4
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.deepEqual(plan.jobs.map((job) => job.id), ["fits"]);
});

test("claims stop at MAX_LEASES_PER_UTC_DAY and resume on the next UTC day", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_LEASES_PER_UTC_DAY: "3", MAX_BUNDLE_JOBS: "4" });
  await coordinator.enqueueBatch(Array.from({ length: 6 }, (_, i) => makeJob(`lease-${i}`)));
  const day = Date.UTC(2026, 8, 23, 12);
  const first = await coordinator.claimDispatchWindow(day, 30);
  assert.ok(first.jobs.length <= 3 && first.jobs.length > 0);
  let leased = first.jobs.length;
  // Settle the first bundle so concurrency never masks the lease cap.
  sql.exec("UPDATE bundles SET state = 'completed'");
  sql.exec("UPDATE jobs SET state = 'completed' WHERE state = 'leased'");
  for (let t = 1; t < 5 && leased < 3; t += 1) {
    const plan = await coordinator.claimDispatchWindow(day + t * 61_000, 30);
    leased += plan.jobs.length;
    sql.exec("UPDATE bundles SET state = 'completed'");
    sql.exec("UPDATE jobs SET state = 'completed' WHERE state = 'leased'");
  }
  assert.equal(leased, 3, "never more leases than the daily cap");
  const capped = await coordinator.claimDispatchWindow(day + 10 * 61_000, 30);
  assert.equal(capped.claim_reason, "daily_lease_limit");
  const scheduler = [...sql.exec("SELECT lease_count_today FROM scheduler WHERE id = 1")][0];
  assert.equal(scheduler.lease_count_today, 3);
  const nextDay = await coordinator.claimDispatchWindow(Date.UTC(2026, 8, 24, 0, 1), 30);
  assert.ok(nextDay.jobs.length > 0, "the counter resets with the UTC day");
});

test("a 404 requeues the job with a short escalating cooldown, not the retirement block", async () => {
  const { coordinator, sql } = makeCoordinator({ ROUTE_UNAVAILABLE_BLOCK_SECONDS: "21600" });
  await coordinator.enqueueBatch([makeJob("j1")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 30);
  const job = plan.jobs[0];
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: job.id,
      lease_token: job.lease_token,
      attempt_id: "a1",
      planned_at: job.not_before_at,
      actual_start_at: now,
      actual_end_at: now + 100,
      outcome: "retryable_error",
      provider_status_code: 404,
      failure_class: "upstream_capacity",
    },
  ]);
  const jobRow = [...sql.exec("SELECT state FROM jobs WHERE id = 'j1'")][0];
  assert.equal(jobRow.state, "queued");
  const route = [...sql.exec(
    "SELECT blocked_until, upstream_capacity_streak FROM routes WHERE route_id = ?",
    job.route_id
  )][0];
  assert.equal(route.upstream_capacity_streak, 1);
  assert.ok(route.blocked_until > now && route.blocked_until <= now + 300_000);
});

test("a 410 requeues the job and stands the whole route down", async () => {
  const { coordinator, sql } = makeCoordinator({ ROUTE_UNAVAILABLE_BLOCK_SECONDS: "3600" });
  await coordinator.enqueueBatch([makeJob("j1")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 30);
  const job = plan.jobs[0];
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: job.id,
      lease_token: job.lease_token,
      attempt_id: "a1",
      planned_at: job.not_before_at,
      actual_start_at: now,
      actual_end_at: now + 100,
      outcome: "retryable_error",
      provider_status_code: 410,
      failure_class: "route_unavailable",
    },
  ]);
  const jobRow = [...sql.exec("SELECT state, transient_retry_count FROM jobs WHERE id = 'j1'")][0];
  assert.equal(jobRow.state, "queued");
  assert.equal(jobRow.transient_retry_count, 1);
  const route = [...sql.exec(
    "SELECT blocked_until, last_failure_class FROM routes WHERE route_id = ?",
    job.route_id
  )][0];
  assert.ok(route.blocked_until >= now + 3_600_000 - 1000);
  assert.equal(route.last_failure_class, "route_unavailable");
  const indexed = [...sql.exec("SELECT COUNT(*) AS n FROM job_models WHERE job_id = 'j1'")][0];
  assert.ok(indexed.n > 0, "a requeued job must be re-indexed so a sibling route can take it");
});

test("purgePendingBatch and confirmPurge clean up old terminal jobs idempotently", async () => {
  const { coordinator, sql } = makeCoordinator({ COMPLETED_RETENTION_DAYS: "1" });
  await coordinator.enqueueBatch([makeJob("j1")]);
  const longAgo = Date.now() - 5 * 86_400_000;
  sql.exec(
    "UPDATE jobs SET state='completed', result_key='results/j1/x.json', updated_at=? WHERE id='j1'",
    longAgo
  );

  const pending = await coordinator.purgePendingBatch(10);
  assert.equal(pending.jobs.length, 1);
  assert.equal(pending.jobs[0].id, "j1");
  assert.equal(pending.jobs[0].payload_key, "payloads/j1/request.json");

  let rows = [...sql.exec("SELECT state FROM jobs WHERE id='j1'")];
  assert.equal(rows[0].state, "purge_pending");

  const result = await coordinator.confirmPurge(["j1"]);
  assert.equal(result.purged, 1);
  rows = [...sql.exec("SELECT id FROM jobs WHERE id='j1'")];
  assert.equal(rows.length, 0);

  // Repeating confirmPurge for the same (now-gone) id is a safe no-op.
  const again = await coordinator.confirmPurge(["j1"]);
  assert.equal(again.purged, 1); // counts attempted ids, not rows actually deleted; idempotent either way
});

test("confirmNeverAccepted reports which preassigned ids the DO has no record of", async () => {
  const { coordinator } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("accepted-1")]);
  const result = await coordinator.confirmNeverAccepted(["accepted-1", "never-sent-2"]);
  assert.deepEqual(result.neverAccepted, ["never-sent-2"]);
});

// --------------------------------------------------------------------------------------------
// Rows-read regression guards (2026-08-27 Durable Objects free-tier overage).
//
// The incident's cause was structural: `bundles` had no index on `state`, so claimDispatchWindow
// full-scanned it twice per cron tick, and nothing ever deleted from it -- measured at 6,400 of a
// tick's 6,424 rows read. These tests pin the query plans, so a dropped index or a reshaped
// predicate fails here rather than as a silent production quota burn.
// --------------------------------------------------------------------------------------------

/** Plan details for `query`, as one string. */
function planOf(sql, query, ...params) {
  return [...sql.exec(`EXPLAIN QUERY PLAN ${query}`, ...params)].map((r) => r.detail).join("; ");
}

test("claimDispatchWindow's two per-tick bundles statements are index seeks, never table scans", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  // A large terminal-bundle history is exactly the production shape that made these scans fatal.
  for (let i = 0; i < 500; i++) {
    sql.exec(
      "INSERT INTO bundles VALUES (?,?,'completed',?,0,?,?)",
      `b${i}`, "tok", now - 9e8, now - 9e8, now - 9e8
    );
  }

  const expireSweep = planOf(
    sql,
    "UPDATE bundles SET state='expired' WHERE state='active' AND lease_expires_at < ?",
    now
  );
  const activeRead = planOf(sql, "SELECT active_call_count FROM bundles WHERE state='active'");

  for (const plan of [expireSweep, activeRead]) {
    assert.match(plan, /SEARCH bundles USING (COVERING )?INDEX idx_bundles_state_created/);
    assert.doesNotMatch(plan, /SCAN/);
  }

  // And the claim still works with that history present.
  await coordinator.enqueueBatch([makeJob("j1")]);
  const plan = await coordinator.claimDispatchWindow(now, 25);
  assert.equal(plan.jobs.length, 1);
});

test("purgePendingBatch's terminal-job lookups are bounded per-state index seeks", async () => {
  const { sql } = makeCoordinator();
  for (const state of ["completed", "failed"]) {
    const plan = planOf(
      sql,
      `SELECT id, payload_key, result_key, updated_at FROM jobs
       WHERE state = '${state}' AND updated_at < ?
       ORDER BY updated_at ASC, id ASC LIMIT ?`,
      Date.now(),
      10
    );
    assert.match(plan, /SEARCH jobs USING INDEX idx_jobs_state_updated_id/);
    assert.doesNotMatch(plan, /SCAN|TEMP B-TREE/);
  }
});

test("purgePendingBatch merges completed and failed rows by age without changing its limit", async () => {
  const { coordinator, sql } = makeCoordinator({ COMPLETED_RETENTION_DAYS: "1" });
  const ids = ["completed-old", "failed-old", "completed-mid", "failed-mid", "recent"];
  await coordinator.enqueueBatch(ids.map((id) => makeJob(id)));
  const now = Date.now();
  const updates = [
    ["completed-old", "completed", now - 5 * 86_400_000],
    ["failed-old", "failed", now - 4 * 86_400_000],
    ["completed-mid", "completed", now - 3 * 86_400_000],
    ["failed-mid", "failed", now - 2 * 86_400_000],
    ["recent", "completed", now - 12 * 60 * 60 * 1000],
  ];
  for (const [id, state, updatedAt] of updates) {
    sql.exec("UPDATE jobs SET state=?, updated_at=? WHERE id=?", state, updatedAt, id);
  }

  const pending = await coordinator.purgePendingBatch(4);
  assert.deepEqual(
    pending.jobs.map((job) => job.id),
    ["completed-old", "failed-old", "completed-mid", "failed-mid"]
  );
  assert.equal([...sql.exec("SELECT COUNT(*) n FROM jobs WHERE state='purge_pending'")][0].n, 4);
});

test("_pruneTerminalRecords deletes aged-out terminal bundles and attempts, bounded per tick", async () => {
  const { coordinator, sql } = makeCoordinator({
    BUNDLE_RETENTION_DAYS: "7",
    ATTEMPT_RETENTION_DAYS: "7",
    MAX_BUNDLE_PRUNE_PER_TICK: "10",
    MAX_ATTEMPT_PRUNE_PER_TICK: "10",
  });
  const now = Date.now();
  const old = now - 30 * 86_400_000;
  for (let i = 0; i < 25; i++) {
    sql.exec("INSERT INTO bundles VALUES (?,?,'completed',?,0,?,?)", `b${i}`, "t", old, old, old);
    sql.exec(
      "INSERT INTO attempts (attempt_id, job_id, route_id, planned_at, start_state, created_at)" +
        " VALUES (?,?,?,?,'started',?)",
      `a${i}`, `j${i}`, "route-a", old, old
    );
  }

  const first = coordinator._pruneTerminalRecords(now);
  assert.deepEqual(first, { bundlesDeleted: 10, attemptsDeleted: 10, routeFailuresDeleted: 0 });
  assert.equal([...sql.exec("SELECT COUNT(*) n FROM bundles")][0].n, 15);
  assert.equal([...sql.exec("SELECT COUNT(*) n FROM attempts")][0].n, 15);

  // Repeated ticks drain the backlog and then stop finding work.
  coordinator._pruneTerminalRecords(now);
  const third = coordinator._pruneTerminalRecords(now);
  assert.deepEqual(third, { bundlesDeleted: 5, attemptsDeleted: 5, routeFailuresDeleted: 0 });
  assert.deepEqual(
    coordinator._pruneTerminalRecords(now),
    { bundlesDeleted: 0, attemptsDeleted: 0, routeFailuresDeleted: 0 }
  );
});

test("_pruneTerminalRecords never removes an active bundle, a recent one, or one whose lease could still be current", async () => {
  const { coordinator, sql } = makeCoordinator({ BUNDLE_RETENTION_DAYS: "7" });
  const now = Date.now();
  const old = now - 30 * 86_400_000;
  // (a) still active; (b) terminal but inside the retention window; (c) terminal and old, but its
  // lease has not expired yet -- a late completeBatch could still legitimately settle it.
  sql.exec("INSERT INTO bundles VALUES ('active-1','t','active',?,1,?,?)", now + 6e5, now, old);
  sql.exec("INSERT INTO bundles VALUES ('recent-1','t','completed',?,0,?,?)", now, now, now - 1000);
  sql.exec("INSERT INTO bundles VALUES ('leased-1','t','completed',?,0,?,?)", now + 6e5, now, old);
  sql.exec("INSERT INTO bundles VALUES ('stale-1','t','completed',?,0,?,?)", old, old, old);

  const result = coordinator._pruneTerminalRecords(now);
  assert.equal(result.bundlesDeleted, 1);
  const remaining = [...sql.exec("SELECT bundle_id FROM bundles ORDER BY bundle_id")].map((r) => r.bundle_id);
  assert.deepEqual(remaining, ["active-1", "leased-1", "recent-1"]);
});

test("a zero per-tick prune cap pauses retention without affecting dispatch", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_BUNDLE_PRUNE_PER_TICK: "0",
    MAX_ATTEMPT_PRUNE_PER_TICK: "0",
  });
  const now = Date.now();
  const old = now - 30 * 86_400_000;
  sql.exec("INSERT INTO bundles VALUES ('stale-1','t','completed',?,0,?,?)", old, old, old);

  assert.deepEqual(
    coordinator._pruneTerminalRecords(now),
    { bundlesDeleted: 0, attemptsDeleted: 0, routeFailuresDeleted: 0 }
  );
  assert.equal([...sql.exec("SELECT COUNT(*) n FROM bundles")][0].n, 1);

  await coordinator.enqueueBatch([makeJob("j1")]);
  assert.equal((await coordinator.claimDispatchWindow(now, 25)).jobs.length, 1);
});

// --------------------------------------------------------------------------------------------
// Consumption ack (review/44 "Consumption ack"). The client acks a job once its result is
// fetched, validated and durably written to the deferred registry, which is the real moment the
// DO row and its B2 objects stop being needed -- COMPLETED_RETENTION_DAYS is only the backstop.
// --------------------------------------------------------------------------------------------

/** Enqueue, claim and settle `jobId` with the given outcome, returning nothing. */
async function settleJob(coordinator, jobId, outcome, extra = {}) {
  await coordinator.enqueueBatch([makeJob(jobId)]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  const claimed = plan.jobs.find((j) => j.id === jobId);
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: claimed.id,
      lease_token: claimed.lease_token,
      attempt_id: `att-${jobId}`,
      planned_at: claimed.not_before_at,
      actual_start_at: Date.now(),
      actual_end_at: Date.now(),
      outcome,
      ...extra,
    },
  ]);
}

test("ackResults retires a completed job immediately, without waiting out COMPLETED_RETENTION_DAYS", async () => {
  const { coordinator, sql } = makeCoordinator();
  await settleJob(coordinator, "j1", "success", {
    provider_status_code: 200,
    result_key: "results/j1.json",
  });

  // Not yet ackable-by-age: freshly completed, so the time-based purge finds nothing.
  assert.deepEqual((await coordinator.purgePendingBatch(10)).jobs, []);

  const result = await coordinator.ackResults(["j1"]);
  assert.deepEqual(result, { acked: ["j1"], ignored: [] });
  assert.equal([...sql.exec("SELECT state FROM jobs WHERE id='j1'")][0].state, "purge_pending");

  // The existing cleanup handshake now picks it up and hands back both B2 keys to delete.
  const pending = await coordinator.purgePendingBatch(10);
  assert.deepEqual(pending.jobs.map((j) => j.id), ["j1"]);
  assert.equal(pending.jobs[0].result_key, "results/j1.json");
  await coordinator.confirmPurge(["j1"]);
  assert.equal([...sql.exec("SELECT id FROM jobs WHERE id='j1'")].length, 0);
});

test("ackResults refuses a failed job so the sweep's schema-correction path keeps its row", async () => {
  const { coordinator, sql } = makeCoordinator();
  await settleJob(coordinator, "bad", "terminal_error", { provider_status_code: 400 });
  assert.equal([...sql.exec("SELECT state FROM jobs WHERE id='bad'")][0].state, "failed");

  const result = await coordinator.ackResults(["bad"]);
  assert.deepEqual(result, { acked: [], ignored: ["bad"] });
  assert.equal([...sql.exec("SELECT state FROM jobs WHERE id='bad'")][0].state, "failed");
});

test("ackResults never retires a job that is still queued or leased, and reports unknown ids", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("queued-1")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  const leasedId = plan.jobs[0].id;
  await coordinator.enqueueBatch([makeJob("queued-2")]);

  const result = await coordinator.ackResults([leasedId, "queued-2", "never-existed"]);
  assert.deepEqual(result.acked, []);
  assert.deepEqual(new Set(result.ignored), new Set([leasedId, "queued-2", "never-existed"]));
  const states = [...sql.exec("SELECT id, state FROM jobs ORDER BY id")];
  assert.deepEqual(states.find((r) => r.id === leasedId).state, "leased");
  assert.deepEqual(states.find((r) => r.id === "queued-2").state, "queued");
});

test("ackResults is idempotent and chunks past the 100 bound-parameter ceiling", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_BUNDLE_JOBS: "4" });
  const ids = Array.from({ length: 250 }, (_, i) => `bulk-${i}`);
  await coordinator.enqueueBatch(ids.map((id) => makeJob(id)));
  // Settle them all directly: this test is about ack chunking, not dispatch pacing.
  sql.exec("UPDATE jobs SET state='completed', result_key='r.json' WHERE state='queued'");

  const first = await coordinator.ackResults(ids);
  assert.equal(first.acked.length, 250);
  assert.deepEqual(first.ignored, []);
  assert.equal(
    [...sql.exec("SELECT COUNT(*) n FROM jobs WHERE state='purge_pending'")][0].n,
    250
  );

  // A replayed ack (client retry) is a harmless no-op, not an error.
  const second = await coordinator.ackResults(ids);
  assert.deepEqual(second.acked, []);
  assert.equal(second.ignored.length, 250);
});

test("purgePendingBatch re-lists a job stranded in purge_pending by a crashed cleanup run", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1")]);
  sql.exec(
    "UPDATE jobs SET state='completed', result_key='results/j1.json', updated_at=? WHERE id='j1'",
    Date.now() - 60 * 86_400_000
  );

  // First run marks it and hands back its keys...
  const first = await coordinator.purgePendingBatch(10);
  assert.deepEqual(first.jobs.map((j) => j.id), ["j1"]);
  // ...then the executor dies before confirmPurge. The row must not be stranded: the next run has
  // to hand back the very same keys so the orphaned B2 objects still get deleted.
  const second = await coordinator.purgePendingBatch(10);
  assert.deepEqual(second.jobs.map((j) => j.id), ["j1"]);
  assert.equal(second.jobs[0].payload_key, "payloads/j1/request.json");
  assert.equal(second.jobs[0].result_key, "results/j1.json");

  await coordinator.confirmPurge(["j1"]);
  assert.deepEqual((await coordinator.purgePendingBatch(10)).jobs, []);
});

test("purgePendingBatch honors its limit across carried-over and newly-eligible rows together", async () => {
  const { coordinator, sql } = makeCoordinator();
  const ids = Array.from({ length: 8 }, (_, i) => `j${i}`);
  await coordinator.enqueueBatch(ids.map((id) => makeJob(id)));
  sql.exec("UPDATE jobs SET state='completed', updated_at=? WHERE state='queued'",
    Date.now() - 60 * 86_400_000);

  const first = await coordinator.purgePendingBatch(3);
  assert.equal(first.jobs.length, 3); // 3 newly eligible, now purge_pending
  const second = await coordinator.purgePendingBatch(5);
  // 3 carried over + 2 newly eligible, never more than the limit.
  assert.equal(second.jobs.length, 5);
  assert.equal(
    [...sql.exec("SELECT COUNT(*) n FROM jobs WHERE state='purge_pending'")][0].n,
    5
  );
});

// A paid route deliberately given far more headroom than the free one it competes with: the
// capacity ranking alone would pick it every time. `allow_paid` is permission to spend when
// nothing free will do, not a preference for spending, so free must win regardless of headroom.
const FREE_VS_PAID_CATALOG = {
  model_aliases: {},
  // Paid listed first on purpose: the routes tie on capacity fraction (both unused), so without
  // an explicit free-before-paid term the tie falls through to catalog order and the paid route
  // wins. Listing free first would let this test pass with the bug still present.
  model_routes_map: { "opencode/mimo-v2.5-free": ["paid-large", "free-small"] },
  routes_by_id: {
    "free-small": {
      provider: "opencode",
      upstream_model: "mimo-v2.5-free",
      rpm: 5,
      rpd: 50,
      tpm: 100000,
      free: true,
      input_context_limit: 1000000,
      output_context_limit: 100000,
    },
    "paid-large": {
      provider: "siliconflow",
      upstream_model: "deepseek-ai/DeepSeek-V4-Flash-0731",
      rpm: 1000,
      rpd: 100000,
      tpm: 10000000,
      free: false,
      input_context_limit: 1000000,
      output_context_limit: 100000,
    },
  },
};

test("a job allowing paid still takes the free route when the paid one has more capacity", async () => {
  const { sql, storage } = createMockSqlStorage();
  const env = {
    MAX_JOBS_PER_UTC_DAY: "10000",
    MAX_BUNDLE_JOBS: "1",
    MAX_JOBS_PER_ROUTE_PER_BUNDLE: "1",
    MAX_CONCURRENT_ROUTE_LANES: "5",
    MAX_ACTIVE_BUNDLES: "2",
    MAX_IN_FLIGHT_LLM_CALLS: "8",
    MAX_BUNDLES_PER_UTC_DAY: "1000",
    MAX_QUEUE_WAIT_SECONDS: "3600",
    LEASE_DURATION_SECONDS: "840",
    MAX_429_RETRIES: "1",
    MAX_429_BACKOFF_SECONDS: "5",
    ESTIMATED_CALL_DURATION_CEILING_SECONDS: "5",
    DISPATCH_LIMITS_OVERRIDE: FREE_VS_PAID_CATALOG,
  };
  const coordinator = new LLMSchedulerDO({ storage }, withTestReservations(env));
  const paidJob = (id) => ({
    id,
    idempotency_key: `key-${id}`,
    request_digest: `digest-${id}`,
    policy_json: JSON.stringify({
      allowed_models: ["opencode/mimo-v2.5-free"],
      allow_paid: true,
    }),
    prompt_family: "tags",
    input_token_estimate: 500,
    max_output_token_estimate: 200,
    payload_key: `payloads/${id}/request.json`,
  });

  // Spend some of the free route's capacity first, so the two routes are NOT tied on capacity
  // fraction: paid-large (1000 rpm / 100k rpd, untouched) now scores strictly higher than
  // free-small (5 rpm / 50 rpd, partly consumed). Without a real difference the comparator's
  // capacity terms tie and the assertion below would also pass with capacity ranked ahead of
  // free -- it would only be testing the tie-break, not the precedence.
  const t0 = Date.now();
  await coordinator.enqueueBatch([paidJob("warmup")]);
  const warm = await coordinator.claimDispatchWindow(t0, 25);
  assert.equal(warm.jobs[0].route_id, "free-small");
  await coordinator.completeBatch(warm.bundle_id, warm.execution_token, [
    {
      job_id: "warmup",
      lease_token: warm.jobs[0].lease_token,
      attempt_id: "warmup-attempt",
      planned_at: warm.jobs[0].not_before_at,
      actual_start_at: t0,
      actual_end_at: t0 + 500,
      observed_input_tokens: 500,
      observed_output_tokens: 200,
      outcome: "success",
      provider_status_code: 200,
      result_key: "results/warmup.json",
    },
  ]);

  const consumed = [...sql.exec("SELECT rpm_count, rpd_count FROM routes WHERE route_id=?", "free-small")];
  assert.ok(
    (consumed[0]?.rpm_count || 0) > 0 || (consumed[0]?.rpd_count || 0) > 0,
    "the free route must actually have spent capacity for this test to mean anything",
  );

  await coordinator.enqueueBatch([paidJob("paid-allowed")]);
  const plan = await coordinator.claimDispatchWindow(t0 + 60_000, 25);
  assert.equal(plan.jobs.length, 1);
  assert.equal(
    plan.jobs[0].route_id,
    "free-small",
    "allow_paid must not send a job to a paid route while a free one has capacity, even when the "
      + "paid route ranks higher on available capacity",
  );
});

test("a 402-requeued job is claimable again, not stranded queued-without-index", async () => {
  // Claiming a job deletes its job_models index rows. A requeue that only rewrites `state` leaves
  // the job queued with a stale lease and no index row, so claimDispatchWindow can never select it
  // again -- silently stranded, and invisible to the operator requeue script, which only reads
  // v1's R2 queue. The second claim below is the actual assertion.
  const { coordinator } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-402")]);

  const first = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(first.jobs.length, 1);
  const claimed = first.jobs[0];

  const t = Date.now();
  await coordinator.completeBatch(first.bundle_id, first.execution_token, [
    {
      job_id: claimed.id,
      lease_token: claimed.lease_token,
      attempt_id: "attempt-402",
      planned_at: claimed.not_before_at,
      actual_start_at: t,
      actual_end_at: t + 500,
      observed_input_tokens: 400,
      observed_output_tokens: 0,
      outcome: "retryable_error",
      provider_status_code: 402,
    },
  ]);

  const second = await coordinator.claimDispatchWindow(Date.now() + 120_000, 25);
  assert.equal(second.jobs.length, 1, "the 402-requeued job must be claimable again");
  assert.equal(second.jobs[0].id, "j-402");
});

// A provider whose daily quota rolls at midnight Pacific, probed at a moment when UTC and Pacific
// disagree about the date: 2026-08-29T03:00Z is still 2026-08-28 20:00 in America/Los_Angeles.
// Keying the daily window on UTC (or on a rolling 24h from first use) gets this backwards.
const PACIFIC_CATALOG = {
  model_aliases: {},
  model_routes_map: { "gemini/flash-lite": ["gem-a"] },
  providers: { gemini: { reset_timezone: "America/Los_Angeles" } },
  routes_by_id: {
    "gem-a": {
      provider: "gemini",
      upstream_model: "flash-lite",
      rpm: 15,
      rpd: 250,
      tpm: 250000,
      free: true,
      input_context_limit: 1000000,
      output_context_limit: 65536,
    },
  },
};
const PACIFIC_NOW = Date.parse("2026-08-29T03:00:00Z"); // 2026-08-28 20:00 Pacific

function pacificCoordinator() {
  const { sql, storage } = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO(
    { storage },
    withTestReservations({
      MAX_JOBS_PER_UTC_DAY: "10000",
      MAX_BUNDLE_JOBS: "1",
      MAX_JOBS_PER_ROUTE_PER_BUNDLE: "1",
      MAX_CONCURRENT_ROUTE_LANES: "5",
      MAX_ACTIVE_BUNDLES: "2",
      MAX_IN_FLIGHT_LLM_CALLS: "8",
      MAX_BUNDLES_PER_UTC_DAY: "1000",
      MAX_QUEUE_WAIT_SECONDS: "86400",
      LEASE_DURATION_SECONDS: "840",
      MAX_429_RETRIES: "1",
      MAX_429_BACKOFF_SECONDS: "5",
      ESTIMATED_CALL_DURATION_CEILING_SECONDS: "5",
      DISPATCH_LIMITS_OVERRIDE: PACIFIC_CATALOG,
    })
  );
  return { coordinator, sql };
}

function pacificJob(id) {
  return {
    id,
    idempotency_key: `key-${id}`,
    request_digest: `digest-${id}`,
    policy_json: JSON.stringify({ allowed_models: ["gemini/flash-lite"], allow_paid: false }),
    prompt_family: "tags",
    input_token_estimate: 500,
    max_output_token_estimate: 200,
    payload_key: `payloads/${id}/request.json`,
  };
}

test("a daily quota spent earlier the same Pacific day still blocks, even though UTC has rolled", async () => {
  const { coordinator, sql } = pacificCoordinator();
  await coordinator.enqueueBatch([pacificJob("j-pt-1")]);
  coordinator._getOrCreateRouteLedger("gem-a", PACIFIC_NOW, PACIFIC_CATALOG.routes_by_id["gem-a"]);
  // Exhausted earlier today in Pacific terms. UTC is already 2026-08-29, so a UTC-keyed (or
  // rolling-window) implementation would wrongly consider this reset.
  sql.exec("UPDATE routes SET rpd_count = 250, rpd_day_key = '2026-08-28' WHERE route_id = 'gem-a'");

  const plan = await coordinator.claimDispatchWindow(PACIFIC_NOW, 25);
  assert.equal(plan.jobs.length, 0, "the provider's day has not rolled yet, so nothing may dispatch");
});

test("once the Pacific day rolls, the quota resets even though under 24h has passed", async () => {
  const { coordinator, sql } = pacificCoordinator();
  await coordinator.enqueueBatch([pacificJob("j-pt-2")]);
  coordinator._getOrCreateRouteLedger("gem-a", PACIFIC_NOW, PACIFIC_CATALOG.routes_by_id["gem-a"]);
  // Exhausted yesterday in Pacific terms; the provider has since rolled over.
  sql.exec("UPDATE routes SET rpd_count = 250, rpd_day_key = '2026-08-27' WHERE route_id = 'gem-a'");

  const plan = await coordinator.claimDispatchWindow(PACIFIC_NOW, 25);
  assert.equal(plan.jobs.length, 1, "a rolled-over daily quota must not keep the model out");
  assert.equal(plan.jobs[0].route_id, "gem-a");
});

test("_capacityFraction reads the daily quota on the provider's calendar, not UTC", () => {
  // Direct unit assertion: with one model the ranking order is unobservable end-to-end, so the
  // score has to be checked here or a UTC-keyed regression slips through.
  const { coordinator } = pacificCoordinator();
  const route = {
    ...PACIFIC_CATALOG.routes_by_id["gem-a"],
    route_id: "gem-a",
    reset_timezone: "America/Los_Angeles",
    rpm_window_start: 0,
    rpm_count: 0,
    rpd_count: 250,
    tpm_window_start: 0,
    tpm_reserved: 0,
    full_token_budget: 250000 * 5,
    token_budget_updated_at: PACIFIC_NOW,
    blocked_until: null,
    buffer_seconds: 0,
  };
  // Spent earlier the same Pacific day -> no capacity, even though UTC has already rolled to 08-29.
  assert.equal(
    coordinator._capacityFraction({ ...route, rpd_day_key: "2026-08-28" }, PACIFIC_NOW, 60),
    0,
  );
  // Spent the previous Pacific day -> the provider has reset, so full daily headroom.
  assert.ok(
    coordinator._capacityFraction({ ...route, rpd_day_key: "2026-08-27" }, PACIFIC_NOW, 60) > 0,
  );
});

test("a reservation stamps the provider's calendar day, not a rolling-window anchor", async () => {
  const { coordinator, sql } = pacificCoordinator();
  await coordinator.enqueueBatch([pacificJob("j-pt-3")]);
  const plan = await coordinator.claimDispatchWindow(PACIFIC_NOW, 25);
  assert.equal(plan.jobs.length, 1);

  const row = [...sql.exec("SELECT rpd_day_key, rpd_count FROM routes WHERE route_id='gem-a'")][0];
  assert.equal(row.rpd_day_key, "2026-08-28", "must record the Pacific date, not UTC's 08-29");
  assert.equal(row.rpd_count, 1);
});

test("a capacity-400 requeues the job and stands the route down, like a final 5xx", async () => {
  // v2's half of the OpenCode Zen case: HTTP 400 whose body blames the provider's own upstream.
  // The DO never sees payloads, so the dispatcher does the sniffing and completeBatch keys off
  // the pair (retryable_error, 400) alone. Without the pairing this fell to the generic branch,
  // failing the job and leaving the route selectable for every sibling behind it.
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-cap-400")]);

  const first = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(first.jobs.length, 1);
  const claimed = first.jobs[0];
  const routeId = claimed.route_id;

  const t = Date.now();
  await coordinator.completeBatch(first.bundle_id, first.execution_token, [
    {
      job_id: claimed.id,
      lease_token: claimed.lease_token,
      attempt_id: "attempt-cap-400",
      planned_at: claimed.not_before_at,
      actual_start_at: t,
      actual_end_at: t + 500,
      observed_input_tokens: 400,
      observed_output_tokens: 0,
      outcome: "retryable_error",
      provider_status_code: 400,
    },
  ]);

  const blockedUntil = [...sql.exec(
    "SELECT blocked_until FROM routes WHERE route_id=?", routeId)][0]?.blocked_until;
  assert.ok(blockedUntil && blockedUntil > t, "the unreachable route must be stood down");
  // Clear it so the retry below is testing job recoverability, not this same block.
  sql.exec("UPDATE routes SET blocked_until = 0 WHERE route_id = ?", routeId);

  const second = await coordinator.claimDispatchWindow(Date.now() + 120_000, 25);
  assert.equal(second.jobs.length, 1, "the job must be retried, not destroyed");
  assert.equal(second.jobs[0].id, "j-cap-400");
});

test("a provider missing-function 404 requeues and cools only the affected route", async () => {
  // NVIDIA NIM reports a retired hosted function as HTTP 404. It is not a malformed job, so the
  // classifier must preserve the upstream_capacity signal through completeBatch; otherwise this
  // response is terminal and every large submission is lost before lane backups can activate.
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-cap-404")]);

  const first = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(first.jobs.length, 1);
  const claimed = first.jobs[0];
  const routeId = claimed.route_id;

  const t = Date.now();
  await coordinator.completeBatch(first.bundle_id, first.execution_token, [
    {
      job_id: claimed.id,
      lease_token: claimed.lease_token,
      attempt_id: "attempt-cap-404",
      planned_at: claimed.not_before_at,
      actual_start_at: t,
      actual_end_at: t + 500,
      observed_input_tokens: 400,
      observed_output_tokens: 0,
      outcome: "retryable_error",
      provider_status_code: 404,
      failure_class: "upstream_capacity",
    },
  ]);

  const route = [...sql.exec(
    "SELECT blocked_until, upstream_capacity_streak FROM routes WHERE route_id=?",
    routeId,
  )][0];
  assert.ok(route.blocked_until && route.blocked_until > t, "the missing function route is cooled");
  assert.equal(route.upstream_capacity_streak, 1);

  sql.exec("UPDATE routes SET blocked_until = 0 WHERE route_id = ?", routeId);
  const second = await coordinator.claimDispatchWindow(Date.now() + 120_000, 25);
  assert.equal(second.jobs.length, 1, "the job must be retried, not destroyed");
  assert.equal(second.jobs[0].id, "j-cap-404");
});

test("a provider input-limit failure requeues for a sibling route", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-input-limit")]);

  const first = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(first.jobs.length, 1);
  const claimed = first.jobs[0];
  const now = Date.now();
  await coordinator.completeBatch(first.bundle_id, first.execution_token, [
    {
      job_id: claimed.id,
      lease_token: claimed.lease_token,
      attempt_id: "attempt-input-limit",
      planned_at: claimed.not_before_at,
      actual_start_at: now,
      actual_end_at: now + 100,
      outcome: "terminal_error",
      provider_status_code: 500,
      failure_class: "route_input_limit",
    },
  ]);

  const job = [...sql.exec(
    "SELECT state, transient_retry_count FROM jobs WHERE id='j-input-limit'"
  )][0];
  assert.equal(job.state, "queued");
  assert.equal(job.transient_retry_count, 1);
  const route = [...sql.exec(
    "SELECT blocked_until, last_failure_class FROM routes WHERE route_id=?",
    claimed.route_id
  )][0];
  assert.ok(route.blocked_until > now);
  assert.equal(route.last_failure_class, "route_input_limit");

  const second = await coordinator.claimDispatchWindow(now + 1_000, 25);
  assert.equal(second.jobs.length, 1);
  assert.equal(second.jobs[0].id, "j-input-limit");
  assert.notEqual(second.jobs[0].route_id, claimed.route_id);
});

test("a genuine 400 still fails the job terminally and leaves the route selectable", async () => {
  // The negative case that keeps the pairing narrow. A real request defect reaches the DO as
  // `terminal_error`, and must not block a healthy route just because it shares a status code.
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-bad-400")]);

  const first = await coordinator.claimDispatchWindow(Date.now(), 25);
  const claimed = first.jobs[0];
  const routeId = claimed.route_id;

  const t = Date.now();
  await coordinator.completeBatch(first.bundle_id, first.execution_token, [
    {
      job_id: claimed.id,
      lease_token: claimed.lease_token,
      attempt_id: "attempt-bad-400",
      planned_at: claimed.not_before_at,
      actual_start_at: t,
      actual_end_at: t + 500,
      observed_input_tokens: 400,
      observed_output_tokens: 0,
      outcome: "terminal_error",
      provider_status_code: 400,
    },
  ]);

  const row = [...sql.exec("SELECT blocked_until FROM routes WHERE route_id=?", routeId)][0];
  assert.ok(!row?.blocked_until, "a malformed request says nothing about the route's health");

  const second = await coordinator.claimDispatchWindow(Date.now() + 120_000, 25);
  assert.equal(second.jobs.length, 0, "a genuine request defect must not be retried");
});

test("stats distinguishes an empty queue from a stranded one", async () => {
  // The whole reason the endpoint exists. On 2026-08-29 v2 idled through 705 of 721 cron ticks
  // and there was no way to tell from outside whether the queue was empty, every route was
  // blocked, or jobs were queued but missing their job_models index rows -- three causes that
  // look identical and have nothing in common.
  const { coordinator, sql } = makeCoordinator();

  const empty = await coordinator.detailedStats(Date.now());
  assert.equal(empty.jobs.by_state.queued ?? 0, 0);
  assert.equal(empty.jobs.queued_without_model_index, 0);
  assert.equal(empty.jobs.oldest_queued_age_ms, null);
  assert.deepEqual(empty.queued_by_model, []);

  await coordinator.enqueueBatch([makeJob("j-a"), makeJob("j-b")]);
  const queued = await coordinator.detailedStats(Date.now());
  assert.equal(queued.jobs.by_state.queued, 2);
  assert.equal(queued.jobs.queued_without_model_index, 0, "healthy jobs are indexed");
  assert.ok(queued.queued_by_model.length > 0, "queued work is visible per model");
  assert.equal(
    queued.queued_by_model.reduce((n, r) => Math.max(n, r.queued), 0),
    2
  );

  // Now reproduce the stranding shape: rows present, index gone. by_state still says "queued".
  sql.exec("DELETE FROM job_models");
  const stranded = await coordinator.detailedStats(Date.now());
  assert.equal(stranded.jobs.by_state.queued, 2, "still queued as far as the jobs table knows");
  assert.deepEqual(stranded.queued_by_model, [], "but invisible to the scheduler");
  assert.equal(stranded.jobs.queued_without_model_index, 2, "which is exactly what this reports");
});

test("stats reports a standing-down route and its reason", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-blocked")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  const job = plan.jobs[0];
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: job.id,
      lease_token: job.lease_token,
      attempt_id: "a-402",
      planned_at: job.not_before_at,
      outcome: "terminal_error",
      provider_status_code: 402,
    },
  ]);

  const now = Date.now();
  const s = await coordinator.detailedStats(now);
  const blocked = s.routes.blocked.find((r) => r.route_id === job.route_id);
  assert.ok(blocked, "a 402-blocked route must be listed");
  assert.ok(blocked.blocked_until > now);
  assert.equal(blocked.payment_required_streak, 1);
  assert.equal(blocked.last_provider_status, 402);

  // A route whose block has lapsed is healthy again and must drop off the list, or every route
  // ever throttled would accumulate here and bury the ones actually standing down.
  sql.exec("UPDATE routes SET blocked_until = ? WHERE route_id = ?", now - 1000, job.route_id);
  const after = await coordinator.detailedStats(now);
  assert.equal(after.routes.blocked.find((r) => r.route_id === job.route_id), undefined);
});

test("a route that took a 429 becomes claimable again once its buffer decays", async () => {
  // THE 2026-08-30 production stall. One 429 added 60s of buffer; _capacityFraction scores a
  // route 0 as soon as its buffer covers the 25s dispatch window; and the only code that cleared
  // the buffer ran on a *successful* completeBatch for that route -- which scoring 0 makes
  // unreachable. A single 429 therefore removed a route from the ranking permanently, with no
  // blocked_until and no error: 15,833 jobs queued, every route silently at zero, nothing
  // dispatched for days. The final claim below is the whole assertion.
  const { coordinator, sql } = makeCoordinator();
  // Single-route model, or a sibling route absorbs the claim and the buffer is never exercised.
  const soloPolicy = JSON.stringify({
    allowed_models: ["mistral/mistral-small"],
    allow_paid: false,
  });
  await coordinator.enqueueBatch([makeJob("j-buffered-1", { policy_json: soloPolicy })]);

  const first = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(first.jobs.length, 1);
  const routeId = first.jobs[0].route_id;
  const t = Date.now();

  // Settle the first bundle so it is not itself holding a slot, and settle it with a plain
  // terminal 400 -- a success would clear buffer_seconds and a 5xx would set blocked_until,
  // and either would mask the thing under test.
  await coordinator.completeBatch(first.bundle_id, first.execution_token, [
    {
      job_id: first.jobs[0].id,
      lease_token: first.jobs[0].lease_token,
      attempt_id: "a-settle",
      planned_at: first.jobs[0].not_before_at,
      outcome: "terminal_error",
      provider_status_code: 400,
    },
  ]);

  // Fresh queued work for the two claims below.
  await coordinator.enqueueBatch([makeJob("j-buffered-2", { policy_json: soloPolicy })]);

  // Put the route in exactly the state one 429 leaves behind.
  sql.exec(
    "UPDATE routes SET buffer_seconds = 60, buffer_updated_at = ?, throttle_streak = 1 WHERE route_id = ?",
    t,
    routeId
  );

  // While the buffer is still owed the route is correctly stood down...
  const during = await coordinator.claimDispatchWindow(t + 1000, 25);
  assert.equal(during.jobs.length, 0, "the buffer should gate the route while it is still owed");

  // ...and once it has run down the route must come back on its own, with no success required.
  const after = await coordinator.claimDispatchWindow(t + 120_000, 25);
  assert.equal(after.jobs.length, 1, "a 429 must not remove a route from the ranking forever");
  assert.equal(after.jobs[0].route_id, routeId);
});

test("an expired bundle gives its token reservation back to the route", async () => {
  // provisional_reservation is added at claim and subtracted only in completeBatch, which by
  // definition never runs for a bundle that died mid-tick. Without an explicit release here the
  // expire-sweep requeued the job but kept its reservation charged against the route forever --
  // a silent, cumulative drain that lowers the route's capacity score on every crash until it
  // stops being ranked at all.
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-leak")]);

  const before = [...sql.exec("SELECT route_id, provisional_reservation FROM routes")];
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(plan.jobs.length, 1);
  const routeId = plan.jobs[0].route_id;

  const held = [...sql.exec(
    "SELECT provisional_reservation FROM routes WHERE route_id = ?", routeId
  )][0].provisional_reservation;
  assert.ok(held > 0, "claiming must actually reserve something for this test to mean anything");

  // Let the lease expire without any completeBatch, then run the sweep via the next claim.
  // LEASE_DURATION_SECONDS is 840 in this env, so step comfortably past it.
  const later = Date.now() + 20 * 60 * 1000;
  await coordinator.claimDispatchWindow(later, 25);

  // Assert the accounting identity, not one route's balance. That same claim tick also REQUEUES
  // the expired job and immediately re-leases it, so `routeId` legitimately ends up holding a
  // reservation again whenever the re-claim happens to rank the same route first. Which route
  // wins depends on live daily-quota state, which keys on the provider's calendar day -- so the
  // old per-route assertion passed only when the re-claim landed elsewhere, and failed for real
  // on any run whose +20min step crossed UTC midnight (observed in CI at 23:50 UTC, and
  // reproducible at will by pinning the clock). The release itself was never broken.
  //
  // What "not leaked" actually means: every token still reserved on a route must belong to a
  // lease that is still alive. A leaked dead lease shows up as exactly one job's reservation
  // counted twice.
  const reservedOnRoutes = [...sql.exec(
    "SELECT COALESCE(SUM(provisional_reservation), 0) AS n FROM routes"
  )][0].n;
  const heldByLiveLeases = [...sql.exec(
    "SELECT COALESCE(SUM(token_reservation), 0) AS n FROM jobs WHERE state = 'leased'"
  )][0].n;
  assert.equal(
    reservedOnRoutes,
    heldByLiveLeases,
    "the dead lease's reservation must be released, not leaked: routes hold " +
      `${reservedOnRoutes} tokens but only ${heldByLiveLeases} belong to a live lease`
  );
  assert.ok(before.every((r) => r.provisional_reservation === 0), "baseline sanity");
});

test("stats surfaces a route zeroed by its 429 buffer, not just by blocked_until", async () => {
  // The observability half of the same bug: the first version of this endpoint reported only
  // blocked_until, so during the stall it showed an empty `blocked` list while every route was
  // scoring 0. Capacity is the number the scheduler ranks on, so report that.
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-stats-buffer")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  const routeId = plan.jobs[0].route_id;

  const t = Date.now();
  sql.exec(
    "UPDATE routes SET buffer_seconds = 60, buffer_updated_at = ? WHERE route_id = ?",
    t,
    routeId
  );

  const s = await coordinator.detailedStats(t + 1000);
  const row = s.routes.all.find((r) => r.route_id === routeId);
  assert.ok(row, "the route must appear even though nothing blocked it");
  assert.equal(row.blocked_until, null, "no block is set on this path -- that was the trap");
  assert.ok(row.buffer_remaining_seconds > 25, "the buffer still covers the dispatch window");
  assert.equal(row.capacity, 0, "so it contributes no capacity");
  assert.ok(
    s.routes.zero_capacity.some((r) => r.route_id === routeId),
    "and it must be listed as zero-capacity"
  );
  assert.ok(s.routes.zero_capacity_count >= 1);
});

test("a superseded job is claimable and completes, closing the 2026-08-25 enqueue stall", async () => {
  // End-to-end version of the coordinator-level supersede tests: not just that enqueueBatch
  // accepts the corrected resubmission, but that the resulting row is actually reachable by
  // claimDispatchWindow and can run to completion under its new payload.
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j-stale", { request_digest: "digest-v1-leaked-fields" })]);
  sql.exec("UPDATE jobs SET state = 'failed', updated_at = ? WHERE id = 'j-stale'", Date.now());

  const enqueueResult = await coordinator.enqueueBatch([
    makeJob("resubmit", {
      idempotency_key: "key-j-stale",
      request_digest: "digest-v2-corrected",
      payload_key: "payloads/j-stale/v2.json",
    }),
  ]);
  assert.deepEqual(enqueueResult.accepted, [
    { id: "j-stale", submitted_id: "resubmit", superseded: true },
  ]);

  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(plan.jobs.length, 1, "the superseded row must be reachable by the scheduler");
  assert.equal(plan.jobs[0].id, "j-stale");
  assert.equal(plan.jobs[0].payload_key, "payloads/j-stale/v2.json");

  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: plan.jobs[0].id,
      lease_token: plan.jobs[0].lease_token,
      attempt_id: "a-resubmit",
      planned_at: plan.jobs[0].not_before_at,
      actual_start_at: Date.now(),
      actual_end_at: Date.now() + 500,
      observed_input_tokens: 100,
      observed_output_tokens: 50,
      outcome: "success",
      provider_status_code: 200,
      result_key: "results/j-stale/v2.json",
    },
  ]);
  const row = [...sql.exec("SELECT state, result_key FROM jobs WHERE id = 'j-stale'")][0];
  assert.equal(row.state, "completed");
  assert.equal(row.result_key, "results/j-stale/v2.json");
});

test("authorizeRetry honors an explicit Retry-After floor and blocks the route", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_429_RETRIES: "3" });
  await coordinator.enqueueBatch([makeJob("j1")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);
  const job = plan.jobs[0];

  await coordinator.attemptStarted(job.id, job.lease_token, "attempt-1", now);
  // Upstream returns 429 with Retry-After: 5
  const auth = await coordinator.authorizeRetry(job.id, job.lease_token, "attempt-1", now, 5);
  assert.equal(auth.authorized, true);
  // Positive jitter may follow an upstream delay, but can never move the retry before it.
  assert.ok(auth.retry_not_before >= now + 5000);
  assert.ok(auth.retry_not_before <= now + 6000);

  const routeRow = [
    ...sql.exec(
      "SELECT buffer_seconds, blocked_until, throttle_streak FROM routes WHERE route_id=?",
      job.route_id
    ),
  ][0];
  assert.ok(routeRow.buffer_seconds >= 5);
  assert.ok(routeRow.blocked_until >= now + 5000);
  assert.equal(routeRow.throttle_streak, 1);
});

test("authorizeRetry preserves an Airforce guarantee beyond the local buffer cap", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_429_RETRIES: "3",
    MAX_ROUTE_BUFFER_SECONDS: "60",
  });
  await coordinator.enqueueBatch([makeJob("j-airforce")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);
  const job = plan.jobs[0];

  await coordinator.attemptStarted(job.id, job.lease_token, "attempt-1", now);
  // Production Airforce response: the next answer was guaranteed only after 111 seconds.
  const auth = await coordinator.authorizeRetry(job.id, job.lease_token, "attempt-1", now, 111);
  assert.equal(
    auth.authorized,
    false,
    "the retry must not squeeze into a 25-second bundle window"
  );

  const routeRow = [
    ...sql.exec(
      "SELECT buffer_seconds, blocked_until FROM routes WHERE route_id=?",
      job.route_id
    ),
  ][0];
  assert.equal(routeRow.buffer_seconds, 60, "the adaptive buffer remains separately bounded");
  assert.ok(
    routeRow.blocked_until >= now + 111_000,
    "a later cron must not bypass Airforce's guaranteed-response deadline"
  );
  }
);

test("claimDispatchWindow enforces route-level and provider-level concurrency", async () => {
  const limits = {
    providers: {
      strict_prov: {
        api_base: "https://example.com",
        accounts: [{ id: "acc1" }],
        concurrency: 1,
      },
    },
    routes_by_id: {
      r1: {
        route_id: "r1",
        model: "m1",
        provider: "strict_prov",
        account_id: "acc1",
        input_context_limit: 100000,
        output_context_limit: 10000,
        rpm: 30,
        rpd: 1000,
        free: true,
        concurrency: 1,
      },
      r2: {
        route_id: "r2",
        model: "m2",
        provider: "strict_prov",
        account_id: "acc1",
        input_context_limit: 100000,
        output_context_limit: 10000,
        rpm: 30,
        rpd: 1000,
        free: true,
        concurrency: 1,
      },
    },
    model_routes_map: {
      m1: ["r1"],
      m2: ["r2"],
    },
  };

  const { coordinator } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: limits });
  await coordinator.enqueueBatch([
    makeJob("j1", { policy_json: JSON.stringify({ allowed_models: ["m1"] }) }),
    makeJob("j2", { policy_json: JSON.stringify({ allowed_models: ["m2"] }) }),
  ]);

  const now = Date.now();
  // Provider concurrency is 1, so only 1 job should be admitted across the whole provider
  const plan = await coordinator.claimDispatchWindow(now, 25);
  assert.equal(plan.jobs.length, 1);

  // A second claim while j1 is still leased admits 0 jobs
  const secondPlan = await coordinator.claimDispatchWindow(now + 100, 25);
  assert.equal(secondPlan.jobs.length, 0);
  assert.equal(secondPlan.claim_result, "empty");
  assert.equal(secondPlan.claim_reason, "concurrency_limit");
  assert.equal(secondPlan.claim_diagnostics.rejections.provider_concurrency, 1);
  assert.deepEqual(secondPlan.claim_diagnostics.routes.provider_concurrency, { strict_prov: 1 });

  const stats = await coordinator.detailedStats(now + 100);
  assert.equal(stats.claim.last_result, "empty");
  assert.equal(stats.claim.last_reason, "concurrency_limit");
  assert.equal(stats.claim.empty_count_today, 1);
  assert.equal(stats.claim.reason_counts_today.concurrency_limit, 1);
  assert.equal(stats.in_flight.by_provider.strict_prov, 1);
});

test("claimDispatchWindow identifies a route concurrency ceiling", async () => {
  const limits = {
    providers: {
      route_only_prov: {
        api_base: "https://example.com",
        accounts: [{ id: "acc1" }],
      },
    },
    routes_by_id: {
      route_only: {
        route_id: "route_only",
        model: "route-model",
        provider: "route_only_prov",
        account_id: "acc1",
        input_context_limit: 100000,
        output_context_limit: 10000,
        rpm: 30,
        rpd: 1000,
        free: true,
        concurrency: 1,
      },
    },
    model_routes_map: { "route-model": ["route_only"] },
  };

  const { coordinator } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: limits });
  await coordinator.enqueueBatch([
    makeJob("route-j1", { policy_json: JSON.stringify({ allowed_models: ["route-model"] }) }),
    makeJob("route-j2", { policy_json: JSON.stringify({ allowed_models: ["route-model"] }) }),
  ]);

  const now = Date.now();
  const first = await coordinator.claimDispatchWindow(now, 25);
  assert.equal(first.jobs.length, 1);
  const second = await coordinator.claimDispatchWindow(now + 100, 25);
  assert.equal(second.claim_reason, "concurrency_limit");
  assert.equal(second.claim_diagnostics.rejections.route_concurrency, 1);
  assert.deepEqual(second.claim_diagnostics.routes.route_concurrency, { route_only: 1 });
});

test("claimDispatchWindow enforces provider-level TPM across routes sharing a provider", async () => {
  const limits = {
    providers: {
      shared_tpm_prov: {
        api_base: "https://example.com",
        accounts: [{ id: "acc1" }],
        tpm: 60000,
      },
    },
    routes_by_id: {
      tpm_r1: {
        route_id: "tpm_r1",
        model: "tm1",
        provider: "shared_tpm_prov",
        account_id: "acc1",
        input_context_limit: 100000,
        output_context_limit: 50000,
        rpm: 30,
        rpd: 1000,
        tpm: 100000,
        free: true,
      },
      tpm_r2: {
        route_id: "tpm_r2",
        model: "tm2",
        provider: "shared_tpm_prov",
        account_id: "acc1",
        input_context_limit: 100000,
        output_context_limit: 50000,
        rpm: 30,
        rpd: 1000,
        tpm: 100000,
        free: true,
      },
    },
    model_routes_map: {
      tm1: ["tpm_r1"],
      tm2: ["tpm_r2"],
    },
  };

  const { coordinator } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: limits });
  // Each job requests 60,000 tokens (exhausts the 1-minute provider TPM window of 60k)
  await coordinator.enqueueBatch([
    makeJob("j1", {
      input_token_estimate: 30000,
      max_output_token_estimate: 30000,
      policy_json: JSON.stringify({ allowed_models: ["tm1"] }),
    }),
    makeJob("j2", {
      input_token_estimate: 30000,
      max_output_token_estimate: 30000,
      policy_json: JSON.stringify({ allowed_models: ["tm2"] }),
    }),
  ]);

  const now = Date.now();
  // Within a 25-second dispatch window, only 1 job fits into the 60k TPM allowance
  const plan = await coordinator.claimDispatchWindow(now, 25);
  assert.equal(plan.jobs.length, 1);
});

test("calibration persists every completion until its window is full, then a 1-in-4 sample", () => {
  const { coordinator, sql } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: CEILING_CATALOG });
  const key = "gemma-ai-studio:google/gemma-4-31b-it:tags";
  const summary = () => {
    const row = [...sql.exec("SELECT recent_observed_summary FROM estimates WHERE key = ?", key)][0];
    return row ? JSON.parse(row.recent_observed_summary) : { r: [], o: [] };
  };
  const writes = [];
  const record = (i) => {
    const before = JSON.stringify(summary());
    coordinator._calibrateEstimate("gemma-ai-studio", "tags", 1300, Date.now(), {
      jobId: `job-${i}`,
      inputEstimate: 1000,
      observedInput: 1100,
      observedOutput: 200 + i,
    });
    writes.push(JSON.stringify(summary()) !== before);
  };
  for (let i = 0; i < 32; i += 1) record(i);
  assert.ok(writes.every(Boolean), "every completion is recorded while the window fills");
  writes.length = 0;
  for (let i = 32; i < 432; i += 1) record(i);
  const rate = writes.filter(Boolean).length / writes.length;
  assert.ok(rate > 0.15 && rate < 0.35, `sampled write rate ${rate}`);
});

test("retireConsumed deletes only completed jobs whose consumed result_key still matches", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch(["done", "stale", "queued", "failed"].map((id) => makeJob(id)));
  sql.exec("UPDATE jobs SET state = 'completed', result_key = 'results/' || id || '/r1.json' WHERE id IN ('done', 'stale')");
  sql.exec("UPDATE jobs SET state = 'failed' WHERE id = 'failed'");
  sql.exec("DELETE FROM job_models WHERE job_id IN ('done', 'stale', 'failed')");
  // 'stale' was superseded and re-completed after the consumer polled it: new result_key.
  sql.exec("UPDATE jobs SET result_key = 'results/stale/r2.json' WHERE id = 'stale'");
  const result = await coordinator.retireConsumed([
    { id: "done", result_key: "results/done/r1.json" },
    { id: "stale", result_key: "results/stale/r1.json" },
    { id: "queued", result_key: "results/queued/x.json" },
    { id: "failed", result_key: "results/failed/x.json" },
    { id: "unknown", result_key: "results/unknown/x.json" },
  ]);
  assert.deepEqual(result.retired, ["done"]);
  assert.deepEqual(result.ignored.sort(), ["failed", "queued", "stale", "unknown"]);
  const remaining = sql.exec("SELECT id, state FROM jobs ORDER BY id").map((row) => `${row.id}:${row.state}`);
  assert.deepEqual(remaining, ["failed:failed", "queued:queued", "stale:completed"]);
});

test("pollBatch reports the payload_key of a completed job for consumption-based retirement", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("p1"), makeJob("p2")]);
  sql.exec("UPDATE jobs SET state = 'completed', result_key = 'results/p1/r.json' WHERE id = 'p1'");
  const { statuses } = await coordinator.pollBatch(["p1", "p2"]);
  const byId = Object.fromEntries(statuses.map((s) => [s.id, s]));
  assert.equal(byId.p1.payload_key, "payloads/p1/request.json");
  assert.equal(byId.p2.payload_key, null, "only completed jobs expose their payload_key");
});

test("an empty claim counts jobs its own lease sweep just requeued", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_ACTIVE_BUNDLES: "1",
    MAX_BUNDLE_JOBS: "1",
    LEASE_DURATION_SECONDS: "1",
  });
  // A single-route model, so blocking that route leaves the reaped job nowhere to go.
  await coordinator.enqueueBatch([
    makeJob("j1", {
      policy_json: JSON.stringify({ allowed_models: ["mistral/mistral-small"], allow_paid: false }),
    }),
  ]);
  const start = Date.now();
  const stuck = await coordinator.claimDispatchWindow(start, 25);
  assert.equal(stuck.jobs.length, 1);
  // Nothing can take the job once it is reaped, so the reaping tick itself comes back empty.
  sql.exec("UPDATE routes SET blocked_until = ?", start + 3_600_000);
  const after = await coordinator.claimDispatchWindow(start + 2000, 25);
  assert.equal(after.bundle_id, null);
  assert.notEqual(after.claim_reason, "no_queued_work");
  assert.equal(after.claim_diagnostics.queued_jobs, 1);
});

test("a claim writes each route's ledger once however many jobs it admits there", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2"), makeJob("j3")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(plan.jobs.length, 3);
  const byRoute = new Map();
  for (const job of plan.jobs) byRoute.set(job.route_id, (byRoute.get(job.route_id) || 0) + 1);
  for (const [routeId, count] of byRoute) {
    const [route] = [...sql.exec(
      "SELECT rpm_count, provisional_reservation FROM routes WHERE route_id = ?",
      routeId
    )];
    const reserved = plan.jobs
      .filter((job) => job.route_id === routeId)
      .reduce((sum, job) => sum + job.token_reservation, 0);
    // The single flushed write carries every admitted job's reservation.
    assert.equal(route.rpm_count, count);
    assert.equal(route.provisional_reservation, reserved);
  }
});

test("completeBatch folds same-route successes into one settlement", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_CONCURRENT_ROUTE_LANES: "1" });
  await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(plan.jobs.length, 2);
  const routeId = plan.jobs[0].route_id;
  assert.equal(plan.jobs[1].route_id, routeId);
  sql.exec("UPDATE routes SET throttle_streak = 3 WHERE route_id = ?", routeId);
  await coordinator.completeBatch(
    plan.bundle_id,
    plan.execution_token,
    plan.jobs.map((job, i) => ({
      job_id: job.id,
      lease_token: job.lease_token,
      attempt_id: `a${i}`,
      planned_at: Date.now(),
      outcome: "success",
      provider_status_code: 200,
      observed_input_tokens: 400,
      observed_output_tokens: 100,
      result_key: `results/${job.id}.json`,
    }))
  );
  const [route] = [...sql.exec(
    "SELECT provisional_reservation, settled_usage, throttle_streak FROM routes WHERE route_id = ?",
    routeId
  )];
  assert.equal(route.provisional_reservation, 0);
  assert.equal(route.settled_usage, 1000);
  assert.equal(route.throttle_streak, 0);
});

test("a failure after a same-route success in one batch keeps its block", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_CONCURRENT_ROUTE_LANES: "1" });
  await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  const [first, second] = plan.jobs;
  assert.equal(first.route_id, second.route_id);
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: first.id,
      lease_token: first.lease_token,
      attempt_id: "a1",
      planned_at: Date.now(),
      outcome: "success",
      provider_status_code: 200,
      observed_input_tokens: 400,
      observed_output_tokens: 100,
      result_key: "results/j1.json",
    },
    {
      job_id: second.id,
      lease_token: second.lease_token,
      attempt_id: "a2",
      planned_at: Date.now(),
      outcome: "retryable_error",
      provider_status_code: 503,
    },
  ]);
  const [route] = [...sql.exec(
    "SELECT blocked_until, last_provider_status FROM routes WHERE route_id = ?",
    first.route_id
  )];
  // Per-job statement order: the success's backoff reset must not land after the 503's block.
  assert.ok(route.blocked_until > Date.now());
  assert.equal(route.last_provider_status, 503);
});

test("a legacy rowid bundles table is rebuilt WITHOUT ROWID, keeping only open bundles", () => {
  const { sql, storage } = createMockSqlStorage();
  sql.exec(`
    CREATE TABLE bundles (
      bundle_id TEXT PRIMARY KEY, execution_token TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('active','completed','expired')),
      lease_expires_at INTEGER NOT NULL, active_call_count INTEGER NOT NULL DEFAULT 0,
      dispatch_window_end INTEGER NOT NULL, created_at INTEGER NOT NULL
    );
    INSERT INTO bundles VALUES ('b-active','t','active',9,1,9,1);
    INSERT INTO bundles VALUES ('b-expired','t','expired',9,0,9,2);
    INSERT INTO bundles VALUES ('b-done','t','completed',9,0,9,3);
  `);
  const coordinator = new LLMSchedulerDO(
    { storage },
    withTestReservations({ DISPATCH_LIMITS_OVERRIDE: TEST_CATALOG })
  );
  coordinator._getSql();
  const [table] = [...sql.exec("SELECT sql FROM sqlite_master WHERE name = 'bundles'")];
  assert.match(table.sql, /WITHOUT ROWID/i);
  const ids = [...sql.exec("SELECT bundle_id FROM bundles ORDER BY bundle_id")].map((r) => r.bundle_id);
  assert.deepEqual(ids, ["b-active", "b-expired"]);
  const [index] = [...sql.exec(
    "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_bundles_state_created'"
  )];
  assert.ok(index);
});

// ---- Daily DO row thresholds -------------------------------------------------------------

function setRowsWrittenToday(sql, rows) {
  sql.exec("UPDATE scheduler SET rows_written_today = ? WHERE id = 1", rows);
}

test("the coordinator tallies the rows its RPCs write and persists them on the claim write", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2")]);
  const afterEnqueue = [...sql.exec("SELECT rows_written_today FROM scheduler WHERE id = 1")][0];
  await coordinator.claimDispatchWindow(Date.now(), 30);
  await coordinator.claimDispatchWindow(Date.now() + 61_000, 30);
  const afterClaims = [...sql.exec("SELECT rows_written_today FROM scheduler WHERE id = 1")][0];
  assert.ok(afterClaims.rows_written_today > afterEnqueue.rows_written_today);
  const stats = await coordinator.stats(Date.now());
  assert.ok(stats.row_budget.rows_written_today >= afterClaims.rows_written_today);
  assert.equal(stats.row_budget.enqueue_open, true);
});

test("past the enqueue threshold new work is refused but an exact replay still succeeds", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1")]);
  setRowsWrittenToday(sql, 90_000);
  const result = await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2")]);
  assert.deepEqual(result.accepted.map((row) => row.id), ["j1"]);
  assert.deepEqual(result.rejected, [{ id: "j2", reason: "daily_row_budget" }]);
  // Dispatch keeps going between the enqueue and claim thresholds.
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(plan.jobs.length, 1);
  // Scheduled cleanup waits for tomorrow.
  assert.deepEqual(await coordinator.purgePendingBatch(15), { jobs: [] });
});

test("the pending cap refuses new jobs once MAX_QUEUED_JOBS are waiting", async () => {
  const { coordinator } = makeCoordinator({ MAX_QUEUED_JOBS: "2" });
  const result = await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2"), makeJob("j3")]);
  assert.deepEqual(result.accepted.map((row) => row.id), ["j1", "j2"]);
  assert.deepEqual(result.rejected, [{ id: "j3", reason: "queue_full" }]);
});

test("past the claim threshold no lease is claimed and pending rows still get persisted", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1")]);
  setRowsWrittenToday(sql, 97_000);
  const read = () => [...sql.exec("SELECT last_claim_reason, rows_written_today FROM scheduler")][0];
  const before = read();
  // Rows written since the last flush (in production: completions, acks, retires).
  coordinator._rowsUnflushed = 40;
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(plan.bundle_id, null);
  assert.equal(plan.claim_reason, "daily_row_budget");
  const after = read();
  assert.equal(after.last_claim_reason, before.last_claim_reason);
  assert.ok(after.rows_written_today >= before.rows_written_today + 40);
  assert.equal([...sql.exec("SELECT state FROM jobs WHERE id = 'j1'")][0].state, "queued");
  // An idle braked tick (nothing pending but its own reads) writes nothing more.
  coordinator._rowsUnflushed = 0;
  const settled = read().rows_written_today;
  await coordinator.claimDispatchWindow(Date.now() + 61_000, 30);
  assert.ok(read().rows_written_today - settled <= 1);
});

test("past the optional threshold acks and retires are refused but completions still land", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1"), makeJob("j2")]);
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  setRowsWrittenToday(sql, 99_000);
  await coordinator.completeBatch(
    plan.bundle_id,
    plan.execution_token,
    plan.jobs.map((job, i) => ({
      job_id: job.id,
      lease_token: job.lease_token,
      attempt_id: `a${i}`,
      planned_at: Date.now(),
      outcome: "success",
      provider_status_code: 200,
      result_key: `results/${job.id}.json`,
    }))
  );
  const states = [...sql.exec("SELECT state FROM jobs ORDER BY id")].map((row) => row.state);
  assert.deepEqual(states, ["completed", "completed"]);
  assert.deepEqual(await coordinator.ackResults(["j1"]), { acked: [], ignored: ["j1"] });
  assert.deepEqual(
    await coordinator.retireConsumed([{ id: "j2", result_key: "results/j2.json" }]),
    { retired: [], ignored: ["j2"] }
  );
  assert.equal([...sql.exec("SELECT COUNT(*) AS n FROM jobs")][0].n, 2);
});

test("yesterday's row count does not close today", async () => {
  const { coordinator, sql } = makeCoordinator();
  sql.exec("UPDATE scheduler SET utc_day = '2000-01-01', rows_written_today = 99999 WHERE id = 1");
  const status = await coordinator.ingressStatus("unspecified");
  assert.equal(status.open, true);
  const result = await coordinator.enqueueBatch([makeJob("j1")]);
  assert.equal(result.accepted.length, 1);
  const row = [...sql.exec("SELECT utc_day, rows_written_today FROM scheduler")][0];
  assert.notEqual(row.utc_day, "2000-01-01");
  assert.ok(row.rows_written_today < 1000);
});

test("ingressStatus reports why ingress is closed without writing anything", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_QUEUED_JOBS: "1" });
  const open = await coordinator.ingressStatus("unspecified");
  assert.equal(open.open, true);
  assert.deepEqual(open.reasons, []);
  assert.equal(open.purpose, "unspecified");

  await coordinator.enqueueBatch([makeJob("j1")]);
  setRowsWrittenToday(sql, 95_000);
  const snapshot = () => [...sql.exec("SELECT * FROM scheduler")][0];
  const before = snapshot();
  const closed = await coordinator.ingressStatus("unspecified");
  assert.equal(closed.open, false);
  assert.deepEqual(closed.reasons, ["daily_row_budget", "queue_full"]);
  assert.equal(closed.row_budget.enqueue_open, false);
  assert.equal(closed.row_budget.claims_open, true);
  assert.deepEqual(snapshot(), before);

  const unknown = await coordinator.ingressStatus("not-a-lane");
  assert.ok(unknown.reasons.includes("purpose_not_registered"));
});

test("ingressStatus closes a lane whose daily write units are spent", async () => {
  const { coordinator } = makeCoordinator({
    INGRESS_PURPOSE_RESERVATIONS: JSON.stringify({
      unspecified: { reserved_write_units: 0, daily_write_units: 4, write_units_per_job: 4 },
    }),
  });
  assert.equal((await coordinator.ingressStatus("unspecified")).open, true);
  await coordinator.enqueueBatch([makeJob("j1")]);
  const status = await coordinator.ingressStatus("unspecified");
  assert.equal(status.open, false);
  assert.deepEqual(status.reasons, ["purpose_write_budget_exceeded"]);
  assert.equal(status.lane.write_units_available, 0);
});

test("a mid-day deploy seeds the new row counter from today's recorded work, never from zero", () => {
  const { sql, storage } = createMockSqlStorage();
  const env = withTestReservations({ DISPATCH_LIMITS_OVERRIDE: TEST_CATALOG });
  new LLMSchedulerDO({ storage }, env);
  // An already-deployed coordinator from before the counter existed, mid-way through its day.
  sql.exec("ALTER TABLE scheduler DROP COLUMN rows_written_today");
  sql.exec(
    `UPDATE scheduler SET ingress_write_units_today = 5000, lease_count_today = 1000,
       bundle_count_today = 400 WHERE id = 1`
  );
  const coordinator = new LLMSchedulerDO({ storage }, env);
  const { rows_written_today: seeded } = [...sql.exec("SELECT rows_written_today FROM scheduler")][0];
  // 2 x 5,000 ingress + 6 x 400 bundles + 24 x 1,000 leases, before cleanup and idle ticks.
  assert.ok(seeded >= 10_000 + 2_400 + 24_000, `seeded ${seeded}`);
  assert.ok(coordinator._readRowsWrittenToday() >= seeded);
  // A later construction (column present) leaves the running count alone.
  sql.exec("UPDATE scheduler SET rows_written_today = 5 WHERE id = 1");
  new LLMSchedulerDO({ storage }, env);
  assert.equal([...sql.exec("SELECT rows_written_today FROM scheduler")][0].rows_written_today, 5);
});
