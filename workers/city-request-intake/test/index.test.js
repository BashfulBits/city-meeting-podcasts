import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest } from "../src/index.js";

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
