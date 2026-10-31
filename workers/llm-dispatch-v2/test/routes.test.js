import test from "node:test";
import assert from "node:assert/strict";
import DISPATCH_LIMITS from "../src/dispatch_limits.json" with { type: "json" };
import {
  backupModelsActive,
  modelForRouteId,
  modelsForJob,
  routesEligibleFor,
} from "../src/routes.js";

// Native Mistral (Codestral-only, account-tier restricted and monthly metered) was removed from the
// catalog on 2026-09-30. A `mistral/*` model name may still be served by another provider's route.
test("no native Mistral provider, account or route remains in the catalog", () => {
  assert.equal(DISPATCH_LIMITS.providers.mistral, undefined);
  const native = (DISPATCH_LIMITS.routes || Object.values(DISPATCH_LIMITS.routes_by_id || {})).filter(
    (route) => route.provider === "mistral",
  );
  assert.deepEqual(native, []);
});

test("paused rpd:0 routes are excluded before free/paid admission", () => {
  const catalog = {
    model_aliases: {},
    model_routes_map: { "example/model": ["paused-free", "paid-fallback"] },
    routes_by_id: {
      "paused-free": {
        route_id: "paused-free",
        free: true,
        rpd: 0,
        input_context_limit: 10000,
        output_context_limit: 1000,
      },
      "paid-fallback": {
        route_id: "paid-fallback",
        free: false,
        input_context_limit: 10000,
        output_context_limit: 1000,
      },
    },
  };
  const baseJob = {
    input_token_estimate: 100,
    max_output_token_estimate: 50,
  };
  const freeJob = {
    ...baseJob,
    policy_json: JSON.stringify({ allowed_models: ["example/model"], allow_paid: false }),
  };
  const paidJob = {
    ...baseJob,
    policy_json: JSON.stringify({ allowed_models: ["example/model"], allow_paid: true }),
  };

  assert.deepEqual(routesEligibleFor(freeJob, catalog), []);
  assert.deepEqual(
    routesEligibleFor(paidJob, catalog).map((route) => route.route_id),
    ["paid-fallback"],
  );
});

test("allowed_route_ids narrows a multi-provider model pool to the pinned physical route", () => {
  const job = {
    policy_json: JSON.stringify({
      allowed_models: ["deepseek/deepseek-v4.1-flash"],
      allowed_route_ids: ["beatapi_deepseek_v4_1_flash_free"],
      allow_paid: false,
    }),
    input_token_estimate: 100,
    max_output_token_estimate: 100,
  };

  assert.deepEqual(
    routesEligibleFor(job, DISPATCH_LIMITS).map((route) => route.route_id),
    ["beatapi_deepseek_v4_1_flash_free"],
  );
});

test("allowed_route_ids cannot widen a model pool or fall back when its pin is invalid", () => {
  const job = {
    policy_json: JSON.stringify({
      allowed_models: ["deepseek/deepseek-v4.1-flash"],
      allowed_route_ids: ["nvidia_kimi_k3_free"],
      allow_paid: false,
    }),
    input_token_estimate: 100,
    max_output_token_estimate: 100,
  };

  assert.deepEqual(routesEligibleFor(job, DISPATCH_LIMITS), []);
});

