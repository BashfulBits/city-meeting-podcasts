import test from "node:test";
import assert from "node:assert/strict";
import { assembleChatStream, callAiGateway } from "../src/gateway.js";

const enc = new TextEncoder();
const frame = (obj) => `data: ${typeof obj === "string" ? obj : JSON.stringify(obj)}\n\n`;
const chunk = (delta, finish = null, extra = {}) => ({
  id: "c1", model: "m", created: 1, choices: [{ index: 0, delta, finish_reason: finish }], ...extra,
});

function streamResponse(parts, { headers = { "content-type": "text/event-stream" } } = {}) {
  const body = new ReadableStream({
    start(controller) {
      for (const part of parts) controller.enqueue(enc.encode(part));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers });
}

test("assembleChatStream rebuilds one reply, across split frames, with reasoning and usage", async () => {
  const text =
    frame(chunk({ role: "assistant", reasoning_content: "think " })) +
    frame(chunk({ reasoning_content: "more" })) +
    frame(chunk({ content: "Hel" })) +
    frame(chunk({ content: "lo" }, "stop")) +
    frame({ id: "c1", choices: [], usage: { prompt_tokens: 3, completion_tokens: 5 } }) +
    "data: [DONE]\n\n";
  // Cut at arbitrary byte positions, including inside a frame.
  const parts = [text.slice(0, 17), text.slice(17, 90), text.slice(90)];
  const body = await assembleChatStream(streamResponse(parts));
  assert.equal(body.choices[0].message.content, "Hello");
  assert.equal(body.choices[0].message.reasoning_content, "think more");
  assert.equal(body.choices[0].finish_reason, "stop");
  assert.deepEqual(body.usage, { prompt_tokens: 3, completion_tokens: 5 });
});

test("assembleChatStream merges streamed tool calls", async () => {
  const text =
    frame(chunk({ tool_calls: [{ index: 0, id: "t1", function: { name: "look", arguments: '{"a"' } }] })) +
    frame(chunk({ tool_calls: [{ index: 0, function: { arguments: ":1}" } }] }, "tool_calls")) +
    "data: [DONE]\n\n";
  const body = await assembleChatStream(streamResponse([text]));
  assert.deepEqual(body.choices[0].message.tool_calls[0].function, { name: "look", arguments: '{"a":1}' });
});

test("assembleChatStream returns a mid-stream error object as the body", async () => {
  const text = frame(chunk({ content: "x" })) + frame({ error: { message: "boom", code: 500 } });
  const body = await assembleChatStream(streamResponse([text]));
  assert.equal(body.error.message, "boom");
});

test("assembleChatStream rejects a stream that ends without a finish reason", async () => {
  await assert.rejects(
    assembleChatStream(streamResponse([frame(chunk({ content: "partial" }))])),
    /stream_ended_early/
  );
});

test("assembleChatStream rejects a stalled stream and an oversized one", async () => {
  const stalled = new Response(new ReadableStream({ start(c) { c.enqueue(enc.encode(frame(chunk({ content: "a" })))); } }), {
    headers: { "content-type": "text/event-stream" },
  });
  await assert.rejects(assembleChatStream(stalled, { idleMs: 20 }), /stream_idle_timeout/);
  await assert.rejects(
    assembleChatStream(streamResponse([frame(chunk({ content: "a".repeat(500) }))]), { maxChars: 100 }),
    /stream_too_large/
  );
});

test("assembleChatStream cost stays small for a long reasoning stream", async () => {
  const parts = [];
  for (let i = 0; i < 12000; i += 1) parts.push(frame(chunk({ reasoning_content: ` token${i} of text` })));
  parts.push(frame(chunk({ content: "ok" }, "stop")), "data: [DONE]\n\n");
  const started = process.hrtime.bigint();
  const body = await assembleChatStream(streamResponse(parts));
  const ms = Number(process.hrtime.bigint() - started) / 1e6;
  assert.equal(body.choices[0].message.content, "ok");
  assert.ok(ms < 1000, `assembly took ${ms} ms`);
});

const LIMITS = (streamResponse) => ({
  providers: {
    beat: {
      api_base: "https://api.example.test/v1",
      chat_path: "/chat/completions",
      ...(streamResponse === undefined ? {} : { stream_response: streamResponse }),
      accounts: [{ id: "primary", api_key_env: "BEAT_KEY" }],
    },
  },
});
const ROUTE = { route_id: "beat_x", provider: "beat", upstream_model: "m", account_id: "primary" };

async function run(limits, route, respond) {
  const original = globalThis.fetch;
  const sent = [];
  try {
    globalThis.fetch = async (url, init) => {
      sent.push(JSON.parse(init.body));
      return respond();
    };
    const result = await callAiGateway({
      env: { BEAT_KEY: "k" }, route, payload: { messages: [{ role: "user", content: "hi" }] }, dispatchLimits: limits,
    });
    return { result, sent };
  } finally {
    globalThis.fetch = original;
  }
}

test("a provider with stream_response asks for a stream and gets the rebuilt reply", async () => {
  const text = frame(chunk({ content: "yo" }, "stop")) + "data: [DONE]\n\n";
  const { result, sent } = await run(LIMITS(true), ROUTE, () => streamResponse([text]));
  assert.equal(sent[0].stream, true);
  assert.deepEqual(sent[0].stream_options, { include_usage: true });
  assert.equal(result.ok, true);
  assert.equal(result.body.choices[0].message.content, "yo");
});

test("without stream_response (or with a route override) the call is plain request/response", async () => {
  const plain = () => new Response(JSON.stringify({ choices: [{ message: { content: "p" } }] }), { status: 200 });
  for (const [limits, route] of [
    [LIMITS(undefined), ROUTE],
    [LIMITS(false), ROUTE],
    [LIMITS(true), { ...ROUTE, stream_response: false }],
  ]) {
    const { result, sent } = await run(limits, route, plain);
    assert.equal(sent[0].stream, false);
    assert.equal(sent[0].stream_options, undefined);
    assert.equal(result.body.choices[0].message.content, "p");
  }
});

test("a streaming provider's JSON error reply is still read as an ordinary error", async () => {
  const { result } = await run(LIMITS(true), ROUTE, () =>
    new Response(JSON.stringify({ error: { message: "rate" } }), { status: 429, headers: { "content-type": "application/json" } })
  );
  assert.equal(result.status, 429);
  assert.equal(result.ok, false);
  assert.equal(result.body.error.message, "rate");
});
