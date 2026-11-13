import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import PRODUCER_TRIGGERS from "../src/producer_triggers.json" with { type: "json" };
import DISPATCH_TUNING from "../src/dispatch_tuning.json" with { type: "json" };
import { dueTriggers, runProducerTriggers, triggerSettings } from "../src/producer_trigger.js";

const TOKEN = "github_pat_TEST_ONLY_not_a_real_token";
const REPO = "example-owner/example-repo";
const ENV = { GITHUB_DISPATCH_TOKEN: TOKEN, GITHUB_DISPATCH_REPOSITORY: REPO };
const DAY = Date.UTC(2026, 10, 12); // 2026-11-12T00:00:00Z
const at = (hour, minute, dayOffset = 0) => DAY + dayOffset * 86_400_000 + (hour * 60 + minute) * 60_000;

function captureConsole(fn) {
  const lines = [];
  const saved = { info: console.info, warn: console.warn, error: console.error, log: console.log };
  for (const level of Object.keys(saved)) console[level] = (...args) => lines.push(args.join(" "));
  return Promise.resolve()
    .then(fn)
    .finally(() => Object.assign(console, saved))
    .then((value) => ({ value, lines }));
}

function recordingFetch(status = 204, body = "") {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    return new Response(status === 204 ? null : body, { status });
  };
  return { calls, impl };
}

// ---- schedule matching (stateless, from scheduledTime alone) -----------------------------------

test("each table entry fires exactly on its minutes and hours across a simulated UTC day", () => {
  const fired = new Map();
  for (let minuteOfDay = 0; minuteOfDay < 24 * 60; minuteOfDay += 1) {
    const due = dueTriggers(at(Math.floor(minuteOfDay / 60), minuteOfDay % 60));
    const names = due.map((entry) => entry.event_type);
    assert.equal(new Set(names).size, names.length, "no workflow twice in one tick");
    for (const entry of due) {
      if (!fired.has(entry.event_type)) fired.set(entry.event_type, []);
      fired.get(entry.event_type).push(minuteOfDay);
    }
  }
  const expected = {
    "llm-deferred-sweep": Array.from({ length: 24 }, (_, h) => h * 60 + 52),
    "chapter-agenda": Array.from({ length: 12 }, (_, i) => i * 120 + 3),
    "chapter-locator": Array.from({ length: 12 }, (_, i) => (2 * i + 1) * 60 + 13),
    tag: Array.from({ length: 12 }, (_, i) => i * 120 + 14),
    judge: Array.from({ length: 12 }, (_, i) => i * 120 + 33),
    moments: Array.from({ length: 12 }, (_, i) => (2 * i + 1) * 60 + 23),
  };
  assert.deepEqual(Object.fromEntries(fired), expected);
});

test("trigger minutes never coincide with a cleanup tick", () => {
  const interval = DISPATCH_TUNING.values.CLEANUP_INTERVAL_MINUTES;
  for (const entry of PRODUCER_TRIGGERS.triggers) {
    assert.notEqual(entry.minute % interval, 0, `${entry.event_type} at :${entry.minute}`);
  }
});

test("UTC boundaries: midnight and day rollover use UTC hours, not local time", () => {
  assert.deepEqual(dueTriggers(at(0, 3)).map((e) => e.event_type), ["chapter-agenda"]);
  assert.deepEqual(dueTriggers(at(0, 14)).map((e) => e.event_type), ["tag"]);
  assert.deepEqual(dueTriggers(at(23, 13)).map((e) => e.event_type), ["chapter-locator"]);
  assert.deepEqual(dueTriggers(at(23, 52)).map((e) => e.event_type), ["llm-deferred-sweep"]);
  // Next day, same minutes: the table is purely periodic.
  assert.deepEqual(dueTriggers(at(0, 3, 1)).map((e) => e.event_type), ["chapter-agenda"]);
  // Odd hour: agenda, tag and judge are even-hour only.
  assert.deepEqual(dueTriggers(at(1, 3)), []);
  assert.deepEqual(dueTriggers(at(1, 33)), []);
  // Tag every two hours on even hours: 02:14 and 04:14 fire, 03:14 does not.
  assert.equal(dueTriggers(at(4, 14)).length, 1);
  assert.equal(dueTriggers(at(2, 14)).length, 1);
  assert.equal(dueTriggers(at(3, 14)).length, 0);
  // Moments every two hours on odd hours at :23.
  assert.deepEqual(dueTriggers(at(1, 23)).map((e) => e.event_type), ["moments"]);
  assert.equal(dueTriggers(at(2, 23)).length, 0);
});

