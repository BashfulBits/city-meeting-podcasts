import { LLMSchedulerDO } from "../../src/coordinator.js";
import { DurableObject } from "cloudflare:workers";

const NEMOTRON = "nvidia/nemotron-3-ultra-550b-a55b:free";
const GEMMA = "google/gemma-4-31b-it";

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

export class MeasureDO extends LLMSchedulerDO {
  _getSql() {
    if (!this._wrapped) {
      const real = this.ctx.storage.sql;
      this._cursors = [];
      const cursors = this._cursors;
      this._wrapped = {
        exec: (...args) => {
          const c = real.exec(...args);
          cursors.push(c);
          return c;
        },
        get databaseSize() { return real.databaseSize; },
      };
    }
    return this._wrapped;
  }
  _take() {
    // Drain any unconsumed cursors so their counters are final, then total and reset.
    let w = 0, r = 0;
    for (const c of this._cursors) {
      try { for (const _ of c) { /* consume */ } } catch {}
      w += c.rowsWritten; r += c.rowsRead;
    }
    this._cursors.length = 0;
    return { w, r };
  }

  async measure({ n = 60, purpose = "chapter-agenda", model = NEMOTRON, bundle = 0, retire = false }) {
    // bundle > 0 caps jobs per bundle (bundle=1 is write_budget.js's worst-case lease).
    if (bundle > 0) this.env = { ...this.env, MAX_BUNDLE_JOBS: String(bundle) };
    this._getSql();
    const out = {};
    const t0 = Date.now();
    // Warm up: schema, scheduler row, route ledgers (one idle claim).
    await this.claimDispatchWindow(t0, 30);
    this._take();

    const jobs = Array.from({ length: n }, (_, i) => job(i, purpose, model));
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
        const attemptId = crypto.randomUUID();
        await this.attemptStarted(j.id, j.lease_token, attemptId, now + 100);
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
      let idleW = 0;
      for (let k = 0; k < 20; k += 1) { now += 61_000; await this.claimDispatchWindow(now, 30); idleW += this._take().w; }
      out.idle_tick_w = idleW / 20;
      return out;
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

    // Idle ticks with an empty queue.
    let idleW = 0;
    for (let k = 0; k < 20; k += 1) { now += 61_000; await this.claimDispatchWindow(now, 30); idleW += this._take().w; }
    out.idle_tick_w = idleW / 20;
    return out;
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
    await this.attemptStarted(j.id, j.lease_token, attemptId, now + 100);
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
  await this.attemptStarted(j.id, j.lease_token, a1, now + 100); this._take();
  await this.authorizeRetry(j.id, j.lease_token, a1, now + 2000, 5, "upstream_capacity");
  return { authorize_retry_w: this._take().w };
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const name = url.searchParams.get("name") || crypto.randomUUID();
    const stub = env.M.get(env.M.idFromName(name));
    if (url.pathname === "/retry") return Response.json(await stub.measureRetry());
    if (url.pathname === "/r429") return Response.json(await stub.measure429());
    if (url.pathname === "/ingress") return Response.json(await stub.measureIngress({
      purpose: url.searchParams.get("purpose"), models: url.searchParams.get("models").split(","),
      n: Number(url.searchParams.get("n")) }));
    const purpose = url.searchParams.get("purpose") || "chapter-agenda";
    const model = url.searchParams.get("model") || NEMOTRON;
    return Response.json(await stub.measure({
      n: Number(url.searchParams.get("n") || 60), purpose, model,
      bundle: Number(url.searchParams.get("bundle") || 0),
      retire: url.searchParams.get("retire") === "1",
    }));
  },
};
