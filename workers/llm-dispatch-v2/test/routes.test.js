import test from "node:test";
import assert from "node:assert/strict";
import DISPATCH_LIMITS from "../src/dispatch_limits.json" with { type: "json" };
import { backupModelsActive, modelsForJob, routesEligibleFor } from "../src/routes.js";

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
