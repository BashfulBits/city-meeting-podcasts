import test from "node:test";
import assert from "node:assert/strict";
import {
  CALIBRATION_MIN_SAMPLES,
  CALIBRATION_WINDOW,
  MIN_OUTPUT_RESERVE,
  calibrationFor,
  outputReserveFor,
  parseCalibrationSummary,
  recordCalibrationSample,
  routeInputTokenRatio,
  scaledInputTokens,
} from "../src/calibration.js";

test("routeInputTokenRatio uses the catalog prior and falls back to 1 when unset or invalid", () => {
  assert.equal(routeInputTokenRatio({ input_token_ratio: 1.2 }), 1.2);
  assert.equal(routeInputTokenRatio({}), 1);
  assert.equal(routeInputTokenRatio({ input_token_ratio: null }), 1);
  assert.equal(routeInputTokenRatio({ input_token_ratio: 0 }), 1);
  assert.equal(routeInputTokenRatio({ input_token_ratio: 50 }), 1);
  assert.equal(scaledInputTokens(1000, 1.16), 1160);
  assert.equal(scaledInputTokens(null, 2), 0);
});

test("a legacy high-water summary (bare array of totals) starts an empty window", () => {
  assert.deepEqual(parseCalibrationSummary("[15000]"), { r: [], o: [] });
  assert.deepEqual(parseCalibrationSummary(null), { r: [], o: [] });
  assert.deepEqual(parseCalibrationSummary("not json"), { r: [], o: [] });
});

test("the sample window is bounded and ignores template-dominated tiny prompts", () => {
  let summary = { r: [], o: [] };
  for (let i = 0; i < CALIBRATION_WINDOW + 10; i += 1) {
    summary = recordCalibrationSample(summary, {
      inputEstimate: 1000,
      observedInput: 1100,
      observedOutput: 300,
    });
  }
  assert.equal(summary.r.length, CALIBRATION_WINDOW);
  assert.equal(summary.o.length, CALIBRATION_WINDOW);

  const tiny = recordCalibrationSample({ r: [], o: [] }, {
    inputEstimate: 12,
    observedInput: 90,
    observedOutput: 4,
  });
  assert.deepEqual(tiny.r, []);
  assert.deepEqual(tiny.o, [4]);
});

test("calibrationFor keeps the prior until enough samples, then follows the recent p95", () => {
  const route = { input_token_ratio: 1.2 };
  const few = { r: [1.5, 1.5], o: [100, 100] };
  assert.deepEqual(calibrationFor(route, few), {
    inputRatio: 1.2,
    outputForecast: null,
    inputRatioP95: null,
    inputRatioSamples: 2,
    outputSamples: 2,
  });

  // 16 recent samples: ratios mostly 1.0 with one 1.4 outlier; outputs ~300 with one 600.
  const r = Array(CALIBRATION_MIN_SAMPLES - 1).fill(1.0).concat([1.4]);
  const o = Array(CALIBRATION_MIN_SAMPLES - 1).fill(300).concat([600]);
  const result = calibrationFor(route, { r, o });
  // p95 of 16 samples is the 16th value, so the outlier sets both (conservative on purpose).
  assert.equal(result.inputRatio, 1.4);
  assert.equal(result.outputForecast, 750); // ceil(600 * 1.25)

  // The effective ratio never drops below the route's own catalog prior -- it can only add to
  // it, so a well-behaved recent window (here, below the prior) still reserves at the prior.
  const low = calibrationFor(route, { r: Array(20).fill(0.98), o: Array(20).fill(10) });
  assert.equal(low.inputRatio, 1.2);
  assert.equal(low.outputForecast, MIN_OUTPUT_RESERVE);
});

test("outputReserveFor never exceeds the request's own max_tokens", () => {
  assert.equal(outputReserveFor({ max_output_token_estimate: 8200 }, null), 8200);
  assert.equal(outputReserveFor({ max_output_token_estimate: 8200 }, 850), 850);
  assert.equal(outputReserveFor({ max_output_token_estimate: 500 }, 850), 500);
});
