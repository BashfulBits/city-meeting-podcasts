import test from "node:test";
import assert from "node:assert/strict";
import {
  canonicalJson,
  computeRequestDigest,
  sha256Hex,
  validateEnqueueBatchRequest,
  validateEnqueueJob,
  validatePollBatchRequest,
  validateResolveUnknownBatchRequest,
  validateSchemaRetryRequest,
} from "../src/protocol.js";

test("sha256Hex computes correct sha256", async () => {
  const hash = await sha256Hex("hello world");
  assert.equal(hash, "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9");
});

test("canonicalJson sorts keys deterministically", () => {
  const obj1 = { b: 2, a: 1, c: { y: 20, x: 10 } };
  const obj2 = { a: 1, c: { x: 10, y: 20 }, b: 2 };
  assert.equal(canonicalJson(obj1), canonicalJson(obj2));
  assert.equal(canonicalJson(obj1), '{"a":1,"b":2,"c":{"x":10,"y":20}}');
});

test("computeRequestDigest hashes canonical payload", async () => {
  const payload1 = { messages: [{ role: "user", content: "hi" }], model: "mistral-large" };
  const payload2 = { model: "mistral-large", messages: [{ role: "user", content: "hi" }] };
  const digest1 = await computeRequestDigest(payload1);
  const digest2 = await computeRequestDigest(payload2);
  assert.equal(digest1, digest2);
  assert.equal(typeof digest1, "string");
  assert.equal(digest1.length, 64);
});

test("validateEnqueueJob validates required fields", () => {
  assert.equal(
    validateEnqueueJob(null).valid,
    false
  );
  assert.equal(
    validateEnqueueJob({ idempotency_key: "k1" }).valid,
    false
  );
  assert.equal(
    validateEnqueueJob({
      idempotency_key: "k1",
      request_digest: "d1",
      prompt_family: "tags",
      payload_key: "payloads/j1/request.json",
      input_token_estimate: 100,
      max_output_token_estimate: 50,
      priority: 0,
    }).valid,
    true
  );
  assert.equal(
    validateEnqueueJob({
      idempotency_key: "k1",
      request_digest: "d1",
      prompt_family: "tags",
      payload_key: "payloads/j1/request.json",
      input_token_estimate: -1,
      max_output_token_estimate: 50,
    }).valid,
    false
  );
});

test("validateEnqueueBatchRequest validates jobs array and bounds", () => {
  assert.equal(validateEnqueueBatchRequest(null).valid, false);
  assert.equal(validateEnqueueBatchRequest({ jobs: [] }).valid, false);

  const job = {
    idempotency_key: "k1",
    request_digest: "d1",
    prompt_family: "tags",
    payload_key: "payloads/j1/request.json",
    input_token_estimate: 100,
    max_output_token_estimate: 50,
  };

  assert.equal(validateEnqueueBatchRequest({ jobs: [job] }, 10).valid, true);
  assert.equal(validateEnqueueBatchRequest({ jobs: [job, job] }, 1).valid, false);
});

test("validatePollBatchRequest validates IDs array and bounds", () => {
  assert.equal(validatePollBatchRequest(null).valid, false);
  assert.equal(validatePollBatchRequest({ ids: [] }).valid, false);
  assert.equal(validatePollBatchRequest({ ids: ["id1", "id2"] }, 10).valid, true);
  assert.equal(validatePollBatchRequest({ ids: ["id1", ""] }, 10).valid, false);
  assert.equal(validatePollBatchRequest({ ids: ["id1", "id2"] }, 1).valid, false);
});

test("validateSchemaRetryRequest and validateResolveUnknownBatchRequest", () => {
  assert.equal(validateSchemaRetryRequest(null).valid, false);
  assert.equal(validateSchemaRetryRequest({ corrected_payload_key: "k1" }).valid, true);
  assert.equal(validateResolveUnknownBatchRequest({ attempt_ids: ["a1"] }).valid, true);
  assert.equal(validateResolveUnknownBatchRequest({ attempt_ids: [] }).valid, false);
});
