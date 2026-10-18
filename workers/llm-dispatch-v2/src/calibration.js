/**
 * Per-route token calibration for LLM Dispatch v2.
 *
 * Every job carries one tokenizer-agnostic `input_token_estimate` (the producer's chars/4
 * heuristic) and a `max_output_token_estimate` (the request's own `max_tokens`). Neither is what a
 * provider actually counts:
 *
 *   - INPUT: each model family's tokenizer diverges from chars/4 by its own ratio. Measured
 *     2026-09-23 over 1,727 paired B2 payload/result samples: Gemma p95 1.16, Nemotron 3 Ultra
 *     p95 1.73, Gemini 3.1 Flash Lite p95 1.59, Gemini 3.5 Flash Lite p95 2.15. A route's measured
 *     `hard_input_ceiling` is in the PROVIDER's units, so comparing the raw estimate against it
 *     over-admits on some routes and wastes nothing on none.
 *   - OUTPUT: `max_tokens` is a ceiling, not a forecast. Gemma prelabeler batches reserved up to
 *     ~8k output tokens and returned 150-680 (p50-p95). Reserving the ceiling made every such job
 *     drain a 14,400 TPM route for a full minute and a half.
 *
 * The previous calibration (`estimates.margin_tokens`) kept a never-decreasing high-water mark of
 * observed input+output per route/model/prompt family, and `reservationFor` took the max of it and
 * the job's own estimate. One 15k-token Gemma batch therefore made EVERY later Gemma job on that
 * route reserve >=15k -- more than the route's whole per-minute budget.
 *
 * This module replaces that with two bounded, decaying signals kept per estimates key (a ring of
 * the most recent CALIBRATION_WINDOW completions):
 *
 *   input ratio    = the route's catalog `input_token_ratio` prior until CALIBRATION_MIN_SAMPLES
 *                    exist, then a blend of the prior and the learned tail that hands over
 *                    completely at CALIBRATION_FULL_SAMPLES (review/55 piece 1): the prior is a
 *                    seed worth ~CALIBRATION_FULL_SAMPLES pseudo-samples, not a floor.
 *   output reserve = min(max_tokens, ceil(tail(observed_output) * OUTPUT_RESERVE_HEADROOM)), else
 *                    the job's full max_tokens until CALIBRATION_MIN_SAMPLES exist.
 *
 * Under-forecasting one job is recoverable: completeBatch settles every successful reservation to
 * the provider's actual usage (refund or debit), so the token bucket tracks real consumption, and
 * an own_tpm 429 still backs the route off.
 */

// A ring of 128 completions per (tokenizer family, prompt family): a p95 over 16-32 samples is the
// second-largest sample, not a percentile. Below CALIBRATION_MIN_SAMPLES only the prior is used;
// between it and CALIBRATION_FULL_SAMPLES the learned tail is blended in with a linearly shrinking
// weight on the prior; from CALIBRATION_FULL_SAMPLES the learned percentiles stand alone.
export const CALIBRATION_WINDOW = 128;
export const CALIBRATION_MIN_SAMPLES = 16;
export const CALIBRATION_FULL_SAMPLES = 64;
// The route-wide TPM budget already includes a 10% safety margin. Add a separate headroom to
// the learned input ratio so a p95-calibrated prompt does not spend that same margin twice.
// Gemma's paired corpus had p95 1.16 but a 1.48 maximum; 1.2x headroom plus the existing 0.9
// TPM multiplier keeps that measured tail within Google's 16k input-token/minute quota.
export const INPUT_RATIO_HEADROOM = 1.2;
export const OUTPUT_RESERVE_HEADROOM = 1.25;
// Two quantiles for two purposes (review/55 piece 2). Reservation and PACKING stay on the safe tail
// (p95, above); ADMISSION to a route's size gates uses a lower quantile, because a wrong guess costs
// one cheap refused call (the job then overflows to a sibling and the route learns the refusal)
// while a wrong rejection strands the job on an idle route.
export const ADMIT_QUANTILE = 0.75;
export const ADMIT_HEADROOM = 1.05;
export const PACK_HEADROOM = 1.05;
// Guards against a single tiny completion (e.g. "ok") ever producing a near-zero reserve.
export const MIN_OUTPUT_RESERVE = 64;
const MIN_RATIO = 0.25;
const MAX_RATIO = 8;

/** The route's static prior: provider tokens per chars/4 estimate unit (1 when unmeasured). */
export function routeInputTokenRatio(route) {
  const ratio = Number(route?.input_token_ratio);
  return Number.isFinite(ratio) && ratio >= MIN_RATIO && ratio <= MAX_RATIO ? ratio : 1;
}

