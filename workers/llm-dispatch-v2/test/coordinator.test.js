import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { LLMSchedulerDO } from "../src/coordinator.js";

function createMockSqlStorage() {
  const db = new DatabaseSync(":memory:");

  return {
    exec(query, ...params) {
      const trimmed = query.trim();
      if (
        trimmed.startsWith("SELECT") ||
        trimmed.startsWith("select")
      ) {
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

test("LLMSchedulerDO initializes schema and scheduler row", () => {
  const sql = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage: { sql } }, { MAX_JOBS_PER_UTC_DAY: "100" });

  const sched = [...sql.exec("SELECT * FROM scheduler WHERE id = 1")];
  assert.equal(sched.length, 1);
  assert.equal(sched[0].jobs_ingested_today, 0);
});

test("enqueueBatch admits new jobs and updates scheduler counter", async () => {
  const sql = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage: { sql } }, { MAX_JOBS_PER_UTC_DAY: "100" });

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
  assert.deepEqual(res.accepted, [{ id: "j1" }, { id: "j2" }]);
  assert.deepEqual(res.rejected, []);

  const sched = [...sql.exec("SELECT jobs_ingested_today FROM scheduler WHERE id = 1")];
  assert.equal(sched[0].jobs_ingested_today, 2);

  const rows = [...sql.exec("SELECT id, state, priority FROM jobs ORDER BY id")];
  assert.equal(rows.length, 2);
  assert.equal(rows[0].state, "queued");
  assert.equal(rows[0].priority, 1);
  assert.equal(rows[1].priority, 0);
});

test("enqueueBatch handles idempotent replays and detects conflicts", async () => {
  const sql = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage: { sql } }, { MAX_JOBS_PER_UTC_DAY: "100" });

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

  // Replay with identical digest: accepted with existing id
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
  assert.deepEqual(replay.accepted, [{ id: "j1" }]);
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

test("enqueueBatch enforces daily cap with partial admission", async () => {
  const sql = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage: { sql } }, { MAX_JOBS_PER_UTC_DAY: "2" });

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

  assert.deepEqual(res.accepted, [{ id: "j1" }, { id: "j2" }]);
  assert.deepEqual(res.rejected, [{ id: "j3", reason: "daily_cap_exceeded" }]);
});

test("pollBatch returns statuses and omits absent IDs", async () => {
  const sql = createMockSqlStorage();
  const coordinator = new LLMSchedulerDO({ storage: { sql } }, { MAX_JOBS_PER_UTC_DAY: "100" });

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
