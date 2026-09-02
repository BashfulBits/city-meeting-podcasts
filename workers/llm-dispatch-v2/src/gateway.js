/**
 * Provider credential resolution and AI Gateway request construction for LLM Dispatch v2's
 * executor. Adapted from workers/llm-dispatch-proxy/src/index.js's resolveProviderCredentials/
 * upstreamRequestForRoute (per review/44 Phase 1's "extract only pure route-catalog selection and
 * response-normalization helpers from v1; do not fork provider credential logic without tests")
 * -- the account/API-key/Gateway-URL resolution here is the SAME logic, not a rewrite, just
 * re-keyed off v2's own route/job shapes instead of v1's queue record.
 */

// Every real, provider-facing chat-completions field this Worker forwards -- everything else on
// the stored payload is dropped, not spread. Mirrors workers/llm-dispatch-proxy/src/index.js's
// own COPY_FIELDS exactly (that Worker's normalizeChatRequest() rebuilds its request object this
// same way, field-by-field, from the raw incoming HTTP body -- this module's own header comment
// claimed to share that logic without actually including this step, which is how the 2026-08-26
// incident below happened). `messages` is handled separately below since it's required, not
// optional like these.
const COPY_FIELDS = [
  "temperature",
  "top_p",
  "max_tokens",
  "max_completion_tokens",
  "response_format",
  "tools",
  "tool_choice",
  "seed",
  "presence_penalty",
  "frequency_penalty",
];

/** Builds the actual provider request from a stored payload: remaps the logical model (e.g.
 * "gemini/gemini-flash-lite") to the route's real upstream_model string, and forwards only
 * COPY_FIELDS plus `messages` -- never the raw payload via a blind spread.
 *
 * Deliberately an allowlist, not a blocklist: citypods/compute/llm.py's `_payload()` builder is
 * shared with v1's synchronous transport, where policy fields (allow_paid, allow_batch,
 * submit_next, timeout_class, allowed_models, output_token_budget, deadline_at, ...) are expected
 * to ride alongside model/messages in the same dict -- v1's own ingress (normalizeChatRequest)
 * strips them back out before ever building an upstream request. v2 had no equivalent step: this
 * function used to spread the stored payload verbatim into the literal HTTP body sent to the
 * provider, so any policy field that ended up in a stored payload (as one call site did until
 * fixed in citypods/compute/llm.py) became a literal field in that body -- Mistral and Groq both
 * correctly rejected it as an unrecognized field, a 100% dispatch failure invisible until AI
 * Gateway logging surfaced it. An allowlist here means that class of bug can only ever produce an
 * inert extra key that gets silently dropped, not a live incident -- and it retroactively repairs
 * every payload already sitting in B2 from before the write-side fix, since this reads fresh at
 * dispatch time rather than a cached copy: nothing needs to be re-enqueued or backfilled. */
export function upstreamRequestForRoute(payload, route) {
  const request = { model: route.upstream_model, messages: payload?.messages, stream: false };
  for (const field of COPY_FIELDS) {
    if (payload && payload[field] !== undefined) {
      request[field] = payload[field];
    }
  }
  return request;
}

/** Resolves which account/API key to use, and whether this call goes through AI Gateway or
 * directly to the provider, exactly matching v1's own resolution order and error messages. */
export function resolveProviderCredentials(env, route, dispatchLimits) {
  const providerCfg = dispatchLimits.providers?.[route.provider];
  if (!providerCfg) {
    throw new Error(`no provider config compiled for provider ${route.provider}`);
  }
  const accounts = providerCfg.accounts || [];
  const account = route.account_id
    ? accounts.find((candidate) => candidate.id === route.account_id)
    : accounts[0];
  if (!account?.api_key_env) {
    throw new Error(`no account configured for provider ${route.provider} route ${route.route_id}`);
  }
  const apiKey = env[account.api_key_env];
  if (!apiKey) {
    throw new Error(`missing secret ${account.api_key_env} for provider ${route.provider}`);
  }

  const apiBase = String(providerCfg.api_base || "").replace(/\/+$/, "");
  if (!apiBase) {
    throw new Error(`no api_base configured for provider ${route.provider}`);
  }
  const chatPath = providerCfg.chat_path || "/v1/chat/completions";
  const directUrlString = `${apiBase}${chatPath}`;

  let parsedDirectUrl;
  try {
    parsedDirectUrl = new URL(directUrlString);
  } catch {
    throw new Error(`provider ${route.provider} api_base/chat_path is not a valid URL`);
  }
  if (parsedDirectUrl.protocol !== "https:") {
    throw new Error(`provider ${route.provider} api_base must use HTTPS`);
  }

  let url = directUrlString;
  let aiGatewayBase = String(env?.AI_GATEWAY_BASE_URL || "").trim().replace(/\/+$/, "");
  if (!aiGatewayBase && env?.CLOUDFLARE_ACCOUNT_ID && env?.AI_GATEWAY_ID) {
    const accountId = String(env.CLOUDFLARE_ACCOUNT_ID).trim();
    const gatewayId = String(env.AI_GATEWAY_ID).trim();
    if (accountId && gatewayId) {
      aiGatewayBase = `https://gateway.ai.cloudflare.com/v1/${encodeURIComponent(accountId)}/${encodeURIComponent(gatewayId)}`;
    }
  }

  if (aiGatewayBase) {
    let parsedGateway;
    try {
      parsedGateway = new URL(aiGatewayBase);
    } catch {
      throw new Error("AI_GATEWAY_BASE_URL is not a valid URL");
    }
    if (parsedGateway.protocol !== "https:") {
      throw new Error("AI_GATEWAY_BASE_URL must use HTTPS");
    }
    const gatewaySlug = providerCfg.ai_gateway_slug || route.provider;
    const gatewayPath = providerCfg.ai_gateway_chat_path
      ? `${providerCfg.ai_gateway_chat_path}${parsedDirectUrl.search}`
      : `${parsedDirectUrl.pathname}${parsedDirectUrl.search}`;
    url = `${aiGatewayBase}/${gatewaySlug}${gatewayPath}`;
  }

  return {
    apiKey,
    url,
    upstreamModel: route.upstream_model,
    usesGateway: Boolean(aiGatewayBase),
    aiGatewayMaxAttempts: route.ai_gateway_max_attempts ?? providerCfg.ai_gateway_max_attempts ?? null,
  };
}

