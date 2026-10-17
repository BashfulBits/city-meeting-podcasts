// Isolated verification only. No provider execution or production storage bindings.
import { LLMSchedulerDO } from "../../src/coordinator.js";

const MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free";
const catalog = {
  model_routes_map: { [MODEL]: ["fixture-route"] },
  routes_by_id: {
    "fixture-route": {
      free: true, input_context_limit: 10000, output_context_limit: 1000,
    },
  },
};

export class RecoveryCheck extends LLMSchedulerDO {
  constructor(ctx, env) {
    super(ctx, env);
    this.instance = crypto.randomUUID();
    this.env = { ...this.env, DISPATCH_LIMITS_OVERRIDE: catalog, MAX_ACTIVE_BUNDLES: "0" };
  }

  snapshot() {
    const sql = this._getSql();
    return {
      jobs: [...sql.exec("SELECT * FROM jobs ORDER BY id")],
      models: [...sql.exec("SELECT * FROM job_models ORDER BY job_id, model")],
      scheduler: [...sql.exec("SELECT * FROM scheduler")],
      routes: [...sql.exec("SELECT * FROM routes ORDER BY route_id")],
    };
  }

  _pageSync(digest) {
    return this._transactionSync(() => {
      const result = this._reconcileUnroutableJobs(this._getSql(), Date.now(), catalog, digest);
      this._stageSchedulerSet("queued_job_count=MAX(0, queued_job_count-?)", result.failed);
      return result;
    });
  }

  async page() {
    const digest = await this._structuralCatalogDigest(catalog);
    return this._pageSync(digest);
  }

  async fetch(request) {
    const action = new URL(request.url).pathname;
    if (action === "/seed") {
      await this.claimDispatchWindow(Date.now(), 30);
      const sql = this._getSql();
      this._transactionSync(() => {
        sql.exec("CREATE UNIQUE INDEX idx_job_models_job_model ON job_models (job_id, model)");
        for (let i = 0; i < 24; i++) {
          const id = `fixture-${String(i).padStart(2, "0")}`;
          const model = i < 3 ? MODEL : "fixture/retired-model";
          const policy = JSON.stringify({ allowed_models: [model], allow_paid: false });
          sql.exec(`INSERT INTO jobs
            (id,idempotency_key,request_digest,state,policy_json,prompt_family,
             input_token_estimate,max_output_token_estimate,payload_key,created_at,updated_at,
             attempts,schema_retry_count,transient_retry_count)
            VALUES (?,?,?,'queued',?,'fixture',?,1000,?,1000,1000,2,1,3)`,
          id, id, id, policy, i === 2 ? 200000 : 2000, `fixture/${id}`);
          const job = [...sql.exec("SELECT * FROM jobs WHERE id=?", id)][0];
          this._indexQueuedJobModels(job, [i === 0 ? "__unroutable__" : "fixture/obsolete-index"]);
        }
        this._stageSchedulerSet("queued_job_count=24");
        this._stageSchedulerSet("catalog_digest=NULL, catalog_rescue_cursor=NULL, catalog_rescue_complete=0");
      });
    } else if (action === "/fail") {
      const digest = await this._structuralCatalogDigest(catalog);
      const before = JSON.stringify(this.snapshot());
      const original = this._getSql;
      const real = original.call(this);
      let mutations = 0;
      this._getSql = () => ({
        exec: (...args) => {
          if (/^\s*(INSERT|UPDATE|DELETE)/i.test(args[0]) && ++mutations === 3) {
            throw new Error("isolated injected mutation failure");
          }
          return real.exec(...args);
        },
      });
      let failed = false;
      try { this._pageSync(digest); }
      catch (error) {
        if (!String(error).includes("isolated injected mutation failure")) throw error;
        failed = true;
      } finally { this._getSql = original; }
      if (!failed || JSON.stringify(this.snapshot()) !== before) {
        throw new Error("rollback did not preserve exact persisted state");
      }
      return Response.json({ instance: this.instance, rolled_back: true, mutations });
    } else if (action === "/page") {
      const result = await this.page();
      return Response.json({ instance: this.instance, result, snapshot: this.snapshot() });
    } else if (action === "/reset") {
      this.ctx.abort("approved isolated production rescue restart");
    } else if (action !== "/read") {
      return new Response("unknown", { status: 404 });
    }
    return Response.json({ instance: this.instance, snapshot: this.snapshot() });
  }
}

export default {
  async fetch(request, env) {
    if (!env.TEST_AUTH || request.headers.get("Authorization") !== `Bearer ${env.TEST_AUTH}`) {
      return new Response("denied", { status: 403 });
    }
    if (request.method !== "POST") return new Response("POST required", { status: 405 });
    return env.TEST_DO.getByName("isolated-production-rescue").fetch(request);
  },
};
