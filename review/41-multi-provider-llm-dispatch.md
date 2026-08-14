# review/41 — Multi-Provider LLM Dispatch Worker & Per-Route Ledger

**Maturity: L3 · authored 2026-08-06 · extends [`review/27`](27-llm-backend-and-provider-routing.md) §9
(R10, the original single-Mistral dispatch Worker) and [`review/33`](33-llm-quota-cost-scheduler.md)
(R13, the Python-side scheduler) · implemented and corrected in the same pass as a full audit of
PR #1132, `feat/llm-dispatch-expansion`**

## §1. Why this doc exists

PR #1132 extended `workers/llm-dispatch-proxy/` and `citypods/compute/` to route Gemini/Mistral/
DeepSeek/OpenRouter through one Worker with multi-account API key rotation, deadline-based paid-route
elevation, and lowest-cost provider routing — a real extension of R10/R13's scope, not covered by
review/27 or review/33 as written. A full audit against that PR's own implementation plan, review/27,
review/33, and CodeRabbit's review found the first implementation pass didn't achieve several of its
own stated goals and introduced a handful of serious bugs (credential disclosure, double-reservation,
a silent architecture reversal). This doc records the **corrected** design, per AGENTS.md's doc-
lifecycle contract ("Implemented (PR merged) → mature the breakout, update ARCHITECTURE.md"). It is
authored and stamped in the same pass as the fix, not written speculatively in advance of it.

## §2. What the first pass got wrong (kept for the record — don't re-introduce these)

1. **Credential disclosure.** `resolveProviderCredentials` fell back to a single Mistral-shaped
   `cfg` for URL/model whenever a route's own value was merely present-but-unused in the wrong order,
   so a Gemini/secondary-account request's API key could be sent to `api.mistral.ai` with a Mistral
   model string. Root cause: the Worker had no per-provider URL of its own to prefer — `UPSTREAM_BASE_URL`
   etc. were single-model Wrangler vars left over from R10's original single-Mistral design.
2. **Double-reservation on dual-transport routes.** `llm_scheduler.py::_owner_for` checked the scalar
   `route.transport`, while selection/dispatch (`is_dispatch` in `llm.py`) had moved to checking the
   `route.transports` tuple. A Gemini route (`transport="direct"`, `transports=("direct","llm-dispatch")`)
   dispatched over the Worker got a fresh UUID ledger owner instead of the deterministic `recipe_hash`
   the Worker's own `idempotency-key` dedupes on — a retry before settlement missed
   `find_inflight_owner()` and reserved a second time for one underlying provider request.
