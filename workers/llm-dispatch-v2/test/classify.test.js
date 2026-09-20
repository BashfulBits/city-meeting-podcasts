import test from "node:test";
import assert from "node:assert/strict";
import { classifyProviderFailure, FAILURE_SIGNATURES } from "../src/classify.js";
import { upstreamEmptyCompletion } from "../src/gateway.js";

test("classifyProviderFailure handles HTTP 402 as payment_required", () => {
  const res = classifyProviderFailure({
    status: 402,
    body: { error: "insufficient credits" },
    headers: null,
    route: { provider: "openrouter", route_id: "openrouter/free" },
  });
  assert.equal(res.failure_class, "payment_required");
  assert.equal(res.rule_id, "http-402");
  assert.equal(res.scope, "route");
});

test("classifyProviderFailure handles HTTP 5xx as server_error", () => {
  const res = classifyProviderFailure({
    status: 503,
    body: { error: "bad gateway" },
    headers: { "retry-after": "30" },
    route: { provider: "groq", route_id: "groq/llama" },
  });
  assert.equal(res.failure_class, "server_error");
  assert.equal(res.rule_id, "http-5xx");
  assert.equal(res.retry_after_seconds, 30);
  assert.equal(res.scope, "route");
});

test("classifyProviderFailure recognizes provider overload details in 503/504 responses", () => {
  const gemini = classifyProviderFailure({
    status: 503,
    body: {
      error: {
        status: "UNAVAILABLE",
        message: "This model is currently experiencing high demand.",
      },
    },
    headers: null,
    route: { provider: "gemini", route_id: "gemini/gemma" },
  });
  assert.equal(gemini.failure_class, "upstream_capacity");
  assert.equal(gemini.rule_id, "provider-5xx-capacity");

  const timeout = classifyProviderFailure({
    status: 504,
    body: "error code: 504",
    headers: null,
    route: { provider: "sambanova", route_id: "sambanova/gemma" },
  });
  assert.equal(timeout.failure_class, "upstream_capacity");
  assert.equal(timeout.rule_id, "http-504-timeout");
});

test("classifyProviderFailure recognizes an input limit in a provider 500", () => {
  const result = classifyProviderFailure({
    status: 500,
    body: { error: { message: "The input token limit was exceeded for this model." } },
    headers: null,
    route: { provider: "gemini", route_id: "gemini/gemma" },
  });
  assert.equal(result.failure_class, "route_input_limit");
  assert.equal(result.rule_id, "provider-input-limit");
});

test("classifyProviderFailure handles HTTP 400 upstream capacity as upstream_capacity", () => {
  const res = classifyProviderFailure({
    status: 400,
    body: {
      error: {
        type: "server_error",
        message: "Error from provider (Console): Upstream request failed: Model is unavailable.",
      },
    },
    headers: null,
    route: { provider: "opencode", route_id: "opencode/zen" },
  });
  assert.equal(res.failure_class, "upstream_capacity");
  assert.equal(res.rule_id, "upstream-400-body");
  assert.equal(res.scope, "route");
});

test("classifyProviderFailure handles NVIDIA missing-function 404 as upstream_capacity", () => {
  const res = classifyProviderFailure({
    status: 404,
    body: {
      status: 404,
      title: "Not Found",
      detail: "Function id 'abc' version 'null': Specified function is not found",
    },
    headers: null,
    route: { provider: "nvidia", route_id: "nvidia/nemotron" },
  });
  assert.equal(res.failure_class, "upstream_capacity");
  assert.equal(res.rule_id, "upstream-function-not-found");
  assert.equal(res.scope, "route");
});

test("classifyProviderFailure handles OpenCode MissingSessionID 400 as upstream_capacity", () => {
  const res = classifyProviderFailure({
    status: 400,
    body: {
      type: "error",
      error: {
        type: "MissingSessionID",
        message: "Error from provider (Console): OpenCode's free tier can only be used in OpenCode",
      },
    },
    headers: null,
    route: { provider: "opencode", route_id: "opencode/deepseek-v4-flash-free" },
  });
  assert.equal(res.failure_class, "upstream_capacity");
  assert.equal(res.rule_id, "upstream-400-body");
  assert.equal(res.scope, "route");
});

