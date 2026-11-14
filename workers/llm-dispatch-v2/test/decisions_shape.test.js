// review/58 WP9: the `decisions` API shape (Google's Gemini Decision 1, POST /v1beta/decisions), the
// judge:anchor model after BeatAPI retired free JEV. The fixtures follow the measured request and
// response shapes only; they carry no transcript content.
import test from "node:test";
import assert from "node:assert/strict";
import {
  apiShapeFor,
  decisionKeyMap,
  DECISIONS_EMPTY_STATE_INPUT,
} from "../src/api_shapes.js";
import {
  classifyDecisions400, classifyProviderFailure, DECISIONS_SAFETY_CODES, GOOGLE_LOCATION_RULE_ID,
} from "../src/classify.js";
import { resolveProviderCredentials } from "../src/gateway.js";
import { attemptProviderCall } from "../src/index.js";

const GEMINI = {
  api_base: "https://generativelanguage.googleapis.com/v1beta/openai",
  ai_gateway_slug: "google-ai-studio",
  ai_gateway_chat_path: "/v1beta/openai/chat/completions",
  chat_path: "/chat/completions",
  accounts: [
    { id: "project_primary", api_key_env: "GEMINI_API_KEY" },
    { id: "project_secondary", api_key_env: "GEMINI_API_KEY_SECONDARY" },
  ],
};
const LIMITS = { providers: { gemini: GEMINI } };
const DECISION = {
  route_id: "gemini_decision_1_primary",
  provider: "gemini",
  upstream_model: "gemini-decision-1",
  account_id: "project_primary",
  api_shape: "decisions",
  request_path: "/v1beta/decisions",
  hard_input_ceiling: 45000,
};
const GEMINI_CHAT = {
  route_id: "gemini_flash_primary",
  provider: "gemini",
  upstream_model: "gemini-flash",
  account_id: "project_primary",
};

// The producer payload, exactly as the JEV backend builds it (keys carry colons, noul has criteria).
const PAYLOAD = {
  systemone: {
    state: {},
    questions: {
      "s1:supported": { type: "noul", instructions: "Q1", criteria: {} },
      "s1:grade": { type: "score", instructions: "Q2", criteria: ["low", "mid", "high"] },
      "s2:choose": { type: "choice", instructions: "Q3", criteria: { a: "A", b: "B" } },
    },
  },
};

function wireReply(payload, answersByKey, usage = { total_input_tokens: 21000, total_output_tokens: 0 }) {
  const { toWire } = decisionKeyMap(payload.systemone.questions);
  return {
    answers: Object.fromEntries(Object.entries(answersByKey).map(([key, answer]) => [toWire.get(key), answer])),
    usage: { ...usage, input_tokens_by_modality: [], total_tokens: usage.total_input_tokens },
  };
}

const ANSWERS = {
  "s1:supported": { type: "noul", noul: 0.91, confidence: 0.8 },
  "s1:grade": { type: "score", score: "high", legend: ["low", "mid", "high"], probabilities: [0.1, 0.2, 0.7], confidence: 0.7 },
  "s2:choose": { type: "choice", choice: "a", probabilities: { a: 0.6, b: 0.4 }, confidence: 0.6 },
};

test("decisions builds {model, input, questions}: keys mapped, noul criteria dropped", () => {
  const shape = apiShapeFor(DECISION);
  const req = shape.buildRequest({ ...PAYLOAD, max_tokens: 9, messages: [] }, DECISION);
  assert.deepEqual(Object.keys(req).sort(), ["input", "model", "questions"]);
  assert.equal(req.model, "gemini-decision-1");
  assert.equal(req.input, DECISIONS_EMPTY_STATE_INPUT, "an empty state still sends a non-empty input");
  for (const key of Object.keys(req.questions)) assert.match(key, /^[A-Za-z0-9_-]{1,64}$/);
  const byType = Object.fromEntries(Object.values(req.questions).map((q) => [q.type, q]));
  assert.equal("criteria" in byType.noul, false);
  assert.deepEqual(byType.score.criteria, ["low", "mid", "high"]);
  assert.deepEqual(byType.choice.criteria, { a: "A", b: "B" });
  assert.equal("criteria" in PAYLOAD.systemone.questions["s1:supported"], true, "the payload is not mutated");

  const withState = shape.buildRequest({ systemone: { state: { text: "x" }, questions: { q: { type: "noul", instructions: "i" } } } }, DECISION);
  assert.equal(withState.input, JSON.stringify({ text: "x" }));
  assert.equal(shape.buildRequest({ systemone: { state: "  ", questions: {} } }, DECISION).input, DECISIONS_EMPTY_STATE_INPUT);
  assert.throws(() => shape.buildRequest({ messages: [] }, DECISION), /api_shape decisions/);
});

