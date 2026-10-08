import { LLMSchedulerDO } from "../../src/coordinator.js";
import { DurableObject } from "cloudflare:workers";

const NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b:free";
const GEMMA = "google/gemma-4-31b-it";
const GEMMA_26B = "google/gemma-4-26b-a4b-it";

function job(i, purpose, model, models) {
  return {
    id: `job-${purpose}-${i}`,
    idempotency_key: `idem-${purpose}-${i}`,
    request_digest: `digest-${i}`,
    provider_idempotency_key: null,
    policy_json: JSON.stringify({ allowed_models: models || [model], allow_paid: false, purpose }),
    prompt_family: purpose === "chapter-agenda" ? "agenda-item-extract" : "tag",
    input_token_estimate: 2000,
    max_output_token_estimate: 1000,
    payload_key: `payloads/job-${purpose}-${i}/request.json`,
    priority: 1,
  };
}

/** The table a write statement targets, for per-table attribution ("other" for DDL batches). */
function writtenTable(query) {
  const match = /^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+(\w+)/i
    .exec(query || "");
  return match ? match[1] : "other";
}

/** Add each key's count in `source` to `target` (per-table billed-row tallies). */
const addInto = (target, source) => {
  for (const [key, value] of Object.entries(source)) target[key] = (target[key] || 0) + value;
};

/**
 * The review/49 packed-consensus lane mix at 1/10 of its 800-meetings/day volume (capacity
 * evidence, 2026-10-02), on today's lanes and routes. Pinned (`per_model`) lanes index one model
 * per job; pooled lanes index each model in the pool. The sibling and adjudicator pools are
 * proposed, so two-model stand-ins on existing lanes play them.
 */
const CONSENSUS_MIX = [
  { lane: "agenda", purpose: "chapter-agenda", n: 48,
    models: [NEMOTRON, "tencent/hy3", "gemini/gemini-3.1-flash-lite"] },
  { lane: "locator (pinned)", purpose: "chapter-locator", n: 48,
    models: ["deepseek/deepseek-v4-flash"] },
  { lane: "tagger", purpose: "topic-tags:tagger", n: 66,
    models: ["gemini/gemini-3.1-flash-lite", "kilo/stepfun/step-3.7-flash:free", "deepseek/deepseek-v4-flash"] },
  { lane: "moments (4-model pool)", purpose: "r6-moments", n: 10,
    models: ["zai/glm-5.3-flash", "moonshotai/kimi-k3", "gemini/gemini-3.8-flash", "gemini/gemini-3.5-flash-lite"] },
  { lane: "anchor judge (pinned)", purpose: "r6-judge", n: 19, models: ["qwen/qwen3.8-27b"] },
  { lane: "sibling judge (2-model)", purpose: "r6-judge", n: 37,
    models: [GEMMA_26B, "gemini/gemini-3.6-flash"] },
  { lane: "adjudicator (2-model)", purpose: "tournament:tag", n: 19,
    models: ["zai/glm-4.7-flash", "deepseek/deepseek-v4.1-flash"] },
];

/** Lifecycle totals: `lifecycle_w` is what one job costs while it is live; `total_w` adds the
 * deferred retention prune. Both are the per-PR comparison figures in README.md. */
function summarize(out, n) {
  const lifecycle = ["enqueue", "claim", "attempt", "complete", "retire", "ack"]
    .reduce((sum, phase) => sum + (out[phase]?.w || 0), 0)
    + (out.purge ? out.purge.purgePendingBatchW + out.purge.confirmPurgeW : 0);
  const total = lifecycle + (out.prune_w || 0);
  return {
    ...out,
    lifecycle_w: lifecycle,
    total_w: total,
    per_job: { lifecycle: +(lifecycle / n).toFixed(2), total: +(total / n).toFixed(2) },
  };
}

