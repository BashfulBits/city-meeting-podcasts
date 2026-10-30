// Local-only: replay a read-only scheduling capture through the REAL coordinator and meter it.
// Bind to loopback only and never deploy: it is unauthenticated and writes arbitrary rows.
import { LLMSchedulerDO } from "../../src/coordinator.js";

const TABLES = new Set([
  "jobs", "job_models", "routes", "providers", "estimates", "dispatch_pause",
  "ingress_purpose", "attempt_usage", "route_failures", "scheduler",
]);
const hex = bytes => [...new Uint8Array(bytes)].map(b => b.toString(16).padStart(2, "0")).join("");
const sha = async value => hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(value))));
const LIMITS = { maxJobs: 20000, maxModelRows: 200000, maxGroups: 5000, maxProviders: 64, maxStateRows: 5000,
  reserveOtherWrites: 2000 };

export class ReplayDO extends LLMSchedulerDO {
  constructor(ctx, env) {
    super(ctx, env);
    // Meter every statement, including the accounting UPDATE that uses the raw handle.
    const real = ctx.storage.sql;
    this._cursors = [];
    const cursors = this._cursors;
    this.sql = {
      exec: (...args) => { const cursor = real.exec(...args); cursors.push({ cursor, statement: String(args[0]) }); return cursor; },
      get databaseSize() { return real.databaseSize; },
    };
  }

  _take() {
    let reads = 0, writes = 0;
    for (const { cursor } of this._cursors.splice(0)) {
      try { for (const _row of cursor) {} } catch { /* already consumed */ }
      reads += Number(cursor.rowsRead) || 0;
      writes += Number(cursor.rowsWritten) || 0;
    }
    return { reads, writes };
  }

  seed(table, columns, rows) {
    if (!TABLES.has(table) || !columns.every(c => /^[a-z_]+$/.test(c))) throw new Error("bad table");
    const sql = this.ctx.storage.sql;
    const existing = new Set([...sql.exec(`PRAGMA table_info("${table}")`)].map(row => row.name));
    const positions = columns.map((name, index) => existing.has(name) ? index : -1).filter(i => i >= 0);
    const names = positions.map(i => `"${columns[i]}"`).join(",");
    const marks = positions.map(() => "?").join(",");
    for (const row of rows) {
      sql.exec(`INSERT OR REPLACE INTO "${table}" (${names}) VALUES (${marks})`, ...positions.map(i => row[i]));
    }
    this._take();
    return rows.length;
  }

  /** Deterministic clock and jitter for the duration of one operation. */
  async _frozen(now, seed, work) {
    const realNow = Date.now, realRandom = Math.random;
    let state = (seed >>> 0) || 1;
    Date.now = () => now;
    Math.random = () => { state = (Math.imul(state, 1664525) + 1013904223) >>> 0; return state / 4294967296; };
    try { return await work(); } finally { Date.now = realNow; Math.random = realRandom; }
  }

  async claim(now) {
    return this._frozen(now, now, async () => {
      this._take();
      const plan = await this.claimDispatchWindow(now, 30);
      return { ...this._take(), plan };
    });
  }

  /** Settle a bundle with the caller's per-job results, optionally authorizing 429 retries first. */
  async complete(now, plan, results, authorize) {
    return this._frozen(now, now ^ 0x9e3779b1, async () => {
      this._take();
      const auth = [];
      for (const { job_id, failure_class } of authorize || []) {
        const job = plan.jobs.find(item => item.id === job_id);
        if (!job) continue;
        auth.push((await this.authorizeRetry(job.id, job.lease_token, `auth-${job.id}`, now, null, failure_class)).authorized);
      }
      const authMeter = this._take();
      await this.completeBatch(plan.bundle_id, plan.execution_token, results.map(result => ({
        lease_token: plan.jobs.find(job => job.id === result.job_id)?.lease_token, ...result })));
      return { ...this._take(), authorization: authMeter, authorized: auth };
    });
  }

  async cutover(now) {
    return this._frozen(now, 1, async () => {
      await this.pauseDispatch({ scope: "global", seconds: 3600 }, now - 960_000);
      this._take();
      const result = this._packedSwitch(LIMITS, now, "cutover");
      const meter = this._take();
      await this.resumeDispatch({ scope: "global" }, now);
      this._take();
      return { ...meter, result };
    });
  }

