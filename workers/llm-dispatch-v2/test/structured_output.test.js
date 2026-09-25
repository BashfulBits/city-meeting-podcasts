import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { upstreamRequestForRoute } from "../src/gateway.js";
import { shapeForRoute, structuredReplyProblem } from "../src/structured_output.js";

// review/48 R10: the same contract citypods/compute/structured_shaping.py is tested against.
const FIXTURE = JSON.parse(
  readFileSync(new URL("../../../tests/fixtures/structured_output_shaping.json", import.meta.url), "utf8")
);

for (const testCase of FIXTURE.cases) {
  test(`shared shaping contract: ${testCase.name}`, () => {
    const { messages, responseFormat } = shapeForRoute(
      testCase.request.messages,
      testCase.request.structured_output,
      testCase.route
    );
    assert.deepEqual(messages, testCase.expected.messages);
    assert.deepEqual(responseFormat, testCase.expected.response_format);
  });
}

const PROMPT_ONLY_ROUTE = {
  upstream_model: "deepseek-ai/deepseek-v4.1-flash",
  structured_output_response_format: "none",
  structured_output_include_schema_in_prompt: true,
  structured_output_schema_strip_keys: [],
};
const RELAXED_ROUTE = {
  upstream_model: "gemini-3.1-flash-lite",
  structured_output_response_format: "json_schema",
  structured_output_include_schema_in_prompt: false,
  structured_output_schema_strip_keys: ["maxLength"],
};
const SCHEMA = { type: "object", properties: { a: { type: "string", maxLength: 5 } } };

test("a schema-only job is shaped for the route that serves it, not the pool's first model", () => {
  const payload = {
    model: "gemini/gemini-3.1-flash-lite",
    messages: [{ role: "user", content: "hi" }],
    max_tokens: 64,
    structured_output: { name: "Out", schema: SCHEMA },
  };
  const toNvidia = upstreamRequestForRoute(payload, PROMPT_ONLY_ROUTE);
  assert.equal("response_format" in toNvidia, false);
  assert.equal(toNvidia.messages[0].role, "system");
  assert.equal("structured_output" in toNvidia, false, "the schema envelope is never forwarded");
  assert.equal(toNvidia.max_tokens, 64);

  const toGemini = upstreamRequestForRoute(payload, RELAXED_ROUTE);
  assert.deepEqual(toGemini.response_format, {
    type: "json_schema",
    json_schema: { name: "Out", schema: { type: "object", properties: { a: { type: "string" } } } },
  });
  assert.deepEqual(toGemini.messages, payload.messages);
});

test("a legacy pre-shaped payload is forwarded unchanged", () => {
  const legacy = {
    messages: [{ role: "user", content: "hi" }],
    response_format: { type: "json_object" },
  };
  const request = upstreamRequestForRoute(legacy, PROMPT_ONLY_ROUTE);
  assert.deepEqual(request.response_format, { type: "json_object" });
  assert.deepEqual(request.messages, legacy.messages);
});

const reply = (content) => ({ choices: [{ message: { role: "assistant", content } }] });

test("an empty structured reply is flagged, whatever the provider left in reasoning", () => {
  assert.equal(structuredReplyProblem(reply("")), "structured_output_empty");
  assert.equal(structuredReplyProblem(reply("   \n")), "structured_output_empty");
  assert.equal(structuredReplyProblem(reply(null)), "structured_output_empty");
  assert.equal(
    structuredReplyProblem({ choices: [{ message: { content: "", reasoning_content: "{...}" } }] }),
    "structured_output_empty"
  );
  assert.equal(structuredReplyProblem(reply("<think>working</think>")), "structured_output_empty");
});

test("replies Python's parser accepts are never flagged", () => {
  for (const ok of [
    '{"a": 1}',
    ' [1, 2] ',
    '```json\n{"a": 1}\n```',
    '<think>plan</think>\n{"a": 1}',
    'Here you go: {"a": 1}',
    [{ type: "text", text: '{"a": 1}' }],
  ]) {
    assert.equal(structuredReplyProblem(reply(ok)), null, JSON.stringify(ok));
  }
});