test("an unavailable pinned route does not fall back to another route in its model pool", () => {
  const catalog = structuredClone(DISPATCH_LIMITS);
  catalog.routes_by_id.beatapi_deepseek_v4_1_flash_free.rpd = 0;
  const job = {
    policy_json: JSON.stringify({
      allowed_models: ["deepseek/deepseek-v4.1-flash"],
      allowed_route_ids: ["beatapi_deepseek_v4_1_flash_free"],
      allow_paid: false,
    }),
    input_token_estimate: 100,
    max_output_token_estimate: 100,
  };

  assert.deepEqual(routesEligibleFor(job, catalog), []);
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

test("Gemma routes enforce hard_input_ceiling while reopened legs remain eligible", () => {
  const jobUnderCeiling = {
    policy_json: JSON.stringify({
      allowed_models: ["google/gemma-4-31b-it"],
      allow_paid: false,
    }),
    input_token_estimate: 8000,
    max_output_token_estimate: 1000,
  };
  const underIds = routesEligibleFor(jobUnderCeiling, DISPATCH_LIMITS).map((r) => r.route_id);
  assert.ok(underIds.includes("gemma_4_31b_primary"));
  assert.ok(underIds.includes("gemma_4_31b_secondary"));
  // Maintainer reopened these legs at 20 RPD for ordinary dispatch and health monitoring.
  assert.ok(underIds.includes("nvidia_gemma_4_31b_it_free"));
  assert.ok(underIds.includes("openrouter_google_gemma_4_31b_it_free"));

  // The larger job exceeds the Google routes' conservative admission ceiling; the independent
  // SambaNova and reopened legs remain eligible under their own unchanged token bounds.
  const jobOverCeiling = {
    policy_json: JSON.stringify({
      allowed_models: ["google/gemma-4-31b-it"],
      allow_paid: false,
    }),
    input_token_estimate: 12000,
    max_output_token_estimate: 1000,
  };
  const overIds = routesEligibleFor(jobOverCeiling, DISPATCH_LIMITS).map((r) => r.route_id);
  assert.ok(!overIds.includes("gemma_4_31b_primary"));
  assert.ok(!overIds.includes("gemma_4_31b_secondary"));
  assert.deepEqual(new Set(overIds), new Set([
    "sambanova_gemma_4_31b_it_primary", "nvidia_gemma_4_31b_it_free",
    "openrouter_google_gemma_4_31b_it_free",
  ]));
});

test("the reopened Gemma legs keep their allowances and admit a small free-policy job", () => {
  // OpenRouter's two legs stay at 20 RPD. NVIDIA's 31B leg was raised to 800 RPD on 2026-10-16
  // and holds one of the provider's four shared slots (two on 2026-10-17, back to one on
  // 2026-10-19 when hung calls rose), so slow Gemma calls cannot starve Ultra.
  const rpd = {
    openrouter_google_gemma_4_31b_it_free: 20,
    openrouter_google_gemma_4_26b_a4b_it_free: 20,
    nvidia_gemma_4_31b_it_free: 800,
  };
  assert.equal(DISPATCH_LIMITS.routes_by_id.nvidia_gemma_4_31b_it_free.concurrency, 1);
  assert.equal(DISPATCH_LIMITS.providers.nvidia.concurrency, 4);
  for (const id of Object.keys(rpd)) {
    assert.equal(DISPATCH_LIMITS.routes_by_id[id].rpd, rpd[id]);
    const job = { policy_json: JSON.stringify({
      allowed_models: [modelForRouteId(id, DISPATCH_LIMITS)], allow_paid: false,
    }), input_token_estimate: 8000, max_output_token_estimate: 1000 };
    assert.ok(routesEligibleFor(job, DISPATCH_LIMITS).some(route => route.route_id === id));
  }
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

test("a route serving several pools reports its primary model, not the first pool scanned", () => {
  const catalog = {
    model_routes_map: {
      // Deliberately list the secondary pools first.
      "deepseek/deepseek-v4-flash": ["orca", "nvidia-v41"],
      "deepseek/deepseek-v4-pro": ["nvidia-v41"],
      "deepseek/deepseek-v4.1-flash": ["nvidia-v41"],
    },
    routes_by_id: {
      "nvidia-v41": { provider: "nvidia", model: "deepseek/deepseek-v4.1-flash" },
      orca: { provider: "orcarouter", model: "deepseek/deepseek-v4-flash" },
    },
  };
  assert.equal(modelForRouteId("nvidia-v41", catalog), "deepseek/deepseek-v4.1-flash");
  assert.equal(modelForRouteId("orca", catalog), "deepseek/deepseek-v4-flash");
});

test("the compiled catalog has one pool per DeepSeek version (2026-09-24)", () => {
  // Providers serving the same version share a pool (BeatAPI joined 2026-10-07); V4 Pro is
  // BeatAPI's own pool, not the retired alias for NVIDIA's v4.1.
  const map = DISPATCH_LIMITS.model_routes_map;
  assert.deepEqual(map["deepseek/deepseek-v4.1-flash"], [
    "nvidia_deepseek_v4_1_flash_free",
    "beatapi_deepseek_v4_1_flash_free",
    "beatapi_secondary_deepseek_v4_1_flash_free",
  ]);
  // The secondary BeatAPI account lost its free V4 Flash and V4 Pro routes 2026-10-31.
  assert.deepEqual(map["deepseek/deepseek-v4-flash"], [
    "orcarouter_deepseek_v4_flash_free",
    "beatapi_deepseek_v4_flash_free",
  ]);
  assert.deepEqual(map["deepseek/deepseek-v4-pro"], ["beatapi_deepseek_v4_pro_free"]);
  assert.equal(
    modelForRouteId("nvidia_deepseek_v4_1_flash_free", DISPATCH_LIMITS),
    "deepseek/deepseek-v4.1-flash"
  );
  // An exact-model contestant must never be answered by the older v4-flash pool.
  const job = {
    policy_json: JSON.stringify({ allowed_models: ["deepseek/deepseek-v4.1-flash"] }),
    input_token_estimate: 1000,
    max_output_token_estimate: 500,
  };
  assert.deepEqual(
    routesEligibleFor(job, DISPATCH_LIMITS).map((route) => route.route_id),
    [
      "nvidia_deepseek_v4_1_flash_free",
      "beatapi_deepseek_v4_1_flash_free",
      "beatapi_secondary_deepseek_v4_1_flash_free",
    ]
  );
});

test("a pooled model repeated as a backup adds no duplicate routes once backups activate", () => {
  // chapter-agenda lists hy3 / Gemini 3.1 Flash Lite in both models and backup_models so the
  // Worker's extended retry budget applies; routing must be identical before and after.
  const policy = {
    allowed_models: ["tencent/hy3", "gemini/gemini-3.1-flash-lite"],
    backup_models: ["tencent/hy3", "gemini/gemini-3.1-flash-lite"],
    backup_after_attempts: 12,
    allow_paid: false,
  };
  const job = (attempts) => ({
    policy_json: JSON.stringify(policy),
    attempts,
    input_token_estimate: 1000,
    max_output_token_estimate: 1000,
  });
  const ids = (attempts) => routesEligibleFor(job(attempts), DISPATCH_LIMITS).map((r) => r.route_id);
  assert.deepEqual(ids(12), ids(0));
  assert.equal(new Set(ids(12)).size, ids(12).length);
});

function structuralFixture() {
  const catalog = {
    model_aliases: { alias: "primary" },
    model_routes_map: { primary: ["p"], backup: ["b"] },
    routes_by_id: {
      p: { route_id: "p", free: true, input_context_limit: 1000, output_context_limit: 500 },
      b: { route_id: "b", free: true, input_context_limit: 10000, output_context_limit: 500 },
    },
  };
  const policy = {
    allowed_models: ["alias"], backup_models: ["backup"], backup_after_attempts: 12,
    allow_paid: false,
  };
  const job = {
    policy_json: policy, attempts: 0, schema_retry_count: 0,
    input_token_estimate: 1500, max_output_token_estimate: 100,
  };
  return { catalog, policy, job };
}

for (const cause of ["input", "combined", "output", "missing", "paid"]) {
  test(`structurally unusable primary unlocks its declared backup: ${cause}`, () => {
    const { catalog, policy, job } = structuralFixture();
    if (cause === "combined") job.input_token_estimate = 950;
    if (cause === "output") {
      job.input_token_estimate = 100;
      catalog.routes_by_id.p.output_context_limit = 50;
    }
    if (cause === "missing") delete catalog.model_routes_map.primary;
    if (cause === "paid") {
      job.input_token_estimate = 100;
      catalog.routes_by_id.p.free = false;
    }
    assert.deepEqual(modelsForJob(job, policy, catalog), ["alias", "backup"]);
    assert.deepEqual(routesEligibleFor(job, catalog).map(route => route.route_id), ["b"]);
    assert.equal(job.attempts, 0);
    assert.deepEqual(policy.allowed_models, ["alias"]);
  });
}

test("structural backup activation retains paid permission and every primary alternative", () => {
  const { catalog, policy, job } = structuralFixture();
  catalog.routes_by_id.b.free = false;
  assert.deepEqual(routesEligibleFor(job, catalog), []);
  policy.allow_paid = true;
  assert.deepEqual(routesEligibleFor(job, catalog).map(route => route.route_id), ["b"]);
  catalog.model_routes_map.primary.push("alternate");
  catalog.routes_by_id.alternate = { ...catalog.routes_by_id.b, route_id: "alternate", free: true };
  assert.deepEqual(modelsForJob(job, policy, catalog), ["alias"]);
});

test("paused or quota-exhausted primary never triggers structural backup activation", () => {
  const { catalog, policy, job } = structuralFixture();
  job.input_token_estimate = 100;
  Object.assign(catalog.routes_by_id.p, { rpd: 0, tpm: 1, blocked_until: Date.now() + 100000 });
  assert.deepEqual(modelsForJob(job, policy, catalog), ["alias"]);
  const pausedAdmission = { ...catalog, model_routes_map: { backup: ["b"] } };
  assert.deepEqual(routesEligibleFor(job, pausedAdmission, catalog), []);
});

test("the eligibility filter and the pacing check share one size gate", async () => {
  const { routeFitsContext } = await import("../src/routes.js");
  const { earliestSafeStart } = await import("../src/pacing.js");
  const now = Date.now();
  const base = { provider: "gemini", rpm: 30, rpd: 14400, tpm: 16000, hard_input_ceiling: 14400,
    input_token_ratio: 1.4, input_context_limit: 262144, output_context_limit: 32768 };
  // Catalog values only (no live data): 11,000 x 1.4 = 15,400 is over the 14,400 ceiling.
  assert.equal(routeFitsContext(base, 11000, 100), false);
  // Calibrated: the admission ratio 1.06 puts it at 11,660, inside the ceiling.
  const calibrated = { inputRatio: 1.27, admitRatio: 1.06, route: base, now };
  assert.equal(routeFitsContext(base, 11000, 100, calibrated), true);
  // A size the route has actually served lifts the configured ceiling, in both places.
  const served = { ...base, hard_input_ceiling: 10000, input_accepted_max: 12000,
    input_accepted_at: now };
  const live = { inputRatio: 1.27, admitRatio: 1.06, route: served, now };
  assert.equal(routeFitsContext(served, 11000, 100, live), true);
  // ...and a size it refused is not skipped by the coarse filter (tolerant), only by pacing.
  const refused = { ...base, input_rejected_min: 9000, input_rejected_at: now };
  const seen = { inputRatio: 1.27, admitRatio: 1.06, route: refused, now };
  assert.equal(routeFitsContext(refused, 11000, 100, seen), true);
  assert.equal(
    earliestSafeStart(refused, { input_token_estimate: 11000, max_output_token_estimate: 100 },
      now, now, { inputRatio: 1.27, admitRatio: 1.06 }),
    null
  );
  // The two agree across sizes: whatever the filter refuses, pacing refuses too.
  for (const size of [4000, 9000, 11000, 12500, 14000, 16000]) {
    for (const route of [base, served, refused]) {
      const fits = routeFitsContext(route, size, 100,
        { inputRatio: 1.27, admitRatio: 1.06, route, now });
      const paced = earliestSafeStart(route,
        { input_token_estimate: size, max_output_token_estimate: 100 }, now, now,
        { inputRatio: 1.27, admitRatio: 1.06, ceilingTolerance: true });
      if (!fits) assert.equal(paced, null, `size ${size}`);
      if (paced !== null) assert.equal(fits, true, `size ${size}: pacing admits but the filter refuses`);
    }
  }
});

test("quota exclusions preserve model, physical pin, paid and context restrictions", () => {
  const model = "google/gemma-4-31b-it";
  const catalog = structuredClone(DISPATCH_LIMITS);
  const base = {
    input_token_estimate: 500, max_output_token_estimate: 200,
    quota_excluded_routes: JSON.stringify(["gemma_4_31b_primary", "gemma_4_31b_secondary"]),
  };
  const routes = (policy) => routesEligibleFor({ ...base, policy_json: {
    allowed_models: [model], allow_paid: false, ...policy,
  } }, catalog);
  const remaining = routes({});
  assert.ok(remaining.length > 0);
  assert.ok(remaining.every(route => route.provider !== "gemini" && route.model === model));
  assert.deepEqual(routes({ allowed_route_ids: ["gemma_4_31b_primary"] }), []);
  const overflowId = "nvidia_gemma_4_31b_it_free";
  assert.deepEqual(routes({ allowed_route_ids: [overflowId] }).map(r => r.route_id), [overflowId]);
  catalog.routes_by_id[overflowId].free = false;
  assert.deepEqual(routes({ allowed_route_ids: [overflowId] }), []);
  assert.equal(routes({ allowed_route_ids: [overflowId], allow_paid: true }).length, 1);
  catalog.routes_by_id[overflowId].input_context_limit = 100;
  assert.deepEqual(routes({ allowed_route_ids: [overflowId], allow_paid: true }), []);
  const fresh = routesEligibleFor({ ...base, quota_excluded_routes: null, policy_json: {
    allowed_models: [model], allow_paid: false,
  } }, catalog);
  assert.ok(fresh.some(route => route.route_id === "gemma_4_31b_primary"),
    "a refusal for another job does not exclude this route");
});

test("timeout counts exclude only routes with two deadlines without widening policy", () => {
  const catalog = {
    model_aliases: {}, model_routes_map: { model: ["slow", "sibling", "paid", "too-small"] },
    routes_by_id: Object.fromEntries(["slow", "sibling", "paid", "too-small"].map(id => [id, {
      free: id !== "paid", input_context_limit: id === "too-small" ? 100 : 10000,
      output_context_limit: 1000,
    }])),
  };
  const job = { policy_json: { allowed_models: ["model"], allow_paid: false },
    input_token_estimate: 500, max_output_token_estimate: 200 };
  const ids = value => routesEligibleFor({ ...job, response_timeout_routes: value }, catalog)
    .map(route => route.route_id);
  assert.deepEqual(ids(JSON.stringify({ slow: 1 })), ["slow", "sibling"]);
  assert.deepEqual(ids(JSON.stringify({ slow: 2 })), ["sibling"]);
  assert.deepEqual(ids(JSON.stringify({ slow: 99, sibling: 2 })), []);
  assert.deepEqual(ids("broken-json"), ["slow", "sibling"]);
  assert.deepEqual(ids(JSON.stringify({ slow: -1, sibling: 1.5 })), ["slow", "sibling"]);
  assert.deepEqual(routesEligibleFor({ ...job, response_timeout_routes: '{"slow":2}',
    policy_json: { ...job.policy_json, allowed_route_ids: ["slow"] } }, catalog), []);
});
