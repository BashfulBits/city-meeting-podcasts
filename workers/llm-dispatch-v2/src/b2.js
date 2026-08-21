/**
 * Minimal AWS SigV4-signed client for Backblaze B2's S3-compatible API, for the *executor*
 * Worker only (review/44: "Only the caller (Python client) and the executor Worker hold this
 * key -- the ingress Worker never does"). Implements exactly the operations Unit 6 needs: read a
 * claimed job's payload, write its result, and the bounded cleanup/orphan-sweep deletes and
 * listing described in "B2-only payload storage and bounded cleanup". No SDK dependency --
 * Cloudflare Workers don't have Node's `crypto`/`http`, and pulling in the full AWS SDK for four
 * operations is exactly the kind of "fork provider credential logic" Phase 1 was told to avoid;
 * this is a from-scratch, from-spec implementation instead, verified against a manual
 * independent re-derivation of the same SigV4 test inputs in test/b2.test.js.
 *
 * Algorithm: https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html
 */

const encoder = new TextEncoder();

async function sha256Hex(data) {
  const bytes = typeof data === "string" ? encoder.encode(data) : data;
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmac(keyBytes, data) {
  const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
  ]);
  const signature = await crypto.subtle.sign("HMAC", key, typeof data === "string" ? encoder.encode(data) : data);
  return new Uint8Array(signature);
}

function hex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// UriEncode() per the spec: encode everything except unreserved characters, and (for the path)
// leave '/' unencoded. encodeURIComponent already matches AWS's unreserved-character set closely
// enough except for '!', '*', "'", "(", ")", which it leaves unescaped and AWS requires escaped.
function uriEncode(value, { encodeSlash = true } = {}) {
  let encoded = encodeURIComponent(value).replace(
    /[!'()*]/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`
  );
  if (!encodeSlash) encoded = encoded.replace(/%2F/g, "/");
  return encoded;
}

function amzDateParts(date) {
  const iso = date.toISOString().replace(/[:-]|\.\d{3}/g, "");
  return { amzDate: iso, dateStamp: iso.slice(0, 8) };
}

async function deriveSigningKey(secretAccessKey, dateStamp, region, service) {
  const kDate = await hmac(encoder.encode(`AWS4${secretAccessKey}`), dateStamp);
  const kRegion = await hmac(kDate, region);
  const kService = await hmac(kRegion, service);
  return hmac(kService, "aws4_request");
}

/**
 * Signs one request and returns the headers to send alongside it (including Authorization).
 * `query` is an already-sorted array of [key, value] pairs (not a query string) so the canonical
 * query string construction stays exact regardless of caller formatting habits.
 */
export async function signRequest({
  method,
  host,
  region,
  path = "/",
  query = [],
  headers = {},
  body = "",
  accessKeyId,
  secretAccessKey,
  now = new Date(),
}) {
  const service = "s3";
  const { amzDate, dateStamp } = amzDateParts(now);
  const payloadHash = await sha256Hex(body);

  const canonicalUri = path
    .split("/")
    .map((segment, i) => (i === 0 && segment === "" ? "" : uriEncode(segment, { encodeSlash: false })))
    .join("/");

  const canonicalQuery = [...query]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${uriEncode(k)}=${uriEncode(v)}`)
    .join("&");

  const allHeaders = {};
  for (const [k, v] of Object.entries(headers)) {
    allHeaders[k.toLowerCase()] = v;
  }
  allHeaders["host"] = host;
  allHeaders["x-amz-content-sha256"] = payloadHash;
  allHeaders["x-amz-date"] = amzDate;
  const sortedHeaderNames = Object.keys(allHeaders).sort();
  const canonicalHeaders =
    sortedHeaderNames.map((name) => `${name}:${String(allHeaders[name]).trim()}\n`).join("");
  const signedHeaders = sortedHeaderNames.join(";");

  const canonicalRequest = [
    method,
    canonicalUri || "/",
    canonicalQuery,
    canonicalHeaders,
    signedHeaders,
    payloadHash,
  ].join("\n");

  const credentialScope = `${dateStamp}/${region}/${service}/aws4_request`;
  const stringToSign = [
    "AWS4-HMAC-SHA256",
    amzDate,
    credentialScope,
    await sha256Hex(canonicalRequest),
  ].join("\n");

  const signingKey = await deriveSigningKey(secretAccessKey, dateStamp, region, service);
  const signature = hex(await hmac(signingKey, stringToSign));

  const authorization =
    `AWS4-HMAC-SHA256 Credential=${accessKeyId}/${credentialScope}, ` +
    `SignedHeaders=${signedHeaders}, Signature=${signature}`;

  return {
    ...headers,
    "x-amz-content-sha256": payloadHash,
    "x-amz-date": amzDate,
    authorization,
  };
}

/** region inferred from a Backblaze-style endpoint host, e.g. "s3.us-west-004.backblazeb2.com". */
export function regionFromEndpoint(endpointHost) {
  const parts = endpointHost.split(".");
  return parts.length >= 2 ? parts[1] : "us-east-1";
}

export class B2Client {
  constructor({ endpoint, bucket, keyId, appKey }) {
    const url = new URL(endpoint);
    this.host = url.host;
    this.origin = url.origin;
    this.bucket = bucket;
    this.keyId = keyId;
    this.appKey = appKey;
    this.region = regionFromEndpoint(url.hostname);
  }

  async _signed(method, key, { query = [], body = "", extraHeaders = {} } = {}) {
    const path = `/${this.bucket}/${key}`;
    const headers = await signRequest({
      method,
      host: this.host,
      region: this.region,
      path,
      query,
      headers: extraHeaders,
      body,
      accessKeyId: this.keyId,
      secretAccessKey: this.appKey,
    });
    return { url: `${this.origin}${path}${query.length ? `?${query.map(([k, v]) => `${uriEncode(k)}=${uriEncode(v)}`).join("&")}` : ""}`, headers };
  }

  /** Returns the parsed JSON body, or null on a 404. Throws on any other non-2xx status. */
  async getJson(key) {
    const { url, headers } = await this._signed("GET", key);
    const response = await fetch(url, { method: "GET", headers });
    if (response.status === 404) return null;
    if (!response.ok) {
      throw new Error(`B2 GET ${key} failed: ${response.status} ${await response.text()}`);
    }
    return response.json();
  }

  async putJson(key, value) {
    const body = typeof value === "string" ? value : JSON.stringify(value);
    const { url, headers } = await this._signed("PUT", key, {
      body,
      extraHeaders: { "content-type": "application/json" },
    });
    const response = await fetch(url, { method: "PUT", headers, body });
    if (!response.ok) {
      throw new Error(`B2 PUT ${key} failed: ${response.status} ${await response.text()}`);
    }
  }

  async delete(key) {
    const { url, headers } = await this._signed("DELETE", key);
    const response = await fetch(url, { method: "DELETE", headers });
    if (!response.ok && response.status !== 404) {
      throw new Error(`B2 DELETE ${key} failed: ${response.status} ${await response.text()}`);
    }
  }
}
