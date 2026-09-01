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

test("purgePendingBatch's terminal-job lookup is an index seek, never a scan of every completed job", async () => {
  const { sql } = makeCoordinator();
  const plan = planOf(
    sql,
    "SELECT id, payload_key, result_key FROM jobs WHERE state IN ('completed','failed')" +
      " AND updated_at < ? ORDER BY updated_at ASC LIMIT ?",
    Date.now(),
    10
  );
  assert.match(plan, /SEARCH jobs USING INDEX idx_jobs_state_updated/);
  assert.doesNotMatch(plan, /SCAN/);
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
  assert.deepEqual(first, { bundlesDeleted: 10, attemptsDeleted: 10 });
  assert.equal([...sql.exec("SELECT COUNT(*) n FROM bundles")][0].n, 15);
  assert.equal([...sql.exec("SELECT COUNT(*) n FROM attempts")][0].n, 15);

  // Repeated ticks drain the backlog and then stop finding work.
  coordinator._pruneTerminalRecords(now);
  const third = coordinator._pruneTerminalRecords(now);
  assert.deepEqual(third, { bundlesDeleted: 5, attemptsDeleted: 5 });
  assert.deepEqual(coordinator._pruneTerminalRecords(now), { bundlesDeleted: 0, attemptsDeleted: 0 });
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

  assert.deepEqual(coordinator._pruneTerminalRecords(now), { bundlesDeleted: 0, attemptsDeleted: 0 });
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
  model_routes_map: { "deepseek/deepseek-v4-flash": ["paid-large", "free-small"] },
  routes_by_id: {
    "free-small": {
      provider: "opencode",
      upstream_model: "deepseek-v4-flash-free",
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
  const coordinator = new LLMSchedulerDO({ storage }, env);
  const paidJob = (id) => ({
    id,
    idempotency_key: `key-${id}`,
    request_digest: `digest-${id}`,
    policy_json: JSON.stringify({
      allowed_models: ["deepseek/deepseek-v4-flash"],
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
    {
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
    }
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

  const empty = await coordinator.stats(Date.now());
  assert.equal(empty.jobs.by_state.queued ?? 0, 0);
  assert.equal(empty.jobs.queued_without_model_index, 0);
  assert.equal(empty.jobs.oldest_queued_age_ms, null);
  assert.deepEqual(empty.queued_by_model, []);

  await coordinator.enqueueBatch([makeJob("j-a"), makeJob("j-b")]);
  const queued = await coordinator.stats(Date.now());
  assert.equal(queued.jobs.by_state.queued, 2);
  assert.equal(queued.jobs.queued_without_model_index, 0, "healthy jobs are indexed");
  assert.ok(queued.queued_by_model.length > 0, "queued work is visible per model");
  assert.equal(
    queued.queued_by_model.reduce((n, r) => Math.max(n, r.queued), 0),
    2
  );

  // Now reproduce the stranding shape: rows present, index gone. by_state still says "queued".
  sql.exec("DELETE FROM job_models");
  const stranded = await coordinator.stats(Date.now());
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
  const s = await coordinator.stats(now);
  const blocked = s.routes.blocked.find((r) => r.route_id === job.route_id);
  assert.ok(blocked, "a 402-blocked route must be listed");
  assert.ok(blocked.blocked_until > now);
  assert.equal(blocked.payment_required_streak, 1);
  assert.equal(blocked.last_provider_status, 402);

  // A route whose block has lapsed is healthy again and must drop off the list, or every route
  // ever throttled would accumulate here and bury the ones actually standing down.
  sql.exec("UPDATE routes SET blocked_until = ? WHERE route_id = ?", now - 1000, job.route_id);
  const after = await coordinator.stats(now);
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

  const settled = [...sql.exec(
    "SELECT provisional_reservation FROM routes WHERE route_id = ?", routeId
  )][0].provisional_reservation;
  const baseline = before.find((r) => r.route_id === routeId)?.provisional_reservation ?? 0;
  assert.equal(settled, baseline, "the dead lease's reservation must be released, not leaked");
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

  const s = await coordinator.stats(t + 1000);
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

test("authorizeRetry honors explicit retryAfterSeconds and updates route buffer", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_429_RETRIES: "3" });
  await coordinator.enqueueBatch([makeJob("j1")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 25);
  const job = plan.jobs[0];

  await coordinator.attemptStarted(job.id, job.lease_token, "attempt-1", now);
  // Upstream returns 429 with Retry-After: 5
  const auth = await coordinator.authorizeRetry(job.id, job.lease_token, "attempt-1", now, 5);
  assert.equal(auth.authorized, true);
  // 5s backoff with 50%-150% jitter gives 2.5s - 7.5s
  assert.ok(auth.retry_not_before >= now + 2000);
  assert.ok(auth.retry_not_before <= now + 8000);

  const routeRow = [
    ...sql.exec("SELECT buffer_seconds, throttle_streak FROM routes WHERE route_id=?", job.route_id),
  ][0];
  assert.ok(routeRow.buffer_seconds >= 5);
  assert.equal(routeRow.throttle_streak, 1);
});

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