3. **A silent architecture reversal.** Adding `"llm-dispatch"` to Gemini's `transports` combined with
   `is_dispatch`'s unconditional `any(...)` meant *any* caller whose backend had `dispatch_url`
   configured at all — not just one that wanted the Worker — routed Gemini calls through it instead of
   calling Gemini directly. `citypods/discovery/classify.py` (city discovery) is exactly the caller
   review/33 §5 designed around a synchronous, same-run completion requirement
   (`allow_paid=False`, no `deadline_at`, "acts on the classification result immediately"), and its
   workflow (`city-discovery.yml`) happened to set `LLM_DISPATCH_URL` for an unrelated reason. The
   Worker's dispatch transport is *always* asynchronous by construction (a `202` plus a later poll,
   review/27 §9.3) — there is no code path that makes it complete within the same process — so this
   silently turned every city-discovery classification into a same-run failure
   (`ClassificationDeferred`, the whole cycle re-tried tomorrow) even when Gemini's direct free tier had
   ample quota, and additionally throttled whatever *did* route through the Worker down to its shared
   global pacing gate (see #4). Nothing about this reversal was a stated design decision anywhere.
4. **No real per-route rate enforcement.** `dispatch_limits.json` carried real per-route `rpm`/`rpd`/
   `tpm`/`account_id` data, but the Worker's only pacing gate was a single *global* one-request-per-
   `DISPATCH_INTERVAL_SECONDS` (60s) counter shared across every provider/model/account combined —
   sized for Mistral's ~1 RPM, the Worker's original single-route reason for existing. Every route
   added by this PR was silently governed by that same 1-req/min ceiling regardless of its own real
   allowance (Gemini's actual 10 RPM, DeepSeek's concurrency), and nothing chose *which* account to use
   beyond "first in YAML order" — so "unified rate-limit enforcement" and "multi-account key rotation"
   were both dead claims relative to what the code actually did.
5. **Route selection ignored deadline/cost entirely.** The Worker picked the first paid-eligible route
   in declared order; `deadline_at`/`allow_batch` were plumbed through the payload and stored on every
   record but never read by the dispatch loop. "Deadline-based paid route elevation" and "lowest-cost
   provider routing" were payload fields with no behavior behind them.
6. Plus a set of independent stability bugs, all confirmed and fixed in the same pass (full list in the
   PR's CodeRabbit thread): unbounded queue scan (a `scanned` counter that was declared but never
   incremented), an unconditional cron-lease release race, no upstream fetch timeout (a lease could
   expire mid-call and double-dispatch the same record), a lost-create-race that could answer `202` for
   a request never actually persisted, an unpinned `pyyaml` install in the deploy workflow, and a live
   OpenRouter network call running unconditionally inside that same deploy job.
7. `asr-quality-ingest.yml` was left in a botched partial-refactor state by an unrelated change bundled
   into the same PR (dead `strategy.matrix` + dead `$NUMBER`-referencing steps alongside a new
   loop-based step) — unrelated to the LLM dispatch design but fixed in the same pass since it was
   entangled in the same diff and made the workflow's `ingest` job fail on every single run.

## §3. Corrected design

### §3.1 Provider/account config carries its own transport facts

`config/provider_limits.yml` gives every provider (not just Mistral) its own `api_base`/`chat_path`,
carried straight through into the compiled `workers/llm-dispatch-proxy/src/dispatch_limits.json` (no
transformation — the Worker reads `dispatch_limits.json.providers[route.provider]` directly):

```yaml
providers:
  gemini:
    api_base: https://generativelanguage.googleapis.com/v1beta/openai  # Google's own OpenAI-compat endpoint
    chat_path: /chat/completions
    reset_timezone: America/Los_Angeles
    accounts:
      - id: project_primary
        api_key_env: GEMINI_API_KEY
      - id: project_secondary
        api_key_env: GEMINI_API_KEY_SECONDARY
```

Gemini's official OpenAI-compatibility endpoint (confirmed real) means no LiteLLM Proxy hop is needed
for this route — review/27 §3's "may point at a provider's own OpenAI-compatible endpoint" option,
applied. `resolveProviderCredentials(env, route)` resolves `api_key_env`/`api_base`/`chat_path` purely
from the *chosen route's* provider config; there is no fallback to any other provider's shape, and a
missing secret/URL is a hard, loud failure (`saveFailure(..., "credential_resolution_failed")`), never
a silent default. Account resolution also **fails closed** on a route naming an unknown `account_id`:
a route that declares one must match it exactly, never silently fall back to the provider's first
configured account — a hand-authored YAML typo on a secondary route must not send that account's
requests under a *different* (e.g. primary) account's key while still charging the secondary route's
ledger entry. The `accounts[0]` fallback applies only to a route that omits `account_id` altogether.
Credentials are resolved **before** any ledger reservation is made — a missing secret or malformed
provider config must never spend a route's RPM/RPD/TPM window, since nothing was actually attempted.

### §3.2 A real per-route/per-account R2 ledger supersedes the global pacing gate

`state/dispatch_budget.json` (R2) mirrors `citypods/compute/llm_budget.py`'s `RouteLedger` shape
(minute/day window keys, `blocked_until`), keyed by **`route_id`, not canonical model** — this is what
makes account rotation real: `gemini_3_flash_preview_primary` and `..._secondary` are separate
`route_id`s with independent ledger entries, so once primary's window is spent, ranking naturally falls
through to secondary's still-fresh one. Single-writer by construction (only the cron-lease holder ever
mutates it), so a conditional R2 put with no CAS-retry loop is sufficient — unlike the Python-side
ledger, which many concurrent GitHub Actions runners write to.

`dispatchOne`'s route selection (`selectRoute` in `index.js`) mirrors `select_route`'s ranking in
`citypods/compute/llm_scheduler.py`: free before paid, then lowest declared per-token cost. It tries
free candidates for the claimed record's canonical model in that order; the first with real ledger
capacity right now wins. If none does:

- **No route at all configured for the model**, or **only paid routes exist and the caller disallowed
  paid** → permanent failure (`saveFailure`, distinct reason codes `no_configured_route`/
  `no_eligible_route`). This will never resolve on a later tick, so it must not sit `pending` forever.
- **A free route exists but is temporarily exhausted, and paid isn't allowed or elevation doesn't
  apply yet** → requeued as `pending` without counting an attempt (`no_capacity`); a later tick retries.
- **Paid is allowed and free capacity genuinely can't help** — either no free route exists at all for
  this model, or the soonest free route's predicted reset (`nextRouteReset`, mirroring
  `_next_quota_reset` in `llm_scheduler.py`, including that function's own documented "only offer a
  window's rollover as a candidate when that window is the *actual* binding axis" fix) is at or after
  the record's `deadline_at` — elevate to the cheapest available paid route now.

