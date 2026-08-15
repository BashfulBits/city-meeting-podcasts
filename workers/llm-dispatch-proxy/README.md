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
or other upstream payloads in logs. For a non-2xx provider response, it stores a bounded diagnostic
inside the private canonical R2 request record: structured error fields, response content type and byte
length, bounded JSON field names, the nested path used to find common error fields, and an 8 KiB
`body_preview` for terminal failures. Oversized terminal bodies retain only that prefix with
`truncated: true` and the observed byte count. Retryable responses retain the metadata and parsed
fields but do not retain the body preview unless the retry budget is exhausted, keeping repeated 429
responses from creating noisy records. Queue records contain the request, response, and bounded failure diagnostic,
so the R2 bucket must remain private and should have a lifecycle rule appropriate for the catalog's
retry window. Scheduled logs include request ID, route ID, upstream status, and structured provider
error code/status without the provider message.

Scheduled `llm_dispatch_batch` logs also include bounded wall-clock profiling in milliseconds. The
batch profile covers ready-marker listing, budget loading, candidate preparation, the ledger write,
and batched ready-marker cleanup (`marker_delete_ms`). Durable rate usage is committed once, before
the upstream calls, so there is no reservation to release afterwards and no `reservation_release_ms`
phase; a route's concurrency ceiling is enforced in memory for the batch. Each result profile covers
route selection, canonical request claim, credential resolution, upstream fetch, response parsing, R2
persistence, and total dispatch time. These timings are diagnostic wall-clock measurements—not
Cloudflare CPU-time measurements—and are kept out of the provider-facing completion response. The
upstream batch concurrency and total-request limits are unchanged; `BATCH_CONCURRENCY` and
`MAX_TOTAL_REQUESTS` remain available for later tuning.

The Worker imports a generated runtime-only catalog rather than the richer Python route catalog:
duplicate route arrays, direct structured-output metadata, and provider discovery settings are
omitted. `routes_by_id` is keyed by route ID and `model_routes_map` holds route-ID strings, so a
lookup that goes stale fails visibly rather than resolving to a different route. (An earlier
revision stored routes as fixed-position arrays referenced by numeric index; that saved ~0.02 ms of
once-per-isolate parse time and cost a silently-dropped route field, so it was reverted — see
[`review/43`](../../review/43-llm-dispatch-cpu-reduction-plan.md).) Legacy upstream selectors are
folded into the compiled model-alias map. The source YAML remains the single source of truth; the
deploy workflow recompiles and checks the generated artifacts.

## Staying inside the Free-plan cron CPU budget

Cron triggers on the Workers Free plan allow **10 ms of CPU** per invocation. Waiting on R2 or on a
provider is not charged, but *moving bytes across the R2 binding is*, and every operation carries
fixed per-call overhead. The useful unit of optimization is therefore **how many R2 operations an
invocation performs and how many bytes they carry** — not its wall-clock profile, which is dominated
by upstream generation time and routinely reads in seconds while CPU stays in single-digit
milliseconds.

The current shape, measured with `bench/cpu-profile.js` against a 60 KB canonical record and a
16-marker backlog:

| Invocation | R2 operations | Bytes moved |
| --- | --- | --- |
| Idle tick (no ready work) | 4 | 0.3 KB |
| Dispatching one request | 10 | 147 KB |

Rules that keep it there:

- **Never re-read what this invocation just wrote.** The cron lease hands back an ETag on write;
  renew and release CAS onto that ETag and fall back to a re-read only when the CAS fails. The lease
  is a single-writer guard, so the fallback is rare.
- **Do not re-prove ownership that cannot have changed.** The lease is taken for 840 s against an
  820 s run deadline, so it is renewed only once a run has consumed half of it.
- **Cache anything ICU touches.** Constructing an `Intl.DateTimeFormat` measures ~43 µs against ~2 µs
  to reuse one, and route selection computes a zoned day key for every rate-limited route it
  considers. `zonedDateKey` was once the single largest self-time function in the scheduled path.
- **Only write durable state that nothing else can reconstruct.** Minute/day rate windows are derived
  from the current time and re-rolled on every load, so they are never worth an R2 write on their
  own; clearing an `inflight` entry left behind by an older Worker version is, because nothing else
  removes it.
- **Parse marker metadata lazily.** The lookahead lists 16 markers so a throttled route does not stall
  the queue head, but a run that dispatches the first one should not parse the other fifteen.

