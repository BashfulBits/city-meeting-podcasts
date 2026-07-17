# review/33 — LLM Quota, Cost-Window & Batch Scheduler (R13)

**Maturity: L2 · proposed 2026-07-16 · ROADMAP R13 · depends on R2 and R10**

## §1. Purpose

R2 supplies the LiteLLM-backed LLM adapter and R10 supplies the first asynchronous transport
(the Mistral-specific Cloudflare Worker). Neither currently owns the policy needed to use several
providers economically. R13 adds that policy as a provider-neutral scheduler above the adapter.

The scheduler must be able to:

1. consume provider quotas in the correct order, especially Gemini's requests-per-day (RPD),
   requests-per-minute (RPM), and tokens-per-minute (TPM) limits;
2. account for quota reset times, including Gemini's midnight-Pacific daily reset;
3. delay flexible paid work out of provider peak-price windows, including DeepSeek V4's reported
   Beijing-time peak surcharge windows;
4. reserve provider batch APIs for providers/models that explicitly offer a batch operation and
   discount;
5. honor a caller's allowed-model constraint, including a scorer that must run the same job against
   several specifically selected models; and
6. never choose a paid model when the caller permits a free model and that free model can complete
   within the caller's deadline.

R13 is scheduling infrastructure, not a new LLM provider and not a change to the Mistral Worker.

## §2. Existing boundaries

The layers remain separate:

```text
Stage / scorer
  → R13 LLMScheduler: model eligibility, timing, quota, cost, batching
      → R2 LiteLLMBackend: normalized request, direct LiteLLM call, or transport handoff
          → provider transport (direct, Mistral Worker, future provider batch adapter)
```

R10's Worker continues to pace the Mistral route after receiving work. A future Gemini Worker is a
parallel transport for Gemini's quota queue; it is not the owner of cross-provider selection or
cost-window policy.

## §3. Request contract

`InferenceJob` remains backward compatible. Add scheduler metadata as a separate, typed request
envelope rather than optional unstructured fields in `InferenceJob`; stages then request a service
level without learning provider internals.

```python
@dataclass(frozen=True)
class LLMRequestPolicy:
    allowed_models: tuple[str, ...] | None = None
    selection: Literal["auto_one", "exact_one", "each_model"] = "auto_one"
    cost_policy: Literal["free_only", "free_when_feasible", "paid_allowed"] = (
        "free_when_feasible"
    )
    deadline_at: datetime | None = None
    max_wait_seconds: int | None = None
    batch_permitted: bool = True
    priority: Literal["normal", "urgent"] = "normal"
    purpose: str = ""
```

Semantics:

- `allowed_models=None` means the scheduler may use the configured route policy.
- A non-empty list is an allowlist, not a preference list. The scheduler must not dispatch a model
  outside it.
- `auto_one` selects one allowed model. `exact_one` requires exactly one allowed model. `each_model`
  creates one independent job per allowed model; a scorer uses this to compare the same input across
  models, rather than allowing champion routing to collapse the comparison into one result.
- `free_only` never spends money and remains queued when free capacity is unavailable.
  `free_when_feasible` forbids paid work while a free route has a credible completion before the
  deadline. `paid_allowed` permits normal cost-based selection. A scorer explicitly uses
  `paid_allowed`; it never borrows the production champion route implicitly.
- `deadline_at` is the hard freshness boundary. `max_wait_seconds` is an optional earlier bound on
  deliberate cost-window delay.
- `batch_permitted=False` forces single-request execution, even if the route supports batching.

The request's recipe hash must include the selected model, prompt/version, response schema, and
input fingerprint. Scheduling policy, price, quota state, and the delivery deadline do **not** affect
the artifact identity: the same resolved model output is reusable regardless of when it was run. A
scorer's model comparison outputs remain separate shadow artifacts and never overwrite the canonical
feature output.

## §4. Route capabilities

Provider policy is data, not provider-name branching in the scheduler. Each model route declares:

```python
@dataclass(frozen=True)
class LLMRoute:
    model: str
    transport: str                 # direct, mistral-dispatch, gemini-quota-worker, ...
    free: bool
    quota: QuotaPolicy
    pricing: PricingPolicy
    timing: TimingPolicy
    batch: BatchCapability
```

`QuotaPolicy` supports independent dimensions:

- requests per minute;
- requests per day;
- tokens per minute;
- concurrency;
- reset timezone and reset period;
- optional provider-reported remaining quota.

Gemini must declare RPD, RPM, and TPM together. Its daily bucket resets at midnight Pacific time,
not UTC. The reset timezone must be explicit (`America/Los_Angeles`) and tested across DST changes.
Quotas must also declare their scope: for example, a Gemini request can reserve both a
project-scoped RPD/RPM bucket and a model-scoped TPM bucket. This prevents two independently
eligible models from jointly exceeding a project-wide quota.

