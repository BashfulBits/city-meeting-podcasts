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
    this.sql = this._wrapped;
    return super._getSql();
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

  /** Deferred cost of a lifecycle: the retention prune that deletes its bookkeeping rows days
   * later (attempts, terminal bundles), run directly past every retention window. */
  _measurePrune(now) {
    const later = now + 30 * 24 * 3600 * 1000;
    let w = 0;
    for (let round = 0; round < 100; round += 1) {
      const deleted = this._transactionSync(() => this._pruneTerminalRecords(later));
      w += this._take().w;
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

// Existing local-only benchmark surface: verify structural rescue's billing reservation.
MeasureDO.prototype.measureRescue = async function (legacy = false, outage = false, n = 20) {
  const route = { free: true, input_context_limit: 10000, output_context_limit: 1000 };
  const catalog = { model_routes_map: { [NEMOTRON]: ["retired"] },
    routes_by_id: { retired: route } };
  this.env = { ...this.env, DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0" };
  await this.claimDispatchWindow(Date.now(), 30);
  await this.enqueueBatch(Array.from({ length: n }, (_, i) => job(i, "chapter-agenda", NEMOTRON)));
  this._take();
  if (legacy) {
    this.sql.exec("CREATE UNIQUE INDEX idx_job_models_job_model ON job_models (job_id, model)");
    this.sql.exec("UPDATE jobs SET queue_models=NULL");
  }
  catalog.routes_by_id.retired = { ...route, input_context_limit: 100 };
  const digest = await this._structuralCatalogDigest(catalog);
  this._take();
  let runner = this;
  let recovery = null;
  if (outage) {
    const snapshot = () => JSON.stringify({
      jobs: [...this.sql.exec("SELECT id, state, queue_models FROM jobs ORDER BY id")],
      models: [...this.sql.exec("SELECT * FROM job_models ORDER BY job_id, model")],
      scheduler: [...this.sql.exec("SELECT * FROM scheduler")],
    });
    const before = snapshot();
    let aborted = false;
    try {
      this._transactionSync(() => {
        this._reconcileUnroutableJobs(this._getSql(), Date.now(), catalog, digest);
        throw new Error("simulated row-write outage before checkpoint commit");
      });
    } catch (error) {
      if (!String(error).includes("simulated row-write outage")) throw error;
      aborted = true;
    }
    if (!aborted || snapshot() !== before) throw new Error("rescue rollback changed persisted state");
    this._take();
    runner = new MeasureDO(this.ctx, this.env);
    recovery = { rolled_back: true, recreation: runner._take() };
  }
  const page = () => {
    const result = runner._transactionSync(() => {
      const result = runner._reconcileUnroutableJobs(runner._getSql(), Date.now(), catalog, digest);
      runner._stageSchedulerSet("queued_job_count=MAX(0, queued_job_count-?)", result.failed);
      return result;
    });
    return { ...result, ...runner._take() };
  };
  const first = page();
  let scale = null;
  if (n > 20) {
    const pages = [first];
    for (let i = 0; i < Math.ceil(n / 20) + 1; i += 1) {
      const [state] = [...runner.sql.exec("SELECT catalog_rescue_complete FROM scheduler")];
      if (state.catalog_rescue_complete) break;
      runner._take();
      pages.push(page());
    }
    scale = { pages: pages.length, failed: pages.reduce((sum, result) => sum + result.failed, 0),
      max_page_reads: Math.max(...pages.map(result => result.r)),
      max_page_writes: Math.max(...pages.map(result => result.w)),
      complete: [...runner.sql.exec("SELECT catalog_rescue_complete FROM scheduler")][0].catalog_rescue_complete };
  }
  return { ...first, reserved_writes: legacy ? 221 : 181,
    ...(scale ? { scale } : {}),
    ...(recovery ? { recovery } : {}) };
};

// Local-only manual ledger acceptance. No provider calls or production bindings.
MeasureDO.prototype.measureManualContext = async function () {
  const previous = LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS;
  LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS = ["probe"];
  try {
    const catalog = { providers: { groq: { accounts: [{ id: "primary", api_key_env: "TEST" }] } },
      routes_by_id: { probe: { route_id: "probe", provider: "groq", account_id: "primary",
        upstream_model: "probe", free: true, rpm: 10000, rpd: 10000, tpm: 10000000,
        input_context_limit: 10000, output_context_limit: 1000 } }, model_routes_map: {} };
    this.env = { ...this.env, DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0" };
    const now = Date.now();
    await this.claimDispatchWindow(now, 30);
    await this.pauseDispatch({ scope: "provider", target: "groq", seconds: 900 }, now);
    const digest = await this._contextCatalogDigest();
    const start = { operation: "context_manual_start", run_id: "12345", catalog_digest: digest,
      route_ids: ["probe"], dimensions: ["input"], max_requests: 8, max_input: 2097152,
      max_output: 131072, per_call_input: 8192, per_call_output: 256, purpose: "local bench" };
    this._take();
    await this.reserveRouteRequests(start, now);
    const started = this._take();
    const call = { operation: "context_manual_admit", run_id: "12345", catalog_digest: digest,
      route_id: "probe", dimension: "input", attempt_id: "a".repeat(64),
      request_digest: "b".repeat(64), input_tokens: 1000, output_tokens: 256 };
    const snapshot = () => JSON.stringify({
      weeks: [...this._getSql().exec("SELECT * FROM context_manual_weeks")],
      attempts: [...this._getSql().exec("SELECT * FROM context_manual_attempts")],
      routes: [...this._getSql().exec("SELECT * FROM routes")],
      scheduler: [...this._getSql().exec("SELECT * FROM scheduler")],
    });
    const failures = [];
    const original = this._getSql;
    const persist = this._persistRowCount;
    for (let failAt = 1; failAt <= 6; failAt++) {
      const before = snapshot();
      const real = original.call(this);
      let writes = 0;
      this._getSql = () => ({ exec: (...args) => {
        if (/^\s*(INSERT|UPDATE|DELETE)/i.test(args[0]) && ++writes === failAt) {
          throw new Error("injected manual write failure");
        }
        return real.exec(...args);
      } });
      if (failAt === 6) this._persistRowCount = () => {
        throw new Error("injected manual write failure");
      };
      let failed = false;
      try { await this.reserveRouteRequests(call, now); }
      catch (error) {
        if (!String(error).includes("injected manual write failure")) throw error;
        failed = true;
      } finally { this._getSql = original; this._persistRowCount = persist; }
      if (!failed || snapshot() !== before) throw new Error(`manual rollback ${failAt} failed`);
      failures.push(failAt);
      this._take();
    }
    const admitted = await this.reserveRouteRequests(call, now);
    const admission = this._take();
    const replay = await this.reserveRouteRequests(call, now);
    const replayCost = this._take();
    const before = snapshot();
    const recreated = new MeasureDO(this.ctx, this.env);
    const recreationCost = recreated._take();
    if (snapshot() !== before) throw new Error("manual recreation changed charges");
    await recreated.reserveRouteRequests({ operation: "context_manual_finish", run_id: "12345",
      catalog_digest: digest }, now + 1);
    recreated._take();
    const rerun = await recreated.reserveRouteRequests({ ...start, run_id: "12346" }, now + 2);
    const row = [...recreated._getSql().exec("SELECT * FROM context_manual_weeks")][0];
    if (row.requests_used !== 1 || row.input_used !== 1000 || row.run_requests_used !== 0) {
      throw new Error("manual rerun refunded charges");
    }
    const week = this._contextWeek(now);
    for (let age = 0; age < 8; age++) {
      const retainedWeek = this._contextWeek(now - age * 7 * 86400000);
      if (age > 0) this._getSql().exec(`INSERT INTO context_manual_weeks
        (week_start,run_id,catalog_digest,deadline_ms,limits_json,input_used,output_used,requests_used)
        VALUES (?,?,?,?,?,24000,6144,24)`, retainedWeek, String(10000 + age), digest,
      now - 1, JSON.stringify({ route_ids: start.route_ids, dimensions: start.dimensions, max_requests: 8,
        max_input: 2097152, max_output: 131072, per_call_input: 8192, per_call_output: 256, purpose: "fixture" }));
      for (let i = age === 0 ? 1 : 0; i < 24; i++) this._getSql().exec(`INSERT INTO context_manual_attempts
        (week_start,attempt_id,run_id,route_id,dimension,request_digest,input_tokens,output_tokens,admitted_at)
        VALUES (?,?,?,'retained-fixture','input',?,1000,256,?)`,
      retainedWeek, `retained-${age}-${i}`, String(age === 0 ? 12345 : 10000 + age), digest, now - 1);
    }
    this._getSql().exec("UPDATE context_manual_weeks SET input_used=24000,output_used=6144,requests_used=24 WHERE week_start=?", week);
    this._take();
    const page = [...this._getSql().exec(
      "SELECT route_id FROM context_manual_attempts WHERE week_start=? LIMIT 25", week)];
    const boundedPage = { ...this._take(), current_attempts: page.length, retained_attempts: 192 };
    if (boundedPage.r !== 24 || boundedPage.w !== 0) throw new Error("unbounded manual attempt scan");
    return { started, failures, admitted, admission, replay, replayCost, recreationCost,
      preserved: true, rerun, boundedPage };
  } finally { LLMSchedulerDO.CONTEXT_PROBE_ROUTE_IDS = previous; }
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const name = url.searchParams.get("name") || crypto.randomUUID();
    const stub = env.M.get(env.M.idFromName(name));
    if (url.searchParams.get("rescue") === "1") {
      return Response.json(await stub.measureRescue(
        url.searchParams.get("legacy") === "1", url.searchParams.get("outage") === "1",
        Number(url.searchParams.get("n") || 20)
      ));
    }
    if (url.pathname === "/manual-context") return Response.json(await stub.measureManualContext());
    if (url.pathname === "/schema") return Response.json(await stub.measureSchema());
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
