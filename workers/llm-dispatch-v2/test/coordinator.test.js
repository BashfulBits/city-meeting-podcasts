import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import { createMockSqlStorage, withTestReservations } from "./helpers.js";

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

test("enqueueBatch indexes every canonical allowed model without model_routing", async () => {
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
    "future/provider-model",
    "gemini/gemini-3.1-flash-lite",
    "mistral/mistral-small-2603",
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

test("resolveUnknownBatch reports known and unknown attempt ids, chunked past 100", async () => {
  const { coordinator, sql } = makeCoordinator({ MAX_JOBS_PER_UTC_DAY: "100" });

  const knownIds = Array.from({ length: 5 }, (_, i) => `attempt-${i}`);
  for (const attemptId of knownIds) {
    sql.exec(
      `INSERT INTO attempts (attempt_id, job_id, route_id, planned_at, start_state, created_at)
       VALUES (?, 'job-x', 'route-x', 0, 'planned', 0)`,
      attemptId
    );
  }

  const unknownIds = Array.from({ length: 150 }, (_, i) => `missing-${i}`);
  const res = await coordinator.resolveUnknownBatch([...knownIds, ...unknownIds]);

  assert.deepEqual(res.resolved.sort(), knownIds.sort());
  assert.equal(res.not_found.length, unknownIds.length);
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
  sql.exec(
    "INSERT INTO bundles (bundle_id, execution_token, state, lease_expires_at, dispatch_window_end, created_at) VALUES ('b1', 'tok', 'active', ?, ?, ?)",
    bundleDeadline,
    bundleDeadline,
    now
  );
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
