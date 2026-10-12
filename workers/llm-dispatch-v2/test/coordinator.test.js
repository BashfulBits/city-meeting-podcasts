import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import {
  activeBundles, createMockSqlStorage, createRecordingSqlStorage, insertActiveBundle, withTestReservations,
} from "./helpers.js";

function makeCoordinator(env, { sql, storage } = createMockSqlStorage()) {
  return { coordinator: new LLMSchedulerDO({ storage }, withTestReservations(env)), sql, storage };
}

test("LLMSchedulerDO extends a base class (regression: real getByName()-style RPC requires this)", () => {
  // Regression test for the 2026-08-18 incident: every enqueueBatch/pollBatch/resolveUnknownBatch
  // call failed with "The receiving Durable Object does not support RPC, because its class was
  // not declared with `extends DurableObject`" from Phase 1's very first deploy onward, silently
  // -- the DO's own RPC-transport trace still reported outcome "ok" (the error surfaces only on
  // the calling Worker's side), and this suite calls `new LLMSchedulerDO(...)` directly, bypassing
  // the real binding/RPC layer entirely, so it never caught this. Can't exercise the real
  // "cloudflare:workers" DurableObject base class under plain Node (coordinator.js falls back to
  // a plain class there -- see its own comment), but this at least guards against a future
  // accidental removal of the `extends` clause reintroducing the exact same failure mode.
  const proto = Object.getPrototypeOf(LLMSchedulerDO.prototype);
  assert.notEqual(proto, Object.prototype, "LLMSchedulerDO must extend a base class, not plain Object");
});

test("LLMSchedulerDO initializes schema and scheduler row", () => {
  const { sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });

  const sched = [...sql.exec("SELECT * FROM scheduler WHERE id = 1")];
  assert.equal(sched.length, 1);
  assert.equal(sched[0].jobs_ingested_today, 0);
});

test("enqueueBatch admits new jobs and updates scheduler counter", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });

  const jobs = [
    {
      id: "j1",
      idempotency_key: "k1",
      request_digest: "d1",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/request.json",
      priority: 1,
    },
    {
      id: "j2",
      idempotency_key: "k2",
      request_digest: "d2",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 200,
      max_output_token_estimate: 100,
      payload_key: "payloads/j2/request.json",
      priority: 0,
    },
  ];

  const res = await coordinator.enqueueBatch(jobs);
  assert.deepEqual(res.accepted, [
    { id: "j1", submitted_id: "j1" },
    { id: "j2", submitted_id: "j2" },
  ]);
  assert.deepEqual(res.rejected, []);

  const sched = [...sql.exec("SELECT jobs_ingested_today FROM scheduler WHERE id = 1")];
  assert.equal(sched[0].jobs_ingested_today, 2);

  const rows = [...sql.exec("SELECT id, state, priority FROM jobs ORDER BY id")];
  assert.equal(rows.length, 2);
  assert.equal(rows[0].state, "queued");
  assert.equal(rows[0].priority, 1);
  assert.equal(rows[1].priority, 0);
});

test("queued counter heals direct SQLite state edits at the hourly recount", async () => {
  // The counter is maintained by explicit deltas on the RPC paths, not per-row triggers (each
  // trigger write was a billed DO row). A direct Data Studio edit bypasses those deltas, so the
  // hourly exact recount (recountQueuedJobs, called by the scheduled cleanup) corrects it.
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([
    {
      id: "direct-state",
      idempotency_key: "direct-state-key",
      request_digest: "direct-state-digest",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/direct-state/request.json",
    },
  ]);

  const queued = async () => (await coordinator.stats(Date.now())).jobs.by_state.queued;
  assert.equal(await queued(), 1);
  sql.exec("UPDATE jobs SET state = 'completed' WHERE id = 'direct-state'");
  await coordinator.recountQueuedJobs();
  assert.equal(await queued(), 0);
  sql.exec("UPDATE jobs SET state = 'queued' WHERE id = 'direct-state'");
  await coordinator.recountQueuedJobs();
  assert.equal(await queued(), 1);
  sql.exec("DELETE FROM jobs WHERE id = 'direct-state'");
  await coordinator.recountQueuedJobs();
  assert.equal(await queued(), 0);
});

test("queued counter tracks enqueue, claim, requeue and cancel without triggers", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  const truth = () => sql.exec("SELECT COUNT(*) AS n FROM jobs WHERE state = 'queued'")[0].n;
  const stored = async () => (await coordinator.stats(Date.now())).jobs.by_state.queued;
  const job = (id) => ({
    id,
    idempotency_key: `${id}-key`,
    request_digest: `${id}-digest`,
    policy_json: JSON.stringify({ allowed_models: ["gemini/gemini-3.1-flash-lite"], purpose: "topic-tags:tagger" }),
    prompt_family: "tags",
    input_token_estimate: 100,
    max_output_token_estimate: 50,
    payload_key: `payloads/${id}/request.json`,
  });
  await coordinator.enqueueBatch([job("a"), job("b"), job("c")]);
  assert.equal(await stored(), truth());
  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.ok(plan.jobs.length > 0);
  assert.equal(await stored(), truth());
  // Requeue one leased job through completeBatch.
  const leased = plan.jobs[0];
  await coordinator.completeBatch(plan.bundle_id, plan.execution_token, [
    {
      job_id: leased.id,
      lease_token: leased.lease_token,
      attempt_id: "att-1",
      planned_at: leased.not_before_at,
      outcome: "deferred_late",
    },
  ]);
  assert.equal(await stored(), truth());
  await coordinator.cancelBatch(["c"]);
  assert.equal(await stored(), truth());
  assert.equal(
    sql.exec("SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'trg_jobs_queued_count_%'")[0].n,
    0
  );
});

test("enqueueBatch indexes fitting canonical models without inventing routes or model_routing", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([
    {
      id: "multi",
      idempotency_key: "multi-key",
      request_digest: "multi-digest",
      policy_json: JSON.stringify({
        allowed_models: [
          "gemini/gemini-3.1-flash-lite",
          "mistral/mistral-small-2603",
          "future/provider-model",
        ],
        allow_paid: false,
      }),
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/multi/request.json",
    },
  ]);

  const models = [...sql.exec("SELECT model FROM job_models WHERE job_id='multi' ORDER BY model")];
  assert.deepEqual(models.map((row) => row.model), [
    "gemini/gemini-3.1-flash-lite",
  ]);
});

test("claim uses an explicit peer route instead of the tied model that discovered the job", async () => {
  const CATALOG = {
    model_aliases: {},
    model_routes_map: {
      gemini: ["gemini-route"],
      llama: ["llama-route"],
    },
    routes_by_id: {
      "gemini-route": {
        model: "gemini",
        free: true,
        rpm: 20,
        rpd: 1000,
        tpm: 100000,
        input_context_limit: 10000,
        output_context_limit: 10000,
      },
      "llama-route": {
        model: "llama",
        free: true,
        rpm: 20,
        rpd: 1000,
        tpm: 100000,
        input_context_limit: 10000,
        output_context_limit: 10000,
      },
    },
  };
  const { coordinator } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
  });
  await coordinator.enqueueBatch([
    {
      id: "peer-before-discovery-model",
      idempotency_key: "peer-before-discovery-model-key",
      request_digest: "peer-before-discovery-model-digest",
      policy_json: JSON.stringify({ allowed_models: ["llama", "gemini"], allow_paid: false }),
      prompt_family: "agenda",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/peer-before-discovery-model/request.json",
    },
  ]);

  const plan = await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(plan.jobs.length, 1);
  assert.equal(plan.jobs[0].route_id, "llama-route");
});

test("enqueueBatch omits a model whose every configured route is too small for the job's own estimate", async () => {
  // route-a fits this job; route-b (a different model) is structurally too small for it no matter
  // its live capacity. Indexing the job under model-b anyway would let it sit unclaimed forever at
  // the head of model-b's bounded per-claim candidate window, starving smaller model-b jobs queued
  // behind it -- see the coordinator.js comment on _modelsForQueuedJob.
  const CATALOG = {
    model_aliases: {},
    model_routes_map: { "model-a": ["route-a"], "model-b": ["route-b"] },
    routes_by_id: {
      "route-a": { free: true, input_context_limit: 100000, output_context_limit: 100000 },
      "route-b": { free: true, input_context_limit: 100, output_context_limit: 100 },
    },
  };
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
  });
  await coordinator.enqueueBatch([
    {
      id: "oversized-for-b",
      idempotency_key: "oversized-for-b-key",
      request_digest: "oversized-for-b-digest",
      policy_json: JSON.stringify({ allowed_models: ["model-a", "model-b"], allow_paid: false }),
      prompt_family: "tags",
      input_token_estimate: 5000,
      max_output_token_estimate: 500,
      payload_key: "payloads/oversized-for-b/request.json",
    },
  ]);

  const models = [
    ...sql.exec("SELECT model FROM job_models WHERE job_id='oversized-for-b' ORDER BY model"),
  ];
  assert.deepEqual(models.map((row) => row.model), ["model-a"]);
});

test("enqueueBatch omits a route when input plus output exceeds its context window", async () => {
  const CATALOG = {
    model_aliases: {},
    model_routes_map: { "model-a": ["route-a"], "model-b": ["route-b"] },
    routes_by_id: {
      "route-a": { free: true, input_context_limit: 1000, output_context_limit: 500 },
      "route-b": { free: true, input_context_limit: 2200, output_context_limit: 500 },
    },
  };
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
  });
  await coordinator.enqueueBatch([
    {
      id: "combined-context-overflow",
      idempotency_key: "combined-context-overflow-key",
      request_digest: "combined-context-overflow-digest",
      policy_json: JSON.stringify({ allowed_models: ["model-a", "model-b"], allow_paid: false }),
      prompt_family: "tags",
      input_token_estimate: 1800,
      max_output_token_estimate: 300,
      payload_key: "payloads/combined-context-overflow/request.json",
    },
  ]);

  const models = [
    ...sql.exec("SELECT model FROM job_models WHERE job_id='combined-context-overflow'"),
  ];
  assert.deepEqual(models.map((row) => row.model), ["model-b"]);
});

test("enqueueBatch sends a job that fits no configured route under any allowed model to __unroutable__", async () => {
  const CATALOG = {
    model_aliases: {},
    model_routes_map: { "model-a": ["route-a"] },
    routes_by_id: {
      "route-a": { free: true, input_context_limit: 100, output_context_limit: 100 },
    },
  };
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
  });
  await coordinator.enqueueBatch([
    {
      id: "too-big-everywhere",
      idempotency_key: "too-big-everywhere-key",
      request_digest: "too-big-everywhere-digest",
      policy_json: JSON.stringify({ allowed_models: ["model-a"], allow_paid: false }),
      prompt_family: "tags",
      input_token_estimate: 5000,
      max_output_token_estimate: 500,
      payload_key: "payloads/too-big-everywhere/request.json",
    },
  ]);

  const models = [
    ...sql.exec("SELECT model FROM job_models WHERE job_id='too-big-everywhere'"),
  ];
  assert.deepEqual(models.map((row) => row.model), ["__unroutable__"]);
});

test("enqueueBatch handles idempotent replays and supersedes a stale queued row", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });

  await coordinator.enqueueBatch([
    {
      id: "j1",
      idempotency_key: "k1",
      request_digest: "d1",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/request.json",
    },
  ]);

  // Replay with identical digest: accepted with the ORIGINAL canonical id, tagged with the
  // caller's own (different, freshly-generated) submitted id so the caller can still match
  // this response back to its own request.
  const replay = await coordinator.enqueueBatch([
    {
      id: "different-id",
      idempotency_key: "k1",
      request_digest: "d1",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/different/request.json",
    },
  ]);
  assert.deepEqual(replay.accepted, [{ id: "j1", submitted_id: "different-id" }]);
  assert.deepEqual(replay.rejected, []);

  // Same key, different digest, and the existing row is still 'queued': supersede rather than
  // reject -- see "a stale queued row is superseded" below for why this must not be a permanent
  // rejection.
  const superseding = await coordinator.enqueueBatch([
    {
      id: "conflict-id",
      idempotency_key: "k1",
      request_digest: "different-digest",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/conflict/request.json",
    },
  ]);
  assert.deepEqual(superseding.rejected, []);
  assert.deepEqual(superseding.accepted, [
    { id: "j1", submitted_id: "conflict-id", superseded: true },
  ]);
});

test("enqueueBatch rejects an idempotency conflict against a genuinely in-flight attempt", async () => {
  // The one case supersede cannot safely cover: a lease already references the old payload, and
  // overwriting the row out from under a call that may still be running risks a result racing
  // back against content that no longer matches what was sent.
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([
    {
      id: "j1",
      idempotency_key: "k1",
      request_digest: "d1",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/request.json",
    },
  ]);
  sql.exec("UPDATE jobs SET state = 'leased' WHERE id = 'j1'");

  const conflict = await coordinator.enqueueBatch([
    {
      id: "conflict-id",
      idempotency_key: "k1",
      request_digest: "different-digest",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/conflict/request.json",
    },
  ]);
  assert.deepEqual(conflict.accepted, []);
  assert.deepEqual(conflict.rejected, [{ id: "conflict-id", reason: "idempotency_conflict" }]);

  // And the row itself must be untouched -- not overwritten, not requeued.
  const row = [...sql.exec("SELECT state, request_digest, payload_key FROM jobs WHERE id = 'j1'")][0];
  assert.equal(row.state, "leased");
  assert.equal(row.request_digest, "d1");
  assert.equal(row.payload_key, "payloads/j1/request.json");
});

test("enqueueBatch admits an idempotent replay even after the daily cap is reached", async () => {
  // A replay must never be penalized by admission capacity it doesn't consume -- see the
  // idempotency-before-cap-check ordering fix in coordinator.js.
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "1" });

  const first = await coordinator.enqueueBatch([
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
  assert.deepEqual(first.accepted, [{ id: "j1", submitted_id: "j1" }]);

  // Cap is now exhausted (MAX_JOBS_PER_UTC_DAY=1). A brand-new job is correctly rejected...
  const newJob = await coordinator.enqueueBatch([
    {
      id: "j2",
      idempotency_key: "k2",
      request_digest: "d2",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j2/request.json",
    },
  ]);
  assert.deepEqual(newJob.rejected, [{ id: "j2", reason: "daily_cap_exceeded" }]);

  // ...but a retry of the ALREADY-accepted j1 (same idempotency_key/request_digest, a fresh
  // locally-generated id as a real retry would send) must still succeed.
  const retry = await coordinator.enqueueBatch([
    {
      id: "j1-retry-attempt-id",
      idempotency_key: "k1",
      request_digest: "d1",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1-retry/request.json",
    },
  ]);
  assert.deepEqual(retry.accepted, [{ id: "j1", submitted_id: "j1-retry-attempt-id" }]);
  assert.deepEqual(retry.rejected, []);
});

test("enqueueBatch enforces daily cap with partial admission", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "2" });

  const res = await coordinator.enqueueBatch([
    {
      id: "j1",
      idempotency_key: "k1",
      request_digest: "d1",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/request.json",
    },
    {
      id: "j2",
      idempotency_key: "k2",
      request_digest: "d2",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j2/request.json",
    },
    {
      id: "j3",
      idempotency_key: "k3",
      request_digest: "d3",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j3/request.json",
    },
  ]);

  assert.deepEqual(res.accepted, [
    { id: "j1", submitted_id: "j1" },
    { id: "j2", submitted_id: "j2" },
  ]);
  assert.deepEqual(res.rejected, [{ id: "j3", reason: "daily_cap_exceeded" }]);
});

test("ingress reservations preserve write capacity for another purpose", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY: "8",
    INGRESS_PURPOSE_RESERVATIONS: JSON.stringify({
      "topic-tags": { reserved_write_units: 3 },
      "chapter-agenda": { daily_write_units: 10 },
    }),
  });
  const job = (id, purpose) => ({
    id,
    idempotency_key: `${id}-key`,
    request_digest: `${id}-digest`,
    policy_json: JSON.stringify({ allowed_models: ["unknown-model"], purpose }),
    prompt_family: "test",
    input_token_estimate: 1,
    max_output_token_estimate: 1,
    payload_key: `payloads/${id}/request.json`,
  });

  const agenda = await coordinator.enqueueBatch([job("agenda", "chapter-agenda")]);
  assert.deepEqual(agenda.accepted, [{ id: "agenda", submitted_id: "agenda" }]);
  const blocked = await coordinator.enqueueBatch([job("agenda-2", "chapter-agenda")]);
  assert.deepEqual(blocked.rejected, [
    { id: "agenda-2", reason: "ingress_write_budget_reserved" },
  ]);
  const tags = await coordinator.enqueueBatch([job("tags", "topic-tags")]);
  assert.deepEqual(tags.accepted, [{ id: "tags", submitted_id: "tags" }]);
  assert.equal([...sql.exec("SELECT ingress_write_units_today FROM scheduler")][0].ingress_write_units_today, 8);
});

test("schemaRetry obeys the same ingress write budget as enqueueBatch", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY: "8",
  });
  await coordinator.enqueueBatch([{
    id: "source", idempotency_key: "source-key", request_digest: "source-digest",
    policy_json: JSON.stringify({ purpose: "topic-tags" }), prompt_family: "tags",
    input_token_estimate: 1, max_output_token_estimate: 1,
    payload_key: "payloads/source/request.json",
  }]);
  sql.exec("UPDATE jobs SET state = 'completed' WHERE id = 'source'");
  sql.exec("UPDATE scheduler SET ingress_write_units_today = 8 WHERE id = 1");

  assert.deepEqual(await coordinator.schemaRetry("source", {
    corrected_payload_key: "payloads/retry/request.json",
    corrected_request_digest: "retry-digest",
    corrected_input_token_estimate: 1,
  }), { status: "ingress_write_budget_reserved" });
  assert.equal([...sql.exec("SELECT COUNT(*) AS n FROM jobs")][0].n, 1);
});

