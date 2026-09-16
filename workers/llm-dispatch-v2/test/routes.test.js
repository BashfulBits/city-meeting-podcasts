import test from "node:test";
import assert from "node:assert/strict";
import DISPATCH_LIMITS from "../src/dispatch_limits.json" with { type: "json" };
import {
  backupModelsActive,
  modelForRouteId,
  modelsForJob,
  routesEligibleFor,
} from "../src/routes.js";

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

// --- backupModelsActive / modelsForJob -------------------------------------------------------

const AGENDA_POLICY = {
  allowed_models: ["nvidia/nemotron-3-ultra-550b-a55b:free"],
  backup_models: ["gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite"],
  backup_after_attempts: 12,
};

test("backupModelsActive is false below the attempts threshold with no schema retries", () => {
  assert.equal(backupModelsActive({ attempts: 11, schema_retry_count: 0 }, AGENDA_POLICY), false);
});

test("backupModelsActive is true once attempts reaches the threshold", () => {
  assert.equal(backupModelsActive({ attempts: 12, schema_retry_count: 0 }, AGENDA_POLICY), true);
});

test("backupModelsActive is true on any schema-retry clone, regardless of attempts", () => {
  assert.equal(backupModelsActive({ attempts: 0, schema_retry_count: 1 }, AGENDA_POLICY), true);
});

test("backupModelsActive is false when the policy declares no backup_models", () => {
  assert.equal(
    backupModelsActive(
      { attempts: 999, schema_retry_count: 5 },
      { allowed_models: ["m1"] },
    ),
    false,
  );
});

test("backupModelsActive is false when backup_after_attempts is missing or non-positive", () => {
  assert.equal(
    backupModelsActive(
      { attempts: 999, schema_retry_count: 0 },
      { allowed_models: ["m1"], backup_models: ["m2"] },
    ),
    false,
  );
  assert.equal(
    backupModelsActive(
      { attempts: 999, schema_retry_count: 0 },
      { allowed_models: ["m1"], backup_models: ["m2"], backup_after_attempts: 0 },
    ),
    false,
  );
});

test("modelsForJob returns only allowed_models when backups are inactive", () => {
  assert.deepEqual(
    modelsForJob({ attempts: 0, schema_retry_count: 0 }, AGENDA_POLICY),
    ["nvidia/nemotron-3-ultra-550b-a55b:free"],
  );
});

test("modelsForJob appends backup_models once active", () => {
  assert.deepEqual(
    modelsForJob({ attempts: 12, schema_retry_count: 0 }, AGENDA_POLICY),
    [
      "nvidia/nemotron-3-ultra-550b-a55b:free",
      "gemini/gemini-3.1-flash-lite",
      "gemini/gemini-3.5-flash-lite",
    ],
  );
});

test("routesEligibleFor includes backup-model routes only once backupModelsActive says so", () => {
  const job = (attempts) => ({
    policy_json: JSON.stringify(AGENDA_POLICY),
    input_token_estimate: 1000,
    max_output_token_estimate: 1000,
    attempts,
    schema_retry_count: 0,
  });

  const belowThreshold = routesEligibleFor(job(0), DISPATCH_LIMITS);
  assert.ok(belowThreshold.every((route) => route.model === "nvidia/nemotron-3-ultra-550b-a55b:free"));

  const atThreshold = routesEligibleFor(job(12), DISPATCH_LIMITS);
  assert.ok(atThreshold.some((route) => route.model === "gemini/gemini-3.1-flash-lite"));
  assert.ok(atThreshold.some((route) => route.model === "nvidia/nemotron-3-ultra-550b-a55b:free"));
});

// --- modelForRouteId --------------------------------------------------------------------------

test("modelForRouteId resolves a real compiled route_id to its canonical model", () => {
  // routes_by_id entries carry no `model` field of their own (confirmed against the real
  // compiled catalog) -- model_routes_map is the only place the association exists, and only in
  // the model -> routes direction.
  assert.equal(
    modelForRouteId("gemini_3_1_flash_lite_primary", DISPATCH_LIMITS),
    "gemini/gemini-3.1-flash-lite"
  );
});

test("modelForRouteId returns null for an unknown route_id or a missing catalog", () => {
  assert.equal(modelForRouteId("no-such-route", DISPATCH_LIMITS), null);
  assert.equal(modelForRouteId(null, DISPATCH_LIMITS), null);
  assert.equal(modelForRouteId("gemini_3_1_flash_lite_primary", null), null);
});

test("modelForRouteId caches per dispatchLimits object identity, not globally", () => {
  const catalogA = { model_routes_map: { "model-a": ["route-1"] } };
  const catalogB = { model_routes_map: { "model-b": ["route-1"] } };
  assert.equal(modelForRouteId("route-1", catalogA), "model-a");
  assert.equal(modelForRouteId("route-1", catalogB), "model-b");
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
  assert.ok(routes.some((r) => r.provider === "orcarouter"));
});
