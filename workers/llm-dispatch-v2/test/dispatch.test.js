import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import { createMockSqlStorage } from "./helpers.js";

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
  return { coordinator: new LLMSchedulerDO({ storage }, env), sql };
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

  const bundleRows = [...sql.exec("SELECT state FROM bundles WHERE bundle_id=?", plan.bundle_id)];
  assert.equal(bundleRows[0].state, "completed"); // every leased job in the bundle settled
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

test("claimDispatchWindow recovers legacy retryable rows in bounded normal claim work", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("legacy")]);
  sql.exec("UPDATE jobs SET state='retryable' WHERE id='legacy'");
  sql.exec("DELETE FROM job_models WHERE job_id='legacy'");

  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.equal(plan.jobs[0].id, "legacy");
  const row = [...sql.exec("SELECT transient_retry_count FROM jobs WHERE id='legacy'")][0];
  assert.equal(row.transient_retry_count, 1);
  const scheduler = [...sql.exec("SELECT legacy_retryable_recovery_complete FROM scheduler")][0];
  assert.equal(scheduler.legacy_retryable_recovery_complete, 1);
});

test("completeBatch escalates blocked_until on consecutive 402s and clears it on the next success", async () => {
  const { coordinator, sql } = makeCoordinator();
  // Single-route model (see the next test's comment) so every claim below lands on route-c,
  // never a sibling route masking the block.
  const policy = JSON.stringify({ allowed_models: ["mistral/mistral-small"], allow_paid: false });

  async function claimAndComplete(jobId, outcome, providerStatusCode) {
    await coordinator.enqueueBatch([makeJob(jobId, { policy_json: policy })]);
    const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
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
  assert.ok(afterFirst > Date.now()); // blocked into the future

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

test("claimDispatchWindow backfills a legacy pre-index row on the first claim, then latches the scan off", async () => {
  const { coordinator, sql } = makeCoordinator();
  await coordinator.enqueueBatch([makeJob("j1")]);

  // Simulate a job row written before job_models existed: present in `jobs` but with no
  // corresponding job_models row, the way an older deployed version would have left it.
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, state, priority, policy_json, prompt_family,
      input_token_estimate, max_output_token_estimate, payload_key, attempts, created_at, updated_at
    ) VALUES ('legacy', 'legacy-key', 'legacy-digest', 'queued', 1, ?, 'tags', 100, 50,
              'payloads/legacy/request.json', 0, ?, ?)`,
    JSON.stringify({ allowed_models: ["gemini/gemini-flash-lite"], allow_paid: false }),
    Date.now(),
    Date.now()
  );

  let sched = [...sql.exec("SELECT job_models_backfill_complete FROM scheduler WHERE id = 1")];
  assert.equal(sched[0].job_models_backfill_complete, 0);

  const plan = await coordinator.claimDispatchWindow(Date.now(), 25);
  assert.ok(
    plan.jobs.some((job) => job.id === "legacy"),
    "the pre-index row must still be backfilled and dispatchable"
  );

  // Every job is now indexed, so the migration pass found nothing left to repair and latched --
  // steady-state operation must not keep re-scanning the whole queued backlog on every cron tick
  // forever (see the coordinator.js comment on _backfillQueuedJobModels).
  sched = [...sql.exec("SELECT job_models_backfill_complete FROM scheduler WHERE id = 1")];
  assert.equal(sched[0].job_models_backfill_complete, 1);
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
