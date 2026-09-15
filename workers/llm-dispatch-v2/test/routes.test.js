import test from "node:test";
import assert from "node:assert/strict";
import DISPATCH_LIMITS from "../src/dispatch_limits.json" with { type: "json" };
import { routesEligibleFor } from "../src/routes.js";

function eligibleMistralRoutes(inputTokens, outputTokens) {
  return routesEligibleFor(
    {
      policy_json: JSON.stringify({
        allowed_models: ["mistral/mistral-medium-latest"],
        allow_paid: false,
      }),
      input_token_estimate: inputTokens,
      max_output_token_estimate: outputTokens,
    },
    DISPATCH_LIMITS,
  ).filter((route) => route.provider === "mistral");
}

test("all native Mistral Medium latest routes admit a request within context limit", () => {
  // primary + secondary + tertiary (the maintainer's third, non-payment-limited key, 2026-09-12).
  const routes = eligibleMistralRoutes(120000, 8000);
  assert.deepEqual(
    routes.map((route) => route.route_id),
    [
      "mistral_medium_latest_primary",
      "mistral_medium_latest_secondary",
      "mistral_medium_latest_tertiary",
    ],
  );
});

test("native Mistral Medium latest routes reject input plus output above context limit", () => {
  assert.deepEqual(eligibleMistralRoutes(131073, 1000), []);
});

test("Gemma routes enforce hard_input_ceiling and kick large jobs to NVIDIA NIM", () => {
  const jobUnderCeiling = {
    policy_json: JSON.stringify({
      allowed_models: ["google/gemma-4-31b-it"],
      allow_paid: false,
    }),
    input_token_estimate: 8000,
    max_output_token_estimate: 1000,
  };
  const underRoutes = routesEligibleFor(jobUnderCeiling, DISPATCH_LIMITS);
  const underIds = underRoutes.map((r) => r.route_id);
  assert.ok(underIds.includes("gemma_4_31b_primary"));
  assert.ok(underIds.includes("nvidia_gemma_4_31b_it_free"));

  // Above 10000 tokens (Google Gemma ceiling), Google Gemini routes are disqualified
  // and only NVIDIA/OpenRouter routes remain eligible.
  const jobOverCeiling = {
    policy_json: JSON.stringify({
      allowed_models: ["google/gemma-4-31b-it"],
      allow_paid: false,
    }),
    input_token_estimate: 12000,
    max_output_token_estimate: 1000,
  };
  const overRoutes = routesEligibleFor(jobOverCeiling, DISPATCH_LIMITS);
  const overIds = overRoutes.map((r) => r.route_id);
  assert.ok(!overIds.includes("gemma_4_31b_primary"));
  assert.ok(!overIds.includes("gemma_4_31b_secondary"));
  assert.ok(overIds.includes("nvidia_gemma_4_31b_it_free"));
});

test("OrcaRouter free route is eligible for deepseek-v4-flash without paid permission", () => {
  const job = {
    policy_json: JSON.stringify({
      allowed_models: ["deepseek/deepseek-v4-flash"],
      allow_paid: false,
    }),
    input_token_estimate: 10000,
    max_output_token_estimate: 1000,
  };
  const routes = routesEligibleFor(job, DISPATCH_LIMITS);
  const ids = routes.map((r) => r.route_id);
  assert.ok(ids.includes("orcarouter_deepseek_v4_flash_free"));
  // Paused OpenCode routes (rpd: 0) remain in catalog but OrcaRouter is present
  assert.ok(routes.some((r) => r.provider === "orcarouter"));
});

