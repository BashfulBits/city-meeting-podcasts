import { emailTemplate } from "./templates.js";

const MAX_BODY_BYTES = 32 * 1024;

class BodyTooLargeError extends Error {}

function response(status, body) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "cache-control": "no-store", "x-content-type-options": "nosniff" } });
}

function value(input, names, max = 500) {
  for (const name of names) {
    const raw = input[name];
    if (typeof raw === "string" && raw.trim()) return raw.trim().slice(0, max);
  }
  return "";
}

function parseCity(cityState) {
  const match = cityState.match(/^(.+?),\s*([A-Za-z]{2})$/);
  return match ? { city: match[1].trim(), state: match[2].toUpperCase() } : null;
}

async function sha256(value) {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function constantTimeEqual(given, expected) {
  if (!expected || !given) return false;
  const [left, right] = await Promise.all([sha256(given), sha256(expected)]);
  let different = left.length ^ right.length;
  for (let i = 0; i < left.length; i += 1) different |= left.charCodeAt(i) ^ right.charCodeAt(i);
  return different === 0;
}

async function constantTimeSecret(request, env) {
  const parts = new URL(request.url).pathname.split("/").filter(Boolean);
  const given = parts.length === 2 && parts[0] === "formspark" ? parts[1] : "";
  return constantTimeEqual(given, String(env.FORMSPARK_WEBHOOK_SECRET || ""));
}

async function readCappedBody(request) {
  const length = Number(request.headers.get("content-length"));
  if (Number.isFinite(length) && length > MAX_BODY_BYTES) throw new BodyTooLargeError();
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY_BYTES) { await reader.cancel(); throw new BodyTooLargeError(); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder().decode(bytes);
}

function base64Url(bytes) {
  let binary = "";
  for (const value of new Uint8Array(bytes)) binary += String.fromCharCode(value);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function githubAppToken(env, fetchImpl) {
  const pem = String(env.GITHUB_APP_PRIVATE_KEY || "").replace(/\\n/g, "\n");
  const raw = pem.replace(/-----[^-]+-----/g, "").replace(/\s/g, "");
  if (!env.GITHUB_APP_ID || !env.GITHUB_APP_INSTALLATION_ID || !raw) throw new Error("GitHub App is not configured");
  const header = base64Url(new TextEncoder().encode(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const now = Math.floor(Date.now() / 1000);
  const payload = base64Url(new TextEncoder().encode(JSON.stringify({ iat: now - 30, exp: now + 540, iss: String(env.GITHUB_APP_ID) })));
  const keyBytes = Uint8Array.from(atob(raw), (char) => char.charCodeAt(0));
  const key = await crypto.subtle.importKey("pkcs8", keyBytes, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
  const signature = base64Url(await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(`${header}.${payload}`)));
  const token = `${header}.${payload}.${signature}`;
  const api = String(env.GITHUB_API_BASE || "https://api.github.com");
  const result = await fetchImpl(`${api}/app/installations/${env.GITHUB_APP_INSTALLATION_ID}/access_tokens`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/vnd.github+json",
      "content-type": "application/json",
      "user-agent": "citymeetings-intake/1.0",
      "x-github-api-version": "2022-11-28",
    },
    body: "{}",
  });
  if (!result.ok) {
    const failure = await result.json().catch(() => ({}));
    const message = typeof failure.message === "string" ? failure.message.slice(0, 300) : "no message";
    const requestId = result.headers.get("x-github-request-id") || "unknown";
    throw new Error(`GitHub App token request failed (${result.status}): ${message}; request ${requestId}`);
  }
  const body = await result.json();
  if (!body.token) throw new Error("GitHub App token response omitted token");
  return body.token;
}

export function issueBody(payload, source = "website", requestMarker = "") {
  return [
    "### City and state", `${payload.city}, ${payload.state}`, "",
    "### Video platform (if known)", payload.provider || "Not sure", "",
    "### Meeting video / feed URL", payload.meetingUrl || "Not supplied", "",
    "### City website", payload.website || "Not supplied", "",
    "### Anything else", payload.notes || "Not supplied", "",
    source === "website"
      ? "Submitted through the website. The requester email is stored privately and is not in this issue."
      : "Submitted through Discord. The canonical research and approval record is this GitHub issue.",
    requestMarker ? `<!-- citypods:r12:intake ${requestMarker} -->` : "",
  ].join("\n");
}

async function findIssueByMarker(marker, token, env, fetchImpl) {
  if (!marker) return null;
  const api = String(env.GITHUB_API_BASE || "https://api.github.com");
  const query = encodeURIComponent(`repo:${env.GITHUB_REPOSITORY} is:issue in:body "citypods:r12:intake ${marker}"`);
  const result = await fetchImpl(`${api}/search/issues?q=${query}`, { headers: { authorization: `Bearer ${token}`, accept: "application/vnd.github+json", "user-agent": "citymeetings-intake/1.0" } });
  if (!result.ok) throw new Error(`GitHub issue reconciliation failed (${result.status})`);
  return (await result.json()).items?.[0] || null;
}

async function createIssue(payload, env, fetchImpl, source = "website", requestMarker = "") {
  const token = await githubAppToken(env, fetchImpl);
  const api = String(env.GITHUB_API_BASE || "https://api.github.com");
  const existing = await findIssueByMarker(requestMarker, token, env, fetchImpl);
  if (existing) return existing;
  const body = issueBody(payload, source, requestMarker);
  const result = await fetchImpl(`${api}/repos/${env.GITHUB_REPOSITORY}/issues`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      accept: "application/vnd.github+json",
      "content-type": "application/json",
      "user-agent": "citymeetings-intake/1.0",
      "x-github-api-version": "2022-11-28",
    },
    body: JSON.stringify({ title: `Add city: ${payload.city}, ${payload.state}`, body, labels: ["add-city", `source:${source}`, "needs:discovery"] }),
  });
  if (!result.ok) throw new Error(`GitHub issue creation failed (${result.status})`);
  return result.json();
}

