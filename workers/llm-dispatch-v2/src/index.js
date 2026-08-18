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
}

async function hasValidBearer(request, env) {
  const expectedToken =
    env.BEARER_TOKEN ||
    env.DISPATCH_AUTH_TOKEN ||
    env.AUTH_TOKEN ||
    env.LLM_DISPATCH_AUTH_TOKEN;

  if (!expectedToken) {
    return true; // No auth token configured
  }

  const authHeader = request.headers.get("authorization") || "";
  if (!authHeader.startsWith("Bearer ")) {
    return false;
  }
  const token = authHeader.slice(7).trim();
  if (!token) return false;

  const expectedEncoder = new TextEncoder().encode(expectedToken);
  const tokenEncoder = new TextEncoder().encode(token);

  if (expectedEncoder.byteLength !== tokenEncoder.byteLength) {
    return false;
  }

  const expectedHash = await crypto.subtle.digest("SHA-256", expectedEncoder);
  const tokenHash = await crypto.subtle.digest("SHA-256", tokenEncoder);

  const left = new Uint8Array(expectedHash);
  const right = new Uint8Array(tokenHash);
  let diff = 0;
  for (let i = 0; i < left.length; i++) {
    diff |= left[i] ^ right[i];
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
      policy_json: typeof rawJob.policy_json === "string" ? rawJob.policy_json : JSON.stringify(rawJob.policy || {}),
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
      return errorResponse(500, "coordinator_error", err.message);
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
      return errorResponse(500, "coordinator_error", err.message);
    }
  }

  if (request.method === "POST" && /^\/v2\/jobs\/[^/]+:schema-retry$/.test(path)) {
    const match = path.match(/^\/v2\/jobs\/([^/]+):schema-retry$/);
    const jobId = match[1];
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

    const newId = crypto.randomUUID();
    const newKey = `${jobId}:schema-retry:${Date.now()}`;
    return jsonResponse({ id: newId, idempotency_key: newKey }, 200);
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
      return errorResponse(500, "coordinator_error", err.message);
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
