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
 *   input ratio    = max(route prior, p95(observed_input / input_token_estimate) * headroom),
 *                    else the route's catalog `input_token_ratio` prior until the sample gate.
 *   output reserve = min(max_tokens, ceil(p95(observed_output) * OUTPUT_RESERVE_HEADROOM)), else
 *                    the job's full max_tokens until CALIBRATION_MIN_SAMPLES exist.
 *
 * Under-forecasting one job is recoverable: completeBatch settles every successful reservation to
 * the provider's actual usage (refund or debit), so the token bucket tracks real consumption, and
 * an own_tpm 429 still backs the route off.
 */

export const CALIBRATION_WINDOW = 32;
export const CALIBRATION_MIN_SAMPLES = 16;
// The route-wide TPM budget already includes a 10% safety margin. Add a separate headroom to
// the learned input ratio so a p95-calibrated prompt does not spend that same margin twice.
// Gemma's paired corpus had p95 1.16 but a 1.48 maximum; 1.2x headroom plus the existing 0.9
// TPM multiplier keeps that measured tail within Google's 16k input-token/minute quota.
export const INPUT_RATIO_HEADROOM = 1.2;
export const OUTPUT_RESERVE_HEADROOM = 1.25;
// Guards against a single tiny completion (e.g. "ok") ever producing a near-zero reserve.
export const MIN_OUTPUT_RESERVE = 64;
const MIN_RATIO = 0.25;
const MAX_RATIO = 8;

/** The route's static prior: provider tokens per chars/4 estimate unit (1 when unmeasured). */
export function routeInputTokenRatio(route) {
  const ratio = Number(route?.input_token_ratio);
  return Number.isFinite(ratio) && ratio >= MIN_RATIO && ratio <= MAX_RATIO ? ratio : 1;
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
export function calibrationFor(route, summary) {
  const parsed = summary || { r: [], o: [] };
  const inputRatioP95 =
    parsed.r.length >= CALIBRATION_MIN_SAMPLES ? percentile(parsed.r, 0.95) : null;
  const inputHeadroom =
    route?.provider === "gemini" && String(route?.model || "").startsWith("google/gemma-4-")
      ? INPUT_RATIO_HEADROOM
      : 1;
  const inputRatio = Math.max(
    routeInputTokenRatio(route),
    inputRatioP95 == null ? 0 : inputRatioP95 * inputHeadroom
  );
  const outputForecast =
    parsed.o.length >= CALIBRATION_MIN_SAMPLES
      ? Math.max(MIN_OUTPUT_RESERVE, Math.ceil(percentile(parsed.o, 0.95) * OUTPUT_RESERVE_HEADROOM))
      : null;
  return {
    inputRatio,
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