test("seconds inside the minute do not matter; a missing scheduledTime fires nothing", () => {
  assert.equal(dueTriggers(at(0, 52) + 59_000).length, 1);
  assert.deepEqual(dueTriggers(undefined), []);
  assert.deepEqual(dueTriggers(Number.NaN), []);
});

test("per-tick cap and duplicate event types", () => {
  const table = {
    max_triggers_per_tick: 3,
    timeout_seconds: 5,
    triggers: ["a", "b", "c", "d", "a"].map((event_type) => ({ event_type, minute: 7, hours: [5] })),
  };
  assert.deepEqual(dueTriggers(at(5, 7), table).map((e) => e.event_type), ["a", "b", "c"]);
  assert.deepEqual(dueTriggers(at(5, 7), { ...table, max_triggers_per_tick: 1 }).length, 1);
  assert.equal(dueTriggers(at(6, 7), table).length, 0);
});

// ---- configuration --------------------------------------------------------------------------

test("no token, empty token, bad repository or the kill switch make it a silent no-op", async () => {
  for (const env of [
    {},
    { ...ENV, GITHUB_DISPATCH_TOKEN: "" },
    { ...ENV, GITHUB_DISPATCH_TOKEN: "   " },
    { ...ENV, GITHUB_DISPATCH_REPOSITORY: "" },
    { ...ENV, GITHUB_DISPATCH_REPOSITORY: "owner/repo/../other" },
    { ...ENV, PRODUCER_TRIGGER_ENABLED: "0" },
    { ...ENV, PRODUCER_TRIGGER_ENABLED: "false" },
    { ...ENV, PRODUCER_TRIGGER_ENABLED: " OFF " },
  ]) {
    assert.equal(triggerSettings(env), null);
    const { calls, impl } = recordingFetch();
    const { value, lines } = await captureConsole(() =>
      runProducerTriggers(env, at(0, 52), { fetchImpl: impl }));
    assert.deepEqual(value, []);
    assert.equal(calls.length, 0);
    assert.deepEqual(lines, [], "silent");
  }
  assert.deepEqual(triggerSettings({ ...ENV, PRODUCER_TRIGGER_ENABLED: "1" }), {
    token: TOKEN, repository: REPO,
  });
  assert.deepEqual(triggerSettings(ENV), { token: TOKEN, repository: REPO }, "default on");
});

test("nothing due: no request and no log, even when configured", async () => {
  const { calls, impl } = recordingFetch();
  const { value, lines } = await captureConsole(() =>
    runProducerTriggers(ENV, at(0, 51), { fetchImpl: impl }));
  assert.deepEqual(value, []);
  assert.equal(calls.length, 0);
  assert.deepEqual(lines, []);
});

// ---- request shape, logging ---------------------------------------------------------------

test("request shape and headers; one structured log line without the token", async () => {
  const { calls, impl } = recordingFetch(204);
  let clock = 1_000;
  const { value, lines } = await captureConsole(() =>
    runProducerTriggers(ENV, at(4, 14), { fetchImpl: impl, now: () => (clock += 120) }));
  assert.equal(calls.length, 1);
  const [{ url, init }] = calls;
  assert.equal(url, `https://api.github.com/repos/${REPO}/dispatches`);
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(init.headers.Accept, "application/vnd.github+json");
  assert.equal(init.headers["X-GitHub-Api-Version"], "2022-11-28");
  assert.match(init.headers["User-Agent"], /^citypods-llm-dispatch-v2/);
  assert.ok(init.signal instanceof AbortSignal);
  assert.deepEqual(JSON.parse(init.body), {
    event_type: "tag",
    client_payload: { scheduled_time: "2026-11-12T04:14:00.000Z" },
  });
  assert.equal(lines.length, 1);
  const log = JSON.parse(lines[0]);
  assert.deepEqual(log, {
    event: "producer_trigger",
    event_type: "tag",
    scheduled_time: "2026-11-12T04:14:00.000Z",
    status: 204,
    ok: true,
    duration_ms: 120,
  });
  assert.deepEqual(value, [log]);
  assert.ok(!lines.join("\n").includes(TOKEN));
});

