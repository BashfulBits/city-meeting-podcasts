/**
 * Cloudflare Worker for LLM Dispatch v2 (Ingress and Coordinator).
 * Pure validate-then-DO-RPC pass-through with zero B2 I/O on ingress.
 */

import DISPATCH_LIMITS from "./dispatch_limits.json" with { type: "json" };
import { LLMSchedulerDO } from "./coordinator.js";
import {
  validateEnqueueBatchRequest,
  validatePollBatchRequest,
  validateResolveUnknownBatchRequest,
  validateSchemaRetryRequest,
} from "./protocol.js";
import { B2Client } from "./b2.js";
import { callAiGateway, observedTokens } from "./gateway.js";

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

/** Fail fast when a deployment configuration would violate scheduler safety bounds. */
export function validateConfig(env) {
  const dispatchWindow = Number(env.DISPATCH_WINDOW_SECONDS || 25);
  const maxResponse = Number(env.MAX_RESPONSE_SECONDS || 720);
  const finalizationReserve = Number(env.FINALIZATION_RESERVE_SECONDS || 90);
  const leaseDuration = Number(env.LEASE_DURATION_SECONDS || 840);
  const cronLimit = Number(env.CRON_EXECUTION_LIMIT_SECONDS || 900);
  const cronTick = Number(env.CRON_TICK_SECONDS || 60);
  const maxBundlesPerDay = Number(env.MAX_BUNDLES_PER_UTC_DAY || 1000);
  const maxBundleJobs = Number(env.MAX_BUNDLE_JOBS || 4);
  const maxJobsPerModelClaim = Number(env.MAX_JOBS_PER_MODEL_CLAIM || maxBundleJobs);
  const maxConcurrentLanes = Number(env.MAX_CONCURRENT_ROUTE_LANES || 5);
  const maxJobsPerDay = Number(env.MAX_JOBS_PER_UTC_DAY || 5000);
  const max5xxRetries = Number(env.MAX_5XX_RETRIES || 1);
  const max5xxBackoffSeconds = Number(env.MAX_5XX_BACKOFF_SECONDS || 300);
  const queuedJobModelBackfillLimit =
    env.MAX_QUEUED_JOB_MODEL_BACKFILL_PER_CLAIM === undefined
      ? 1000
      : Number(env.MAX_QUEUED_JOB_MODEL_BACKFILL_PER_CLAIM);
  const legacyRetryableRecoveryLimit =
    env.MAX_LEGACY_RETRYABLE_RECOVERY_PER_CLAIM === undefined
      ? 100
      : Number(env.MAX_LEGACY_RETRYABLE_RECOVERY_PER_CLAIM);
  const migrationWriteUnitsPerDay =
    env.MAX_MIGRATION_WRITE_UNITS_PER_UTC_DAY === undefined
      ? 5000
      : Number(env.MAX_MIGRATION_WRITE_UNITS_PER_UTC_DAY);
  const migrationRowsScannedPerDay =
    env.MAX_MIGRATION_ROWS_SCANNED_PER_UTC_DAY === undefined
      ? 250000
      : Number(env.MAX_MIGRATION_ROWS_SCANNED_PER_UTC_DAY);

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

  if (
    !Number.isInteger(maxJobsPerModelClaim) ||
    maxJobsPerModelClaim < 1 ||
    maxJobsPerModelClaim > maxBundleJobs
  ) {
    throw new Error(
      `Invalid config: MAX_JOBS_PER_MODEL_CLAIM (${maxJobsPerModelClaim}) must be an integer ` +
      `from 1 to MAX_BUNDLE_JOBS (${maxBundleJobs})`
    );
  }

  if (maxConcurrentLanes > 5) {
    throw new Error(
      `Invalid config: MAX_CONCURRENT_ROUTE_LANES (${maxConcurrentLanes}) must leave headroom under Cloudflare's ` +
      "fixed 6-simultaneous-connection-per-invocation limit (same on Free and Paid)"
    );
  }

  // AI Gateway has already exhausted its own configured fast retry series before the scheduler
  // sees a final 5xx. Keep the durable fallback intentionally small so it cannot multiply calls.
  if (!Number.isInteger(max5xxRetries) || max5xxRetries < 0 || max5xxRetries > 2) {
    throw new Error("Invalid config: MAX_5XX_RETRIES must be an integer from 0 to 2");
  }
  if (
    !Number.isInteger(max5xxBackoffSeconds) ||
    max5xxBackoffSeconds < 60 ||
    max5xxBackoffSeconds > 300
  ) {
    throw new Error("Invalid config: MAX_5XX_BACKOFF_SECONDS must be an integer from 60 to 300");
  }

  // These compatibility migrations can be paused independently of normal dispatch when the
  // SQLite row-write budget is tight. Zero is an intentional emergency brake, not a fallback.
  if (!Number.isInteger(queuedJobModelBackfillLimit) || queuedJobModelBackfillLimit < 0) {
    throw new Error(
      "Invalid config: MAX_QUEUED_JOB_MODEL_BACKFILL_PER_CLAIM must be a non-negative integer"
    );
  }
  if (!Number.isInteger(legacyRetryableRecoveryLimit) || legacyRetryableRecoveryLimit < 0) {
    throw new Error(
      "Invalid config: MAX_LEGACY_RETRYABLE_RECOVERY_PER_CLAIM must be a non-negative integer"
    );
  }
  if (
    !Number.isInteger(migrationWriteUnitsPerDay) ||
    migrationWriteUnitsPerDay < 0 ||
    (migrationWriteUnitsPerDay > 0 && migrationWriteUnitsPerDay < 5)
  ) {
    throw new Error(
      "Invalid config: MAX_MIGRATION_WRITE_UNITS_PER_UTC_DAY must be 0 or an integer of at least 5"
    );
  }
  if (!Number.isInteger(migrationRowsScannedPerDay) || migrationRowsScannedPerDay < 0) {
    throw new Error(
      "Invalid config: MAX_MIGRATION_ROWS_SCANNED_PER_UTC_DAY must be a non-negative integer"
    );
  }

  // Check projected Free resource usage stays below 75% threshold
  // A queued job writes its job row plus one or more model-index rows, and a claimed job removes
  // those index rows. The 5k ceiling leaves room for the documented dispatch lifecycle writes.
  const maxJobsBudget = 5000;
  if (maxJobsPerDay > maxJobsBudget) {
    throw new Error(
      `Invalid config: MAX_JOBS_PER_UTC_DAY (${maxJobsPerDay}) exceeds 75% of included daily limit (${maxJobsBudget})`
    );
  }

  const estimatedCallDurationCeiling = Number(env.ESTIMATED_CALL_DURATION_CEILING_SECONDS || 20);
  if (estimatedCallDurationCeiling >= dispatchWindow) {
    throw new Error(
      `Invalid config: ESTIMATED_CALL_DURATION_CEILING_SECONDS (${estimatedCallDurationCeiling}) ` +
      `must be < DISPATCH_WINDOW_SECONDS (${dispatchWindow})`
    );
  }

  // Retention/cleanup bounds. A zero per-tick prune cap is a deliberate emergency pause, but a
  // zero or negative retention window would delete records the moment they turn terminal --
  // including a bundle a late completeBatch could still legitimately settle.
  for (const name of ["BUNDLE_RETENTION_DAYS", "ATTEMPT_RETENTION_DAYS"]) {
    if (env[name] === undefined) continue;
    const days = Number(env[name]);
    if (!Number.isInteger(days) || days < 1) {
      throw new Error(`Invalid config: ${name} must be an integer of at least 1`);
    }
  }
  for (const name of ["MAX_BUNDLE_PRUNE_PER_TICK", "MAX_ATTEMPT_PRUNE_PER_TICK", "PURGE_BATCH_LIMIT"]) {
    if (env[name] === undefined) continue;
    const value = Number(env[name]);
    if (!Number.isInteger(value) || value < 0) {
      throw new Error(`Invalid config: ${name} must be a non-negative integer`);
    }
  }
  const cleanupInterval = Number(env.CLEANUP_INTERVAL_MINUTES ?? 60);
  if (!Number.isInteger(cleanupInterval) || cleanupInterval < 1 || cleanupInterval > 60) {
    throw new Error("Invalid config: CLEANUP_INTERVAL_MINUTES must be an integer from 1 to 60");
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

  if (request.method === "POST" && path === "/v2/jobs:ack-batch") {
    let body;
    try {
      body = await request.json();
    } catch {
      return errorResponse(400, "invalid_json", "Request body must be valid JSON");
    }

    // Same {ids: [...]} shape and batch ceiling as poll-batch -- an ack always follows a poll, so
    // it is never larger than the poll that produced it.
    const maxBatch = Number(env.POLL_BATCH_MAX || 1000);
    const validation = validatePollBatchRequest(body, maxBatch);
    if (!validation.valid) {
      return errorResponse(400, validation.error, validation.detail);
    }

    try {
      const result = await coordinator.ackResults(body.ids);
      return jsonResponse(result, 200);
    } catch (err) {
      const detail = describeError(err);
      console.error(`ackResults failed: ${detail}`);
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

function sleepUntil(targetTime) {
  const delayMs = Math.max(0, targetTime - Date.now());
  return new Promise((resolve) => setTimeout(resolve, delayMs));
}

/**
 * No route is currently confirmed to support a client-supplied provider-side idempotency key
 * (see review/44's "no binding shortcut" note and the executor's own doc comment below) -- every
 * job takes the attemptStarted-fencing path today. This function exists as the single place a
 * future per-provider opt-in would be wired in (e.g. a `supports_idempotency_key` flag compiled
 * into dispatch_limits.json's provider block), so nothing else in this file needs to change.
 */
function routeSupportsProviderIdempotency(route, dispatchLimits) {
  return Boolean(dispatchLimits.providers?.[route.provider]?.supports_idempotency_key);
}

function b2ClientFromEnv(env) {
  if (!env.B2_ENDPOINT || !env.B2_KEY_ID || !env.B2_APP_KEY || !env.B2_BUCKET) return null;
  return new B2Client({
    endpoint: env.B2_ENDPOINT,
    bucket: env.B2_BUCKET,
    keyId: env.B2_KEY_ID,
    appKey: env.B2_APP_KEY,
  });
}

function routeForId(routeId, dispatchLimits) {
  const stored = dispatchLimits.routes_by_id?.[routeId];
  return stored ? { ...stored, route_id: routeId } : null;
}

function baseAttemptResult(job, attemptId, actualStartAt, actualEndAt, outcome) {
  return {
    job_id: job.id,
    lease_token: job.lease_token,
    attempt_id: attemptId,
    planned_at: job.not_before_at,
    actual_start_at: actualStartAt,
    actual_end_at: actualEndAt,
    observed_input_tokens: null,
    observed_output_tokens: null,
    outcome,
    provider_status_code: null,
    gateway_correlation_id: null,
    result_key: null,
  };
}

/**
 * One provider call for one job, already fenced and paced by the caller. Returns exactly one of:
 * `{ skip: true }` (lease no longer current -- the DO reaped it; caller must not report anything
 * for this attempt), `{ retry429: true, actualStartAt, actualEndAt, correlationId }` (caller
 * decides whether/how to retry), or `{ result }` (a terminal AttemptResult ready for
 * completeBatch, covering success, transport failure, and every non-429 HTTP status).
 */
async function attemptProviderCall({ env, coordinator, b2, route, dispatchLimits, job, attemptId, idempotencyKey, maxResponseMs }) {
  if (!routeSupportsProviderIdempotency(route, dispatchLimits)) {
    const fence = await coordinator.attemptStarted(job.id, job.lease_token, attemptId, Date.now());
    if (!fence.fenced) return { skip: true };
  }

  const actualStartAt = Date.now();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), maxResponseMs);
  let response;
  try {
    response = await callAiGateway({ env, route, payload: job.payload, dispatchLimits, idempotencyKey, signal: controller.signal });
  } catch (err) {
    return { result: baseAttemptResult(job, attemptId, actualStartAt, Date.now(), "retryable_error") };
  } finally {
    clearTimeout(timeout);
  }
  const actualEndAt = Date.now();

  if (response.status === 429) {
    return { retry429: true, actualStartAt, actualEndAt, correlationId: response.correlationId };
  }

  if (response.ok) {
    const usage = observedTokens(response.body);
    const resultKey = `results/${job.id}/${job.lease_token}.json`;
    try {
      await b2.putJson(resultKey, response.body);
      return {
        result: {
          ...baseAttemptResult(job, attemptId, actualStartAt, actualEndAt, "success"),
          observed_input_tokens: usage.input,
          observed_output_tokens: usage.output,
          provider_status_code: response.status,
          gateway_correlation_id: response.correlationId,
          result_key: resultKey,
        },
      };
    } catch (err) {
      console.error(`scheduled: failed to write result for job ${job.id}`, err);
      return {
        result: {
          ...baseAttemptResult(job, attemptId, actualStartAt, actualEndAt, "retryable_error"),
          observed_input_tokens: usage.input,
          observed_output_tokens: usage.output,
          provider_status_code: response.status,
          gateway_correlation_id: response.correlationId,
        },
      };
    }
  }

  return {
    result: {
      ...baseAttemptResult(
        job,
        attemptId,
        actualStartAt,
        actualEndAt,
        response.status >= 500 ? "retryable_error" : "terminal_error"
      ),
      provider_status_code: response.status,
      gateway_correlation_id: response.correlationId,
    },
  };
}

/**
 * One job's full lifecycle within its lane: pace, read its payload, then attempt/retry until a
 * terminal AttemptResult exists. Every 429 loops back through authorizeRetry, which itself
 * enforces MAX_429_RETRIES (see coordinator.js) -- this loop does not separately bound attempts;
 * it simply keeps retrying for as long as the DO keeps authorizing.
 */
async function dispatchOneJob({ env, coordinator, b2, dispatchLimits, job, laneState, bundleDeadline, maxResponseMs }) {
  let targetTime = laneState.receivedAt + job.wait_ms;
  if (laneState.predecessorActualStart !== null) {
    targetTime = Math.max(targetTime, laneState.predecessorActualStart + job.min_inter_request_gap_ms);
  }
  if (laneState.retryBarrier !== null) targetTime = Math.max(targetTime, laneState.retryBarrier);

  if (targetTime > bundleDeadline) {
    return baseAttemptResult(job, crypto.randomUUID(), null, null, "deferred_late");
  }

  await sleepUntil(targetTime);

  let payload;
  try {
    payload = await b2.getJson(job.payload_key);
  } catch (err) {
    console.error(`scheduled: failed to read payload for job ${job.id}`, err);
    laneState.predecessorActualStart = Date.now();
    return baseAttemptResult(job, crypto.randomUUID(), null, null, "retryable_error");
  }
  if (payload === null) {
    console.error(`scheduled: payload missing for job ${job.id} at ${job.payload_key}`);
    laneState.predecessorActualStart = Date.now();
    return baseAttemptResult(job, crypto.randomUUID(), null, null, "terminal_error");
  }

  const route = routeForId(job.route_id, dispatchLimits);
  if (!route) {
    console.error(`scheduled: unknown route ${job.route_id} for job ${job.id}`);
    laneState.predecessorActualStart = Date.now();
    return baseAttemptResult(job, crypto.randomUUID(), null, null, "terminal_error");
  }
  const idempotencyKey = routeSupportsProviderIdempotency(route, dispatchLimits)
    ? job.provider_idempotency_key || null
    : null;
  const jobWithPayload = { ...job, payload };

  for (;;) {
    const attemptId = crypto.randomUUID();
    const outcome = await attemptProviderCall({
      env,
      coordinator,
      b2,
      route,
      dispatchLimits,
      job: jobWithPayload,
      attemptId,
      idempotencyKey,
      maxResponseMs,
    });

    if (outcome.skip) {
      laneState.predecessorActualStart = Date.now();
      return null; // lease reaped; nothing to report for this attempt
    }

    if (outcome.result) {
      laneState.predecessorActualStart = outcome.result.actual_start_at;
      return outcome.result;
    }

    // 429: ask the DO whether (and when) a retry is authorized.
    laneState.predecessorActualStart = outcome.actualStartAt;
    const auth = await coordinator.authorizeRetry(job.id, job.lease_token, attemptId, Date.now());
    if (!auth.authorized || auth.retry_not_before > bundleDeadline) {
      return {
        ...baseAttemptResult(job, attemptId, outcome.actualStartAt, outcome.actualEndAt, "terminal_error"),
        provider_status_code: 429,
        gateway_correlation_id: outcome.correlationId,
      };
    }
    laneState.retryBarrier = auth.retry_not_before;
    await sleepUntil(auth.retry_not_before);
    // Loop: re-attempt with a fresh attempt_id, still bounded by the DO's own retry count.
  }
}

/**
 * review/44 Unit 6: claim one paced dispatch window, run each route lane independently and
 * just-in-time (no B2 access before a job's own wait_ms elapses, no bulk pre-fetch -- see the
 * unit's own "do not" guardrails), and report every attempt in one completeBatch call.
 */
async function runScheduledDispatch(env) {
  const b2 = b2ClientFromEnv(env);
  if (!b2) {
    console.error("scheduled: B2 credentials not configured; cannot execute claimed plan");
    return;
  }

  const coordinator = getCoordinator(env);
  const dispatchLimits = env.DISPATCH_LIMITS_OVERRIDE || DISPATCH_LIMITS;
  const dispatchWindowSeconds = Number(env.DISPATCH_WINDOW_SECONDS || 25);

  const plan = await coordinator.claimDispatchWindow(Date.now(), dispatchWindowSeconds);
  if (!plan.jobs || plan.jobs.length === 0) return; // no B2 access, no further DO calls

  const receivedAt = Date.now(); // wait_ms is relative to THIS instant, not plan-build time
  const bundleDeadline = receivedAt + dispatchWindowSeconds * 1000;
  const maxResponseMs = Number(env.MAX_RESPONSE_SECONDS || 720) * 1000;

  const lanes = new Map();
  for (const job of plan.jobs) {
    if (!lanes.has(job.route_id)) lanes.set(job.route_id, []);
    lanes.get(job.route_id).push(job);
  }

  const results = [];
  // Promise.allSettled over independent lanes -- safe without a separate concurrency limiter
  // because claimDispatchWindow already caps distinct routes at MAX_CONCURRENT_ROUTE_LANES (see
  // Unit 4/Unit 6's own guardrail on why this constant exists).
  await Promise.allSettled(
    [...lanes.entries()].map(async ([, laneJobs]) => {
      const laneState = { receivedAt, predecessorActualStart: null, retryBarrier: null };
      for (const job of laneJobs) {
        let result;
        try {
          result = await dispatchOneJob({
            env,
            coordinator,
            b2,
            dispatchLimits,
            job,
            laneState,
            bundleDeadline,
            maxResponseMs,
          });
        } catch (err) {
          // An unexpected exception here must still produce a completeBatch result -- letting it
          // propagate would leave this settlement "rejected" (swallowed by Promise.allSettled
          // below) with nothing pushed for this job, stranding it `leased` with no diagnostic
          // trace until the lease-expiry sweep eventually reaps it.
          console.error(`scheduled: unexpected error dispatching job ${job.id}`, err);
          result = baseAttemptResult(job, crypto.randomUUID(), null, null, "retryable_error");
          laneState.predecessorActualStart = Date.now();
        }
        if (result !== null) results.push(result);
      }
    })
  );

  try {
    await coordinator.completeBatch(plan.bundle_id, plan.execution_token, results);
  } catch (err) {
    console.error(`scheduled: completeBatch failed for bundle ${plan.bundle_id}: ${describeError(err)}`, err);
  }
}


/**
 * review/44's B2-only payload lifecycle: the DO owns the job rows but holds no B2 credentials,
 * so retiring a terminal job is a two-phase handshake -- purgePendingBatch moves a bounded batch
 * to 'purge_pending' and hands back their keys, this Worker deletes those keys, then confirmPurge
 * drops the rows. Both DO methods have existed (and been tested) since Phase 2 but nothing ever
 * called them, so `jobs` and its B2 objects grew without bound.
 *
 * Crash-safe by construction: a failure after the B2 deletes but before confirmPurge simply
 * repeats on the next run (confirmPurge is idempotent), and a key that is already gone deletes
 * cleanly. A job is only ever eligible once it is terminal AND older than
 * COMPLETED_RETENTION_DAYS, so this can never delete a result a client has not had the chance
 * to read.
 */
async function runScheduledCleanup(env, scheduledTime) {
  const intervalMinutes = Number(env.CLEANUP_INTERVAL_MINUTES || 60);
  if (!Number.isFinite(intervalMinutes) || intervalMinutes <= 0) return;
  // Cloudflare always supplies scheduledTime for a real cron firing. Without it we cannot know
  // where in the cadence we are, so skip rather than run this on every single tick.
  if (!Number.isFinite(scheduledTime)) return;
  if (new Date(scheduledTime).getUTCMinutes() % intervalMinutes !== 0) return;

  const b2 = b2ClientFromEnv(env);
  if (!b2) return;
  const coordinator = getCoordinator(env);
  const limit = Number(env.PURGE_BATCH_LIMIT || 50);

  let pending;
  try {
    pending = await coordinator.purgePendingBatch(limit);
  } catch (err) {
    console.error(`scheduled: purgePendingBatch failed: ${describeError(err)}`);
    return;
  }
  const jobs = pending?.jobs || [];
  if (jobs.length === 0) return;

  const purgedIds = [];
  for (const job of jobs) {
    const keys = [job.payload_key, job.result_key].filter(Boolean);
    try {
      await Promise.all(keys.map((key) => b2.delete(key)));
      purgedIds.push(job.id);
    } catch (err) {
      // Leave this job in 'purge_pending' and retry it next run rather than confirming a purge
      // whose B2 objects are still present -- dropping the row is what would orphan them.
      console.error(`scheduled: B2 delete failed for job ${job.id}: ${describeError(err)}`);
    }
  }
  if (purgedIds.length === 0) return;
  try {
    await coordinator.confirmPurge(purgedIds);
  } catch (err) {
    console.error(`scheduled: confirmPurge failed: ${describeError(err)}`);
  }
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
    try {
      validateConfig(env);
    } catch (err) {
      console.error("scheduled: configuration_error", err);
      return;
    }
    ctx.waitUntil(runScheduledDispatch(env));
    ctx.waitUntil(runScheduledCleanup(env, event?.scheduledTime));
  },
};