test("the key map is reversible and collision-free, including against keys sent unchanged", () => {
  const keys = ["a:b", "a_b-0", "a_b", "a:b:", "x".repeat(80), "x".repeat(80) + ":", "ok-key", "é"];
  const { toWire, fromWire } = decisionKeyMap(Object.fromEntries(keys.map((k) => [k, {}])));
  const wires = keys.map((k) => toWire.get(k));
  assert.equal(new Set(wires).size, keys.length, "every key gets its own wire key");
  for (const wire of wires) assert.match(wire, /^[A-Za-z0-9_-]{1,64}$/);
  for (const key of keys) assert.equal(fromWire.get(toWire.get(key)), key);
  assert.equal(toWire.get("a_b-0"), "a_b-0", "a valid key is sent unchanged");
  assert.notEqual(toWire.get("a:b"), "a_b-0", "a mapped key never takes a valid key's name");
  assert.equal(toWire.get("ok-key"), "ok-key");
  // Deterministic: the reply is mapped back from the payload alone.
  assert.deepEqual([...decisionKeyMap(Object.fromEntries(keys.map((k) => [k, {}]))).toWire], [...toWire]);
});

test("the reply is mapped back to the asked keys; unknown wire keys are dropped", () => {
  const shape = apiShapeFor(DECISION);
  const reply = wireReply(PAYLOAD, ANSWERS);
  reply.answers["not-asked"] = { type: "noul", noul: 0.5, confidence: 0.5 };
  const normalized = shape.normalizeReply(PAYLOAD, reply);
  assert.deepEqual(normalized.answers, ANSWERS);
  assert.equal(shape.replyProblem(PAYLOAD, normalized), null);
  assert.deepEqual(shape.observedTokens(normalized), { input: 21000, output: 0 });
  assert.equal(shape.lengthTruncated(normalized), false);
  assert.equal(shape.emptyCompletion(200, normalized), false);
  assert.equal(shape.emptyCompletion(200, { usage: {} }), true);
  assert.equal(shape.normalizeReply(PAYLOAD, { error: { message: "x" } }).error.message, "x", "non-answer bodies pass through");
});

test("a reply missing any asked answer is a reply problem", () => {
  const shape = apiShapeFor(DECISION);
  const { "s2:choose": _missing, ...partial } = ANSWERS;
  const normalized = shape.normalizeReply(PAYLOAD, wireReply(PAYLOAD, partial));
  assert.equal(shape.replyProblem(PAYLOAD, normalized), "structured_output_empty");
});

test("request_path resolves to /v1beta/decisions through AI Gateway and directly", () => {
  const gatewayEnv = { GEMINI_API_KEY: "k", CLOUDFLARE_ACCOUNT_ID: "acct", AI_GATEWAY_ID: "gw" };
  assert.equal(
    resolveProviderCredentials(gatewayEnv, DECISION, LIMITS).url,
    "https://gateway.ai.cloudflare.com/v1/acct/gw/google-ai-studio/v1beta/decisions"
  );
  assert.equal(
    resolveProviderCredentials({ GEMINI_API_KEY: "k" }, DECISION, LIMITS).url,
    "https://generativelanguage.googleapis.com/v1beta/decisions"
  );
  assert.equal(
    resolveProviderCredentials({ GEMINI_API_KEY: "k" }, GEMINI_CHAT, LIMITS).url,
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "chat routes on the same provider are unchanged"
  );
});

const err = (fields) => ({ error: { code: 400, status: "INVALID_ARGUMENT", ...fields } });