test("classifyProviderFailure leaves bare OpenCode MissingSessionID 400 as request_defect", () => {
  const res = classifyProviderFailure({
    status: 400,
    body: {
      type: "error",
      error: {
        type: "MissingSessionID",
        message: "Missing session ID in request headers",
      },
    },
    headers: null,
    route: { provider: "opencode", route_id: "opencode/deepseek-v4-flash-free" },
  });
  assert.equal(res.failure_class, "request_defect");
  assert.equal(res.scope, "route");
});

test("classifyProviderFailure handles CF AI Gateway error as gateway_limit with provider scope", () => {
  const resWithHeader = classifyProviderFailure({
    status: 429,
    body: { message: "rate limited by gateway" },
    headers: { "cf-aig-error": "rate_limit" },
    route: { provider: "groq", route_id: "groq/llama" },
  });
  assert.equal(resWithHeader.failure_class, "gateway_limit");
  assert.equal(resWithHeader.rule_id, "cf-aig");
  assert.equal(resWithHeader.scope, "provider");

  // 429 without rate limit headers and without error object
  const resBare429 = classifyProviderFailure({
    status: 429,
    body: "Too many requests to gateway",
    headers: {},
    route: { provider: "groq", route_id: "groq/llama" },
  });
  assert.equal(resBare429.failure_class, "gateway_limit");
  assert.equal(resBare429.rule_id, "cf-aig");
  assert.equal(resBare429.scope, "provider");

  // 429 with empty provider payload field (e.g. { message: "" }) must not be classified as gateway_limit
  const resEmptyPayload = classifyProviderFailure({
    status: 429,
    body: { message: "" },
    headers: {},
    route: { provider: "groq", route_id: "groq/llama" },
  });
  assert.notEqual(resEmptyPayload.failure_class, "gateway_limit");
  assert.notEqual(resEmptyPayload.scope, "provider");
  assert.equal(resEmptyPayload.scope, "route");
});

test("classifyProviderFailure matches gemini-rpd", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        message: "Resource exhausted: quota exceeded for GenerateRequestsPerDayPerProjectPerModel",
        status: "RESOURCE_EXHAUSTED",
      },
    },
    headers: null,
    route: { provider: "gemini", route_id: "gemini/flash" },
  });
  assert.equal(res.failure_class, "own_rpd");
  assert.equal(res.rule_id, "gemini-rpd");
  assert.equal(res.scope, "route");
});

test("classifyProviderFailure matches gemini-tpm", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        message: "Resource exhausted: InputTokensPerMinute exceeded",
      },
    },
    headers: null,
    route: { provider: "gemini", route_id: "gemini/flash" },
  });
  assert.equal(res.failure_class, "own_tpm");
  assert.equal(res.rule_id, "gemini-tpm");
});

test("classifyProviderFailure matches gemini-rpm", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        message: "Quota exceeded: requests per minute exceeded",
      },
    },
    headers: null,
    route: { provider: "gemini", route_id: "gemini/flash" },
  });
  assert.equal(res.failure_class, "own_rpm");
  assert.equal(res.rule_id, "gemini-rpm");
});

test("classifyProviderFailure matches gemini-resource-exhausted fallback", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        status: "RESOURCE_EXHAUSTED",
        message: "Some generic exhaustion message with no known metric",
      },
    },
    headers: null,
    route: { provider: "gemini", route_id: "gemini/flash" },
  });
  assert.equal(res.failure_class, "own_rpm");
  assert.equal(res.rule_id, "gemini-resource-exhausted");
});

test("ordering test: gemini-rpd wins over gemini-resource-exhausted", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        status: "RESOURCE_EXHAUSTED",
        message: "Quota exceeded: requests per day limit reached",
      },
    },
    headers: null,
    route: { provider: "gemini", route_id: "gemini/flash" },
  });
  assert.equal(res.failure_class, "own_rpd");
  assert.equal(res.rule_id, "gemini-rpd");
});

test("classifyProviderFailure matches groq-tpd", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        code: "rate_limit_exceeded",
        message: "Rate limit reached for model on tokens per day (TPD): limit 500000",
      },
    },
    headers: null,
    route: { provider: "groq", route_id: "groq/llama" },
  });
  assert.equal(res.failure_class, "own_rpd");
  assert.equal(res.rule_id, "groq-tpd");
});

test("classifyProviderFailure matches groq-rate-limit", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        code: "rate_limit_exceeded",
        message: "Rate limit reached for model on RPM: limit 30",
      },
    },
    headers: null,
    route: { provider: "groq", route_id: "groq/llama" },
  });
  assert.equal(res.failure_class, "own_rpm");
  assert.equal(res.rule_id, "groq-rate-limit");
});

