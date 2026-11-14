import { test } from "node:test";
import assert from "node:assert/strict";

import {
  AUTH_HEADER, COLO_HEADER, EGRESS_PATH, TARGET_HEADER,
  allowedEgressTarget, handlePlacedEgress, placedFetch, usesPlacedEgress,
} from "../src/placed_egress.js";

const GOOGLE = "https://gateway.ai.cloudflare.com/v1/acct/gw/google-ai-studio/v1beta/models/x:generateContent";

function hopRequest({ token = "secret", target = GOOGLE, method = "POST", colo = "ORD" } = {}) {
  const headers = { "content-type": "application/json", "x-goog-api-key": "k" };
  if (token !== null) headers[AUTH_HEADER] = token;
  if (target !== null) headers[TARGET_HEADER] = target;
  const request = new Request(`https://placed-egress.internal${EGRESS_PATH}`, {
    method, headers, body: method === "POST" ? "{\"a\":1}" : undefined,
  });
  Object.defineProperty(request, "cf", { value: { colo } });
  return request;
}

test("only listed providers with the binding and token use the hop", () => {
  const env = { PLACED_EGRESS: {}, BEARER_TOKEN: "t", PLACED_EGRESS_PROVIDERS: "gemini, nvidia" };
  assert.equal(usesPlacedEgress(env, { provider: "gemini" }), true);
  assert.equal(usesPlacedEgress(env, { provider: "openrouter" }), false);
  assert.equal(usesPlacedEgress({ ...env, PLACED_EGRESS: undefined }, { provider: "gemini" }), false);
  assert.equal(usesPlacedEgress({ ...env, PLACED_EGRESS_PROVIDERS: "" }, { provider: "gemini" }), false);
});

test("the target allow list is the SSRF gate", () => {
  assert.ok(allowedEgressTarget(GOOGLE));
  assert.ok(allowedEgressTarget("https://generativelanguage.googleapis.com/v1beta/models"));
  for (const bad of [
    "http://gateway.ai.cloudflare.com/x",
    "https://evil.example/x",
    "https://gateway.ai.cloudflare.com.evil.example/x",
    "https://user:pw@gateway.ai.cloudflare.com/x",
    "https://gateway.ai.cloudflare.com:8443/x",
    "not a url",
  ]) {
    assert.equal(allowedEgressTarget(bad), null, bad);
  }
});

test("other paths fall through to normal routing", async () => {
  const r = await handlePlacedEgress(new Request("https://x/v2/stats"), { BEARER_TOKEN: "secret" });
  assert.equal(r, null);
});

test("a wrong token, missing token or GET is refused without an upstream call", async () => {
  let calls = 0;
  const fetchImpl = async () => { calls += 1; return new Response("{}"); };
  const env = { BEARER_TOKEN: "secret" };
  assert.equal((await handlePlacedEgress(hopRequest({ token: "nope" }), env, { fetchImpl })).status, 404);
  assert.equal((await handlePlacedEgress(hopRequest({ token: null }), env, { fetchImpl })).status, 404);
  assert.equal((await handlePlacedEgress(hopRequest({ method: "GET" }), env, { fetchImpl })).status, 404);
  assert.equal((await handlePlacedEgress(hopRequest({ target: "https://evil.example/" }), env, { fetchImpl })).status, 400);
  assert.equal(calls, 0);
});

test("a valid hop forwards body and provider headers, strips the hop headers, reports the colo", async () => {
  let seen;
  const fetchImpl = async (url, init) => {
    seen = { url, headers: init.headers, body: await new Response(init.body).text(), redirect: init.redirect };
    return new Response("{\"ok\":true}", { status: 400, headers: { "cf-aig-log-id": "L1" } });
  };
  const reply = await handlePlacedEgress(hopRequest(), { BEARER_TOKEN: "secret" }, { fetchImpl });
  assert.equal(reply.status, 400);
  assert.equal(reply.headers.get("cf-aig-log-id"), "L1");
  assert.equal(reply.headers.get(COLO_HEADER), "ORD");
  assert.equal(seen.url, GOOGLE);
  assert.equal(seen.body, "{\"a\":1}");
  assert.equal(seen.redirect, "manual");
  assert.equal(seen.headers.get("x-goog-api-key"), "k");
  assert.equal(seen.headers.get(AUTH_HEADER), null);
  assert.equal(seen.headers.get(TARGET_HEADER), null);
});

test("placedFetch sends the target and token through the binding", async () => {
  let seen;
  const env = {
    BEARER_TOKEN: "secret",
    PLACED_EGRESS: { fetch: async (url, init) => { seen = { url, init }; return new Response("{}"); } },
  };
  await placedFetch(env, GOOGLE, { method: "POST", headers: { a: "b" }, body: "x" });
  assert.equal(seen.url, `https://placed-egress.internal${EGRESS_PATH}`);
  assert.equal(seen.init.headers.get(TARGET_HEADER), GOOGLE);
  assert.equal(seen.init.headers.get(AUTH_HEADER), "secret");
  assert.equal(seen.init.headers.get("a"), "b");
  assert.equal(seen.init.body, "x");
});
