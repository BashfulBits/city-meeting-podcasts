/**
 * Placed egress for providers that refuse some Cloudflare locations (issue #2682, review/58 §12.12).
 *
 * Google AI Studio answers HTTP 400 "User location is not supported" to calls that leave
 * Cloudflare's HKG colo. The dispatch runs in the cron (`scheduled`) handler, and a cron
 * invocation runs wherever Cloudflare starts it: Worker placement (`placement` in wrangler.jsonc)
 * only moves fetch handler invocations. So the calls of the providers listed in
 * PLACED_EGRESS_PROVIDERS go through the Worker's own fetch handler over a self service binding
 * (`PLACED_EGRESS`), which runs at the placed location and makes the upstream call from there.
 *
 * The hop is private: it needs the ingress bearer token, accepts only POST, and only forwards to the
 * origin the caller's own credentials resolve to for an AI Gateway or Google API base (the SSRF gate:
 * the target is checked against a fixed allow list, never taken on trust).
 */

export const EGRESS_PATH = "/__placed_egress";
export const TARGET_HEADER = "x-citypods-egress-target";
export const AUTH_HEADER = "x-citypods-egress-auth";
export const COLO_HEADER = "x-citypods-egress-colo";

const ALLOWED_TARGET_HOSTS = new Set([
  "gateway.ai.cloudflare.com",
  "generativelanguage.googleapis.com",
]);

export function placedEgressProviders(env) {
  return new Set(
    String(env?.PLACED_EGRESS_PROVIDERS ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
  );
}

export function usesPlacedEgress(env, route) {
  return Boolean(env?.PLACED_EGRESS && env?.BEARER_TOKEN)
    && placedEgressProviders(env).has(String(route?.provider ?? ""));
}

export function allowedEgressTarget(value) {
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password || url.port) return null;
  return ALLOWED_TARGET_HOSTS.has(url.hostname) ? url : null;
}

/** Caller side: sends one provider request through the placed hop. Same contract as fetch(). */
export async function placedFetch(env, url, init) {
  const headers = new Headers(init.headers);
  headers.set(TARGET_HEADER, url);
  headers.set(AUTH_HEADER, env.BEARER_TOKEN);
  return env.PLACED_EGRESS.fetch(`https://placed-egress.internal${EGRESS_PATH}`, {
    ...init,
    headers,
  });
}

async function sameSecret(expected, given) {
  const enc = new TextEncoder();
  const a = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(expected)));
  const b = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(given)));
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

/**
 * Callee side, in the fetch handler. Returns null for any request that is not for the hop, so the
 * normal routing handles it (and answers 404 for an unknown path, as before).
 */
export async function handlePlacedEgress(request, env, { fetchImpl = fetch } = {}) {
  const path = new URL(request.url).pathname;
  if (path !== EGRESS_PATH) return null;
  const given = request.headers.get(AUTH_HEADER) || "";
  if (request.method !== "POST" || !env.BEARER_TOKEN || !given || !(await sameSecret(env.BEARER_TOKEN, given))) {
    return new Response("not found", { status: 404 });
  }
  const target = allowedEgressTarget(request.headers.get(TARGET_HEADER) || "");
  if (!target) {
    return new Response("egress target not allowed", { status: 400 });
  }
  const headers = new Headers(request.headers);
  headers.delete(TARGET_HEADER);
  headers.delete(AUTH_HEADER);
  headers.delete("host");
  const upstream = await fetchImpl(target.toString(), {
    method: "POST",
    headers,
    body: request.body,
    redirect: "manual",
    signal: request.signal,
  });
  const reply = new Response(upstream.body, upstream);
  const colo = request.cf?.colo;
  if (colo) reply.headers.set(COLO_HEADER, String(colo));
  return reply;
}
