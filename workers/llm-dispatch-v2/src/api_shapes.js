/**
 * How a route's provider API is spoken, keyed by the route's `api_shape` -- never by provider.
 *
 * `chat` is every OpenAI-compatible chat-completions route this Worker has always served; its
 * functions are the existing ones, unchanged. `systemone` is BeatAPI's typed-judgment endpoint
 * (`POST /v1/systemone`, JEV): the job payload carries `{systemone: {state, questions}}` instead of
 * messages, and the reply is `{answers: {<id>: {...}}, usage: {input_tokens, output_tokens}}`.
 * review/53 PR1; the judging producer that builds these payloads is PR3.
 *
 * `decisions` is Google's typed-judgment endpoint (`POST /v1beta/decisions`, Gemini Decision 1;
 * review/58 WP9, review/49 "Gemini Decision 1 as the anchor"). It reads the SAME typed-question
 * payload as `systemone` (`payload.systemone.{state, questions}`), so producers are unchanged, and
 * differs only on the wire: a required non-empty `input` instead of `state`; question keys limited
 * to `[A-Za-z0-9_-]{1,64}` (ours carry colons, so they are mapped per request and mapped back in
 * the reply, see `decisionKeyMap`); no `criteria` on `noul`; the key in an `x-goog-api-key`
 * header; usage as `{total_input_tokens, total_output_tokens}`.
 *
 * Optional shape hooks used by gateway.js: `authHeaders(apiKey)` (default: Bearer),
 * `normalizeReply(payload, body)` (applied to a parsed 2xx body before any reply check or storage)
 * and `normalizeStatus(status, body)` (the status the rest of the Worker sees for a non-2xx reply).
 */

import {
  normalizeProviderBody,
  observedTokens as chatObservedTokens,
  upstreamEmptyCompletion,
  upstreamRequestForRoute,
} from "./gateway.js";
import { isStructuredPayload, structuredReplyProblem } from "./structured_output.js";
import { classifyDecisions400 } from "./classify.js";

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
  replyProblem: (payload, body) =>
    missingAnswerProblem(payload, body, SYSTEMONE_MAX_MISSING_ANSWER_FRACTION),
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

function missingAnswerProblem(payload, body, maxMissingFraction) {
  const asked = Object.keys(systemoneQuestions(payload) || {});
  if (!asked.length) return null;
  const answers = body?.answers || {};
  const missing = asked.filter(
    (id) => !Object.hasOwn(answers, id) || !answers[id] || typeof answers[id] !== "object"
  ).length;
  return missing / asked.length > maxMissingFraction ? "structured_output_empty" : null;
}

// Gemini Decision 1 must answer every question it was asked: it is a classifier with one output
// token per answer, so a missing key is a fault, not a long reply cut short.
export const DECISIONS_MAX_MISSING_ANSWER_FRACTION = 0;
// What `input` carries when the producer's state is empty: JEV packets put all evidence in each
// question's instructions and send `state: {}`, and Google rejects an empty `input`.
export const DECISIONS_EMPTY_STATE_INPUT =
  "Answer each question using only the evidence given in that question's instructions.";
const DECISION_KEY_RE = /^[A-Za-z0-9_-]{1,64}$/;
// Room for "-" plus a base-36 index after a mapped key's readable stem (64 characters in all).
const DECISION_KEY_STEM = 54;

/**
 * The wire key for each producer question key, for one request. A key Google already accepts is
 * sent as it is; any other key becomes its readable stem (characters outside the charset replaced
 * by "_", cut to 54) plus "-<n>", n the smallest base-36 index not taken by any wire key of the
 * request. Deterministic in the payload's key order, so the reply is mapped back from the payload
 * alone; collision-free because every wire key is checked against all the others, including the
 * keys sent unchanged. Returns `{ toWire: Map(key -> wire), fromWire: Map(wire -> key) }`.
 */
