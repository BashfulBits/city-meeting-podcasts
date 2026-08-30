import test from "node:test";
import assert from "node:assert/strict";
import { LLMSchedulerDO } from "../src/coordinator.js";
import {
  createRecordingSqlStorage,
  estimateRowsRead,
  GROWABLE_TABLES,
} from "./helpers.js";

/**
 * Guards against the bug class behind the 2026-08-27 Durable Objects free-tier overage: a
 * statement whose cost scales with how much history the DO has accumulated rather than with how
 * much work the tick actually does. There, `bundles` had no index on `state`, so two statements
 * in every cron tick full-scanned it, and nothing ever deleted from it -- 6,400 of a tick's
 * 6,424 rows read at 3,200 accumulated bundles.
 *
 * Two invariants, both applied to every RPC entry point rather than to a hand-picked query list,
 * so a NEW method with the same defect is caught without anyone remembering to add a case here:
 *
 *   1. No statement may SCAN a growable table (jobs/job_models/bundles/attempts).
 *      `routes` and `scheduler` are exempt: both are bounded by static config, not by traffic.
 *
 *   2. Scale invariance -- the same operations against 10x the accumulated history must read
 *      about the same number of rows. This is the stronger check, because it does not depend on
 *      anyone predicting which query shape goes wrong: anything O(history) fails it.
 */

const CATALOG = {
  model_aliases: {},
  model_routes_map: { "gemini/gemini-flash-lite": ["route-a"], "mistral/mistral-small": ["route-c"] },
  routes_by_id: {
    "route-a": { provider: "gemini", upstream_model: "g", rpm: 15, rpd: 500, tpm: 250000, free: true,
      input_context_limit: 1048576, output_context_limit: 65536 },
    "route-c": { provider: "mistral", upstream_model: "m", rpm: 60, rpd: 10000, tpm: 500000, free: true,
      input_context_limit: 32000, output_context_limit: 8000 },
  },
};

/**
 * A DO carrying `history` worth of accumulated state, plus a fixed-size live working set. Only
 * the history varies between scales -- the actionable work does not -- so any growth in rows read
 * is unambiguously history-driven.
 *
 * History spans every table that grows in production, not just the terminal ones: an undrained
 * queued backlog grows `jobs` AND `job_models`, so the candidate-lookup path is covered too.
 */
function seed(history, { liveQueued = 6 } = {}) {
  const { storage, db, recorder } = createRecordingSqlStorage();
  const env = {
    MAX_JOBS_PER_UTC_DAY: "100000",
    MAX_BUNDLE_JOBS: "4",
    MAX_JOBS_PER_ROUTE_PER_BUNDLE: "4",
    MAX_CONCURRENT_ROUTE_LANES: "5",
    MAX_ACTIVE_BUNDLES: "2",
    MAX_IN_FLIGHT_LLM_CALLS: "8",
    LEASE_DURATION_SECONDS: "840",
    ESTIMATED_CALL_DURATION_CEILING_SECONDS: "5",
    // Retention off: this test is about read cost at a given table size, and letting the prune
    // delete the very history being measured would mask a regression.
    MAX_BUNDLE_PRUNE_PER_TICK: "0",
    MAX_ATTEMPT_PRUNE_PER_TICK: "0",
    DISPATCH_LIMITS_OVERRIDE: CATALOG,
  };
  const coordinator = new LLMSchedulerDO({ storage }, env);
  const now = Date.now();
  const old = now - 90 * 86_400_000;

  db.exec("BEGIN");
  // Column-explicit on purpose: a positional INSERT here breaks every time the jobs table gains
  // a column, which says nothing about the rows-read behaviour this file exists to pin.
  const JOB_COLUMNS = [
    "id", "idempotency_key", "request_digest", "provider_idempotency_key", "state", "priority",
    "policy_json", "prompt_family", "input_token_estimate", "max_output_token_estimate",
    "payload_key", "result_key", "lease_token", "lease_route_id", "lease_expires_at",
    "bundle_id", "attempts", "transient_retry_count", "created_at", "updated_at",
  ];
  const insJob = db.prepare(
    `INSERT INTO jobs (${JOB_COLUMNS.join(",")}) VALUES (${JOB_COLUMNS.map(() => "?").join(",")})`
  );
  const insBundle = db.prepare("INSERT INTO bundles VALUES (?,?,?,?,?,?,?)");
  const insAttempt = db.prepare(
    "INSERT INTO attempts (attempt_id, job_id, route_id, planned_at, start_state, created_at)" +
      " VALUES (?,?,?,?,'started',?)"
  );
  const policy = JSON.stringify({ allowed_models: ["gemini/gemini-flash-lite"], allow_paid: false });
  const insJobModel = db.prepare("INSERT INTO job_models VALUES (?,?,?,?)");
  for (let i = 0; i < history; i++) {
    insJob.run(`h${i}`, `kh${i}`, "d", null, "completed", 1, policy, "tags", 500, 200,
      `payloads/h${i}.json`, `results/h${i}.json`, null, null, null, null, 1, 0, old, old);
    insBundle.run(`hb${i}`, "tok", "completed", old, 0, old, old);
    insAttempt.run(`ha${i}`, `h${i}`, "route-a", old, old);
    // An undrained queued backlog: grows both `jobs` and its `job_models` work index, which is
    // what the per-model candidate lookup walks.
    insJob.run(`bk${i}`, `kbk${i}`, "d", null, "queued", 1, policy, "tags", 500, 200,
      `payloads/bk${i}.json`, null, null, null, null, null, 0, 0, old + i, old + i);
    insJobModel.run(`bk${i}`, "gemini/gemini-flash-lite", 1, old + i);
  }
  db.exec("COMMIT");

  // Live working set, identical at every scale.
  const jobs = Array.from({ length: liveQueued }, (_, i) => ({
    id: `live-${i}`, idempotency_key: `klive-${i}`, request_digest: `d${i}`, policy_json: policy,
    prompt_family: "tags", input_token_estimate: 500, max_output_token_estimate: 200,
    payload_key: `payloads/live-${i}.json`,
  }));
  return { coordinator, db, recorder, env, now, jobs };
}

