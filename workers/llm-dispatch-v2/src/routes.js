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

/**
 * Check a request against one route's context ceilings.
 *
 * `input_context_limit` is the route's effective total context window. The provider still gets a
 * separate `output_context_limit` guard because a model can expose a smaller output maximum than
 * its total window (for example, Airforce's Mistral Medium route).
 */
export function routeFitsContext(route, inputTokens, outputTokens) {
  const contextLimit = route.input_context_limit || 32768;
  const outputLimit = route.output_context_limit || 1024;
  return (
    inputTokens <= contextLimit &&
    outputTokens <= outputLimit &&
    inputTokens + outputTokens <= contextLimit
  );
}

/**
 * Every route a job's stored policy (policy_json: { allowed_models, allow_paid }) can legally
 * reach, filtered for combined input/output context-window compatibility -- capacity/pacing
 * eligibility (RPM/TPM/buffer/blocked_until) is a separate, live-ledger-dependent check, see
 * pacing.js.
 *
 * Returns routes in model_routes_map's own catalog order (config/provider_limits.yml's authored
 * order, e.g. "high-capacity workhorses" listed first) with no additional ranking -- Unit 4's
 * admission passes take the first route with capacity, so catalog order alone determines
 * preference among otherwise-equal candidates. This intentionally does not replicate v1's
 * rankRoutes(): v1 ranks against its own R2-based ledger, which v2 does not share.
 */
export function routesEligibleFor(job, dispatchLimits) {
  let policy;
  try {
    policy = typeof job.policy_json === "string" ? JSON.parse(job.policy_json) : job.policy_json;
  } catch {
    policy = {};
  }
  const allowedModels = Array.isArray(policy?.allowed_models) ? policy.allowed_models : [];
  const allowPaid = Boolean(policy?.allow_paid);
  const inputTokens = job.input_token_estimate || 0;
  const outputTokens = job.max_output_token_estimate || 0;

  const seenRouteIds = new Set();
  const eligible = [];
  for (const rawModel of allowedModels) {
    const canonical = canonicalModelName(rawModel, dispatchLimits);
    const routeIds = dispatchLimits.model_routes_map?.[canonical] || [];
    for (const routeId of routeIds) {
      if (seenRouteIds.has(routeId)) continue;
      const route = routeFromCatalog(routeId, dispatchLimits, canonical);
      if (!route) continue;
      if (!allowPaid && !route.free) continue;
      if (!routeFitsContext(route, inputTokens, outputTokens)) continue;
      seenRouteIds.add(routeId);
      eligible.push(route);
    }
  }
  return eligible;
}