test("schemaRetry charges ingress for the backup-model index rows its own clone activates", async () => {
  // A schema-correction clone's schema_retry_count becomes 1, which backupModelsActive()
  // (routes.js) treats as an immediate trigger -- so _indexQueuedJobModels indexes BOTH the
  // primary and backup models for the new row. The write-unit charge computed for admission must
  // reflect that same count, not the source's pre-correction schema_retry_count (0), which would
  // only index the primary and undercount the charge.
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: LANE_WITH_BACKUP_THRESHOLD,
    DISPATCH_LIMITS_OVERRIDE: {
      model_routes_map: { "primary/model": ["p"], "backup/model": ["b"] },
      routes_by_id: {
        p: { free: true, input_context_limit: 10000, output_context_limit: 1000 },
        b: { free: true, input_context_limit: 10000, output_context_limit: 1000 },
      },
    },
  });
  await coordinator.enqueueBatch([backupPolicyJob("source", 12)]);
  sql.exec("UPDATE jobs SET state = 'completed' WHERE id = 'source'");
  const before = [...sql.exec("SELECT ingress_write_units_today FROM scheduler WHERE id = 1")][0]
    .ingress_write_units_today;

  const result = await coordinator.schemaRetry("source", {
    corrected_payload_key: "payloads/retry/request.json",
    corrected_request_digest: "retry-digest",
    corrected_input_token_estimate: 1,
  });
  assert.equal(result.status, "accepted");

  const cloneModels = [...sql.exec(
    "SELECT model FROM job_models WHERE job_id = ? ORDER BY model",
    result.id
  )].map((row) => row.model);
  assert.deepEqual(cloneModels, ["backup/model", "primary/model"]);

  const after = [...sql.exec("SELECT ingress_write_units_today FROM scheduler WHERE id = 1")][0]
    .ingress_write_units_today;
  // 3 (job row + purpose ledger + scheduler counter) + 2 model-index rows = 5, not 4.
  assert.equal(after - before, 5);
});

test("enqueueBatch rolls the whole batch back if a mid-batch exception is thrown", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });

  // A missing required column (prompt_family is NOT NULL) throws partway through the batch.
  await assert.rejects(() =>
    coordinator.enqueueBatch([
      {
        id: "j1",
        idempotency_key: "k1",
        request_digest: "d1",
        prompt_family: "tags",
        input_token_estimate: 100,
        max_output_token_estimate: 50,
        payload_key: "payloads/j1/request.json",
      },
      {
        id: "j2",
        idempotency_key: "k2",
        request_digest: "d2",
        prompt_family: null, // violates NOT NULL -> throws mid-batch
        input_token_estimate: 100,
        max_output_token_estimate: 50,
        payload_key: "payloads/j2/request.json",
      },
    ])
  );

  // j1, inserted before the throw, must not remain committed, and the ingested counter must
  // not have advanced -- the whole batch is one transaction.
  const rows = [...sql.exec("SELECT id FROM jobs")];
  assert.deepEqual(rows, []);
  const sched = [...sql.exec("SELECT jobs_ingested_today FROM scheduler WHERE id = 1")];
  assert.equal(sched[0].jobs_ingested_today, 0);
});

test("enqueueBatch rolls the UTC day forward and resets the ingest counter", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "1" });

  sql.exec(
    "UPDATE scheduler SET utc_day = '2000-01-01', jobs_ingested_today = 1, bundle_count_today = 5 WHERE id = 1"
  );

  const res = await coordinator.enqueueBatch([
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

  assert.deepEqual(res.accepted, [{ id: "j1", submitted_id: "j1" }]);
  const sched = [...sql.exec("SELECT utc_day, jobs_ingested_today FROM scheduler WHERE id = 1")];
  assert.notEqual(sched[0].utc_day, "2000-01-01");
  assert.equal(sched[0].jobs_ingested_today, 1);
});

test("pollBatch returns statuses and omits absent IDs", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });

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

  // Mark j1 as completed
  sql.exec(
    "UPDATE jobs SET state = 'completed', result_key = 'results/j1/lt1.json' WHERE id = 'j1'"
  );

  const pollRes = await coordinator.pollBatch(["j1", "nonexistent"]);
  assert.equal(pollRes.statuses.length, 1);
  assert.equal(pollRes.statuses[0].id, "j1");
  assert.equal(pollRes.statuses[0].state, "completed");
  assert.equal(pollRes.statuses[0].result_key, "results/j1/lt1.json");
});

test("pollBatch reports the model the completed route actually served, not just the primary", async () => {
  // A completed job's lease_route_id names the PHYSICAL route it ran on -- which, once a backup
  // model activates, need not be the job's own allowed_models[0]. Without pollBatch resolving and
  // returning that route's model, the client would record every completion as the primary model
  // (JobHandle.model, guessed at enqueue time), silently misattributing a backup's result.
  const CATALOG = {
    model_aliases: {},
    model_routes_map: {
      "primary/model": ["primary-route"],
      "backup/model": ["backup-route"],
    },
    routes_by_id: {
      "primary-route": { free: true, rpm: 20, rpd: 1000, tpm: 100000, input_context_limit: 10000, output_context_limit: 10000 },
      "backup-route": { free: true, rpm: 20, rpd: 1000, tpm: 100000, input_context_limit: 10000, output_context_limit: 10000 },
    },
  };
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
  });
  await coordinator.enqueueBatch([{
    id: "j1", idempotency_key: "k1", request_digest: "d1",
    policy_json: JSON.stringify({ allowed_models: ["primary/model"] }),
    prompt_family: "tags", input_token_estimate: 100, max_output_token_estimate: 50,
    payload_key: "payloads/j1/request.json",
  }]);
  // Simulate the job having completed on the backup route (as if it escalated there after
  // enough failed attempts): lease_route_id names the backup, not the primary.
  sql.exec(
    "UPDATE jobs SET state = 'completed', result_key = 'results/j1/lt1.json', lease_route_id = 'backup-route' WHERE id = 'j1'"
  );

  const pollRes = await coordinator.pollBatch(["j1"]);
  assert.equal(pollRes.statuses[0].model, "backup/model");
});

test("terminalFeed is keyset paginated and cancelBatch removes queued work from dispatch", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([
    {
      id: "complete-job",
      idempotency_key: "complete-key",
      request_digest: "complete-digest",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 1,
      max_output_token_estimate: 1,
      payload_key: "payloads/complete/request.json",
    },
    {
      id: "cancel-job",
      idempotency_key: "cancel-key",
      request_digest: "cancel-digest",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 1,
      max_output_token_estimate: 1,
      payload_key: "payloads/cancel/request.json",
    },
  ]);
  sql.exec(
    "UPDATE jobs SET state='completed', result_key='results/complete.json', updated_at=100 WHERE id='complete-job'"
  );

  const terminal = await coordinator.terminalFeed({ updated_at: 0, id: "" }, 1);
  assert.deepEqual(terminal.terminals, [
    {
      id: "complete-job",
      state: "completed",
      result_key: "results/complete.json",
      updated_at: 100,
      terminal_reason: null,
      terminal_catalog_digest: null,
    },
  ]);
  assert.deepEqual(await coordinator.terminalFeed(terminal.cursor, 1), {
    terminals: [],
    cursor: terminal.cursor,
  });

  assert.deepEqual(await coordinator.cancelBatch(["cancel-job", "missing-job"]), {
    cancelled: ["cancel-job"],
    in_flight: [],
    not_found: ["missing-job"],
  });
  assert.equal([...sql.exec("SELECT state FROM jobs WHERE id='cancel-job'")][0].state, "purge_pending");
  assert.equal([...sql.exec("SELECT COUNT(*) AS n FROM job_models WHERE job_id='cancel-job'")][0].n, 0);
});

test("pollBatch chunks IDs past Cloudflare's 100-bound-parameter limit", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "10000" });

  const jobs = Array.from({ length: 250 }, (_, i) => ({
    id: `job-${i}`,
    idempotency_key: `key-${i}`,
    request_digest: `digest-${i}`,
    prompt_family: "tags",
    input_token_estimate: 10,
    max_output_token_estimate: 10,
    payload_key: `payloads/job-${i}/request.json`,
  }));
  await coordinator.enqueueBatch(jobs);

  const ids = jobs.map((j) => j.id);
  const pollRes = await coordinator.pollBatch(ids);
  assert.equal(pollRes.statuses.length, 250);
});

test("resolveUnknownBatch reports every id not_found: the DO keeps no per-attempt journal", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  const ids = Array.from({ length: 150 }, (_, i) => `attempt-${i}`);
  assert.deepEqual(await coordinator.resolveUnknownBatch(ids), { resolved: [], not_found: ids });
});

test("enqueueBatch supersedes a completed row whose old payload was wrong, and re-queues it", async () => {
  // THE 2026-08-25 production case: a job completed (or failed) under a payload built before
  // 2c3b2ab stopped leaking policy-only fields into the literal provider request body -- which
  // every provider correctly rejected. The corrected resubmission carries the same
  // idempotency_key (same recipe) and a different request_digest (fixed payload). Rejecting that
  // permanently strands a job that never actually succeeded; superseding lets it run again.
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([
    {
      id: "j1",
      idempotency_key: "k1",
      request_digest: "d1-leaked-fields",
      policy_json: "{}",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/v1.json",
    },
  ]);
  // Nonzero on purpose: this is what a real failed row looks like (it exhausted its retries),
  // and it is the value supersede must actually reset, not one that was already 0.
  sql.exec(
    "UPDATE jobs SET state = 'failed', result_key = NULL, attempts = 2, transient_retry_count = 1,\n" +
      "                 updated_at = ? WHERE id = 'j1'",
    Date.now()
  );

  const result = await coordinator.enqueueBatch([
    {
      id: "retry-id",
      idempotency_key: "k1",
      request_digest: "d1-corrected",
      policy_json: JSON.stringify({ allowed_models: ["gemini/gemini-flash-lite"], allow_paid: false }),
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/v2.json",
    },
  ]);
  assert.deepEqual(result.rejected, []);
  assert.deepEqual(result.accepted, [{ id: "j1", submitted_id: "retry-id", superseded: true }]);

  const row = [...sql.exec(
    "SELECT state, request_digest, payload_key, attempts, transient_retry_count FROM jobs WHERE id = 'j1'"
  )][0];
  assert.equal(row.state, "queued", "a superseded row must be runnable again, not stuck failed");
  assert.equal(row.request_digest, "d1-corrected");
  assert.equal(row.payload_key, "payloads/j1/v2.json");
  assert.equal(row.attempts, 0, "a corrected payload has never actually been attempted");
  assert.equal(row.transient_retry_count, 0);
});

test("supersede does not consume today's admission cap", async () => {
  // Same rationale as an idempotent replay: this replaces a row that already existed, so it is
  // not new admission. Getting this wrong would let a flood of resubmissions after a payload
  // change starve out every genuinely new job for the rest of the day.
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "1" });
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
  // Cap (1) is now exhausted by that one insert.
  const result = await coordinator.enqueueBatch([
    {
      id: "retry-id",
      idempotency_key: "k1",
      request_digest: "d2",
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/request-v2.json",
    },
  ]);
  assert.deepEqual(result.accepted, [{ id: "j1", submitted_id: "retry-id", superseded: true }]);
  assert.deepEqual(result.rejected, []);

  const sched = [...sql.exec("SELECT jobs_ingested_today FROM scheduler WHERE id = 1")][0];
  assert.equal(sched.jobs_ingested_today, 1, "the supersede must not have incremented the cap");
});

test("supersede rebuilds the job_models index for the new payload's allowed models", async () => {
  // The allowed-model set is itself part of policy_json, so a corrected payload can route
  // differently than the one it replaces. A stale index would leave the row invisible to
  // claimDispatchWindow under its real, current model, or visible under a model it can no
  // longer run on.
  const CATALOG = {
    model_aliases: {},
    model_routes_map: {
      "gemini/flash-lite": ["gem-a"],
      "mistral/mistral-small": ["mis-a"],
    },
    providers: {},
    routes_by_id: {
      "gem-a": { input_context_limit: 100000, output_context_limit: 100000, free: true },
      "mis-a": { input_context_limit: 100000, output_context_limit: 100000, free: true },
    },
  };
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
  });
  await coordinator.enqueueBatch([
    {
      id: "j1",
      idempotency_key: "k1",
      request_digest: "d1",
      policy_json: JSON.stringify({ allowed_models: ["gemini/flash-lite"], allow_paid: false }),
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/v1.json",
    },
  ]);
  assert.deepEqual(
    [...sql.exec("SELECT model FROM job_models WHERE job_id = 'j1'")].map((r) => r.model),
    ["gemini/flash-lite"]
  );

  await coordinator.enqueueBatch([
    {
      id: "retry-id",
      idempotency_key: "k1",
      request_digest: "d2",
      policy_json: JSON.stringify({ allowed_models: ["mistral/mistral-small"], allow_paid: false }),
      prompt_family: "tags",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      payload_key: "payloads/j1/v2.json",
    },
  ]);
  const models = [...sql.exec("SELECT model FROM job_models WHERE job_id = 'j1'")].map((r) => r.model);
  assert.deepEqual(models, ["mistral/mistral-small"], "the index must reflect the new payload only");
});

test("_ensureMigratedJobModels persists a durable marker and does not clear active buffers on reboot", async () => {
  const { coordinator, sql, storage } = makeCoordinator();
  // Migration ran on init:
  const sched = [...sql.exec("SELECT mistral_latest_migrated FROM scheduler WHERE id = 1")][0];
  assert.equal(sched.mistral_latest_migrated, 1);

  // Set an active timestamped buffer on a route:
  const now = Date.now();
  sql.exec(
    "INSERT OR REPLACE INTO routes (route_id, buffer_seconds, buffer_updated_at) VALUES ('route-test', 30, ?)",
    now
  );

  // Simulate a new DO instance booting against the same storage:
  const rebooted = new LLMSchedulerDO({ storage }, withTestReservations({}));
  const routeRow = [...sql.exec("SELECT buffer_seconds, buffer_updated_at FROM routes WHERE route_id = 'route-test'")][0];
  assert.equal(routeRow.buffer_seconds, 30, "active buffer must not be cleared on DO reboot");
  assert.equal(routeRow.buffer_updated_at, now);
});