test("classifyProviderFailure matches airforce-guaranteed-response", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        message: "Queue full. Your next guaranteed response is in 45 seconds.",
      },
    },
    headers: null,
    route: { provider: "airforce", route_id: "airforce/chat" },
  });
  assert.equal(res.failure_class, "upstream_capacity");
  assert.equal(res.rule_id, "airforce-guaranteed-response");
  assert.equal(res.retry_after_seconds, 45);
});

test("classifyProviderFailure matches opencode-server-error", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        type: "server_error",
        message: "Upstream rate limited",
      },
    },
    headers: null,
    route: { provider: "opencode", route_id: "opencode/free" },
  });
  assert.equal(res.failure_class, "upstream_capacity");
  assert.equal(res.rule_id, "opencode-server-error");
});

test("classifyProviderFailure matches openrouter-upstream", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        message: "Provider returned error",
        metadata: {
          limit_source: "upstream_provider_shared_pool",
          raw: "google/gemma is temporarily rate-limited upstream",
        },
      },
    },
    headers: null,
    route: { provider: "openrouter", route_id: "openrouter/free" },
  });
  assert.equal(res.failure_class, "upstream_capacity");
  assert.equal(res.rule_id, "openrouter-upstream");
});

test("classifyProviderFailure matches openai-shaped-rate-limit", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        type: "rate_limit_exceeded",
        message: "Rate limit reached",
      },
    },
    headers: null,
    route: { provider: "mistral", route_id: "mistral/large" },
  });
  assert.equal(res.failure_class, "own_rpm");
  assert.equal(res.rule_id, "openai-shaped-rate-limit");
});

test("classifyProviderFailure matches remaining-zero-header", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: { error: { message: "Slow down" } },
    headers: { "x-ratelimit-remaining-requests": "0" },
    route: { provider: "cerebras", route_id: "cerebras/llama" },
  });
  assert.equal(res.failure_class, "own_rpm");
  assert.equal(res.rule_id, "remaining-zero-header");

  const resMinute = classifyProviderFailure({
    status: 429,
    body: { error: { message: "Slow down" } },
    headers: { "x-ratelimit-remaining-req-minute": "0" },
    route: { provider: "mistral", route_id: "mistral/devstral" },
  });
  assert.equal(resMinute.failure_class, "own_rpm");
  assert.equal(resMinute.rule_id, "remaining-zero-header");
});

test("classifyProviderFailure matches remaining-tokens-zero-header", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: { error: { message: "Slow down" } },
    headers: { "x-ratelimit-remaining-tokens": "0" },
    route: { provider: "cerebras", route_id: "cerebras/llama" },
  });
  assert.equal(res.failure_class, "own_tpm");
  assert.equal(res.rule_id, "remaining-tokens-zero-header");
});

test("classifyProviderFailure matches overloaded", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        message: "The server is currently overloaded. Please try again later.",
      },
    },
    headers: null,
    route: { provider: "nvidia", route_id: "nvidia/deepseek" },
  });
  assert.equal(res.failure_class, "upstream_capacity");
  assert.equal(res.rule_id, "overloaded");
});

test("classifyProviderFailure matches concurrency", () => {
  const res = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        message: "Too many concurrent requests on model",
      },
    },
    headers: null,
    route: { provider: "kilo", route_id: "kilo/free" },
  });
  assert.equal(res.failure_class, "upstream_capacity");
  assert.equal(res.rule_id, "concurrency");
});

test("classifyProviderFailure handles unmatched-429 and route-default-upstream", () => {
  // Unmatched without default
  const resUnmatched = classifyProviderFailure({
    status: 429,
    body: { error: { message: "Unknown limit error xyz" } },
    headers: { "retry-after": "60" },
    route: { provider: "unknown_provider", route_id: "unknown/model" },
  });
  assert.equal(resUnmatched.failure_class, "unknown_429");
  assert.equal(resUnmatched.rule_id, "unmatched-429");
  assert.equal(resUnmatched.retry_after_seconds, 60);

  // Unmatched with route-level upstream default
  const resRouteDefault = classifyProviderFailure({
    status: 429,
    body: { error: { message: "Unknown limit error xyz" } },
    headers: null,
    route: {
      provider: "unknown_provider",
      route_id: "unknown/model",
      upstream_429_default: "upstream_capacity",
    },
  });
  assert.equal(resRouteDefault.failure_class, "upstream_capacity");
  assert.equal(resRouteDefault.rule_id, "route-default-upstream");
});

