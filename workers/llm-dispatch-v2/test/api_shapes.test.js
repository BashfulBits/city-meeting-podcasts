// review/53 PR1: the `systemone` API shape (BeatAPI's JEV judge endpoint), `request_path`, and the
// oversize 503 rule. The `chat` shape must stay byte-identical to the pre-existing functions.
import test from "node:test";
import assert from "node:assert/strict";
import { apiShapeFor, API_SHAPES } from "../src/api_shapes.js";
import { classifyProviderFailure } from "../src/classify.js";
import {
  observedTokens,
  resolveProviderCredentials,
  upstreamEmptyCompletion,
  upstreamRequestForRoute,
} from "../src/gateway.js";
import { attemptProviderCall } from "../src/index.js";

const BEATAPI = {
  api_base: "https://api.beatapi.io/v1",
  chat_path: "/chat/completions",
  ai_gateway_slug: "custom-beatapi",
  ai_gateway_chat_path: "/chat/completions",
  accounts: [{ id: "primary", api_key_env: "BEATAPI_API_KEY" }],
};
const LIMITS = { providers: { beatapi: BEATAPI } };
const JEV = {
  route_id: "beatapi_jev_1_13_free",
  provider: "beatapi",
  upstream_model: "jev-1.13-free",
  account_id: "primary",
  api_shape: "systemone",
  request_path: "/systemone",
  hard_input_ceiling: 58000,
};
const CHAT = { route_id: "beatapi_gpt_6_astra_free", provider: "beatapi", upstream_model: "gpt-6-astra-free", account_id: "primary" };
const JEV_PAYLOAD = {
  systemone: {
    state: { text: "The council approved the zoning change 5-2." },
    questions: { "subj-1:supported": { type: "noul", instructions: "Did the council approve it?", criteria: {} } },
  },
};
// Shape of a real JEV reply (evals/judge/results/2026-09-30-question-types-jev.json).
const JEV_REPLY = {
  answers: { "subj-1:supported": { noul: 0.97, type: "noul" } },
  id: "task_x",
  model: "jev-1.13-free",
  usage: { input_tokens: 9830, output_tokens: 1240 },
};

test("the chat shape is the pre-existing functions, unchanged", () => {
  const chat = apiShapeFor(CHAT);
  const payload = { messages: [{ role: "user", content: "hi" }], max_tokens: 16 };
  assert.deepEqual(chat.buildRequest(payload, CHAT, {}), upstreamRequestForRoute(payload, CHAT, {}));
  const body = { choices: [{ message: { content: "ok" }, finish_reason: "length" }], usage: { prompt_tokens: 3, completion_tokens: 1 } };
  assert.deepEqual(chat.observedTokens(body), observedTokens(body));
  assert.equal(chat.emptyCompletion(200, body), upstreamEmptyCompletion(200, body));
  assert.equal(chat.emptyCompletion(200, { error: { code: 503 } }), true);
  assert.equal(chat.lengthTruncated(body), true);
  assert.equal(chat.replyProblem(payload, body), null, "an unstructured chat job has no reply schema");
  assert.equal(apiShapeFor({}).buildRequest, chat.buildRequest, "absent api_shape is chat");
  assert.deepEqual(API_SHAPES, ["chat", "systemone"]);
  assert.throws(() => apiShapeFor({ route_id: "x", api_shape: "fax" }), /unknown api_shape/);
});

test("systemone builds {model, state, questions} with no chat fields", () => {
  const shape = apiShapeFor(JEV);
  assert.deepEqual(shape.buildRequest({ ...JEV_PAYLOAD, max_tokens: 99, messages: [] }, JEV), {
    model: "jev-1.13-free",
    state: JEV_PAYLOAD.systemone.state,
    questions: JEV_PAYLOAD.systemone.questions,
  });
  assert.throws(() => shape.buildRequest({ messages: [] }, JEV), /payload.systemone.questions/);
});

test("systemone reply checks: answers object, missing-answer share, usage, never length-truncated", () => {
  const shape = apiShapeFor(JEV);
  assert.equal(shape.emptyCompletion(200, JEV_REPLY), false);
  assert.equal(shape.emptyCompletion(200, { choices: [] }), true, "a chat-shaped body is not an answer");
  assert.equal(shape.emptyCompletion(200, { error: { code: "x" } }), true);
  assert.equal(shape.emptyCompletion(503, {}), false, "non-2xx is classified elsewhere");
  assert.deepEqual(shape.observedTokens(JEV_REPLY), { input: 9830, output: 1240 });
  assert.equal(shape.lengthTruncated(JEV_REPLY), false);
  assert.equal(shape.replyProblem(JEV_PAYLOAD, JEV_REPLY), null);
  const many = { systemone: { questions: Object.fromEntries([...Array(20)].map((_, i) => [`q${i}`, {}])) } };
  const answers = (n) => ({ answers: Object.fromEntries([...Array(n)].map((_, i) => [`q${i}`, { noul: 0.5 }])) });
  assert.equal(shape.replyProblem(many, answers(18)), null, "10% missing is tolerated");
  assert.equal(shape.replyProblem(many, answers(17)), "structured_output_empty", "15% missing is not");
});

