import test from "node:test";
import assert from "node:assert/strict";
import { resolveProviderCredentials } from "../src/gateway.js";

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
