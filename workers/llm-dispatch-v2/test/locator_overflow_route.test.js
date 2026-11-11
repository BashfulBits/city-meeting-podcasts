// review/58 WP8: chapter-locator packets above 76,000 estimated tokens and up to the 500,000 cap
// (citypods/chapter_locator.py LOCATOR_OVERFLOW_MAX_INPUT_TOKENS) go to MiniMax M3.1 Flash. Every
// Worker size gate must admit a cap-sized packet on the real compiled route, with the 12,800-token
// output the producer reserves for it (citypods/chapter_locator.py locator_max_output_tokens).
import test from "node:test";
import assert from "node:assert/strict";
import DISPATCH_LIMITS from "../src/dispatch_limits.json" with { type: "json" };
import { inputSizeRefusal, routeInputTokenRatio, scaledInputTokens } from "../src/calibration.js";
import { outputTokensForRoute } from "../src/gateway.js";
import { routeFitsContext } from "../src/routes.js";

const ROUTE_ID = "nvidia_minimaxai_minimax_m3_1_flash";
const MODEL = "minimaxai/minimax-m3.1-flash";
const CAP = 500_000;
const OUTPUT = 12_800;

const route = { ...DISPATCH_LIMITS.routes_by_id[ROUTE_ID], route_id: ROUTE_ID, model: MODEL };

test("the MiniMax route carries the WP8a probe limits", () => {
  assert.ok(DISPATCH_LIMITS.model_routes_map[MODEL].includes(ROUTE_ID));
  assert.equal(route.hard_input_ceiling, 941_663);
  assert.equal(route.input_context_limit, 941_663);
  assert.equal(route.output_context_limit, OUTPUT);
  assert.equal(routeInputTokenRatio(route), 1.59);
});

test("a cap-sized locator packet passes every size gate on the MiniMax route", () => {
  assert.equal(inputSizeRefusal(route, CAP), null);
  assert.equal(inputSizeRefusal(route, CAP, { tolerant: true }), null);
  assert.equal(routeFitsContext(route, CAP, OUTPUT), true);
  // Margin: 500,000 x 1.59 = 795,000 provider tokens + 12,800 output, inside 941,663.
  assert.ok(scaledInputTokens(CAP, 1.59) + OUTPUT <= route.input_context_limit);
});

test("the route sends the full 12,800-token output to a cap-sized packet", () => {
  const payload = { max_tokens: OUTPUT, max_tokens_mode: "route_max" };
  assert.equal(outputTokensForRoute(payload, route, CAP), OUTPUT);
});

test("the route still refuses a packet the size the provider rejected", () => {
  // 709,545 estimated tokens was refused by the provider (HTTP 400) in the WP8a probe.
  assert.equal(inputSizeRefusal(route, 709_545), "over_input_ceiling");
  assert.equal(routeFitsContext(route, 709_545, OUTPUT), false);
});