test("authorizeRetry throttles only the specific route_id, keeping providers isolated", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const bundleDeadline = now + 60_000;
  insertActiveBundle(sql, "b1", "tok", bundleDeadline, bundleDeadline, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-airforce', 'idem-1', 'digest-1', '{}', 'leased', 'b1', 'airforce_mistral_medium_3_5_primary',
      'ltok', 'tags', 100, 50, 'payloads/j-airforce/request.json', ?, ?
    )`,
    now,
    now
  );
  sql.exec(
    "INSERT INTO routes (route_id, buffer_seconds, throttle_streak) VALUES ('mistral_medium_latest_primary', 0, 0)"
  );

  const auth = await coordinator.authorizeRetry("j-airforce", "ltok", "att-1", now, 10);
  assert.equal(auth.authorized, true);

  const airforceRow = [...sql.exec("SELECT throttle_streak, buffer_seconds FROM routes WHERE route_id = 'airforce_mistral_medium_3_5_primary'")][0];
  assert.equal(airforceRow.throttle_streak, 1);
  assert.equal(airforceRow.buffer_seconds, 10);

  const mistralRow = [...sql.exec("SELECT throttle_streak, buffer_seconds FROM routes WHERE route_id = 'mistral_medium_latest_primary'")][0];
  assert.equal(mistralRow.throttle_streak, 0, "Mistral route must remain unthrottled when Airforce 429s");
  assert.equal(mistralRow.buffer_seconds, 0);
});

// --- Ingress purpose registry (llm_lanes) -----------------------------------------------------
// Before this gate, a purpose absent from the reservation map fell through to unreserved shared
// headroom. That is how the deployed map came to reserve capacity under "topic-tags" and "moments"
// while the client sent "topic-tags:tagger", "topic-tags:prelabeler", "r6-moments" and "r6-judge":
// 10,000 of 30,000 daily write units were withheld from every real lane on behalf of two keys no
// job could ever match, and nothing failed. The reservation map is now compiled from
// config/site_config.yml's `llm_lanes` block, and an unregistered purpose is rejected outright.

const REGISTERED_ONLY = JSON.stringify({
  "topic-tags:tagger": { reserved_write_units: 0, daily_write_units: 10000 },
});

function purposeJob(id, purpose) {
  return {
    id,
    idempotency_key: `k-${id}`,
    request_digest: `d-${id}`,
    policy_json: JSON.stringify({ purpose }),
    prompt_family: "tags",
    input_token_estimate: 100,
    max_output_token_estimate: 50,
    payload_key: `payloads/${id}/request.json`,
  };
}

test("enqueueBatch rejects a purpose with no registered lane", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: REGISTERED_ONLY,
  });

  const res = await coordinator.enqueueBatch([
    purposeJob("j-known", "topic-tags:tagger"),
    purposeJob("j-new-verb", "topic-tags:summarizer"),
  ]);

  const rejected = res.rejected.find((entry) => entry.id === "j-new-verb");
  assert.ok(rejected, "an unregistered purpose must be rejected, not silently admitted");
  assert.equal(rejected.reason, "purpose_not_registered");
  assert.equal(rejected.purpose, "topic-tags:summarizer");

  // The registered sibling in the same batch still lands: one unregistered purpose must not
  // fail the whole submission.
  const rows = [...sql.exec("SELECT id FROM jobs ORDER BY id")];
  assert.deepEqual(rows.map((row) => row.id), ["j-known"]);
});

test("a sub-purpose does not inherit its prefix's lane", async () => {
  // "topic-tags:prelabeler" is a different verb from "topic-tags:tagger" with its own budget, so
  // registering one must not admit the other. This is the exact shape of the original bug.
  const { coordinator } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: REGISTERED_ONLY,
  });

  const res = await coordinator.enqueueBatch([
    purposeJob("j-prelabel", "topic-tags:prelabeler"),
  ]);

  assert.equal(res.rejected[0]?.reason, "purpose_not_registered");
});

test("the compiled reservation map is used when no env override is set", async () => {
  // Guards the wiring itself: with INGRESS_PURPOSE_RESERVATIONS unset the coordinator must fall
  // back to src/ingress_reservations.json (compiled from llm_lanes), NOT to an empty map. An
  // empty map reads as "no lane has a reservation", which is the degraded state this replaced.
  // Built raw, NOT through makeCoordinator(): that helper injects a test reservation map, which
  // is exactly the wiring this test needs to bypass.
  const { sql, storage } = createMockSqlStorage();
  // Give it the production ingress budget: the compiled map reserves 13,000 units across the
  // other lanes, and admission subtracts those from the headroom this job may use, so a token
  // budget would reject even a correctly-registered purpose for the wrong reason.
  const coordinator = new LLMSchedulerDO({ storage }, {
    MAX_JOBS_PER_UTC_DAY: "100",
    MAX_INGRESS_WRITE_UNITS_PER_UTC_DAY: "30000",
  });

  const res = await coordinator.enqueueBatch([
    purposeJob("j-real", "topic-tags:tagger"),
    purposeJob("j-stale-key", "topic-tags"),
  ]);

  assert.equal([...sql.exec("SELECT id FROM jobs WHERE id = 'j-real'")].length, 1);
  // "topic-tags" was the old, unreachable reservation key. It is not a purpose any client sends,
  // so it must now be rejected rather than quietly accepted.
  assert.equal(
    res.rejected.find((entry) => entry.id === "j-stale-key")?.reason,
    "purpose_not_registered"
  );
});

test("every purpose the Python client can dispatch has a compiled lane", async () => {
  // The client half of this contract is citypods/compute/llm_lanes.py, whose lane_for() raises on
  // an unregistered purpose. This asserts the compiled artifact both halves share actually covers
  // the purposes in the codebase, so adding a call site without a lane fails here rather than in
  // production at 18:15 UTC.
  const { default: compiled } = await import("../src/ingress_reservations.json", {
    with: { type: "json" },
  });
  const dispatchingPurposes = [
    "chapter-agenda",
    "chapter-locator",
    "topic-tags:tagger",
    "topic-tags:prelabeler",
    "r6-moments",
    "r6-judge",
    "tournament:tag",
    "tournament:tag-judge",
    "r5-benchmark:tag",
    "r5-benchmark:judge",
  ];
  for (const purpose of dispatchingPurposes) {
    assert.ok(
      Object.hasOwn(compiled.reservations, purpose),
      `${purpose} has no llm_lanes entry; recompile with scripts/compile_llm_lanes.py`
    );
  }
  assert.ok(
    compiled.reserved_total <= compiled.global_write_budget,
    "reservations must not oversubscribe the global ingress write budget"
  );
});

// --- Lane route allowlist ---------------------------------------------------------------------
// Registering the purpose is half the contract; the lane also names the routes it may spend its
// budget on. Without this check a job stamped `topic-tags:tagger` could be claimed on any route in
// the catalog -- a hand-run `--models` override or a stale client would spend a budget sized for
// one route set on another, and the compiled map's `models` would describe intent rather than what
// actually runs.

const LANE_WITH_MODELS = JSON.stringify({
  "topic-tags:tagger": {
    reserved_write_units: 0,
    daily_write_units: 10000,
    models: ["gemini/gemini-3.1-flash-lite"],
  },
});

function laneModelJob(id, purpose, allowedModels) {
  return {
    id,
    idempotency_key: `k-${id}`,
    request_digest: `d-${id}`,
    policy_json: JSON.stringify({ purpose, allowed_models: allowedModels }),
    prompt_family: "tags",
    input_token_estimate: 100,
    max_output_token_estimate: 50,
    payload_key: `payloads/${id}/request.json`,
  };
}

test("enqueueBatch rejects a model its lane does not declare", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: LANE_WITH_MODELS,
  });

  const res = await coordinator.enqueueBatch([
    laneModelJob("j-in-lane", "topic-tags:tagger", ["gemini/gemini-3.1-flash-lite"]),
    laneModelJob("j-off-lane", "topic-tags:tagger", ["some-other/model"]),
  ]);

  const rejected = res.rejected.find((entry) => entry.id === "j-off-lane");
  assert.ok(rejected, "a route outside the lane must be rejected, not admitted on the lane's budget");
  assert.equal(rejected.reason, "model_not_in_lane");
  assert.deepEqual(rejected.models, ["some-other/model"]);

  // The in-lane sibling in the same batch still lands, and the rejected job consumed nothing.
  assert.deepEqual([...sql.exec("SELECT id FROM jobs ORDER BY id")].map((row) => row.id), [
    "j-in-lane",
  ]);
  const sched = [...sql.exec("SELECT ingress_write_units_today FROM scheduler WHERE id = 1")][0];
  assert.equal(sched.ingress_write_units_today, 4, "only the admitted job may be charged");
});

test("a lane that declares no models constrains no route", async () => {
  // The env override exists so an operator can reshape budgets without a redeploy; it need not
  // restate route lists, and an absent `models` must never read as "no route is allowed" -- that
  // would reject every job the moment someone set a budget override.
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: JSON.stringify({
      "topic-tags:tagger": { reserved_write_units: 0, daily_write_units: 10000 },
    }),
  });

  await coordinator.enqueueBatch([
    laneModelJob("j-any", "topic-tags:tagger", ["whatever/model"]),
  ]);

  assert.equal([...sql.exec("SELECT id FROM jobs WHERE id = 'j-any'")].length, 1);
});

test("superseding a stale row still goes through the registration gate", async () => {
  // Superseding consumes no admission budget, which is exactly why it must not be a way around
  // registration: same idempotency_key, new request_digest, and a lane that has since been
  // removed would otherwise resurrect the row into `queued` under a purpose no reservation covers.
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: REGISTERED_ONLY,
  });

  await coordinator.enqueueBatch([purposeJob("j-super", "topic-tags:tagger")]);
  assert.equal([...sql.exec("SELECT state FROM jobs WHERE id = 'j-super'")][0].state, "queued");
  sql.exec("UPDATE jobs SET state = 'failed' WHERE id = 'j-super'");

  // The lane is retired between the two submissions.
  coordinator.env.INGRESS_PURPOSE_RESERVATIONS = JSON.stringify({
    "chapter-agenda": { reserved_write_units: 0, daily_write_units: 10000 },
  });

  const res = await coordinator.enqueueBatch([
    { ...purposeJob("j-super", "topic-tags:tagger"), request_digest: "d-changed" },
  ]);

  assert.equal(res.rejected[0]?.reason, "purpose_not_registered");
  const row = [...sql.exec("SELECT state, request_digest FROM jobs WHERE id = 'j-super'")][0];
  assert.equal(row.state, "failed", "a rejected supersession must not requeue the stale row");
  assert.equal(row.request_digest, "d-j-super", "the stale row keeps its own payload digest");
});

test("superseding a stale row still goes through the lane route allowlist", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: LANE_WITH_MODELS,
  });

  await coordinator.enqueueBatch([
    laneModelJob("j-route", "topic-tags:tagger", ["gemini/gemini-3.1-flash-lite"]),
  ]);
  sql.exec("UPDATE jobs SET state = 'failed' WHERE id = 'j-route'");

  const res = await coordinator.enqueueBatch([
    {
      ...laneModelJob("j-route", "topic-tags:tagger", ["some-other/model"]),
      request_digest: "d-changed",
    },
  ]);

  assert.equal(res.rejected[0]?.reason, "model_not_in_lane");
  assert.equal(
    [...sql.exec("SELECT state FROM jobs WHERE id = 'j-route'")][0].state,
    "failed",
    "a rejected supersession must not requeue the stale row"
  );
});

test("schemaRetry applies the lane route allowlist, not just the registration gate", async () => {
  // A retry re-admits the job: it consumes the day's job count and charges write units against
  // the lane. So a source whose lane has since dropped the route it names would spend that
  // lane's budget on a route the lane no longer declares -- the same hole the enqueue gate
  // closes, through a different door.
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: LANE_WITH_MODELS,
  });

  await coordinator.enqueueBatch([
    laneModelJob("retry-source", "topic-tags:tagger", ["gemini/gemini-3.1-flash-lite"]),
  ]);
  sql.exec("UPDATE jobs SET state = 'completed' WHERE id = 'retry-source'");

  // The lane is narrowed to a different route between the original admission and the retry.
  coordinator.env.INGRESS_PURPOSE_RESERVATIONS = JSON.stringify({
    "topic-tags:tagger": {
      reserved_write_units: 0,
      daily_write_units: 10000,
      models: ["some-other/model"],
    },
  });

  assert.deepEqual(
    await coordinator.schemaRetry("retry-source", {
      corrected_payload_key: "payloads/retry/request.json",
      corrected_request_digest: "retry-digest",
      corrected_input_token_estimate: 1,
    }),
    { status: "model_not_in_lane" }
  );
  // Rejected before any budget arithmetic: no clone row, and the day's admission is untouched.
  assert.equal([...sql.exec("SELECT COUNT(*) AS n FROM jobs")][0].n, 1);
  const sched = [...sql.exec("SELECT jobs_ingested_today FROM scheduler WHERE id = 1")][0];
  assert.equal(sched.jobs_ingested_today, 1);
});

// --- backup_after_attempts ingress enforcement -------------------------------------------------

const LANE_WITH_BACKUP_THRESHOLD = JSON.stringify({
  "chapter-agenda": {
    reserved_write_units: 0,
    daily_write_units: 10000,
    models: ["primary/model"],
    backup_models: ["backup/model"],
    backup_after_attempts: 12,
  },
});

function backupPolicyJob(id, backupAfterAttempts) {
  return {
    id,
    idempotency_key: `k-${id}`,
    request_digest: `d-${id}`,
    policy_json: JSON.stringify({
      purpose: "chapter-agenda",
      allowed_models: ["primary/model"],
      backup_models: ["backup/model"],
      backup_after_attempts: backupAfterAttempts,
    }),
    prompt_family: "agenda",
    input_token_estimate: 100,
    max_output_token_estimate: 50,
    payload_key: `payloads/${id}/request.json`,
  };
}

test("enqueueBatch rejects a job whose backup_after_attempts undercuts its lane's minimum", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: LANE_WITH_BACKUP_THRESHOLD,
  });

  const res = await coordinator.enqueueBatch([
    backupPolicyJob("j-at-minimum", 12),
    backupPolicyJob("j-below-minimum", 1),
  ]);

  assert.deepEqual(
    res.accepted.map((row) => row.id),
    ["j-at-minimum"]
  );
  const rejected = res.rejected.find((entry) => entry.id === "j-below-minimum");
  assert.ok(rejected, "a lower-than-declared threshold must be rejected, not silently honored");
  assert.equal(rejected.reason, "backup_after_attempts_below_lane_minimum");
  assert.deepEqual([...sql.exec("SELECT id FROM jobs ORDER BY id")].map((row) => row.id), [
    "j-at-minimum",
  ]);
});

test("enqueueBatch admits a job that omits backup_models even when its lane declares a minimum", async () => {
  // The lane minimum only constrains a job that actually names backup models -- a job with none
  // has nothing to widen.
  const { coordinator } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: LANE_WITH_BACKUP_THRESHOLD,
  });
  const job = {
    id: "j-no-backups", idempotency_key: "k1", request_digest: "d1",
    policy_json: JSON.stringify({ purpose: "chapter-agenda", allowed_models: ["primary/model"] }),
    prompt_family: "agenda", input_token_estimate: 100, max_output_token_estimate: 50,
    payload_key: "payloads/j-no-backups/request.json",
  };
  const res = await coordinator.enqueueBatch([job]);
  assert.deepEqual(res.rejected, []);
});

test("schemaRetry applies the same backup_after_attempts floor as enqueueBatch", async () => {
  const { coordinator, sql } = makeCoordinator({
    MAX_JOBS_PER_UTC_DAY: "100",
    INGRESS_PURPOSE_RESERVATIONS: LANE_WITH_BACKUP_THRESHOLD,
  });
  await coordinator.enqueueBatch([backupPolicyJob("retry-source", 12)]);
  sql.exec("UPDATE jobs SET state = 'completed' WHERE id = 'retry-source'");

  // The lane raises its minimum between the original admission and the retry.
  coordinator.env.INGRESS_PURPOSE_RESERVATIONS = JSON.stringify({
    "chapter-agenda": {
      reserved_write_units: 0,
      daily_write_units: 10000,
      models: ["primary/model"],
      backup_models: ["backup/model"],
      backup_after_attempts: 20,
    },
  });

  assert.deepEqual(
    await coordinator.schemaRetry("retry-source", {
      corrected_payload_key: "payloads/retry/request.json",
      corrected_request_digest: "retry-digest",
      corrected_input_token_estimate: 1,
    }),
    { status: "backup_after_attempts_below_lane_minimum" }
  );
  assert.equal([...sql.exec("SELECT COUNT(*) AS n FROM jobs")][0].n, 1);
});

// --- Initiative 20 PR-3 (Failure-class aware backoff & terminal 429 requeue) ---

test("authorizeRetry with own_rpd refuses in-window retry and sets midnight blocked_until", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const bundleDeadline = now + 60_000;
  insertActiveBundle(sql, "b1", "tok", bundleDeadline, bundleDeadline, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-rpd', 'idem-1', 'digest-1', '{}', 'leased', 'b1', 'gemini_3_5_flash_primary',
      'ltok', 'tags', 100, 50, 'payloads/j-rpd/request.json', ?, ?
    )`,
    now, now
  );

  const auth = await coordinator.authorizeRetry("j-rpd", "ltok", "att-1", now, null, "own_rpd");
  assert.equal(auth.authorized, false);
  assert.equal(auth.retry_not_before, null);

  const row = [...sql.exec("SELECT throttle_streak, buffer_seconds, rpd_count, blocked_until, last_failure_class FROM routes WHERE route_id = 'gemini_3_5_flash_primary'")][0];
  assert.equal(row.throttle_streak, 0, "own_rpd must not increment throttle_streak");
  assert.equal(row.buffer_seconds, 0, "own_rpd must not set buffer_seconds");
  assert.ok(row.rpd_count > 0, "rpd_count must be set to route rpd limit");
  assert.ok(row.blocked_until > now, "blocked_until must be set to next midnight");
  assert.equal(row.last_failure_class, "own_rpd");
});

test("authorizeRetry with own_tpm zeroes token budget and refuses in-window retry", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const bundleDeadline = now + 60_000;
  insertActiveBundle(sql, "b1", "tok", bundleDeadline, bundleDeadline, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-tpm', 'idem-1', 'digest-1', '{}', 'leased', 'b1', 'gemini_3_5_flash_primary',
      'ltok', 'tags', 100, 50, 'payloads/j-tpm/request.json', ?, ?
    )`,
    now, now
  );

  const auth = await coordinator.authorizeRetry("j-tpm", "ltok", "att-1", now, null, "own_tpm");
  assert.equal(auth.authorized, false);
  assert.equal(auth.retry_not_before, null);

  const row = [...sql.exec("SELECT throttle_streak, buffer_seconds, full_token_budget, last_failure_class FROM routes WHERE route_id = 'gemini_3_5_flash_primary'")][0];
  assert.equal(row.throttle_streak, 0);
  assert.equal(row.buffer_seconds, 0);
  assert.equal(row.full_token_budget, 0);
  assert.equal(row.last_failure_class, "own_tpm");
});

test("authorizeRetry with payment_required sets the billing day cooldown, not a short own_rpm backoff", async () => {
  // A 429 the classifier reads as payment_required (zero-provisioned-limit, insufficient-budget)
  // used to fall into authorizeRetry's default branch alongside own_rpm/unknown_429, buying only
  // a short retry-friendly buffer instead of the day/week/month billing ladder a state that
  // "does not clear on a retry cadence" actually needs (CodeRabbit, 2026-09-13).
  const { coordinator, sql } = makeCoordinator();
  // Fix the clock away from midnight: the first billing rung is the next UTC midnight, which can
  // legitimately be less than an hour away in production.
  const now = Date.UTC(2026, 8, 14, 12, 0, 0);
  const bundleDeadline = now + 60_000;
  insertActiveBundle(sql, "b1", "tok", bundleDeadline, bundleDeadline, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-pr', 'idem-1', 'digest-1', '{}', 'leased', 'b1', 'gemini_3_5_flash_primary',
      'ltok', 'tags', 100, 50, 'payloads/j-pr/request.json', ?, ?
    )`,
    now, now
  );

  const auth = await coordinator.authorizeRetry("j-pr", "ltok", "att-1", now, null, "payment_required");
  assert.equal(auth.authorized, false);
  assert.equal(auth.retry_not_before, null);

  const row = [...sql.exec("SELECT throttle_streak, buffer_seconds, payment_required_streak, blocked_until, last_failure_class FROM routes WHERE route_id = 'gemini_3_5_flash_primary'")][0];
  assert.equal(row.throttle_streak, 0, "payment_required must not go through the own_rpm buffer path");
  assert.equal(row.buffer_seconds, 0);
  assert.equal(row.payment_required_streak, 1);
  // The first billing rung is exactly the next UTC midnight, not the short own_rpm buffer.
  assert.equal(row.blocked_until, Date.UTC(2026, 8, 15));
  assert.equal(row.last_failure_class, "payment_required");
});

test("authorizeRetry with upstream_capacity sets cooldown and preserves healthy route stats", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const bundleDeadline = now + 60_000;
  insertActiveBundle(sql, "b1", "tok", bundleDeadline, bundleDeadline, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-up', 'idem-1', 'digest-1', '{}', 'leased', 'b1', 'openrouter_google_gemma_4_31b_it_free',
      'ltok', 'tags', 100, 50, 'payloads/j-up/request.json', ?, ?
    )`,
    now, now
  );

  const auth = await coordinator.authorizeRetry("j-up", "ltok", "att-1", now, null, "upstream_capacity");
  assert.equal(auth.authorized, false);
  assert.equal(auth.retry_not_before, null);

  const row = [...sql.exec("SELECT throttle_streak, buffer_seconds, upstream_capacity_streak, blocked_until, last_failure_class FROM routes WHERE route_id = 'openrouter_google_gemma_4_31b_it_free'")][0];
  assert.equal(row.throttle_streak, 0, "upstream_capacity must not increase throttle_streak");
  assert.equal(row.buffer_seconds, 0, "upstream_capacity must not add buffer_seconds");
  assert.equal(row.upstream_capacity_streak, 1);
  assert.ok(row.blocked_until >= now + 15_000, "cooldown must be at least 15s");
  assert.equal(row.last_failure_class, "upstream_capacity");
});

test("a non-consuming refund does not decrement a route window newer than the one it reserved", async () => {
  // claimDispatchWindow stores only lease_route_id/token_reservation on the job -- not the
  // reservation's own rpm/rpd/tpm window identity. If the route's window rolls over between claim
  // and this refund (a slow provider call spanning a window boundary is enough), an unconditional
  // decrement would undercount a NEWER reservation with nothing to do with this one, which can
  // admit excess requests (CodeRabbit, 2026-09-13, "Heavy lift"). full_token_budget has no such
  // risk -- it is a continuously-refilling bucket, not a discrete window-keyed counter -- so it
  // still refunds unconditionally.
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const routeId = "gemini_3_5_flash_primary"; // a real route with tpm configured
  coordinator._getOrCreateRouteLedger(routeId, now, {});
  // Simulate the route having rolled into a brand-new window since this job's claim, already
  // carrying its own usage that a blind decrement would corrupt.
  sql.exec(
    `UPDATE routes SET rpm_window_start = ?, rpm_count = 5, rpd_day_key = '2099-01-01',
                       rpd_count = 3, tpm_window_start = ?, tpm_reserved = 1000,
                       full_token_budget = 100 WHERE route_id = ?`,
    now + 60_000,
    now + 60_000,
    routeId
  );

  insertActiveBundle(sql, "b1", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at, token_reservation,
      reservation_rpm_window_start, reservation_rpd_day_key, reservation_tpm_window_start
    ) VALUES (
      'j-stale-window', 'idem-1', 'digest-1', '{}', 'leased', 'b1', ?,
      'ltok', 'tags', 100, 50, 'payloads/j-stale-window/request.json', ?, ?, 500, 1000, '2020-01-01', 1000
    )`,
    routeId, now, now
  );

  await coordinator.completeBatch("b1", "tok", [
    {
      job_id: "j-stale-window",
      lease_token: "ltok",
      attempt_id: "att-stale",
      planned_at: now,
      actual_start_at: now,
      actual_end_at: now + 500,
      outcome: "retryable_error",
      provider_status_code: 200,
      failure_class: "upstream_capacity",
    },
  ]);

  const row = [...sql.exec(
    "SELECT rpm_count, rpd_count, tpm_reserved, full_token_budget FROM routes WHERE route_id = ?",
    routeId
  )][0];
  assert.equal(row.rpm_count, 5, "a mismatched rpm window must not be decremented");
  assert.equal(row.rpd_count, 3, "a mismatched rpd day must not be decremented");
  assert.equal(row.tpm_reserved, 1000, "a mismatched tpm window must not be decremented");
  assert.equal(row.full_token_budget, 600, "the token bucket refunds unconditionally regardless of window");
});

