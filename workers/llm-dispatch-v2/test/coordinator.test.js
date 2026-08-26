import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import { createMockSqlStorage } from "./helpers.js";

function makeCoordinator(env, { sql, storage } = createMockSqlStorage()) {
  return { coordinator: new LLMSchedulerDO({ storage }, env), sql, storage };
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

test("enqueueBatch handles idempotent replays and detects conflicts", async () => {
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

  // Replay with different digest: rejected with idempotency_conflict
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
