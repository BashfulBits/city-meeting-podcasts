import test from "node:test";
import assert from "node:assert/strict";
import { classifyProviderFailure, FAILURE_SIGNATURES } from "../src/classify.js";

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
