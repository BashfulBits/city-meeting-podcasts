/**
 * Per-route structured-output shaping and reply checks (review/48 R10).
 *
 * A durable job carries only its response schema (`payload.structured_output = {name, schema}`).
 * This Worker is the only component that knows which route a pooled job actually lands on, so it
 * shapes the request for that route's compiled method here. Python's direct path does the same
 * in citypods/compute/structured_shaping.py; both implementations are asserted against one shared
 * fixture, tests/fixtures/structured_output_shaping.json, so neither can drift from the other.
 *
 * A route's method arrives compiled (scripts/compile_llm_limits.py) as three fields:
 * `structured_output_response_format` ("json_schema" | "json_object" | "none"),
 * `structured_output_include_schema_in_prompt`, and `structured_output_schema_strip_keys`.
 */

export const SCHEMA_INSTRUCTION =
  "Return one JSON object only (no Markdown or commentary) matching this JSON Schema:\n";

function sortKeys(node) {
  if (Array.isArray(node)) return node.map(sortKeys);
  if (node && typeof node === "object") {
    const sorted = {};
    for (const key of Object.keys(node).sort()) sorted[key] = sortKeys(node[key]);
    return sorted;
  }
  return node;
}

/** Sorted keys, compact separators -- byte-identical to structured_shaping.canonical_schema_json. */
export function canonicalSchemaJson(schema) {
  return JSON.stringify(sortKeys(schema));
}

/** Deep copy of a JSON Schema node with every object key in `keys` removed. */
export function stripSchemaKeys(node, keys) {
  const drop = keys instanceof Set ? keys : new Set(keys || []);
  if (drop.size === 0) return node;
  if (Array.isArray(node)) return node.map((item) => stripSchemaKeys(item, drop));
  if (node && typeof node === "object") {
    const out = {};
    for (const [key, value] of Object.entries(node)) {
      if (!drop.has(key)) out[key] = stripSchemaKeys(value, drop);
    }
    return out;
  }
  return node;
}

/** Append the schema instruction to the first string system message, or prepend one. */
export function messagesWithSchema(messages, schema) {
  const instruction = SCHEMA_INSTRUCTION + canonicalSchemaJson(schema);
  const enriched = (messages || []).map((message) => ({ ...message }));
  for (const message of enriched) {
    if (message.role === "system" && typeof message.content === "string") {
      message.content = `${message.content}\n\n${instruction}`;
      return enriched;
    }
  }
  return [{ role: "system", content: instruction }, ...enriched];
}

/** Returns `{ messages, responseFormat }`; `responseFormat` is null when none may be sent. */
export function shapeForRoute(messages, structuredOutput, route) {
  const responseFormat = route?.structured_output_response_format || "json_schema";
  const includeSchema = route?.structured_output_include_schema_in_prompt === true;
  const schema = stripSchemaKeys(
    structuredOutput?.schema ?? {},
    route?.structured_output_schema_strip_keys || []
  );
  const shaped = includeSchema
    ? messagesWithSchema(messages, schema)
    : (messages || []).map((message) => ({ ...message }));
  if (responseFormat === "json_schema") {
    return {
      messages: shaped,
      responseFormat: { type: "json_schema", json_schema: { name: structuredOutput?.name, schema } },
    };
  }
  if (responseFormat === "json_object") {
    return { messages: shaped, responseFormat: { type: "json_object" } };
  }
  if (responseFormat === "none") {
    return { messages: shaped, responseFormat: null };
  }
  throw new Error(`unsupported structured-output response_format: ${responseFormat}`);
}

/** True for a payload whose reply must be JSON: schema-only jobs and legacy pre-shaped ones. */
export function isStructuredPayload(payload) {
  return Boolean(payload?.structured_output || payload?.response_format);
}

const THOUGHT_BLOCK_RE = /^\s*<(think|thought)>[\s\S]*?<\/\1>/i;
// Bounds the Worker's CPU on a long non-JSON reply. Past this many failed candidate starts the
// reply is treated as invalid (fail closed), so it takes the same retry path as any other.
const MAX_JSON_START_CANDIDATES = 32;

function assistantText(body) {
  const content = body?.choices?.[0]?.message?.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => (typeof part === "string" ? part : typeof part?.text === "string" ? part.text : ""))
      .join("");
  }
  return null;
}

/**
 * Why a 2xx reply to a structured request is not usable, or null when it is. Mirrors
 * citypods/compute/structured.py:parse_structured_json's tolerance -- a leading
 * <think>/<thought> block, one Markdown fence, JSON starting at any `{`/`[` with nothing but
 * whitespace after it -- so a reply Python would accept is never failed here.
 *
 * `structured_output_empty` is the silent failure this exists for: NVIDIA's deepseek-v4.1-flash
 * answered every response_format with a 200 whose content was empty (2026-09-24). Settling that as
 * success stores a non-answer and clears the route's backoff; failing it retries the job on
 * another route and stands this one down (coordinator.js, upstream class).
 */
export function structuredReplyProblem(body) {
  const raw = assistantText(body);
  if (raw === null || raw.trim() === "") return "structured_output_empty";
  let text = raw.replace(/^﻿/, "").replace(THOUGHT_BLOCK_RE, "").trim();
  if (text === "") return "structured_output_empty";
  const fenceStart = text.indexOf("```");
  if (fenceStart >= 0) {
    const closing = text.indexOf("```", fenceStart + 3);
    if (closing < 0) return "structured_output_invalid";
    let fenced = text.slice(fenceStart + 3, closing).trimStart();
    if (fenced.toLowerCase().startsWith("json")) fenced = fenced.slice(4).replace(/^[ \t\r\n]+/, "");
    text = fenced.trim();
  }
  let tried = 0;
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (char !== "{" && char !== "[" && char !== "]") continue;
    if (++tried > MAX_JSON_START_CANDIDATES) return "structured_output_invalid";
    try {
      JSON.parse(text.slice(index));
      return null;
    } catch {
      // Not JSON from this start; try the next candidate, as Python's raw_decode loop does.
    }
  }
  return "structured_output_invalid";
}