  async rollback(now) {
    return this._frozen(now, 1, async () => {
      await this.pauseDispatch({ scope: "global", seconds: 3600 }, now - 960_000);
      this._take();
      const result = this._packedSwitch(LIMITS, now, "rollback");
      const meter = this._take();
      await this.resumeDispatch({ scope: "global" }, now);
      this._take();
      return { ...meter, result };
    });
  }

  /** Authority-independent digest of everything a restart or operator could observe. */
  async audit() {
    const sql = this.ctx.storage.sql;
    const scheduler = [...sql.exec("SELECT * FROM scheduler WHERE id=1")][0];
    const packed = scheduler.packed_state_active === 1;
    const rows = table => [...sql.exec(`SELECT * FROM ${table}`)].map(row => ({ ...row }));
    let routes, estimates, usage, failures, providers;
    if (packed) {
      routes = Object.values(JSON.parse(scheduler.packed_routes_json));
      estimates = Object.values(JSON.parse(scheduler.packed_estimates_json));
      usage = []; failures = [];
      for (const { json } of sql.exec("SELECT json FROM packed_telemetry ORDER BY shard")) {
        const shard = JSON.parse(json);
        usage.push(...Object.values(shard.usage)); failures.push(...Object.values(shard.failures));
      }
    } else {
      routes = rows("routes"); estimates = rows("estimates"); usage = rows("attempt_usage"); failures = rows("route_failures");
    }
    providers = scheduler.packed_providers_active === 1
      ? Object.values(JSON.parse(scheduler.packed_providers_json)) : rows("providers");
    const owners = new Map([...sql.exec("SELECT id, lease_token FROM jobs WHERE lease_token IS NOT NULL")]
      .map(row => [row.lease_token, row.id]));
    const key = (list, ...cols) => list.map(row => ({ ...row })).sort((a, b) =>
      cols.map(c => String(a[c])).join("|") < cols.map(c => String(b[c])).join("|") ? -1 : 1);
    const normalizedRoutes = key(routes, "route_id").map(row => ({ ...row, input_window_json: JSON.stringify(
      JSON.parse(row.input_window_json).map(e => e.lease_token ? { ...e, lease_token: owners.get(e.lease_token) ?? "?" } : e)) }));
    const jobs = rows("jobs").map(({ lease_token, bundle_id, ...row }) => ({ ...row, leased: lease_token !== null }))
      .sort((a, b) => a.id < b.id ? -1 : 1);
    const queued = scheduler.compact_queue_active === 1
      ? [...sql.exec("SELECT COUNT(*) AS n FROM compact_queue")][0].n
      : [...sql.exec("SELECT COUNT(DISTINCT job_id) AS n FROM job_models")][0].n;
    const leased = [...sql.exec("SELECT COUNT(*) AS n FROM jobs WHERE state='leased'")][0].n;
    const registry = packed ? Object.keys(JSON.parse(scheduler.packed_leases_json)).length : leased;
    return {
      hashes: { jobs: await sha(jobs), routes: await sha(normalizedRoutes), estimates: await sha(key(estimates, "key")),
        usage: await sha(key(usage, "utc_day", "purpose", "route_id")),
        failures: await sha(key(failures, "utc_day", "route_id", "failure_class")),
        providers: await sha(key(providers, "provider")) },
      counts: { jobs: jobs.length, queued, leased, registry, routes: routes.length, estimates: estimates.length,
        usage: usage.length, failures: failures.length },
      authority: await this.packedAuthority(),
      rowsWrittenToday: scheduler.rows_written_today,
    };
  }

  abort() { this.ctx.abort("replay restart"); }
}

export default {
  async fetch(request, env) {
    const host = new URL(request.url).hostname;
    if (!["127.0.0.1", "localhost", "[::1]", "::1"].includes(host)) return new Response("local only", { status: 403 });
    const body = await request.json();
    const stub = env.BENCH.getByName(body.name);
    const run = {
      seed: () => stub.seed(body.table, body.columns, body.rows),
      claim: () => stub.claim(body.now),
      complete: () => stub.complete(body.now, body.plan, body.results, body.authorize),
      cutover: () => stub.cutover(body.now),
      rollback: () => stub.rollback(body.now),
      audit: () => stub.audit(),
      stats: () => stub.stats(body.now),
      abort: async () => { try { await stub.abort(); } catch { /* the object restarts on the next call */ } return { ok: true }; },
    }[body.action];
    if (!run) return new Response("unknown action", { status: 400 });
    try { return Response.json(await run()); } catch (error) {
      return Response.json({ error: String(error?.message || error) }, { status: 500 });
    }
  },
};
