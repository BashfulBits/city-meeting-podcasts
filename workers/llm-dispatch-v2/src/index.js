/**
 * Cloudflare Worker for LLM Dispatch v2 (Ingress and Coordinator).
 * Pure validate-then-DO-RPC pass-through with zero B2 I/O on ingress.
 */

import { LLMSchedulerDO } from "./coordinator.js";
import {
  validateEnqueueBatchRequest,
  validatePollBatchRequest,
  validateResolveUnknownBatchRequest,
  validateSchemaRetryRequest,
} from "./protocol.js";

export { LLMSchedulerDO };

function jsonResponse(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      ...extraHeaders,
    },
  });
}

function errorResponse(status, error, detail) {
  return jsonResponse({ error, detail }, status);
}

/**
 * Render a thrown value into a single string safe to pass as console.error's/errorResponse's
 * message. Cloudflare Workers Logs only reliably captures console.error's first STRING
 * argument -- an Error object passed as a second argument (the natural `console.error("x
 * failed", err)` pattern) is dropped from the exported log entirely, with no way to recover it
 * short of a code change (confirmed against a live 2026-08-18 coordinator_error incident: the
 * DO's own RPC trace showed outcome "ok" while the caller's catch block fired, and the deployed
 * logs carried nothing beyond the literal "enqueueBatch failed" call-site string, even after
 * adding a custom Logs field). Folding the real message/stack into the string argument, and
 * into the HTTP response body callers already log/print, means the actual cause survives
 * without needing dashboard access at all.
 */
function describeError(err) {
  if (err instanceof Error) {
    return `${err.name}: ${err.message}${err.stack ? `\n${err.stack}` : ""}`;
  }
  try {
    return String(err);
  } catch {
    return "unknown error (not stringifiable)";
  }
}

export function validateConfig(env) {
  const dispatchWindow = Number(env.DISPATCH_WINDOW_SECONDS || 25);
  const maxResponse = Number(env.MAX_RESPONSE_SECONDS || 720);
  const finalizationReserve = Number(env.FINALIZATION_RESERVE_SECONDS || 90);
  const leaseDuration = Number(env.LEASE_DURATION_SECONDS || 840);
  const cronLimit = Number(env.CRON_EXECUTION_LIMIT_SECONDS || 900);
  const cronTick = Number(env.CRON_TICK_SECONDS || 60);
  const maxBundlesPerDay = Number(env.MAX_BUNDLES_PER_UTC_DAY || 1000);
  const maxConcurrentLanes = Number(env.MAX_CONCURRENT_ROUTE_LANES || 5);
  const maxJobsPerDay = Number(env.MAX_JOBS_PER_UTC_DAY || 20000);

  if (dispatchWindow + maxResponse + finalizationReserve > leaseDuration) {
    throw new Error(
      `Invalid config: DISPATCH_WINDOW_SECONDS (${dispatchWindow}) + MAX_RESPONSE_SECONDS (${maxResponse}) + ` +
      `FINALIZATION_RESERVE_SECONDS (${finalizationReserve}) must be <= LEASE_DURATION_SECONDS (${leaseDuration})`
    );
  }

  if (leaseDuration >= cronLimit) {
    throw new Error(
      `Invalid config: LEASE_DURATION_SECONDS (${leaseDuration}) must be < CRON_EXECUTION_LIMIT_SECONDS (${cronLimit})`
    );
  }

  const ticksPerDay = Math.floor(86400 / cronTick);
  if (maxBundlesPerDay >= ticksPerDay) {
    throw new Error(
      `Invalid config: MAX_BUNDLES_PER_UTC_DAY (${maxBundlesPerDay}) must be < implied ticks per day (${ticksPerDay})`
    );
  }

  if (maxConcurrentLanes > 5) {
    throw new Error(
      `Invalid config: MAX_CONCURRENT_ROUTE_LANES (${maxConcurrentLanes}) must leave headroom under Cloudflare's ` +
      "fixed 6-simultaneous-connection-per-invocation limit (same on Free and Paid)"
    );
  }

  // Check projected Free resource usage stays below 75% threshold
  const maxJobsBudget = 75000; // 75% of 100,000 DO row-writes/day
  if (maxJobsPerDay > maxJobsBudget) {
    throw new Error(
      `Invalid config: MAX_JOBS_PER_UTC_DAY (${maxJobsPerDay}) exceeds 75% of included daily limit (${maxJobsBudget})`
    );
  }

  // Fail closed at deploy time, not per-request: an unset BEARER_TOKEN must never silently
  // disable auth (see hasValidBearer below).
  if (!env.BEARER_TOKEN) {
    throw new Error("Invalid config: BEARER_TOKEN must be set");
  }
}

async function hasValidBearer(request, env) {
  // Exactly one accepted variable name, matching the requirement validateConfig() enforces at
  // startup -- a deployment can never reach this function with BEARER_TOKEN unset.
  const expectedToken = env.BEARER_TOKEN;
  if (!expectedToken) {
    return false; // Fail closed: refuse all traffic until the secret is configured.
  }

  const authHeader = request.headers.get("authorization") || "";
  if (!authHeader.startsWith("Bearer ")) {
    return false;
  }
  const token = authHeader.slice(7).trim();
  if (!token) return false;

  const expectedEncoder = new TextEncoder().encode(expectedToken);
  const tokenEncoder = new TextEncoder().encode(token);

  // Hash both unconditionally, and fold the length difference into the diff accumulator below,
  // rather than returning early on a byteLength mismatch -- an early return leaks the expected
  // token's exact length to a timing attacker (matches workers/llm-dispatch-proxy's approach).
  const expectedHash = await crypto.subtle.digest("SHA-256", expectedEncoder);
  const tokenHash = await crypto.subtle.digest("SHA-256", tokenEncoder);

  const left = new Uint8Array(expectedHash);
  const right = new Uint8Array(tokenHash);
  let diff = expectedEncoder.byteLength ^ tokenEncoder.byteLength;
  for (let i = 0; i < left.length; i++) {
    diff |= left[i] ^ (right[i] || 0);
  }
  return diff === 0;
}

