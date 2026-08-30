import test from "node:test";
import assert from "node:assert/strict";
import DISPATCH_LIMITS from "../src/dispatch_limits.json" with { type: "json" };
import { routesEligibleFor } from "../src/routes.js";

function eligibleMistralRoutes(inputTokens, outputTokens) {
  return routesEligibleFor(
    {
      policy_json: JSON.stringify({
        allowed_models: ["mistral/mistral-medium-3-5"],
        allow_paid: false,
      }),
      input_token_estimate: inputTokens,
      max_output_token_estimate: outputTokens,
    },
    DISPATCH_LIMITS,
  ).filter((route) => route.provider === "mistral");
}

test("both native Mistral Medium 3.5 routes admit a request exactly at 256k total context", () => {
  const routes = eligibleMistralRoutes(252144, 10000);
  assert.deepEqual(
    routes.map((route) => route.route_id),
    ["mistral_medium_3_5_primary", "mistral_medium_3_5_secondary"],
  );
});

test("native Mistral Medium 3.5 routes reject input plus output above 256k", () => {
  assert.deepEqual(eligibleMistralRoutes(252145, 10000), []);
});
