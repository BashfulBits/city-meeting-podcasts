# LLM dispatch proxy

`citypods-llm-dispatch-proxy` is the Phase R/R10 rate-limited LLM dispatch Worker, extended
(review/41) to route multiple providers/accounts through one Worker. It accepts OpenAI-shaped
chat-completion requests, persists them in a private R2 bucket, and a per-minute Cron Trigger claims
a bounded batch of ready requests, ranks each request's canonical model's candidate routes against a
**per-route/per-account R2 ledger**, and calls the chosen route's own provider endpoint. The upstream
response is persisted beside the request, so the caller can pick it up on a later scheduled run
without a GitHub Actions runner waiting for provider pacing.

The HTTP API is intentionally asynchronous:

1. `POST /v1/chat/completions` with a bearer token returns `202` and a `Location` header. This is an
   asynchronous queue API, not a synchronous LiteLLM `completion()` endpoint. **There is no synchronous
   path through this Worker** — a caller that needs a same-run answer (e.g. `citypods/discovery/
   classify.py`) must call the target provider directly instead (`LLMRequestPolicy.require_direct=True`
   / `allow_dispatch_overflow=False`, the default for any route that also offers `direct`).
2. `GET /v1/requests/{id}` returns `202` while pending and returns the upstream OpenAI-shaped JSON
   with `200` after completion.
3. `GET /v1/models` advertises the Worker's configured default route (`MODEL_ID`).
4. `GET /v1/queue/estimate?model=<canonical model>` reports the current pending backlog for that model.
5. `GET /healthz` is an unauthenticated liveness check and does not inspect R2.

`stream: true` is rejected. The Worker never returns provider error bodies, request prompts, API keys,
or other upstream payloads in logs. Queue records contain the request and generated response, so the
R2 bucket must remain private and should have a lifecycle rule appropriate for the catalog's retry
window.

## Multi-provider routing (review/41)

Provider/account/route data is **not** Wrangler config any more — it's compiled from
[`config/provider_limits.yml`](../../config/provider_limits.yml) into `src/dispatch_limits.json` by
[`scripts/compile_llm_limits.py`](../../scripts/compile_llm_limits.py) (run automatically by
`.github/workflows/llm-dispatch-worker-deploy.yml` before every deploy — that default invocation makes
no network call, so the deployed artifact always matches what's committed). Each provider block gives
its own `api_base`/`chat_path` and one or more `accounts` (each with an `api_key_env` naming a Worker
secret); each route in `routes:` maps a canonical `model` (the same string the Python `ROUTES` table
uses) to one `provider`/`account_id`/`upstream_model` plus its own `rpm`/`rpd`/`tpm`/pricing. Two
accounts of the same provider (e.g. `project_primary`/`project_secondary` for Gemini) compile to two
separate `route_id`s with **independent ledger entries** — this is what makes account rotation real:
exhausting one account's window rolls dispatch onto the other rather than blocking the model.

`dispatchOne`'s route selection ranks a request's candidate routes free-before-paid, then cheapest,
checks each against `state/dispatch_budget.json` (R2, per-`route_id` minute/day counters, optional
cost fields, `inflight` reservations, and a reactive `blocked_until`, mirroring
`citypods/compute/llm_budget.py`'s shape), and reserves the first with real
capacity. A request whose caller disallowed paid and whose model has no free route left fails
permanently; one that's merely temporarily out of capacity is left `pending` for a later tick — never
silently dispatched on a fallback default.

**Auto-discovery of a provider's live model/pricing catalog (OpenRouter today) is opt-in and
maintainer-run only** — `python scripts/compile_llm_limits.py --discover openrouter` (or bare
`--discover` for every provider with a `discovery.endpoint` block), which rewrites
`provider_limits.yml` for you to review and commit. It is never invoked by the deploy workflow.

## Configuration

Create the bucket named by `r2_buckets.bucket_name`, then set the runtime secrets: the dispatch bearer
token, plus every `api_key_env` named in `config/provider_limits.yml`'s `accounts` blocks.

```bash
npx wrangler r2 bucket create citypods-llm-dispatch
npx wrangler secret put DISPATCH_AUTH_TOKEN
npx wrangler secret put MISTRAL_API_KEY
npx wrangler secret put GEMINI_API_KEY
npx wrangler secret put GEMINI_API_KEY_SECONDARY   # second Gemini account/project, same free tier shape
npx wrangler secret put DEEPSEEK_API_KEY
npx wrangler secret put OPENROUTER_API_KEY         # only if OpenRouter routes are in use
```

`PROVIDER_NAME`/`UPSTREAM_MODEL`/`MODEL_ID` are Wrangler variables describing only the Worker's
*advertised default route* (`GET /v1/models`, and the fallback when a request omits `model`) — they no
longer resolve credentials or an upstream URL for a real dispatch; that is entirely
`dispatch_limits.json`'s job, keyed by whichever route the ranked selection above actually chose. A
missing/invalid value here fails fast rather than silently defaulting to Mistral's shape (a real
credential-disclosure bug fixed in review/41 — see its §2 for the incident).

`FAST_UPSTREAM_TIMEOUT_SECONDS` and `UPSTREAM_TIMEOUT_SECONDS` define the fast and long request lanes.
`MAX_EXECUTION_SECONDS` is the invocation deadline, `FINALIZATION_RESERVE_SECONDS` is held back for
terminal request/ledger writes, `BATCH_CONCURRENCY` and `MAX_TOTAL_REQUESTS` bound per-run parallelism
and volume, and `LEASE_DURATION_SECONDS` is the renewable single-runner lock.
Before dispatching, the Worker checks the candidate's lane timeout against the effective deadline and
requeues work that cannot fit; timeout telemetry reports `unknown duration` when the provider did not
provide a trustworthy duration. A timeout is therefore a loud, retryable signal rather than a silent
loss of a long-context result. The cron lease (`locks/cron.json`) carries a per-invocation owner token,
and every batch renews it with CAS, so a slow invocation's eventual release can never delete a later
invocation's lease.
The Worker marks a record pending before its upstream call and retains a matching `inflight`
reservation in `state/dispatch_budget.json`. If an invocation crashes, the request stays pending for
the next tick and the reservation reaper removes its expiring concurrency claim, so either artifact
cannot permanently block work.

Deployment is path-scoped by `.github/workflows/llm-dispatch-worker-deploy.yml` and uses the same
Cloudflare deployment secrets as the existing media proxy.

## Scheduling lanes

Each scheduled invocation claims up to `MAX_TOTAL_REQUESTS` records in batches of
`BATCH_CONCURRENCY`, subject to the route ledger and the effective deadline. Fast-lane requests are
ordered before long-lane requests so a backlog of quick calls drains first, except that a fresh run
reserves one first-batch slot for a long request that can fit. With the deployed values, the long-lane
start window is `820 - 720 - 20 = 80` seconds: starting it in that first batch lets the remaining
batch slots drain fast work concurrently, while a later start is requeued with the loud
`deadline_guard` scheduler status for the next invocation. This keeps the Worker from starting work
it cannot finish before the 15-minute Cron Trigger ceiling without allowing fast backlog to starve
long-context work indefinitely.