`PricingPolicy` supports input cache-hit, input cache-miss, output, fixed request, and time-window
multipliers. DeepSeek's current V4 peak windows should be configured as provider data in
`Asia/Shanghai`, with a default multiplier of 2 only when a reviewed, committed pricing policy
confirms it. Price policy is versioned configuration, not a live pricing feed. If it expires or is
absent, the scheduler disables intentional delay, honors deadlines, and records a warning rather
than trusting unverified price data.

## §5. Selection policy

For each request, the scheduler builds candidates from the configured routes intersected with
`allowed_models`. It then applies these gates in order:

1. Remove routes without enough quota for the estimated request.
2. Remove routes whose predicted completion is after `deadline_at`. The prediction uses durable
   queue depth, active reservations, the next known reset, and recent provider behavior; it is not
   simply "a quota counter is nonzero."
3. Under `free_only`, remove all paid routes. Under `free_when_feasible`, remove paid routes when
   any free route can meet the deadline.
4. Prefer immediate free capacity over delayed free capacity.
5. For flexible work, prefer the lowest expected cost, including peak multipliers and batch pricing.
6. Break ties by deadline, queue age, then configured route priority.

This is intentionally not "always choose the cheapest model": a paid model may be selected only
under `paid_allowed`, when `free_when_feasible` has no free route predicted to meet the deadline, or
when the caller explicitly requested a paid-model comparison. Under `free_only`, it is never selected.

The initial city-onboarding consumer should submit:

```python
LLMRequestPolicy(
    allowed_models=None,
    selection="auto_one",
    cost_policy="free_when_feasible",
    deadline_at=now + timedelta(hours=24),
    batch_permitted=True,
    purpose="city-onboarding",
)
```

This allows any configured free model to run as soon as capacity exists, while permitting a paid
fallback only when all eligible free routes cannot finish within 24 hours.

## §6. Central scheduler and provider workers

R13 owns one durable scheduler queue and is the only component that selects a model. It transitions
an accepted request from candidate routes to a reservation and then a route-specific queue. Provider
Workers only claim work already assigned to their route. This is necessary to consume free capacity
between GitHub Actions runs without allowing either the Gemini or Mistral Worker to grow its own
cross-provider policy.

```text
R13 scheduler queue → selected/reserved route queue → provider Worker or direct transport
```

The scheduler may run on a frequent control-plane tick, but route-policy parsing and selection must
be one tested R13 implementation. Every worker records authoritative `429`, `503`, and retry-after
outcomes back to that durable state; repeated failures temporarily reduce predicted route capacity
without silently falling through to a paid model.

## §7. Gemini quota worker

Gemini's RPD/RPM/TPM combination makes direct calls from a short-lived GitHub Actions process a poor
place to own quota accounting. R13 should add a second Cloudflare Worker transport with:

- an authenticated asynchronous enqueue/poll API matching the R10 contract;
- an R2-backed durable queue;
- atomic quota reservations for RPM, RPD, and TPM;
- a daily bucket keyed to `America/Los_Angeles` midnight;
- token estimates at enqueue time and actual usage settlement on completion;
- retry-after/provider-error handling;
- result retention until the next scheduled reconciliation;
- `/healthz` and quota-status telemetry without exposing prompts or secrets.

The Gemini Worker should only manage Gemini's provider quota and transport. R13 decides whether a
job is eligible for that Worker and when to submit it. This prevents a Worker implementation detail
from becoming a global routing rule. Its implementation should share the R10 asynchronous protocol
and common queue primitives where practical, but be deployed/configured independently so a Mistral
change cannot alter Gemini admission.

## §8. Timing and peak pricing

Every delayed job gets an `eligible_at` and a `deadline_at`. The scheduler runs a cheap planner on
each normal invocation and promotes jobs whose `eligible_at` has arrived.

For DeepSeek V4, the reported peak windows are 09:00–12:00 and 14:00–18:00 in `Asia/Shanghai`.
Flexible jobs should receive `eligible_at` equal to the end of the current peak window when the
expected saving is meaningful. Urgent jobs and jobs near their deadline run immediately and record
`peak_override=true` plus the reason.

The same mechanism handles future providers with free windows, reserved-capacity windows, or
time-varying batch completion pricing. No provider-specific clock logic belongs in a Stage.

## §9. Batch capability

R13 must distinguish a provider's server-side batch API from LiteLLM's client-side parallel helper.
Only the former may receive batch-discount treatment.

