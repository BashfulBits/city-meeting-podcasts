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
