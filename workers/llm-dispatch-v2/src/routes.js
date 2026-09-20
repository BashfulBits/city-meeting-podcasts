/**
 * Route catalog selection for LLM Dispatch v2.
 *
 * Pure extraction of workers/llm-dispatch-proxy/src/index.js's model-alias-resolution and
 * route-eligibility pattern (per review/44 Phase 1's "extract only pure route-catalog selection
 * and response-normalization helpers from v1; do not fork provider credential logic without
 * tests") -- adapted here for v2's own claimDispatchWindow admission pass rather than v1's
 * single-job selectRouteForModel. dispatch_limits.json is compiled by
 * scripts/compile_llm_limits.py from the same config/provider_limits.yml v1 uses, and written to
 * this Worker's own src/ directory (not imported across Worker directories) so v2 has no
 * build-time dependency on v1's directory continuing to exist past its Phase 3 retirement.
 */

/** Follow model_aliases until a non-aliased (canonical) model name is reached. */
export function canonicalModelName(model, dispatchLimits) {
  let current = model;
  const seen = new Set();
  const aliases = dispatchLimits?.model_aliases || {};
  while (typeof aliases[current] === "string" && !seen.has(current)) {
    seen.add(current);
    current = aliases[current];
  }
  return current;
}

/** Look up one route by id, attaching the canonical model name it was selected under. */
export function routeFromCatalog(routeId, dispatchLimits, model) {
  const stored = dispatchLimits?.routes_by_id?.[routeId];
  return stored ? { ...stored, route_id: routeId, model } : null;
}

// Reverse of model_routes_map (model -> [route_ids]), built lazily and cached per dispatchLimits
// object identity -- there can be more than one live catalog object in a test process (a real
// DISPATCH_LIMITS import plus a per-test DISPATCH_LIMITS_OVERRIDE), so a single module-level cache
// would leak a stale mapping across them. A WeakMap key means a discarded test catalog's entry is
// GC'd for free, and a keyless plain object is never accidentally cached against `undefined`.
const _routeModelCache = new WeakMap();

/**
 * The canonical model a given `route_id` serves. `routes_by_id` entries carry no `model` field of
 * their own (confirmed against the real compiled catalog) -- `model_routes_map` is the only place
 * that association exists, and only in the model -> routes direction, so this builds the reverse
 * map once per catalog rather than scanning it on every call.
 */
export function modelForRouteId(routeId, dispatchLimits) {
  if (!routeId || !dispatchLimits) return null;
  let cache = _routeModelCache.get(dispatchLimits);
  if (!cache) {
    cache = new Map();
    for (const [model, routeIds] of Object.entries(dispatchLimits.model_routes_map || {})) {
      if (!Array.isArray(routeIds)) continue;
      for (const id of routeIds) {
        if (!cache.has(id)) cache.set(id, model);
      }
    }
    _routeModelCache.set(dispatchLimits, cache);
  }
  return cache.get(routeId) ?? null;
}

/**
 * Check a request against one route's context ceilings.
 *
 * `input_context_limit` is the route's effective total context window. The provider still gets a
 * separate `output_context_limit` guard because a model can expose a smaller output maximum than
 * its total window (for example, Airforce's Mistral Medium route).
 *
 * `hard_input_ceiling` is an optional, measured per-request input ceiling (e.g. Google Gemma's
 * 16k TPM token bucket). Requests exceeding it must never be admitted to this route and must fall
 * through to alternative routes (e.g. NVIDIA NIM).
 */
export function routeFitsContext(route, inputTokens, outputTokens) {
  const contextLimit = route.input_context_limit || 32768;
  const outputLimit = route.output_context_limit || 1024;
  const hardCeiling = Number(route?.hard_input_ceiling);
  if (Number.isFinite(hardCeiling) && hardCeiling > 0 && inputTokens > hardCeiling) {
    return false;
  }
  return (
    inputTokens <= contextLimit &&
    outputTokens <= outputLimit &&
    inputTokens + outputTokens <= contextLimit
  );
}