export class MeasureDO extends LLMSchedulerDO {
  _getSql() {
    if (!this._wrapped) {
      const real = this.sql;
      this._cursors = [];
      this._cursorSql = [];
      const cursors = this._cursors;
      const texts = this._cursorSql;
      this._wrapped = {
        exec: (...args) => {
          const c = real.exec(...args);
          cursors.push(c);
          texts.push(String(args[0]));
          return c;
        },
        get databaseSize() { return real.databaseSize; },
      };
    }
    this.sql = this._wrapped;
    return super._getSql();
  }
  _take() {
    // Drain any unconsumed cursors so their counters are final, then total and reset. Billed
    // writes are also attributed to the table each statement writes (`byTable`).
    let w = 0, r = 0;
    const byTable = {};
    this._cursors.forEach((c, i) => {
      try { for (const _ of c) { /* consume */ } } catch {}
      w += c.rowsWritten; r += c.rowsRead;
      if (c.rowsWritten > 0) {
        const table = writtenTable(this._cursorSql?.[i]);
        byTable[table] = (byTable[table] || 0) + c.rowsWritten;
      }
    });
    this._cursors.length = 0;
    if (this._cursorSql) this._cursorSql.length = 0;
    return { w, r, byTable };
  }

  /** Deferred cost of a lifecycle: the retention prune that deletes its bookkeeping rows days
   * later (attempts, terminal bundles), run directly past every retention window. */
  _measurePrune(now, byTable = null) {
    const later = now + 30 * 24 * 3600 * 1000;
    let w = 0;
    for (let round = 0; round < 100; round += 1) {
      const deleted = this._transactionSync(() => this._pruneTerminalRecords(later));
      const taken = this._take();
      w += taken.w;
      if (byTable) addInto(byTable, taken.byTable);
      if (Object.values(deleted).every((count) => count === 0)) break;
    }
    return w;
  }

  async measure({ n = 60, purpose = "chapter-agenda", model = NEMOTRON, models = null, bundle = 0, retire = false }) {
    // bundle > 0 caps jobs per bundle (bundle=1 is write_budget.js's worst-case lease).
    if (bundle > 0) this.env = { ...this.env, MAX_BUNDLE_JOBS: String(bundle) };
    this._getSql();
    const out = {};
    const t0 = Date.now();
    // Warm up: schema, scheduler row, route ledgers (one idle claim).
    await this.claimDispatchWindow(t0, 30);
    this._take();

    const jobs = Array.from({ length: n }, (_, i) => job(i, purpose, model, models));
    const enq = await this.enqueueBatch(jobs);
    out.enqueue = { ...this._take(), accepted: (enq.accepted || enq.results || []).length ?? null, raw: Object.keys(enq) };

    let now = t0 + 61_000;
    const phase = { claim: { w: 0, r: 0, bundles: 0, leased: 0 }, attempt: { w: 0, r: 0 }, complete: { w: 0, r: 0 } };
    const leasedIds = new Set();
    for (let tick = 0; tick < 400 && leasedIds.size < n; tick += 1, now += 61_000) {
      const plan = await this.claimDispatchWindow(now, 30);
      const c = this._take();
      phase.claim.w += c.w; phase.claim.r += c.r;
      if (!plan.jobs || plan.jobs.length === 0) continue;
      phase.claim.bundles += 1; phase.claim.leased += plan.jobs.length;
      const results = [];
      for (const j of plan.jobs) {
        // The executor makes no DO call before a provider call (the claim counts the attempt);
        // the `attempt` phase stays in the output so older ledger rows remain comparable.
        const attemptId = crypto.randomUUID();
        results.push({
          job_id: j.id, lease_token: j.lease_token, attempt_id: attemptId,
          planned_at: j.not_before_at, actual_start_at: now + 100, actual_end_at: now + 5000,
          observed_input_tokens: 2100, observed_output_tokens: 400,
          outcome: "success", provider_status_code: 200, result_key: `results/${j.id}/x.json`,
        });
        leasedIds.add(j.id);
      }
      const a = this._take(); phase.attempt.w += a.w; phase.attempt.r += a.r;
      await this.completeBatch(plan.bundle_id, plan.execution_token, results);
      const cc = this._take(); phase.complete.w += cc.w; phase.complete.r += cc.r;
    }
    Object.assign(out, phase);

    const ids = [...leasedIds];
    const polled = await this.pollBatch(ids);
    out.poll = this._take();
    if (retire) {
      // Consumption-based retirement (the client's default path for a persisted completion).
      await this.retireConsumed(polled.statuses.map((s) => ({ id: s.id, result_key: s.result_key })));
      out.retire = this._take();
      out.prune_w = this._measurePrune(now);
      let idleW = 0;
      for (let k = 0; k < 20; k += 1) { now += 61_000; await this.claimDispatchWindow(now, 30); idleW += this._take().w; }
      out.idle_tick_w = idleW / 20;
      return summarize(out, n);
    }
    // The ack + scheduled-cleanup path (fallback, and every job the client never retires).
    await this.ackResults(ids);
    out.ack = this._take();

    let purged = 0, purgeW = 0, confirmW = 0, rounds = 0;
    while (purged < ids.length && rounds < 100) {
      rounds += 1;
      const pending = await this.purgePendingBatch(15);
      purgeW += this._take().w;
      const pj = (pending.jobs || []).map((j) => j.id);
      if (pj.length === 0) break;
      await this.confirmPurge(pj);
      confirmW += this._take().w;
      purged += pj.length;
    }
    out.purge = { purgePendingBatchW: purgeW, confirmPurgeW: confirmW, purged, rounds };
    out.prune_w = this._measurePrune(now);

    // Idle ticks with an empty queue.
    let idleW = 0;
    for (let k = 0; k < 20; k += 1) { now += 61_000; await this.claimDispatchWindow(now, 30); idleW += this._take().w; }
    out.idle_tick_w = idleW / 20;
    return summarize(out, n);
  }

