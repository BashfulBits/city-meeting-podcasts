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
4. `GET /healthz` is an unauthenticated liveness check and does not inspect R2.

`GET /v1/queue/estimate` and `POST /v1/queue/reindex` return `410`: neither endpoint may walk the
full R2 request history from a Worker. Use metrics/logs for operational queue depth, and use the
offline migration below when upgrading old records.

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
uses) to one `provider`/`account_id`/`upstream_model` plus its own `rpm`/`rpd`/`tpm`/pricing. A route
may declare `model_key` when its provider-qualified selector is an alias for a shared logical model;
the compiler emits `model_aliases` and the Worker canonicalizes chat requests and ready records
before route lookup. Two
accounts of the same provider (e.g. `project_primary`/`project_secondary` for Gemini) compile to two
separate `route_id`s with **independent ledger entries** — this is what makes account rotation real:
exhausting one account's window rolls dispatch onto the other rather than blocking the model.

`dispatchOne`'s route selection ranks a request's candidate routes free-before-paid, then cheapest,
checks each against `state/dispatch_budget.json` (R2, per-`route_id` request pacing/day counters,
optional cost fields, `inflight` reservations, and a reactive `blocked_until`, mirroring
`citypods/compute/llm_budget.py`'s shape), and reserves the first with real
capacity. A request whose caller disallowed paid and whose model has no free route left fails
permanently; one that's merely temporarily out of capacity is left `pending` for a later tick — never
silently dispatched on a fallback default.

RPM is interpreted as a continuous pace: `rpm: 60` means the next submission is eligible one second
after the previous reservation, rather than allowing a burst of 60 requests at a wall-clock minute
boundary. A provider-level `rpm` in `config/provider_limits.yml` is shared by every model and account
for that provider; a route-level `rpm` remains an additional model/account gate. The compiler carries
provider settings into `dispatch_limits.json` and the Python route catalog, so the pacing values are
configuration data rather than provider-specific Worker logic.

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

## Scheduling and migration

Every pending canonical record has a compact marker at `ready/<eligible-time>-<priority>-…`. R2
lists keys lexicographically, so each cron invocation lists a fixed lookahead of 16 markers and
uses their routing metadata to skip a temporarily blocked provider/model. It reads canonical
requests only for viable candidates or records that must be requeued, then dispatches up to four
independent requests concurrently. Queue selection therefore stays bounded in queue depth —
including 10,000 historical or pending records — rather than parsing the first 1,000 prompts. The
deployed Free-plan configuration is `BATCH_CONCURRENCY=4` and `MAX_TOTAL_REQUESTS=4`; the marker
lookahead keeps the scheduled CPU path bounded while allowing independent routes to make progress.

Priority is `submit_next`, then fast, then long for work with the same eligibility timestamp. A
request blocked by a provider ledger is moved to that route's next eligible time, so it cannot repeatedly occupy the
head marker. The canonical request remains authoritative: a stale marker left by a crash is repaired
on observation and cannot cause early dispatch.

This index is not inferred by cron. After deploying this version, run the following once for records
already in the bucket; it reads `requests/` outside Workers and writes a marker for every canonical
`pending` record. It is safe to run again.

The preferred production path is **Actions → Reindex LLM dispatch queue**: leave **Write ready
markers** unchecked for its dry-run first, then run it from `main` with that checkbox enabled. The
workflow uses the existing R2 secrets and serializes reindex runs. The local equivalent is:

```bash
python scripts/reindex_llm_dispatch_queue.py --bucket citypods-llm-dispatch --dry-run
python scripts/reindex_llm_dispatch_queue.py --bucket citypods-llm-dispatch
```

The command needs `CLOUDFLARE_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY` (and
optionally `R2_ENDPOINT`). Leave old `pending/` markers alone; the new Worker ignores them and they
can be removed later by an explicit lifecycle/cleanup operation after the migration is verified.
