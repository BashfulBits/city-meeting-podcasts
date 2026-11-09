/** Cloudflare trace facts describe this probe connection, not Google-facing egress. */
const TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace";

export function networkProbeAllowed(env, jobCount, scheduledTime) {
  if (!Number.isFinite(scheduledTime)) return false;
  const cleanup = Number(env.CLEANUP_INTERVAL_MINUTES || 10);
  if (new Date(scheduledTime).getUTCMinutes() % cleanup === 0) return false;
  // Payload GET + result PUT + initial provider call + authorized 429 retries,
  // plus three diagnostic requests.
  return jobCount * (3 + Number(env.MAX_429_RETRIES ?? 1)) + 3 <= 50;
}

/** Accept literal addresses only; never let a probe reply supply a hostname or URL. */
function normalizedIp(value) {
  if (typeof value !== "string" || value.length > 45) return null;
  if (/^(?:\d{1,3}\.){3}\d{1,3}$/.test(value) && value.split(".")
    .every(part => Number(part) <= 255 && (part === "0" || !part.startsWith("0")))) return value;
  if (!value.includes(":") || !/^[a-f0-9:]+$/i.test(value)) return null;
  try { return new URL(`https://[${value}]/`).hostname.slice(1, -1); }
  catch { return null; }
}

export function parseNetworkTrace(text) {
  const values = Object.fromEntries(text.split("\n").map(line => {
    const at = line.indexOf("=");
    return at < 0 ? ["", ""] : [line.slice(0, at), line.slice(at + 1)];
  }));
  const ip = normalizedIp(values.ip);
  return {
    worker_probe_ip: ip,
    worker_probe_ip_kind: ip === "2a06:98c0:3600::103" ? "worker_placeholder"
      : ip ? "trace_reported" : null,
    worker_probe_colo: /^[A-Z]{3}$/.test(values.colo || "") ? values.colo : null,
    worker_probe_country: /^[A-Z]{2}$/.test(values.loc || "") ? values.loc : null,
  };
}

/** Fixed public diagnostic endpoints only, no redirects, bounded reads and a shared deadline. */
async function readProbe(url, maxBytes, signal, fetchImpl) {
  try {
    const response = await fetchImpl(url, {
      redirect: "error", signal,
      headers: { "user-agent": "Citypods-Network-Diagnostics/1.0" },
    });
    if (!response.ok) {
      await response.body?.cancel();
      return { status: "http_error", http_status: response.status };
    }
    const reader = response.body?.getReader();
    if (!reader) return { status: "empty" };
    const decoder = new TextDecoder();
    let bytes = 0;
    let text = "";
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
        if (bytes > maxBytes) return { status: "oversize" };
        text += decoder.decode(value, { stream: true });
      }
      return { status: "ok", text: text + decoder.decode() };
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  } catch {
    return { status: "unavailable" };
  }
}

export async function observeExecutionNetwork(
  fetchImpl = fetch, signal = AbortSignal.timeout(2000),
) {
  const reply = await readProbe(TRACE_URL, 4096, signal, fetchImpl);
  if (reply.status !== "ok") return {
    probe_status: reply.status,
    ...(reply.http_status == null ? {} : { probe_http_status: reply.http_status }),
  };
  const facts = parseNetworkTrace(reply.text);
  return { probe_status: facts.worker_probe_ip ? "ok" : "invalid", ...facts };
}

export async function observeIndependentNetwork(
  fetchImpl = fetch, signal = AbortSignal.timeout(2000),
) {
  // One deadline covers AWS plus the dependent GeoIP lookup, not two independent waits.
  const reply = await readProbe("https://checkip.global.api.aws/", 256, signal, fetchImpl);
  const base = { external_probe_source: "aws_checkip" };
  if (reply.status !== "ok") return { ...base, external_probe_status: reply.status,
    ...(reply.http_status == null ? {} : { external_probe_http_status: reply.http_status }) };
  const ip = normalizedIp(reply.text.trim());
  if (!ip || ip === "2a06:98c0:3600::103") return { ...base, external_probe_status: "invalid" };
  const facts = { ...base, external_probe_status: "ok", external_probe_ip: ip,
    external_geoip_source: "ip_sb_maxmind" };
  // Query the explicit AWS-observed IP, not IP.SB's view of a separate outbound connection.
  const geo = await readProbe(
    `https://api.ip.sb/geoip/${encodeURIComponent(ip)}`, 4096, signal, fetchImpl,
  );
  if (geo.status !== "ok") return { ...facts, external_geoip_status: geo.status,
    ...(geo.http_status == null ? {} : { external_geoip_http_status: geo.http_status }) };
  let body;
  try { body = JSON.parse(geo.text); }
  catch { return { ...facts, external_geoip_status: "invalid" }; }
  if (!body || normalizedIp(body.ip) !== ip) {
    return { ...facts, external_geoip_status: "ip_mismatch" };
  }
  const organization = body.asn_organization;
  return { ...facts, external_geoip_status: "ok",
    external_country_code: /^[A-Z]{2}$/.test(body.country_code || "") ? body.country_code : null,
    external_asn: Number.isSafeInteger(body.asn) && body.asn > 0 ? body.asn : null,
    external_asn_organization: typeof organization === "string" && organization.length <= 120 &&
      !/[\x00-\x1f\x7f]/.test(organization) ? organization : null,
  };
}

export async function observeBundleNetwork(fetchImpl = fetch) {
  // The caller starts this only once a lane has settled when six lanes run, so at most five
  // inference lanes plus this probe's single connection are open.
  // Serialize all three requests under one deadline; never compete for a seventh connection.
  const signal = AbortSignal.timeout(2000);
  // Prioritize independent evidence; a slow Cloudflare trace must not consume its deadline.
  const external = await observeIndependentNetwork(fetchImpl, signal);
  const trace = await observeExecutionNetwork(fetchImpl, signal);
  return { ...trace, ...external };
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
      ? "unsupported_location"
      : error?.status === "INTERNAL" && message === "internal error encountered."
        ? "google_internal" : message.includes("high demand") ? "high_demand" : null,
  };
}