/**
 * Sends one chat-completion call for a claimed job. Returns { status, ok, body, usage,
 * correlationId } -- never throws for a normal (even non-2xx) provider response; only network-
 * level failures (abort, DNS, TLS) throw, which the caller maps to a retryable/terminal outcome
 * itself.
 */
export async function callAiGateway({ env, route, payload, dispatchLimits, idempotencyKey, signal }) {
  const creds = resolveProviderCredentials(env, route, dispatchLimits);
  const upstreamPayload = upstreamRequestForRoute(payload, route);

  const headers = { accept: "application/json", "content-type": "application/json" };
  if (creds.apiKey) headers.authorization = `Bearer ${creds.apiKey}`;
  // Matches v1: only attach the gateway's own auth when this call actually goes through it, and
  // only when a token is configured -- a direct-to-provider call has no gateway edge to
  // authenticate against.
  if (creds.usesGateway && env.AI_GATEWAY_AUTH_TOKEN) {
    headers["cf-aig-authorization"] = `Bearer ${env.AI_GATEWAY_AUTH_TOKEN}`;
  }
  if (creds.usesGateway && creds.aiGatewayMaxAttempts != null) {
    headers["cf-aig-max-attempts"] = String(creds.aiGatewayMaxAttempts);
  }
  if (idempotencyKey) {
    headers["idempotency-key"] = idempotencyKey;
  }

  const response = await fetch(creds.url, {
    method: "POST",
    headers,
    body: JSON.stringify(upstreamPayload),
    redirect: "manual",
    signal,
  });

  const correlationId = response.headers.get("cf-aig-log-id") || response.headers.get("cf-ray") || null;
  let body = null;
  let parseError = null;
  try {
    body = await response.json();
  } catch (err) {
    parseError = err;
  }
  const retryAfterSeconds = parseRetryAfterSeconds(response, body);

  return {
    status: response.status,
    ok: response.ok && parseError === null,
    body,
    parseError,
    correlationId,
    retryAfterSeconds,
  };
}

/**
 * A 4xx whose *body* reports a provider-side failure: the status blames the request, the payload
 * blames the upstream. Treated as transport, not as a defect in the job.
 *
 * OpenCode Zen returns HTTP 400 with `{"error":{"type":"server_error","message":"Error from
 * provider (Console): Upstream request failed: Model is unavailable."}}` while still advertising
 * the model in its own /v1/models listing. Classified as terminal, every job that reached it was
 * destroyed rather than retried.
 *
 * Deliberately narrow. It applies only to 400 (other 4xx really are request defects: 401 bad
 * credentials, 404 unknown model, 422 schema), and only when the body self-identifies as a server
 * or upstream failure. A provider that returns a genuine 400 for a malformed request says nothing
 * of the sort, and still fails terminally as it should.
 *
 * Kept byte-identical in behaviour to v1's copy in workers/llm-dispatch-proxy: the two dispatchers
 * face the same providers, and a body that costs a job in one must not cost it in the other.
 */
export function upstreamCapacityFailure(status, body) {
  if (status !== 400) return false;
  const providerError = body?.error;
  if (!providerError || typeof providerError !== "object") return false;
  if (String(providerError.type || "").toLowerCase() === "server_error") return true;
  const message = String(providerError.message || "").toLowerCase();
  return (
    message.includes("upstream request failed") ||
    message.includes("model is unavailable") ||
    message.includes("no capacity") ||
    message.includes("temporarily unavailable")
  );
}

/**
 * Parse Go/Groq-style duration strings into whole seconds, rounding up.
 * Examples: "7.66s", "2m59.56s", "500ms", "1h30m", "15s".
 */
