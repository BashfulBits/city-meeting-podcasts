# Gemma AI Studio acceptance: investigation and delivery plan

**Status: L2 — evidence-backed approach; diagnostic and canary gates below precede tuning.**
Maintainer-directed follow-up to [review/55](55-adaptive-input-token-admission.md), 2026-10-19
Central (evidence collected through 2026-10-20 01:43 UTC). This document is a plan, not a
production policy change. No new inference calls were made for this investigation.

## Goal and measurement contract

Size new Gemma packets so roughly 95% can execute on AI Studio, maximizing useful throughput.
Send the remaining size overflow to physical routes with larger input capacity for the **same
model**. Five percent is an acceptable overflow allowance, not a quota of failures to manufacture.
Keep 31B and 26B measurements separate; the evidence here principally concerns 31B.

Report these distinct metrics by model, prompt family, producer policy revision, and Google route:

- **First-attempt admission:** unique new packets whose first AI Studio request returns HTTP 2xx,
  divided by all unique new packets actually attempted there. Count every refusal, including
  ambiguous quota failures; do not improve this metric by excluding inconvenient failures.
- **AI Studio coverage:** unique eligible new packets completed on AI Studio divided by all eligible
  new packets, including those pre-routed elsewhere. This prevents gaming acceptance by sending
  only tiny packets to Google. Report preflight size bypasses separately.
- **Usable completion:** validated outputs on AI Studio divided by eligible new packets. HTTP 200
  with invalid structured output is admitted but does not satisfy the execution goal.
- **Overflow and recovery:** size bypass/refusal fraction, eventual completion on larger same-model
  routes, terminal failures, and overflow queue age. Report transient quota retries separately.
- **Goodput:** validated items and input tokens per wall-clock hour, p50/p95 packet latency, and
  attempts per completed packet. Smaller packets alone do not establish better throughput.

Use stable job/packet identity to deduplicate attempts. Report both numerator and denominator,
size buckets, and confidence intervals. Operational target: at least 95% first-attempt admission
and approximately 95% AI Studio coverage, with usable completion tracked alongside them. These
are empirical rollout targets, not a claim that a tokenizer's p95 guarantees 95% acceptance.

## Findings established from code and production evidence

### The expected smaller target is not a fixed ceiling

`citypods/judging/runner.py:_sibling_backend` uses 8,500 estimated tokens only as its fallback.
With a Worker ceiling, it uses the smaller of 1.5 times that fallback and 90% of the reported
ceiling. Adaptive packing shipped on October 16; PR #2507 did not change it.

Producer run
[39317202477](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39317202477)
read a Worker ceiling of 13,673 at 20:14 UTC, giving a 12,305-token **item** target. Three payloads
later refused by Google were staged at 20:46 UTC, with item estimates 12,168, 11,021, and 11,904.
They fit that producer policy. Run
[39344556526](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39344556526)
read 13,073 at 23:59 UTC, giving an 11,765-token item target. Its final gauge still showed 332
outstanding Gemma packets. The queue is continuously replenished under adaptive sizing; waiting
for pre-fix work to drain cannot remove this packet-size class.

`packing.py:item_size` falls back to chars/4 of `item.text()` because `ChatJudgeBackend` has no
`item_tokens` method. `ChatJudgeBackend.build` subsequently adds IDs, answer fields, options,
JSON framing, the system prompt, and the structured-output contract. Enqueue estimates that
larger request. A fixed 10% allowance is not measurement of this variable overhead.

| Job prefix | Items | Item estimate | Enqueued input estimate | Conservative reservation |
| --- | ---: | ---: | ---: | ---: |
| `39370916` | 19 | 12,168 | 13,181 | 16,672 |
| `45a3f286` | 16 | 11,021 | 11,980 | 15,153 |
| `4748e054` | 15 | 11,904 | 12,762 | 16,142 |

A reservation above 16k is an uncertainty bound, not proof the actual packet exceeds 16k.
The admission ratio is intentionally less conservative than the reservation ratio.

### PR #2507 deployed, but the observed refusal rate remains high