test("completeBatch requeues terminal 429 under transient retry budget instead of failing", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_5XX_RETRIES: "2" });
  const now = Date.now();
  insertActiveBundle(sql, "b1", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at, transient_retry_count
    ) VALUES (
      'j-429-requeue', 'idem-1', 'digest-1', '{}', 'leased', 'b1', 'openrouter_google_gemma_4_31b_it_free',
      'ltok', 'tags', 100, 50, 'payloads/j-429/request.json', ?, ?, 0
    )`,
    now, now
  );

  await coordinator.completeBatch("b1", "tok", [
    {
      job_id: "j-429-requeue",
      lease_token: "ltok",
      attempt_id: "att-term",
      planned_at: now,
      actual_start_at: now,
      actual_end_at: now + 500,
      outcome: "terminal_error",
      provider_status_code: 429,
    },
  ]);

  const job = [...sql.exec("SELECT state, transient_retry_count, lease_token FROM jobs WHERE id = 'j-429-requeue'")][0];
  assert.equal(job.state, "queued", "terminal 429 must be requeued");
  assert.equal(job.transient_retry_count, 1, "transient_retry_count must be incremented");
  assert.equal(job.lease_token, null, "lease_token must be cleared on requeue");

  const models = [...sql.exec("SELECT COUNT(*) AS n FROM job_models WHERE job_id = 'j-429-requeue'")][0].n;
  assert.ok(models > 0, "job must be re-indexed in job_models for future claims");
});

test("completeBatch applies the upstream_capacity cooldown to a 2xx with no usable completion", async () => {
  // c7a1a6c classifies an empty-2xx response upstream_capacity, but isTransientRouteFailure only
  // covered that failure class arriving as a 400 (gateway.js's upstreamCapacityFailure) -- this
  // status-200 shape fell through to a branch that only records last_provider_status, with no
  // cooldown at all. The same saturated route could be reselected on the very next tick and burn
  // through the whole upstream-capacity retry budget back to back (CodeRabbit, 2026-09-13).
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  insertActiveBundle(sql, "b1", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at, transient_retry_count
    ) VALUES (
      'j-empty-2xx', 'idem-1', 'digest-1', '{}', 'leased', 'b1', 'openrouter_google_gemma_4_31b_it_free',
      'ltok', 'tags', 100, 50, 'payloads/j-empty-2xx/request.json', ?, ?, 0
    )`,
    now, now
  );

  await coordinator.completeBatch("b1", "tok", [
    {
      job_id: "j-empty-2xx",
      lease_token: "ltok",
      attempt_id: "att-empty",
      planned_at: now,
      actual_start_at: now,
      actual_end_at: now + 500,
      outcome: "retryable_error",
      provider_status_code: 200,
      failure_class: "upstream_capacity",
    },
  ]);

  const row = [...sql.exec("SELECT upstream_capacity_streak, blocked_until FROM routes WHERE route_id = 'openrouter_google_gemma_4_31b_it_free'")][0];
  assert.equal(row.upstream_capacity_streak, 1);
  assert.ok(row.blocked_until >= now + 15_000, "cooldown must be at least the base 15s, same as authorizeRetry's");
});

test("an empty structured reply requeues the job, stands the route down and is counted (review/48 R10)", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  insertActiveBundle(sql, "b1", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at, transient_retry_count
    ) VALUES (
      'j-empty-json', 'idem-1', 'digest-1', '{"allowed_models":["google/gemma-4-31b-it"]}', 'leased', 'b1', 'openrouter_google_gemma_4_31b_it_free',
      'ltok', 'tags', 100, 50, 'payloads/j-empty-json/request.json', ?, ?, 0
    )`,
    now, now
  );

  await coordinator.completeBatch("b1", "tok", [
    {
      job_id: "j-empty-json",
      lease_token: "ltok",
      attempt_id: "att-empty-json",
      planned_at: now,
      actual_start_at: now,
      actual_end_at: now + 500,
      outcome: "retryable_error",
      provider_status_code: 200,
      failure_class: "structured_output_empty",
    },
  ]);

  const job = [...sql.exec("SELECT state FROM jobs WHERE id = 'j-empty-json'")][0];
  assert.equal(job.state, "queued", "the job must retry, not complete with a non-answer or fail");
  // Requeued under a model a route can actually claim, not the __unroutable__ sentinel.
  const indexed = [...sql.exec("SELECT model FROM job_models WHERE job_id = 'j-empty-json'")];
  assert.deepEqual(indexed.map((row) => row.model), ["google/gemma-4-31b-it"]);
  const route = [...sql.exec("SELECT upstream_capacity_streak, blocked_until FROM routes WHERE route_id = 'openrouter_google_gemma_4_31b_it_free'")][0];
  assert.equal(route.upstream_capacity_streak, 1);
  assert.ok(route.blocked_until >= now + 15_000, "the route cools down so the job can move elsewhere");
  const failures = [...sql.exec("SELECT failure_class, count FROM route_failures WHERE route_id = 'openrouter_google_gemma_4_31b_it_free'")];
  assert.deepEqual(
    failures.map((row) => [row.failure_class, row.count]),
    [["structured_output_empty", 1]]
  );
});

test("a reply cut off at its output limit requeues without cooling the route down, and is counted", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  insertActiveBundle(sql, "b1", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at, transient_retry_count
    ) VALUES (
      'j-length', 'idem-len', 'digest-len', '{"allowed_models":["google/gemma-4-31b-it"]}', 'leased', 'b1', 'openrouter_google_gemma_4_31b_it_free',
      'ltok', 'tags', 100, 50, 'payloads/j-length/request.json', ?, ?, 0
    )`,
    now, now
  );
  await coordinator.completeBatch("b1", "tok", [
    {
      job_id: "j-length",
      lease_token: "ltok",
      attempt_id: "att-length",
      planned_at: now,
      actual_start_at: now,
      actual_end_at: now + 500,
      outcome: "retryable_error",
      provider_status_code: 200,
      failure_class: "output_budget_exhausted",
    },
  ]);
  const job = [...sql.exec("SELECT state FROM jobs WHERE id = 'j-length'")][0];
  assert.equal(job.state, "queued");
  const route = [...sql.exec("SELECT blocked_until, upstream_capacity_streak FROM routes WHERE route_id = 'openrouter_google_gemma_4_31b_it_free'")][0];
  assert.ok(!route || !route.blocked_until, "the job's budget is not the route's fault");
  const failures = [...sql.exec("SELECT failure_class, count FROM route_failures WHERE route_id = 'openrouter_google_gemma_4_31b_it_free'")];
  assert.deepEqual(failures.map((row) => [row.failure_class, row.count]), [["output_budget_exhausted", 1]]);
});

test("completeBatch success clears upstream_capacity_streak and last_failure_class", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  insertActiveBundle(sql, "b1", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-success', 'idem-1', 'digest-1', '{}', 'leased', 'b1', 'route-suc',
      'ltok', 'tags', 100, 50, 'payloads/j-suc/request.json', ?, ?
    )`,
    now, now
  );
  sql.exec(
    `INSERT INTO routes (
      route_id, upstream_capacity_streak, last_failure_class, throttle_streak, buffer_seconds
    ) VALUES ('route-suc', 3, 'upstream_capacity', 2, 45)`
  );

  await coordinator.completeBatch("b1", "tok", [
    {
      job_id: "j-success",
      lease_token: "ltok",
      attempt_id: "att-ok",
      planned_at: now,
      actual_start_at: now,
      actual_end_at: now + 500,
      outcome: "success",
      provider_status_code: 200,
      observed_input_tokens: 10,
      observed_output_tokens: 20,
    },
  ]);

  const route = [...sql.exec("SELECT upstream_capacity_streak, last_failure_class, throttle_streak, buffer_seconds FROM routes WHERE route_id = 'route-suc'")][0];
  assert.equal(route.upstream_capacity_streak, 0);
  assert.equal(route.last_failure_class, "");
  assert.equal(route.throttle_streak, 0);
  assert.equal(route.buffer_seconds, 0);
});

test("authorizeRetry records route_failures telemetry for 429s", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const today = new Date(now).toISOString().slice(0, 10);
  insertActiveBundle(sql, "b-telem", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-telem', 'idem-1', 'digest-1', '{}', 'leased', 'b-telem', 'route-telem',
      'ltok', 'tags', 100, 50, 'payloads/j-telem/request.json', ?, ?
    )`,
    now,
    now
  );

  // First 429 encounter
  await coordinator.authorizeRetry("j-telem", "ltok", "att-1", now, null, "upstream_capacity");
  let rows = [...sql.exec(
    "SELECT * FROM route_failures WHERE utc_day = ? AND route_id = 'route-telem'",
    today
  )];
  assert.equal(rows.length, 1);
  assert.equal(rows[0].failure_class, "upstream_capacity");
  assert.equal(rows[0].count, 1);
  assert.equal(rows[0].last_status, 429);
  assert.equal(rows[0].last_seen_at, now);

  // Second 429 encounter on the same route/class increments count
  await coordinator.authorizeRetry(
    "j-telem", "ltok", "att-2", now + 1000, null, "upstream_capacity"
  );
  rows = [...sql.exec(
    "SELECT * FROM route_failures WHERE utc_day = ? AND route_id = 'route-telem'",
    today
  )];
  assert.equal(rows.length, 1);
  assert.equal(rows[0].count, 2);
  assert.equal(rows[0].last_seen_at, now + 1000);
});

test("completeBatch records route_failures telemetry for non-429 failures", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const today = new Date(now).toISOString().slice(0, 10);
  insertActiveBundle(sql, "b-batch-fail", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-402', 'idem-402', 'digest-402', '{}', 'leased', 'b-batch-fail', 'route-402',
      'tok-402', 'tags', 100, 50, 'payloads/402.json', ?, ?
    ), (
      'j-500', 'idem-500', 'digest-500', '{}', 'leased', 'b-batch-fail', 'route-500',
      'tok-500', 'tags', 100, 50, 'payloads/500.json', ?, ?
    ), (
      'j-400', 'idem-400', 'digest-400', '{}', 'leased', 'b-batch-fail', 'route-400',
      'tok-400', 'tags', 100, 50, 'payloads/400.json', ?, ?
    )`,
    now, now, now, now, now, now
  );

  await coordinator.completeBatch("b-batch-fail", "tok", [
    {
      job_id: "j-402",
      lease_token: "tok-402",
      attempt_id: "att-402",
      planned_at: now,
      outcome: "retryable_error",
      provider_status_code: 402,
    },
    {
      job_id: "j-500",
      lease_token: "tok-500",
      attempt_id: "att-500",
      planned_at: now,
      outcome: "retryable_error",
      provider_status_code: 503,
    },
    {
      job_id: "j-400",
      lease_token: "tok-400",
      attempt_id: "att-400",
      planned_at: now,
      outcome: "retryable_error",
      provider_status_code: 400,
      failure_class: "upstream_capacity",
    },
  ]);

  const rows = [...sql.exec(
    "SELECT route_id, failure_class, count, last_status FROM route_failures WHERE utc_day = ?",
    today
  )];
  assert.equal(rows.length, 3);

  const row402 = rows.find((r) => r.route_id === "route-402");
  assert.ok(row402);
  assert.equal(row402.failure_class, "payment_required");
  assert.equal(row402.last_status, 402);
  assert.equal(row402.count, 1);

  const row500 = rows.find((r) => r.route_id === "route-500");
  assert.ok(row500);
  assert.equal(row500.failure_class, "server_error");
  assert.equal(row500.last_status, 503);
  assert.equal(row500.count, 1);

  const row400 = rows.find((r) => r.route_id === "route-400");
  assert.ok(row400);
  assert.equal(row400.failure_class, "upstream_capacity");
  assert.equal(row400.last_status, 400);
  assert.equal(row400.count, 1);
});

test("_pruneTerminalRecords prunes route_failures older than attempt retention", async () => {
  const { coordinator, sql } = makeCoordinator({
    ATTEMPT_RETENTION_DAYS: "7",
    MAX_ATTEMPT_PRUNE_PER_TICK: "10",
  });
  const now = Date.now();
  const staleDay = new Date(now - 14 * 86_400_000).toISOString().slice(0, 10);
  const today = new Date(now).toISOString().slice(0, 10);

  sql.exec(
    `INSERT INTO route_failures (utc_day, route_id, failure_class, count, last_status, last_seen_at)
     VALUES (?, 'route-stale', 'own_rpm', 5, 429, ?),
            (?, 'route-fresh', 'own_rpm', 2, 429, ?)`,
    staleDay,
    now - 14 * 86_400_000,
    today,
    now
  );

  const result = coordinator._pruneTerminalRecords(now);
  assert.equal(result.routeFailuresDeleted, 1);

  const remaining = [...sql.exec("SELECT utc_day, route_id FROM route_failures")];
  assert.equal(remaining.length, 1);
  assert.equal(remaining[0].utc_day, today);
  assert.equal(remaining[0].route_id, "route-fresh");
});

test("stats exposes today's route_failures ordered by count DESC capped at limit", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const today = new Date(now).toISOString().slice(0, 10);
  const yesterday = new Date(now - 86_400_000).toISOString().slice(0, 10);

  sql.exec(
    `INSERT INTO route_failures (utc_day, route_id, failure_class, count, last_status, last_seen_at)
     VALUES (?, 'r-1', 'own_rpm', 10, 429, ?),
            (?, 'r-2', 'upstream_capacity', 25, 429, ?),
            (?, 'r-3', 'payment_required', 5, 402, ?),
            (?, 'r-old', 'own_rpm', 99, 429, ?)`,
    today,
    now,
    today,
    now,
    today,
    now,
    yesterday,
    now - 86_400_000
  );

  const s = await coordinator.detailedStats(now, 2);
  assert.ok(Array.isArray(s.route_failures), "stats must include route_failures");
  assert.equal(s.route_failures.length, 2, "must be capped at limit=2");
  assert.equal(s.route_failures[0].route_id, "r-2");
  assert.equal(s.route_failures[0].count, 25);
  assert.equal(s.route_failures[1].route_id, "r-1");
  assert.equal(s.route_failures[1].count, 10);
  assert.ok(!s.route_failures.some((r) => r.utc_day === yesterday), "must only include today");
});

test("authorizeRetry overrides untrustworthy Retry-After with observed_recovery_seconds", async () => {
  const dispatchOverride = {
    routes_by_id: {
      "r-untrustworthy": {
        route_id: "r-untrustworthy",
        provider: "mock",
        retry_after_trustworthy: false,
        observed_recovery_seconds: 45,
      },
    },
  };
  const { coordinator, sql } = makeCoordinator({
    DISPATCH_LIMITS_OVERRIDE: dispatchOverride,
  });
  const now = Date.now();
  insertActiveBundle(sql, "b-untrust", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at
    ) VALUES (
      'j-untrust', 'idem-1', 'digest-1', '{}', 'leased', 'b-untrust', 'r-untrustworthy',
      'ltok', 'tags', 100, 50, 'payloads/j-untrust/request.json', ?, ?
    )`,
    now,
    now
  );

  // Provider claims Retry-After is 5s, but route is measured untrustworthy with 45s recovery.
  const auth = await coordinator.authorizeRetry(
    "j-untrust", "ltok", "att-1", now, 5, "unknown_429"
  );
  assert.equal(auth.authorized, true);
  // Must back off by at least 45 seconds (not 5 seconds).
  assert.ok(auth.retry_not_before >= now + 45_000);

  const routeRow = [
    ...sql.exec(
      "SELECT buffer_seconds, blocked_until FROM routes WHERE route_id='r-untrustworthy'"
    ),
  ][0];
  assert.ok(routeRow.blocked_until >= now + 45_000);
});


// ---------------------------------------------------------------------------
// Regressions from the 2026-09-09 Initiative 20 review.
// ---------------------------------------------------------------------------

test("a route with no declared rpd is unlimited on that axis, not treated as paused", () => {
  // THE bug: `Number(null) === 0`, and this repo uses an explicit 0 to mean "paused/exhausted",
  // so a route whose compiled JSON carried `"rpd": null` scored capacity 0. claimDispatchWindow
  // filters `score > 0`, so those routes were never ranked, never claimed, never dispatched --
  // with no blocked_until and no error. That was 34 of 69 catalog routes, including all 14
  // Mistral routes, behind which 21,287 jobs sat queued for 22 days.
  const { coordinator } = makeCoordinator({});
  const now = Date.now();

  const unlimited = coordinator._capacityFraction(
    { route_id: "r", rpm: 60, rpd: null, tpm: null, rpm_window_start: 0, rpd_day_key: "" },
    now,
    25
  );
  assert.ok(unlimited > 0, "rpd:null must not zero the route out of the ranking");

  const paused = coordinator._capacityFraction(
    { route_id: "r", rpm: 60, rpd: 0, tpm: null, rpm_window_start: 0, rpd_day_key: "" },
    now,
    25
  );
  assert.equal(paused, 0, "an explicit rpd:0 is still the repository's paused convention");
});

test("a route with no declared rpm is unlimited on that axis, not treated as paused", () => {
  const { coordinator } = makeCoordinator({});
  const score = coordinator._capacityFraction(
    { route_id: "r", rpm: null, rpd: 500, tpm: null, rpd_day_key: "" },
    Date.now(),
    25
  );
  assert.ok(score > 0, "rpm:null must not zero the route out of the ranking");
});

test("every catalog route can be ranked (no route is silently unclaimable)", async () => {
  // A fleet-wide guard: if any compiled route scores 0 on a clean ledger, it can never be
  // dispatched and no operator surface would say why.
  const { default: limits } = await import("../src/dispatch_limits.json", {
    with: { type: "json" },
  });
  const { coordinator } = makeCoordinator({});
  const now = Date.now();
  const dead = Object.values(limits.routes_by_id)
    .filter((r) => Number(r.rpd) !== 0)
    .filter((r) => coordinator._capacityFraction({ ...r, rpd_day_key: "" }, now, 25) === 0)
    .map((r) => r.route_id);
  assert.deepEqual(dead, [], `routes unrankable on a clean ledger: ${dead.join(", ")}`);
});