  async measureRetry() {
    // One job, one retryable 5xx then success: the extra cost of a retried attempt.
    this._getSql();
    const t0 = Date.now();
    await this.claimDispatchWindow(t0, 30); this._take();
    await this.enqueueBatch([job(9999, "chapter-agenda", NEMOTRON)]); this._take();
    let now = t0 + 61_000;
    const plan = await this.claimDispatchWindow(now, 30);
    const claimW = this._take().w;
    const j = plan.jobs[0];
    const attemptId = crypto.randomUUID();
    await this.completeBatch(plan.bundle_id, plan.execution_token, [{
      job_id: j.id, lease_token: j.lease_token, attempt_id: attemptId, planned_at: j.not_before_at,
      actual_start_at: now + 100, actual_end_at: now + 5000, outcome: "retryable_error",
      provider_status_code: 503, failure_class: "upstream_capacity",
    }]);
    return { claim_w: claimW, attempt_and_requeue_w: this._take().w };
  }
}

MeasureDO.prototype.measureIngress = async function ({ purpose, models, n }) {
  this._getSql();
  const t0 = Date.now();
  await this.claimDispatchWindow(t0, 30); this._take();
  const jobs = Array.from({ length: n }, (_, i) => job(i, purpose, models[0], models));
  const res = await this.enqueueBatch(jobs);
  const { w } = this._take();
  return { w, accepted: res.accepted.length, rejected: res.rejected.length, per_accepted: w / Math.max(1, res.accepted.length) };
};

MeasureDO.prototype.measure429 = async function () {
  this._getSql();
  const t0 = Date.now();
  await this.claimDispatchWindow(t0, 30); this._take();
  await this.enqueueBatch([job(7777, "chapter-agenda", NEMOTRON)]); this._take();
  const now = t0 + 61_000;
  const plan = await this.claimDispatchWindow(now, 30); this._take();
  const j = plan.jobs[0];
  const a1 = crypto.randomUUID();
  await this.authorizeRetry(j.id, j.lease_token, a1, now + 2000, 5, "upstream_capacity");
  const declined = this._take().w;
  // A granted in-lease retry (generic 429 within the bundle window) on a second job.
  await this.enqueueBatch([job(7778, "chapter-agenda", NEMOTRON)]); this._take();
  const later = now + 120_000;
  const plan2 = await this.claimDispatchWindow(later, 30); this._take();
  const j2 = plan2.jobs[0];
  const auth = await this.authorizeRetry(j2.id, j2.lease_token, crypto.randomUUID(), later + 100, 1, "unknown_429");
  return { authorize_retry_w: declined, authorize_grant_w: this._take().w, granted: auth.authorized };
};