No route in the current static table actually mixes free and paid candidates for one canonical model
(Gemini/Mistral routes are uniformly free, DeepSeek uniformly paid), so the elevation path is exercised
by unit tests against synthetic route fixtures today, not live traffic — forward-looking, not dead code
(`selectRoute`'s Worker-side ranking and per-route/account rotation are exercised by real traffic; only
the free/paid-elevation *branch specifically* awaits a mixed-tier model).

The reservation write itself is retried (a bounded 3 attempts, reloading the ledger fresh each time)
before giving up — the cron lease makes this invocation the ledger's sole writer under normal
operation, so a conflict should not happen, but **the record is never dispatched without a durable
reservation**: exhausting the retries requeues it (`no_capacity`) rather than proceeding unreserved,
which would let a later tick spend the same capacity again.

### §3.3 Direct-first: a dual-transport route only dispatches on explicit opt-in

`LLMRequestPolicy.allow_dispatch_overflow: bool = False` (new). A route that offers only dispatch
transports (Mistral — no direct alternative exists) always dispatches, unchanged. A route that also
offers `direct` (today only Gemini) dispatches over the Worker **only** when the caller explicitly sets
`allow_dispatch_overflow=True`; otherwise it always goes direct, restoring review/33 §7's original
decision ("only build a dedicated Gemini Worker later, and only if real usage shows it's needed") as
the default rather than an accident of whether `dispatch_url` happened to be configured.

`citypods/discovery/classify.py` now states `require_direct=True` explicitly on its policy — not
relying on the new default alone — so its same-run-completion requirement is stated in code, not
implied. `city-discovery.yml` no longer sets `LLM_DISPATCH_URL`/`LLM_DISPATCH_AUTH_TOKEN` at all; that
workflow never needed dispatch reachability, and its presence was what let §2 item 3 happen silently.

`allow_dispatch_overflow` is the sanctioned lever for a *future* caller that wants Gemini's secondary-
account capacity once the primary direct route's own ledger is exhausted — nothing production sets it
yet.

**Correction (same pass, caught by a second CodeRabbit review of this fix):** the first version of
this fix computed `is_dispatch` directly in `llm.py`, re-deriving a transport preference from
`route.transports`/`policy.allow_dispatch_overflow`/`self.config.dispatch_url` independently of what
`llm_scheduler.py::_owner_for` used for the ledger-owner decision — the same two-places-deciding-
the-same-thing shape that caused the original double-reservation bug (§2 item 2). Concretely: a
version of `_owner_for` keyed on `route.transports` (the route's *capability*, e.g. Gemini always has
`"llm-dispatch"` in it) rather than the transport *actually selected for this call* still reserved
every dual-transport route as if it always dispatched — including a call that correctly went
`direct`. That deduped two genuinely concurrent direct Gemini calls sharing a `recipe_hash` into one
ledger reservation, undercounting real API calls — the mirror-image of the original bug.