/** Run the full RPC surface once, returning per-operation rows read and every statement seen. */
async function exerciseAll(fixture) {
  const { coordinator, db, recorder, now, jobs } = fixture;
  const perOp = {};
  const all = [];

  const run = async (label, fn) => {
    recorder.reset();
    const value = await fn();
    const statements = recorder.statements.slice();
    perOp[label] = statements.reduce((sum, s) => sum + estimateRowsRead(db, s), 0);
    all.push(...statements);
    return value;
  };

  recorder.start();
  await run("enqueueBatch", () => coordinator.enqueueBatch(jobs));
  const plan = await run("claimDispatchWindow", () => coordinator.claimDispatchWindow(now, 25));
  const claimed = plan.jobs[0];
  await run("pollBatch", () => coordinator.pollBatch(jobs.map((j) => j.id)));
  await run("attemptStarted", () =>
    coordinator.attemptStarted(claimed.id, claimed.lease_token, "att-1", now));
  await run("authorizeRetry", () =>
    coordinator.authorizeRetry(claimed.id, claimed.lease_token, "att-1", now));
  await run("completeBatch", () =>
    coordinator.completeBatch(plan.bundle_id, plan.execution_token,
      plan.jobs.map((job, i) => ({
        job_id: job.id, lease_token: job.lease_token, attempt_id: `done-${i}`,
        planned_at: job.not_before_at, actual_start_at: now, actual_end_at: now,
        observed_input_tokens: 500, observed_output_tokens: 100, outcome: "success",
        provider_status_code: 200, result_key: `results/${job.id}.json`,
      }))));
  await run("resolveUnknownBatch", () => coordinator.resolveUnknownBatch(["att-1", "nope"]));
  await run("confirmNeverAccepted", () => coordinator.confirmNeverAccepted(["live-0", "nope"]));
  const purge = await run("purgePendingBatch", () => coordinator.purgePendingBatch(20));
  await run("confirmPurge", () => coordinator.confirmPurge(purge.jobs.map((j) => j.id)));
  await run("pruneTerminalRecords", async () => coordinator._pruneTerminalRecords(now));

  return { perOp, all, total: Object.values(perOp).reduce((a, b) => a + b, 0) };
}

test("no coordinator statement ever full-scans a table that grows with traffic", async () => {
  const fixture = seed(400);
  const { all } = await exerciseAll(fixture);
  assert.ok(all.length > 30, "expected the RPC surface to issue a meaningful number of statements");

  const offenders = [];
  for (const statement of all) {
    let plan;
    try {
      plan = fixture.db.prepare(`EXPLAIN QUERY PLAN ${statement.query}`).all(...statement.params);
    } catch {
      continue;
    }
    for (const { detail } of plan) {
      const scan = detail.match(/^SCAN (\w+)/);
      if (scan && GROWABLE_TABLES.includes(scan[1])) {
        offenders.push(`${detail}  <-  ${statement.query.slice(0, 110)}`);
      }
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "a statement full-scans a growable table; add an index or bound the predicate:\n" +
      offenders.join("\n")
  );
});

test("rows read per operation does not grow with accumulated history", async () => {
  const small = await exerciseAll(seed(200));
  const large = await exerciseAll(seed(2000)); // 10x the history, same live working set

  const regressions = [];
  for (const [op, smallRows] of Object.entries(small.perOp)) {
    const largeRows = large.perOp[op];
    // Allow a small absolute slack for incidental variation, but nothing proportional to the
    // 10x history growth: an O(history) statement blows past this immediately.
    if (largeRows > smallRows + 20) {
      regressions.push(`${op}: ${smallRows} rows at 200 history -> ${largeRows} at 2000`);
    }
  }
  assert.deepEqual(
    regressions,
    [],
    "rows read scaled with accumulated history (the 2026-08-27 overage shape):\n" +
      regressions.join("\n")
  );

  // Whole-surface guard too, so cost cannot be shuffled between operations.
  assert.ok(
    large.total <= small.total + 40,
    `total rows read scaled with history: ${small.total} -> ${large.total}`
  );
});