/** Parse a job's `policy_json` (string or already-parsed object), never throwing. */
export function jobPolicy(job) {
  try {
    return typeof job.policy_json === "string" ? JSON.parse(job.policy_json) : job.policy_json || {};
  } catch {
    return {};
  }
}

/**
 * Whether `policy.backup_models` should be folded into a job's eligible model list right now.
 *
 * Two independent triggers, mirroring `LaneConfig.backup_models`/`backup_after_attempts`
 * (citypods/compute/llm_lanes.py): (a) `job.attempts` (a durable, cross-lease dispatch counter --
 * see `attemptStarted` in coordinator.js) has reached the configured threshold without a
 * successful response, or (b) `job.schema_retry_count` is at least 1 -- this job is itself a
 * schema-correction clone (see `schemaRetry`), which is already evidence of one confirmed
 * structural JSON-output failure on the model that produced it, a different failure class from
 * ordinary capacity/latency retries.
 */
export function backupModelsActive(job, policy) {
  const backupModels = Array.isArray(policy?.backup_models) ? policy.backup_models : [];
  const threshold = policy?.backup_after_attempts;
  if (backupModels.length === 0 || !Number.isInteger(threshold) || threshold <= 0) return false;
  const attempts = Number.isInteger(job.attempts) ? job.attempts : 0;
  const schemaRetryCount = Number.isInteger(job.schema_retry_count) ? job.schema_retry_count : 0;
  return attempts >= threshold || schemaRetryCount >= 1;
}

/** Every model a job may currently be dispatched to: `allowed_models`, plus `backup_models` once
 * `backupModelsActive` says so. */
export function modelsForJob(job, policy) {
  const allowedModels = Array.isArray(policy?.allowed_models) ? policy.allowed_models : [];
  if (!backupModelsActive(job, policy)) return allowedModels;
  const backupModels = Array.isArray(policy?.backup_models) ? policy.backup_models : [];
  return [...allowedModels, ...backupModels];
}

/**
 * Every route a job's stored policy (policy_json: { allowed_models, allow_paid, backup_models,
 * backup_after_attempts }) can legally reach, filtered for combined input/output context-window
 * compatibility -- capacity/pacing eligibility (RPM/TPM/buffer/blocked_until) is a separate,
 * live-ledger-dependent check, see pacing.js.
 *
 * Returns routes in model_routes_map's own catalog order (config/provider_limits.yml's authored
 * order, e.g. "high-capacity workhorses" listed first) with no additional ranking -- Unit 4's
 * admission passes take the first route with capacity, so catalog order alone determines
 * preference among otherwise-equal candidates. This intentionally does not replicate v1's
 * rankRoutes(): v1 ranks against its own R2-based ledger, which v2 does not share.
 */
export function routesEligibleFor(job, dispatchLimits) {
  const policy = jobPolicy(job);
  const modelsToConsider = modelsForJob(job, policy);
  const allowPaid = Boolean(policy?.allow_paid);
  const inputTokens = job.input_token_estimate || 0;
  const outputTokens = job.max_output_token_estimate || 0;

  const seenRouteIds = new Set();
  const eligible = [];
  for (const rawModel of modelsToConsider) {
    const canonical = canonicalModelName(rawModel, dispatchLimits);
    const routeIds = dispatchLimits.model_routes_map?.[canonical] || [];
    for (const routeId of routeIds) {
      if (seenRouteIds.has(routeId)) continue;
      const route = routeFromCatalog(routeId, dispatchLimits, canonical);
      if (!route) continue;
      // An explicit rpd: 0 is the catalog's paused-route convention. Filter it before the
      // free/paid admission decision; otherwise a paused free route can outrank a paid sibling,
      // and the final pacing check treats zero as immediately ready.
      if (route.rpd != null && Number(route.rpd) === 0) continue;
      if (!allowPaid && !route.free) continue;
      if (!routeFitsContext(route, inputTokens, outputTokens)) continue;
      seenRouteIds.add(routeId);
      eligible.push(route);
    }
  }
  return eligible;
}