```python
class BatchTransport(Protocol):
    def supports(self, request: NormalizedLLMRequest) -> bool: ...
    def submit(self, items: Sequence[BatchItem]) -> BatchHandle: ...
    def poll(self, handle: BatchHandle) -> BatchStatus: ...
```

Jobs may share a batch only when provider, model, task, prompt/version, response schema, generation
settings, privacy policy, and batch eligibility all match. They also need `max_batch_wait` and the
provider's documented completion SLA; a batch is ineligible when that SLA cannot meet the caller's
deadline. Each item retains its own job ID, recipe hash, result, retry state, and cost settlement.
Partial batch failure is item-level retryable work, not an automatic failure of the entire batch.

Anthropic is the first planned future batch-capable route. R13 should ship the capability interface
and scheduler grouping logic without pretending that Gemini, DeepSeek, or Mistral currently provide
a discounted batch path in this repository.

## §10. Durable state and accounting

Each scheduled job records:

```text
ready → delayed → reserved → batched/submitted → running → completed
                                      ↘ retryable / failed
```

Persist at least the caller idempotency key, job ID, request fingerprint, candidate and selected
models, transport, quota reservations, `eligible_at`, `deadline_at`, batch ID, estimated
tokens/cost, actual usage/cost, defer reason, and a bounded route-attempt history. Queue records that
contain prompts have a documented retention TTL. Reservations are made before irreversible
submission, then settled or released when the transport reports completion/failure. The ledger is
provider/model scoped and must use CAS when shared across parallel Actions jobs.

## §11. Additional recommendations

1. **Make quota policy versioned and observable.** Store the policy version used for every decision;
   provider limits and prices change independently of code releases.
2. **Add a safety margin to token reservations.** Reserve estimated input plus a bounded output
   allowance, then settle to actual usage. This prevents TPM overshoot from concurrent jobs.
3. **Use admission snapshots.** A planner should publish one immutable route/quota snapshot per
   scheduler run so parallel workers make consistent decisions.
4. **Prevent starvation.** A free-only job should not wait forever behind newer work; use aging and
   deadline promotion within each provider queue.
5. **Separate evaluation from production.** Scorer requests should carry explicit model allowlists,
   `selection="each_model"`, `cost_policy="paid_allowed"`, and `purpose="evaluation"`; they must
   not silently consume the production champion route.
6. **Cache by request fingerprint.** Identical prompt/context/schema/model requests should reuse a
   completed result before consuming quota. Cache hits must still respect caller isolation and
   untrusted-output rules.
7. **Expose explainable decisions.** Admin status should answer: why this model, why now/later,
   which quota blocked alternatives, expected versus actual cost, and whether a peak window was
   overridden.
8. **Treat pricing as advisory until verified.** A stale or missing price policy must not deadlock
   work. It should disable deliberate delay, preserve deadlines, and emit a visible warning.
9. **Keep provider data out of feature stages.** Stages request capability and policy; they should
   not inspect Gemini reset times, DeepSeek windows, or Worker URLs.

## §12. Implementation sequence and acceptance criteria

### Phase A — policy and selection

- Add request policy and route capability models.
- Implement allowlist intersection, `each_model` scorer fan-out, and free-model protection.
- Implement deadline-aware selection with deterministic explanations.
- Add unit tests for scorer-specific model lists and 24-hour city onboarding.

### Phase B — quota-aware transports

- Add the Gemini quota Worker using the R10 enqueue/poll shape.
- Add RPD/RPM/TPM reservation and settlement, including Pacific reset tests.
- Adapt the scheduler to route Gemini jobs through it while leaving the Mistral Worker unchanged.

### Phase C — timing, accounting, and batching

- Add timezone-aware price windows and peak overrides.
- Add provider/model cost ledger and telemetry.
- Add batch grouping and `BatchTransport`; implement the first provider only after its API and
  discount are verified.

Acceptance requires that:

- a free-eligible job never selects a paid model when a free route can meet its deadline;
- a scorer can execute the same logical task against an explicit model list;
- Gemini daily quota reservations roll over at Pacific midnight and never overspend across shards;
- flexible DeepSeek work is deferred out of configured peak windows;
- deadline pressure overrides deferral with an auditable reason;
- unsupported batching is never treated as discounted batching;
- Mistral Worker pacing remains unchanged; and
- every decision is explainable from durable state and telemetry.

## §13. Open decisions before L3

- Whether Gemini's first Worker should use one queue per model or one queue with model-scoped quota
  buckets.
- The initial default output-token reservation margin.
- Whether the scheduler ledger belongs in the existing compute budget object or a separate
  `llm_budget.json` state object.
- The exact Anthropic batch adapter and retention/polling cadence.
- Whether city onboarding should prefer the newest eligible free job or oldest-deadline-first once
  the initial queue exists.