MeasureDO.prototype.measureAccounting = async function () {
  await this.claimDispatchWindow(Date.now(), 30);
  this._take();
  const count = () => [...this.ctx.storage.sql.exec(
    "SELECT rows_written_today FROM scheduler WHERE id = 1"
  )][0].rows_written_today;
  const before = count();
  for (let i = 0; i < 5; i++) {
    // Recreate JS state against the same real SQLite storage, as after hibernation.
    const fresh = new LLMSchedulerDO(this.ctx, this.env);
    fresh.sql = this.sql; // Include accounting writes in the independent benchmark meter.
    await fresh.enqueueBatch([job(i, "chapter-agenda", NEMOTRON)]);
    await fresh.pauseDispatch({ scope: "global", seconds: 60, reason: "local accounting test" });
    await fresh.resumeDispatch({ scope: "global" });
  }
  return { actual_writes: this._take().w, persisted_delta: count() - before };
};

// Offline-only regression: old quota schema, expired leases, migration, and recreation.
MeasureDO.prototype.measureSchema = async function () {
  const now = Date.now();
  await this.claimDispatchWindow(now, 30);
  await this.enqueueBatch(Array.from({ length: 30 }, (_, i) =>
    job(i, "chapter-agenda", NEMOTRON)
  ));
  await this.claimDispatchWindow(now + 61_000, 30);
  for (const column of ["tpd_updated_at", "tpd_used", "prompt_cap_estimate", "input_window_json"]) {
    this.sql.exec(`ALTER TABLE routes DROP COLUMN ${column}`);
  }
  this._take();
  const failures = [];
  for (let i = 0; i < 3; i++) {
    let error = null;
    try {
      await this.claimDispatchWindow(now + 3_600_000 + i * 61_000, 30);
    } catch (err) {
      error = String(err);
    }
    failures.push({ error, ...this._take() });
  }
  const repaired = new MeasureDO(this.ctx, this.env);
  const migration = repaired._take();
  const resumed = await repaired.claimDispatchWindow(now + 3_600_000, 30);
  const resumedWrites = repaired._take();
  const recreated = new MeasureDO(this.ctx, this.env);
  return {
    failures, migration, resumed_jobs: resumed.jobs.length, resumed_writes: resumedWrites.w,
    recreation: recreated._take(),
  };
};

/**
 * A full first-try lifecycle of a mixed-lane workload (default CONSENSUS_MIX), retired by
 * consumption, reporting billed rows by table. `queue_index` is what the model queue (job_models)
 * wrote; `one_row_per_job_projection` replaces it with what a queue writing one row per job per
 * index (insert + delete = 2) would write, the pooled queue index's saving (#1844 follow-up).
 */
