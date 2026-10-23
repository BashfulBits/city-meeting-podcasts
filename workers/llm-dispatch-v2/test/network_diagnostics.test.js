import test from "node:test";
import assert from "node:assert/strict";
import {
  gatewayNetworkFacts, networkProbeAllowed, observeExecutionNetwork, parseNetworkTrace,
  observeBundleNetwork, observeIndependentNetwork,
} from "../src/network_diagnostics.js";

test("network observation retains only explicit IP/location facts", async () => {
  let calls = 0;
  const facts = await observeExecutionNetwork(async (url, options) => {
    calls++;
    assert.equal(url, "https://www.cloudflare.com/cdn-cgi/trace");
    assert.equal(options.redirect, "error");
    assert.ok(options.signal);
    return new Response("ip=192.0.2.1\ncolo=IAD\nloc=US\nuag=PRIVATE\n");
  });
  assert.equal(calls, 1);
  assert.deepEqual(facts, { probe_status: "ok", worker_probe_ip: "192.0.2.1",
    worker_probe_ip_kind: "trace_reported",
    worker_probe_colo: "IAD", worker_probe_country: "US" });
});

test("network observation bounds response reads and excludes raw errors", async () => {
  assert.deepEqual(await observeExecutionNetwork(async () => { throw Error("PRIVATE"); }),
    { probe_status: "unavailable" });
  assert.deepEqual(await observeExecutionNetwork(async () => new Response("x".repeat(4097))),
    { probe_status: "oversize" });
  const invalid = await observeExecutionNetwork(
    async () => new Response("ip=PRIVATE\ncolo=PRIVATE"),
  );
  assert.equal(invalid.probe_status, "invalid");
  assert.equal(invalid.worker_probe_ip, null);
});

test("probe skips cleanup ticks and respects the Free subrequest budget", () => {
  const now = Date.UTC(2026, 9, 23, 20, 1);
  assert.equal(networkProbeAllowed({}, 5, now), true);
  assert.equal(networkProbeAllowed({}, 12, now), false);
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
    provider_error_status: "FAILED_PRECONDITION",
    provider_error_signature: "unsupported_location" });
  assert.equal(JSON.stringify(facts).includes("PRIVATE"), false);
  assert.equal(gatewayNetworkFacts({ headers: new Headers({ "cf-ray": "PRIVATE" }) })
    .gateway_cf_ray, null);
});


test("the cross-zone Worker placeholder is explicitly labeled", () => {
  assert.equal(parseNetworkTrace("ip=2a06:98c0:3600:0:0:0:0:103").worker_probe_ip_kind,
    "worker_placeholder");
});


test("independent lookup uses the AWS-observed IP and one deadline", async () => {
  const requests = [];
  const result = await observeIndependentNetwork(async (url, options) => {
    requests.push({ url, ...options });
    if (url === "https://checkip.global.api.aws/") return new Response("1.1.1.1\n");
    assert.equal(url, "https://api.ip.sb/geoip/1.1.1.1");
    return Response.json({ ip: "1.1.1.1", country_code: "AU", asn: 13335,
      asn_organization: "Cloudflare, Inc.", city: "PRIVATE", extra: "PRIVATE" });
  });
  assert.equal(requests.length, 2);
  assert.equal(requests[0].signal, requests[1].signal);
  assert.equal(requests[0].redirect, "error");
  assert.equal(requests[1].headers["user-agent"], "Citypods-Network-Diagnostics/1.0");
  assert.equal(result.external_probe_ip, "1.1.1.1");
  assert.equal(result.external_country_code, "AU");
  assert.equal(result.external_asn, 13335);
  assert.equal(JSON.stringify(result).includes("PRIVATE"), false);
});

test("independent lookup rejects URLs and wrong-address GeoIP", async () => {
  for (const ip of ["https://PRIVATE/", "1.2.3.999", "1::2::3", "2a06:98c0:3600::103"]) {
    let calls = 0;
    const result = await observeIndependentNetwork(async () => {
      calls++;
      return new Response(ip);
    });
    assert.equal(result.external_probe_status, "invalid");
    assert.equal(calls, 1);
  }
  let calls = 0;
  const mismatch = await observeIndependentNetwork(async () => ++calls === 1
    ? new Response("1.1.1.1") : Response.json({ ip: "8.8.8.8", country_code: "US" }));
  assert.equal(mismatch.external_geoip_status, "ip_mismatch");
  assert.equal(mismatch.external_country_code, undefined);
});

test("bundle diagnostics retain both IP observations when GeoIP fails", async () => {
  const calls = [];
  const signals = [];
  const result = await observeBundleNetwork(async (url, options) => {
    calls.push(url);
    signals.push(options.signal);
    if (url.includes("cloudflare.com")) {
      return new Response("ip=2a06:98c0:3600::103\ncolo=IAD\nloc=US");
    }
    if (url.includes("api.aws")) return new Response("2606:4700:4700::1111");
    assert.equal(url, "https://api.ip.sb/geoip/2606%3A4700%3A4700%3A%3A1111");
    return new Response("PRIVATE", { status: 429 });
  });
  assert.equal(calls.length, 3);
  assert.ok(signals.every(signal => signal === signals[0]));
  assert.ok(calls[0].includes("api.aws"));
  assert.ok(calls[2].includes("cloudflare.com"));
  assert.equal(result.worker_probe_ip_kind, "worker_placeholder");
  assert.equal(result.external_probe_ip, "2606:4700:4700::1111");
  assert.equal(result.external_geoip_status, "http_error");
  assert.equal(result.external_geoip_http_status, 429);
  assert.equal(JSON.stringify(result).includes("PRIVATE"), false);
});