The fix: `select_route` (`llm_scheduler.py`) now resolves the transport **once**, as
`SelectionResult.transport`, using the same rule (`_selected_transport`: `direct` wins unless a
dispatch alternative exists *and* `allow_dispatch_overflow=True`, or there's no `direct` alternative
at all). Both `_owner_for` and `llm.py`'s dispatch-vs-direct branch (`is_dispatch = selection.transport
in {"mistral-dispatch", "llm-dispatch"}`) read that same resolved value — they cannot disagree because
there is only one place the decision is made.

### §3.4 Discovery is opt-in and generalized, never live in deploy

`scripts/compile_llm_limits.py`'s default invocation (what the deploy workflow runs) is pure
YAML→JSON, no network call — deterministic, and the deployed artifact can never differ from the
reviewed one. `--discover [provider ...]` (maintainer-run only, bare form covers every provider with a
`discovery.endpoint` in its YAML block) fetches and appends newly discovered routes, then rewrites
`provider_limits.yml` for the maintainer to review and commit. The gate and the fetcher/transform
registry (`DISCOVERY_FETCHERS`/`DISCOVERY_TRANSFORMS`) are provider-name-keyed, not OpenRouter-specific
— a future provider that gains a real discovery endpoint (Mistral/Gemini/DeepSeek `GET /models`-style)
plugs in the same way, gated identically.

## §4. Known, accepted limitations (not fixed in this pass)

- **Worker throughput is still capped at one dispatch per cron tick**, regardless of a route's own
  `rpm`. The per-route ledger fixes *correctness* (never exceeding a route's real limit, and rotating
  accounts over time/ticks) but not *burst throughput* — a route with `rpm=10` still only gets one
  request/minute through the Worker until a future pass loops dispatch within a single invocation
  (Cloudflare's CPU-time-excludes-fetch-await property, review/27 §9.2, makes this feasible; not built
  here to keep this pass scoped to correctness). Account rotation is still meaningful under this
  ceiling for RPD-scale exhaustion (the realistic way an account's quota fills), just not for
  RPM-scale bursts.
- **`allow_batch` remains plumbed but inert.** No route has a confirmed server-side batch-submission
  API (review/33 §9's own open item); nothing reads this field to change routing. Not a regression —
  matches the project's existing "advisory field until the real capability exists" pattern
  (review/27 §5.3/§8.2).
- **`PROCESSING_TIMEOUT_SECONDS`/`processingTimeoutSeconds`** remains Wrangler config but, as before
  this pass, nothing in `dispatchOne` actually reads it to reclaim a stuck `processing`-marked record —
  the Worker's synchronous, single-invocation dispatch flow has never needed that reclaim path (a
  crashed invocation simply leaves the record `status: "pending"`, which the next tick picks up
  normally). The README no longer claims otherwise.
- **The `mistral-large-latest` → `mistral-large-2512` rename (unrelated to the multi-provider work,
  bundled in the same PR) invalidates no durable artifact.** Only ephemeral coordination-state entries
  (`state/llm_budget.json` inflight rows, `state/llm_deferred/*.json` records) keyed on the old model
  string become unreachable after deploy — both are already documented as loss-tolerant
  (review/33 §10.4/§10.6: re-initializes to zero, worst case is one over-count window before the
  provider's own throttling corrects it, never a lost artifact).
- **Resolved 2026-08-13 — date-ordered ready index replaces queue scans.** Pending records now have a
  compact `ready/<eligible-time>-<priority>-…` marker. The cron reads a fixed lookahead of compact
  markers and their routing metadata, independent of `requests/` depth; it never falls back to a
  legacy scan. The Free-plan deployment dispatches one independently paced request per tick
  and skips a blocked provider/model when a later marker has capacity. `GET /v1/queue/estimate` is
  deliberately retired rather than retaining a second unbounded Worker scan, and the offline
  `scripts/reindex_llm_dispatch_queue.py` creates markers for pre-index pending records. The marker
  body contains routing policy but never the prompt; canonical state is re-read before dispatch, so a
  stale marker from a crash is safe and self-repairs.
- **The two ingest workflows (`asr-quality-ingest.yml`, `llm-tag-review-ingest.yml`) are not
  replay-safe.** Both persist a decision (`citypods transcript-quality ingest-review` /
  `citypods llm-evaluation ingest`) and then comment on and close the source issue as two separate,
  non-atomic steps; a GitHub API failure between the two leaves a persisted decision with no comment/
  close, and a subsequent retry of the same issue re-runs the persist step. This predates the
  multi-provider work (the persist-then-comment-then-close shape is unchanged by this pass) and is a
  real, if unlikely, "heavy lift" fix (CodeRabbit) — deduplicating the persisted decision and detecting
  an already-completed confirmation action needs its own design, not a quick patch bundled into this
  redesign's scope.

## §5. Tests

`tests/test_compute_llm_scheduler.py`/`tests/test_compute_llm.py` cover: the corrected `_owner_for`
(dual-transport routes reserve under `recipe_hash`, direct-only routes under a fresh UUID), a
dual-transport route preferring `direct` without `allow_dispatch_overflow`, the same route dispatching
and reserving correctly *with* it, and (an independent bug caught by tightening a test double from a
permissive `lambda **_:` to a strict one per CodeRabbit) that dispatch-only policy fields
(`allow_paid`/`allow_batch`/`submit_next`/`deadline_at`) never leak into a direct LiteLLM call's kwargs.

`workers/llm-dispatch-proxy/test/index.test.js` covers: non-Mistral credential/URL resolution (the
credential-disclosure regression test), per-route-ledger-driven account rotation across a real 10-request
Gemini burst, `no_capacity` (temporary, requeued) vs. permanent-failure (`no_configured_route`/
`no_eligible_route`) outcomes, deadline-based paid elevation against synthetic mixed-tier route
fixtures (§3.2), owner-token cron-lease release semantics,
  the bounded ready-index lifecycle (including a 10,000-record queue that uses one list, a fixed marker
  lookahead, and only the selected canonical requests), blocked-provider bypass, four-route batch
  dispatch, and retirement of the historical reindex/estimate scans.

## §6. Acceptance

A request for any configured canonical model resolves to a concrete route whose own provider/account
credentials and URL are used for the real upstream call — never another provider's. Exhausting one
account's ledger window rotates dispatch onto a configured sibling account without caller involvement.
A route that also offers `direct` is never silently routed through the Worker; a caller must opt in.
`allow_paid=False` against an all-paid model fails the record permanently rather than dispatching
anyway. The deploy workflow never makes a live network call. `asr-quality-ingest.yml` and
`llm-tag-review-ingest.yml` process their resolved issue list exactly once each, sequentially, and the
job fails loudly (not silently) if any issue's ingestion fails.
