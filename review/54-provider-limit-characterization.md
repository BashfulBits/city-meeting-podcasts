# Provider limit revalidation and burst/refill characterization

**Status: Deferred, L2.** Tracking issue: [#2264](https://github.com/BashfulBits/city-meeting-podcasts/issues/2264).

**Maintainer scope correction — 2026-10-11.** Review/48 remains new-route discovery,
maintainer-approved additions, bounded discovery-time request-cap searches and dead-route removal
with existing recovery. Periodic empirical quota/cap characterization and dispatcher redesign are
separate future work. Existing Worker adaptation and provider-configured quotas remain the
operational baseline. The historic L3 designation below does not authorize its implementation or
activation now. A fresh bounded L3 contract and prioritization are required before resuming it.

Future scope includes periodic proof/refinement of input/output/combined request caps, RPM/TPM
measurement, RPD/TPD/monthly evidence, exact model/shared scopes, token/request burst, overflow and
refill semantics, provider-canonical provenance/re-check, production evidence, and consistent
Python/v2 policy with existing adaptive feedback preserved. Discovery-time admitted/refused size
search remains in [review/48 §8.13](48-provider-catalog-reconciliation.md). Explicit quota-pool
configuration and dispatcher enforcement need a separate bounded design, rather than becoming
a dependency of completing route maintenance.

Input/request capacity above TPM suggests oversized-request support; how subsequent jobs refill,
accumulate credit or incur debt must be tested separately. Revalidate request caps over time even
when they are at/below TPM. Preserve unknown versus provider-declared, empirically measured and
operationally configured values. A provider-canonical mismatch triggers an independent source
re-check, not silent replacement. Daily/monthly exhaustion may bound limits with complete scoped
period accounting; no deliberate daily/monthly exhaustion experiment is planned.

## Retained prior design and local prototype notes

The following preserves useful earlier discussion and acceptance ideas. It is historical design
material, not active review/48 scope, implementation status or permission to change runtime state.
Local quota prototypes have not been integrated into a live workflow and should not be bundled
into the narrowed route-maintenance delivery.

### 8.12 Experimental request ceilings and TPM topping-off — L3 contract, 2026-10-10

The maintainer requested that calibration use experimental data to adjust the request sizes the
Worker will admit, including routes whose provider accepts an individual request larger than its
published tokens-per-minute (TPM) rate. The goal is to learn the largest safe per-request input
over time, while keeping the provider's refill rate and the model's context window as separate,
unchanged facts. This extends §8.10–8.11, which currently use calibration evidence to offer
reviewed catalog cap changes; it does not authorize changing catalog TPM, model window, route
allowlist, weekly budget, output authority or quota scope.

**Separate the limits.** There are three different quantities:

1. The model's input context window is a model capability. Calibration does not change it.
2. Provider TPM is the token-bucket refill rate and remains sourced from reviewed provider limits.
   A larger accepted request does not demonstrate a higher TPM.
3. The per-request hard input ceiling is a request-size constraint. Authenticated experiments may
   establish a verified-success lower bound and, only when an endpoint has a reliable typed
   context-size rejection parser, a rejection upper bound. The Worker may adjust its effective
   request ceiling from those bounds without rewriting YAML or compiled catalogs.

The current Worker already refills route and shared-provider token budgets at configured TPM and
can wait for a reservation larger than one TPM interval when the bucket has accumulated enough
tokens. It separately applies `hard_input_ceiling`, and bounds the shared provider bucket to five
TPM windows. Therefore a route may top off to a request above TPM only when its effective hard
ceiling permits that size, the route and provider buckets have enough balance, and existing
shared-scope and rolling gates pass. This does not establish that every provider permits such a
burst; `context_limit > tpm` alone must never infer it.

**Use observed provider counts and typed outcomes.** For every experiment, retain the exact
reserved amount and provider-reported input/output/total counts when available. The parser must
feed the observed count, not the local tokenizer estimate, into calibration brackets and Worker
settlement. Record only allowlisted, payload-free response evidence: outcome class, typed HTTP or
provider error class, request/response timing, documented rate-limit headers and retry timing.
Never persist prompts, completions, API keys or arbitrary response bodies. Classify at least:

- verified success, with actual provider token count;
- explicit context/request-size rejection, only for a provider-specific recognized condition;
- rate-limit/TPM rejection (for example 429), which is not a context ceiling;
- transport, authentication, quota-scope or otherwise unknown failure.

An explicit context-size rejection narrows the request-size upper bound but does not erase the
largest verified successful request. A rate-limit rejection updates rate-limit/backoff evidence
and the applicable token balance/cooldown; it must not lower the learned Worker request ceiling or
configured TPM. A separately typed request-count rejection may guide probe size under the
maintainer-approved search addendum below, but it never becomes a context bound or runtime ceiling.
Unknown outcomes move neither bound. Success above TPM proves only that the
observed request size was accepted with that experiment's bucket state; it is not by itself an
exact maximum or proof of sustained throughput.

**Learn the safe request ceiling over time.** Calibration keeps a search interval bound to the
route and physical quota scope: the highest request size verified successful using actual provider counts,
and the lowest reliably classified legal context ceiling, if one exists. Search/refinement follows
§8.10's accepted 0.5% or 128-token stopping goal and existing weekly job budget; it adds no
blanket safety margins at each stage. If no context rejection has been observed, later approved
experiments may stretch the successful bound upward, subject to existing per-run and weekly
allowances, route/input authority, the shared bucket's five-TPM-window ceiling and normal provider
admission. After a verified context rejection, probe within the narrower bracket. The Worker raises
its effective request ceiling only to a provider-count-verified success; it lowers that ceiling
only when an unambiguous context-size rejection establishes a tighter bound. Until then, retain the
current configured ceiling/behavior. Expired, stale-catalog, mismatched physical-scope, partial or
failed-run evidence cannot raise runtime authority; §8.12.D defines conservative rejection guards.

This is automatic adjustment of the Worker's runtime request ceiling from authenticated experiment
evidence, not automatic editing of provider catalogs or provider quotas. Each result must link to
its admitted attempt, route, physical quota scope, parser version, deployed catalog digest and
actual provider count. Persist it through a typed, idempotent settlement to the existing durable
Worker authority, with bounded row/read/write cost and outage-safe rollback and recreation
behavior. A workflow artifact alone is not runtime authority. Competing runs cannot overwrite
newer evidence; accepted results are monotonic by evidence time/version and stale runners are
fenced. Provider counts exceeding a reservation are useful observed-count evidence but do not
retroactively increase admission or weekly charges.

**Evidence for topping-off behavior.** The canary tests more than a single large request. For an
approved route/scope, controlled requests should demonstrate (a) success for an actual
provider-counted request larger than one TPM interval after sufficient idle/refill and (b) a later
request submitted only after the Worker replenishes the corresponding token deficit. Capture
documented rate-limit headers and timings where available. A rate-limit response remains rate
evidence and is never labeled a context failure. Keep attempts inside existing canary/manual
budgets and shared-scope gates; do not create synthetic Worker jobs or override provider quotas.
A successful above-TPM request supports a bounded burst-capability classification for that exact
physical scope and observed size, not a blanket provider-family rule.

**Activation and failure behavior.** This new ceiling-update authority remains disabled until a
separately reviewed activation specifies route/scope, dimensions, bounded experiments and rollback.
The already-approved input pilot remains available under its existing authority. Runtime ceilings
may move only within the model context limit, existing compiled/transport maximum and provider's
established shared bucket bound. No experiment changes TPM, context window, RPD/TPD, concurrency,
paid policy, output enablement or route eligibility. Missing or conflicting evidence fails closed to
the last authenticated ceiling. Rollback disables experimental ceiling updates and returns to the
last reviewed static ceiling; it does not erase evidence, weekly charges or rate-limit state.
Telemetry must expose the active ceiling, evidence version/source, last observed actual count and
rejection class without request content.

#### 8.12.A Authority and delivery — L3 contract

The maintainer authorized L3 maturation on PR #2263; implementation is tracked in
[issue #2264](https://github.com/BashfulBits/city-meeting-podcasts/issues/2264).
Implementation follows design merge, in one
prerequisite PR for durable receipt/publication primitives and one integration PR for parsers,
search and runtime overlay. Both remain offline/disabled for production ceiling updates. Separate
reviewed activation enables automatic updates for named input routes only; the existing input pilot
and manual allowance are unaffected. Output and broader rotation keep #2222/#2223 gates.

This contract deliberately replaces the earlier reviewed-choice-only rule for the **runtime input
request ceiling** on activated routes. Static YAML cap changes continue through explicit `/apply`
choices. A valid completed experiment can publish a higher verified input bound automatically,
without two distinct weekly runs; two weeks remain required for static catalog offers. This is the
requested automatic experimental adjustment, with no extra tokenizer margin. Existing producer and
Worker token-ratio calibration remains authoritative for translating ordinary-job estimates.

Implementation authority uses three server-owned constants, initially empty: `CONTEXT_SETTLE_ROUTE_IDS`,
`CONTEXT_CEILING_UPDATE_ROUTE_IDS` and `CONTEXT_BURST_TEST_ROUTE_IDS`. Each is a subset of the existing
approved input-probe routes with verified physical scope. No dispatch input or environment string
can widen them. Settlement can be enabled before updates, allowing observation without a production
ceiling change. A route's existing `CONTEXT_INPUT_CEILINGS` bounds every experiment and update; raising
that experiment ceiling remains a reviewed activation change. No context probe bypasses TPM/RPM,
TPD/RPD, provider pause/drain, session/call/token budgets or the shared five-window cap.

**Publication trust boundary.** The existing Worker bearer credential authenticates the reconciler,
not the provider. The Worker cannot independently prove a provider response or GitHub artifact.
Consequently the trusted main-workflow reconciler must authenticate completed successful run/artifact
provenance using `verified_context_history` before publication; the Worker independently verifies
that the published values match a durable receipt from a real charged admission. No arbitrary artifact
upload, CLI input or caller-supplied token count without such a receipt is authority. There is no new
credential or Worker-side GitHub network fetch. Document this boundary in ARCHITECTURE and tests.

#### 8.12.B Exact observations, parsers and time/count units

Keep `ContextObservation` v1 and existing static-cap history compatible. Add
`RequestCeilingObservation` v1 in `evidence.py`, paired with its existing context observation by
`context_observation_digest`. Retain only paired records inside the existing bounded artifact schema;
the publication verifier authenticates both record digests in the same successful artifact. Its fields are: `schema_version=1`, `pool` (`scheduled|manual`),
`week_start`, `run_id`, `attempt_id`, `route_id`, `catalog_digest`, `quota_scope`, `identity_digest`,
`request_digest`, `parser_version`, `context_observation_digest`, `outcome`, `http_status`,
`reported_input`, `reported_output`, `reported_total`, `reported_cached_input`,
`reported_context_ceiling`, `count_basis`, `rate_count_basis`,
`provider_tpm`, `remaining_tokens`, `reset_tokens_ms`, `retry_after_ms`, `started_at_ms`,
`finished_at_ms`, `admission_not_before_ms` and `refill_pair_attempt_id` (nullable).

Counts are nullable nonnegative safe integers; a verified success requires positive input plus
known output/total with input+output=total, input counting basis and verified sentinel/tail result
from the existing parser. Never infer actual counts from the requested output, byte count, local
estimate or arbitrary error-message numbers. A count above either admitted reservation records an
`overshoot` outcome, stops the route and cannot publish a new ceiling; charges remain consumed.
The existing fixture-estimate correction uses that count on a future attempt. Cached input is
nullable and cannot exceed reported input; parse only exact `usage.prompt_tokens_details.cached_tokens`.
`rate_count_basis` is `total_uncached` only when that field is exactly zero, otherwise `unknown`.
Missing cached-token detail never implies a zero count. Context success may still be useful without
a known rate-count basis. See [Groq prompt caching](https://console.groq.com/docs/prompt-caching). Output-only,
combined-window-ambiguous and reasoning-ambiguous responses cannot supply this input authority.

`outcome` is exactly `success|context_rejection|rate_limited|inconclusive|transport|overshoot`.
Reject unknown/extra fields. Payload is at most 2 KiB in canonical JSON. Digests are 64 lowercase
hex characters; route/run/attempt syntax uses existing validators. Scope and identity are server-
checked against admission and the current raw catalog. UTC timings are safe integers; duration is
0..120,000 ms and start cannot precede admission or finish follow settlement. Provider timing fields
are evidence rather than a clock authority; scheduling uses the Worker's admission timestamp.

Groq chat is the first settlement-supported parser (`groq-chat-ceiling-v1`). Preserve only parsed
`x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens`, `x-ratelimit-reset-tokens` and `retry-after`.
Decimal token headers must be integers; reset durations accept the documented h/m/s sequence with
fractional seconds, converted by ceiling to milliseconds; Retry-After accepts decimal seconds.
Bound headers individually to 128 bytes, reset to 86,400,000 ms and retry to 3,600,000 ms. Missing or
malformed headers become null, never guesses. Groq's token limit header is TPM, whereas its request
limit/reset headers refer to RPD; do not treat request headers as RPM. See the
[Groq rate-limit contract](https://console.groq.com/docs/rate-limits).

**Manual rerun diagnostics — 2026-10-11.** Extend the existing `ContextObservation` v1 additively so
manual artifacts explain otherwise ambiguous results without changing their evidence meaning.
Record a bounded `diagnostic_code`, HTTP status and finite HTTP/provider error classes, normalized
response media type, capped response-byte count, admission/start/finish timestamps, total and
first-byte duration, and the parsed Groq request and token limit/remaining/reset headers plus
decimal `Retry-After`. Preserve no raw headers, prompt or completion. For manually triggered runs
only, retain a UTF-8 excerpt of an HTTP 4xx/5xx response body up to 4 KiB after replacing the
active Authorization value and generated probe prompt (including its JSON-escaped form). Mark
excerpts truncated when the source or sanitized excerpt exceeds the limit. Never retain successful
response bodies, and never use retained provider text for classification or authority; it is
diagnostic evidence for the maintainer reviewing the manual artifact. Scheduled runs retain no
body. The separately approved Groq parser below examines only the bounded in-memory response;
artifacts retain its validated integer, not the message. Generic diagnostics cannot change identity,
context bounds or Worker authority.
Parser reason codes distinguish preflight/credential failure, transport timeout/error,
oversize/invalid/incomplete SSE, HTTP failure, invalid JSON/envelope/usage/counts, refusal or tool
call, missing completion, sentinel mismatch and verified fixture completion. A structured
`context_size_candidate` diagnostic is informational only: it does not create a context rejection
bound unless a separately reviewed typed parser does so. Old observations load with empty diagnostic
defaults. Apart from the typed probe-search hint below, diagnostics do not change identity, context
search bounds or authority. This prepares the next manual Groq attempt to distinguish an accepted
completion from a response the current parser cannot interpret; it does not activate §8.12
settlement, runtime ceiling updates, or TPM topping-off behavior.

HTTP 429 is `rate_limited`, even when its message contains a token limit or "request too large".
Without the exact structured Groq `error.type=tokens` / `error.code=rate_limit_exceeded` response,
the documented TPM message and a matching `x-ratelimit-limit-tokens` header, the exhausted dimension
is unknown. Even a typed request-count rejection cannot establish a context maximum, a one-minute-only
bucket or a permanently reduced burst allowance. Current Groq/OpenRouter context parsers do not
establish a trustworthy numeric context rejection. Initial production support therefore learns
successful lower bounds, with `context_rejection` publication disabled.
Gemini, BeatAPI JEV, OpenRouter quota settlement and other endpoints explicitly return unsupported
for this new authority until a separately reviewed parser/scope contract is added. The protocol and
pure reducer support a context-rejection capability for future parsers and fixture tests, but no
production allowlist entry can enable it without the documented parser. Unknown 400/413 errors
remain inconclusive. This removes any requirement to invent a size-error parser during implementation.

**Provider-neutral bidirectional probe policy — maintainer direction 2026-10-11.** The search
reducer applies the same direction rules to every provider, while each provider adapter classifies
its own response semantics and count basis. A status code alone is never a numeric bound. Keep rate
rejections separate from input, output and combined-context rejections; a transient quota response
must never become a model context limit.

For a typed rate-limit response, first wait for the documented reset or `Retry-After` (or the longer
Worker cooldown), then issue a distinct newly admitted probe. If the response identifies the attempted
token count and rate threshold, retain a rate bracket separate from the context bracket and downshift
when the attempt exceeds that threshold. An untyped 429 in a manual run gets one same-shape retry
after cooldown; if it repeats, defer without creating a numeric bracket. A documented daily quota
error defers until its reset. Choose a reset only when the corresponding remaining counter says that
quota is exhausted; a token-rate retry must not wait for an unrelated requests-per-day reset. If
multiple counters are exhausted, wait for the longest applicable reset. Shared-capacity errors do
not masquerade as per-request size ceilings. Never replay an admission or reuse its charge.

For a typed context/input-size rejection, reduce the affected request dimension until it passes, then
grow from the highest passing actual provider count and bisect against the lowest comparable
rejection. Hold the other dimensions fixed while probing input or output. A provider that reports a
combined-window rejection must bracket actual input-plus-output total instead. Stop at the existing
0.5% or 128-token goal; a later run may stretch again to test newly available headroom. Only
provider-reported usage or an endpoint-documented count may update the passing bound. A rejection
without a comparable provider count can direct a conservative retry but cannot claim an exact
boundary. Providers without a reliable typed parser remain inconclusive for that constraint.

The observed Groq 413 body reported `Limit 8000, Requested 10488` with `error.type=tokens`,
`error.code=rate_limit_exceeded` and a matching documented token-limit header. Groq is the first
adapter for this policy: preserve only the validated requested-token integer and
`rate_limit_parser_version=groq-tpm-rejection-v1` in `ContextObservation` v1; never retain the
provider's message as search state. The reducer is provider-neutral; other adapters opt in only when
their endpoint contract and fixtures establish equivalent typed evidence. Scheduled runs record a
hint and defer the next probe to a later run. Unknown quota responses do not steer either bracket.
These searches change neither configured TPM nor the model context window or Worker authority, and do
not classify topping-off behavior without §8.12's separate paired-success/refill evidence.

#### 8.12.C Durable receipts, operations and outage bounds

Add two tables to the existing coordinator readiness contract; no new DO namespace or index:

- `context_ceiling_receipts`: `pool TEXT`, `week_start TEXT`, `attempt_id TEXT`, `run_id TEXT`,
  `route_id TEXT`, `result_digest TEXT`, `result_json TEXT`, `settled_at INTEGER`,
  `published_digest TEXT NULL`, `published_at INTEGER NULL`; primary key
  `(pool, week_start, attempt_id) WITHOUT ROWID`.
- `context_ceiling_state`: `route_id TEXT PRIMARY KEY`, `catalog_digest TEXT`, `quota_scope TEXT`,
  `identity_digest TEXT`, `parser_version TEXT`, `version INTEGER`, `state_json TEXT`.
  `state_json` is at most 8 KiB, retaining at most 16 observation/provenance references and bounds,
  burst classification, last update and effective ceiling. References store only receipt key/digest, count and admission
  time, not copied observations. At most 128 configured route rows.

Receipts retain the same eight UTC weeks as admitted attempts: at most 24 scheduled plus 24 manual
receipts per week (384 total). Prune receipt rows for the same one expired pool/week removed by
session start, using exact clustered keys; at most 24 receipts per pruned pool/week. No new cleanup
loop, global historical scan or secondary index. Live ordinary-job paths never prune or write them.
Readiness uses additive table creation with existing migration/accounting conventions. Corrupt or
oversized rows withhold the overlay and surface a diagnostic; do not silently recreate evidence.

Extend `POST /v2/dispatch:reserve` with two operations, through `validateReserveRequest` and the
existing authenticated index handler (no new endpoint):

1. `context_settle`: `{operation, run_id, catalog_digest, pool, week_start, attempt_id, result}`.
   Match the pool's active session, raw catalog and exact admitted receipt, including request digest,
   route, dimension, token reservations and physical scope. Require input dimension, settle authority,
   admission no older than 205 seconds and a live session. Settlement of a charged attempt at the
   immediately previous UTC week boundary remains possible only through its original live session;
   it never starts or charges a new week. Identical replay returns `already_settled`; changed content
   returns `result_conflict`. Stage a bounded receipt only; success does not yet change dispatch.
   For 429, conservatively retain all charged tokens and atomically extend the route's existing
   `blocked_until` to max(current, now+max(60,000, parsed retry delay)); no refund, quota increase,
   guessed bucket reset or ordinary-job failure-counter increment. Scheduled probing stops for
   this scope; a manual run may make a distinct, newly admitted probe only after the cooldown and
   within its existing run budget. A provider-specific typed parser may steer a rate bracket only
   when its count basis and physical scope match the admitted route (see the provider-neutral
   bidirectional probe policy above). Groq is the first adapter; each additional adapter requires
   its own contract-backed parser and fixtures before its rejection can steer a search.
2. `context_publish`: `{operation, run_id, catalog_digest, entries}` with 1..16 entries. Each entry
   supplies `{pool, week_start, attempt_id, result_digest, source_run_id, artifact_id,
   artifact_sha256, head_sha, context_observation_digest}`. Caller authenticates the completed
   successful source run's default-branch main ancestry, repository, exact workflow/event, artifact
   identity/digest/size and paired observation through existing evidence code. Worker requires a
   currently live publisher session in either pool, exact staged values, an unexpired receipt and
   matching catalog/scope/parser authority. It never accepts an unstaged observation, unfinished
   source run or result supplied solely in the publication body. The publisher may be a later run.
   Canonically sort by server admission time then `(pool, week_start, attempt_id)`; deduplicate before
   reduction. Atomically mark all newly published receipts and update each affected state once.
   Replay is write-free; conflicting provenance is rejected before mutation. Entries may include
   non-success diagnostics but only the permitted success/rejection classes move ceiling bounds.

Expose nullable receipt/state summaries through existing context pause-status and route
`calibrationStatus`; raw request/response content never appears. Typed Python methods are
`settle_context` and `publish_context_results`. Neither retries an ambiguous mutation automatically:
read exact receipt status after a lost response, accept its matching stored digest or defer. Lost
settlement leaves a charged attempt with no usable proof; lost publication must never resubmit a
provider request. Manual diagnostic artifacts remain uploaded even when settlement/status fails.

All mutations use `_transactionSync`, `_contextRowBudget` and existing row accounting. Budget
preflight covers receipt/state rows, route cooldown, expired receipt cleanup and scheduler accounting.
No writes may survive a mutation/accounting-flush failure. Acceptance budgets: first settlement
at most 3 billed writes including accounting (one additional route write for 429); publication
of N new receipts touching R routes at most N+R+1 writes; replay zero. Settlement reads at most
one attempt, one session, one receipt and one existing route ledger. Publication reads at most
16 receipts, 16 state rows and two session rows plus fixed accounting, never historical jobs.
Pruning reads/deletes at most 24 exact receipt rows in the selected pool/week. Measure real workerd
billed units; if the measurements exceed these bounds, stop under AGENTS rather than ship an
unreviewed larger cost. Existing weekly charges are never refunded, reset or transferred.

#### 8.12.D Runtime reducer, identity and pacing precedence

Use a pure JS reducer in a new `src/context-ceilings.js`, with matching Python planning reducer in
`limits.py` and shared JSON fixture cases. Admission records, not caller timestamps, order results.
Version increments once per publication with new authority. Replay cannot increase version. Late
older evidence is retained as a diagnostic but cannot replace a newer bound. Keep the largest
non-overshooting success `L`; for future supported rejections, keep the lowest explicit legal input
ceiling `U`. A failure's attempted estimate never becomes `U`. Initial Groq state has no `U`.

For a route with an existing static hard ceiling H, a success produces
`max(H, min(L, reviewed_experiment_ceiling))`; a small experiment must never reduce an existing
larger H. The ordinary request still fits the model window minus its output reservation. For a route without H, never turn a finite observed lower bound into a new restrictive
maximum: preserve existing uncapped behavior until a typed rejection supplies U. With supported
rejections, cap at U; if retained successes contradict U, freeze increases, mark uncertainty and
apply min(previous effective ceiling,U) until fresh compatible evidence repairs the interval.
No arbitrary extra margin or tolerance is added. Active learned ceilings have tolerance zero;
existing tokenizer-ratio and input+reserved-output window checks remain intact.

Keep `_dispatchLimits()` and `_contextCatalogDigest()` based on the unchanged compiled catalog.
Add `_effectiveDispatchLimits(now)` that overlays **only** `hard_input_ceiling` and its tolerance on
approved routes. Cache bounded state reads per request; ordinary claims load at most 128 state rows
by route primary keys, in chunks within `MAX_SQL_BOUND_PARAMS`. Apply one consistent effective
catalog in `_claimDispatchLimits`, coarse `routesEligibleFor`, capacity/pacing checks, producer
calibration preflight and structural rescue compatibility. The rescue digest must include the
ceiling evidence version so bounded queue-index rechecks can revisit jobs after a ceiling rises.
Do not fabricate a removed-model rescue or terminal result from temporary rate state. Provider
pause filtering still wraps the same effective catalog. No change to job hashes, policy, aliases,
model context/output limits or estimator ratios. Existing ordinary-job `prompt_cap_estimate` is an
independent constraint and remains binding; never clear it from a generic canary success.

Require exact current catalog digest, physical quota scope, parser/identity and enabled update
route. Runtime raising evidence expires 90 days after server admission; its overlay then returns
to the reviewed static ceiling. Expiry does not delete charges/history or activate another route.
In a future rejection-capable parser, a tightened guard must not expire upward silently: retain
min(static ceiling, learned U) for the same physical identity until explicit reviewed reset or
compatible newer publication. Catalog/scope changes invalidate raising authority; a conservative
rejection guard cannot migrate to a different model/account. Changing such an identity requires the
activation PR to state the old guard's disposition. Status exposes the expiry/reset requirement.

Pacing stays reservation-based: wait until the full input-plus-output reservation is available,
then debit it using existing route/provider logic; refill at the unchanged effective TPM before
another call. Do not switch to "balance merely positive" admission, which could overspend another
large request. No new five-window clamp on route buckets; existing shared-provider clamp remains.
Native Gemini retains its trailing-minute rule; it is never relabeled bucketed by this implementation.
Provider headers may extend cooldown but cannot mint local token credit or change refill slopes.

**Producer parity.** Extend the existing `/v2/calibration` response with raw `catalog_digest`,
physical scope, `ceiling_version`, effective ceiling, static ceiling and expiry, without changing
input-ratio fields. `LiteLLMBackend.dispatch_v2_calibration` validates these optional fields;
old Workers retain static behavior. Add a per-process bounded cache (at most 128 route/family
entries), sharing existing ratio lookups; authenticate the response with the existing Worker client
and require its raw catalog digest to match the local compiled dispatch catalog before using a
ceiling. A failed/stale lookup uses static ceilings; enqueue/claim remains the final authority. Cache entries
expire after 300 seconds or their authority expiry, whichever comes first; long-running producers
must not retain a ceiling through rollback for an entire process lifetime.

Add optional `dispatch_ceiling_overrides` to `select_route` and `select_and_reserve` in
`llm_scheduler.py`. Choose the route's physical transport before its hard-ceiling check and apply
the validated override only when that selected transport is `dispatch_v2`. Direct, v1 and batch
transports keep their existing catalog caps. Wire `LiteLLMBackend` selection and structural recovery
context through that mapping. `structural_recovery_context` accepts the validated dispatch overlay
and includes ceiling version in its lineage fingerprint for v2 resubmission eligibility, while
retaining stable recipe hashes and existing non-v2 routing. This is advisory route selection, not
authority to bypass the Worker's current ceiling. No global mutation of `ROUTE_REGISTRY` or model
capabilities. Cache invalidation does not retry an already-admitted job or change its recipe hash. Existing tag/prelabeler chunk sizing may remain smaller until its separate workflow
changes; do not silently enlarge producer batches or their output/token budgets in this work.

#### 8.12.E Search, burst classification and publication workflow

Use the existing 50% stretch without a rejection, 10% refinement around a previously bounded
maximum and 0.5%/128-token convergence rule. Preserve existing weekly/re-scan limits and uncertainty
restart semantics. Read published state before planning; one workflow shares its input/output
call budgets, with input first. Context-authority planning uses actual input counts, while burst
classification requires a known rate-count basis. Groq counts cached tokens differently; cached-
usage responses without an exact comparable rate count cannot prove above-TPM capability.

A `burst_verified` classification requires two distinct published compatible successes in the same
identity: both have non-cached actual total tokens strictly above the parsed provider TPM (not
compiled discounted TPM), consistent positive TPM headers and no reservation overshoot. Second
admission must occur after the first, with sufficient Worker-calculated refill and existing pacing
checks; both are charged separately. A single success is `burst_observed`; missing headers/count
basis is `unknown`. Rejection/429 cannot classify `one_minute_only`; that needs a future documented
provider parser, not a guessed behavior from an ambiguous failure. Classification reports measured
size and scope, not a provider-wide capacity. It does not raise bucket capacity or bypass the
five-window gate; automatic hard-ceiling updates may use individually verified successes even
before the informational two-call burst label is complete.

Extend the existing workflow with `context_publish_only` boolean, default false and mutually
exclusive with context-canary, due-only and discovery modes. This main-only dispatch authenticates
completed evidence, opens a short session in the existing manual pool, publishes at most 16 staged
observations and finishes without provider I/O, pause, token admission or allowance consumption.
It preserves the writer concurrency lock. Once a canary run completes successfully, a publish-only
run can apply its evidence immediately; no Monday/week-long wait or new provider calls are needed.
Scheduled and regular manual entrypoints also publish authenticated prior completed evidence before
measurement. Current-run observations are settled and uploaded, but cannot publish until their run
has completed successfully. A successful workflow containing an inconclusive/429 observation still
cannot turn that observation into success authority. Failed/partial artifacts cannot publish.

Discovery reuses bounded eight-week/16-observation/90-day history. Publish only newly staged,
authenticated results matching their receipts; sort identically to Worker reduction. During a
measurement run, a settlement or publication ambiguity abandons further optional probes while
preserving cleanup and artifacts. No automatic provider retries. The prior successful >TPM
observation can supply the next refill-pair reference only if its receipt and scope still match.
Existing route experiment ceilings may limit upward progress: report `activation_ceiling` and the
next desired target rather than silently raising them. This contract activates no new experiment.

#### 8.12.F File/function, verification and rollout plan

Permitted implementation changes, supplementing §8.11's file plan:

- `citypods/provider_catalog/evidence.py`: typed paired observations and successful completed-run
  authentication, canonical digest, bounded discovery and publication selections; old v1 history
  remains readable but cannot supply an unstaged runtime result.
- `rules.py`, `providers/groq.py`, `probe.py`: allowlisted response headers/timing through the existing
  cancellable child, Groq success/rate parser and explicit unsupported rejection capability. Keep
  existing 120-second/4-MiB limits, URL validation, no redirects and no raw artifact content.
- `limits.py`, `reconcile.py`: pure bracket/burst reduction, published-state planning, settle after
  each admitted result and publish earlier authenticated results; same shared budget and pause gates.
- `citypods/compute/llm_dispatch_pause.py`: typed settle/publish/status methods and bounded errors.
- `citypods/compute/llm.py`, `llm_scheduler.py`, `llm_deferred.py`: validated read-only calibration
  ceiling cache, optional dispatch-only selection override and versioned recovery fingerprint as
  specified in §8.12.D. Direct/v1/batch and recipe hashes remain unchanged.
- `scripts/reconcile_provider_routes.py`, `.github/workflows/provider-catalog-reconcile.yml`:
  publish-only mode, mutual exclusion, main/event guards and staged diagnostic artifact handling;
  existing pinned actions, writer lock and permissions retained.
- Worker `coordinator.js`, `protocol.js`, `index.js`, new `context-ceilings.js`: additive readiness,
  strict two-operation validation, settlement/publication/status, pure reducer and effective catalog
  integration at the named selection/preflight/rescue call sites. `pacing.js`, `routes.js` and
  `calibration.js` may change only to consume the same effective-ceiling/tolerance metadata and
  preserve pacing invariants; no new refill algorithm or estimate margin.
- Python context/evidence/limits/reconcile/pause and compute LLM/scheduler/deferred tests;
  Worker coordinator/protocol/index/schema-
  readiness/pacing/calibration tests and new `test/context-ceilings.test.js`; shared fixture
  `tests/fixtures/provider_catalog/context_ceiling_cases.json`; existing rows-written harness and
  README. Cover both pool schemas without modifying the normal jobs ledger.
- Lifecycle: review/48, review/11, ROADMAP, ARCHITECTURE and CHANGELOG in implementation PRs.

Required offline acceptance: exact header parsing/invalid bounds; 429 with misleading context
numbers; missing/cached/overshooting counts; old artifacts unsupported; wrong scope/digest/route;
future/out-of-order/stale results; same/conflicting replay; successful artifact paired to wrong
receipt; failed-run publication denial; two-call actual above-TPM/refill proof; TPM unchanged;
no mutation from unknown/rejected evidence; static vs learned ceiling, tolerance, model/output and
independent prompt-cap guards; v2-only producer overrides with direct/v1/batch unchanged;
claim/producer/rescue consistency and effective-digest invalidation;
90-day expiry, UTC boundary, shutdown/reset; manual/scheduled pool conservation; no calls in
publish-only mode. Test effective runtime increases without editing compiled catalogs.

Real local workerd must measure row bounds with 384 retained receipts plus 128 route states,
inject failure at every settlement/publication/prune mutation and accounting flush, lose responses,
replay, dispose/recreate and verify exact restored snapshots, charges, version and ceiling. Verify
multi-route batch atomicity and no idle/background writes. Run focused Python and Worker suites,
full offline Python/Worker acceptance, whole-repository Ruff, both compilers with unchanged outputs
and diff checks. No live calls are implementation acceptance.

Rollout: merge/deploy disabled prerequisite and integration through normal processes; inspect
bounded status. A separate activation PR names the input route, scope, experiment ceiling and
settlement/update authorities plus finite manual or existing scheduled budget. Run the separately
approved canary, authenticate completed evidence and publish-only dispatch, then inspect producer
preflight, request ceiling/version, refill admission and unchanged quota accounting. Rollback by
normal reviewed deployment removes the update-route authority; this immediately restores the
reviewed static ceiling while preserving receipts, charges and cooldowns. Settlement may remain
observation-only. Input evidence, output activation, broader rotation and removal gates remain
separate. Do not deploy, call providers or enable ordinary-job ceiling changes from this docs PR.

Do not modify provider/compiled catalogs, credentials, dependencies, namespace/binding config,
ordinary queue schema/indexes, job payload/hash policy, paid/free decisions, route RPM/RPD/TPM/TPD,
concurrency or output activation. No new endpoint, synthetic production jobs or extra per-week
calls. If code or real-workerd measurements disagree with this contract, use the AGENTS stop/ask
gate before implementation scope changes.

#### Groq manual probe stretch preparation — 2026-10-10

The successful manual run at 2026-10-10 23:40 UTC measured 6,986 input / 7,177 total tokens
against a 7,119 input / 256 output reservation. It did not test above Groq's documented 8,000 TPM.
The next 50% search target is approximately 10,479 actual input tokens, above the current 8,192
manual reservation ceiling. A narrow follow-up PR raises only this route's manual per-call server
authority to 16,384; scheduled scans stay at 8,192, ordinary jobs at 7,125 and output at 256.
The §8.12 runtime ceiling design is not implemented by this change. No new call or deployment is
included; #2221 stays open for stepwise evidence review.

### 8.13 Unified quota and dispatcher-pacing characterization — L2, 2026-10-11

The maintainer requests one recurring probe flow that characterizes request size, request/token
rate limits, burst/refill behavior and the behavior the job dispatchers should implement. The
flow should begin with one locally exercised route, expand to multiple routes within one provider,
then cover the current provider set before any GitHub Actions canary. This is a design extension to
§8.11–8.12; it does not authorize new live calls, quota exhaustion, production tuning or deployment
during local development.

**One orchestration, separate evidence axes.** A single weekly workflow and report should plan and
account for the phases together, but must keep independent evidence for input context, output cap,
combined context, RPM, RPD, TPM, TPD, request burst, token burst/refill, concurrency and reset
semantics. A success on one axis cannot silently advance another. Per-call limits and request-size
probes use actual provider-reported usage where available. Rate measurements distinguish successful
demonstrated load (a lower bound) from provider-declared limits and correctly typed rejections;
throughput alone is never mislabeled a hard ceiling.

Per-request and aggregate-window TPM evidence stay separate. Groq's current typed “Requested N
tokens against TPM limit” response describes a rejected request, which may indicate a request-size
ceiling or a special provider rule; it does not by itself prove aggregate minute-bucket exhaustion.
Only a parser that classifies an aggregate-window rejection for the same scope and window may bound
the minute-load search. A per-request result belongs to request-size search and retry/wait logic.

There should be one recurring maintenance workflow for these experiments, not parallel weekly rate
and context workflows. It may stage or defer phases within its bounded call/token budget and still
publish one combined report with explicit unknowns. The older manual rate-probe CLI may remain a
developer diagnostic while the weekly flow is brought to parity, but it is not a second source of
recurring catalog changes.

**Configuration and runtime are separate.** The Worker may adjust each job's provider-token estimate
from observed actual usage, pace/reserve jobs against the currently configured limits, wait for
refill when TPM credit is insufficient, and defer a request that exceeds a separately verified hard
input ceiling. Those runtime estimates and ledger balances are not edits to YAML or compiled route
limits. The weekly probe gathers evidence and may prepare a reviewed catalog/config change; it does
not have the Worker rewrite provider YAML. Rate-limit failures never lower a context ceiling, and a
request above one minute of TPM may be delayed rather than rejected when accumulated credit can
cover it.

**Scope is per metric, not a provider-wide boolean.** Every metric is bound to an explicit quota
identity established by provider documentation, exact response headers or reviewed configuration:
route/model, project, account/organization, or another provider-defined pool. One route may have
model-specific RPM/RPD and share organization-level ITPM/OTPM with other models. Aggregate route
samples only when their metric-specific scope IDs match. Never copy an aggregate bucket into
independent model capacities, sum different accounts, or assume that a provider name means one
shared pool. Unknown or conflicting scope remains unknown and cannot tune dispatcher authority.
Groq illustrates this mixed hierarchy: its [rate-limit documentation](https://console.groq.com/docs/rate-limits)
lists model-specific RPM/RPD/TPM/TPD, says quotas apply at organization level, and separately
documents organization-level ITPM/OTPM; project limits may further restrict individual models.
Its response headers expose request/day and token/minute figures, not all six dimensions. The
parser must retain those distinctions instead of converting every observation into a provider cap.

**Characterize dispatch, not just responses.** The combined evidence/report needs to establish:

- request ceiling and tokenizer error using actual input, output and total counts;
- request burst accepted after idle, token burst accepted after idle, and refill timing after
  consumption, with request and token buckets kept separate;
- whether each observed rate window is fixed, rolling or continuously refilling, its reset timezone,
  and how a typed rejection changes retry/backoff;
- concurrency and interleaving across routes that share an exact quota scope;
- the provider-count-based reservation and `not_before` behavior needed for the existing Python
  scheduler and Worker dispatcher, including when a request larger than one TPM interval must wait
  for accumulated credit and when it is permanently inadmissible.

These are separate from ordinary queue/runtime regression tests: those tests prove our local pacing
implementation against supplied limits, not that a provider still enforces those limits or scopes.
The legacy manual rate probe already samples RPM, back-to-back request burst, input size, typed
throttling and recovery, but is a separate flow and does not aggregate provider scopes, measure
TPD/RPD safely, or validate actual-token dispatcher accounting. The weekly catalog flow samples
some response limit headers, runs one optional rate sample per provider and separately performs
context probes; it does not currently converge the rate/burst axes. Existing scheduler/Worker unit
tests cover shared Python provider RPM/TPM and the Worker route/provider fixed-window/token-bucket
math, but do not test live provider quota scopes. Existing `observed_burst` catalog values are not
consumed by the dispatchers, so this proposal must not treat them as runtime burst controls.

The local same-case dispatcher comparison found provider-wide TPM policy differences. At 100
provider TPM, Python accepts a 600-token reservation and schedules it six minutes out, while Worker
pacing rejects it because the provider bucket has a five-window maximum. Even within that maximum,
the refill behavior differs: after a 400-token reservation, a second 200-token reservation waits
four minutes in Python's serial schedule, while Worker uses the remaining 100-token bucket balance
and waits one minute. Route-level oversized reservations wait for refill in both systems. This is
characterization only; resolve shared burst and refill semantics in an L3 dispatcher contract
before calibration can tune either runtime.

**Daily-limit safety.** Prefer authoritative per-model/account headers or provider quota endpoints
and preserve their declared reset units/timezone. Do not burn through a live RPD/TPD allowance just
to find its exact rejection edge. If no authoritative value exists, bounded successful daily use is
only a lower bound; report unknown ceiling and retain the reviewed runtime cap. A typed daily-limit
rejection may refine evidence only when the provider identifies its metric and scope unambiguously.
Ordinary provider/customer traffic can contend with the probe; unexplained 429s, reset changes,
external usage and ambiguous token-count basis are inconclusive rather than a lower capacity proof.

**Local-first progression and gates.** First build offline fixtures/simulation for one route using
the already supported Groq parser, and run them through `/usr/local/bin/citypods-env` without
provider calls. Then extend the same pure reducer to two or more Groq models, proving model-scoped
request quotas remain separate while any explicitly shared token scope aggregates once. Only after
that, add providers one at a time with documented units, parser fixtures, scope IDs and unsupported
cases; run the full provider catalog offline. Before GitHub Actions testing, produce a complete
provider/metric/scope/parser coverage report and a reviewed finite-call budget/rollback plan. Any
provider without enough authoritative or typed evidence stays report-only. Existing weekly/manual
quotas, pauses, provider ceilings and disable/rollback behavior remain binding until a later L3
contract names exact tests, files and activation limits.

**Offline reducer prototype status (2026-10-11).** `limits.py` now has a pure successful-load
reducer for route, project, provider-account and explicitly provider-wide scopes. It keeps RPM/RPD
request counts separate from TPM/TPD provider-counted tokens, preserves count basis and provider
window IDs, and computes observed sliding-window peaks across routes only when their exact scope
identity matches. Replay is idempotent and conflicting attempt evidence fails closed. A stateless
RPM/TPM planner stretches 50% above a successful lower bound, bisects only a caller-verified typed
rejection bracket, stops at the 0.5%-or-128-token goal (one request for RPM), enforces the supplied
per-run budget and refuses active RPD/TPD edge probing. Its typed upper bound must explicitly mean
`aggregate_window_limit`; a single-request TPM cap cannot be supplied. A strict adapter converts
verified context observations into request/token load events only when explicit scope/window
mappings and provider token-count basis are supplied. A local bridge now passes context observations
through the existing workflow artifact verifier before adapting them and retaining the newest six
runs/90 days. Tests start with Groq, including two route-specific responses; separate fixtures cover
routes sharing or not sharing account/project/org scopes. A shared JSON scenario drives matching
Python and Worker tests for the same oversized reservation and cross-route burst refill. That
comparison confirms their current provider-wide policy differences. This remains an offline
prototype: it does not serialize quota-load history into weekly artifacts, feed typed rejection
events into the rate planner, infer provider refill/reset behavior, or create a user-facing
comparison report. The next local step is to define explicit provider parser/scope/window contracts
and cover other configured providers; no unsupported provider is assumed to share Groq's semantics.

Current adapter inventory (offline registry inspection, 2026-10-11):

| Provider | Rate headers parsed by its plugin | Typed rejection parser | Scope currently emitted |
|---|---|---|---|
| Groq | RPD request headers; TPM token headers | Single-request TPM error | Route only |
| Airforce, BeatAPI, DeepSeek, Gemini, Kilo, NVIDIA | None | None | Unknown / report-only |
| OpenRouter, OrcaRouter, SambaNova, SiliconFlow, z.ai | None | None | Unknown / report-only |

“None” means no parser in this repo, not that the provider lacks a published quota or that it
cannot later be supported. Groq's current parser is insufficient for the full scope hierarchy:
the unified probe must not inherit its existing `route` labels as provider-wide evidence without a
reviewed, metric-specific scope mapping.

No production provider/compiled catalog, Worker pacing, credential, quota ledger or workflow is
changed by this L2 section. The design must mature to L3 before those changes or live probes.

#### 8.13.A Independent constraint search and scope experiments — maintainer direction 2026-10-11

The maintainer requires the unified adapter to answer every agreed constraint independently at
model and shared-provider scope, then make Python direct and v2 dispatch apply the same measured
semantics. Legacy `observed_burst` and `hard_input_ceiling` values are not experimental answers.
The Groq 7,125 hard input ceiling is an older estimate retained in configuration; neither this
proposal nor the current run derives that number from an 8,000-token boundary. Do not introduce
another percentage margin. Tokenizer correction, output reservation and any justified statistical
uncertainty must be explicit once, with no hidden multiplication across producer and dispatcher.

**Independent result matrix.** For every configured model and each candidate shared account/
project/organization pool, report all seven scalar metrics below, plus request burst, token burst,
refill, reset and concurrency. Each result carries scope identity, count basis, controlled
counterpart, highest successful bound, lowest typed rejected bound, declared value/source, evidence
age and status: unknown, lower bound, declared, experimentally bounded, conflicting, or documented
absent. Missing headers and a successful multi-model test never mean no shared limit. Independent
buckets observed under a finite test budget mean only that those tested counters appeared independent.
Provider-declared ceilings are useful evidence, but are distinguished from measured convergence.

| Axis | Experiment and comparable bound |
|---|---|
| Input maximum | Vary actual input; fix output reservation, response task and cache policy. A success must process start/middle/tail. Only a typed input rejection bounds this axis. |
| Output maximum | Fix small input; increase requested output and require actual generation. Record actual completion and reasoning counts and finish reason. Early natural stop is only a lower bound; accepting `max_tokens` alone does not prove generated capacity. |
| Total-request maximum | Vary input/output mixes on both sides of the suspected boundary. Verify whether the provider counts actual input plus reserved output, actual output, hidden reasoning or another documented basis. A shared boundary across mixes establishes a combined constraint; an input-only rejection does not. |
| TPM | Controlled repeated requests over at least two refill/reset intervals; sum comparable provider-counted tokens, excluding cached tokens when required. Record counter depletion, resets and typed aggregate exhaustion. A single-request TPM rejection cannot become a sustained-throughput upper bound. |
| RPM | Small fixed requests that cannot hit token limits; increase starts per measured interval, record successful starts and typed request-rate rejection. Preserve interval semantics and temporal distribution, not just count. |
| RPD | Declared daily limit, counter deltas from ordinary/probe traffic, reset timezone and observed daily rejection when naturally encountered. No deliberate daily exhaustion. |
| TPD | Same daily evidence, using the provider's billable/rate token basis and separate input/output daily pools where exposed. No deliberate daily exhaustion. |

Each search decreases after a comparable rejection until it passes, then increases and brackets
using actual counts. Keep the 0.5% or 128-token convergence goal (one request for RPM) and the
existing finite experiment budgets. Re-test previously bounded values over later weekly runs to
notice increases or decreases. A contradictory fresh result marks the old bracket stale and
requires baseline revalidation; never hide it by retaining only favorable observations. Output
and combined searches keep separate experiment controls rather than pretending one maximum
remains invariant when the counterpart changes.

**Scope characterization protocol.** A scope includes provider, credential account/project, model
identity where applicable and metric. Alias routes to the same upstream model use one identity.
For each metric, start with provider documentation and typed quota identifiers; verify with two
controlled A/B and B/A rounds on distinct models. Observe B's relevant counter before and after
load on A, correcting only for documented refill/reset and the counter-read request's own charge.
Compare depletion and recovery of both counters. Consistent coupled depletion supports a shared
pool; consistent zero cross-depletion supports independent model pools. External traffic, unknown
refill, changing limits, rounded/undocumented counters or asymmetric results remain unknown or
conflicting. Typed quota IDs can identify nested model-plus-project caps; enforce every applicable
cap rather than choosing one level. Without counter visibility, paired rejection/recovery experiments
can supply weaker supporting evidence only when quota dimensions are unambiguous and budget allows.

Gemini is the explicit independence acceptance case: preserve each project's model-specific RPM,
input TPM and RPD identities. Do not synthesize a provider-wide cap from the provider name. A second
model must retain its own counter while the first consumes its model quota. A simulated shared pool
is the contrasting acceptance case; both models must debit it exactly once. The current Gemini
plugin has no quota parser, so its live result remains unknown until documented quota IDs, token
usage and reset/count fixtures are implemented. Provider docs are starting scope evidence, not a
substitute for the requested experimental cross-model check.

**Burst is more than request size divided by TPM.** A verified request maximum above one minute
of TPM proves oversized single-request capability for that count basis. It does not establish
request burst, aggregate token burst capacity or when another request may start. Conversely a
request maximum at/below TPM does not prove there is no burst of multiple small requests. Measure
both: idle-credit tests at increasing idle durations; repeated equal-size calls at controlled
spacing; and recovery at staggered times after a known debit. Distinguish fixed reset, trailing
window, capped continuous credit and positive-balance admission with debt. Compare measured
counter trajectories with each candidate model and report unknown when more than one fits. Repeat
compatible episodes before promotion; retain the demonstrated capacity and refill intervals,
not a blanket `observed_burst` multiplier. Burst capacity cannot be inferred from configuration.

**Current Groq evidence.** Run 38115240871 accepted 7,739 input plus 232 generated output,
7,971 reported total, and rejected a request whose rate error reported 8,020 against 8,000.
The provider-declared TPM header is 8,000 and the request/day header 1,000. RPM 30 and TPD
200,000 are published free-plan values, not measured throughput/daily edges. Groq documents
organization-owned, model-specific rates plus possible separate organization ITPM/OTPM, so a
single model's responses do not resolve all shared pools. The error's requested-count semantics
must be verified across input/output mixes before reporting an experimentally bounded combined
request maximum. No above-TPM success or multi-call burst/refill episode was established. The
working hypothesis is a request-rate ceiling near 8,000; sustained TPM, exact size semantics,
burst and cross-model scopes remain separate experiments. Do not call the 131,072/65,536 catalog
context values discovered limits, or relabel a rate ceiling as architectural model context.

**Offline implementation in this change.** `limits.py` adds `ConstraintAxis`, `ConstraintEvidence`,
`ConstraintProbePlan`, `next_constraint_probe` and `classify_cross_model_coupling`. Axes bind metric,
exact model/shared identity, count semantics and fixed controls. The planner retains declared and
measured values separately, downshifts/brackets only matching axes, reports budget-limited or
contradictory evidence, and never plans daily exhaustion. The coupling reducer requires repeated
controlled counter deltas and preserves unknown/conflict. These helpers rely on caller-authenticated,
provider-parser-normalized evidence; they do not themselves verify live scopes, parse bodies or
execute the phases. Tests cover independent Gemini-shaped models, explicit shared pools, control
changes, conflicting bounds, all actively probed axes and daily report-only behavior. Serializing
this matrix, wiring real provider adapters and scheduling live experiments remain work before L3.

#### 8.13.B Apply measured policies consistently in Python direct and v2 — proposed sequence

1. Finish provider-specific fixtures, scope/count/window contracts and bounded experiment planning
   in `provider_catalog/{rules,evidence,limits,probe,reconcile}.py` and `providers/*.py`. One weekly
   report contains the full matrix and an explanation of unknown/budget-limited cells. Authenticate
   history and keep declared, empirical and proposed runtime values separate. Every finite run
   accounts for all calls including counter snapshots, failures and retries. Current eight-call
   manual runs cannot confirm a 30-RPM edge; report that budget gap and prepare an explicit reviewed
   allowance before a live RPM experiment. Daily ceilings primarily come from counters/documents.
2. Mature a reviewed L3 catalog/policy contract: independent input/output/combined ceilings;
   model and shared quota identities; RPM/TPM/RPD/TPD and optional ITPM/OTPM; fixed/trailing/refilling
   window mode, timezone/reset and measured burst capacity/admission rule. Preserve absent versus
   unknown. Use proposed reviewed config changes, not Worker edits to YAML. No new config/schema
   fields are authorized by this L2 plan. Unknown behavior retains the reviewed fallback and is
   labeled accordingly. Remove the five-window assumption only with an accepted replacement.
3. Compile one canonical policy for both consumers via `scripts/compile_llm_limits.py` and
   `scripts/compile_dispatch_limits.py`. Apply in `citypods/compute/{llm_policy,llm_routes,
   llm_budget,llm_scheduler}.py`, backend transport selection and
   `workers/llm-dispatch-v2/src/{routes,pacing,coordinator,calibration}.js`, extending the exact
   file/function/schema list at L3 after checking current signatures. Both select fitting input,
   output and combined sizes, then enforce every applicable model/shared quota. Provider-count
   correction applies once and settlement uses actual rate-counted usage.
4. Define the same admission arithmetic in both runtimes. Credit mode waits for sufficient credit;
   measured debt mode may start only at its verified balance threshold and then debit into debt,
   blocking subsequent starts until recovery. Fixed and trailing counters follow measured reset
   semantics; daily quotas reset in the provider timezone. Permanent size rejection is independent
   of temporary quota deficit. An absent shared cap creates no invented shared bucket; an unknown
   cap does not erase an existing reviewed one. Per-model Gemini pools remain independent.
5. Replay identical timestamped request sequences against both implementations using
   `tests/fixtures/provider-tpm-cross-runtime.json`, `tests/test_compute_llm_budget.py` and
   `workers/llm-dispatch-v2/test/pacing.test.js`. Require equal admission/rejection, not-before time,
   model/shared counter debits, actual-usage settlement, daily resets and behavior after failure.
   Cover bounded credit, debt, oversized requests, multiple models, cached/reasoning counts,
   uncertainty and downward/upward limit changes. Add real-workerd rollback/recreation and billed
   row acceptance before durable state/schema changes; Python restart/idempotency must preserve
   equivalent reservations. Do not add per-request SQL logs merely to implement trailing windows.
6. Validate all configured providers offline, then use separately reviewed finite local live
   experiments and final Actions canaries. Activate one model, then two models of that provider,
   then additional providers using their own parser/count/scope contract. Compare predicted starts
   and counter depletion with actual provider results. Keep immediate rollback to the prior reviewed
   catalog/policy; removals and recurring activation retain their existing independent gates.

The runtime plan uses the Workers best-practices guidance for bounded work and durable coordination.
Production Worker arithmetic, Python pacing, configuration, weekly workflow and DO schema remain
unchanged by this offline iteration. Architecture updates accompany their later implementation.

#### 8.13.C Measured baseline plus adaptive dispatch feedback — maintainer direction 2026-10-11

RPD/TPD are not actively measurable by a short adapter. Trust exact provider-declared daily limits
with scope and reset evidence; supplement them with typed ordinary-dispatch exhaustion responses.
A typed error that says only "daily quota exhausted" sets an exhaustion/block signal. It can
supply an empirical numerical bound when complete accounting covers that exact quota scope and
reset period; otherwise it cannot invent a numerical daily ceiling. A numeric error must bind the metric, model/shared identity,
count basis and reset semantics before it can become limit evidence. Active adapter budgets cover
input/output/combined size and minute throughput/refill; daily exhaustion is never their objective.

**Three separate state layers.** The weekly adapter maintains the most accurate evidenced baseline
and proposes reviewed catalog/policy changes. The dispatchers retain short-lived operational
state: reservations, actual-usage settlement, token-estimate calibration, provider remaining-credit
hints, blocked-until times and failure-specific penalties. Ordinary runtime outcomes feed bounded,
payload-free evidence back to the weekly reducer. Operational slowdown does not rewrite the
baseline or masquerade as a newly measured provider maximum.

The current v2 code already contains important behavior that must survive parity work:

- `own_tpm` empties route credit and marks minute reservation exhausted, forcing refill;
- `own_rpm` adds bounded request-start buffer and throttle streak; unknown 429 also backs off,
  but cannot become numerical rate evidence;
- provider `Retry-After` is a dispatch floor, with jitter after that floor; buffers decay with time;
- `own_rpd` marks the route's day exhausted and blocks until its provider-zoned reset;
- `own_tpd` reconciles remaining daily tokens when a typed limit/used pair is available and blocks;
- upstream-capacity/gateway/server errors take their own cooldown/retry treatment instead of
  reducing our RPM/TPM baseline; the capacity path clears unrelated stale own-rate penalties;
- successful settlement refunds/debits to actual usage and resets applicable throttle penalties;
- bounded recent input-token ratios and output-use summaries improve job reservations. These are
  prediction corrections, not provider-capacity measurements or YAML changes.

Preserve these classifications, decay, bounded retries, requeue and crash-safe settlement in both
paths before changing rate arithmetic. A temporary controller can lower operational throughput
through the existing buffer/cooldown mechanism without introducing another permanent safety
multiplier. Recovery is gradual/bounded and never exceeds the reviewed baseline, measured burst
rule or remaining daily quota. Controller bounds/decay require explicit L3 arithmetic and replay
acceptance, rather than selecting an arbitrary new coefficient in implementation.

**Feedback routing.** A typed model RPM/TPM throttle updates that model's operational state; a typed
shared-pool throttle updates the shared pool and gates all members. Daily errors block exactly the
exhausted pool. Unknown errors produce conservative temporary cooldown only. A typed request-size
error triggers fitting-route recovery/defer and a size observation, not a throughput penalty or
changed immutable job payload. Transient capacity and 5xx errors change availability, not declared
limits. Actual usage updates tokenizer prediction once, with existing uncertainty accounted for;
no additional blanket margin is added by the probe. External account traffic may require immediate
remaining-credit reconciliation even when the catalog maximum is correct.

**Two feedback timescales.** Dispatch reacts immediately to exhaustion/errors. The weekly process
consumes authenticated probe and production evidence, revalidates stale/contradictory hypotheses,
and proposes a persistent change when repeated comparable evidence warrants one. Do not infer a
smaller permanent limit from a busy day or larger one from the absence of 429s. Report declared,
measured and operationally effective rate separately so operators can understand the throughput
tradeoff. Existing failure telemetry does not yet expose every numerical/scope signal needed;
any new evidence fields/read mode/schema must be specified at L3, preferably piggybacking bounded
existing settlements instead of adding per-request DO writes or automatic detailed job scans.

**Parity includes authority.** Matching Python and v2 algorithms is necessary but separate ledgers
cannot jointly enforce one shared account quota when both paths run concurrently. The L3 rollout
must specify either shared admission authority for those transports or explicit disjoint quota
allocations; identical local arithmetic alone is not acceptance. Direct requests remain direct
provider I/O, but concurrent consumers must not each claim the entire same bucket. No new endpoint,
DO coordination mode or quota split is authorized by this L2 proposal. Replay both steady-state
policy and adaptive feedback, including two-model/shared exhaustion, transient capacity, actual
usage corrections, resets, restart and recovery; compare scopes, not only average wait time.


#### 8.13.D Provider-canonical limits and provenance — maintainer direction 2026-10-11

Provider-supplied limits are canonical for their exact account/tier/model/shared pool, metric, units
and reset semantics. Distinguish provider-account quota API/dashboard values, documented exact
response headers or typed numeric provider errors, public plan defaults, empirical probe bounds,
legacy guesses and intentionally reviewed operational caps. Public defaults are provisional when
the account has exceptions; contradictory provider sources trigger re-check rather than an
arbitrary source precedence or most-restrictive guess. Preserve source reference/parser version,
identity, retrieval time and applicable period. A generic `observed_on` date and a number do not
establish provider provenance. Existing untraceable values remain `legacy_unknown`; do not silently
promote them to canonical or erase their operational role before replacement is reviewed.

The report and eventual policy schema retain four independent facts: canonical provider limit,
empirical bracket, configured operational cap and runtime adaptive state. The measured result does
not overwrite canonical data. A fresh comparable measurement above/below a canonical maximum
marks `provider_recheck_required` and pauses limit publication for that axis, while preserving
protective runtime cooldowns and the last reviewed configuration. Re-fetch exact account quota
information/headers, verify tier/model/credential identity, reset/window semantics, counting basis,
cache/reasoning policy and competing traffic. Only a fresh provider-source confirmation can update
canonical authority; a probe can justify a separately labeled effective operational cap after
review when the provider continues to declare a higher value. Record unresolved discrepancies
explicitly. If there is no canonical value, empirical convergence may propose a measured limit,
but its provenance stays empirical. Legacy guesses need independent validation, not a fabricated
provider citation. Normalization must never represent guesses or probe bounds as `declared`.

The offline `next_constraint_probe` now reports `provider_recheck_required`, preserves the declared
value and supplies no next experiment target when declarations conflict or comparable measured
bounds contradict them. Regression tests cover measurements above and below a declared TPM and
the distinction between an empirical 7,125 input bound and a provider-canonical number. It does
not implement the provider-source re-fetch or canonical catalog schema yet.

**Empirical daily/monthly discovery from production.** Typed RPD/TPD or monthly exhaustion plus
complete accounting can bound a limit even when the message omits its number. Require the exact
quota period/reset timezone, counter at period start (or an authoritative later checkpoint), all
consumers of the scope, retries/failed attempts under provider counting rules, and comparable
provider-counted tokens. Include any overshoot from concurrent in-flight requests and uncertainty
about whether the rejected attempt is charged. Record an interval unless the provider semantics
justify an exact value. Missing direct-path or external-client usage makes a local successful count
only a lower bound; provider counters may fill that gap. DO outages, retained-data truncation and
monthly accounting gaps must be explicit. Monthly constraints use provider billing periods, not a
30-day guess; future normalization/schema must be specified at L3, not added silently here.

An inferred daily/monthly limit is empirical evidence. If it conflicts with a provider-canonical
limit, it triggers the same independent provider re-check. It cannot replace canonical information
merely because the Worker encountered exhaustion. Trusted numeric provider errors supply provider
evidence only when the parser establishes exact scope and semantics. No active daily/monthly
quota exhaustion is introduced.