test("classifyProviderFailure handles generic 4xx as request_defect", () => {
  const res = classifyProviderFailure({
    status: 404,
    body: { error: { message: "Model not found" } },
    headers: null,
    route: { provider: "gemini", route_id: "gemini/unknown" },
  });
  assert.equal(res.failure_class, "request_defect");
  assert.equal(res.rule_id, "http-4xx");
  assert.equal(res.scope, "route");
});

test("FAILURE_SIGNATURES has at least 13 rules", () => {
  assert.ok(FAILURE_SIGNATURES.length >= 13);
  const ids = new Set(FAILURE_SIGNATURES.map((r) => r.rule_id));
  assert.equal(ids.size, FAILURE_SIGNATURES.length, "all rule_id values must be unique");
});

test("a zero PROVISIONED limit is billing, not pacing", () => {
  // Mistral reports an account with no allowance as a plain 429 whose message gives nothing away;
  // the only tell is `x-ratelimit-limit-req-minute: 0` -- the limit, not the remaining. Read as
  // own_rpm it bought a 60s buffer and retried forever against a route that can never serve a
  // request. Confirmed live 2026-09-09 while /v1/models still returned 200.
  const headers = new Map([
    ["x-ratelimit-limit-req-minute", "0"],
    ["x-ratelimit-remaining-req-minute", "0"],
  ]);
  const result = classifyProviderFailure({
    status: 429,
    body: { message: "Rate limit exceeded", type: "rate_limited", code: "1300" },
    headers,
    route: { provider: "mistral" },
  });
  assert.equal(result.failure_class, "payment_required");
  assert.equal(result.rule_id, "zero-provisioned-limit");
});

test("an ordinary exhausted limit is still pacing, not billing", () => {
  const headers = new Map([
    ["x-ratelimit-limit-requests", "1000"],
    ["x-ratelimit-remaining-requests", "0"],
  ]);
  const result = classifyProviderFailure({
    status: 429,
    body: { error: { message: "slow down" } },
    headers,
    route: { provider: "groq" },
  });
  assert.equal(result.failure_class, "own_rpm");
  assert.equal(result.rule_id, "remaining-zero-header");
});

test("a bare RateLimit-Limit: 0 (no x- prefix) is still read as zero-provisioned, not gateway_limit", () => {
  // Some providers emit the newer, unprefixed standard header instead of the older de facto
  // X-RateLimit-* convention. Before hasRateLimitHeader recognized it, this shape fell through to
  // the generic AI-Gateway heuristic (isAig429: no known rate-limit header + no body `error` key)
  // and was misclassified gateway_limit -- which fans out a cooldown to every sibling route on the
  // provider, not just the one whose own zero allowance was actually the problem.
  const headers = new Map([["ratelimit-limit", "0"]]);
  const result = classifyProviderFailure({
    status: 429,
    body: { message: "Rate limit exceeded" },
    headers,
    route: { provider: "mistral" },
  });
  assert.equal(result.failure_class, "payment_required");
  assert.equal(result.rule_id, "zero-provisioned-limit");
});

test("a bare 'quota exceeded' 429 from a non-gemini provider stays own_rpm, not billing", () => {
  // insufficient-budget used to match the bare phrase "quota exceeded" for any provider. Gemini's
  // own RPD/TPM messages say exactly that ("Resource exhausted: quota exceeded for
  // GenerateRequestsPerDayPerProjectPerModel"), and are correctly caught by earlier, gemini-scoped
  // rules -- but a provider-agnostic match would have routed an ordinary rate 429 from any OTHER
  // provider onto the day/week/month payment_required cooldown ladder instead of the correct
  // short-lived own_rpm backoff.
  const result = classifyProviderFailure({
    status: 429,
    body: { error: { message: "quota exceeded, please slow down" } },
    headers: null,
    route: { provider: "some-other-provider" },
  });
  assert.notEqual(result.failure_class, "payment_required");
});

test("a size-status daily token quota is own_rpd, not own_tpm", () => {
  // "tokens per day"/"(tpd)" used to be lumped into the same branch as the per-minute cases,
  // applying own_tpm's ~60s bucket-wait pacing to a quota that only resets on the provider's
  // calendar day -- matching the existing groq-tpd rule's own_rpd classification for the same
  // axis elsewhere in this file.
  const result = classifyProviderFailure({
    status: 413,
    body: { error: { message: "Request too large: exceeds tokens per day (TPD) limit" } },
    headers: null,
    route: { provider: "groq" },
  });
  assert.equal(result.failure_class, "own_rpd");
  assert.equal(result.rule_id, "size-status-daily-rate-limit");
});

