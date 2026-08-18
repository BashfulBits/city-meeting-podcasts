import test from "node:test";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import { signRequest, regionFromEndpoint } from "../src/b2.js";

/**
 * Independently re-derives the same SigV4 signature using Node's built-in `crypto` module (a
 * completely separate implementation path from src/b2.js's Web Crypto-based one) for the exact
 * same fixed inputs, following https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html
 * step by step. Agreement between the two independent implementations is the correctness check --
 * there's no live B2/AWS credential available in a unit test to verify against the real service.
 */
function referenceSignature({ method, host, region, path, query, headers, body, accessKeyId, secretAccessKey, amzDate, dateStamp }) {
  const sha256Hex = (data) => crypto.createHash("sha256").update(data).digest("hex");
  const hmac = (key, data) => crypto.createHmac("sha256", key).update(data).digest();

  const payloadHash = sha256Hex(body);
  const canonicalQuery = [...query]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");
  const allHeaders = { ...headers, host, "x-amz-content-sha256": payloadHash, "x-amz-date": amzDate };
  const sortedNames = Object.keys(allHeaders).map((k) => k.toLowerCase()).sort();
  const canonicalHeaders = sortedNames.map((n) => `${n}:${String(allHeaders[n]).trim()}\n`).join("");
  const signedHeaders = sortedNames.join(";");

  const canonicalRequest = [method, path, canonicalQuery, canonicalHeaders, signedHeaders, payloadHash].join(
    "\n"
  );
  const credentialScope = `${dateStamp}/${region}/s3/aws4_request`;
  const stringToSign = ["AWS4-HMAC-SHA256", amzDate, credentialScope, sha256Hex(canonicalRequest)].join("\n");

  const kDate = hmac(`AWS4${secretAccessKey}`, dateStamp);
  const kRegion = hmac(kDate, region);
  const kService = hmac(kRegion, "s3");
  const kSigning = hmac(kService, "aws4_request");
  const signature = hmac(kSigning, stringToSign).toString("hex");

  return {
    signature,
    authorization:
      `AWS4-HMAC-SHA256 Credential=${accessKeyId}/${credentialScope}, ` +
      `SignedHeaders=${signedHeaders}, Signature=${signature}`,
  };
}

test("signRequest matches an independent Node-crypto re-derivation for a GET with no body", async () => {
  const now = new Date("2024-03-15T12:34:56.000Z");
  const params = {
    method: "GET",
    host: "s3.us-west-004.backblazeb2.com",
    region: "us-west-004",
    path: "/my-bucket/payloads/job-1/request.json",
    query: [],
    headers: {},
    body: "",
    accessKeyId: "0041234567890abcdef",
    secretAccessKey: "K004abcdefghijklmnopqrstuvwxyz0123456789",
    now,
  };

  const headers = await signRequest(params);
  const { amzDate, dateStamp } = { amzDate: "20240315T123456Z", dateStamp: "20240315" };
  const reference = referenceSignature({ ...params, amzDate, dateStamp });

  assert.equal(headers["x-amz-date"], amzDate);
  assert.equal(headers.authorization, reference.authorization);
});

test("signRequest matches an independent Node-crypto re-derivation for a PUT with a JSON body", async () => {
  const now = new Date("2024-03-15T00:00:00.000Z");
  const body = JSON.stringify({ hello: "world", n: 1 });
  const params = {
    method: "PUT",
    host: "s3.us-west-004.backblazeb2.com",
    region: "us-west-004",
    path: "/my-bucket/results/job-1/lease-token.json",
    query: [],
    headers: { "content-type": "application/json" },
    body,
    accessKeyId: "0041234567890abcdef",
    secretAccessKey: "K004abcdefghijklmnopqrstuvwxyz0123456789",
    now,
  };

  const headers = await signRequest(params);
  const reference = referenceSignature({ ...params, amzDate: "20240315T000000Z", dateStamp: "20240315" });

  assert.equal(headers.authorization, reference.authorization);
});

test("signRequest sorts query parameters and percent-encodes them consistently", async () => {
  const now = new Date("2024-01-01T00:00:00.000Z");
  const params = {
    method: "GET",
    host: "s3.us-west-004.backblazeb2.com",
    region: "us-west-004",
    path: "/my-bucket/",
    query: [
      ["prefix", "payloads/orphan sweep"],
      ["marker", "abc"],
    ],
    headers: {},
    body: "",
    accessKeyId: "keyid",
    secretAccessKey: "secret",
    now,
  };
  const headers = await signRequest(params);
  const reference = referenceSignature({ ...params, amzDate: "20240101T000000Z", dateStamp: "20240101" });
  assert.equal(headers.authorization, reference.authorization);
});

test("regionFromEndpoint extracts the region segment from a Backblaze-style host", () => {
  assert.equal(regionFromEndpoint("s3.us-west-004.backblazeb2.com"), "us-west-004");
  assert.equal(regionFromEndpoint("s3.eu-central-003.backblazeb2.com"), "eu-central-003");
});