test("_rankModelsByCapacity does not drop a model whose every route declares no rpd", () => {
  // Number(null) === 0, so a route with no rpd configured -- unlimited on that axis, per
  // _capacityFraction's own treatment of the same field -- was silently weighted zero here.
  // A model whose every route declares neither rpd nor rpm (several real NVIDIA/DeepSeek/zai
  // routes today) got totalWeight === 0, forcing score to 0 and dropping the model out of ranking
  // entirely via the `score > 0` filter (CodeRabbit, 2026-09-13) -- not merely under-weighted, but
  // invisible to dispatch no matter how available its routes actually were.
  const { coordinator } = makeCoordinator({});
  const dispatchLimits = {
    providers: {},
    routes_by_id: {
      unlimited_route: {
        route_id: "unlimited_route",
        provider: "x",
        rpd: null,
        rpm: null,
        tpm: null,
      },
    },
    model_routes_map: { "x/unlimited": ["unlimited_route"] },
  };
  const ranked = coordinator._rankModelsByCapacity(Date.now(), 25, dispatchLimits);
  const entry = ranked.find((r) => r.model === "x/unlimited");
  assert.ok(entry, "a model with no rpd/rpm/tpm on any route must still appear in ranking");
  assert.ok(entry.score > 0, "its score must be positive, not the hardcoded zero-weight fallback");
});

test("unconfigured account credentials are reported, never used to gate dispatch", () => {
  // This is a DIAGNOSTIC, deliberately not a dispatch gate. Gating on secret presence would mean
  // that if the DO env ever failed to expose secrets the way this assumes, the whole catalog would
  // drop out of the ranking silently -- the same failure shape as the `rpd: null` coercion fixed
  // in this review. Reporting it lets an operator see an unconfigured account without risking that.
  const { coordinator } = makeCoordinator({ PRESENT_KEY: "sk-real" });
  const limits = {
    providers: {
      p: { accounts: [{ id: "primary", api_key_env: "PRESENT_KEY" }, { id: "second", api_key_env: "ABSENT_KEY" }] },
    },
  };

  assert.equal(
    coordinator._routeCredentialConfigured({ provider: "p", account_id: "primary" }, limits),
    true
  );
  assert.equal(
    coordinator._routeCredentialConfigured({ provider: "p", account_id: "second" }, limits),
    false
  );
  // An account_id that does not exist at all must not fall through to "configured".
  assert.equal(
    coordinator._routeCredentialConfigured({ provider: "p", account_id: "ghost" }, limits),
    false
  );
  // A provider declaring no accounts is not something this gate can judge; leave it rankable.
  assert.equal(
    coordinator._routeCredentialConfigured({ provider: "q" }, { providers: { q: {} } }),
    true
  );
});

test("stats() names accounts whose secret is not set in this deployment", async () => {
  const { coordinator } = makeCoordinator({
    PRESENT_KEY: "sk-real",
    DISPATCH_LIMITS_OVERRIDE: {
      providers: {
        p: {
          accounts: [
            { id: "primary", api_key_env: "PRESENT_KEY" },
            { id: "tertiary", api_key_env: "ABSENT_KEY" },
          ],
        },
      },
      routes_by_id: {},
      model_routes_map: {},
    },
  });
  const stats = await coordinator.detailedStats(Date.now(), 20);
  assert.deepEqual(stats.unconfigured_accounts, ["p:tertiary (ABSENT_KEY)"]);
});

test("jobs carries only the terminal-history state index; retired indexes are dropped", () => {
  // Every index on jobs is a billed DO row on each insert and state change (write_budget.js /
  // bench/rows-written). Adding one back must be a deliberate, re-measured decision.
  const { storage, sql } = createMockSqlStorage();
  // An already-deployed coordinator still carries the retired indexes.
  new LLMSchedulerDO({ storage }, withTestReservations({}));
  sql.exec("CREATE INDEX IF NOT EXISTS idx_jobs_state_updated ON jobs (state, updated_at)");
  sql.exec("CREATE INDEX IF NOT EXISTS idx_jobs_state_priority_created ON jobs (state, priority, created_at)");
  sql.exec("CREATE INDEX IF NOT EXISTS idx_jobs_purpose_state_created ON jobs (purpose, state, created_at)");
  new LLMSchedulerDO({ storage }, withTestReservations({}));
  const names = sql
    .exec("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'jobs'")
    .map((row) => row.name)
    .filter((name) => !name.startsWith("sqlite_autoindex_"))
    .sort();
  assert.deepEqual(names, ["idx_jobs_state_updated_id"]);
});

test("an existing rowid job_models is rebuilt clustered, keeping rows, key uniqueness and the priority trigger", async () => {
  const { storage, sql } = createMockSqlStorage();
  // A pre-2026-09-23 coordinator: rowid job_models + separate scan index.
  sql.exec(`
    CREATE TABLE job_models (
      job_id TEXT NOT NULL, model TEXT NOT NULL, priority INTEGER NOT NULL,
      created_at INTEGER NOT NULL, PRIMARY KEY (job_id, model)
    );
    CREATE INDEX idx_job_models_model_priority_created
      ON job_models (model, priority, created_at, job_id);
    INSERT INTO job_models (job_id, model, priority, created_at)
      VALUES ('queued-before-deploy', 'gemini/gemini-flash-lite', 1, 42);
  `);
  const { coordinator } = makeCoordinator({}, { storage, sql });
  // The copy is the one migration step that could lose queued work: every column must survive.
  assert.deepEqual(
    sql
      .exec(
        "SELECT job_id, model, priority, created_at FROM job_models WHERE job_id = 'queued-before-deploy'"
      )
      .map((row) => ({ ...row })),
    [{ job_id: "queued-before-deploy", model: "gemini/gemini-flash-lite", priority: 1, created_at: 42 }]
  );
  await coordinator.enqueueBatch([
    {
      id: "legacy-1",
      idempotency_key: "k1",
      request_digest: "d1",
      policy_json: JSON.stringify({ allowed_models: ["gemini/gemini-flash-lite"] }),
      prompt_family: "tags",
      input_token_estimate: 10,
      max_output_token_estimate: 10,
      payload_key: "p1",
    },
  ].map((job) => ({ ...job })));
  // Rebuild happens at construction; a second construction must be a no-op.
  const rowsBefore = sql.exec("SELECT * FROM job_models ORDER BY job_id").map((row) => ({ ...row }));
  makeCoordinator({}, { storage, sql });
  assert.deepEqual(
    sql.exec("SELECT * FROM job_models ORDER BY job_id").map((row) => ({ ...row })),
    rowsBefore
  );
  const ddl = sql.exec("SELECT sql FROM sqlite_master WHERE name = 'job_models'")[0].sql;
  assert.match(ddl, /WITHOUT ROWID/i);
  const indexes = sql
    .exec("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'job_models'")
    .map((row) => row.name)
    .filter((name) => !name.startsWith("sqlite_autoindex_"));
  // No job_id index (2026-10-07): deletes seek the primary key from jobs.queue_models, and the
  // one legacy row here has no queued job, so the transitional index is retired at once.
  assert.deepEqual(indexes, []);
  // Uniqueness per queue key survives: a duplicate insert is ignored.
  const before = sql.exec("SELECT COUNT(*) AS n FROM job_models")[0].n;
  sql.exec(
    "INSERT OR IGNORE INTO job_models (job_id, model, priority, created_at) SELECT job_id, model, priority, created_at FROM job_models"
  );
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM job_models")[0].n, before);
  // The priority-sync trigger still reaches the rebuilt table.
  sql.exec("UPDATE jobs SET priority = 0 WHERE id IN (SELECT job_id FROM job_models)");
  assert.ok(
    sql
      .exec("SELECT priority FROM job_models WHERE job_id = 'legacy-1'")
      .every((row) => row.priority === 0)
  );
  // The admission scan reads the clustered key in order: no temp sort.
  const plan = sql
    .exec(
      "EXPLAIN QUERY PLAN SELECT jobs.* FROM job_models JOIN jobs ON jobs.id = job_models.job_id WHERE job_models.model = 'm' AND jobs.state = 'queued' ORDER BY job_models.priority, job_models.created_at, job_models.job_id LIMIT 4"
    )
    .map((row) => row.detail)
    .join(" | ");
  assert.ok(!plan.includes("TEMP B-TREE"), plan);
});

test("queued jobs from before queue_models keep the legacy job_id index until none remain", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  const policy = JSON.stringify({ allowed_models: ["gemini/gemini-3.1-flash-lite"] });
  // A production coordinator at deploy: the (job_id, model) index and a job queued without
  // queue_models.
  sql.exec(`
    CREATE UNIQUE INDEX idx_job_models_job_model ON job_models (job_id, model);
    INSERT INTO jobs (id, idempotency_key, request_digest, state, priority, policy_json,
      prompt_family, input_token_estimate, max_output_token_estimate, payload_key, created_at,
      updated_at)
      VALUES ('legacy', 'k-legacy', 'd', 'queued', 1, '${policy}', 'tags', 10, 10, 'p', 5, 5);
    INSERT INTO job_models (job_id, model, priority, created_at)
      VALUES ('legacy', 'gemini/gemini-3.1-flash-lite', 1, 5);
  `);
  await coordinator.enqueueBatch([{
    id: "fresh", idempotency_key: "k-fresh", request_digest: "d", policy_json: policy,
    prompt_family: "tags", input_token_estimate: 10, max_output_token_estimate: 10,
    payload_key: "p-fresh",
  }]);
  const fresh = sql.exec("SELECT queue_models FROM jobs WHERE id = 'fresh'")[0];
  assert.equal(fresh.queue_models, JSON.stringify(["gemini/gemini-3.1-flash-lite"]));

  const indexExists = () =>
    sql.exec("SELECT 1 FROM sqlite_master WHERE name = 'idx_job_models_job_model'").length > 0;
  await coordinator.recountQueuedJobs();
  assert.ok(indexExists(), "a legacy job is still queued, so its delete path must stay indexed");

  await coordinator.cancelBatch(["legacy"]);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM job_models WHERE job_id = 'legacy'")[0].n, 0);
  await coordinator.recountQueuedJobs();
  assert.ok(!indexExists(), "the index is retired once every queued job records queue_models");

  // A recorded queue key is deleted by primary key, with no job_id index.
  const plan = sql.exec(
    "EXPLAIN QUERY PLAN DELETE FROM job_models WHERE model = ? AND priority = ? AND created_at = ? AND job_id = ?",
    "m", 1, 1, "fresh"
  ).map((row) => row.detail).join(" | ");
  assert.match(plan, /PRIMARY KEY/);
  await coordinator.cancelBatch(["fresh"]);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM job_models")[0].n, 0);
});

test("a direct priority edit moves a recorded queue key's rows by primary key", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([{
    id: "promote", idempotency_key: "k-promote", request_digest: "d",
    policy_json: JSON.stringify({ allowed_models: ["gemini/gemini-flash-lite"] }),
    prompt_family: "tags", input_token_estimate: 10, max_output_token_estimate: 10,
    payload_key: "p", priority: 1,
  }]);
  sql.exec("UPDATE jobs SET priority = 0 WHERE id = 'promote'");
  assert.deepEqual(
    sql.exec("SELECT priority FROM job_models WHERE job_id = 'promote'").map((row) => row.priority),
    [0]
  );
  // ...and the moved rows are still found by the job's key when it is unindexed.
  await coordinator.cancelBatch(["promote"]);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM job_models")[0].n, 0);
});

// ---------------------------------------------------------------------------------------------
// Dispatch pause (POST /v2/dispatch:pause|resume, pause-status, reserve)
// ---------------------------------------------------------------------------------------------

const PAUSE_MODEL = "gemini/gemini-3.1-flash-lite";
const pauseJob = (id) => ({
  id,
  idempotency_key: `${id}-key`,
  request_digest: `${id}-digest`,
  policy_json: JSON.stringify({ allowed_models: [PAUSE_MODEL], purpose: "topic-tags:tagger" }),
  prompt_family: "tags",
  input_token_estimate: 100,
  max_output_token_estimate: 50,
  payload_key: `payloads/${id}/request.json`,
});

test("a provider pause stops claims on that provider until resumed", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([pauseJob("p1"), pauseJob("p2")]);
  const now = Date.now();

  const paused = await coordinator.pauseDispatch(
    { scope: "provider", target: "gemini", seconds: 600, reason: "canary" },
    now
  );
  assert.equal(paused.ok, true);
  assert.equal(paused.scope, "provider:gemini");
  const blocked = await coordinator.claimDispatchWindow(now, 30);
  assert.deepEqual(blocked.jobs, []);

  const resumed = await coordinator.resumeDispatch({ scope: "provider", target: "gemini" }, now);
  assert.equal(resumed.resumed, true);
  const claimed = await coordinator.claimDispatchWindow(now, 30);
  assert.ok(claimed.jobs.length > 0);
});

test("a route pause leaves the model's other routes claimable", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([pauseJob("r1"), pauseJob("r2")]);
  const now = Date.now();
  await coordinator.pauseDispatch(
    { scope: "route", target: "gemini_3_1_flash_lite_primary", seconds: 600 },
    now
  );
  const plan = await coordinator.claimDispatchWindow(now, 30);
  assert.ok(plan.jobs.length > 0);
  for (const job of plan.jobs) {
    assert.equal(job.route_id ?? job.lease_route_id, "gemini_3_1_flash_lite_secondary");
  }
});

test("a pause expires by itself", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([pauseJob("t1")]);
  const now = Date.now();
  await coordinator.pauseDispatch({ scope: "global", seconds: 60 }, now);
  assert.equal((await coordinator.claimDispatchWindow(now + 59_000, 30)).claim_reason, "dispatch_paused");
  const after = await coordinator.claimDispatchWindow(now + 61_000, 30);
  assert.ok(after.jobs.length > 0);
  assert.deepEqual((await coordinator.dispatchPauseStatus({}, now + 61_000)).pauses, []);
});

test("a globally paused claim tick executes no SQL at all, so it writes no rows", async () => {
  const { sql, storage, recorder } = createRecordingSqlStorage();
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" }, { sql, storage });
  await coordinator.enqueueBatch([pauseJob("g1")]);
  const now = Date.now();
  await coordinator.pauseDispatch({ scope: "global", seconds: 600, reason: "incident" }, now);
  await coordinator.claimDispatchWindow(now, 30); // warms nothing: the pause cache is already fresh
  recorder.start();
  const plan = await coordinator.claimDispatchWindow(now + 1000, 30);
  assert.equal(plan.claim_reason, "dispatch_paused");
  assert.deepEqual(plan.jobs, []);
  assert.deepEqual(recorder.statements, []);
});

test("pause and status reject targets the catalog does not know", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  const bad = await coordinator.pauseDispatch({ scope: "provider", target: "nope", seconds: 60 });
  assert.equal(bad.ok, false);
  assert.equal(bad.error, "unknown_target");
  const badRoute = await coordinator.dispatchPauseStatus({ scope: "route", target: "nope" });
  assert.equal(badRoute.ok, false);
});

test("pause-status reports the selection's in-flight count and each route's daily quota", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([pauseJob("s1"), pauseJob("s2")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 30);
  assert.ok(plan.jobs.length > 0);

  const status = await coordinator.dispatchPauseStatus({ scope: "provider", target: "gemini" }, now);
  assert.equal(status.ok, true);
  assert.equal(status.selection, "provider:gemini");
  assert.equal(status.in_flight, plan.jobs.length);
  const route = status.routes.gemini_3_1_flash_lite_primary;
  assert.equal(route.rpd_limit, 500);
  assert.equal(route.rpd_remaining, 500 - route.rpd_used);
  assert.ok(route.rpd_resets_at > now);

  const other = await coordinator.dispatchPauseStatus({ scope: "provider", target: "nvidia" }, now);
  assert.equal(other.in_flight, 0);

  const stats = await coordinator.stats(now);
  assert.equal(stats.in_flight.by_provider.gemini, plan.jobs.length);
});

test("reserve charges out-of-band calls to the route's daily ledger, on the provider's day", async () => {
  const { coordinator } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  // 2026-09-24 12:00 America/Los_Angeles (19:00 UTC); Gemini resets at Pacific midnight.
  const noonPacific = Date.UTC(2026, 8, 24, 19, 0, 0);
  const reserved = await coordinator.reserveRouteRequests(
    { route_id: "gemini_3_1_flash_lite_primary", requests: 2 },
    noonPacific
  );
  assert.equal(reserved.ok, true);
  assert.equal(reserved.rpd_count, 2);
  assert.equal(reserved.rpd_day_key, "2026-09-24");

  const selection = { scope: "route", target: "gemini_3_1_flash_lite_primary" };
  const sameDay = await coordinator.dispatchPauseStatus(selection, noonPacific + 60_000);
  assert.equal(sameDay.routes.gemini_3_1_flash_lite_primary.rpd_remaining, 498);
  // Midnight PDT, to nextZonedMidnightMs's one-minute resolution.
  const resetsAt = sameDay.routes.gemini_3_1_flash_lite_primary.rpd_resets_at;
  const midnightPdt = Date.UTC(2026, 8, 25, 7, 0, 0);
  assert.ok(resetsAt >= midnightPdt && resetsAt < midnightPdt + 60_000, String(resetsAt));
  // 20:00 Pacific is already 2026-09-25 in UTC but still the same provider day.
  const eveningPacific = Date.UTC(2026, 8, 25, 3, 0, 0);
  const evening = await coordinator.dispatchPauseStatus(selection, eveningPacific);
  assert.equal(evening.routes.gemini_3_1_flash_lite_primary.rpd_used, 2);
  const nextDay = await coordinator.dispatchPauseStatus(selection, Date.UTC(2026, 8, 25, 8, 0, 0));
  assert.equal(nextDay.routes.gemini_3_1_flash_lite_primary.rpd_used, 0);
  assert.equal(nextDay.routes.gemini_3_1_flash_lite_primary.rpd_remaining, 500);
});

test("pause targets must be own catalog entries, not Object.prototype members", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  for (const target of ["constructor", "toString", "__proto__"]) {
    assert.equal((await coordinator.pauseDispatch({ scope: "route", target, seconds: 60 })).ok, false);
    assert.equal((await coordinator.pauseDispatch({ scope: "provider", target, seconds: 60 })).ok, false);
    assert.equal((await coordinator.reserveRouteRequests({ route_id: target, requests: 1 })).ok, false);
  }
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM routes WHERE route_id = 'constructor'")[0].n, 0);
});

