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
