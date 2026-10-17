import assert from "node:assert/strict";
import test from "node:test";

import TUNING from "../src/dispatch_tuning.json" with { type: "json" };
import worker from "../src/index.js";
import { TUNING_NAMES, withTuning } from "../src/tuning.js";

test("an unset name falls back to the compiled value, surfaced as a string like a dashboard var", () => {
  const env = withTuning({ BEARER_TOKEN: "t" });
  assert.equal(env.DISPATCH_WINDOW_SECONDS, String(TUNING.values.DISPATCH_WINDOW_SECONDS));
  assert.equal(typeof env.MAX_BUNDLE_JOBS, "string");
  assert.equal("MAX_BUNDLE_JOBS" in env, true);
});

test("a Cloudflare variable or secret of the same name overrides the compiled value", () => {
  const env = withTuning({ MAX_BUNDLE_JOBS: "2", BEARER_TOKEN: "t" });
  assert.equal(env.MAX_BUNDLE_JOBS, "2");
  assert.equal(env.BEARER_TOKEN, "t");
});

test("names that are not tuning keys stay undefined, bindings pass through untouched", () => {
  const binding = { getByName() {} };
  const env = withTuning({ LLM_SCHEDULER: binding });
  assert.equal(env.NOT_A_TUNING_KEY, undefined);
  assert.equal(env.LLM_SCHEDULER, binding);
  assert.equal(withTuning(undefined).NOT_A_TUNING_KEY, undefined);
});

test("wrapping twice is a no-op", () => {
  const once = withTuning({ A: "1" });
  assert.equal(withTuning(once), once);
});

test("the compiled tuning holds no credential-, account- or URL-specific names", () => {
  for (const name of TUNING_NAMES) {
    assert.doesNotMatch(name, /KEY|TOKEN|SECRET|PASSWORD|ENDPOINT|ACCOUNT|URL|GATEWAY/, name);
    assert.ok(Number.isInteger(TUNING.values[name]) && TUNING.values[name] >= 0, name);
  }
});

test("the Worker entry point validates and serves with only the account-specific env", async () => {
  // Production sets just BEARER_TOKEN, the AI_GATEWAY_ID var and secrets; everything else is tuning.
  const res = await worker.fetch(new Request("https://x/healthz"), { BEARER_TOKEN: "t" }, {});
  assert.equal(res.status, 200);
});