test("the drain signal ignores expired leases, which a global pause never reaps", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  await coordinator.enqueueBatch([pauseJob("e1"), pauseJob("e2")]);
  const now = Date.now();
  const plan = await coordinator.claimDispatchWindow(now, 30);
  assert.ok(plan.jobs.length > 0);
  await coordinator.pauseDispatch({ scope: "global", seconds: 3600 }, now);
  const live = await coordinator.dispatchPauseStatus({ scope: "provider", target: "gemini" }, now);
  assert.equal(live.in_flight, plan.jobs.length);

  // The bundle dies: its leases pass lease_expires_at while still `leased`.
  const expiry = sql.exec("SELECT MAX(lease_expires_at) AS t FROM jobs WHERE state = 'leased'")[0].t;
  const later = expiry + 1;
  assert.equal((await coordinator.claimDispatchWindow(later, 30)).claim_reason, "dispatch_paused");
  const dead = await coordinator.dispatchPauseStatus({ scope: "provider", target: "gemini" }, later);
  assert.equal(dead.in_flight, 0);
  assert.equal((await coordinator.stats(later)).in_flight.by_provider.gemini, undefined);
});


/** Lease `jobs` ([id, purpose, reservedOutput, route]) in a fresh bundle and complete them. */
function completeLeased(sql, coordinator, bundleId, jobs, results) {
  const now = Date.now();
  insertActiveBundle(sql, bundleId, "tok", now + 60_000, now + 60_000, now);
  for (const [id, purpose, reserved, route] of jobs) {
    sql.exec(
      `INSERT INTO jobs (id, idempotency_key, request_digest, policy_json, state, bundle_id,
         lease_route_id, lease_token, prompt_family, input_token_estimate,
         max_output_token_estimate, payload_key, created_at, updated_at, purpose)
       VALUES (?, ?, 'd', ?, 'leased', ?, ?, ?, 'x', 100, ?, ?, ?, ?, ?)`,
      id, `idem-${id}`, JSON.stringify({ purpose }), bundleId, route, `lt-${id}`, reserved,
      `payloads/${id}.json`, now, now, purpose
    );
  }
  return coordinator.completeBatch(bundleId, "tok", results.map((result) => ({
    lease_token: `lt-${result.job_id}`, planned_at: now, outcome: "success",
    provider_status_code: 200, result_key: `results/${result.job_id}.json`, ...result,
  })));
}

test("detailedStats reports today's usage per lane and route from completed attempts", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const yesterday = new Date(now - 86_400_000).toISOString().slice(0, 10);
  // A cell from yesterday is excluded.
  sql.exec(
    "INSERT INTO attempt_usage (utc_day, purpose, route_id, calls, outputs_json) VALUES (?, 'chapter-agenda', 'route-a', 1, '[99999]')",
    yesterday
  );
  const at = (offset, duration) => ({ actual_start_at: now - offset, actual_end_at: now - offset + duration });
  await completeLeased(sql, coordinator, "b1", [
    ["j1", "chapter-agenda", 16384, "route-a"],
    ["j2", "chapter-agenda", 16384, "route-a"],
    ["j3", "topic-tags:tagger", 8192, "route-b"],
  ], [
    { job_id: "j1", attempt_id: "a1", observed_output_tokens: 2000, ...at(3000, 30_000) },
    // over its reservation, and slow
    { job_id: "j2", attempt_id: "a2", observed_output_tokens: 20000, ...at(2000, 650_000) },
    { job_id: "j3", attempt_id: "a3", observed_output_tokens: 500, ...at(1000, 5_000) },
  ]);

  const usage = (await coordinator.detailedStats(now, 50)).usage_today;
  const agenda = usage.find((row) => row.purpose === "chapter-agenda");
  assert.equal(agenda.route_id, "route-a");
  assert.equal(agenda.calls, 2);
  assert.equal(agenda.reserved_output_mean, 16384);
  assert.equal(agenda.over_reservation_calls, 1);
  assert.equal(agenda.slow_calls, 1);
  assert.equal(agenda.output_tokens_max, 20000);
  const tagger = usage.find((row) => row.purpose === "topic-tags:tagger");
  assert.deepEqual([tagger.calls, tagger.output_tokens_p90, tagger.slow_calls], [1, 500, 0]);
  // One cell per lane/route per day, written once for the whole completion.
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM attempt_usage")[0].n, 3);
});

test("a length-truncated reply settles the token bucket to its measured usage", async () => {
  // A route_max reply can use far more than its reservation; leaving the bucket at the
  // reservation would admit the next claim against capacity already spent.
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const routeId = "gemini_3_5_flash_primary"; // a real route with tpm configured
  coordinator._getOrCreateRouteLedger(routeId, now, {});
  sql.exec("UPDATE routes SET full_token_budget = 100000 WHERE route_id = ?", routeId);
  insertActiveBundle(sql, "b1", "tok", now + 60_000, now + 60_000, now);
  sql.exec(
    `INSERT INTO jobs (
      id, idempotency_key, request_digest, policy_json, state, bundle_id, lease_route_id,
      lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
      payload_key, created_at, updated_at, transient_retry_count, token_reservation, purpose
    ) VALUES (
      'j-long', 'idem-long', 'digest-long', '{"purpose":"chapter-agenda"}', 'leased', 'b1', ?,
      'ltok', 'agenda', 1000, 16384, 'payloads/j-long/request.json', ?, ?, 0, 17384, 'chapter-agenda'
    )`,
    routeId, now, now
  );
  await coordinator.completeBatch("b1", "tok", [
    {
      job_id: "j-long",
      lease_token: "ltok",
      attempt_id: "att-long",
      planned_at: now,
      actual_start_at: now,
      actual_end_at: now + 500,
      outcome: "retryable_error",
      provider_status_code: 200,
      failure_class: "output_budget_exhausted",
      observed_input_tokens: 1000,
      observed_output_tokens: 65536,
    },
  ]);
  const route = sql.exec("SELECT full_token_budget FROM routes WHERE route_id = ?", routeId)[0];
  // Google TPM counts only input; output usage stays in the attempt telemetry.
  assert.equal(route.full_token_budget, 100000 + 17384 - 1000);
  // usage_today keeps the lane and reservation, folded at completion.
  const cell = sql.exec("SELECT purpose, reserved_sum, over_reservation_calls FROM attempt_usage")[0];
  assert.deepEqual(
    [cell.purpose, cell.reserved_sum, cell.over_reservation_calls],
    ["chapter-agenda", 16384, 1]
  );
});

test("usage_today keeps a retired job's lane and leaves unmeasured calls out of the percentiles", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const call = { actual_start_at: now - 1000, actual_end_at: now - 500 };
  await completeLeased(sql, coordinator, "b-m", [
    ["m1", "chapter-agenda", 16384, "route-a"],
    ["m2", "chapter-agenda", 16384, "route-a"],
    ["m3", "chapter-agenda", 16384, "route-a"],
  ], [
    { job_id: "m1", attempt_id: "a-m1", observed_output_tokens: 12000, ...call },
    // failed before usage came back
    { job_id: "m2", attempt_id: "a-m2", outcome: "terminal_error", provider_status_code: 400, ...call },
    { job_id: "m3", attempt_id: "a-m3", outcome: "terminal_error", provider_status_code: 400, ...call },
  ]);
  // The jobs are retired after their results are consumed; usage was folded at completion.
  sql.exec("DELETE FROM jobs");
  const [row] = (await coordinator.detailedStats(now, 50)).usage_today;
  assert.equal(row.purpose, "chapter-agenda");
  assert.equal(row.calls, 3);
  assert.equal(row.measured_calls, 1);
  assert.equal(row.output_tokens_p50, 12000);
  assert.equal(row.reserved_output_mean, 16384);
});

test("a usage cell keeps exact counts past its percentile sample cap", async () => {
  const { coordinator, sql } = makeCoordinator();
  const cap = coordinator.constructor.ATTEMPT_USAGE_SAMPLE_CAP;
  const today = new Date().toISOString().slice(0, 10);
  sql.exec(
    `INSERT INTO attempt_usage (utc_day, purpose, route_id, calls, measured_calls, outputs_json)
     VALUES (?, 'chapter-agenda', 'route-a', ?, ?, ?)`,
    today, cap, cap, JSON.stringify(Array(cap).fill(10))
  );
  const now = Date.now();
  await completeLeased(sql, coordinator, "b-cap", [["c1", "chapter-agenda", 100, "route-a"]], [
    { job_id: "c1", attempt_id: "a-c1", observed_output_tokens: 99, actual_start_at: now, actual_end_at: now },
  ]);
  const cell = sql.exec("SELECT calls, measured_calls, outputs_json FROM attempt_usage")[0];
  assert.equal(cell.calls, cap + 1);
  assert.equal(cell.measured_calls, cap + 1);
  assert.equal(JSON.parse(cell.outputs_json).length, cap);
});

test("detailedStats can restrict route_failures to named classes past the row limit", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const today = new Date(now).toISOString().slice(0, 10);
  const insert = (route, cls, count) => sql.exec(
    `INSERT INTO route_failures (utc_day, route_id, failure_class, count, last_status, last_seen_at)
     VALUES (?, ?, ?, ?, 429, ?)`,
    today, route, cls, count, now
  );
  for (let i = 0; i < 5; i++) insert(`busy-${i}`, "upstream_capacity", 1000 + i);
  insert("cut", "output_budget_exhausted", 3);
  const unfiltered = (await coordinator.detailedStats(now, 2)).route_failures;
  assert.equal(unfiltered.some((row) => row.failure_class === "output_budget_exhausted"), false);
  const filtered = (
    await coordinator.detailedStats(now, 2, { failureClasses: ["output_budget_exhausted"] })
  ).route_failures;
  assert.deepEqual(filtered.map((row) => row.route_id), ["cut"]);
});


test("TPD reserves and settles daily tokens to actual usage", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const routeId = "groq_qwen_3_8_27b_primary";
  const catalog = coordinator._dispatchLimits().routes_by_id[routeId];
  const ledger = coordinator._getOrCreateRouteLedger(routeId, now, catalog);
  const reserved = coordinator._applyProvisionalReservation({ ...catalog, ...ledger }, 2000, now);
  coordinator._writeRouteLedger(reserved);
  assert.equal(sql.exec("SELECT tpd_used FROM routes WHERE route_id=?", routeId)[0].tpd_used, 2000);
  insertActiveBundle(sql, "tpd-b", "tok", now + 60000, now + 60000, now);
  sql.exec(`INSERT INTO jobs (id, idempotency_key, request_digest, policy_json, state, bundle_id,
    lease_route_id, lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
    payload_key, created_at, updated_at, token_reservation, reservation_rpd_day_key)
    VALUES ('tpd-j', 'tpd-k', 'd', '{}', 'leased', 'tpd-b', ?, 'lt', 'tags', 1000, 1000,
    'payload', ?, ?, 2000, ?)`, routeId, now, now, reserved.rpd_day_key);
  await coordinator.completeBatch('tpd-b', 'tok', [{ job_id: 'tpd-j', lease_token: 'lt',
    attempt_id: 'tpd-a', planned_at: now, outcome: 'success', provider_status_code: 200,
    observed_input_tokens: 800, observed_output_tokens: 200 }]);
  assert.equal(sql.exec("SELECT tpd_used FROM routes WHERE route_id=?", routeId)[0].tpd_used, 1000);
});

test("Orca prompt cap requeues and learns a size bound without blocking short work", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const routeId = 'orcarouter_zai_glm_5_3_flash_free';
  coordinator._getOrCreateRouteLedger(routeId, now, {});
  insertActiveBundle(sql, "cap-b", "tok", now + 60000, now + 60000, now);
  sql.exec(`INSERT INTO jobs (id, idempotency_key, request_digest, policy_json, state, bundle_id,
    lease_route_id, lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
    payload_key, created_at, updated_at, token_reservation)
    VALUES ('cap-j', 'cap-k', 'd', '{"allowed_models":["zai/glm-5.3-flash"]}', 'leased', 'cap-b',
    ?, 'lt', 'tags', 12000, 1000, 'payload', ?, ?, 13000)`, routeId, now, now);
  await coordinator.completeBatch('cap-b', 'tok', [{ job_id: 'cap-j', lease_token: 'lt',
    attempt_id: 'cap-a', planned_at: now, outcome: 'terminal_error', provider_status_code: 400,
    failure_class: 'free_prompt_cap' }]);
  assert.equal(sql.exec("SELECT state FROM jobs WHERE id='cap-j'")[0].state, 'queued');
  const route = sql.exec("SELECT * FROM routes WHERE route_id=?", routeId)[0];
  assert.equal(route.prompt_cap_estimate, 11999);
  assert.equal(route.blocked_until, null);
});


test("TPD feedback keeps remaining tokens and does not exhaust daily requests", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  const routeId = "groq_qwen_3_8_27b_primary";
  coordinator._getOrCreateRouteLedger(routeId, now, {});
  insertActiveBundle(sql, "tpd-feedback", "tok", now + 60000, now + 60000, now);
  sql.exec(`INSERT INTO jobs (id, idempotency_key, request_digest, policy_json, state, bundle_id,
    lease_route_id, lease_token, prompt_family, input_token_estimate, max_output_token_estimate,
    payload_key, created_at, updated_at)
    VALUES ('tpd-feedback-j', 'tpd-feedback-k', 'd', '{}', 'leased', 'tpd-feedback', ?, 'lt',
    'tags', 1000, 89, 'payload', ?, ?)`, routeId, now, now);
  const reply = await coordinator.authorizeRetry('tpd-feedback-j', 'lt', 'a', now,
    323.136, 'own_tpd', { limit: 200000, used: 199659 });
  assert.equal(reply.authorized, false);
  const ledger = sql.exec("SELECT * FROM routes WHERE route_id=?", routeId)[0];
  assert.equal(ledger.tpd_used, 180000 - 341);
  assert.equal(ledger.rpd_count, 0);
  assert.equal(ledger.blocked_until, now + 323136);
});

test("a coordinator from before attempt_usage drops its per-attempt journal on recreation", () => {
  const { storage, sql } = createMockSqlStorage();
  makeCoordinator({}, { storage, sql });
  sql.exec(`
    CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, job_id TEXT, created_at INTEGER);
    INSERT INTO attempts VALUES ('a1', 'j1', 1);
  `);
  const { coordinator } = makeCoordinator({}, { storage, sql });
  assert.equal(coordinator._inspectCurrentSchema().current, true);
  assert.equal(
    sql.exec("SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'attempts'")[0].n,
    0
  );
});

test("enqueue reserves the legacy-index row cost until that index is retired", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  assert.equal(coordinator._rowsPerIngressWriteUnit(), 1.25);
  sql.exec("CREATE UNIQUE INDEX idx_job_models_job_model ON job_models (job_id, model)");
  assert.equal(coordinator._rowsPerIngressWriteUnit(), 2);
  await coordinator.recountQueuedJobs(); // nothing legacy is queued, so the index is dropped
  assert.equal(coordinator._rowsPerIngressWriteUnit(), 1.25);
});

test("a failed attempt on a lease with no recorded route still completes", async () => {
  const { coordinator, sql } = makeCoordinator();
  const now = Date.now();
  await completeLeased(sql, coordinator, "b-noroute", [["nr1", "chapter-agenda", 100, null]], [{
    job_id: "nr1", attempt_id: "a-nr1", outcome: "terminal_error", provider_status_code: 400,
    actual_start_at: now, actual_end_at: now,
  }]);
  assert.equal(sql.exec("SELECT state FROM jobs WHERE id = 'nr1'")[0].state, "failed");
  assert.equal(
    sql.exec("SELECT route_id FROM route_failures WHERE failure_class = 'request_defect'")[0].route_id,
    "unknown"
  );
});


test("structural terminal fields are additive and legacy failed rows stay generic", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });
  for (const id of ["retired", "oversized", "legacy"]) {
    await coordinator.enqueueBatch([{
      id, idempotency_key: id, request_digest: id, policy_json: "{}", prompt_family: "tags",
      input_token_estimate: 1, max_output_token_estimate: 1, payload_key: `payloads/${id}.json`,
    }]);
  }
  sql.exec("UPDATE jobs SET state='failed', updated_at=100");
  sql.exec("UPDATE jobs SET terminal_reason='route_retired', terminal_catalog_digest='catalog' WHERE id='retired'");
  sql.exec("UPDATE jobs SET terminal_reason='unadmissible', terminal_catalog_digest='catalog' WHERE id='oversized'");
  const { statuses } = await coordinator.pollBatch(["retired", "oversized", "legacy"]);
  const { terminals } = await coordinator.terminalFeed(null);
  for (const rows of [statuses, terminals]) {
    assert.equal(rows.find(row => row.id === "retired").terminal_reason, "route_retired");
    assert.equal(rows.find(row => row.id === "oversized").terminal_reason, "unadmissible");
    assert.equal(rows.find(row => row.id === "legacy").terminal_reason, null);
    assert.equal(rows.find(row => row.id === "legacy").terminal_catalog_digest, null);
    assert.equal(rows.find(row => row.id === "retired").terminal_catalog_digest, "catalog");
  }
  assert.ok(statuses.every(row => row.error === "job_failed"));
});

test("terminal metadata migration preserves existing failed jobs without guessing reasons", async () => {
  const { coordinator, sql, storage } = makeCoordinator();
  await coordinator.enqueueBatch([{
    id: "old-failure", idempotency_key: "old-failure", request_digest: "digest",
    policy_json: "{}", prompt_family: "tags", input_token_estimate: 1,
    max_output_token_estimate: 1, payload_key: "old-payload",
  }]);
  sql.exec("UPDATE jobs SET state='failed' WHERE id='old-failure'");
  sql.exec("ALTER TABLE jobs DROP COLUMN terminal_reason");
  sql.exec("ALTER TABLE jobs DROP COLUMN terminal_catalog_digest");
  const rebooted = new LLMSchedulerDO({ storage }, withTestReservations({}));
  const { statuses } = await rebooted.pollBatch(["old-failure"]);
  assert.equal(statuses[0].state, "failed");
  assert.equal(statuses[0].error, "job_failed");
  assert.equal(statuses[0].terminal_reason, null);
  assert.equal(statuses[0].terminal_catalog_digest, null);
  assert.equal(sql.exec("SELECT payload_key FROM jobs WHERE id='old-failure'")[0].payload_key,
    "old-payload");
});

function rescueCatalog() {
  return {
    model_aliases: {}, model_routes_map: { primary: ["p"], backup: ["b"] },
    routes_by_id: {
      p: { provider: "test", free: true, input_context_limit: 10000, output_context_limit: 1000 },
      b: { provider: "test", free: true, input_context_limit: 10000, output_context_limit: 1000 },
    },
  };
}

function rescueJob(id, policy = { allowed_models: ["primary"], allow_paid: false }) {
  return {
    id, idempotency_key: id, request_digest: id, policy_json: JSON.stringify(policy),
    prompt_family: "tags", input_token_estimate: 100, max_output_token_estimate: 100,
    payload_key: `payloads/${id}.json`,
  };
}

