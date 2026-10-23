/** Fixed Cloudflare diagnostic endpoint: reports Worker-to-Cloudflare trace facts, not Google's connection. */
const TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace";

export function networkProbeAllowed(env, jobCount, scheduledTime) {
  if (!Number.isFinite(scheduledTime)) return false;
  const cleanup = Number(env.CLEANUP_INTERVAL_MINUTES || 10);
  if (new Date(scheduledTime).getUTCMinutes() % cleanup === 0) return false;
  // Payload GET + result PUT + initial provider call + authorized 429 retries, plus one probe.
  return jobCount * (3 + Number(env.MAX_429_RETRIES ?? 1)) + 1 <= 50;
}

export function parseNetworkTrace(text) {
  const values = Object.fromEntries(text.split("\n").map(line => {
    const at = line.indexOf("=");
    return at < 0 ? ["", ""] : [line.slice(0, at), line.slice(at + 1)];
  }));
  const ip = values.ip || "";
  const ipv4 = /^(?:\d{1,3}\.){3}\d{1,3}$/.test(ip) &&
    ip.split(".").every(part => Number(part) <= 255);
  const ipv6 = ip.includes(":") && /^[a-f0-9:]{2,45}$/i.test(ip);
  let placeholder = false;
  if (ipv6) {
    try { placeholder = new URL(`https://[${ip}]/`).hostname === "[2a06:98c0:3600::103]"; }
    catch { /* Invalid trace IP is not treated as a known placeholder. */ }
  }
  return {
    worker_probe_ip: ipv4 || ipv6 ? ip : null,
    worker_probe_ip_kind: placeholder ? "worker_placeholder" : ipv4 || ipv6 ? "trace_reported" : null,
    worker_probe_colo: /^[A-Z]{3}$/.test(values.colo || "") ? values.colo : null,
    worker_probe_country: /^[A-Z]{2}$/.test(values.loc || "") ? values.loc : null,
  };
}

export async function observeExecutionNetwork(fetchImpl = fetch) {
  try {
    const response = await fetchImpl(TRACE_URL, {
      redirect: "error", signal: AbortSignal.timeout(2000),
    });
    if (!response.ok) {
      await response.body?.cancel();
      return { probe_status: "http_error", probe_http_status: response.status };
    }
    const reader = response.body?.getReader();
    if (!reader) return { probe_status: "empty" };
    const decoder = new TextDecoder();
    let bytes = 0;
    let text = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > 4096) return { probe_status: "oversize" };
        text += decoder.decode(value, { stream: true });
      }
      text += decoder.decode();
      const facts = parseNetworkTrace(text);
      return { probe_status: facts.worker_probe_ip ? "ok" : "invalid", ...facts };
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  } catch {
    return { probe_status: "unavailable" };
  }
}

/** Header facts and a fixed signature only; never log provider messages or arbitrary headers. */
export function gatewayNetworkFacts(response) {
  const ray = response?.headers?.get("cf-ray") || "";
  const validRay = /^[a-f0-9]{16,32}-[A-Z]{3}$/i.test(ray);
  const raw = response?.body;
  const error = (Array.isArray(raw) ? raw[0] : raw)?.error;
  const message = typeof error?.message === "string" ? error.message.toLowerCase() : "";
  const statuses = new Set(["INTERNAL", "UNAVAILABLE", "FAILED_PRECONDITION", "INVALID_ARGUMENT",
    "RESOURCE_EXHAUSTED", "PERMISSION_DENIED"]);
  return {
    gateway_cf_ray: validRay ? ray : null,
    gateway_colo: validRay ? ray.split("-").at(-1).toUpperCase() : null,
    provider_error_status: statuses.has(error?.status) ? error.status : null,
    provider_error_signature: message.includes("user location is not supported for the api use")
      ? "unsupported_location" : error?.status === "INTERNAL" && message === "internal error encountered."
        ? "google_internal" : message.includes("high demand") ? "high_demand" : null,
  };
}
