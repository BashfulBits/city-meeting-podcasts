import test from "node:test";
import assert from "node:assert/strict";
import { resolveProviderCredentials, upstreamRequestForRoute } from "../src/gateway.js";

const DISPATCH_LIMITS = {
  providers: {
    gemini: {
      api_base: "https://generativelanguage.googleapis.com/v1beta/openai",
      ai_gateway_slug: "google-ai-studio",
      ai_gateway_chat_path: "/v1beta/openai/chat/completions",
      chat_path: "/chat/completions",
      accounts: [{ id: "project_primary", api_key_env: "GEMINI_API_KEY" }],
    },
  },
};

const ROUTE = {
  route_id: "gemini_flash_primary",
  provider: "gemini",
  upstream_model: "gemini-flash",
  account_id: "project_primary",
};

test("resolveProviderCredentials routes through AI Gateway when AI_GATEWAY_ID + CLOUDFLARE_ACCOUNT_ID are set", () => {
  // Matches production wrangler.jsonc: AI_GATEWAY_ID is a plain var, CLOUDFLARE_ACCOUNT_ID a
  // secret -- both required together to build the gateway URL (see gateway.js).
  const env = {
    GEMINI_API_KEY: "test-key",
    CLOUDFLARE_ACCOUNT_ID: "acct123",
    AI_GATEWAY_ID: "citypods-dispatch",
  };
  const creds = resolveProviderCredentials(env, ROUTE, DISPATCH_LIMITS);
  assert.equal(creds.usesGateway, true);
  assert.equal(
    creds.url,
    "https://gateway.ai.cloudflare.com/v1/acct123/citypods-dispatch/google-ai-studio/v1beta/openai/chat/completions"
  );
});

test("resolveProviderCredentials falls back to calling the provider directly when AI_GATEWAY_ID is missing", () => {
  // Regression for the 2026-08 incident: this deployment ran for days with no AI_GATEWAY_ID
  // configured (neither as a var nor a secret) and every call silently went straight to the
  // provider -- no error, no log line, nothing in the AI Gateway dashboard. This must stay a
  // deliberate, visible fallback the test suite pins, not something that can regress unnoticed.
  const env = {
    GEMINI_API_KEY: "test-key",
    CLOUDFLARE_ACCOUNT_ID: "acct123",
    // AI_GATEWAY_ID intentionally omitted.
  };
  const creds = resolveProviderCredentials(env, ROUTE, DISPATCH_LIMITS);
  assert.equal(creds.usesGateway, false);
  assert.equal(creds.url, "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions");
});

test("resolveProviderCredentials prefers an explicit AI_GATEWAY_BASE_URL over AI_GATEWAY_ID", () => {
  const env = {
    GEMINI_API_KEY: "test-key",
    AI_GATEWAY_BASE_URL: "https://gateway.example.com/v1/acct/gw",
    AI_GATEWAY_ID: "citypods-dispatch",
    CLOUDFLARE_ACCOUNT_ID: "acct123",
  };
  const creds = resolveProviderCredentials(env, ROUTE, DISPATCH_LIMITS);
  assert.equal(creds.usesGateway, true);
  assert.equal(
    creds.url,
    "https://gateway.example.com/v1/acct/gw/google-ai-studio/v1beta/openai/chat/completions"
  );
});

test("upstreamRequestForRoute strips policy-only fields instead of spreading the stored payload", () => {
  // Regression for the 2026-08-26 incident: citypods/compute/llm.py's enqueue_batch() stored a
  // payload carrying policy/router-only fields alongside model/messages (fixed there separately),
  // and this function's old blind `...payload` spread forwarded them straight to the provider --
  // Mistral and Groq both rejected the request outright as a result. This must stay an allowlist
  // so a bad stored payload (already in B2 from before that fix, or any future one) can only ever
  // produce a silently-dropped extra key, never a live 100%-failure incident again.
  const payload = {
    model: "gemini/gemini-flash-lite", // the logical name -- must be overridden by route below
    messages: [{ role: "user", content: "hi" }],
    stream: true, // must always be forced to false regardless of what the stored payload says
    allow_paid: true,
    allow_batch: true,
    submit_next: false,
    timeout_class: "long",
    allowed_models: ["gemini/gemini-flash-lite"],
    input_tokens_estimate: 500,
    output_token_budget: 1024,
    deadline_at: "2026-08-26T00:00:00Z",
  };
  const request = upstreamRequestForRoute(payload, ROUTE);
  assert.deepEqual(request, {
    model: "gemini-flash", // ROUTE.upstream_model, not the payload's logical model
    messages: payload.messages,
    stream: false,
  });
});

test("upstreamRequestForRoute forwards only recognized provider-tuning fields", () => {
  const payload = {
    messages: [{ role: "user", content: "hi" }],
    temperature: 0.2,
    top_p: 0.9,
    max_tokens: 512,
    tools: [{ type: "function", function: { name: "noop" } }],
    tool_choice: "auto",
    response_format: { type: "json_object" },
    // Not a recognized field -- must be dropped, same as any policy field would be.
    some_unrecognized_field: "should not appear",
  };
  const request = upstreamRequestForRoute(payload, ROUTE);
  assert.deepEqual(request, {
    model: "gemini-flash",
    messages: payload.messages,
    stream: false,
    temperature: 0.2,
    top_p: 0.9,
    max_tokens: 512,
    tools: payload.tools,
    tool_choice: "auto",
    response_format: payload.response_format,
  });
});