test("catalog rescue is bounded, resumes by time/id and terminalizes old retired model indexes", async () => {
  const catalog = rescueCatalog();
  const { coordinator, sql } = makeCoordinator({
    DISPATCH_LIMITS_OVERRIDE: catalog, MAX_UNROUTABLE_RECONCILE_PER_TICK: "2",
    MAX_ACTIVE_BUNDLES: "0",
  });
  await coordinator.enqueueBatch(["a", "b", "c", "d", "leased"].map(id => rescueJob(id)));
  sql.exec("UPDATE jobs SET state='leased', lease_expires_at=? WHERE id='leased'", Date.now() + 600000);
  delete catalog.model_routes_map.primary;
  await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM jobs WHERE state='failed'")[0].n, 2);
  assert.equal(JSON.parse(sql.exec("SELECT catalog_rescue_cursor FROM scheduler")[0].catalog_rescue_cursor).after[1], "b");
  await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM jobs WHERE state='failed'")[0].n, 4);
  await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(sql.exec("SELECT catalog_rescue_complete FROM scheduler")[0].catalog_rescue_complete, 1);
  assert.equal(sql.exec("SELECT state FROM jobs WHERE id='leased'")[0].state, "leased");
  const rows = sql.exec("SELECT terminal_reason, terminal_catalog_digest FROM jobs WHERE state='failed'");
  assert.ok(rows.every(row => row.terminal_reason === "route_retired"));
  assert.ok(rows.every(row => /^[a-f0-9]{64}$/.test(row.terminal_catalog_digest)));
  assert.equal(sql.exec("SELECT COUNT(*) AS n FROM job_models WHERE job_id != 'leased'")[0].n, 0);
});

test("catalog rescue reindexes declared backups without changing provider attempt counters", async () => {
  const catalog = rescueCatalog();
  const { coordinator, sql } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0" });
  await coordinator.enqueueBatch([rescueJob("old-primary", {
    allowed_models: ["primary"], backup_models: ["backup"], backup_after_attempts: 12,
    allow_paid: false,
  })]);
  delete catalog.model_routes_map.primary;
  await coordinator.claimDispatchWindow(Date.now(), 30);
  const row = sql.exec("SELECT state, attempts, schema_retry_count, queue_models FROM jobs")[0];
  assert.equal(row.state, "queued");
  assert.equal(row.attempts, 0);
  assert.equal(row.schema_retry_count, 0);
  assert.deepEqual(JSON.parse(row.queue_models), ["backup"]);
});

test("catalog change restarts an unfinished rescue pass and transient fields do not change digest", async () => {
  const catalog = rescueCatalog();
  const { coordinator, sql } = makeCoordinator({
    DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0",
    MAX_UNROUTABLE_RECONCILE_PER_TICK: "1",
  });
  await coordinator.enqueueBatch([rescueJob("a"), rescueJob("b")]);
  await coordinator.claimDispatchWindow(Date.now(), 30);
  const digest = sql.exec("SELECT catalog_digest FROM scheduler")[0].catalog_digest;
  Object.assign(catalog.routes_by_id.p, { rpd: 0, blocked_until: Date.now() + 100000, rpm: 1 });
  assert.equal(await coordinator._structuralCatalogDigest(catalog), digest);
  catalog.routes_by_id.p.input_context_limit = 100;
  await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(JSON.parse(sql.exec("SELECT catalog_rescue_cursor FROM scheduler")[0].catalog_rescue_cursor).after[1], "a");
  assert.equal(sql.exec("SELECT terminal_reason FROM jobs WHERE id='a'")[0].terminal_reason, "unadmissible");
  assert.equal(sql.exec("SELECT state FROM jobs WHERE id='b'")[0].state, "queued");
});

test("rescue defers a whole page when its worst-case writes do not fit", async () => {
  const catalog = rescueCatalog();
  const { coordinator, sql } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0" });
  await coordinator.enqueueBatch([rescueJob("a")]);
  delete catalog.model_routes_map.primary;
  sql.exec("UPDATE scheduler SET rows_written_today=89995");
  await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(sql.exec("SELECT state FROM jobs")[0].state, "queued");
  assert.equal(sql.exec("SELECT catalog_rescue_cursor FROM scheduler")[0].catalog_rescue_cursor, null);
  sql.exec("UPDATE scheduler SET rows_written_today=0");
  await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(sql.exec("SELECT terminal_reason FROM jobs")[0].terminal_reason, "route_retired");
});