`test/index.test.js` pins the operation counts above, so a change that reintroduces a round trip
fails there rather than in production. Run `node bench/cpu-profile.js` for V8 self-time per function;
it is a comparative signal only — deployed, version-tagged `cpuTime` remains the acceptance metric.

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
and volume, `LEASE_DURATION_SECONDS` is the renewable single-runner lock, and
`DEFER_IN_PLACE_SECONDS` (default 600) is how long a route may be pacing before the scheduler
relocates its queue head rather than skipping it in memory. A head whose route frees up sooner than
this is left where it is and re-examined next minute, which costs nothing; rewriting it costs four
R2 operations, more than dispatching a request. Raise it to tolerate longer route cooldowns without
index churn; lower it to move blocked work out of the lookahead window sooner.

`PROFILE_SAMPLE_RATE` controls detailed successful-dispatch profile logging (failures always retain
their profile; the default is 0.1). The cron lease provides single-writer coordination at every
batch size, so the Worker commits route/provider usage up front and never writes a temporary
`inflight` reservation; a route's concurrency ceiling is counted in memory for the batch instead.
Before dispatching, the Worker checks the candidate's lane timeout against the effective deadline and
requeues work that cannot fit; timeout telemetry reports `unknown duration` when the provider did not
provide a trustworthy duration. A timeout is therefore a loud, retryable signal rather than a silent
loss of a long-context result. The cron lease (`locks/cron.json`) carries a per-invocation owner token,
and every batch renews it with CAS, so a slow invocation's eventual release can never delete a later
invocation's lease.
The Worker marks a record pending and commits its durable rate usage to
`state/dispatch_budget.json` before the upstream call. If an invocation crashes, the request stays
pending for the next tick and is retried; usage already committed is never rolled back, so a crash
over-counts rather than under-counts against a provider's limits. Any `inflight` entry left by an
older Worker version is reaped on load — including one written without an expiry, which would
otherwise occupy a concurrency slot that nothing clears.

Deployment is path-scoped by `.github/workflows/llm-dispatch-worker-deploy.yml` and uses the same
Cloudflare deployment secrets as the existing media proxy.

## Observability & Cloudflare AI Gateway

The Worker supports optional proxying through **Cloudflare AI Gateway** to provide real-time
time-series charts, filtering by provider, model, and HTTP response code (200, 429, 400, 500), error inspection,
and CSV exports in the Cloudflare dashboard.

To enable:
1. In Cloudflare Dashboard, go to **AI** → **AI Gateway** → **Create Gateway** with name `citypods-dispatch`.
2. Native providers (`google-ai-studio`, `mistral`, `groq`, `deepseek`, `openrouter`) work automatically.
3. For custom providers, navigate to your gateway's **Settings** / **Custom Providers** and provision each:

   | Custom Provider Name | Endpoint Base URL |
   |---|---|
   | `custom-siliconflow` | `https://api.siliconflow.com/v1` |
   | `custom-sambanova` | `https://api.sambanova.ai/v1` |
   | `custom-zai` | `https://api.z.ai/api/paas/v4` |
   | `custom-kilo` | `https://api.kilo.ai/api/gateway` |
   | `custom-opencode` | `https://opencode.ai/zen/v1` |

4. Automatic deployment: The deploy workflow (`.github/workflows/llm-dispatch-worker-deploy.yml`) automatically
   injects `CLOUDFLARE_ACCOUNT_ID` from GitHub Secrets and pairs it with `AI_GATEWAY_ID: "citypods-dispatch"` from `wrangler.jsonc`.
5. If neither `AI_GATEWAY_BASE_URL` nor `CLOUDFLARE_ACCOUNT_ID` is set, requests default directly to each provider's standard endpoint.

## Scheduling and migration

Every pending canonical record has a compact marker at `ready/<eligible-time>-<priority>-…`. R2
lists keys lexicographically, so each cron invocation lists a fixed lookahead of 16 markers and
uses their compact custom metadata to skip a temporarily blocked provider/model without reading
each marker body. Legacy markers without metadata fall back to a body read. It reads canonical
requests only for viable candidates or records that must be requeued, then dispatches one request
per scheduled run by default. Queue selection therefore stays bounded in queue depth — including
10,000 historical or pending records — rather than parsing the first 1,000 prompts. The deployed
Free-plan configuration is `BATCH_CONCURRENCY=1` and `MAX_TOTAL_REQUESTS=1`; the marker lookahead
keeps the scheduled CPU path bounded while allowing a later eligible route to make progress when
the queue head is blocked.

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
