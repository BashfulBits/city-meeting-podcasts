/**
 * Protocol schema and request validation for LLM Dispatch v2.
 */

export async function sha256Hex(data) {
  const dataBuf = typeof data === "string" ? new TextEncoder().encode(data) : data;
  const digest = await crypto.subtle.digest("SHA-256", dataBuf);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Not currently called anywhere in the live request path -- request_digest is validated as a
// non-empty string only (see validateEnqueueJob) and never independently recomputed from the
// payload here. If a future change wires computeRequestDigest() into server-side verification,
// align its canonicalization with the Python producer's first (citypods/compute/llm.py's
// enqueue_batch uses json.dumps(payload, sort_keys=True), which differs from canonicalJson's
// compact/raw-UTF-8 output for non-ASCII content) -- otherwise equivalent payloads from the two
// producers would hash differently and a legitimate idempotent replay could be misdiagnosed as
// an idempotency_conflict.
export function canonicalJson(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  const keys = Object.keys(value).sort();
  const pairs = keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`);
  return `{${pairs.join(",")}}`;
}

export async function computeRequestDigest(requestPayload) {
  const canonical = canonicalJson(requestPayload);
  return sha256Hex(canonical);
}

export function validateEnqueueJob(job) {
  if (!job || typeof job !== "object" || Array.isArray(job)) {
    return { valid: false, error: "invalid_job", detail: "Job must be an object" };
  }

  if (typeof job.idempotency_key !== "string" || !job.idempotency_key.trim()) {
    return { valid: false, error: "invalid_job", detail: "Job missing valid idempotency_key" };
  }

  if (typeof job.request_digest !== "string" || !job.request_digest.trim()) {
    return { valid: false, error: "invalid_job", detail: "Job missing valid request_digest" };
  }

  if (typeof job.prompt_family !== "string" || !job.prompt_family.trim()) {
    return { valid: false, error: "invalid_job", detail: "Job missing valid prompt_family" };
  }

  if (typeof job.payload_key !== "string" || !job.payload_key.trim()) {
    return { valid: false, error: "invalid_job", detail: "Job missing valid payload_key" };
  }

  const inTokens = Number(job.input_token_estimate);
  if (!Number.isFinite(inTokens) || inTokens < 0) {
    return { valid: false, error: "invalid_job", detail: "Job input_token_estimate must be >= 0" };
  }

  const outTokens = Number(job.max_output_token_estimate);
  if (!Number.isFinite(outTokens) || outTokens < 0) {
    return { valid: false, error: "invalid_job", detail: "Job max_output_token_estimate must be >= 0" };
  }

  if (job.priority !== undefined && job.priority !== 0 && job.priority !== 1) {
    return { valid: false, error: "invalid_job", detail: "Job priority must be 0 or 1" };
  }

  return { valid: true };
}

export function validateEnqueueBatchRequest(body, maxBatchSize = 1000) {
  if (!body || typeof body !== "object" || !Array.isArray(body.jobs)) {
    return { valid: false, error: "invalid_request", detail: "Request body must contain 'jobs' array" };
  }

  if (body.jobs.length === 0) {
    return { valid: false, error: "invalid_request", detail: "Jobs array must not be empty" };
  }

  if (body.jobs.length > maxBatchSize) {
    return {
      valid: false,
      error: "batch_too_large",
      detail: `Batch size ${body.jobs.length} exceeds maximum limit of ${maxBatchSize}`,
    };
  }

  for (let i = 0; i < body.jobs.length; i++) {
    const check = validateEnqueueJob(body.jobs[i]);
    if (!check.valid) {
      return { valid: false, error: check.error, detail: `jobs[${i}]: ${check.detail}` };
    }
  }

  return { valid: true };
}

export function validatePollBatchRequest(body, maxBatchSize = 1000) {
  if (!body || typeof body !== "object" || !Array.isArray(body.ids)) {
    return { valid: false, error: "invalid_request", detail: "Request body must contain 'ids' array" };
  }

  if (body.ids.length === 0) {
    return { valid: false, error: "invalid_request", detail: "Ids array must not be empty" };
  }

  if (body.ids.length > maxBatchSize) {
    return {
      valid: false,
      error: "batch_too_large",
      detail: `Batch size ${body.ids.length} exceeds maximum limit of ${maxBatchSize}`,
    };
  }

  for (let i = 0; i < body.ids.length; i++) {
    if (typeof body.ids[i] !== "string" || !body.ids[i].trim()) {
      return { valid: false, error: "invalid_request", detail: `ids[${i}] must be a non-empty string` };
    }
  }

  return { valid: true };
}

export function validateSchemaRetryRequest(body) {
  if (
    !body ||
    typeof body !== "object" ||
    typeof body.corrected_payload_key !== "string" ||
    !body.corrected_payload_key.trim()
  ) {
    return {
      valid: false,
      error: "invalid_request",
      detail: "Body must contain 'corrected_payload_key'",
    };
  }
  if (
    typeof body.corrected_request_digest !== "string" ||
    !body.corrected_request_digest.trim()
  ) {
    return {
      valid: false,
      error: "invalid_request",
      detail: "Body must contain 'corrected_request_digest'",
    };
  }
  const inputTokens = body.corrected_input_token_estimate;
  if (typeof inputTokens !== "number" || !Number.isFinite(inputTokens) || inputTokens < 0) {
    return {
      valid: false,
      error: "invalid_request",
      detail: "Body corrected_input_token_estimate must be >= 0",
    };
  }
  return { valid: true };
}

export function validateResolveUnknownBatchRequest(body) {
  if (!body || typeof body !== "object" || !Array.isArray(body.attempt_ids) || body.attempt_ids.length === 0) {
    return { valid: false, error: "invalid_request", detail: "Body must contain non-empty 'attempt_ids' array" };
  }
  return { valid: true };
}

/**
 * `POST /v2/jobs:retire-batch` -- `{items: [{id, result_key}]}`: completed jobs the caller has
 * durably consumed and whose B2 objects it has already deleted (see retireConsumed).
 */
export function validateRetireBatchRequest(body, maxBatchSize = 1000) {
  if (!body || typeof body !== "object" || !Array.isArray(body.items)) {
    return { valid: false, error: "invalid_request", detail: "Request body must contain 'items' array" };
  }
  if (body.items.length === 0) {
    return { valid: false, error: "invalid_request", detail: "Items array must not be empty" };
  }
  if (body.items.length > maxBatchSize) {
    return {
      valid: false,
      error: "batch_too_large",
      detail: `Batch size ${body.items.length} exceeds maximum limit of ${maxBatchSize}`,
    };
  }
  for (let i = 0; i < body.items.length; i++) {
    const item = body.items[i];
    if (
      !item ||
      typeof item !== "object" ||
      typeof item.id !== "string" ||
      !item.id.trim() ||
      typeof item.result_key !== "string" ||
      !item.result_key.trim()
    ) {
      return {
        valid: false,
        error: "invalid_request",
        detail: `items[${i}] must have non-empty string 'id' and 'result_key'`,
      };
    }
  }
  return { valid: true };
}

const PAUSE_SCOPES = new Set(["global", "provider", "route"]);

function validatePauseSelection(body) {
  if (!body || typeof body !== "object") {
    return { valid: false, error: "invalid_request", detail: "Request body must be an object" };
  }
  if (!PAUSE_SCOPES.has(body.scope)) {
    return { valid: false, error: "invalid_request", detail: "scope must be global, provider or route" };
  }
  if (body.scope === "global") {
    if (body.target != null) {
      return { valid: false, error: "invalid_request", detail: "global scope takes no target" };
    }
  } else if (typeof body.target !== "string" || !body.target.trim()) {
    return { valid: false, error: "invalid_request", detail: `${body.scope} scope requires a target` };
  }
  return { valid: true };
}

/**
 * `POST /v2/dispatch:pause` -- `{scope, target?, seconds, reason?}`. `seconds` is required and
 * bounded so every pause ends by itself: a probe that dies mid-run cannot leave dispatch halted.
 */
export function validatePauseRequest(body, maxSeconds = 3600) {
  const selection = validatePauseSelection(body);
  if (!selection.valid) return selection;
  if (!Number.isInteger(body.seconds) || body.seconds < 1 || body.seconds > maxSeconds) {
    return {
      valid: false,
      error: "invalid_request",
      detail: `seconds must be an integer from 1 to ${maxSeconds}`,
    };
  }
  if (body.reason != null && typeof body.reason !== "string") {
    return { valid: false, error: "invalid_request", detail: "reason must be a string" };
  }
  return { valid: true };
}

/** `POST /v2/dispatch:resume` -- `{scope, target?}`. */
export function validateResumeRequest(body) {
  return validatePauseSelection(body);
}

/** `POST /v2/dispatch:reserve` -- `{route_id, requests}`: charge out-of-band probe calls. */
export function validateReserveRequest(body, maxRequests = 5) {
  if (body && Object.hasOwn(body, "operation")) {
    const common = ["operation", "run_id", "catalog_digest"];
    const admit = ["route_id", "dimension", "attempt_id", "input_tokens", "output_tokens",
      "request_digest"];
    const manualStart = ["route_ids", "dimensions", "max_requests", "max_input", "max_output",
      "per_call_input", "per_call_output", "purpose"];
    const fields = body.operation === "context_manual_start" ? [...common, ...manualStart]
      : body.operation === "context_manual_finish" ? common
      : body.operation === "context_manual_admit" ? [...common, ...admit]
      : body.operation === "context_start" ? common
      : body.operation === "context_admit" ? [...common, ...admit] : [];
    const hash = value => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
    const integer = value => Number.isSafeInteger(value) && value > 0;
    const valid = !Array.isArray(body) && fields.length > 0 &&
      Object.keys(body).every(key => fields.includes(key)) &&
      fields.every(key => Object.hasOwn(body, key)) &&
      typeof body.run_id === "string" && /^[1-9][0-9]{0,19}$/.test(body.run_id) &&
      hash(body.catalog_digest) && (body.operation === "context_manual_start" ? (
        Array.isArray(body.route_ids) && body.route_ids.length > 0 && body.route_ids.length <= 8 &&
        new Set(body.route_ids).size === body.route_ids.length &&
        body.route_ids.every(id => typeof id === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(id)) &&
        Array.isArray(body.dimensions) && body.dimensions.length > 0 && body.dimensions.length <= 2 &&
        new Set(body.dimensions).size === body.dimensions.length &&
        body.dimensions.every(d => ["input", "output"].includes(d)) &&
        integer(body.max_requests) && body.max_requests <= 8 &&
        integer(body.max_input) && body.max_input <= 2097152 &&
        integer(body.max_output) && body.max_output <= 131072 &&
        integer(body.per_call_input) && body.per_call_input <= 524288 &&
        integer(body.per_call_output) && body.per_call_output <= 32768 &&
        typeof body.purpose === "string" && body.purpose.length > 0 && body.purpose.length <= 256
      ) : ["context_start", "context_manual_finish"].includes(body.operation) || (
        typeof body.route_id === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(body.route_id) &&
        ["input", "output"].includes(body.dimension) && hash(body.attempt_id) &&
        hash(body.request_digest) && integer(body.input_tokens) && integer(body.output_tokens)
      ));
    return valid ? { valid: true }
      : { valid: false, error: "invalid_request", detail: "invalid context reservation" };
  }
  if (!body || typeof body !== "object" || typeof body.route_id !== "string" || !body.route_id.trim()) {
    return { valid: false, error: "invalid_request", detail: "route_id must be a non-empty string" };
  }
  if (!Number.isInteger(body.requests) || body.requests < 1 || body.requests > maxRequests) {
    return {
      valid: false,
      error: "invalid_request",
      detail: `requests must be an integer from 1 to ${maxRequests}`,
    };
  }
  return { valid: true };
}