test("decisions 400s: safety terminal for the job, limits and keys are the route's", () => {
  for (const code of DECISIONS_SAFETY_CODES) {
    const cls = classifyProviderFailure({ status: 400, body: { error: { code, message: "blocked" } }, headers: {}, route: DECISION });
    assert.equal(cls.failure_class, "request_defect", code);
    assert.equal(cls.rule_id, "decisions-safety-block");
  }
  const cases = [
    [err({ message: "Quota exceeded for metric: input tokens per minute" }), "own_tpm"],
    [err({ message: "Quota exceeded for metric: requests per minute" }), "own_rpm"],
    [err({ message: "Quota exceeded: requests per day" }), "own_rpd"],
    [err({ status: "RESOURCE_EXHAUSTED", message: "Resource has been exhausted" }), "own_rpm"],
    [err({ message: "The input token count exceeds the maximum number of tokens allowed" }), "route_input_limit"],
    [err({ message: "A request may have at most 1000 questions" }), "route_input_limit"],
    [err({ message: "API key not valid. Please pass a valid API key." }), "route_unavailable"],
    [err({ message: "Model gemini-decision-1 is not available to this key" }), "route_unavailable"],
  ];
  for (const [body, expected] of cases) {
    const cls = classifyProviderFailure({ status: 400, body, headers: {}, route: DECISION });
    assert.equal(cls.failure_class, expected, body.error.message);
    assert.notEqual(cls.failure_class, "request_defect");
  }
  assert.equal(classifyDecisions400(err({ message: "API key not valid" })).scope, "account");
  assert.equal(classifyDecisions400(err({ message: "Model is not available to this key" })).scope, "route");
  // An unrecognised 400 (a malformed question) stays this job's defect; other shapes are untouched.
  assert.equal(classifyDecisions400(err({ message: "criteria must have at least 2 options" })), null);
  assert.equal(
    classifyProviderFailure({ status: 400, body: err({ message: "criteria must have at least 2 options" }), headers: {}, route: DECISION }).failure_class,
    "request_defect"
  );
  assert.notEqual(
    classifyProviderFailure({ status: 400, body: { error: { code: "safety" } }, headers: {}, route: GEMINI_CHAT }).rule_id,
    "decisions-safety-block"
  );
});

// Issue #2672: the exact bodies Google returned on 2026-11-13 to every request the AI Gateway sent
// from its HKG colo (AI Gateway logs; no other colo got them).
const LOCATION_DECISIONS = {
  error: { message: "User location is not supported for the API use.", code: "invalid_request" },
};
const LOCATION_CHAT = [{
  error: { code: 400, message: "User location is not supported for the API use.", status: "FAILED_PRECONDITION" },
}];

test("a location refusal is retryable upstream capacity, not a withdrawn model or a job defect", () => {
  for (const [route, body] of [[DECISION, LOCATION_DECISIONS], [GEMINI_CHAT, LOCATION_CHAT]]) {
    const cls = classifyProviderFailure({ status: 400, body, headers: {}, route });
    assert.deepEqual(
      [cls.failure_class, cls.rule_id, cls.scope],
      ["upstream_capacity", GOOGLE_LOCATION_RULE_ID, "route"],
      route.route_id
    );
  }
  assert.equal(classifyDecisions400(LOCATION_DECISIONS).rule_id, GOOGLE_LOCATION_RULE_ID);
  // Kept at 400: it is not a quota, so it must not take the 429 pacing path either.
  assert.equal(apiShapeFor(DECISION).normalizeStatus(400, LOCATION_DECISIONS), 400);
  // A genuine "model not available / not supported" answer still stands the route down.
  for (const message of [
    "Model gemini-decision-1 is not available to this key",
    "Model gemini-decision-1 is not supported for this API version",
    "Permission denied on model gemini-decision-1",
  ]) {
    const cls = classifyProviderFailure({ status: 400, body: err({ message }), headers: {}, route: DECISION });
    assert.deepEqual([cls.failure_class, cls.rule_id], ["route_unavailable", "decisions-model-unavailable"], message);
  }
  // Only Google's routes: another provider's 400 with these words keeps its existing reading.
  const other = classifyProviderFailure({
    status: 400, body: LOCATION_DECISIONS, headers: {}, route: { route_id: "x", provider: "openrouter" },
  });
  assert.equal(other.failure_class, "request_defect");
});

test("a quota 400 is reported as 429 so it takes the rate path; other 400s keep their status", () => {
  const shape = apiShapeFor(DECISION);
  assert.equal(shape.normalizeStatus(400, err({ message: "Quota exceeded: tokens per minute" })), 429);
  assert.equal(shape.normalizeStatus(400, { error: { code: "safety", message: "blocked" } }), 400);
  assert.equal(shape.normalizeStatus(400, err({ message: "Model is not available to this key" })), 400);
  assert.equal(shape.normalizeStatus(503, {}), 503);
  const cls = classifyProviderFailure({ status: 429, body: err({ message: "Quota exceeded: tokens per minute" }), headers: {}, route: DECISION });
  assert.equal(cls.failure_class, "own_tpm");
});