function getCoordinator(env) {
  if (env.LLM_SCHEDULER?.getByName) {
    return env.LLM_SCHEDULER.getByName("global-v2");
  }
  if (env.LLM_SCHEDULER?.idFromName) {
    const id = env.LLM_SCHEDULER.idFromName("global-v2");
    return env.LLM_SCHEDULER.get(id);
  }
  return env.LLM_SCHEDULER;
}

export async function handleRequest(request, env) {
  const url = new URL(request.url);
  const path = url.pathname;

  if ((request.method === "GET" || request.method === "HEAD") && path === "/healthz") {
    return jsonResponse({ ok: true });
  }

  const isAuth = await hasValidBearer(request, env);
  if (!isAuth) {
    return errorResponse(401, "unauthorized", "Invalid or missing Bearer token");
  }

  const coordinator = getCoordinator(env);

  if (request.method === "POST" && path === "/v2/jobs:enqueue-batch") {
    let body;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_json", "Request body must be valid JSON");
    }

    const maxBatch = Number(env.ENQUEUE_BATCH_MAX || 1000);
    const validation = validateEnqueueBatchRequest(body, maxBatch);
    if (!validation.valid) {
      return errorResponse(400, validation.error, validation.detail);
    }

    const preparedJobs = body.jobs.map((rawJob) => ({
      id: rawJob.id || crypto.randomUUID(),
      idempotency_key: rawJob.idempotency_key,
      request_digest: rawJob.request_digest,
      provider_idempotency_key: rawJob.provider_idempotency_key || null,
      // rawJob.policy_json is the documented field (see Unit 2's EnqueueJobInput); a caller
      // that sends it as an object rather than a pre-serialized string must still have it
      // persisted, not silently dropped by falling through to an unrelated `.policy` field
      // that appears nowhere else in this protocol.
      policy_json:
        typeof rawJob.policy_json === "string"
          ? rawJob.policy_json
          : JSON.stringify(rawJob.policy_json ?? {}),
      prompt_family: rawJob.prompt_family,
      input_token_estimate: Number(rawJob.input_token_estimate || 0),
      max_output_token_estimate: Number(rawJob.max_output_token_estimate || 0),
      payload_key: rawJob.payload_key,
      priority: rawJob.priority !== undefined ? rawJob.priority : 1,
    }));

    try {
      const result = await coordinator.enqueueBatch(preparedJobs);
      return jsonResponse(result, 200);
    } catch (err) {
      const detail = describeError(err);
      console.error(`enqueueBatch failed: ${detail}`);
      return errorResponse(500, "coordinator_error", detail);
    }
  }

  if (request.method === "POST" && path === "/v2/jobs:poll-batch") {
    let body;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_json", "Request body must be valid JSON");
    }

    const maxBatch = Number(env.POLL_BATCH_MAX || 1000);
    const validation = validatePollBatchRequest(body, maxBatch);
    if (!validation.valid) {
      return errorResponse(400, validation.error, validation.detail);
    }

    try {
      const result = await coordinator.pollBatch(body.ids);
      return jsonResponse(result, 200);
    } catch (err) {
      const detail = describeError(err);
      console.error(`pollBatch failed: ${detail}`);
      return errorResponse(500, "coordinator_error", detail);
    }
  }

  if (request.method === "POST" && /^\/v2\/jobs\/[^/]+:schema-retry$/.test(path)) {
    let body;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_json", "Request body must be valid JSON");
    }

    const validation = validateSchemaRetryRequest(body);
    if (!validation.valid) {
      return errorResponse(400, validation.error, validation.detail);
    }

    // Schema-retry semantics (a new payload/idempotency namespace persisted through the
    // coordinator) land with Phase 2's dispatch/structured-output-validation machinery -- this
    // endpoint must not fabricate an id and claim success for a job that was never actually
    // written to the jobs table; a caller polling that id would wait forever.
    return errorResponse(
      501,
      "not_implemented",
      "schema-retry is not yet implemented (lands with Phase 2)"
    );
  }

  if (request.method === "POST" && path === "/v2/jobs:resolve-unknown-batch") {
    let body;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_json", "Request body must be valid JSON");
    }

    const validation = validateResolveUnknownBatchRequest(body);
    if (!validation.valid) {
      return errorResponse(400, validation.error, validation.detail);
    }

    try {
      const result = await coordinator.resolveUnknownBatch(body.attempt_ids);
      return jsonResponse(result, 200);
    } catch (err) {
      const detail = describeError(err);
      console.error(`resolveUnknownBatch failed: ${detail}`);
      return errorResponse(500, "coordinator_error", detail);
    }
  }

  return errorResponse(404, "not_found", `Unknown endpoint: ${path}`);
}

export default {
  async fetch(request, env, ctx) {
    try {
      validateConfig(env);
    } catch (err) {
      return errorResponse(500, "configuration_error", err.message);
    }
    return handleRequest(request, env, ctx);
  },

  async scheduled(event, env, ctx) {
    // Scheduled cron handler lands in Phase 2
  },
};
