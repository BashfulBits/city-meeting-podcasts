// review/58 §12.5 (R2-F): the 780 s ceiling for the slow reasoning routes, every route's pinned
// response timeout, and the bounded B2 result write that keeps the lease arithmetic honest.
import assert from "node:assert/strict";
import test from "node:test";

import LIMITS from "../src/dispatch_limits.json" with { type: "json" };
import TUNING from "../src/dispatch_tuning.json" with { type: "json" };
import RESERVATIONS from "../src/ingress_reservations.json" with { type: "json" };
import { B2Client } from "../src/b2.js";
import { upstreamRequestForRoute } from "../src/gateway.js";
import {
  effectiveResponseMs, leaseCoversCall, resultWriteTimeoutMs, validateConfig,
} from "../src/index.js";
import { withTuning } from "../src/tuning.js";

const tuned = (overrides = {}) => withTuning({ BEARER_TOKEN: "t", ...overrides });

const SLOW_ROUTES = [
  "orcarouter_zai_glm_5_3_flash_free",
  "nvidia_nemotron_3_ultra_550b_a55b_free",
  "kilo_nvidia_nemotron_3_ultra_550b_a55b_free",
  "openrouter_nvidia_nemotron_3_ultra_550b_a55b_free",
];

test("the compiled tuning passes validateConfig with the 780 / 45 / 870 timing set", () => {
  const v = TUNING.values;
  assert.equal(v.MAX_RESPONSE_SECONDS, 780);
  assert.equal(v.FINALIZATION_RESERVE_SECONDS, 45);
  assert.equal(v.LEASE_DURATION_SECONDS, 870);
  assert.equal(v.DISPATCH_WINDOW_SECONDS, 30);
  assert.equal(v.RESULT_WRITE_TIMEOUT_SECONDS, 30);
  assert.ok(v.DISPATCH_WINDOW_SECONDS + v.MAX_RESPONSE_SECONDS + v.FINALIZATION_RESERVE_SECONDS
    <= v.LEASE_DURATION_SECONDS);
  assert.ok(v.LEASE_DURATION_SECONDS < v.CRON_EXECUTION_LIMIT_SECONDS);
  assert.doesNotThrow(() => validateConfig(tuned()));
});

test("validateConfig rejects a result write that does not fit the finalization reserve", () => {
  assert.throws(() => validateConfig(tuned({ RESULT_WRITE_TIMEOUT_SECONDS: "45" })),
    /RESULT_WRITE_TIMEOUT_SECONDS \(45\) must be > 0 and < FINALIZATION_RESERVE_SECONDS \(45\)/);
  assert.throws(() => validateConfig(tuned({ RESULT_WRITE_TIMEOUT_SECONDS: "0" })),
    /RESULT_WRITE_TIMEOUT_SECONDS/);
  // 780 s with the old 90 s reserve no longer fits an 870 s lease: 30 + 780 + 90 = 900.
  assert.throws(() => validateConfig(tuned({ FINALIZATION_RESERVE_SECONDS: "90" })),
    /LEASE_DURATION_SECONDS \(870\)/);
  // Reverting to 720 / 90 / 840 stays valid.
  assert.doesNotThrow(() => validateConfig(tuned({
    MAX_RESPONSE_SECONDS: "720", FINALIZATION_RESERVE_SECONDS: "90", LEASE_DURATION_SECONDS: "840",
  })));
});

test("every compiled route pins its timeout; only the slow reasoning routes use 780 s", () => {
  const globalMs = TUNING.values.MAX_RESPONSE_SECONDS * 1000;
  for (const [routeId, route] of Object.entries(LIMITS.routes_by_id)) {
    const seconds = route.response_timeout_seconds;
    assert.ok(Number.isInteger(seconds) && seconds > 0, `${routeId} pins response_timeout_seconds`);
    if (SLOW_ROUTES.includes(routeId)) {
      assert.equal(seconds, 780, routeId);
      assert.equal(effectiveResponseMs(route, globalMs), 780_000, routeId);
    } else {
      assert.ok(seconds <= 720, `${routeId} stays at or below 720 s (${seconds})`);
      assert.ok(effectiveResponseMs(route, globalMs) <= 720_000, routeId);
    }
  }
  for (const routeId of SLOW_ROUTES) assert.ok(LIMITS.routes_by_id[routeId], routeId);
  // Gemma 4 31B on NVIDIA is left at the ordinary value (review/58 §12.5: no lever applies).
  assert.equal(LIMITS.routes_by_id.nvidia_gemma_4_31b_it_free.response_timeout_seconds, 720);
});

