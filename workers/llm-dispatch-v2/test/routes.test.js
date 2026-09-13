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