test("sentinel work enqueued after a completed catalog pass still becomes terminal", async () => {
  const catalog = rescueCatalog();
  const { coordinator, sql } = makeCoordinator({ DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0" });
  await coordinator.claimDispatchWindow(Date.now(), 30);
  await coordinator.enqueueBatch([rescueJob("new-oversized", { allowed_models: ["missing"] })]);
  await coordinator.claimDispatchWindow(Date.now(), 30);
  assert.equal(sql.exec("SELECT terminal_reason FROM jobs")[0].terminal_reason, "route_retired");
});


test("rescue resumes after recreation in time/id order and finishes despite new arrivals", async () => {
  const catalog = rescueCatalog();
  const env = { DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0",
    MAX_UNROUTABLE_RECONCILE_PER_TICK: "1" };
  const { coordinator, sql, storage } = makeCoordinator(env);
  const policy = { allowed_models: ["primary"], backup_models: ["backup"],
    backup_after_attempts: 12, allow_paid: false };
  await coordinator.enqueueBatch(["a", "b", "c"].map(id => rescueJob(id, policy)));
  sql.exec("UPDATE jobs SET updated_at=CASE id WHEN 'a' THEN 30 WHEN 'b' THEN 10 ELSE 20 END");
  delete catalog.model_routes_map.primary;
  await coordinator.claimDispatchWindow(Date.now(), 30);
  const first = JSON.parse(sql.exec("SELECT catalog_rescue_cursor FROM scheduler")[0].catalog_rescue_cursor);
  assert.deepEqual(first, { after: [10, "b"], through: [30, "a"] });
  assert.equal(sql.exec("SELECT updated_at FROM jobs WHERE id='b'")[0].updated_at, 10);
  assert.deepEqual(JSON.parse(sql.exec("SELECT queue_models FROM jobs WHERE id='b'")[0].queue_models), ["backup"]);
  await coordinator.enqueueBatch([rescueJob("new", policy)]);
  const restarted = makeCoordinator(env, { sql, storage }).coordinator;
  await restarted.claimDispatchWindow(Date.now(), 30);
  const second = JSON.parse(sql.exec("SELECT catalog_rescue_cursor FROM scheduler")[0].catalog_rescue_cursor);
  assert.deepEqual(second, { after: [20, "c"], through: [30, "a"] });
  await restarted.claimDispatchWindow(Date.now(), 30);
  await restarted.claimDispatchWindow(Date.now(), 30);
  assert.equal(sql.exec("SELECT catalog_rescue_complete FROM scheduler")[0].catalog_rescue_complete, 1);
  assert.ok(sql.exec("SELECT queue_models FROM jobs").every(row => row.queue_models === '["backup"]'));
  const plan = sql.exec(`EXPLAIN QUERY PLAN SELECT * FROM jobs WHERE state='queued'
    AND (updated_at, id) > (?, ?) AND (updated_at, id) <= (?, ?)
    ORDER BY updated_at, id LIMIT ?`, 10, "b", 30, "a", 1);
  assert.ok(plan.some(row => /SEARCH jobs USING INDEX idx_jobs_state_updated_id/.test(row.detail)));
  assert.ok(plan.every(row => !/SCAN jobs|TEMP B-TREE/.test(row.detail)));
});

for (const failureAt of ["mutation", "checkpoint"]) {
  test(`rescue rolls back ${failureAt} quota outage and resumes its committed page after restart`, async () => {
    const catalog = rescueCatalog();
    const env = { DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0",
      MAX_UNROUTABLE_RECONCILE_PER_TICK: "2" };
    const { coordinator, sql, storage } = makeCoordinator(env);
    await coordinator.enqueueBatch(["a", "b", "c", "d"].map(id => rescueJob(id)));
    delete catalog.model_routes_map.primary;
    await coordinator.claimDispatchWindow(Date.now(), 30);
    const before = sql.exec("SELECT * FROM scheduler")[0];
    const jobsBefore = sql.exec("SELECT * FROM jobs ORDER BY id");
    const indexesBefore = sql.exec("SELECT * FROM job_models ORDER BY job_id");
    const exec = sql.exec.bind(sql);
    let mutations = 0;
    sql.exec = (query, ...params) => {
      if (failureAt === "mutation" && /UPDATE jobs SET state='failed'/.test(query) && ++mutations === 2) {
        throw new Error("Durable Object row writes exceeded");
      }
      if (failureAt === "checkpoint" && /UPDATE scheduler SET rows_written_today =/.test(query) &&
          /catalog_rescue_cursor/.test(query)) throw new Error("Durable Object row writes exceeded");
      return exec(query, ...params);
    };
    await assert.rejects(coordinator.claimDispatchWindow(Date.now(), 30), /row writes exceeded/);
    sql.exec = exec;
    assert.deepEqual(sql.exec("SELECT * FROM jobs ORDER BY id"), jobsBefore);
    assert.deepEqual(sql.exec("SELECT * FROM job_models ORDER BY job_id"), indexesBefore);
    assert.deepEqual(sql.exec("SELECT * FROM scheduler")[0], before);
    assert.equal(coordinator._txSchedulerSets, null);
    // While the account remains unavailable even a read fails; no cursor may advance in memory.
    sql.exec = () => { throw new Error("Durable Object row writes exceeded"); };
    await assert.rejects(coordinator.claimDispatchWindow(Date.now(), 30), /row writes exceeded/);
    sql.exec = exec;
    const restarted = makeCoordinator(env, { sql, storage }).coordinator;
    await restarted.claimDispatchWindow(Date.now(), 30);
    await restarted.claimDispatchWindow(Date.now(), 30);
    assert.equal(sql.exec("SELECT COUNT(*) AS n FROM jobs WHERE state='failed'")[0].n, 4);
    assert.equal(sql.exec("SELECT queued_job_count, catalog_rescue_complete FROM scheduler")[0].queued_job_count, 0);
    assert.equal(sql.exec("SELECT catalog_rescue_complete FROM scheduler")[0].catalog_rescue_complete, 1);
  });
}


test("rescue keeps its checkpoint at the write stop and resumes after recreation and UTC reset", async t => {
  let now = Date.UTC(2026, 9, 8, 23, 59);
  t.mock.method(Date, "now", () => now);
  const catalog = rescueCatalog();
  const env = { DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0",
    MAX_UNROUTABLE_RECONCILE_PER_TICK: "1" };
  const { coordinator, sql, storage } = makeCoordinator(env);
  await coordinator.enqueueBatch(["a", "b"].map(id => rescueJob(id)));
  delete catalog.model_routes_map.primary;
  await coordinator.claimDispatchWindow(now, 30);
  const checkpoint = sql.exec("SELECT catalog_rescue_cursor FROM scheduler")[0].catalog_rescue_cursor;
  sql.exec("UPDATE scheduler SET rows_written_today=89995");
  await coordinator.claimDispatchWindow(now, 30);
  assert.equal(sql.exec("SELECT catalog_rescue_cursor FROM scheduler")[0].catalog_rescue_cursor, checkpoint);
  assert.equal(sql.exec("SELECT state FROM jobs WHERE id='b'")[0].state, "queued");
  now += 120000;
  const restarted = makeCoordinator(env, { sql, storage }).coordinator;
  await restarted.claimDispatchWindow(now, 30);
  await restarted.claimDispatchWindow(now, 30);
  assert.equal(sql.exec("SELECT state FROM jobs WHERE id='b'")[0].state, "failed");
  assert.equal(sql.exec("SELECT catalog_rescue_complete FROM scheduler")[0].catalog_rescue_complete, 1);
  assert.ok(sql.exec("SELECT rows_written_today FROM scheduler")[0].rows_written_today < 100);
});


test("immutable catalog digest cache avoids enumeration while mutable overrides remain checked", async t => {
  const { default: limits } = await import("../src/dispatch_limits.json", { with: { type: "json" } });
  const { coordinator } = makeCoordinator({});
  const digest = await coordinator._structuralCatalogDigest(limits);
  const entries = Object.entries;
  let enumerations = 0;
  t.mock.method(Object, "entries", value => {
    if ([limits.model_aliases, limits.model_routes_map, limits.routes_by_id].includes(value)) {
      enumerations += 1;
    }
    return entries(value);
  });
  for (let i = 0; i < 20; i += 1) assert.equal(await coordinator._structuralCatalogDigest(limits), digest);
  assert.equal(enumerations, 0);
  const override = rescueCatalog();
  const first = await coordinator._structuralCatalogDigest(override);
  override.routes_by_id.p.output_context_limit += 1;
  assert.notEqual(await coordinator._structuralCatalogDigest(override), first);
  // Switching through an override cannot invalidate the immutable import's identity cache.
  assert.equal(await coordinator._structuralCatalogDigest(limits), digest);
  assert.equal(enumerations, 0);
});

async function contextFixture(t, { provider = "groq", ids = ["probe"], tpm = 10000000 } = {}) {
  const previous = LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS;
  const output = LLMSchedulerDO.CONTEXT_OUTPUT_ENABLED;
  LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS = ids;
  LLMSchedulerDO.CONTEXT_OUTPUT_ENABLED = true;
  t.after(() => {
    LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS = previous;
    LLMSchedulerDO.CONTEXT_OUTPUT_ENABLED = output;
  });
  const routes = Object.fromEntries(ids.map(id => [id, {
    route_id: id, provider, account_id: "primary", upstream_model: id, model: id,
    free: true, rpm: 10000, rpd: 10000, tpm, input_context_limit: 10,
    output_context_limit: 1, hard_input_ceiling: 5,
  }]));
  const catalog = { providers: { [provider]: { accounts: [{ id: "primary", api_key_env: "KEY" }] } },
    routes_by_id: routes, model_routes_map: {}, model_aliases: {} };
  const env = { DISPATCH_LIMITS_OVERRIDE: catalog };
  const fixture = makeCoordinator(env);
  const now = Date.now();
  const common = { run_id: "12345", catalog_digest: await fixture.coordinator._contextCatalogDigest() };
  const start = { operation: "context_start", ...common };
  const admit = { operation: "context_admit", ...common, route_id: ids[0], dimension: "input",
    attempt_id: "a".repeat(64), request_digest: "b".repeat(64), input_tokens: 1000, output_tokens: 256 };
  await fixture.coordinator.pauseDispatch({ scope: "provider", target: provider, seconds: 900 }, now);
  return { ...fixture, env, catalog, now, start, admit };
}

test("context admission stays disabled without server activation, including RPC calls", async t => {
  const previous = LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS;
  LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS = [];
  t.after(() => { LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS = previous; });
  const { coordinator, sql } = makeCoordinator({});
  const digest = await coordinator._contextCatalogDigest();
  assert.deepEqual(await coordinator.reserveRouteRequests({ operation: "context_start",
    run_id: "1", catalog_digest: digest }), { ok: false, error: "disabled" });
  assert.deepEqual([...sql.exec("SELECT * FROM context_probe_weeks")], []);
});

test("context starts once, admits once, and retains charges across lost response and recreation", async t => {
  const { coordinator: c, sql, storage, env, now, start, admit } = await contextFixture(t);
  const opened = await c.reserveRouteRequests(start, now);
  assert.equal(opened.remaining_requests, 24);
  assert.equal(opened.deadline_ms, now + 3600000);
  const written = c._readRowsWrittenToday();
  assert.deepEqual(await c.reserveRouteRequests(start, now + 1000), opened);
  assert.equal(c._readRowsWrittenToday(), written);
  assert.deepEqual(await c.reserveRouteRequests(admit, now),
    { ok: true, disposition: "new", attempt_id: admit.attempt_id });
  const ledger = [...sql.exec("SELECT * FROM routes WHERE route_id='probe'")][0];
  assert.equal(ledger.rpd_count, 1);
  assert.equal(ledger.provisional_reservation, 0);
  assert.equal(ledger.tpm_reserved, 1256);
  const recreated = new LLMSchedulerDO({ storage }, withTestReservations(env));
  const before = recreated._readRowsWrittenToday();
  assert.deepEqual(await recreated.reserveRouteRequests(admit, now + 1000),
    { ok: false, error: "already_consumed" });
  assert.equal(recreated._readRowsWrittenToday(), before);
  assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")][0].input_used, 1000);
  assert.equal((await recreated.reserveRouteRequests({ ...admit, input_tokens: 1001 }, now)).error,
    "attempt_conflict");
  assert.equal((await recreated.reserveRouteRequests({ ...start, run_id: "2" }, now)).error,
    "session_expired");
  assert.equal((await recreated.reserveRouteRequests(start, now + 3600000)).error, "session_expired");
});

test("context uses full generation allowance and Google input-only trailing quota", async t => {
  const { coordinator: c, sql, now, start, admit } = await contextFixture(t, { provider: "gemini", tpm: 1500 });
  await c.reserveRouteRequests(start, now);
  assert.equal((await c.reserveRouteRequests({ ...admit, dimension: "output", input_tokens: 1000,
    output_tokens: 32768 }, now)).ok, true);
  const route = [...sql.exec("SELECT * FROM routes WHERE route_id='probe'")][0];
  assert.equal(route.tpm_reserved, 1000);
  assert.equal(JSON.parse(route.input_window_json)[0].tokens, 1000);
  const retry = { ...admit, attempt_id: "c".repeat(64) };
  assert.equal((await c.reserveRouteRequests(retry, now + 1)).error, "quota_wait");
  assert.equal((await c.reserveRouteRequests(retry, now + 61000)).ok, true);
  assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")][0].output_used, 33024);
});

test("context fences input/output ceilings, route counts, weekly totals and UTC rollover", async t => {
  const { coordinator: c, sql, now, start, admit } = await contextFixture(t, { ids: ["p1", "p2", "p3", "p4", "p5"] });
  await c.reserveRouteRequests(start, now);
  assert.equal((await c.reserveRouteRequests({ ...admit, input_tokens: 524289 }, now)).error,
    "budget_exhausted");
  for (let i = 0; i < 4; i++) {
    const result = await c.reserveRouteRequests({ ...admit, route_id: `p${i + 1}`,
      attempt_id: String(i + 1).repeat(64), input_tokens: 524288 }, now + i * 1000);
    assert.equal(result.ok, true);
  }
  assert.equal((await c.reserveRouteRequests({ ...admit, route_id: "p5" }, now + 5000)).error,
    "budget_exhausted");
  assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")][0].input_used, 2097152);
  const next = now + 7 * 86400000;
  assert.equal((await c.reserveRouteRequests({ ...start, run_id: "12346" }, next)).remaining_requests, 24);
  assert.equal(c._contextWeek(Date.UTC(2026, 9, 11, 23, 59)), "2026-10-05");
  assert.equal(c._contextWeek(Date.UTC(2026, 9, 12)), "2026-10-12");
});

test("context enforces per-route and weekly request ceilings independently", async t => {
  const { coordinator: c, now, start, admit } = await contextFixture(t, { ids: ["p1", "p2", "p3", "p4", "p5"] });
  await c.reserveRouteRequests(start, now);
  for (let i = 0; i < 24; i++) {
    const result = await c.reserveRouteRequests({ ...admit, route_id: `p${Math.floor(i / 6) + 1}`,
      attempt_id: i.toString(16).padStart(64, "0") }, now + i * 1000);
    assert.equal(result.ok, true);
    if (i === 5) assert.equal((await c.reserveRouteRequests({ ...admit,
      attempt_id: "f".repeat(64) }, now + 6000)).error, "budget_exhausted");
  }
  assert.equal((await c.reserveRouteRequests({ ...admit, route_id: "p5",
    attempt_id: "f".repeat(64) }, now + 24000)).error, "budget_exhausted");
});

test("context rejects stale catalog, shared scopes, paid/paused routes and short pauses", async t => {
  const { coordinator: c, catalog, now, start, admit } = await contextFixture(t);
  assert.equal((await c.reserveRouteRequests({ ...start, catalog_digest: "f".repeat(64) }, now)).error,
    "stale_catalog");
  await c.reserveRouteRequests(start, now);
  await c.resumeDispatch({ scope: "provider", target: "groq" }, now);
  assert.equal((await c.reserveRouteRequests(admit, now)).error, "pause_not_drained");
  await c.pauseDispatch({ scope: "provider", target: "groq", seconds: 200 }, now);
  assert.equal((await c.reserveRouteRequests(admit, now)).error, "pause_not_drained");
  catalog.routes_by_id.duplicate = { ...catalog.routes_by_id.probe, route_id: "duplicate" };
  assert.equal(c._contextQuotaScope(catalog.routes_by_id.probe, catalog), null);
  assert.equal(c._contextQuotaScope({ ...catalog.routes_by_id.probe, provider: "beatapi" }, catalog), null);
});

test("context admission rolls back every durable charge when any mutation fails", async t => {
  const { coordinator: c, sql, now, start, admit } = await contextFixture(t);
  await c.reserveRouteRequests(start, now);
  const original = sql.exec;
  for (const pattern of ["INSERT INTO routes", "UPDATE routes SET", "INSERT INTO context_probe_attempts",
    "UPDATE context_probe_weeks SET", "UPDATE scheduler SET"]) {
    sql.exec = (query, ...params) => {
      if (query.includes(pattern)) throw new Error("storage outage");
      return original(query, ...params);
    };
    await assert.rejects(c.reserveRouteRequests(admit, now), /storage outage/);
    sql.exec = original;
    assert.equal([...sql.exec("SELECT * FROM context_probe_attempts")].length, 0);
    assert.equal([...sql.exec("SELECT * FROM routes")].length, 0);
    assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")][0].requests_used, 0);
  }
  assert.equal((await c.reserveRouteRequests(admit, now)).ok, true);
});

test("context row-budget denials and status reads perform no bookkeeping writes", async t => {
  const { coordinator: c, sql, now, start, admit } = await contextFixture(t);
  await c.reserveRouteRequests(start, now);
  c.env.DO_ROWS_OPTIONAL_STOP = String(c._readRowsWrittenToday() + 9);
  const before = c._readRowsWrittenToday();
  assert.equal((await c.reserveRouteRequests(admit, now)).error, "daily_row_budget");
  const status = await c.dispatchPauseStatus({ scope: "provider", target: "groq", context: true }, now);
  assert.equal(status.context.session.remaining_requests, 24);
  assert.equal(c._readRowsWrittenToday(), before);
  assert.equal([...sql.exec("SELECT * FROM context_probe_attempts")].length, 0);
});

test("context retention prunes one bounded week and never resets a spent current session", async t => {
  const { coordinator: c, sql, now, start } = await contextFixture(t);
  for (let i = 8; i >= 1; i--) {
    const week = c._contextWeek(now - i * 7 * 86400000);
    sql.exec(`INSERT INTO context_probe_weeks
      (week_start, run_id, catalog_digest, deadline_ms) VALUES (?, '1', ?, 1)`,
    week, start.catalog_digest);
    for (let j = 0; j < 24; j++) sql.exec(`INSERT INTO context_probe_attempts
      VALUES (?, ?, '1', 'probe', 'input', ?, 1, 1, 1)`,
    `${i}:${j}`, week, "a".repeat(64));
  }
  assert.equal((await c.reserveRouteRequests(start, now)).ok, true);
  assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")].length, 8);
  assert.equal([...sql.exec("SELECT * FROM context_probe_attempts")].length, 168);
  assert.equal((await c.reserveRouteRequests(start, now + 3600000)).error, "session_expired");
});

test("context additive startup never rebuilds an existing queue", async t => {
  const { storage, sql, env } = await contextFixture(t);
  sql.exec("DROP TABLE context_probe_weeks; DROP TABLE context_probe_attempts;");
  const original = sql.exec;
  const queries = [];
  sql.exec = (query, ...args) => { queries.push(query); return original(query, ...args); };
  new LLMSchedulerDO({ storage }, withTestReservations(env));
  assert.equal(queries.some(query => /CREATE TABLE.*jobs|UPDATE jobs|INSERT INTO job_models/s.test(query)), false);
  assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")].length, 0);
});


test("context clusters weekly attempts and fences expired run reuse across week cleanup", async t => {
  const { coordinator: c, sql, now, start } = await contextFixture(t);
  await c.reserveRouteRequests(start, now);
  const next = now + 10 * 7 * 86400000;
  const before = c._readRowsWrittenToday();
  assert.equal((await c.reserveRouteRequests(start, next)).error, "session_expired");
  assert.equal(c._readRowsWrittenToday(), before);
  assert.equal((await c.reserveRouteRequests({ ...start, run_id: "12344" }, next)).error,
    "session_expired");
  assert.equal((await c.reserveRouteRequests({ ...start, run_id: "12346" }, next)).ok, true);
  assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")].length, 1);
  const plan = [...sql.exec(`EXPLAIN QUERY PLAN SELECT route_id FROM context_probe_attempts
    WHERE week_start = ? LIMIT 25`, c._contextWeek(next))];
  assert.match(plan[0].detail, /SEARCH context_probe_attempts USING PRIMARY KEY/);
  const schema = [...sql.exec("SELECT sql FROM sqlite_master WHERE name='context_probe_attempts'")][0];
  assert.match(schema.sql, /WITHOUT ROWID/);
});

test("context denies weekly output exhaustion and undrained providers without charging", async t => {
  const { coordinator: c, sql, now, start, admit } = await contextFixture(t);
  await c.reserveRouteRequests(start, now);
  const counts = t.mock.method(c, "_inFlightCounts", () => ({ by_provider: { groq: 1 } }));
  assert.equal((await c.reserveRouteRequests(admit, now)).error, "pause_not_drained");
  assert.equal([...sql.exec("SELECT * FROM context_probe_attempts")].length, 0);
  counts.mock.restore();
  for (let i = 0; i < 4; i++) assert.equal((await c.reserveRouteRequests({ ...admit,
    dimension: "output", output_tokens: 32768, attempt_id: String(i).repeat(64) }, now + i * 1000)).ok,
  true);
  assert.equal((await c.reserveRouteRequests({ ...admit, dimension: "output", output_tokens: 32768 },
    now + 4000)).error, "budget_exhausted");
  assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")][0].output_used, 131072);
});

test("context optional thresholds cannot override account row exhaustion", async t => {
  const { coordinator: c, sql, now, start, admit } = await contextFixture(t);
  await c.reserveRouteRequests(start, now);
  c.env.DO_ROWS_OPTIONAL_STOP = "1000000";
  sql.exec("UPDATE scheduler SET rows_written_today=99999");
  const before = c._readRowsWrittenToday();
  assert.equal((await c.reserveRouteRequests(admit, now)).error, "daily_row_budget");
  assert.equal(c._readRowsWrittenToday(), before);
  assert.equal([...sql.exec("SELECT * FROM context_probe_attempts")].length, 0);
});

test("context paid, paused and unknown shared scopes fail closed", async t => {
  for (const [kind, error] of [["paid", "unknown_target"], ["paused", "unknown_target"],
    ["shared", "quota_unknown"]]) await t.test(kind, async sub => {
    const { coordinator: c, catalog, now, start, admit } = await contextFixture(sub);
    if (kind === "paid") catalog.routes_by_id.probe.free = false;
    if (kind === "paused") catalog.routes_by_id.probe.rpd = 0;
    if (kind === "shared") catalog.providers.groq.rpm = 100;
    const digest = await c._contextCatalogDigest();
    await c.reserveRouteRequests({ ...start, catalog_digest: digest }, now);
    assert.equal((await c.reserveRouteRequests({ ...admit, catalog_digest: digest }, now)).error,
      error);
  });
});


test("reviewed input pilot admits six bounded calls and excludes output and other routes", async t => {
  const rid = "groq_gpt_oss_120b_primary";
  assert.deepEqual(LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS, [rid]);
  assert.equal(LLMSchedulerDO.CONTEXT_OUTPUT_ENABLED, false);
  const { coordinator: c, now, start, admit, sql } = await contextFixture(t, { ids: [rid] });
  LLMSchedulerDO.CONTEXT_OUTPUT_ENABLED = false;
  assert.equal((await c.reserveRouteRequests(start, now)).ok, true);
  assert.equal((await c.reserveRouteRequests({ ...admit, input_tokens: 8193 }, now)).error,
    "budget_exhausted");
  assert.equal((await c.reserveRouteRequests({ ...admit, dimension: "output" }, now)).error,
    "disabled");
  assert.equal((await c.reserveRouteRequests({ ...admit, route_id: "other" }, now)).error,
    "disabled");
  for (let i = 0; i < 6; i++) {
    assert.equal((await c.reserveRouteRequests({ ...admit, input_tokens: 8192,
      attempt_id: i.toString(16).padStart(64, "0") }, now + i * 1000)).ok, true);
  }
  assert.equal((await c.reserveRouteRequests({ ...admit,
    attempt_id: "f".repeat(64) }, now + 6000)).error, "budget_exhausted");
  const row = [...sql.exec("SELECT * FROM context_probe_weeks")][0];
  assert.equal(row.requests_used, 6);
  assert.equal(row.input_used, 6 * 8192);
  assert.equal(row.output_used, 6 * 256);
});

test("16k Groq ceiling is manual-only; scheduled probes remain capped at 8192", async t => {
  const rid = "groq_gpt_oss_120b_primary";
  const { coordinator: c, now, start, admit } = await contextFixture(t, {
    ids: [rid], tpm: 10000000,
  });
  assert.equal(LLMSchedulerDO.CONTEXT_INPUT_CEILINGS[rid], 8192);
  assert.equal(LLMSchedulerDO.CONTEXT_MANUAL_INPUT_CEILINGS[rid], 16384);
  const status = await c._contextStatus(now, [rid]);
  assert.equal(status.input_ceilings[rid], 8192);
  assert.equal(status.manual_input_ceilings[rid], 16384);

  await c.reserveRouteRequests(start, now);
  assert.equal((await c.reserveRouteRequests({ ...admit, input_tokens: 8193 }, now)).error,
    "budget_exhausted");

  const manual = { ...start, operation: "context_manual_start", route_ids: [rid],
    dimensions: ["input"], max_requests: 1, max_input: 16384, max_output: 256,
    per_call_input: 16384, per_call_output: 256, purpose: "#2221 manual ceiling test" };
  assert.equal((await c.reserveRouteRequests(manual, now)).ok, true);
  assert.equal((await c.reserveRouteRequests({ ...admit, operation: "context_manual_admit",
    input_tokens: 16384 }, now)).ok, true);
  const oversized = { ...admit, operation: "context_manual_admit",
    attempt_id: "c".repeat(64), input_tokens: 16385 };
  assert.equal((await c.reserveRouteRequests(oversized, now)).error, "budget_exhausted");
});

function manualStart(start, overrides = {}) {
  return { ...start, operation: "context_manual_start", route_ids: ["probe"],
    dimensions: ["input"], max_requests: 8, max_input: 2097152, max_output: 131072,
    per_call_input: 8192, per_call_output: 256, purpose: "#2221 offline test", ...overrides };
}

test("manual context reruns retain charges, fence old runners and isolate scheduled pool", async t => {
  const { coordinator: c, sql, storage, env, now, start, admit } = await contextFixture(t);
  const opened = manualStart(start, { max_requests: 2 });
  assert.equal((await c.reserveRouteRequests(opened, now)).remaining_requests, 2);
  const call = { ...admit, operation: "context_manual_admit" };
  assert.equal((await c.reserveRouteRequests(call, now)).ok, true);
  assert.equal((await c.reserveRouteRequests(call, now)).error, "already_consumed");
  assert.equal((await c.reserveRouteRequests({ ...opened, run_id: "12346" }, now)).error,
    "session_expired");
  assert.equal((await c.reserveRouteRequests({ ...call, attempt_id: "c".repeat(64) }, now)).ok, true);
  assert.equal((await c.reserveRouteRequests({ ...call, attempt_id: "d".repeat(64) }, now)).error,
    "budget_exhausted");
  const finished = await c.reserveRouteRequests({ ...start, operation: "context_manual_finish" }, now + 1);
  assert.equal(finished.weekly_requests_used, 2);
  assert.equal(finished.remaining_requests, 0);
  const recreated = new LLMSchedulerDO({ storage }, withTestReservations(env));
  const later = { ...opened, run_id: "12346" };
  assert.equal((await recreated.reserveRouteRequests(later, now + 2)).remaining_requests, 2);
  assert.equal((await recreated.reserveRouteRequests({ ...call, attempt_id: "d".repeat(64) }, now + 2)).error,
    "session_expired");
  assert.equal((await recreated.reserveRouteRequests({ ...call, run_id: "12346",
    attempt_id: "d".repeat(64) }, now + 2)).ok, true);
  const row = [...sql.exec("SELECT * FROM context_manual_weeks")][0];
  assert.equal(row.requests_used, 3);
  assert.equal(row.input_used, 3000);
  assert.equal(row.run_requests_used, 1);
  assert.equal([...sql.exec("SELECT * FROM context_probe_weeks")].length, 0);
  assert.equal((await recreated.reserveRouteRequests(start, now + 2)).remaining_requests, 24);
});

test("manual context enforces per-route 12 and weekly 24 without refunds or pool borrowing", async t => {
  const { coordinator: c, sql, now, start, admit } = await contextFixture(t, { ids: ["probe", "other"] });
  for (let run = 0; run < 4; run++) {
    const run_id = String(12345 + run);
    const route_id = run < 2 ? "probe" : "other";
    const open = manualStart({ ...start, run_id }, { route_ids: [route_id] });
    assert.equal((await c.reserveRouteRequests(open, now + run * 100)).ok, true);
    for (let i = 0; i < 6; i++) assert.equal((await c.reserveRouteRequests({ ...admit,
      operation: "context_manual_admit", run_id, route_id,
      attempt_id: String(run * 6 + i).padStart(64, "0") }, now + run * 100)).ok, true);
    if (run === 1) assert.equal((await c.reserveRouteRequests({ ...admit,
      operation: "context_manual_admit", run_id, route_id, attempt_id: "f".repeat(64) }, now + 100)).error,
      "budget_exhausted");
    await c.reserveRouteRequests({ ...start, run_id, operation: "context_manual_finish" }, now + run * 100);
  }
  assert.equal([...sql.exec("SELECT * FROM context_manual_weeks")][0].requests_used, 24);
  const open = manualStart({ ...start, run_id: "12349" }, { route_ids: ["other"] });
  assert.equal((await c.reserveRouteRequests(open, now + 400)).remaining_requests, 0);
  assert.equal((await c.reserveRouteRequests({ ...admit, operation: "context_manual_admit",
    run_id: "12349", route_id: "other", attempt_id: "f".repeat(64) }, now + 400)).error,
    "budget_exhausted");
});

test("manual context denies unknown selections, invalid limits and unsupported authority before writes", async t => {
  const { coordinator: c, sql, now, start } = await contextFixture(t);
  for (const [change, error] of [
    [{ route_ids: ["unknown"] }, "disabled"],
    [{ max_requests: 9 }, "invalid_request"],
    [{ max_input: 2097153 }, "invalid_request"],
    [{ route_ids: ["probe", "probe"] }, "invalid_request"],
    [{ dimensions: ["input", "input"] }, "invalid_request"],
    [{ per_call_output: 257 }, "budget_exhausted"],
  ]) assert.equal((await c.reserveRouteRequests(manualStart(start, change), now)).error, error);
  assert.equal([...sql.exec("SELECT * FROM context_manual_weeks")].length, 0);
});

test("manual session token ceilings, finish idempotency and expiry preserve weekly totals", async t => {
  const { coordinator: c, now, start, admit, sql } = await contextFixture(t);
  const opened = manualStart(start, { max_input: 1000, max_output: 256 });
  assert.equal((await c.reserveRouteRequests(opened, now)).ok, true);
  const call = { ...admit, operation: "context_manual_admit" };
  assert.equal((await c.reserveRouteRequests({ ...call, input_tokens: 1001 }, now)).error,
    "budget_exhausted");
  assert.equal((await c.reserveRouteRequests(call, now)).ok, true);
  assert.equal((await c.reserveRouteRequests({ ...call, attempt_id: "c".repeat(64) }, now)).error,
    "budget_exhausted");
  const finish = { ...start, operation: "context_manual_finish" };
  assert.equal((await c.reserveRouteRequests(finish, now + 1)).ok, true);
  const writes = c._readRowsWrittenToday();
  assert.equal((await c.reserveRouteRequests(finish, now + 2)).ok, true);
  assert.equal(c._readRowsWrittenToday(), writes);
  assert.equal((await c.reserveRouteRequests(opened, now + 2)).error, "session_expired");
  await c.reserveRouteRequests({ ...opened, run_id: "12346" }, now + 2);
  assert.equal((await c.reserveRouteRequests({ ...opened, run_id: "12347" }, now + 3600003)).ok,
    true);
  assert.equal([...sql.exec("SELECT * FROM context_manual_weeks")][0].requests_used, 1);
  const next = await c.reserveRouteRequests({ ...opened, run_id: "12348" }, now + 7 * 86400000);
  assert.equal(next.weekly_requests_used, 0);
  assert.equal([...sql.exec("SELECT * FROM context_manual_weeks")].length, 2);
});
