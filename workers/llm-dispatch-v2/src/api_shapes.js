/**
 * How a route's provider API is spoken, keyed by the route's `api_shape` -- never by provider.
 *
 * `chat` is every OpenAI-compatible chat-completions route this Worker has always served; its
 * functions are the existing ones, unchanged. `systemone` is BeatAPI's typed-judgment endpoint
 * (`POST /v1/systemone`, JEV): the job payload carries `{systemone: {state, questions}}` instead of
 * messages, and the reply is `{answers: {<id>: {...}}, usage: {input_tokens, output_tokens}}`.
 * review/53 PR1; the judging producer that builds these payloads is PR3.
 */

import { observedTokens as chatObservedTokens, upstreamEmptyCompletion, upstreamRequestForRoute } from "./gateway.js";
import { isStructuredPayload, structuredReplyProblem } from "./structured_output.js";

// A JEV reply may omit a few answers; the Python parser writes no judgment for those. Past this
// share the reply is treated like an empty structured answer (retryable, counted per route).
export const SYSTEMONE_MAX_MISSING_ANSWER_FRACTION = 0.1;

const chat = {
  buildRequest: (payload, route, options) => upstreamRequestForRoute(payload, route, options),
  emptyCompletion: (status, body) => upstreamEmptyCompletion(status, body),
  replyProblem: (payload, body) => (isStructuredPayload(payload) ? structuredReplyProblem(body) : null),
  observedTokens: (body) => chatObservedTokens(body),
  lengthTruncated: (body) => body?.choices?.[0]?.finish_reason === "length",
};

function systemoneQuestions(payload) {
  const questions = payload?.systemone?.questions;
  return questions && typeof questions === "object" && !Array.isArray(questions) ? questions : null;
}

const systemone = {
  buildRequest(payload, route) {
    const body = payload?.systemone;
    if (!body || typeof body !== "object" || !systemoneQuestions(payload)) {
      throw new Error(`route ${route?.route_id} (api_shape systemone) needs payload.systemone.questions`);
    }
    // No max_tokens, response_format or reasoning controls: the endpoint takes none of them.
    return { model: route.upstream_model, state: body.state ?? {}, questions: body.questions };
  },
  emptyCompletion(status, body) {
    if (status < 200 || status >= 300) return false;
    if (!body || typeof body !== "object" || body.error) return true;
    const answers = body.answers;
    return !answers || typeof answers !== "object" || Array.isArray(answers);
  },
  replyProblem(payload, body) {
    const asked = Object.keys(systemoneQuestions(payload) || {});
    if (!asked.length) return null;
    const answers = body?.answers || {};
    const missing = asked.filter((id) => !answers[id] || typeof answers[id] !== "object").length;
    return missing / asked.length > SYSTEMONE_MAX_MISSING_ANSWER_FRACTION ? "structured_output_empty" : null;
  },
  observedTokens(body) {
    const usage = body?.usage;
    if (!usage || typeof usage !== "object") return { input: null, output: null };
    return {
      input: Number.isFinite(usage.input_tokens) ? usage.input_tokens : null,
      output: Number.isFinite(usage.output_tokens) ? usage.output_tokens : null,
    };
  },
  lengthTruncated: () => false,
};

const SHAPES = { chat, systemone };
export const API_SHAPES = Object.freeze(Object.keys(SHAPES));

/** The API shape a route speaks; an absent `api_shape` is `chat`. Unknown shapes throw. */
export function apiShapeFor(route) {
  const name = route?.api_shape || "chat";
  const shape = SHAPES[name];
  if (!shape) throw new Error(`route ${route?.route_id} has unknown api_shape ${name}`);
  return shape;
}
