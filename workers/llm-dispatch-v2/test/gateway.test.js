import test from "node:test";
import assert from "node:assert/strict";
import {
  parseErrorMessageRetryAfter,
  parseRetryAfterSeconds,
  resolveProviderCredentials,
  upstreamCapacityFailure,
  upstreamRequestForRoute,
} from "../src/gateway.js";

const DISPATCH_LIMITS = {
  providers: {
    gemini: {
      api_base: "https://generativelanguage.googleapis.com/v1beta/openai",
      ai_gateway_slug: "google-ai-studio",
      ai_gateway_chat_path: "/v1beta/openai/chat/completions",
      chat_path: "/chat/completions",
      ai_gateway_max_attempts: 1,
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
  assert.equal(creds.aiGatewayMaxAttempts, 1);
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

test("upstreamCapacityFailure recognises a 400 whose body blames the provider's upstream", () => {
  // The exact payload OpenCode Zen returned on 2026-08-30 while still listing the model as
  // available. Both signals in it are load-bearing and asserted separately below.
  const observed = {
    error: {
      type: "server_error",
      message: "Error from provider (Console): Upstream request failed: Model is unavailable.",
    },
  };
  assert.equal(upstreamCapacityFailure(400, observed), true);

  // type alone is enough...
  assert.equal(upstreamCapacityFailure(400, { error: { type: "server_error", message: "" } }), true);
  // ...and so is the message alone, for providers that do not set a machine-readable type.
  assert.equal(
    upstreamCapacityFailure(400, { error: { type: "", message: "Upstream request failed" } }),
    true
  );
  assert.equal(upstreamCapacityFailure(400, { error: { message: "No capacity available" } }), true);
});

test("upstreamCapacityFailure leaves genuine request defects terminal", () => {
  // The rule must stay narrow, or a schema bug becomes an infinite retry against a healthy route.
  assert.equal(
    upstreamCapacityFailure(400, { error: { type: "invalid_request_error", message: "unknown field 'foo'" } }),
    false
  );
  assert.equal(upstreamCapacityFailure(400, { error: { message: "messages: field required" } }), false);
  assert.equal(upstreamCapacityFailure(400, {}), false, "no error object at all");
  assert.equal(upstreamCapacityFailure(400, null), false);
  assert.equal(upstreamCapacityFailure(400, { error: "a bare string" }), false);
});

test("upstreamCapacityFailure applies to 400 only, never to other 4xx", () => {
  // 401/403/404/422 really do blame the request, and a provider echoing "server_error" in one of
  // them must not win the job an unbounded retry loop.
  const body = { error: { type: "server_error", message: "Upstream request failed" } };
  for (const status of [401, 403, 404, 409, 422, 429]) {
    assert.equal(upstreamCapacityFailure(status, body), false, `status ${status} must stay terminal`);
  }
});

test("parseRetryAfterSeconds parses integer and HTTP date headers", () => {
  assert.equal(parseRetryAfterSeconds(null), null);
  assert.equal(parseRetryAfterSeconds({ headers: new Headers() }), null);

  const numHeaders = new Headers({ "retry-after": "42" });
  assert.equal(parseRetryAfterSeconds({ headers: numHeaders }), 42);

  const resetReqHeaders = new Headers({ "x-ratelimit-reset-requests": "15" });
  assert.equal(parseRetryAfterSeconds({ headers: resetReqHeaders }), 15);

  const futureDate = new Date(Date.now() + 30_000).toUTCString();
  const dateHeaders = new Headers({ "retry-after": futureDate });
  const parsedSec = parseRetryAfterSeconds({ headers: dateHeaders });
  assert.ok(parsedSec >= 25 && parsedSec <= 35);
});

test("parseRetryAfterSeconds parses duration strings like 7.66s and 2m59.56s", () => {
  const groqSeconds = new Headers({ "x-ratelimit-reset-tokens": "7.66s" });
  assert.equal(parseRetryAfterSeconds({ headers: groqSeconds }), 8);

  const groqMinutes = new Headers({ "x-ratelimit-reset-requests": "2m59.56s" });
  assert.equal(parseRetryAfterSeconds({ headers: groqMinutes }), 180);

  const groqMs = new Headers({ "retry-after": "500ms" });
  assert.equal(parseRetryAfterSeconds({ headers: groqMs }), 1);

  const groqHours = new Headers({ "retry-after": "1h2m3s" });
  assert.equal(parseRetryAfterSeconds({ headers: groqHours }), 3723);
});

test("parseRetryAfterSeconds parses Airforce rate limit error message payload", () => {
  const airforceBody = {
    error: {
      message:
        "Global rate limit exceeded (1 requests per second). Try again in 1.0 seconds. Your next guaranteed response is in 119 seconds. Or upgrade at api.airforce - discord.gg/airforce",
      type: "rate_limit_exceeded",
      param: null,
      code: "429",
    },
  };
  assert.equal(parseRetryAfterSeconds({ headers: new Headers() }, airforceBody), 1);
  assert.equal(
    parseErrorMessageRetryAfter(airforceBody.error.message),
    1
  );
});

test("parseErrorMessageRetryAfter parses various provider rate limit messages", () => {
  assert.equal(
    parseErrorMessageRetryAfter("Rate limit reached. Please try again in 20s."),
    20
  );
  assert.equal(
    parseErrorMessageRetryAfter("Rate limit reached. Please try again in 2m30s."),
    150
  );
  assert.equal(
    parseErrorMessageRetryAfter("Rate limit exceeded. Please wait 15 seconds before retrying."),
    15
  );
  assert.equal(
    parseErrorMessageRetryAfter("Too many requests, retry after 45 seconds"),
    45
  );
  assert.equal(
    parseErrorMessageRetryAfter("Your next guaranteed response is in 119 seconds"),
    119
  );
  assert.equal(parseErrorMessageRetryAfter("Non-rate limit error"), null);
  assert.equal(parseErrorMessageRetryAfter(""), null);
  assert.equal(parseErrorMessageRetryAfter(null), null);
});
