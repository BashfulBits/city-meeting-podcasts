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

  return { apiKey, url, upstreamModel: route.upstream_model, usesGateway: Boolean(aiGatewayBase) };
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

  return {
    status: response.status,
    ok: response.ok && parseError === null,
    body,
    parseError,
    correlationId,
  };
}

/** Sums prompt/completion tokens from an OpenAI-shaped `usage` object, if present. */
export function observedTokens(body) {
  const usage = body?.usage;
  if (!usage || typeof usage !== "object") return { input: null, output: null };
  const input = Number.isFinite(usage.prompt_tokens) ? usage.prompt_tokens : null;
  const output = Number.isFinite(usage.completion_tokens) ? usage.completion_tokens : null;
  return { input, output };
}
