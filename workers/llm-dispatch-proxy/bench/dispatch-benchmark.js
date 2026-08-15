import { writeFile } from "node:fs/promises";
import { Session } from "node:inspector/promises";

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
    // Must match cpu-profile.js: the Worker removes a batch's finished markers in one keyed
    // delete, so a stub that only accepts a scalar would silently leave markers behind here and
    // make the two harnesses' results incomparable.
    for (const one of Array.isArray(key) ? key : [key]) this.objects.delete(one);
  }
}

const ENV = {
  DISPATCH_AUTH_TOKEN: "dispatch-secret",
  PROVIDER_NAME: "mistral",
  UPSTREAM_MODEL: "mistral-large-2512",
  MODEL_ID: "mistral/mistral-large-2512",
  MISTRAL_API_KEY: "benchmark-secret",
  RETRY_BASE_SECONDS: "60",
  RETRY_MAX_SECONDS: "3600",
};

function parseArgs(argv) {
  const options = { iterations: 100, payloadBytes: 50_000, mode: "success", profilePath: null };
  for (const arg of argv) {
    const [key, value] = arg.split("=", 2);
    if (key === "--iterations") options.iterations = Math.max(1, Number(value));
    if (key === "--payload-bytes") options.payloadBytes = Math.max(1_000, Number(value));
    if (key === "--mode" && ["success", "failure"].includes(value)) options.mode = value;
    if (key === "--cpu-profile") options.profilePath = value;
  }
  if (!Number.isInteger(options.iterations) || !Number.isFinite(options.iterations)) {
    throw new Error("--iterations must be a positive integer");
  }
  if (!Number.isInteger(options.payloadBytes) || !Number.isFinite(options.payloadBytes)) {
    throw new Error("--payload-bytes must be a positive integer");
  }
  return options;
}

function requestBody(payloadBytes) {
  const content = "x".repeat(Math.max(0, payloadBytes - 120));
  return {
    model: "mistral/mistral-large-2512",
    messages: [{ role: "user", content }],
  };
}

function dispatchResponse(mode, sequence) {
  if (mode === "failure") {
    return new Response(
      JSON.stringify({
        error: {
          code: 400,
          status: "INVALID_ARGUMENT",
          message: "benchmark failure",
        },
      }),
      { status: 400, headers: { "content-type": "application/json" } },
    );
  }
  return new Response(
    JSON.stringify({ id: `benchmark-${sequence}`, choices: [{ message: { content: "ok" } }] }),
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
    throw new Error(`benchmark seed failed: ${response.status} ${await response.text()}`);
  }
}

async function measureDispatch(env, sequence, mode) {
  const fetchImpl = async () => dispatchResponse(mode, sequence);
  const cpuBefore = process.cpuUsage();
  const wallBefore = process.hrtime.bigint();
  const result = await runScheduled(env, { fetchImpl, nowMs: () => Date.now() });
  if (result.totalDispatched !== 1) {
    throw new Error(`benchmark dispatch failed: ${JSON.stringify(result)}`);
  }
  const wallMs = Number(process.hrtime.bigint() - wallBefore) / 1_000_000;
  const cpu = process.cpuUsage(cpuBefore);
  return {
    cpuMs: (cpu.user + cpu.system) / 1_000,
    wallMs,
  };
}

function percentile(values, fraction) {
  const sorted = [...values].sort((left, right) => left - right);
  return sorted[Math.min(sorted.length - 1, Math.floor((sorted.length - 1) * fraction))];
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const cpuSamples = [];
  const wallSamples = [];
  const body = requestBody(options.payloadBytes);
  const originalLog = console.log;
  const originalError = console.error;
  console.log = () => {};
  console.error = () => {};

  try {
    const cases = [];
    for (let sequence = 0; sequence < options.iterations; sequence += 1) {
      const env = { ...ENV, LLM_QUEUE: new BenchmarkBucket() };
      await seed(env, body, sequence);
      cases.push({ env, sequence });
    }

    let profiler;
    if (options.profilePath) {
      profiler = new Session();
      await profiler.connect();
      await profiler.post("Profiler.enable");
      await profiler.post("Profiler.start");
    }

    for (const { env, sequence } of cases) {
      const sample = await measureDispatch(env, sequence, options.mode);
      cpuSamples.push(sample.cpuMs);
      wallSamples.push(sample.wallMs);
    }

    if (profiler) {
      const { profile } = await profiler.post("Profiler.stop");
      await writeFile(options.profilePath, JSON.stringify(profile));
      await profiler.disconnect();
    }
  } finally {
    console.log = originalLog;
    console.error = originalError;
  }

  console.log(
    JSON.stringify(
      {
        event: "llm_dispatch_benchmark",
        iterations: options.iterations,
        mode: options.mode,
        payload_bytes: JSON.stringify(body).length,
        cpu_ms: {
          p50: percentile(cpuSamples, 0.5),
          p95: percentile(cpuSamples, 0.95),
          max: Math.max(...cpuSamples),
        },
        wall_ms: {
          p50: percentile(wallSamples, 0.5),
          p95: percentile(wallSamples, 0.95),
          max: Math.max(...wallSamples),
        },
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
