// review/58 WP8: chapter-locator packets above 76,000 estimated tokens and up to the overflow cap
// (citypods/chapter_locator.py LOCATOR_OVERFLOW_MAX_INPUT_TOKENS, derived from this route:
// floor(1,034,675 / 1.59) = 650,738 since the 2026-11-19 probe) go to MiniMax M3.1 Flash. Every
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
const CAP = 650_738;
const OUTPUT = 12_800;

const route = { ...DISPATCH_LIMITS.routes_by_id[ROUTE_ID], route_id: ROUTE_ID, model: MODEL };

test("the MiniMax route carries the 2026-11-19 probe limits", () => {
  assert.ok(DISPATCH_LIMITS.model_routes_map[MODEL].includes(ROUTE_ID));
  // 1,034,675 prompt tokens was the largest admitted; NVIDIA reports a 1,048,576 window.
  assert.equal(route.hard_input_ceiling, 1_034_675);
  assert.equal(route.input_context_limit, 1_048_576);
  assert.equal(route.output_context_limit, OUTPUT);
  assert.equal(routeInputTokenRatio(route), 1.59);
});

test("a cap-sized locator packet passes every size gate on the MiniMax route", () => {
  assert.equal(inputSizeRefusal(route, CAP), null);
  assert.equal(inputSizeRefusal(route, CAP, { tolerant: true }), null);
  assert.equal(routeFitsContext(route, CAP, OUTPUT), true);
  // Margin: 650,738 x 1.59 = 1,034,674 provider tokens + 12,800 output, inside 1,048,576.
  assert.ok(scaledInputTokens(CAP, 1.59) + OUTPUT <= route.input_context_limit);
});

test("the route sends the full 12,800-token output to a cap-sized packet", () => {
  const payload = { max_tokens: OUTPUT, max_tokens_mode: "route_max" };
  assert.equal(outputTokensForRoute(payload, route, CAP), OUTPUT);
});

test("the route still refuses a packet the size the provider rejected", () => {
  // 1,053,919 provider tokens (662,842 estimated at 1.59) was refused with HTTP 400
  // context_length in the 2026-11-19 probe.
  assert.equal(inputSizeRefusal(route, 662_842), "over_input_ceiling");
  assert.equal(routeFitsContext(route, 662_842, OUTPUT), false);
});
