# LLM dispatch proxy

`citypods-llm-dispatch-proxy` is the Phase R/R10 rate-limited LLM dispatch Worker. It accepts
OpenAI-shaped chat-completion requests, persists them in a private R2 bucket, and the per-minute
Cron Trigger claims at most one ready request before calling the configured OpenAI-shaped upstream
route. The upstream response is persisted beside the request, so the caller can pick it up on a later
scheduled run without a GitHub Actions runner waiting for provider pacing.

The HTTP API is intentionally asynchronous:

1. `POST /v1/chat/completions` with a bearer token returns `202` and a `Location` header. This is an
   asynchronous queue API, not a synchronous LiteLLM `completion()` endpoint.
2. `GET /v1/requests/{id}` returns `202` while pending and returns the upstream OpenAI-shaped JSON
   with `200` after completion.
3. `GET /v1/models` advertises the configured provider-qualified model (`MODEL_ID`).
4. `GET /healthz` is an unauthenticated liveness check and does not inspect R2.

`stream: true` is rejected. The Worker never returns provider error bodies, request prompts, API keys,
or other upstream payloads in logs. Queue records contain the request and generated response, so the
R2 bucket must remain private and should have a lifecycle rule appropriate for the catalog's retry
window.

## Configuration

Create the bucket named by `r2_buckets.bucket_name`, then set the runtime secrets:

```bash
npx wrangler r2 bucket create citypods-llm-dispatch
npx wrangler secret put DISPATCH_AUTH_TOKEN
npx wrangler secret put UPSTREAM_API_KEY
```

`UPSTREAM_BASE_URL`, `UPSTREAM_CHAT_PATH`, `UPSTREAM_MODEL`, `UPSTREAM_REQUEST_MODEL`, `MODEL_ID`,
`PROVIDER_NAME`, and the retry/pacing limits are Wrangler variables. The upstream base URL must use
HTTPS and is maintainer-controlled; request fields cannot select a different provider, model, or URL.
`MODEL_ID` is the public/provider-qualified route accepted from the Python LLM backend. The Worker
forwards `UPSTREAM_REQUEST_MODEL` when set, otherwise the suffix of `UPSTREAM_MODEL`. That allows a
deployment to call a provider's OpenAI-compatible endpoint directly (the current Mistral deployment)
or to call an OpenAI-compatible LiteLLM Proxy, where `UPSTREAM_REQUEST_MODEL` can retain the full
`provider/model` route. Provider-native wire-format translation remains LiteLLM's responsibility;
the Worker deliberately does not reimplement provider clients.

The `control/dispatch.json` R2 object is a small CAS-protected rate gate. The request claim also uses
R2 conditional writes, so overlapping Cron invocations cannot dispatch the same request or exceed the
configured interval. A stale `processing` request is returned to the claimable set after
`PROCESSING_TIMEOUT_SECONDS`; transient upstream failures use bounded exponential backoff and stop
after `MAX_ATTEMPTS`.

Deployment is path-scoped by `.github/workflows/llm-dispatch-worker-deploy.yml` and uses the same
Cloudflare deployment secrets as the existing media proxy.
