import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest, issueBody } from "../src/index.js";

const body = JSON.stringify({ city_state: "Example, TX", email: "person@example.test" });

test("rejects a request without the Formspark secret URL segment", async () => {
  const result = await handleRequest(new Request("https://worker.test", { method: "POST", body }), {});
  assert.equal(result.status, 401);
});

test("accepts only the exact Formspark secret URL segment", async () => {
  const result = await handleRequest(
    new Request("https://worker.test/formspark/secret", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
    }),
    {
      FORMSPARK_WEBHOOK_SECRET: "secret",
      REQUESTS_DB: {
        prepare() {
          return { bind() { return { run: async () => ({ meta: { changes: 0 } }), first: async () => ({ issue_number: 42, status: "created" }) }; } };
        },
      },
    },
  );
  assert.equal(result.status, 202);
});

test("rejects an oversized authenticated body while streaming it", async () => {
  const result = await handleRequest(
    new Request("https://worker.test/formspark/secret", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "x".repeat(33 * 1024),
    }),
    { FORMSPARK_WEBHOOK_SECRET: "secret" },
  );
  assert.equal(result.status, 413);
});

test("website issues preserve the GitHub Issue Form field contract used by discovery", () => {
  const rendered = issueBody({
    city: "Example",
    state: "TX",
    provider: "Swagit",
    meetingUrl: "https://example.test/meetings",
    website: "https://example.test",
    notes: "Council and planning commission",
  });
  assert.match(rendered, /### City and state\nExample, TX/);
  assert.match(rendered, /### Video platform \(if known\)\nSwagit/);
  assert.match(rendered, /### Meeting video \/ feed URL/);
  assert.doesNotMatch(rendered, /person@example/);
});

test("lifecycle status fans out to private email and edits the stored Discord message", async () => {
  const calls = [];
  const updates = [];
  const db = {
    prepare(sql) {
      return {
        bind(...values) {
          return {
            first: async () => ({
              email: "person@example.test",
              discord_message_id: "message-1",
              email_notification_key: null,
              discord_notification_key: null,
            }),
            run: async () => { updates.push({ sql, values }); return { meta: { changes: 1 } }; },
          };
        },
      };
    },
  };
  const fetchImpl = async (url, init) => {
    calls.push({ url: String(url), init });
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  };
  const result = await handleRequest(
    new Request("https://worker.test/status/status-secret", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        issue_number: 42,
        status: "evidence_ready",
        issue_url: "https://github.com/example/repo/issues/42",
      }),
    }),
    {
      STATUS_WEBHOOK_SECRET: "status-secret",
      REQUESTS_DB: db,
      DISCORD_WEBHOOK_URL: "https://discord.com/api/webhooks/id/token",
      RESEND_API_KEY: "resend-secret",
      MAIL_FROM: "City Meetings <updates@example.test>",
      PROJECT_URL: "https://example.test",
    },
    fetchImpl,
  );

  assert.equal(result.status, 200);
  assert.equal(calls.length, 2);
  assert.ok(calls.some((call) => call.url === "https://api.resend.com/emails"));
  assert.ok(calls.some((call) => call.url.endsWith("/messages/message-1")));
  assert.equal(updates.length, 4);
});

test("accepts a correctly signed Discord PING", async () => {
  const keys = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
  const publicKey = new Uint8Array(await crypto.subtle.exportKey("raw", keys.publicKey));
  const raw = JSON.stringify({ type: 1 });
  const timestamp = String(Math.floor(Date.now() / 1000));
  const signature = new Uint8Array(
    await crypto.subtle.sign({ name: "Ed25519" }, keys.privateKey, new TextEncoder().encode(timestamp + raw)),
  );
  const hex = (bytes) => [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
  const result = await handleRequest(
    new Request("https://worker.test/discord/interactions", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-signature-ed25519": hex(signature),
        "x-signature-timestamp": timestamp,
      },
      body: raw,
    }),
    { DISCORD_PUBLIC_KEY: hex(publicKey) },
  );

  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), { type: 1 });
});
