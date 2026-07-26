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

const GRANICUS_PATHS = new Set([
  "Archive.php",
  "DownloadFile.php",
  "JSON.php",
  "ViewPublisherRSS.php",
]);
const GRANICUS_QUERY_KEYS = new Set(["view_id", "clip_id", "mode", "file", "entrytime"]);

function parseGranicusPath(requestUrl, env) {
  const url = new URL(requestUrl);
  if (url.hash) return null;
  const match = url.pathname.match(/^\/v1\/granicus\/([a-z0-9.-]+)\/([^/]+)$/);
  if (!match) return null;
  const host = match[1];
  const path = match[2];
  const tenant = host.endsWith(".granicus.com") ? host.slice(0, -".granicus.com".length) : "";
  if (!tenant || !allowedTenants(env).has(tenant) || !GRANICUS_PATHS.has(path)) return null;
  const pairs = [...url.searchParams.entries()];
  if (
    pairs.length > 8 ||
    pairs.some(([key, value]) => !GRANICUS_QUERY_KEYS.has(key) || value.length > 240)
  ) return null;
  return { host, path, search: url.search };
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
  const provider = archive ? null : parseGranicusPath(request.url, env);
  if (!archive && !provider) {
    return plain(404, "not found");
  }

  const upstreamUrl = archive
    ? `${UPSTREAM_ORIGIN}/${archive.tenant}/${archive.filename}`
    : `https://${provider.host}/${provider.path}${provider.search}`;
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

  if (
    upstream.status !== 304 && upstream.status >= 300 && upstream.status < 400 &&
    provider?.path !== "DownloadFile.php"
  ) {
    return plain(502, "upstream redirect refused");
  }

  const noBody = request.method === "HEAD" || upstream.status === 304;
  const headers = clientResponseHeaders(upstream);
  if (provider?.path === "DownloadFile.php" && upstream.headers.get("location")) {
    headers.set("location", upstream.headers.get("location"));
  }
  return new Response(noBody ? null : upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers,
  });
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  },
};
