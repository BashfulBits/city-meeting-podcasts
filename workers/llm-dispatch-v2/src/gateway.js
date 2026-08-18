/**
 * Provider credential resolution and AI Gateway request construction for LLM Dispatch v2's
 * executor. Adapted from workers/llm-dispatch-proxy/src/index.js's resolveProviderCredentials/
 * upstreamRequestForRoute (per review/44 Phase 1's "extract only pure route-catalog selection and
 * response-normalization helpers from v1; do not fork provider credential logic without tests")
 * -- the account/API-key/Gateway-URL resolution here is the SAME logic, not a rewrite, just
 * re-keyed off v2's own route/job shapes instead of v1's queue record.
 */

/** Remaps the stored payload's logical model (e.g. "gemini/gemini-flash-lite") to the route's
 * actual upstream_model string, exactly as v1's upstreamRequestForRoute does -- the payload was
 * written by the Python client with the logical name (see citypods/compute/llm.py's _payload). */
export function upstreamRequestForRoute(payload, route) {
  return {
    ...payload,
    model: route.upstream_model,
    stream: false,
  };
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
