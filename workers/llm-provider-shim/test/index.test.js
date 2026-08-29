import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest, parseShimPath } from "../src/index.js";

const ENV = { SHIM_TOKEN: "test-shim-token" };
const VALID_URL = "https://shim.example/test-shim-token/zai/v1/chat/completions";

function request(url = VALID_URL, options = {}) {
  const headers = new Headers(options.headers);
  if (!headers.has("authorization")) headers.set("authorization", "Bearer provider-key");
  return new Request(url, { method: "POST", body: "{}", ...options, headers });
}

function recordingFetch(response = new Response("{}", { status: 200 })) {
  const calls = [];
  return {
    calls,
    impl: async (url, init) => {
      calls.push({ url, init });
      return response;
    },
  };
}

test("restores the real provider prefix the gateway rewrote away", async () => {
  const fetchImpl = recordingFetch();
  const response = await handleRequest(request(), ENV, fetchImpl.impl);

  assert.equal(response.status, 200);
  assert.equal(fetchImpl.calls.length, 1);
  assert.equal(
    fetchImpl.calls[0].url,
    "https://api.z.ai/api/paas/v4/chat/completions",
    "z.ai's /api/paas/v4 prefix is exactly what AI Gateway cannot express",
  );
});

test("forwards the query string alongside the rewritten path", async () => {
  const fetchImpl = recordingFetch();
  await handleRequest(
    request("https://shim.example/test-shim-token/opencode/v1/models?limit=2"),
    ENV,
    fetchImpl.impl,
  );

  assert.equal(fetchImpl.calls[0].url, "https://opencode.ai/zen/v1/models?limit=2");
});

test("rejects a wrong token without contacting any upstream", async () => {
  const fetchImpl = recordingFetch();
  const response = await handleRequest(
    request("https://shim.example/wrong-token/zai/v1/chat/completions"),
    ENV,
    fetchImpl.impl,
  );

  assert.equal(response.status, 404);
  assert.equal(fetchImpl.calls.length, 0);
});

test("rejects an unknown provider so the destination is never caller-chosen", async () => {
  const fetchImpl = recordingFetch();
  const response = await handleRequest(
    request("https://shim.example/test-shim-token/evil/v1/chat/completions"),
    ENV,
    fetchImpl.impl,
  );

  assert.equal(response.status, 404);
  assert.equal(fetchImpl.calls.length, 0);
});

test("fails closed when the secret is unset rather than becoming an open relay", async () => {
  const fetchImpl = recordingFetch();
  const response = await handleRequest(request(), {}, fetchImpl.impl);

  assert.equal(response.status, 503);
  assert.equal(fetchImpl.calls.length, 0);
});

test("requires the caller to supply provider credentials", async () => {
  const fetchImpl = recordingFetch();
  const headers = new Headers();
  headers.set("content-type", "application/json");
  const response = await handleRequest(
    new Request(VALID_URL, { method: "POST", body: "{}", headers }),
    ENV,
    fetchImpl.impl,
  );

  assert.equal(response.status, 401);
  assert.equal(fetchImpl.calls.length, 0);
});

test("forwards only the allowlisted request headers", async () => {
  const fetchImpl = recordingFetch();
  await handleRequest(
    request(VALID_URL, {
      headers: new Headers({
        authorization: "Bearer provider-key",
        "content-type": "application/json",
        "cf-connecting-ip": "203.0.113.7",
        cookie: "session=secret",
      }),
    }),
    ENV,
    fetchImpl.impl,
  );

  const sent = fetchImpl.calls[0].init.headers;
  assert.equal(sent.get("authorization"), "Bearer provider-key");
  assert.equal(sent.get("content-type"), "application/json");
  assert.equal(sent.get("cf-connecting-ip"), null);
  assert.equal(sent.get("cookie"), null);
});

test("refuses path traversal that would escape the pinned prefix", async () => {
  for (const path of [
    "/test-shim-token/zai/v1/../../../etc/passwd",
    "/test-shim-token/zai/v1/%2e%2e/x",
  ]) {
    assert.equal(parseShimPath(`https://shim.example${path}`, ENV), null, path);
  }
});

test("requires the literal v1 segment the gateway substitutes", async () => {
  assert.equal(
    parseShimPath("https://shim.example/test-shim-token/zai/chat/completions", ENV),
    null,
  );
});

test("surfaces an upstream network failure as 502", async () => {
  const response = await handleRequest(request(), ENV, async () => {
    throw new Error("connection reset");
  });

  assert.equal(response.status, 502);
});

test("passes the upstream status through unchanged", async () => {
  const fetchImpl = recordingFetch(new Response("{}", { status: 429 }));
  const response = await handleRequest(request(), ENV, fetchImpl.impl);

  assert.equal(response.status, 429);
});

test("refuses an upstream redirect instead of replaying the provider key to its target", async () => {
  // Workers' fetch defaults to redirect: "follow" and forwards Authorization across origins, so a
  // 3xx from a provider would hand its API key to whatever host Location named.
  const redirect = new Response(null, {
    status: 302,
    headers: { location: "https://attacker.example/collect" },
  });
  const fetchImpl = recordingFetch(redirect);
  const response = await handleRequest(request(), ENV, fetchImpl.impl);

  assert.equal(response.status, 502);
  assert.equal(fetchImpl.calls.length, 1, "must not have followed the redirect");
  assert.equal(
    fetchImpl.calls[0].init.redirect,
    "manual",
    "redirect must be manual so the runtime never replays credentials on its own",
  );
  assert.equal(response.headers.get("location"), null, "must not relay the redirect target");
});

test("passes a non-redirect 3xx-adjacent status through untouched", async () => {
  const fetchImpl = recordingFetch(new Response("{}", { status: 200 }));
  const response = await handleRequest(request(), ENV, fetchImpl.impl);

  assert.equal(response.status, 200);
});