test("a 2xx carrying no completion is upstream capacity, never a success", () => {
  // Airforce returns HTTP 200 with no `choices` and an error whose own code says 503. response.ok
  // is true for it, so the executor stored that error object in B2 as the job's RESULT and settled
  // the job completed -- a permanently wrong answer no retry would revisit -- while the success
  // path cleared every backoff signal, keeping a route that served nothing ranked as healthy.
  assert.equal(
    upstreamEmptyCompletion(200, {
      error: { message: "No content was returned", type: "upstream_unavailable", code: "503" },
    }),
    true
  );
  assert.equal(upstreamEmptyCompletion(200, { choices: [] }), true);
  assert.equal(upstreamEmptyCompletion(200, {}), true);
  assert.equal(upstreamEmptyCompletion(200, null), true);
  assert.equal(upstreamEmptyCompletion(429, { choices: [{ message: {} }] }), false);
  assert.equal(
    upstreamEmptyCompletion(200, { choices: [{ message: { content: "hi" } }] }),
    false
  );
});

test("gemini's array-wrapped error body is unwrapped before classification", () => {
  // Confirmed live 2026-09-13 during an endurance ceiling probe: Gemini's OpenAI-compatible
  // endpoint wraps its error body in a JSON ARRAY, not a bare object. This genuine, real quota
  // exhaustion on gemma-4-26b/31b (a per-model token quota -- nothing to do with Cloudflare's AI
  // Gateway, since the probe calls Gemini directly) was falling through every dict-shaped check
  // and landing on the isAig429 fallback as gateway_limit -- which in production incorrectly
  // cools down every OTHER Gemini route sharing the account, not just the exhausted model.
  const body = [
    {
      error: {
        code: 429,
        message:
          "You exceeded your current quota, please check your plan and billing details. " +
          "Quota exceeded for metric: generativelanguage.googleapis.com/" +
          "generate_content_free_tier_input_token_count, limit: 16000, model: gemma-4-26b\n" +
          "Please retry in 31.44s.",
        status: "RESOURCE_EXHAUSTED",
      },
    },
  ];
  const result = classifyProviderFailure({
    status: 429,
    body,
    headers: new Map(),
    route: { provider: "gemini" },
  });
  assert.equal(result.failure_class, "own_tpm");
  assert.equal(result.rule_id, "gemini-tpm");
  assert.equal(result.retry_after_seconds, 32);
});

test("gemini's input_token_count quota metric is own_tpm, not the generic resource-exhausted fallback", () => {
  const result = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        message: "Quota exceeded for metric: .../generate_content_free_tier_input_token_count, limit: 16000",
        status: "RESOURCE_EXHAUSTED",
      },
    },
    headers: new Map(),
    route: { provider: "gemini" },
  });
  assert.equal(result.failure_class, "own_tpm");
  assert.equal(result.rule_id, "gemini-tpm");
});

test("orcarouter free-tier prompt cap is request_defect when Retry-After is absent", () => {
  const result = classifyProviderFailure({
    status: 429,
    body: {
      error: {
        code: "free_rate_limited",
        message: "Rate limit exceeded",
      },
    },
    headers: new Map(),
    route: { provider: "orcarouter" },
  });
  assert.equal(result.failure_class, "request_defect");
  assert.equal(result.rule_id, "orcarouter-prompt-cap");
});

test("orcarouter free-tier rate limits with Retry-After map to own_rpm and own_rpd", () => {
  const body = {
    error: {
      code: "free_rate_limited",
      message: "Rate limit exceeded",
    },
  };

  // Minute window (<= 120s)
  const headersRpm = new Map([["retry-after", "45"]]);
  const resRpm = classifyProviderFailure({
    status: 429,
    body,
    headers: headersRpm,
    route: { provider: "orcarouter" },
  });
  assert.equal(resRpm.failure_class, "own_rpm");
  assert.equal(resRpm.rule_id, "orcarouter-minute-window");

  // Daily window (> 120s)
  const headersRpd = new Map([["retry-after", "3600"]]);
  const resRpd = classifyProviderFailure({
    status: 429,
    body,
    headers: headersRpd,
    route: { provider: "orcarouter" },
  });
  assert.equal(resRpd.failure_class, "own_rpd");
  assert.equal(resRpd.rule_id, "orcarouter-daily-window");
});
