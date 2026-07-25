const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
// Swagit archive list/view pages, e.g. /views/117/city-council-meetings, /views/5,
// /views/default/city-council -- one or two path segments after "views".
const PATH_RE = /^views\/[A-Za-z0-9_-]+(\/[A-Za-z0-9_-]+)?$/;
const PAGE_RE = /^[1-9][0-9]{0,3}$/; // bounded 1..9999, mirrors swagit.py's MAX_ARCHIVE_PAGES
const FORWARDED_RESPONSE_HEADERS = ["content-length", "content-type"];

function plain(status, message, extraHeaders = {}) {
  return new Response(`${message}\n`, {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "text/plain; charset=utf-8",
      "x-content-type-options": "nosniff",
      ...extraHeaders,
    },
  });
}

function allowedHosts(env) {
  return new Set(
    String(env.ALLOWED_HOSTS || "")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean),
  );
}

function parseSwagitRequest(requestUrl, env) {
  const url = new URL(requestUrl);
  if (url.hash) {
    return null;
  }
  const match = url.pathname.match(/^\/v1\/swagit\/([a-z0-9.-]+)\/(.+)$/);
  if (!match) {
    return null;
  }
  const host = match[1];
  const path = match[2];
  if (!allowedHosts(env).has(host) || !PATH_RE.test(path)) {
    return null;
  }
  const params = [...url.searchParams.keys()];
  if (params.length > 1 || (params.length === 1 && params[0] !== "page")) {
    return null;
  }
  const page = url.searchParams.get("page");
  if (page !== null && !PAGE_RE.test(page)) {
    return null;
  }
  return { host, path, page };
}

async function authorized(request, env) {
  const expected = String(env.PROXY_TOKEN || "");
  const authorization = request.headers.get("authorization") || "";
  if (!expected || !authorization.startsWith("Bearer ")) {
    return false;
  }
  const supplied = authorization.slice("Bearer ".length);
  const encoder = new TextEncoder();
  const [expectedHash, suppliedHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
    crypto.subtle.digest("SHA-256", encoder.encode(supplied)),
  ]);
  const left = new Uint8Array(expectedHash);
  const right = new Uint8Array(suppliedHash);
  let difference = left.length ^ right.length;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left[index] ^ (right[index] || 0);
  }
  return difference === 0;
}

function upstreamRequestHeaders(env) {
  return new Headers({
    accept: "text/html,*/*",
    "user-agent": env.UPSTREAM_USER_AGENT || DEFAULT_USER_AGENT,
  });
}

function clientResponseHeaders(upstream) {
  const headers = new Headers({
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) {
      headers.set(name, value);
    }
  }
  return headers;
}

export async function handleRequest(request, env, fetchImpl = fetch) {
  if (request.method !== "GET") {
    return plain(405, "method not allowed", { allow: "GET" });
  }
  if (!(await authorized(request, env))) {
    return plain(401, "unauthorized", { "www-authenticate": "Bearer" });
  }
  const parsed = parseSwagitRequest(request.url, env);
  if (!parsed) {
    return plain(404, "not found");
  }

  const upstreamUrl = `https://${parsed.host}/${parsed.path}${
    parsed.page ? `?page=${parsed.page}` : ""
  }`;
  let upstream;
  try {
    upstream = await fetchImpl(upstreamUrl, {
      method: "GET",
      headers: upstreamRequestHeaders(env),
      redirect: "manual",
      cf: {
        cacheEverything: false,
        cacheTtl: 0,
      },
    });
  } catch {
    return plain(502, "upstream fetch failed");
  }

  if (upstream.status >= 300 && upstream.status < 400) {
    return plain(502, "upstream redirect refused");
  }

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: clientResponseHeaders(upstream),
  });
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
};