test("the result write timeout comes from the tuning, with a 30 s fallback", () => {
  assert.equal(resultWriteTimeoutMs({ RESULT_WRITE_TIMEOUT_SECONDS: "30" }), 30_000);
  assert.equal(resultWriteTimeoutMs({ RESULT_WRITE_TIMEOUT_SECONDS: "0.5" }), 500);
  assert.equal(resultWriteTimeoutMs({}), 30_000);
  assert.equal(resultWriteTimeoutMs(withTuning({})), TUNING.values.RESULT_WRITE_TIMEOUT_SECONDS * 1000);
});

test("a late start must leave the result write inside the lease", () => {
  const v = TUNING.values;
  const claim = 0;
  const job = { lease_expires_at: claim + v.LEASE_DURATION_SECONDS * 1000 };
  const write = resultWriteTimeoutMs(withTuning({}));
  // The worst measured start (10.7 s claim + 30 s window) still fits a 780 s call and its write.
  assert.equal(leaseCoversCall(job, claim + 40_700, 780_000, write), true);
  // A start that would end the call at the lease deadline is declined: the write would overrun.
  const lateStart = job.lease_expires_at - 780_000;
  assert.equal(leaseCoversCall(job, lateStart, 780_000), true);
  assert.equal(leaseCoversCall(job, lateStart, 780_000, write), false);
  assert.equal(leaseCoversCall(job, lateStart - write, 780_000, write), true);
});

test("putJson aborts a write that outlives its timeout, and sends no signal without one", async (t) => {
  const client = new B2Client({
    endpoint: "https://s3.us-west-004.backblazeb2.com", bucket: "b", keyId: "k", appKey: "s",
  });
  const seen = [];
  t.mock.method(globalThis, "fetch", (url, init) => {
    seen.push(init);
    if (!init.signal) return Promise.resolve(new Response("", { status: 200 }));
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason));
    });
  });
  await client.putJson("results/j/t.json", { ok: true });
  assert.equal(seen[0].signal, undefined);
  await assert.rejects(client.putJson("results/j/t.json", { ok: true }, { timeoutMs: 20 }),
    (err) => err.name === "TimeoutError");
  assert.ok(seen[1].signal instanceof AbortSignal);
});

test("Nemotron 3 Ultra's declared reasoning switch is sent only when a lane asks for it", () => {
  const payload = { messages: [{ role: "user", content: "x" }], max_tokens: 100 };
  const ultra = LIMITS.routes_by_id.nvidia_nemotron_3_ultra_550b_a55b_free;
  assert.deepEqual(ultra.reasoning_controls, { off: { chat_template_kwargs: { enable_thinking: false } } });
  // No lane sets a level for the model today (review/58 §12.5): the request is unchanged.
  for (const lane of Object.values(RESERVATIONS.reservations)) {
    assert.equal(lane.reasoning?.[ultra.model], undefined);
  }
  assert.equal(upstreamRequestForRoute(payload, ultra).chat_template_kwargs, undefined);
  assert.deepEqual(
    upstreamRequestForRoute(payload, ultra, { reasoningLevel: "off" }).chat_template_kwargs,
    { enable_thinking: false },
  );
  // The other slow routes declare nothing, so no lane level can change their requests.
  for (const routeId of SLOW_ROUTES.filter((id) => !id.startsWith("nvidia_"))) {
    const route = LIMITS.routes_by_id[routeId];
    assert.equal(Object.keys(route.reasoning_controls || {}).length, 0, routeId);
    const sent = upstreamRequestForRoute(payload, route, { reasoningLevel: "off" });
    for (const key of ["chat_template_kwargs", "reasoning_effort", "reasoning"]) {
      assert.equal(sent[key], undefined, `${routeId} ${key}`);
    }
  }
});
