// Function-level CPU attribution for the scheduled dispatch path.
//
// This differs from a plain wall-time benchmark in three ways that matter for the Free-plan
// 10 ms cron budget:
//
//   * the ledger is seeded with every configured route, as production's
//     `state/dispatch_budget.json` is after a few days of traffic -- an empty ledger hides both
//     the parse/stringify cost and the per-route window rollover work;
//   * the ready index carries a backlog of markers, so `loadReadyHeads` does the bounded
//     lookahead it does in production rather than returning a single head; and
//   * it reports V8 self-time per function, so a hypothesis names a function rather than a phase.
//
// Node and workerd are both V8 + ICU, so relative attribution transfers even though absolute
// numbers do not. Deployed `cpuTime` remains the acceptance signal.

import { Session } from "node:inspector/promises";

import DISPATCH_LIMITS from "../src/dispatch_limits.json" with { type: "json" };
import { handleRequest, runScheduled } from "../src/index.js";

class BenchmarkBucket {
  constructor() {
    this.objects = new Map();
    this.sequence = 0;
  }

  async put(key, value, options = {}) {
    const current = this.objects.get(key);
    const condition = options.onlyIf || {};
    if (condition.etagMatches && (!current || current.etag !== condition.etagMatches)) {
      return null;
    }
    if (condition.etagDoesNotMatch === "*" && current) return null;
    const etag = `etag-${++this.sequence}`;
    const text = typeof value === "string" ? value : await new Response(value).text();
    this.objects.set(key, {
      key,
      etag,
      value: text,
      customMetadata: options.customMetadata || {},
      uploaded: new Date(),
    });
    return { key, etag, httpEtag: `"${etag}"` };
  }

  async get(key) {
    const stored = this.objects.get(key);
    if (!stored) return null;
    return {
      key: stored.key,
      etag: stored.etag,
      httpEtag: `"${stored.etag}"`,
      customMetadata: stored.customMetadata,
      async json() {
        return JSON.parse(stored.value);
      },
      async text() {
        return stored.value;
      },
    };
  }

  async head(key) {
    const stored = this.objects.get(key);
    if (!stored) return null;
    return {
      key: stored.key,
      etag: stored.etag,
      httpEtag: `"${stored.etag}"`,
      customMetadata: stored.customMetadata,
      uploaded: stored.uploaded,
    };
  }

  async list(options = {}) {
    const prefix = options.prefix || "";
    const limit = options.limit || 1000;
    const objects = [...this.objects.values()]
      .filter((object) => object.key.startsWith(prefix))
      .sort((left, right) => left.key.localeCompare(right.key))
      .slice(0, limit);
    return {
      objects: objects.map((object) => ({
        key: object.key,
        etag: object.etag,
        customMetadata: object.customMetadata,
        uploaded: object.uploaded,
      })),
      truncated: false,
    };
  }

  async delete(key) {
    for (const one of Array.isArray(key) ? key : [key]) this.objects.delete(one);
  }
}

const ENV = {
  DISPATCH_AUTH_TOKEN: "dispatch-secret",
  PROVIDER_NAME: "mistral",
  UPSTREAM_MODEL: "mistral-large-2512",
  MODEL_ID: "mistral/mistral-large-2512",
  RETRY_BASE_SECONDS: "60",
  RETRY_MAX_SECONDS: "3600",
  MAX_ATTEMPTS: "1",
  BATCH_CONCURRENCY: "1",
  MAX_TOTAL_REQUESTS: "1",
};

for (const provider of Object.values(DISPATCH_LIMITS.providers || {})) {
  for (const account of provider.accounts || []) {
    if (account.api_key_env) ENV[account.api_key_env] = "benchmark-secret";
  }
}

// A production ledger has an entry for every route the scheduler has ever considered, not just
// the one it is about to dispatch on.
function seededBudget(now) {
  const routes = {};
  const minute = new Date(now).toISOString().slice(0, 16);
  const day = new Date(now).toISOString().slice(0, 10);
  for (const [routeId, route] of Object.entries(DISPATCH_LIMITS.routes_by_id || {})) {
    routes[routeId] = {
      requests_minute: 0,
      tokens_minute: 0,
      requests_available_at: "",
      tokens_available_at: "",
      requests_minute_key: minute,
      requests_day: route.rpd != null ? Math.floor(route.rpd / 2) : 0,
      requests_day_key: day,
      blocked_until: null,
      cost_used: 0,
      cost_cycle_key: "",
      cost_day_used: 0,
      cost_day_key: "",
      inflight: {},
    };
  }
  const providers = {};
  for (const name of Object.keys(DISPATCH_LIMITS.providers || {})) {
    providers[name] = { requests_available_at: "" };
  }
  return { version: 1, routes, providers };
}

function parseArgs(argv) {
  const options = {
    iterations: 200,
    payloadBytes: 60_000,
    backlog: 16,
    mode: "success",
    top: 25,
  };
  for (const arg of argv) {
    const [key, value] = arg.split("=", 2);
    if (key === "--iterations") options.iterations = Math.max(1, Number(value));
    if (key === "--payload-bytes") options.payloadBytes = Math.max(1_000, Number(value));
    if (key === "--backlog") options.backlog = Math.max(1, Number(value));
    if (key === "--mode" && ["success", "failure"].includes(value)) options.mode = value;
    if (key === "--top") options.top = Math.max(1, Number(value));
  }
  return options;
}