MeasureDO.prototype.measurePooled = async function ({ mix = CONSENSUS_MIX } = {}) {
  this._getSql();
  const t0 = Date.now();
  await this.claimDispatchWindow(t0, 30); this._take();
  const jobs = [];
  for (const [laneIndex, lane] of mix.entries()) {
    for (let i = 0; i < lane.n; i += 1) {
      jobs.push(job(laneIndex * 1000 + i, lane.purpose, lane.models[0], lane.models));
    }
  }
  const byTable = {};
  const phases = {};
  const record = (phase) => {
    const taken = this._take();
    phases[phase] = (phases[phase] || 0) + taken.w;
    addInto(byTable, taken.byTable);
  };
  const enqueued = await this.enqueueBatch(jobs);
  record("enqueue");
  const accepted = new Set((enqueued.accepted || []).map((row) => row.id));
  let now = t0 + 61_000;
  const leased = new Set();
  for (let tick = 0; tick < 600 && leased.size < accepted.size; tick += 1, now += 61_000) {
    const plan = await this.claimDispatchWindow(now, 30);
    record("claim");
    if (!plan.jobs || plan.jobs.length === 0) continue;
    await this.completeBatch(plan.bundle_id, plan.execution_token, plan.jobs.map((j) => {
      leased.add(j.id);
      return {
        job_id: j.id, lease_token: j.lease_token, attempt_id: crypto.randomUUID(),
        planned_at: j.not_before_at, actual_start_at: now + 100, actual_end_at: now + 5000,
        observed_input_tokens: 2100, observed_output_tokens: 400, outcome: "success",
        provider_status_code: 200, result_key: `results/${j.id}/x.json`,
      };
    }));
    record("complete");
  }
  const polled = await this.pollBatch([...leased]);
  this._take();
  await this.retireConsumed(polled.statuses.map((s) => ({ id: s.id, result_key: s.result_key })));
  record("retire");
  phases.prune = this._measurePrune(now, byTable);

  const lanes = mix.map((lane, laneIndex) => {
    const ids = jobs.filter((_, i) => Math.floor(Number(jobs[i].id.split("-").pop()) / 1000) === laneIndex);
    return {
      lane: lane.lane, purpose: lane.purpose, models: lane.models.length, jobs: lane.n,
      accepted: ids.filter((j) => accepted.has(j.id)).length,
      completed: ids.filter((j) => leased.has(j.id)).length,
    };
  });
  const total = Object.values(phases).reduce((sum, w) => sum + w, 0);
  const completed = leased.size;
  // A first-try job is indexed once (enqueue) and unindexed once (claim).
  const queueRows = byTable.job_models || 0;
  const oneRowQueue = 2 * completed;
  const projected = total - queueRows + oneRowQueue;
  return {
    lanes, rejected: enqueued.rejected || [], completed, phases, by_table: byTable,
    total_w: total,
    per_job: +(total / Math.max(1, completed)).toFixed(2),
    queue_index: {
      measured_w: queueRows,
      expected_per_model_w: lanes.reduce((sum, lane) => sum + 2 * lane.models * lane.completed, 0),
      one_row_per_job_w: oneRowQueue,
    },
    one_row_per_job_projection: {
      total_w: projected,
      per_job: +(projected / Math.max(1, completed)).toFixed(2),
      saved_w: total - projected,
      saved_fraction: +((total - projected) / Math.max(1, total)).toFixed(3),
    },
  };
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const name = url.searchParams.get("name") || crypto.randomUUID();
    const stub = env.M.get(env.M.idFromName(name));
    if (url.pathname === "/schema") return Response.json(await stub.measureSchema());
    if (url.pathname === "/pooled") {
      const mix = url.searchParams.get("mix");
      return Response.json(await stub.measurePooled(mix ? { mix: JSON.parse(mix) } : {}));
    }
    if (url.pathname === "/accounting") return Response.json(await stub.measureAccounting());
    if (url.pathname === "/retry") return Response.json(await stub.measureRetry());
    if (url.pathname === "/r429") return Response.json(await stub.measure429());
    if (url.pathname === "/ingress") return Response.json(await stub.measureIngress({
      purpose: url.searchParams.get("purpose"), models: url.searchParams.get("models").split(","),
      n: Number(url.searchParams.get("n")) }));
    const purpose = url.searchParams.get("purpose") || "chapter-agenda";
    const model = url.searchParams.get("model") || NEMOTRON;
    const models = url.searchParams.get("models")?.split(",") || null;
    return Response.json(await stub.measure({
      n: Number(url.searchParams.get("n") || 60), purpose, model, models,
      bundle: Number(url.searchParams.get("bundle") || 0),
      retire: url.searchParams.get("retire") === "1",
    }));
  },
};