test("request_path replaces the chat path, through AI Gateway and directly", () => {
  const gatewayEnv = { BEATAPI_API_KEY: "k", CLOUDFLARE_ACCOUNT_ID: "acct", AI_GATEWAY_ID: "gw" };
  assert.equal(
    resolveProviderCredentials(gatewayEnv, JEV, LIMITS).url,
    "https://gateway.ai.cloudflare.com/v1/acct/gw/custom-beatapi/systemone"
  );
  assert.equal(
    resolveProviderCredentials(gatewayEnv, CHAT, LIMITS).url,
    "https://gateway.ai.cloudflare.com/v1/acct/gw/custom-beatapi/chat/completions",
    "routes without request_path are unchanged"
  );
  assert.equal(resolveProviderCredentials({ BEATAPI_API_KEY: "k" }, JEV, LIMITS).url, "https://api.beatapi.io/v1/systemone");
});

test("JEV's processing_failed 503 is an oversize request_defect only near the ceiling", () => {
  const body = { code: "processing_failed", retryable: true };
  const near = classifyProviderFailure({ status: 503, body, headers: {}, route: JEV, inputTokens: 0.9 * 58000 });
  assert.equal(near.failure_class, "request_defect");
  assert.equal(near.rule_id, "systemone-oversize-503");
  const small = classifyProviderFailure({ status: 503, body, headers: {}, route: JEV, inputTokens: 2000 });
  assert.equal(small.failure_class, "upstream_capacity");
  assert.equal(small.rule_id, "systemone-processing-503");
  const unknownSize = classifyProviderFailure({ status: 503, body, headers: {}, route: JEV });
  assert.equal(unknownSize.failure_class, "upstream_capacity", "no estimate is never read as oversize");
  const onChat = classifyProviderFailure({ status: 503, body, headers: {}, route: CHAT, inputTokens: 0.9 * 58000 });
  assert.notEqual(onChat.rule_id, "systemone-oversize-503", "chat routes keep the generic 5xx rules");
});

async function attemptWith(reply, { inputTokens = 1000, route = JEV } = {}) {
  const realFetch = globalThis.fetch;
  const sent = [];
  globalThis.fetch = async (url, init) => {
    sent.push({ url, body: JSON.parse(init.body) });
    return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { "content-type": "application/json" } });
  };
  try {
    const stored = [];
    const out = await attemptProviderCall({
      env: { BEATAPI_API_KEY: "k" },
      b2: { putJson: async (key, value) => stored.push({ key, value }) },
      route,
      dispatchLimits: LIMITS,
      job: {
        id: "job-1",
        lease_token: "lease",
        not_before_at: 0,
        lease_expires_at: Date.now() + 600_000,
        input_token_estimate: inputTokens,
        payload: JEV_PAYLOAD,
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

test("a JEV call is sent to /systemone and its answer stored with JEV usage", async () => {
  const { out, sent, stored } = await attemptWith({ status: 200, body: JEV_REPLY });
  assert.equal(sent[0].url, "https://api.beatapi.io/v1/systemone");
  assert.deepEqual(Object.keys(sent[0].body).sort(), ["model", "questions", "state"]);
  assert.equal(out.result.outcome, "success");
  assert.equal(out.result.observed_input_tokens, 9830);
  assert.equal(out.result.observed_output_tokens, 1240);
  assert.equal(stored.length, 1);
});

test("an oversize JEV 503 ends the job instead of retrying it; a small one retries", async () => {
  const oversize = await attemptWith(
    { status: 503, body: { code: "processing_failed", retryable: true } },
    { inputTokens: 57000 }
  );
  assert.equal(oversize.out.result.outcome, "terminal_error");
  assert.equal(oversize.out.result.failure_class, "request_defect");
  const transient = await attemptWith(
    { status: 503, body: { code: "processing_failed", retryable: true } },
    { inputTokens: 1000 }
  );
  assert.equal(transient.out.result.outcome, "retryable_error");
  assert.equal(transient.out.result.failure_class, "upstream_capacity");
});

test("a JEV 200 with no answers object is never stored as a result", async () => {
  const { out, stored } = await attemptWith({ status: 200, body: { id: "task_x" } });
  assert.equal(out.result.outcome, "retryable_error");
  assert.equal(stored.length, 0);
});