async function notifyDiscord(issue, env, fetchImpl, source = "website") {
  if (!env.DISCORD_WEBHOOK_URL) return null;
  const webhook = new URL(env.DISCORD_WEBHOOK_URL);
  webhook.searchParams.set("wait", "true");
  const result = await fetchImpl(webhook, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ content: `New ${source} city request: ${issue.html_url}`, allowed_mentions: { parse: [] } }) });
  if (!result.ok) throw new Error(`Discord notification failed (${result.status})`);
  const message = await result.json();
  return message.id || null;
}

async function sendEmail(email, issueUrl, env, fetchImpl) {
  if (!env.RESEND_API_KEY || !env.MAIL_FROM) return;
  const message = emailTemplate("submission_received", { issueUrl }, env.PROJECT_URL);
  const result = await fetchImpl("https://api.resend.com/emails", { method: "POST", headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, "content-type": "application/json" }, body: JSON.stringify({ from: env.MAIL_FROM, to: [email], reply_to: env.MAIL_REPLY_TO || env.MAIL_FROM, subject: message.subject, html: message.html, text: message.text }) });
  if (!result.ok) throw new Error(`Resend acknowledgement failed (${result.status})`);
}

const STATUS_LABELS = {
  evidence_ready: "Research is ready for maintainer review",
  batched_for_review: "The request is in a maintainer-review PR",
  applied: "The request was merged and is being published",
  needs_more_information: "More information is needed",
  research_only: "An unsupported-provider finding was recorded",
  evidence_expired: "The 90-day evidence window expired; fresh research is queued",
};

async function sendLifecycleEmail(email, kind, issueUrl, targetUrl, env, fetchImpl) {
  if (!email || !env.RESEND_API_KEY || !env.MAIL_FROM) return;
  const message = emailTemplate(kind, { issueUrl, prUrl: targetUrl || issueUrl }, env.PROJECT_URL);
  const result = await fetchImpl("https://api.resend.com/emails", {
    method: "POST",
    headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, "content-type": "application/json" },
    body: JSON.stringify({ from: env.MAIL_FROM, to: [email], reply_to: env.MAIL_REPLY_TO || env.MAIL_FROM, subject: message.subject, html: message.html, text: message.text }),
  });
  if (!result.ok) throw new Error(`Resend lifecycle email failed (${result.status})`);
}

async function updateDiscord(issueNumber, messageId, kind, issueUrl, targetUrl, env, fetchImpl) {
  if (!messageId || !env.DISCORD_WEBHOOK_URL) return;
  const webhook = new URL(env.DISCORD_WEBHOOK_URL);
  webhook.pathname = `${webhook.pathname.replace(/\/$/, "")}/messages/${encodeURIComponent(messageId)}`;
  webhook.search = "";
  const link = targetUrl && targetUrl !== issueUrl ? `\n${targetUrl}` : "";
  const result = await fetchImpl(webhook, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ content: `City request #${issueNumber}: ${STATUS_LABELS[kind]}\n${issueUrl}${link}`, allowed_mentions: { parse: [] } }),
  });
  if (!result.ok) throw new Error(`Discord status update failed (${result.status})`);
}