async function attemptWith(reply) {
  const realFetch = globalThis.fetch;
  const sent = [];
  globalThis.fetch = async (url, init) => {
    sent.push({ url, headers: init.headers, body: JSON.parse(init.body) });
    return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { "content-type": "application/json" } });
  };
  try {
    const stored = [];
    const out = await attemptProviderCall({
      env: { GEMINI_API_KEY: "test-key" },
      b2: { putJson: async (key, value) => stored.push({ key, value }) },
      route: DECISION,
      dispatchLimits: LIMITS,
      job: {
        id: "job-1",
        lease_token: "lease",
        not_before_at: 0,
        lease_expires_at: Date.now() + 600_000,
        input_token_estimate: 20000,
        payload: PAYLOAD,
        purpose: "judge:anchor",
      },
      attemptId: "attempt-1",
      idempotencyKey: "idem",
      maxResponseMs: 60_000,
    });
    return { out, sent, stored };
  } finally {
    globalThis.fetch = realFetch;
  }
}

test("a decisions call is sent with the Google key header and stored under the asked keys", async () => {
  const { out, sent, stored } = await attemptWith({ status: 200, body: wireReply(PAYLOAD, ANSWERS) });
  assert.equal(sent[0].url, "https://generativelanguage.googleapis.com/v1beta/decisions");
  assert.equal(sent[0].headers["x-goog-api-key"], "test-key");
  assert.equal("authorization" in sent[0].headers, false);
  assert.deepEqual(Object.keys(sent[0].body).sort(), ["input", "model", "questions"]);
  assert.equal(out.result.outcome, "success");
  assert.equal(out.result.observed_input_tokens, 21000);
  assert.equal(stored.length, 1);
  assert.deepEqual(stored[0].value.answers ?? stored[0].value.body?.answers ?? stored[0].value.response?.answers, ANSWERS);
});

test("decisions failures end the attempt the right way", async () => {
  const safety = await attemptWith({ status: 400, body: { error: { code: "prohibited_content", message: "blocked" } } });
  assert.equal(safety.out.result.outcome, "terminal_error");
  assert.equal(safety.out.result.failure_class, "request_defect");

  const unavailable = await attemptWith({ status: 400, body: err({ message: "Model is not available to this key" }) });
  assert.equal(unavailable.out.result.outcome, "retryable_error");
  assert.equal(unavailable.out.result.failure_class, "route_unavailable");

  const location = await attemptWith({ status: 400, body: LOCATION_DECISIONS });
  assert.equal(location.out.result.outcome, "retryable_error");
  assert.equal(location.out.result.failure_class, "upstream_capacity");
  assert.equal(location.out.result.rule_id, GOOGLE_LOCATION_RULE_ID);

  const quota = await attemptWith({ status: 400, body: err({ message: "Quota exceeded: tokens per minute" }) });
  assert.equal(quota.out.retry429, true, "a quota 400 loops through the 429 retry path");

  const missing = await attemptWith({ status: 200, body: wireReply(PAYLOAD, { "s1:supported": ANSWERS["s1:supported"] }) });
  assert.equal(missing.out.result.outcome, "retryable_error");
  assert.equal(missing.stored.length, 0);
});

test("an array-wrapped success is unwrapped and stored under the asked keys", async () => {
  const { out, stored } = await attemptWith({ status: 200, body: [wireReply(PAYLOAD, ANSWERS)] });
  assert.equal(out.result.outcome, "success");
  assert.equal(stored.length, 1);
  assert.deepEqual(stored[0].value.answers ?? stored[0].value.body?.answers ?? stored[0].value.response?.answers, ANSWERS);
});

test("a question key named __proto__ keeps its own answer", () => {
  const shape = apiShapeFor(DECISION);
  // A computed key creates an own property, as JSON.parse does for the reply.
  const payload = { systemone: { state: {}, questions: { ["__proto__"]: { type: "noul", instructions: "i" } } } };
  const answer = { type: "noul", noul: 0.4, confidence: 0.5 };
  const body = JSON.parse(`{"answers": {"__proto__": ${JSON.stringify(answer)}}}`);
  const normalized = shape.normalizeReply(payload, body);
  assert.equal(Object.hasOwn(normalized.answers, "__proto__"), true);
  assert.deepEqual(normalized.answers.__proto__, answer);
  assert.equal(shape.replyProblem(payload, normalized), null);
  assert.equal(shape.replyProblem(payload, { answers: {} }), "structured_output_empty");
});

test("one request above the per-minute token quota is an input limit, not a wait", () => {
  const over = err({ message: "Quota exceeded for input tokens per minute. limit: 45000, requested: 46000" });
  assert.equal(classifyDecisions400(over).failure_class, "route_input_limit");
  assert.equal(apiShapeFor(DECISION).normalizeStatus(400, over), 400);
  const under = err({ message: "Quota exceeded for input tokens per minute. limit: 45000, requested: 30000" });
  assert.equal(classifyDecisions400(under).failure_class, "own_tpm");
});