[PR #2507](https://github.com/BashfulBits/city-meeting-podcasts/pull/2507) merged at 00:17:45 UTC.
Deployment
[39346789423](https://github.com/BashfulBits/city-meeting-podcasts/actions/runs/39346789423)
finished at 00:18:38; deployed version was `295df6a5-161e-406f-9c53-e3caaba588b8`.
The extended gateway sample from 00:18:38 through 01:43:15 contains 15 Google 31B records:
7 HTTP 200 and 8 HTTP 429 (53.3% refusal per recorded attempt). This is small and includes retries;
it is **not** a measured 46.7% unique-packet first-attempt acceptance rate.

Ten `own_tpm_observed` Worker events in that interval each show one ledger call and one pending
call, with a 15-second margin. The current failed requests have lease-tagged entries from the new
code. Old, untagged claims do not explain those requests. This weakens the simple local overlap
explanation; it does not establish Google's accounting window or exclude unobserved traffic.
Gateway and completion logs have different arrival times and counts; correlate identities rather
than summing them into a fabricated job denominator.

### At least one rejected packet demonstrably fit before

Job `39370916-2a30-4c86-896d-9faa0395555b` received HTTP 200 at 00:11:22 UTC with **12,653 input
tokens**, then the identical stored payload received HTTP 429 at 00:23:39. The earlier output
failed structured validation, explaining why the job was attempted again. The later request
started at 00:22:50, under the new deployment.

Gateway IDs: accepted `01a156a4-9c06-74d6-ab82-e3ed87b3fae7`; refused
`01a156b0-81a5-74dd-bf92-2e7be17d7122`. This disproves the claim that every observed refusal is
an intrinsically oversized packet. Another packet (`45a3f286`) was refused on both distinct
Google project routes. The maintainer verified those projects are distinct.

A second counterexample emerged from read-only result retrieval: job `4748e054`, refused at
00:28:07, subsequently completed on the same primary Google route with **13,717 input tokens**
and 1,083 output tokens. Its offline message-content count below is only two tokens lower.
This further supports investigating quota admission independently of intrinsic packet size.

An offline audit using Google's published tokenizer at revision
`842da3794eaa0b77d5f08bae87a17459d91ff475` counted message-content tokens of 12,651, 12,852, and
13,715 for the three payloads. These exclude provider framing and schema shaping, so they are
cross-checks, not authoritative Google request counts. The first closely matches observed usage;
the other two show why a universal chars/4 correction can mislead. Tokenizer SHA-256:
`cc8d3a0ce36466ccc1278bf987df5f71db1719b9ca6b4118264f45cb627bfe0f`.
[Published tokenizer][tokenizer].

[tokenizer]: https://huggingface.co/google/gemma-4-31B-it

### Calibration and recovery do not implement the requested acceptance loop

`calibration.js` learns pooled model/tokenizer × prompt-family ratios from responses with usage.
Its packing ratio is p95 × 1.05; admission uses p75 × 1.05; reservation uses a higher bound.
Generic 429s without input usage do not enter the token-ratio ring. None of these percentiles
measures route acceptance or aims at a 5% overflow rate.

Explicit `Requested > Limit` now becomes `route_input_limit`: the job excludes that physical route
and can try same-model siblings. A limit-only token-quota error remains `own_tpm`, which cools the
route but does not establish a job-specific size exclusion. Therefore generic 429s can revisit
Google and do not teach the size loop. Read-only polling found several investigated jobs terminal
`job_failed` after one or two attempts; others completed, including on SambaNova. Overflow is not
currently a demonstrated guarantee for every affected job.

Twelve-minute Google and NVIDIA timeouts also occur. They occupy leases and delay batch
settlement, so throughput and usable completion need separate latency/failure accounting.

## What remains unknown

The attachments contain status summaries, not full quota bodies. Existing Worker telemetry lacks
quota dimensions and provider request counts. The credential wrapper successfully reads Worker
logs and deployment metadata; AI Gateway request/response management endpoints returned 403.
No credential values, raw prompts, or raw private logs belong in this committed document.

[Google's rate-limit documentation](https://ai.google.dev/gemini-api/docs/rate-limits) describes
project-level limits and input TPM, but it does not prove the exact enforcement behavior of these
requests. Preserve these hypotheses until measured:

- Provider-visible timing/accounting differs from the coordinator's view, or another caller consumes
  the project/model quota. The observed gateway interval had no other Google model records, but
  that cannot exclude calls outside this gateway.
- Gateway retries or provider processing account for additional demand. A summary retry count of
  zero alone does not prove there was exactly one provider submission.
- Request shaping or output limits affect admission. Stored judge payloads request 2,048 output
  tokens with `route_max`; `gateway.js:outputTokensForRoute` sends the route's 32,768 cap instead.
  Google documents input TPM, so output reservation causing these 429s is **unproven**.
- Some packets exceed Google's actual usable request budget despite estimator admission.
  The successful-then-refused counterexample means this cannot explain all refusals.

## Delivery sequence

### 1. Establish a trustworthy denominator and quota diagnosis

First implementation slice: instrument the executor's existing response handling and
`coordinator.js` completion/retry paths. Emit bounded structured events with job/lease identity,
producer policy revision, model, route, actual start/end, gateway request ID, first/retry ordinal,
raw/admitted/reserved input, sent output cap, usage, and outcome. For failures allowlist quota
metric, quota ID/dimensions, limit, requested amount, RetryInfo/Retry-After, and normalized failure
class. Never log prompts, authorization headers, keys, or unrestricted error bodies.

Join these with `own_tpm_observed`; add the missing identity there. Export a read-only cohort
report through existing operational tooling. Prefer logs and existing folded route writes;
no per-attempt SQL journal, new hot-path RPC, or fresh detailed-stats polling loop. Verify logging
sampling/retention and mark missing joins explicitly. Log-only denominators must disclose sampling;
if exact counts need persistence, design bounded counters folded into existing writes first.

Run a passive new-packet cohort before any size tuning. If quota bodies remain ambiguous, prepare
a capped diagnostic canary using real work: isolate a project route, one provider submission per
request, sufficient quiet time, and matched small/near-target packets. Separately vary the output
cap only if needed. Fix project/model traffic ownership or hidden retries if even small isolated
packets fail. Do not respond by shrinking packets indefinitely.

**Gate:** resolve the dominant quota dimension and distinguish size from time-window failures,
or explicitly retain an unknown class and keep tuning unpromoted. Any diagnostic request budget,
gateway setting change, production rollout, and quota-spending experiment needs a reviewed,
bounded execution contract; this investigation has not performed those actions.

### 2. Size the complete serialized request

Change `citypods/judging/backends.py` to share one pure serialization path between sizing and
`build`. Change `packing.py` to check the candidate packet's full request, including fixed and
per-item overhead, rather than treating additive item text as the full request. Preserve whole
items, evidence, deterministic ordering, and existing split-unit behavior.

Change `runner.py:_sibling_backend` and the Worker packing-ceiling response so units are explicit:
complete-request estimate versus provider input tokens. Audit other Gemma producers in
`citypods/compute/llm.py` and lane-specific callers; list them in the implementation contract.
Use offline tokenizer replay to validate estimator tails by model/family. Do not add a runtime
model/tokenizer dependency until its packaging, memory, latency, and pinned-revision costs are
justified. A measured full-request estimator may suffice.

Keep token-ratio calibration separate from **physical-route acceptance** calibration. Pooled
model usage remains useful for tokenization; an accepted maximum on NVIDIA must not raise
Google's enforceable TPM ceiling. Replace the blind adaptive-growth rule with a bounded target
selected from measured complete-request outcomes. Select the largest useful target that meets
the acceptance goal, with a conservative cold-start target and slow, reversible growth. Before
L3, specify the exact controller, sample floor, update cadence, bounds, and policy revision field
from the diagnostic cohort; do not invent a numerical target from these three payloads.

### 3. Make overflow dependable without misclassifying quota exhaustion

Use `classify.js`, `calibration.js`, `pacing.js`, and `coordinator.js` to preserve explicit
size-refusal exclusions and route proven-unfit work directly to an eligible larger same-model
route. A generic 429 must not become a global learned size ceiling: the identical-payload
counterexample makes that unsafe. Time-window failures retain cooldown semantics. Allow a bounded
job-level recovery path to a larger same-model route after repeated ambiguous Google refusal,
without poisoning size calibration or immediately cycling between two equally limited projects.
Specify that retry threshold and attempt budget in the L3 contract after phase 1 classification.

Audit caller exhaustion as well as coordinator requeueing. A recoverable overflow must not turn
into terminal `job_failed` merely because its Google transient allowance was spent. Respect
larger-route context, concurrency, daily quotas, existing fairness and write budgets; keep work
queued with an explicit reason when eligible capacity is exhausted. No implicit cross-model
substitution. Repacking is the last resort when no same-model route can fit an indivisible packet;
never truncate evidence or drop individual items silently.

Calculate real overflow capacity before promotion: 0.05 × eligible daily packet volume plus
existing larger-route traffic must fit measured sustainable capacity and latency. Advertised
Google RPD alone is not an attainable throughput plan. Do not raise provider quotas, concurrency,
paid spend, or Cloudflare budget ceilings as an incidental fix.

### 4. Validate, drain old work deliberately, then expand

Replay stored payload metadata and usage offline, then canary new packets with a fixed policy
revision. Start with 31B; validate 26B independently rather than inheriting its controller. Collect
at least 200 unique first-attempt packets per model before promoting (about ten failures at the
target), report binomial confidence intervals, and continue observation through at least one full
producer cycle. A 200-packet result is an initial gate, not proof of a precise population rate.
Require admission/coverage near or above 95%, no lost items, usable-completion improvement, bounded
overflow backlog, and improved goodput. Failure of small-packet controls blocks size-only promotion.

Inventory queued/leased work by creation time and policy revision. New policy does not mutate
stored payloads. Let leased work settle; route old queued packets by the same proven-fit and
bounded-recovery rules. If repacking is necessary, prepare a bounded ID manifest with reconciliation
and idempotency checks before changing queue state. Report old and new cohorts separately until
the old cohort drains; do not purge the DO queue or trigger catalog-wide reprocessing.

Rollback freezes the last proven packing target and disables controller growth while retaining
quota diagnostics and safe recovery. Validate old payload compatibility on both forward and
rollback deployments. No pipeline version or stored artifact invalidation is planned; any future
payload identity change must document its own bounded backfill story.

## Implementation map and verification before L3

- Quota diagnosis: executor response handling, `coordinator.js`, and `classify.js`. Test redacted
  real scalar/array errors, missing fields, request identities, and retry correlation.
- Complete-request packing: `judging/backends.py`, `packing.py`, and `runner.py`. Test mixed item
  kinds/options, fixed/schema overhead, boundary splits, deterministic packing, and no item loss.
- Estimator/target: `compute/llm.py` and Worker `calibration.js`. Use held-out replay, family/model
  isolation, cold-start/drift fixtures, and guards against fabricating usage from failed requests.
- Recovery: `pacing.js`, `coordinator.js`, and caller polling. Test explicit size versus ambiguous
  quota failures, exhausted allowances, same-model fallback, stale leases, and duplicate completion.
- Operational report: existing reporting tools. Test unique first attempt versus retry, old/new
  policies, missing evidence, coverage, and validated outputs.

Before implementing, replace broad executor/report references with exact current functions and
list every changed file/test; identify whether bounded acceptance state requires a schema change.
Run affected Python and Worker suites, whole-repo Ruff lint/format checks, and existing row-read/
row-write benchmarks for coordinator SQL/schema changes. Verify review/44's daily safe-stop
invariants
with retries and fallback included. Run CLI CodeRabbit on the final diff and resolve valid findings.
Update review/11, architecture if changed, roadmap status, and a changelog fragment with each slice.

This plan supersedes any interpretation of review/55 that equates a token-ratio percentile with
95% Google acceptance, or attributes these failures conclusively to scheduling jitter. It does
not supersede the already shipped lease accounting, cooldown, and explicit-size recovery fixes.
Recurring synthetic provider probing in [review/54](54-provider-limit-characterization.md) remains
separate; the first step here uses real-work observations rather than promoting that deferred scope.

## Follow-up: quota interpretation and downward feedback (2026-10-19 Central)

The maintainer supplied the message `Quota exceeded for metric: ...input_token_count, limit:
16000, model: gemma-4-31b` with a 32.154473808-second retry hint. This identifies an input-token
quota failure but supplies **no requested token count**. A 12k request encountering 6k of recent
usage can produce the same failure as an individually oversized request. A cooldown hint does
not distinguish those cases. Preserve the structured quota ID to distinguish per-minute from
per-day dimensions when the metric name is shared.

There is already negative feedback: `completeBatch` records `input_rejected_min` for
`route_input_limit`, and `packingInputLimit` includes that bound. However, the recorded size is
`raw estimate × conservative input ratio`. A refusal of a 13,181-estimate job can thus record
16,672, above the configured 14,400 ceiling, leaving the producer's packing ceiling unchanged.
This is a second gap beyond the absence of usage in generic 429s.

**Proposed downward signal (L2, after diagnostics):** keep a separate bound in raw complete-request
estimate units, scoped to physical route and prompt family. On a confirmed size refusal, a fresh
packing bound must be strictly below that refused raw estimate; take the minimum with the normal
packing limit before returning it to producers. Do not divide it by another tokenizer ratio or
write synthetic observations into the pooled ratio ring. Apply expiry and bounded relearning;
a later success with a comparable or larger raw request can contradict the bound. A success
on another provider cannot clear Google's signal. Specify storage/cardinality, expiry, and growth
rules using the cohort before promoting this controller to L3. This protects against one unusual
high-ratio packet permanently shrinking all prompt families.

For ambiguous TPM failures, retain a separate operational signal. Repeated isolated failures with
known complete-request sizes can justify a reversible target reduction in an experiment, but
must not be recorded as proven intrinsic size failures. Compare small controls and near-target
packets; if both fail, the response is pacing/ownership/provider diagnosis rather than repeated
packing reductions. The 95% goal permits statistical adaptation without falsely claiming a
per-request limit was measured.

### First diagnostic slice: L3 implementation contract

Implemented locally, awaiting PR review and deployment. The complete acceptance/controller plan
remains L2; these logs provide evidence and do not establish that the 95% goal has been met.

This slice is authorized by the maintainer's instruction to continue. It prepares evidence for
tuning; it does not change packing, retries, request shapes, quotas, dependencies, or schemas.

- `src/classify.js`: add a pure, bounded extractor for allowlisted Google quota metadata and
  separately labelled message limit/requested/used values. Normalize array bodies. Never emit
  full messages, arbitrary dimensions, credentials, or prompt text. Keep classification unchanged.
- `src/index.js:attemptProviderCall`: emit one structured event for every actual Google Gemma
  call, including network failures and HTTP 2xx, with identity, timing, known attempt ordinal,
  full-job estimates, usage, sent output cap, quota details, and response validation outcome.
  Include configured gateway attempt policy as configuration, not a measured provider call count.
- `src/coordinator.js:claimDispatchWindow`: include attempt ordinal, prompt family, and calibrated
  admission/reservation estimates in the existing claim response using values already available.
  `authorizeRetry` adds identity to `own_tpm_observed`; no SQL/RPC or write-budget change.
- `src/gateway.js:callAiGateway`: return the already resolved gateway retry configuration for
  diagnostics and expose a local callback immediately before fetch, after request preparation.
  Pre-request failures remain unstarted and do not enter provider-attempt logs or consume an
  attempt. `dispatchOneJob` advances its local ordinal only when another attempt is authorized.
- Tests: `test/classify.test.js` covers the exact supplied message, explicit requested amounts,
  structured per-day/per-minute details, malformed/array bodies, bounded output, and redaction.
  `test/api_shapes.test.js` covers actual executor success, quota refusal, transport failure, and
  unchanged non-Google behavior. Coordinator and scheduled suites guard claim/ordinal propagation.
- Documentation: this plan, review/11 status, architecture telemetry description, and a changelog
  fragment. No artifacts are invalidated and no existing queued payload is rewritten.

Paths in the contract above are relative to `workers/llm-dispatch-v2/`. No other runtime files
are in scope. Missing identity from an old coordinator remains null; do not invent a first attempt.

Google's [QuotaFailure schema][quota-failure] independently confirms that `quotaValue` is the
**enforced quota**, not the number of tokens requested. The [countTokens API][count-tokens] is
another possible diagnostic: verify the exact Gemma model advertises support and that the native
request's token count matches this OpenAI-compatible route's shaping before relying on it. A
capped comparison against known successful payloads could establish packet size without generating
another completion. It is not yet verified for these routes, and adding a preflight to every call
would add latency and requests; prefer sampled checks or a validated local tokenizer if sufficient.

[quota-failure]: https://docs.cloud.google.com/tasks/docs/reference/rpc/google.rpc#quotafailure
[count-tokens]: https://ai.google.dev/api/tokens
