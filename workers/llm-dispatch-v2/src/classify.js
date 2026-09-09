import { upstreamCapacityFailure, parseRetryAfterSeconds } from "./gateway.js";

/**
 * Ordered rule table for HTTP 429 responses. First match wins.
 * Each rule is data: { rule_id, provider, failure_class, match }.
 * `provider: null` applies to any provider.
 */
export const FAILURE_SIGNATURES = [
  {
    rule_id: "gemini-rpd",
    provider: "gemini",
    failure_class: "own_rpd",
    match: ({ msg }) =>
      msg.includes("perday") ||
      msg.includes("requests per day") ||
      msg.includes("generaterequestsperdayper"),
  },
  {
    rule_id: "gemini-tpm",
    provider: "gemini",
    failure_class: "own_tpm",
    match: ({ msg }) =>
      msg.includes("inputtokensperminute") || msg.includes("tokens per minute"),
  },
  {
    rule_id: "gemini-rpm",
    provider: "gemini",
    failure_class: "own_rpm",
    match: ({ msg }) =>
      msg.includes("requests per minute") ||
      msg.includes("generaterequestsperminuteper"),
  },
  {
    rule_id: "gemini-resource-exhausted",
    provider: "gemini",
    failure_class: "own_rpm",
    match: ({ body }) => body?.error?.status === "RESOURCE_EXHAUSTED",
  },
  {
    rule_id: "groq-tpd",
    provider: "groq",
    failure_class: "own_rpd",
    match: ({ msg }) =>
      msg.includes("tokens per day") || msg.includes("requests per day"),
  },
  {
    rule_id: "groq-rate-limit",
    provider: "groq",
    failure_class: "own_rpm",
    match: ({ body }) => body?.error?.code === "rate_limit_exceeded",
  },
  {
    rule_id: "airforce-guaranteed-response",
    provider: "airforce",
    failure_class: "upstream_capacity",
    match: ({ msg }) => msg.includes("guaranteed response"),
  },
  {
    rule_id: "opencode-server-error",
    provider: "opencode",
    failure_class: "upstream_capacity",
    match: ({ body }) => body?.error?.type === "server_error",
  },
  {
    rule_id: "openrouter-upstream",
    provider: "openrouter",
    failure_class: "upstream_capacity",
    match: ({ body, msg }) => {
      const meta = body?.error?.metadata;
      return (
        msg.includes("provider returned error") ||
        meta?.limit_source === "upstream_provider_shared_pool" ||
        (typeof meta?.raw === "string" && meta.raw.toLowerCase().includes("upstream"))
      );
    },
  },
  {
    rule_id: "openai-shaped-rate-limit",
    provider: null,
    failure_class: "own_rpm",
    match: ({ body }) =>
      body?.error?.type === "rate_limit_exceeded" ||
      body?.error?.code === "rate_limit_exceeded",
  },
  {
    rule_id: "remaining-zero-header",
    provider: null,
    failure_class: "own_rpm",
    match: ({ headers }) =>
      headers?.get("x-ratelimit-remaining-requests") === "0" ||
      headers?.get("x-ratelimit-remaining-req-minute") === "0",
  },
  {
    rule_id: "remaining-tokens-zero-header",
    provider: null,
    failure_class: "own_tpm",
    match: ({ headers }) =>
      headers?.get("x-ratelimit-remaining-tokens") === "0",
  },
  {
    rule_id: "overloaded",
    provider: null,
    failure_class: "upstream_capacity",
    match: ({ msg }) => {
      const patterns = [
        "overloaded",
        "no capacity",
        "capacity",
        "try again later",
        "temporarily unavailable",
        "model is unavailable",
        "upstream",
        "server busy",
        "all providers",
        "no available provider",
        "service unavailable",
      ];
      return patterns.some((p) => msg.includes(p));
    },
  },
  {
    rule_id: "concurrency",
    provider: null,
    failure_class: "upstream_capacity",
    match: ({ msg }) =>
      msg.includes("concurrent") || msg.includes("too many concurrent requests"),
  },
];

function normalizeHeaders(headers) {
  if (!headers) {
    return {
      get: () => null,
      has: () => false,
      entries: () => [],
    };
  }
  if (typeof headers.get === "function") {
    return headers;
  }
  const lowerMap = new Map();
  for (const [k, v] of Object.entries(headers)) {
    lowerMap.set(String(k).toLowerCase(), String(v));
  }
  return {
    get: (name) => lowerMap.get(String(name).toLowerCase()) ?? null,
    has: (name) => lowerMap.has(String(name).toLowerCase()),
    entries: () => lowerMap.entries(),
  };
}

