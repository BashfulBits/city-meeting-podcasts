// review/58 12.9: the `openrouter_decisions` API shape (OpenRouter POST /api/alpha/decisions,
// Perplexity Decider V1.3), the second judge:anchor. The fixtures follow the request and reply
// shapes measured in the 2026-11-13 replay only; they carry no transcript content.
import test from "node:test";
import assert from "node:assert/strict";
import { API_SHAPES, apiShapeFor, decisionKeyMap } from "../src/api_shapes.js";
import { resolveProviderCredentials } from "../src/gateway.js";

const PROVIDER = {
  api_base: "https://openrouter.ai/api",
  ai_gateway_slug: "custom-openrouter-api",
  ai_gateway_chat_path: "/v1/chat/completions",
  chat_path: "/v1/chat/completions",
  accounts: [{ id: "primary", api_key_env: "OPENROUTER_API_KEY" }],
};
const LIMITS = { providers: { openrouter_decisions: PROVIDER } };
const DECIDER = {
  route_id: "openrouter_pplx_decider_v1_3_free",
  provider: "openrouter_decisions",
  upstream_model: "perplexity/pplx-decider-v1.3:free",
  account_id: "primary",
  api_shape: "openrouter_decisions",
  request_path: "/alpha/decisions",
};
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
const ANSWERS = {
  "s1:supported": { type: "noul", noul: 0.91 },
  "s1:grade": { type: "score", score: "high" },
  "s2:choose": { type: "choice", choice: "a", probabilities: { a: 0.6, b: 0.4 }, confidence: 0.6 },
};

function wireReply(answersByKey) {
  const { toWire } = decisionKeyMap(PAYLOAD.systemone.questions);
  return {
    id: "gen-1",
    model: "perplexity/pplx-decider-v1.3:free",
    provider: "Perplexity",
    answers: Object.fromEntries(Object.entries(answersByKey).map(([k, a]) => [toWire.get(k), a])),
    usage: { input_tokens: 1800, output_tokens: 6, cost: 0 },
  };
}

test("openrouter_decisions is a known shape", () => {
  assert.ok(API_SHAPES.includes("openrouter_decisions"));
});

test("it builds {model, state, questions}: keys mapped, noul criteria dropped, Bearer auth", () => {
  const shape = apiShapeFor(DECIDER);
  const req = shape.buildRequest({ ...PAYLOAD, max_tokens: 1, messages: [] }, DECIDER);
  assert.deepEqual(Object.keys(req).sort(), ["model", "questions", "state"]);
  assert.equal(req.model, "perplexity/pplx-decider-v1.3:free");
  assert.deepEqual(req.state, {});
  for (const key of Object.keys(req.questions)) assert.match(key, /^[A-Za-z0-9_-]{1,64}$/);
  const byType = Object.fromEntries(Object.values(req.questions).map((q) => [q.type, q]));
  assert.equal("criteria" in byType.noul, false);
  assert.deepEqual(byType.choice.criteria, { a: "A", b: "B" });
  assert.equal(shape.authHeaders, undefined, "the default Bearer header is used");
  assert.equal(shape.normalizeStatus, undefined, "OpenRouter's statuses are read as they are");
  assert.throws(() => shape.buildRequest({ messages: [] }, DECIDER), /api_shape openrouter_decisions/);
});

test("the reply is mapped back, usage read, and every asked answer required", () => {
  const shape = apiShapeFor(DECIDER);
  const normalized = shape.normalizeReply(PAYLOAD, wireReply(ANSWERS));
  assert.deepEqual(normalized.answers, ANSWERS);
  assert.equal(shape.replyProblem(PAYLOAD, normalized), null);
  assert.deepEqual(shape.observedTokens(normalized), { input: 1800, output: 6 });
  assert.equal(shape.emptyCompletion(200, normalized), false);
  const { "s2:choose": _missing, ...partial } = ANSWERS;
  const short = shape.normalizeReply(PAYLOAD, wireReply(partial));
  assert.equal(shape.replyProblem(PAYLOAD, short), "structured_output_empty");
});

test("request_path resolves under the custom-openrouter-api registration and directly", () => {
  const gatewayEnv = { OPENROUTER_API_KEY: "k", CLOUDFLARE_ACCOUNT_ID: "acct", AI_GATEWAY_ID: "gw" };
  assert.equal(
    resolveProviderCredentials(gatewayEnv, DECIDER, LIMITS).url,
    "https://gateway.ai.cloudflare.com/v1/acct/gw/custom-openrouter-api/alpha/decisions"
  );
  assert.equal(
    resolveProviderCredentials({ OPENROUTER_API_KEY: "k" }, DECIDER, LIMITS).url,
    "https://openrouter.ai/api/alpha/decisions"
  );
});
