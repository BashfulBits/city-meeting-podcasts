import test from "node:test";
import assert from "node:assert/strict";
import {
  gatewayNetworkFacts, networkProbeAllowed, observeExecutionNetwork, parseNetworkTrace,
} from "../src/network_diagnostics.js";

test("network observation retains only explicit IP/location facts from one bounded request", async () => {
  let calls = 0;
  const facts = await observeExecutionNetwork(async (url, options) => {
    calls++;
    assert.equal(url, "https://www.cloudflare.com/cdn-cgi/trace");
    assert.equal(options.redirect, "error");
    assert.ok(options.signal);
    return new Response("ip=192.0.2.1\ncolo=IAD\nloc=US\nuag=PRIVATE\n");
  });
  assert.equal(calls, 1);
  assert.deepEqual(facts, { probe_status: "ok", worker_probe_ip: "192.0.2.1", worker_probe_ip_kind: "trace_reported",
    worker_probe_colo: "IAD", worker_probe_country: "US" });
});

test("network observation fails closed without raw errors or unlimited response buffering", async () => {
  assert.deepEqual(await observeExecutionNetwork(async () => { throw Error("PRIVATE"); }),
    { probe_status: "unavailable" });
  assert.deepEqual(await observeExecutionNetwork(async () => new Response("x".repeat(4097))),
    { probe_status: "oversize" });
  const invalid = await observeExecutionNetwork(async () => new Response("ip=PRIVATE\ncolo=PRIVATE"));
  assert.equal(invalid.probe_status, "invalid");
  assert.equal(invalid.worker_probe_ip, null);
});

test("probe skips cleanup ticks and respects the Free subrequest budget", () => {
  const now = Date.UTC(2026, 9, 23, 20, 1);
  assert.equal(networkProbeAllowed({}, 5, now), true);
  assert.equal(networkProbeAllowed({}, 5, now - 60_000), false);
  assert.equal(networkProbeAllowed({}, 5, undefined), false);
  assert.equal(networkProbeAllowed({ MAX_429_RETRIES: 8 }, 5, now), false);
});

test("Gateway trace and location refusal remain distinct from Worker egress observations", () => {
  const facts = gatewayNetworkFacts({ headers: new Headers({
    "cf-ray": "1234567890abcdef-SIN", "authorization": "PRIVATE",
  }), body: [{ error: { status: "FAILED_PRECONDITION",
    message: "User location is not supported for the API use. PRIVATE" } }] });
  assert.deepEqual(facts, { gateway_cf_ray: "1234567890abcdef-SIN", gateway_colo: "SIN",
    provider_error_status: "FAILED_PRECONDITION", provider_error_signature: "unsupported_location" });
  assert.equal(JSON.stringify(facts).includes("PRIVATE"), false);
  assert.equal(gatewayNetworkFacts({ headers: new Headers({ "cf-ray": "PRIVATE" }) })
    .gateway_cf_ray, null);
});


test("Cloudflare's cross-zone Worker placeholder is explicitly labeled, never claimed as egress", () => {
  assert.equal(parseNetworkTrace("ip=2a06:98c0:3600:0:0:0:0:103").worker_probe_ip_kind,
    "worker_placeholder");
});
