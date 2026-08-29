// Thin URL-rewriting shim that sits between Cloudflare AI Gateway and providers whose API path
// the gateway cannot express.
//
// AI Gateway rewrites the LAST path segment of a Custom Provider's registered Base URL to a
// hardcoded `v1` before appending the caller's path (verified 2026-08-29 against a throwaway echo
// provider; undocumented, and contrary to the documented `{base_url}/{provider-path}` join). A
// provider whose API lives under a prefix that does not end in `v1` is therefore unreachable:
// z.ai's `/api/paas/v4` becomes `/api/paas/v1`. Registering this Worker as the Custom Provider and
// letting it restore the real prefix is what keeps those providers inside AI Gateway's logging
// instead of bypassing the gateway entirely.
//
// Registered Base URL shape (the trailing segment is deliberately sacrificial -- the gateway eats
// it and substitutes `v1`, which is what makes the rewrite predictable rather than accidental):
//
//   https://<worker-host>/<SHIM_TOKEN>/<provider>/x
//     -> gateway calls: /<SHIM_TOKEN>/<provider>/v1/<caller path>
//
// The token lives in the Base URL because the gateway strips `cf-aig-authorization` before it
// reaches the upstream (confirmed by the same echo probe), so there is no gateway-supplied
// credential left to authenticate against. Everything else is a fixed allowlist: the destination
// is never caller-controlled, which is what keeps this from being an open relay for the provider
// API keys it forwards.

const UPSTREAMS = new Map([
  ["zai", "https://api.z.ai/api/paas/v4"],
  ["opencode", "https://opencode.ai/zen/v1"],
]);

// Hop-by-hop and Cloudflare-injected headers are dropped rather than relayed; everything the
// providers actually need is a short allowlist.
const FORWARDED_REQUEST_HEADERS = ["authorization", "content-type", "accept"];
const FORWARDED_RESPONSE_HEADERS = ["content-type", "retry-after", "x-request-id"];

const MAX_PATH_LENGTH = 240;
const MAX_QUERY_LENGTH = 240;

function plain(status, message) {
  return new Response(`${message}\n`, {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "text/plain; charset=utf-8",
      "x-content-type-options": "nosniff",
    },
  });
}

/** Length-independent string compare, so a wrong token cannot be recovered byte by byte. */
function secretEquals(a, b) {
  const left = String(a ?? "");
  const right = String(b ?? "");
  if (left.length === 0 || left.length !== right.length) return false;
  let diff = 0;
  for (let i = 0; i < left.length; i += 1) {
    diff |= left.charCodeAt(i) ^ right.charCodeAt(i);
  }
  return diff === 0;
}

/**
 * Split `/<token>/<provider>/v1/<rest>` into its parts, or return null if anything is off.
 *
 * The literal `v1` is required: it is the segment AI Gateway substitutes, so its absence means the
 * request did not arrive the way this Worker is meant to be reached and should not be proxied.
 */
export function parseShimPath(requestUrl, env) {
  const url = new URL(requestUrl);
  if (url.hash) return null;
  if (url.pathname.includes("%") || url.pathname.includes("..")) return null;
  if (url.pathname.length > MAX_PATH_LENGTH || url.search.length > MAX_QUERY_LENGTH) return null;

  const match = url.pathname.match(/^\/([A-Za-z0-9_-]+)\/([a-z0-9-]+)\/v1\/([A-Za-z0-9/._-]+)$/);
  if (!match) return null;

  const [, token, provider, rest] = match;
  if (!secretEquals(token, env.SHIM_TOKEN)) return null;

  const upstream = UPSTREAMS.get(provider);
  if (!upstream) return null;

  return { provider, url: `${upstream}/${rest}${url.search}` };
}

export async function handleRequest(request, env, fetchImpl = fetch) {
  if (request.method !== "POST" && request.method !== "GET") {
    return plain(405, "Method not allowed");
  }
  if (!env.SHIM_TOKEN) {
    // Fail closed: an unset secret must never degrade into an unauthenticated open relay.
    return plain(503, "Shim is not configured");
  }

  const target = parseShimPath(request.url, env);
  // One opaque 404 for every rejection -- a bad token, an unknown provider and a malformed path
  // are indistinguishable to a prober.
  if (!target) return plain(404, "Not found");

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (!headers.has("authorization")) return plain(401, "Missing provider credentials");

  let upstreamResponse;
  try {
    upstreamResponse = await fetchImpl(target.url, {
      method: request.method,
      headers,
      body: request.method === "POST" ? request.body : undefined,
    });
  } catch {
    return plain(502, "Upstream request failed");
  }

  const responseHeaders = new Headers({ "cache-control": "no-store" });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstreamResponse.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    headers: responseHeaders,
  });
}

export default {
  fetch: (request, env) => handleRequest(request, env),
};