/**
 * The largest input, in the route's tokenizer units, the Worker will try on this route:
 * `hard_input_ceiling`, or with `tolerant` also its optional `hard_input_ceiling_tolerance`
 * fraction. The scaled input is itself an estimate, so a claim that finds nothing else to
 * dispatch may try a near miss on a ceiling authored with slack rather than strand it.
 * Infinity when the route has no ceiling.
 */
export function hardInputCeilingLimit(route, { tolerant = false } = {}) {
  const tpm = Number(route?.tpm);
  const quotaCeiling = route?.provider === "gemini" && tpm > 0
    ? tpm : Number.POSITIVE_INFINITY;
  const ceiling = Number(route?.hard_input_ceiling);
  if (!Number.isFinite(ceiling) || ceiling <= 0) return quotaCeiling;
  if (!tolerant) return Math.min(ceiling, quotaCeiling);
  const tolerance = Number(route?.hard_input_ceiling_tolerance);
  const slack = Number.isFinite(tolerance) && tolerance > 0 && tolerance <= 0.5 ? tolerance : 0;
  return Math.min(Math.floor(ceiling * (1 + slack)), quotaCeiling);
}

/** How long a learned accepted/rejected input size is trusted before the route is re-tested. */
export const LEARNED_CEILING_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/**
 * What this route has actually done with input sizes, in the provider's own tokens: the largest
 * input it accepted (`input_accepted_max`, from usage.prompt_tokens) and the smallest it refused
 * for size (`input_rejected_min`). An entry older than the TTL is ignored so a provider that
 * changed its limit is re-tested. If the two contradict (accepted >= rejected) the more recent
 * observation wins and the other is dropped.
 */
export function learnedCeilings(route, now = Date.now(), ttlMs = LEARNED_CEILING_TTL_MS) {
  let acceptedMax = Number(route?.input_accepted_max) || 0;
  const acceptedAt = Number(route?.input_accepted_at) || 0;
  let rejectedMin = Number(route?.input_rejected_min) || 0;
  const rejectedAt = Number(route?.input_rejected_at) || 0;
  if (acceptedMax > 0 && now - acceptedAt > ttlMs) acceptedMax = 0;
  if (rejectedMin > 0 && now - rejectedAt > ttlMs) rejectedMin = 0;
  if (acceptedMax > 0 && rejectedMin > 0 && acceptedMax >= rejectedMin) {
    if (acceptedAt >= rejectedAt) rejectedMin = 0;
    else acceptedMax = 0;
  }
  return { acceptedMax, rejectedMin };
}

/**
 * The largest chars/4 input estimate a packet may carry and still be admissible on this route,
 * from the route's tightest known size limit (configured ceiling, a learned refusal, the Gemini
 * per-minute input quota, the context window) divided by the PACKING ratio. Null when the route
 * states no such limit.
 */
export function packingInputLimit(route, packRatio, now = Date.now()) {
  const limits = [];
  const { acceptedMax, rejectedMin } = learnedCeilings(route, now);
  // The same size admission applies (pacing.js): a route that has served more than its configured
  // ceiling is not limited to it.
  const ceiling = Math.max(hardInputCeilingLimit(route), acceptedMax);
  if (Number.isFinite(ceiling)) limits.push(ceiling);
  if (rejectedMin > 0) limits.push(rejectedMin - 1);
  const context = Number(route?.input_context_limit);
  if (Number.isFinite(context) && context > 0) limits.push(context);
  if (limits.length === 0) return null;
  const ratio = Number.isFinite(packRatio) && packRatio > 0 ? packRatio : routeInputTokenRatio(route);
  return Math.floor(Math.min(...limits) / ratio);
}

/** A raw chars/4 estimate expressed in the route's own tokenizer units. */
export function scaledInputTokens(rawInput, ratio) {
  const raw = Math.max(0, Number(rawInput) || 0);
  const r = Number.isFinite(ratio) && ratio > 0 ? ratio : 1;
  return Math.ceil(raw * r);
}

function percentile(values, p) {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(p * sorted.length) - 1));
  return sorted[index];
}

/**
 * Parse an estimates row's `recent_observed_summary`. Rows written by the old high-water
 * calibration hold a bare JSON array of totals, which carries no usable input/output split, so
 * they start from an empty window rather than being misread.
 */