export function parseDurationSeconds(str) {
  if (typeof str !== "string" || !str.trim()) return null;
  const s = str.trim();
  const durationRegex =
    /^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$/;
  const match = s.match(durationRegex);
  if (!match) return null;
  const [, h, m, sec, ms] = match;
  if (!h && !m && !sec && !ms) return null;
  const totalSeconds =
    (h ? parseFloat(h) * 3600 : 0) +
    (m ? parseFloat(m) * 60 : 0) +
    (sec ? parseFloat(sec) : 0) +
    (ms ? parseFloat(ms) / 1000 : 0);
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return 0;
  return Math.ceil(totalSeconds);
}

/**
 * Extract retry / cooldown delay from provider error message strings.
 * Handles patterns such as:
 * - "Try again in 1.0 seconds. Your next guaranteed response is in 119 seconds." -> 1
 * - "Please try again in 20s." -> 20
 * - "Rate limit reached. Please try again in 2m30s." -> 150
 * - "Please wait 15 seconds before retrying" -> 15
 * - "Your next guaranteed response is in 119 seconds" -> 119
 */
export function parseErrorMessageRetryAfter(message) {
  if (typeof message !== "string" || !message.trim()) return null;

  // Prioritize guaranteed response windows (e.g. Airforce "Your next guaranteed response is in 119 seconds")
  // before short burst hints (e.g. "Try again in 1.0 seconds").
  const guaranteedRegex =
    /(?:guaranteed response (?:is )?in)\s*([0-9]+(?:\.[0-9]+)?\s*[a-z0-9.]+)/i;
  const generalRegex =
    /(?:try again in|retry after|wait|retry in|reset in|response (?:is )?in)\s*([0-9]+(?:\.[0-9]+)?\s*[a-z0-9.]+)/i;

  const match = message.match(guaranteedRegex) || message.match(generalRegex);
  if (!match) return null;
  const raw = match[1].trim().replace(/[.,;:]+$/, "");
  const durationSec = parseDurationSeconds(raw);
  if (durationSec !== null && durationSec > 0) return durationSec;

  const numericWithUnit = raw.match(
    /^(\d+(?:\.\d+)?)\s*(hours?|h|minutes?|mins?|m|seconds?|secs?|s|ms)$/i
  );
  if (numericWithUnit) {
    const val = parseFloat(numericWithUnit[1]);
    const unit = numericWithUnit[2].toLowerCase();
    if (unit.startsWith("h")) return Math.ceil(val * 3600);
    if (unit.startsWith("m") && !unit.startsWith("ms")) return Math.ceil(val * 60);
    if (unit.startsWith("ms")) return Math.ceil(val / 1000);
    if (unit.startsWith("s")) return Math.ceil(val);
  }
  const numeric = Number(raw);
  if (Number.isFinite(numeric) && numeric > 0) return Math.ceil(numeric);
  return null;
}

/**
 * Extract and parse standard Retry-After or provider reset headers (and fallback error body
 * messages) into whole seconds. Handles integer seconds, duration strings (e.g. "7.66s",
 * "2m59.56s"), HTTP-date strings, and error message text like "Try again in 1.0 seconds".
 * Returns null if no valid delay can be found.
 */
export function parseRetryAfterSeconds(response, body = null) {
  if (response && response.headers) {
    const raw =
      response.headers.get("retry-after") ||
      response.headers.get("x-ratelimit-reset-requests") ||
      response.headers.get("x-ratelimit-reset-tokens");
    if (raw) {
      const trimmed = raw.trim();
      const numeric = Number(trimmed);
      if (Number.isFinite(numeric) && numeric > 0) {
        return Math.ceil(numeric);
      }
      const durationSec = parseDurationSeconds(trimmed);
      if (durationSec !== null && durationSec > 0) {
        return durationSec;
      }
      const dateMs = Date.parse(trimmed);
      if (Number.isFinite(dateMs)) {
        const diffSec = Math.ceil((dateMs - Date.now()) / 1000);
        return diffSec > 0 ? diffSec : 0;
      }
    }
  }
  if (body) {
    const directRetrySec = Number(
      body?.retry_after_seconds ??
      body?.retry_after ??
      body?.error?.retry_after_seconds ??
      body?.error?.retry_after
    );
    if (Number.isFinite(directRetrySec) && directRetrySec > 0) {
      return Math.ceil(directRetrySec);
    }
    const message =
      body?.error?.message ||
      body?.error?.detail ||
      body?.message ||
      body?.detail ||
      (typeof body === "string" ? body : null);
    if (message) {
      const parsed = parseErrorMessageRetryAfter(message);
      if (parsed !== null && parsed > 0) {
        return parsed;
      }
    }
  }
  return null;
}

/** Sums prompt/completion tokens from an OpenAI-shaped `usage` object, if present. */
export function observedTokens(body) {
  const usage = body?.usage;
  if (!usage || typeof usage !== "object") return { input: null, output: null };
  const input = Number.isFinite(usage.prompt_tokens) ? usage.prompt_tokens : null;
  const output = Number.isFinite(usage.completion_tokens) ? usage.completion_tokens : null;
  return { input, output };
}