function requestBody(payloadBytes, model) {
  return {
    model,
    messages: [{ role: "user", content: "x".repeat(Math.max(0, payloadBytes - 120)) }],
    estimated_tokens: 20_000,
    input_tokens_estimate: 20_000,
    output_token_budget: 800,
  };
}

function dispatchResponse(mode, sequence) {
  if (mode === "failure") {
    return new Response(
      JSON.stringify({ error: { code: 400, status: "INVALID_ARGUMENT", message: "bench" } }),
      { status: 400, headers: { "content-type": "application/json" } },
    );
  }
  return new Response(
    JSON.stringify({
      id: `benchmark-${sequence}`,
      choices: [{ message: { content: "ok".repeat(400) }, finish_reason: "stop" }],
      usage: { prompt_tokens: 20_000, completion_tokens: 400, total_tokens: 20_400 },
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

async function seed(env, body, sequence) {
  const response = await handleRequest(
    new Request("https://dispatch.example/v1/chat/completions", {
      method: "POST",
      headers: {
        authorization: "Bearer dispatch-secret",
        "content-type": "application/json",
        "idempotency-key": `benchmark-${sequence}`,
      },
      body: JSON.stringify(body),
    }),
    env,
  );
  if (response.status !== 202) {
    throw new Error(`seed failed: ${response.status} ${await response.text()}`);
  }
}

async function buildCase(options, sequence) {
  const env = { ...ENV, LLM_QUEUE: new BenchmarkBucket() };
  const model = "gemini/gemini-3.5-flash-lite";
  for (let index = 0; index < options.backlog; index += 1) {
    await seed(env, requestBody(options.payloadBytes, model), `${sequence}-${index}`);
  }
  await env.LLM_QUEUE.put(
    "state/dispatch_budget.json",
    JSON.stringify(seededBudget(Date.now())),
    {},
  );
  return env;
}

function percentile(values, fraction) {
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * fraction))];
}

// Aggregate V8 sampling-profiler self-time by function, so the report names the code that is
// actually burning CPU rather than the phase that contains it.
function selfTimeByFunction(profile) {
  const byId = new Map(profile.nodes.map((node) => [node.id, node]));
  const totals = new Map();
  const deltas = profile.timeDeltas || [];
  const samples = profile.samples || [];
  let total = 0;
  for (let index = 0; index < samples.length; index += 1) {
    const node = byId.get(samples[index]);
    if (!node) continue;
    const micros = Math.max(0, deltas[index] || 0);
    total += micros;
    const frame = node.callFrame;
    const file = String(frame.url || "").split("/").pop() || "";
    const name = frame.functionName || "(anonymous)";
    const label = file ? `${name} @ ${file}` : name;
    totals.set(label, (totals.get(label) || 0) + micros);
  }
  return { totals: [...totals.entries()].sort((a, b) => b[1] - a[1]), total };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const originalLog = console.log;
  const originalError = console.error;
  const cpuSamples = [];
  const cases = [];

  for (let sequence = 0; sequence < options.iterations; sequence += 1) {
    cases.push(await buildCase(options, sequence));
  }

  const profiler = new Session();
  await profiler.connect();
  await profiler.post("Profiler.enable");
  await profiler.post("Profiler.setSamplingInterval", { interval: 50 });
  await profiler.post("Profiler.start");

  console.log = () => {};
  console.error = () => {};
  try {
    for (let sequence = 0; sequence < cases.length; sequence += 1) {
      const env = cases[sequence];
      const fetchImpl = async () => dispatchResponse(options.mode, sequence);
      const before = process.cpuUsage();
      const result = await runScheduled(env, { fetchImpl, nowMs: () => Date.now() });
      const cpu = process.cpuUsage(before);
      if (result.totalDispatched !== 1) {
        throw new Error(`dispatch failed: ${JSON.stringify(result)}`);
      }
      cpuSamples.push((cpu.user + cpu.system) / 1000);
    }
  } finally {
    console.log = originalLog;
    console.error = originalError;
  }

  const { profile } = await profiler.post("Profiler.stop");
  await profiler.disconnect();
  const { totals, total } = selfTimeByFunction(profile);

  originalLog(
    JSON.stringify(
      {
        event: "llm_dispatch_cpu_profile",
        iterations: options.iterations,
        mode: options.mode,
        backlog: options.backlog,
        payload_bytes: options.payloadBytes,
        cpu_ms_per_dispatch: {
          p50: Number(percentile(cpuSamples, 0.5).toFixed(3)),
          p90: Number(percentile(cpuSamples, 0.9).toFixed(3)),
          p99: Number(percentile(cpuSamples, 0.99).toFixed(3)),
          mean: Number((cpuSamples.reduce((a, b) => a + b, 0) / cpuSamples.length).toFixed(3)),
        },
        self_time_top: totals.slice(0, options.top).map(([name, micros]) => ({
          name,
          ms_per_dispatch: Number((micros / 1000 / options.iterations).toFixed(4)),
          percent: Number(((micros / total) * 100).toFixed(2)),
        })),
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