export function parseCalibrationSummary(raw) {
  let parsed = null;
  try {
    parsed = typeof raw === "string" && raw.trim() !== "" ? JSON.parse(raw) : null;
  } catch {
    parsed = null;
  }
  const clean = (list, lo, hi) =>
    Array.isArray(list)
      ? list.map(Number).filter((v) => Number.isFinite(v) && v >= lo && v <= hi)
      : [];
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { r: [], o: [] };
  return {
    r: clean(parsed.r, MIN_RATIO, MAX_RATIO).slice(-CALIBRATION_WINDOW),
    o: clean(parsed.o, 0, Number.MAX_SAFE_INTEGER).slice(-CALIBRATION_WINDOW),
  };
}

/** Append one completion's observations to a summary, keeping the most recent window. */
export function recordCalibrationSample(summary, { inputEstimate, observedInput, observedOutput }) {
  const next = { r: [...(summary?.r || [])], o: [...(summary?.o || [])] };
  const estimate = Number(inputEstimate);
  const input = Number(observedInput);
  // Tiny prompts are dominated by fixed chat-template overhead, not the tokenizer ratio.
  if (Number.isFinite(estimate) && estimate >= 200 && Number.isFinite(input) && input > 0) {
    const ratio = input / estimate;
    if (ratio >= MIN_RATIO && ratio <= MAX_RATIO) next.r.push(Math.round(ratio * 1000) / 1000);
  }
  const output = Number(observedOutput);
  if (Number.isFinite(output) && output >= 0) next.o.push(Math.round(output));
  next.r = next.r.slice(-CALIBRATION_WINDOW);
  next.o = next.o.slice(-CALIBRATION_WINDOW);
  return next;
}

/** The effective input ratio and output-reserve forecast for one route/model/prompt family. */
/** Weight left on the route prior after n samples: 1 below the minimum, 0 from the full gate. */
function priorWeight(n) {
  if (n < CALIBRATION_MIN_SAMPLES) return 1;
  return Math.max(0, 1 - n / CALIBRATION_FULL_SAMPLES);
}

export function calibrationFor(route, summary) {
  const parsed = summary || { r: [], o: [] };
  const n = parsed.r.length;
  const prior = routeInputTokenRatio(route);
  const weight = priorWeight(n);
  const learned = n >= CALIBRATION_MIN_SAMPLES;
  const inputRatioP95 = learned ? percentile(parsed.r, 0.95) : null;
  const inputHeadroom =
    route?.provider === "gemini" && String(route?.model || "").startsWith("google/gemma-4-")
      ? INPUT_RATIO_HEADROOM
      : 1;
  const blend = (learnedRatio) =>
    Math.min(MAX_RATIO, Math.max(MIN_RATIO, weight * prior + (1 - weight) * learnedRatio));
  // Reservation: the safe tail -- p95 x headroom, or the p99 (about the window's largest samples)
  // if that is higher, so a rare high-ratio prompt keeps protecting the minute quota while it is
  // still in the window.
  const inputRatio = learned
    ? blend(Math.max(inputRatioP95 * inputHeadroom, percentile(parsed.r, 0.99)))
    : prior;
  // Admission: p75 x a small headroom, never above the reservation ratio.
  const admitRatio = learned
    ? Math.min(inputRatio, blend(percentile(parsed.r, ADMIT_QUANTILE) * ADMIT_HEADROOM))
    : inputRatio;
  // Packing: the p95 tail with its own headroom.
  const packRatio = learned ? blend(inputRatioP95 * PACK_HEADROOM) : prior;
  // Output reserve: with 16-63 samples the largest sample stands in for the (unreliable) p95.
  const outputTail =
    parsed.o.length >= CALIBRATION_FULL_SAMPLES
      ? percentile(parsed.o, 0.95)
      : parsed.o.length >= CALIBRATION_MIN_SAMPLES
        ? Math.max(...parsed.o)
        : null;
  const outputForecast =
    outputTail == null
      ? null
      : Math.max(MIN_OUTPUT_RESERVE, Math.ceil(outputTail * OUTPUT_RESERVE_HEADROOM));
  return {
    inputRatio,
    admitRatio,
    packRatio,
    outputForecast,
    inputRatioP95,
    inputRatioSamples: parsed.r.length,
    outputSamples: parsed.o.length,
  };
}

/** Output tokens to reserve for a job: its forecast, never above the request's own max_tokens. */
export function outputReserveFor(job, outputForecast) {
  const ceiling = Math.max(0, Number(job?.max_output_token_estimate) || 0);
  if (outputForecast == null) return ceiling;
  return Math.min(ceiling, outputForecast);
}