export function decisionKeyMap(questions) {
  const keys = Object.keys(questions || {});
  const toWire = new Map();
  const taken = new Set();
  for (const key of keys) {
    if (DECISION_KEY_RE.test(key)) {
      toWire.set(key, key);
      taken.add(key);
    }
  }
  for (const key of keys) {
    if (toWire.has(key)) continue;
    const stem = key.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, DECISION_KEY_STEM) || "q";
    let n = 0;
    while (taken.has(`${stem}-${n.toString(36)}`)) n += 1;
    const wire = `${stem}-${n.toString(36)}`;
    toWire.set(key, wire);
    taken.add(wire);
  }
  return { toWire, fromWire: new Map([...toWire].map(([key, wire]) => [wire, key])) };
}

function decisionInput(state) {
  if (typeof state === "string") return state.trim() ? state : DECISIONS_EMPTY_STATE_INPUT;
  if (state && typeof state === "object" && Object.keys(state).length) return JSON.stringify(state);
  return DECISIONS_EMPTY_STATE_INPUT;
}

function decisionQuestion(question) {
  // `criteria` is required on choice and score and rejected on noul (JEV took `criteria: {}`).
  if (!question || typeof question !== "object" || question.type !== "noul") return question;
  const { criteria: _dropped, ...rest } = question;
  return rest;
}

const decisions = {
  buildRequest(payload, route) {
    const body = payload?.systemone;
    const questions = systemoneQuestions(payload);
    if (!body || typeof body !== "object" || !questions) {
      throw new Error(`route ${route?.route_id} (api_shape decisions) needs payload.systemone.questions`);
    }
    const { toWire } = decisionKeyMap(questions);
    return {
      model: route.upstream_model,
      input: decisionInput(body.state),
      questions: Object.fromEntries(
        Object.entries(questions).map(([key, question]) => [toWire.get(key), decisionQuestion(question)])
      ),
    };
  },
  // The reply as producers read it: answers under the keys they asked with, as JEV returned them.
  // A wire key the request did not send is dropped. An array-wrapped reply is unwrapped first, as
  // `normalizeProviderBody` does for every other reader.
  normalizeReply(payload, rawBody) {
    const body = normalizeProviderBody(rawBody);
    if (!body || typeof body !== "object" || !body.answers || typeof body.answers !== "object" ||
        Array.isArray(body.answers)) {
      return rawBody;
    }
    const { fromWire } = decisionKeyMap(systemoneQuestions(payload));
    // Built with defineProperty semantics, so a key such as "__proto__" stays an own answer.
    const answers = Object.fromEntries(
      Object.entries(body.answers)
        .filter(([wire]) => fromWire.has(wire))
        .map(([wire, answer]) => [fromWire.get(wire), answer])
    );
    return { ...body, answers };
  },
  // Google reports a quota or rate limit on this endpoint as HTTP 400. Read as 429, it takes the
  // existing rate path (authorizeRetry: retry later, pace the route) instead of failing the job as
  // a non-429 4xx would; safety blocks and every other 400 keep their status.
  normalizeStatus(status, body) {
    if (status !== 400) return status;
    const cls = classifyDecisions400(body)?.failure_class;
    return cls === "own_rpm" || cls === "own_tpm" || cls === "own_rpd" ? 429 : status;
  },
  authHeaders: (apiKey) => ({ "x-goog-api-key": apiKey }),
  emptyCompletion: (status, body) => systemone.emptyCompletion(status, body),
  replyProblem: (payload, body) =>
    missingAnswerProblem(payload, body, DECISIONS_MAX_MISSING_ANSWER_FRACTION),
  observedTokens(body) {
    const usage = body?.usage;
    if (!usage || typeof usage !== "object") return { input: null, output: null };
    return {
      input: Number.isFinite(usage.total_input_tokens) ? usage.total_input_tokens : null,
      output: Number.isFinite(usage.total_output_tokens) ? usage.total_output_tokens : null,
    };
  },
  lengthTruncated: () => false,
};

const SHAPES = { chat, systemone, decisions };
export const API_SHAPES = Object.freeze(Object.keys(SHAPES));

/** The API shape a route speaks; an absent `api_shape` is `chat`. Unknown shapes throw. */
export function apiShapeFor(route) {
  const name = route?.api_shape || "chat";
  const shape = SHAPES[name];
  if (!shape) throw new Error(`route ${route?.route_id} has unknown api_shape ${name}`);
  return shape;
}