test("a GitHub error logs status and GitHub's message only, never the body or token", async () => {
  const body = JSON.stringify({
    message: "Resource not accessible by personal access token",
    documentation_url: "https://docs.github.com/rest",
    echo: TOKEN,
  });
  const { impl } = recordingFetch(403, body);
  const { value, lines } = await captureConsole(() =>
    runProducerTriggers(ENV, at(0, 52), { fetchImpl: impl }));
  assert.equal(value[0].status, 403);
  assert.equal(value[0].ok, false);
  assert.equal(value[0].message, "Resource not accessible by personal access token");
  assert.equal(lines.length, 1);
  assert.ok(!lines[0].includes(TOKEN));
  assert.ok(!lines[0].includes("documentation_url"));
});

test("a token echoed in GitHub's message is redacted before logging", async () => {
  const body = JSON.stringify({ message: `Bad credentials: ${TOKEN}` });
  const { impl } = recordingFetch(401, body);
  const { value, lines } = await captureConsole(() =>
    runProducerTriggers(ENV, at(0, 52), { fetchImpl: impl }));
  assert.equal(value[0].message, "Bad credentials: [redacted]");
  assert.ok(!lines.join("\n").includes(TOKEN));
});

test("a non-JSON error body logs no message", async () => {
  const { impl } = recordingFetch(502, "<html>bad gateway</html>");
  const { value } = await captureConsole(() =>
    runProducerTriggers(ENV, at(0, 52), { fetchImpl: impl }));
  assert.equal(value[0].status, 502);
  assert.equal(value[0].message, undefined);
});

test("a thrown fetch is logged and swallowed", async () => {
  const impl = async () => { throw new TypeError(`connect failed ${TOKEN}`); };
  const { value, lines } = await captureConsole(() =>
    runProducerTriggers(ENV, at(0, 52), { fetchImpl: impl }));
  assert.equal(value.length, 1);
  assert.equal(value[0].ok, false);
  assert.equal(value[0].error, "TypeError");
  assert.ok(!lines.join("\n").includes(TOKEN), "the exception message is not logged");
});

test("a hanging GitHub call is cut off at the timeout even if fetch ignores the signal", async () => {
  const table = { ...PRODUCER_TRIGGERS, timeout_seconds: 0.05 };
  const impl = () => new Promise(() => {}); // never settles, ignores abort
  const started = Date.now();
  const { value } = await captureConsole(() =>
    runProducerTriggers(ENV, at(0, 52), { fetchImpl: impl, table }));
  assert.ok(Date.now() - started < 2_000);
  assert.equal(value[0].error, "timeout");
  assert.equal(value[0].status, null);
});

test("triggers run one at a time, capped per tick", async () => {
  const table = {
    max_triggers_per_tick: 2,
    timeout_seconds: 5,
    triggers: ["a", "b", "c"].map((event_type) => ({ event_type, minute: 7, hours: [5] })),
  };
  let open = 0;
  let peak = 0;
  const seen = [];
  const impl = async (_url, init) => {
    open += 1;
    peak = Math.max(peak, open);
    seen.push(JSON.parse(init.body).event_type);
    await new Promise((resolve) => setTimeout(resolve, 5));
    open -= 1;
    return new Response(null, { status: 204 });
  };
  await captureConsole(() => runProducerTriggers(ENV, at(5, 7), { fetchImpl: impl, table }));
  assert.deepEqual(seen, ["a", "b"]);
  assert.equal(peak, 1);
});