function hasRateLimitHeader(normHeaders) {
  if (normHeaders.get("retry-after") !== null) return true;
  if (normHeaders.get("x-ratelimit-remaining-requests") !== null) return true;
  if (normHeaders.get("x-ratelimit-remaining-tokens") !== null) return true;
  if (normHeaders.get("x-ratelimit-reset-requests") !== null) return true;
  if (normHeaders.get("x-ratelimit-reset-tokens") !== null) return true;
  if (normHeaders.get("x-ratelimit-limit-requests") !== null) return true;
  if (normHeaders.get("x-ratelimit-limit-tokens") !== null) return true;
  if (typeof normHeaders.entries === "function") {
    for (const [k] of normHeaders.entries()) {
      if (String(k).toLowerCase().startsWith("x-ratelimit-")) {
        return true;
      }
    }
  }
  return false;
}

/**
 * Classify a provider failure response into the 9-class taxonomy.
 * Pure function with zero caller side effects.
 *
 * @param {{status:number, body:any, headers:Headers|Object|null, route:{provider:string,route_id:string,
 *          upstream_429_default?:string}}} input
 * @returns {{failure_class:string, rule_id:string, retry_after_seconds:number|null,
 *            scope:"route"|"provider"|"account"}}
 */
export function classifyProviderFailure({ status, body, headers, route }) {
  const normHeaders = normalizeHeaders(headers);
  const retryAfterSeconds = parseRetryAfterSeconds({ headers: normHeaders }, body);

  // 1. HTTP 402 -> payment_required
  if (status === 402) {
    return {
      failure_class: "payment_required",
      rule_id: "http-402",
      retry_after_seconds: retryAfterSeconds,
      scope: "route",
    };
  }

  // 2. HTTP 5xx -> server_error
  if (status >= 500 && status <= 599) {
    return {
      failure_class: "server_error",
      rule_id: "http-5xx",
      retry_after_seconds: retryAfterSeconds,
      scope: "route",
    };
  }

  // 3. HTTP 400 with upstream capacity error in body -> upstream_capacity
  if (status === 400 && upstreamCapacityFailure(status, body)) {
    return {
      failure_class: "upstream_capacity",
      rule_id: "upstream-400-body",
      retry_after_seconds: retryAfterSeconds,
      scope: "route",
    };
  }

  // 4. Cloudflare AI Gateway rate limit or rejection
  const hasCfAigError = normHeaders.has("cf-aig-error");
  const isAig429 =
    status === 429 &&
    !hasRateLimitHeader(normHeaders) &&
    !(body && typeof body === "object" && body.error);

  if (hasCfAigError || isAig429) {
    return {
      failure_class: "gateway_limit",
      rule_id: "cf-aig",
      retry_after_seconds: retryAfterSeconds,
      scope: "provider",
    };
  }

  // 5. HTTP 429 -> walk FAILURE_SIGNATURES
  if (status === 429) {
    const rawMsg =
      body?.error?.message ||
      body?.message ||
      body?.detail ||
      (typeof body === "string" ? body : "");
    const msg = String(rawMsg || "").toLowerCase();
    const routeProvider = route?.provider || "";

    const matchContext = { status, body, headers: normHeaders, msg };

    for (const rule of FAILURE_SIGNATURES) {
      if (rule.provider !== null && rule.provider !== routeProvider) {
        continue;
      }
      if (rule.match(matchContext)) {
        return {
          failure_class: rule.failure_class,
          rule_id: rule.rule_id,
          retry_after_seconds: retryAfterSeconds,
          scope: "route",
        };
      }
    }

    // 6. HTTP 429 unmatched -> check route default or unmatched-429
    if (route?.upstream_429_default === "upstream_capacity") {
      return {
        failure_class: "upstream_capacity",
        rule_id: "route-default-upstream",
        retry_after_seconds: retryAfterSeconds,
        scope: "route",
      };
    }

    return {
      failure_class: "unknown_429",
      rule_id: "unmatched-429",
      retry_after_seconds: retryAfterSeconds,
      scope: "route",
    };
  }

  // 7. Any other status -> request_defect
  return {
    failure_class: "request_defect",
    rule_id: "http-4xx",
    retry_after_seconds: retryAfterSeconds,
    scope: "route",
  };
}