async function handoffStatus(input, env, fetchImpl) {
  if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("invalid lifecycle status payload");
  const issueNumber = Number(input.issue_number);
  const kind = String(input.status || "");
  const issueUrl = String(input.issue_url || "").slice(0, 1000);
  const targetUrl = String(input.target_url || "").slice(0, 1000);
  if (!Number.isInteger(issueNumber) || issueNumber < 1 || !STATUS_LABELS[kind] || !issueUrl.startsWith("https://github.com/")) {
    throw new Error("invalid lifecycle status payload");
  }
  const origin = await env.REQUESTS_DB.prepare(
    "SELECT o.discord_message_id, o.email_notification_key, o.discord_notification_key, r.email FROM request_origins o LEFT JOIN city_requests r ON r.issue_number = o.issue_number WHERE o.issue_number = ?",
  ).bind(issueNumber).first();
  if (!origin) return;
  const key = await sha256(`${kind}|${issueUrl}|${targetUrl}`);
  async function notifyChannel(column, send) {
    if (origin[column] === key) return;
    const pending = `pending:${key}`;
    const claim = await env.REQUESTS_DB.prepare(
      `UPDATE request_origins SET ${column} = ?, updated_at = ? WHERE issue_number = ? AND (${column} IS NULL OR ${column} NOT LIKE 'pending:%') AND ${column} != ?`,
    ).bind(pending, new Date().toISOString(), issueNumber, key).run();
    if (!claim.meta.changes) return;
    try {
      await send();
      await env.REQUESTS_DB.prepare(`UPDATE request_origins SET ${column} = ?, updated_at = ? WHERE issue_number = ? AND ${column} = ?`).bind(key, new Date().toISOString(), issueNumber, pending).run();
    } catch (error) {
      await env.REQUESTS_DB.prepare(`UPDATE request_origins SET ${column} = ?, updated_at = ? WHERE issue_number = ? AND ${column} = ?`).bind(origin[column] || null, new Date().toISOString(), issueNumber, pending).run();
      throw error;
    }
  }
  const updates = [];
  if (origin.email && env.RESEND_API_KEY && env.MAIL_FROM) updates.push(notifyChannel("email_notification_key", () => sendLifecycleEmail(origin.email, kind, issueUrl, targetUrl, env, fetchImpl)));
  if (origin.discord_message_id && env.DISCORD_WEBHOOK_URL) updates.push(notifyChannel("discord_notification_key", () => updateDiscord(issueNumber, origin.discord_message_id, kind, issueUrl, targetUrl, env, fetchImpl)));
  await Promise.all(updates);
}

function hexBytes(value) {
  if (!/^[0-9a-f]+$/i.test(value) || value.length % 2) throw new Error("invalid hex value");
  return Uint8Array.from(value.match(/../g), (byte) => Number.parseInt(byte, 16));
}

async function verifyDiscordSignature(request, rawBody, env) {
  const signature = request.headers.get("x-signature-ed25519") || "";
  const timestamp = request.headers.get("x-signature-timestamp") || "";
  if (!env.DISCORD_PUBLIC_KEY || !signature || !/^\d+$/.test(timestamp)) return false;
  if (Math.abs(Date.now() - Number(timestamp) * 1000) > 5 * 60 * 1000) return false;
  try {
    const key = await crypto.subtle.importKey("raw", hexBytes(env.DISCORD_PUBLIC_KEY), { name: "Ed25519" }, false, ["verify"]);
    return crypto.subtle.verify({ name: "Ed25519" }, key, hexBytes(signature), new TextEncoder().encode(timestamp + rawBody));
  } catch {
    return false;
  }
}

function discordOptions(interaction) {
  const options = {};
  for (const option of interaction.data?.options || []) options[option.name] = String(option.value || "").trim();
  return options;
}