test("runProducerTriggers never rejects, even on a broken table", async () => {
  const table = { max_triggers_per_tick: 3, triggers: [{ event_type: "a", minute: 7, hours: null }] };
  const { value, lines } = await captureConsole(() => runProducerTriggers(ENV, at(5, 7), { table }));
  assert.deepEqual(value, []);
  assert.deepEqual(lines.map((line) => JSON.parse(line).event), ["producer_trigger_error"]);
});

// ---- isolation from the dispatch tick ------------------------------------------------------

test("the scheduled handler hands the trigger to waitUntil and does not wait on GitHub", async () => {
  const originalFetch = globalThis.fetch;
  const githubCalls = [];
  let releaseGithub;
  const githubGate = new Promise((resolve) => { releaseGithub = resolve; });
  globalThis.fetch = async (url) => {
    if (String(url).startsWith("https://api.github.com/")) {
      githubCalls.push(url);
      await githubGate; // GitHub hangs until the test lets it go
      return new Response(null, { status: 204 });
    }
    throw new Error(`unexpected fetch ${url}`);
  };
  const claims = [];
  const coordinator = {
    claimDispatchWindow: async (now) => {
      claims.push(now);
      return { jobs: [], claim_reason: "queue_empty", claim_diagnostics: {} };
    },
  };
  // Fails validateConfig (no BEARER_TOKEN): the trigger still runs, independent of dispatch.
  const brokenEnv = { ...ENV };
  // Valid enough to reach the claim: a bearer token and B2 credentials.
  const env = {
    ...ENV,
    BEARER_TOKEN: "test-bearer",
    B2_ENDPOINT: "https://s3.us-west-004.backblazeb2.com",
    B2_KEY_ID: "k",
    B2_APP_KEY: "s",
    B2_BUCKET: "b",
    LLM_SCHEDULER: { getByName: () => coordinator },
  };
  try {
    const pending = [];
    const { lines } = await captureConsole(async () => {
      await worker.scheduled({ scheduledTime: at(0, 52) }, env, { waitUntil: (p) => pending.push(p) });
      // The claim settles while the GitHub call is still hanging.
      await Promise.race([
        Promise.all(pending.slice(1)),
        new Promise((_, reject) => setTimeout(() => reject(new Error("claim waited on GitHub")), 1_000)),
      ]);
      assert.equal(claims.length, 1);
      assert.equal(githubCalls.length, 1);
      releaseGithub();
      await Promise.all(pending);

      const before = githubCalls.length;
      const brokenPending = [];
      await worker.scheduled({ scheduledTime: at(0, 52) }, brokenEnv, {
        waitUntil: (p) => brokenPending.push(p),
      });
      await Promise.all(brokenPending);
      assert.equal(githubCalls.length, before + 1);
    });
    assert.ok(!lines.join("\n").includes(TOKEN));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("a failing GitHub call does not stop the claim", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url) => {
    if (String(url).startsWith("https://api.github.com/")) throw new Error("boom");
    throw new Error(`unexpected fetch ${url}`);
  };
  let claimed = 0;
  const env = {
    ...ENV,
    BEARER_TOKEN: "test-bearer",
    B2_ENDPOINT: "https://s3.us-west-004.backblazeb2.com",
    B2_KEY_ID: "k",
    B2_APP_KEY: "s",
    B2_BUCKET: "b",
    LLM_SCHEDULER: {
      getByName: () => ({
        claimDispatchWindow: async () => {
          claimed += 1;
          return { jobs: [], claim_reason: "queue_empty", claim_diagnostics: {} };
        },
      }),
    },
  };
  try {
    const pending = [];
    await captureConsole(async () => {
      await worker.scheduled({ scheduledTime: at(0, 52) }, env, { waitUntil: (p) => pending.push(p) });
      const settled = await Promise.allSettled(pending);
      assert.ok(settled.every((s) => s.status === "fulfilled"));
    });
    assert.equal(claimed, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
