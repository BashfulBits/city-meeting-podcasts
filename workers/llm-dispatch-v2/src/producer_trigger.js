/**
 * review/58 WP6 part B: fire the LLM producer workflows from this Worker's per-minute cron.
 *
 * GitHub drops most scheduled runs of the producer workflows; Cloudflare's cron does not. On the
 * minutes in config/producer_triggers.yml (compiled to producer_triggers.json) this module sends
 * one `POST /repos/{owner}/{repo}/dispatches` per due workflow, and the workflow's
 * `repository_dispatch` trigger starts it. Each workflow keeps its GitHub `schedule:` as a
 * fallback that skips itself when a dispatched run already ran.
 *
 * Stateless by design: what to fire is decided from the cron event's scheduledTime alone (UTC
 * minute and hour), so there is no Durable Object read, write, row or alarm. A minute Cloudflare
 * never fires is a trigger skipped; the GitHub schedule fallback covers it. A late tick keeps its
 * scheduledTime and fires normally.
 *
 * Isolation from the dispatch tick: runProducerTriggers never throws and never rejects; index.js
 * hands it to ctx.waitUntil without awaiting it, so the claim never waits on GitHub. Triggers run
 * one at a time (one extra connection at most), each bounded by a short timeout, and at most
 * max_triggers_per_tick per invocation. Trigger minutes are never cleanup minutes (the compiler
 * enforces it), so the extra subrequests never coincide with cleanup's B2 deletes.
 *
 * Off unless configured: no GITHUB_DISPATCH_TOKEN secret, no GITHUB_DISPATCH_REPOSITORY var, or
 * PRODUCER_TRIGGER_ENABLED set to 0 makes it a silent no-op. The token is never logged.
 */

import PRODUCER_TRIGGERS from "./producer_triggers.json" with { type: "json" };

const GITHUB_API = "https://api.github.com";
const USER_AGENT = "citypods-llm-dispatch-v2-producer-trigger";
const REPOSITORY = /^[A-Za-z0-9-]{1,39}\/[A-Za-z0-9._-]{1,100}$/;
const DISABLED_VALUES = new Set(["0", "false", "off", "no"]);
const MAX_MESSAGE_CHARS = 200;

/** Entries due on the tick whose scheduled time is `scheduledTime` (ms since epoch, UTC). */
export function dueTriggers(scheduledTime, table = PRODUCER_TRIGGERS) {
  if (!Number.isFinite(scheduledTime)) return [];
  const when = new Date(scheduledTime);
  const minute = when.getUTCMinutes();
  const hour = when.getUTCHours();
  const cap = Math.max(0, Math.trunc(Number(table.max_triggers_per_tick) || 0));
  const seen = new Set();
  const due = [];
  for (const entry of table.triggers || []) {
    if (entry.minute !== minute || !entry.hours.includes(hour)) continue;
    if (seen.has(entry.event_type)) continue; // never the same workflow twice in one tick
    seen.add(entry.event_type);
    due.push(entry);
  }
  return due.slice(0, cap);
}

/** The settings a trigger needs, or null when it is not configured or switched off. */
export function triggerSettings(env) {
  const token = typeof env?.GITHUB_DISPATCH_TOKEN === "string" ? env.GITHUB_DISPATCH_TOKEN.trim() : "";
  if (!token) return null;
  const flag = String(env.PRODUCER_TRIGGER_ENABLED ?? "1").trim().toLowerCase();
  if (DISABLED_VALUES.has(flag)) return null;
  const repository = String(env.GITHUB_DISPATCH_REPOSITORY ?? "").trim();
  if (!REPOSITORY.test(repository)) return null;
  return { token, repository };
}

function safeMessage(text) {
  try {
    const message = JSON.parse(text)?.message;
    return typeof message === "string" ? message.slice(0, MAX_MESSAGE_CHARS) : null;
  } catch {
    return null;
  }
}

async function fireOne(entry, scheduledTime, settings, { fetchImpl, now, timeoutMs }) {
  const started = now();
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve({ timedOut: true });
    }, timeoutMs);
  });
  const record = {
    event: "producer_trigger",
    event_type: entry.event_type,
    scheduled_time: new Date(scheduledTime).toISOString(),
    status: null,
    ok: false,
    duration_ms: 0,
  };
  try {
    const attempt = (async () => {
      const response = await fetchImpl(
        `${GITHUB_API}/repos/${settings.repository}/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${settings.token}`,
            Accept: "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
          },
          body: JSON.stringify({
            event_type: entry.event_type,
            client_payload: { scheduled_time: record.scheduled_time },
          }),
          signal: controller.signal,
        },
      );
      // 204 has no body. On an error keep only GitHub's `message`, never the rest of the body.
      const text = response.status === 204 ? "" : await response.text();
      return { response, text };
    })();
    // A fetch that ignores the abort signal still cannot hold this past the deadline.
    attempt.catch(() => {});
    const outcome = await Promise.race([attempt, deadline]);
    if (outcome.timedOut) {
      record.error = "timeout";
    } else {
      record.status = outcome.response.status;
      record.ok = outcome.response.status === 204;
      if (!record.ok) {
        const message = safeMessage(outcome.text);
        if (message) record.message = message;
      }
    }
  } catch (err) {
    record.error = err?.name === "AbortError" ? "timeout" : String(err?.name || "error");
  } finally {
    clearTimeout(timer);
  }
  record.duration_ms = Math.max(0, now() - started);
  (record.ok ? console.info : console.warn)(JSON.stringify(record));
  return record;
}

/**
 * Fire every producer trigger due on this tick. Never throws; resolves to the log records (empty
 * when nothing is due or the trigger is not configured).
 */
export async function runProducerTriggers(
  env,
  scheduledTime,
  { fetchImpl = (...args) => fetch(...args), now = Date.now, table = PRODUCER_TRIGGERS } = {},
) {
  try {
    const due = dueTriggers(scheduledTime, table);
    if (due.length === 0) return [];
    const settings = triggerSettings(env);
    if (!settings) return [];
    const timeoutMs = Math.max(1, Number(table.timeout_seconds) || 5) * 1000;
    const records = [];
    for (const entry of due) {
      records.push(await fireOne(entry, scheduledTime, settings, { fetchImpl, now, timeoutMs }));
    }
    return records;
  } catch (err) {
    try {
      console.error(JSON.stringify({
        event: "producer_trigger_error",
        error: String(err?.name || "error"),
      }));
    } catch {
      // Logging must not be the thing that throws.
    }
    return [];
  }
}