async function editDiscordInteraction(interaction, content, fetchImpl) {
  const url = `https://discord.com/api/v10/webhooks/${encodeURIComponent(interaction.application_id)}/${encodeURIComponent(interaction.token)}/messages/@original`;
  const result = await fetchImpl(url, { method: "PATCH", headers: { "content-type": "application/json" }, body: JSON.stringify({ content, allowed_mentions: { parse: [] } }) });
  if (!result.ok) throw new Error(`Discord interaction response failed (${result.status})`);
}

async function handoffDiscord(interaction, payload, env, fetchImpl) {
  try {
    const issue = await createIssue(payload, env, fetchImpl, "discord");
    const now = new Date().toISOString();
    await env.REQUESTS_DB.prepare("INSERT OR REPLACE INTO request_origins (issue_number, source, created_at, updated_at) VALUES (?, 'discord', ?, ?)").bind(issue.number, now, now).run();
    const messageId = await notifyDiscord(issue, env, fetchImpl, "Discord");
    if (messageId) await env.REQUESTS_DB.prepare("UPDATE request_origins SET discord_message_id = ?, updated_at = ? WHERE issue_number = ?").bind(messageId, new Date().toISOString(), issue.number).run();
    await editDiscordInteraction(interaction, `Request received. Follow the canonical GitHub issue: ${issue.html_url}`, fetchImpl);
  } catch (error) {
    console.error(JSON.stringify({ event: "discord_city_request_failed", error: error instanceof Error ? error.message : "unknown error" }));
    await editDiscordInteraction(interaction, "I couldn't create the city request. Please try again later or use the website form.", fetchImpl);
  }
}

async function handleDiscordInteraction(request, env, fetchImpl, ctx) {
  let rawBody;
  try { rawBody = await readCappedBody(request); } catch (error) { return response(error instanceof BodyTooLargeError ? 413 : 400, { error: "request too large" }); }
  if (!(await verifyDiscordSignature(request, rawBody, env))) return response(401, { error: "invalid Discord signature" });
  let interaction;
  try { interaction = JSON.parse(rawBody); } catch { return response(400, { error: "request must be JSON" }); }
  if (interaction.type === 1) return response(200, { type: 1 });
  if (interaction.type !== 2 || interaction.data?.name !== "request-city") return response(400, { error: "unsupported Discord interaction" });
  const options = discordOptions(interaction);
  const state = String(options.state || "").toUpperCase();
  if (!options.city || !/^[A-Z]{2}$/.test(state)) return response(200, { type: 4, data: { content: "City and a two-letter state are required.", flags: 64, allowed_mentions: { parse: [] } } });
  const payload = { city: options.city.slice(0, 200), state, provider: options.provider || "", meetingUrl: options.source_url || "", website: options.city_website || "", notes: (options.notes || "").slice(0, 4000) };
  const work = handoffDiscord(interaction, payload, env, fetchImpl);
  if (ctx) ctx.waitUntil(work); else await work;
  return response(200, { type: 5, data: { flags: 64 } });
}

async function handoffSubmission(payload, env, fetchImpl) {
  let { email } = payload;
  const fingerprint = await sha256(`${payload.city.toLowerCase()}|${payload.state}|${email}|${payload.meetingUrl.toLowerCase()}`);
  const now = new Date().toISOString();
  let requestMarker = crypto.randomUUID();
  let stage = "deduplicate";
  try {
    const insert = await env.REQUESTS_DB.prepare("INSERT OR IGNORE INTO city_requests (fingerprint, email, city_name, state, payload_json, request_marker, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)").bind(fingerprint, email, payload.city, payload.state, JSON.stringify(payload), requestMarker, now, now).run();
    if (insert.meta.changes === 0) {
      const existing = await env.REQUESTS_DB.prepare("SELECT issue_number, status, payload_json, request_marker FROM city_requests WHERE fingerprint = ?").bind(fingerprint).first();
      if (existing?.issue_number || existing?.status !== "pending" || !existing.payload_json || !existing.request_marker) return { status: 202, body: { duplicate: true, issue_number: existing?.issue_number || null } };
      payload = JSON.parse(existing.payload_json);
      email = payload.email;
      requestMarker = existing.request_marker;
    }
    stage = "create_github_issue";
    const issue = await createIssue(payload, env, fetchImpl, "website", requestMarker);
    stage = "record_github_issue";
    await env.REQUESTS_DB.prepare("UPDATE city_requests SET issue_number = ?, status = 'created', last_error = NULL, updated_at = ? WHERE fingerprint = ?").bind(issue.number, new Date().toISOString(), fingerprint).run();
    stage = "record_origin";
    await env.REQUESTS_DB.prepare("INSERT OR REPLACE INTO request_origins (issue_number, source, created_at, updated_at) VALUES (?, 'website', ?, ?)").bind(issue.number, now, now).run();
    stage = "notify";
    const [discord] = await Promise.allSettled([notifyDiscord(issue, env, fetchImpl), sendEmail(email, issue.html_url, env, fetchImpl)]);
    if (discord.status === "fulfilled" && discord.value) {
      await env.REQUESTS_DB.prepare("UPDATE request_origins SET discord_message_id = ?, updated_at = ? WHERE issue_number = ?").bind(discord.value, new Date().toISOString(), issue.number).run();
    }
    return { status: 201, body: { issue_number: issue.number, issue_url: issue.html_url } };
  } catch (error) {
    await env.REQUESTS_DB.prepare("UPDATE city_requests SET status = 'pending', last_error = ?, updated_at = ? WHERE fingerprint = ?").bind(error instanceof Error ? error.message.slice(0, 500) : "unknown error", new Date().toISOString(), fingerprint).run();
    console.error(JSON.stringify({
      event: "city_request_handoff_failed",
      stage,
      error: error instanceof Error ? error.message : "unknown error",
    }));
    return { status: 502, body: { error: "request handoff failed; please try again later" } };
  }
}

