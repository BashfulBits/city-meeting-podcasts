const UPSTREAM_ORIGIN = "https://archive-video.granicus.com";
const DEFAULT_USER_AGENT =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
const FORWARDED_REQUEST_HEADERS = ["range", "if-range", "if-none-match", "if-modified-since"];
const FORWARDED_RESPONSE_HEADERS = [
  "accept-ranges",
  "content-length",
  "content-range",
  "content-type",
  "etag",
  "last-modified",
];

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

function allowedTenants(env) {
  return new Set(
    String(env.ALLOWED_TENANTS || "")
      .split(",")
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean),
  );
}

function parseArchivePath(requestUrl, env) {
  const url = new URL(requestUrl);
  if (url.search || url.hash || url.pathname.includes("%")) {
    return null;
  }
  const match = url.pathname.match(/^\/v1\/archive\/([a-z0-9-]+)\/([^/]+)$/);
  if (!match) {
    return null;
  }
  const tenant = match[1];
  const filename = match[2];
  if (!allowedTenants(env).has(tenant)) {
    return null;
  }
  if (
    filename.length > 240 ||
    filename.includes("..") ||
    !filename.startsWith(`${tenant}_`) ||
    !/^[a-zA-Z0-9][a-zA-Z0-9._-]*\.mp4$/.test(filename)
  ) {
    return null;
  }
  return { tenant, filename };
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

function upstreamRequestHeaders(request, env) {
  const headers = new Headers({
    accept: "*/*",
    "user-agent": env.UPSTREAM_USER_AGENT || DEFAULT_USER_AGENT,
  });
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) {
      headers.set(name, value);
    }
  }
  return headers;
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
  if (!["GET", "HEAD"].includes(request.method)) {
    return plain(405, "method not allowed", { allow: "GET, HEAD" });
  }
  if (!(await authorized(request, env))) {
    return plain(401, "unauthorized", { "www-authenticate": "Bearer" });
  }
  const archive = parseArchivePath(request.url, env);
  if (!archive) {
    return plain(404, "not found");
  }

  const upstreamUrl = `${UPSTREAM_ORIGIN}/${archive.tenant}/${archive.filename}`;
  let upstream;
  try {
    upstream = await fetchImpl(upstreamUrl, {
      method: request.method,
      headers: upstreamRequestHeaders(request, env),
      redirect: "manual",
      cf: {
        cacheEverything: false,
        cacheTtl: 0,
      },
    });
  } catch {
    return plain(502, "upstream fetch failed");
  }

  if (upstream.status !== 304 && upstream.status >= 300 && upstream.status < 400) {
    return plain(502, "upstream redirect refused");
  }

  const noBody = request.method === "HEAD" || upstream.status === 304;
  return new Response(noBody ? null : upstream.body, {
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