test("a non-JSON structured reply is flagged invalid", () => {
  assert.equal(structuredReplyProblem(reply("I cannot help with that.")), "structured_output_invalid");
  assert.equal(structuredReplyProblem(reply('{"a": 1} and more prose')), "structured_output_invalid");
  assert.equal(structuredReplyProblem(reply('```json\n{"a": 1}')), "structured_output_invalid");
});

test("a route's request_params are sent with every request to it", () => {
  const route = {
    ...PROMPT_ONLY_ROUTE,
    request_params: { chat_template_kwargs: { enable_thinking: false } },
  };
  const request = upstreamRequestForRoute(
    { messages: [{ role: "user", content: "hi" }], structured_output: { name: "Out", schema: SCHEMA } },
    route
  );
  assert.deepEqual(request.chat_template_kwargs, { enable_thinking: false });
  const plain = upstreamRequestForRoute({ messages: [] }, { ...PROMPT_ONLY_ROUTE, request_params: null });
  assert.equal("chat_template_kwargs" in plain, false);
});

test("a reply with too many unparseable JSON starts fails closed", () => {
  // Past the candidate cap the Worker cannot afford to keep trying, so the reply takes the same
  // retry path as any other invalid structured reply instead of being stored as a result.
  const junk = "[x] ".repeat(40) + '{"a": 1} trailing prose';
  assert.equal(structuredReplyProblem(reply(junk)), "structured_output_invalid");
});

import { MAX_ROUTE_OUTPUT_TOKENS, outputTokensForRoute } from "../src/gateway.js";

test("a route_max job is sent the route's output limit, capped and bounded by input room", () => {
  const big = { output_context_limit: 384000, input_context_limit: 1000000 };
  assert.equal(outputTokensForRoute({ max_tokens: 16384, max_tokens_mode: "route_max" }, big, 20000), MAX_ROUTE_OUTPUT_TOKENS);
  const tight = { output_context_limit: 65536, input_context_limit: 262144 };
  assert.equal(outputTokensForRoute({ max_tokens: 8192, max_tokens_mode: "route_max" }, tight, 240000), 22144);
  // The input room bounds even a reservation above it, so input + output fits the window.
  assert.equal(outputTokensForRoute({ max_tokens: 30000, max_tokens_mode: "route_max" }, tight, 240000), 22144);
  // The room is measured in the route's own tokenizer units.
  const dense = { ...tight, input_token_ratio: 1.05 };
  assert.equal(outputTokensForRoute({ max_tokens: 8192, max_tokens_mode: "route_max" }, dense, 240000), 10144);
  // No room at all keeps the job's figure; the provider rejects the oversized request.
  assert.equal(outputTokensForRoute({ max_tokens: 8192, max_tokens_mode: "route_max" }, tight, 300000), 8192);
  // Without the flag the job's own max_tokens is sent unchanged.
  assert.equal(outputTokensForRoute({ max_tokens: 4096 }, big, 20000), 4096);
});

test("a schema added to the prompt counts against the route_max input room", () => {
  const route = { ...PROMPT_ONLY_ROUTE, output_context_limit: 65536, input_context_limit: 262144 };
  const schema = { type: "object", description: "y".repeat(4000) };
  const payload = {
    messages: [{ role: "user", content: "x" }],
    structured_output: { name: "s", schema },
    max_tokens: 8192,
    max_tokens_mode: "route_max",
  };
  const request = upstreamRequestForRoute(payload, route, { inputTokens: 240000 });
  const added = request.messages.reduce((n, m) => n + m.content.length, 0) - 1;
  assert.equal(request.max_tokens, 22144 - Math.ceil(added / 4));
});

test("a lane's reasoning level is applied through the route's controls, and only when set", () => {
  const route = {
    ...PROMPT_ONLY_ROUTE,
    reasoning_controls: { off: { chat_template_kwargs: { enable_thinking: false } } },
  };
  const payload = { messages: [{ role: "user", content: "hi" }], max_tokens: 64 };
  assert.deepEqual(
    upstreamRequestForRoute(payload, route, { reasoningLevel: "off" }).chat_template_kwargs,
    { enable_thinking: false }
  );
  assert.equal("chat_template_kwargs" in upstreamRequestForRoute(payload, route, {}), false);
  assert.equal("chat_template_kwargs" in upstreamRequestForRoute(payload, route, { reasoningLevel: "low" }), false);
});