async function retryPendingSubmissions(env, fetchImpl) {
  const rows = await env.REQUESTS_DB.prepare("SELECT payload_json FROM city_requests WHERE status = 'pending' ORDER BY updated_at LIMIT 10").all();
  for (const row of rows.results || []) {
    try { await handoffSubmission(JSON.parse(row.payload_json), env, fetchImpl); } catch (error) { console.error(JSON.stringify({ event: "city_request_retry_failed", error: error instanceof Error ? error.message : "unknown error" })); }
  }
}

export async function handleRequest(request, env, fetchImpl = fetch, ctx = null) {
  if (request.method !== "POST") return response(405, { error: "method not allowed" });
  const path = new URL(request.url).pathname;
  if (path === "/discord/interactions") return handleDiscordInteraction(request, env, fetchImpl, ctx);
  if (path.startsWith("/status/")) {
    const parts = path.split("/").filter(Boolean);
    const given = parts.length === 2 && parts[0] === "status" ? parts[1] : "";
    const expected = String(env.STATUS_WEBHOOK_SECRET || "");
    if (!(await constantTimeEqual(given, expected))) return response(401, { error: "unauthorized" });
    let statusInput;
    try { statusInput = JSON.parse(await readCappedBody(request)); } catch (error) { return response(error instanceof BodyTooLargeError ? 413 : 400, { error: error instanceof BodyTooLargeError ? "request too large" : "request must be JSON" }); }
    const work = handoffStatus(statusInput, env, fetchImpl);
    if (ctx) {
      ctx.waitUntil(work);
      return response(202, { accepted: true });
    }
    await work;
    return response(200, { notified: true });
  }
  if (!(await constantTimeSecret(request, env))) return response(401, { error: "unauthorized" });
  let input;
  try { input = JSON.parse(await readCappedBody(request)); } catch (error) { return response(error instanceof BodyTooLargeError ? 413 : 400, { error: error instanceof BodyTooLargeError ? "request too large" : "request must be JSON" }); }
  if (!input || typeof input !== "object" || Array.isArray(input)) return response(400, { error: "request must be an object" });
  const cityState = value(input, ["city_state", "city and state", "city"]);
  const parsed = parseCity(cityState);
  const email = value(input, ["email", "requester_email"], 254).toLowerCase();
  if (!parsed || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return response(400, { error: "city_state and email are required" });
  const payload = { ...parsed, email, provider: value(input, ["provider", "video_platform"]), meetingUrl: value(input, ["source_url", "meeting_url"]), website: value(input, ["city_website", "website"]), notes: value(input, ["notes", "anything_else"], 4000) };
  const handoff = handoffSubmission(payload, env, fetchImpl);
  if (ctx) {
    ctx.waitUntil(handoff);
    return response(202, { accepted: true });
  }
  const result = await handoff;
  return response(result.status, result.body);
}

export default {
  fetch(request, env, ctx) {
    return handleRequest(request, env, fetch, ctx);
  },
  scheduled(_event, env, ctx) {
    ctx.waitUntil(retryPendingSubmissions(env, fetch));
  },
};
