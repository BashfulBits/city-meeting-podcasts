# review/33 — LLM Quota, Cost-Window & Batch Scheduler (R13)

**Maturity: L3 · revised 2026-07-16 (simplification pass on the initial L2 draft) · completed to
dev-ready depth 2026-07-16 · implemented and revised again 2026-07-17 (code review + a unified
deferred-completion interface) · ROADMAP R13 · depends on R2 and R10 · co-ships the still-unbuilt
review/27 §8 budget ledger as one shared object**

**2026-07-17 revision note:** this doc originally shipped as a build spec (§12's LLM-SCHED-1…6);
the implementation landed against it, went through a full code-review pass, and then gained one
real interface change beyond what LLM-SCHED-1…6 specified: a caller no longer needs to distinguish
"not eligible right now" from "genuinely in flight at the Mistral Worker" — both produce the same
portable `JobHandle`, both can be picked up later by anyone (a real deferred-request registry, plus
a daily sweep workflow, closes the gap where a caller would otherwise have to rebuild its whole
request to check back). §6, §7, §10, and §12 below describe the *current* (post-revision) design;
§13 keeps a dated log of both revisions rather than pretending this shipped in one pass.

## §1. Purpose

R2 supplies the LiteLLM-backed LLM adapter and R10 supplies the first asynchronous transport
(the Mistral-specific Cloudflare Worker). Neither owns the policy needed to use several providers
economically — today `citypods/compute/llm.py` dispatches to exactly **one statically configured
model** (`config/site_config.yml: llm_model`), with no per-request choice, no quota tracking, and no
cost ledger. R13 adds that policy as a provider-neutral layer above the adapter.

R13 must let a caller:

1. consume provider quotas in the correct order, especially Gemini's requests-per-day (RPD),
   requests-per-minute (RPM), and tokens-per-minute (TPM) limits;
2. account for quota reset times, including Gemini's midnight-Pacific daily reset;
3. wait for DeepSeek's cheapest recurring pricing window rather than dispatching at a higher price
   when the work isn't time-sensitive;
4. honor a caller's allowed-model constraint, including a scorer that must run the same job against
   several specifically selected models;
5. never choose a paid model when the caller permits a free model and that free model can complete
   within the caller's deadline; and
6. let a caller ask for "the cheapest response from model Y within X days, deferral is fine" and
   actually get one *without* re-deriving the whole request itself on every scheduled run — the same
   `JobHandle`/`reconcile()` shape whether the reason it didn't complete synchronously was nothing
   being eligible yet, a real rate-limit response, or a genuine in-flight Mistral dispatch (§6, §7.1,
   §10.7).

R13 is scheduling **policy**, not a new LLM provider and not a change to the Mistral Worker. It is also
not a new persistent service: this codebase's execution model is periodic, stateless GitHub Actions
runs that consult durable CAS state and idempotently retry next run (`compute/budget.py` H14d,
`ops/work_leases.py` H17). R13 fits that shape rather than introducing a parallel one — see §6 for
why the initial draft's "central scheduler queue" and second Cloudflare Worker are cut.

**Relationship to review/27:** review/27 (L3) already owns provider-priority ordering (reused H5
windowed-recency), the quality tournament/champion routing, and — per its §8.1 — commits to shipping a
soft per-provider $ budget ledger "as part of [R2]." That ledger does not exist in code yet. R13 does
not re-litigate provider ordering or the tournament; it fills the two gaps review/27 explicitly left
open (real RPM/RPD/TPM quota accounting, Gemini's async handling) and, per maintainer decision, **ships
review/27's $ budget ledger and R13's quota ledger as the same CAS object** rather than as two ledgers
built at two different times (§10).

## §2. Existing boundaries

```text
Stage / scorer
  → R13 selection function: model eligibility, quota, cost-window preference
      → R2 LiteLLMBackend: normalized request, direct LiteLLM call, or transport handoff
          → provider transport (direct, Mistral Worker, future provider batch adapter)
```

R10's Worker continues to pace the Mistral route after receiving work; it does not gain cross-provider
policy. R13 has no equivalent Worker of its own (§7) — it is a pure function plus one CAS ledger,
called synchronously from inside the calling Stage's normal run.

**Transport availability, stated up front because it shapes gate 0 in §5 (revised 2026-07-17):**
`LLMBackendConfig.mode` (`"direct"` or `"dispatch"`) only governs the legacy static-model path
(`_run_without_policy`, unchanged from before R13). A policy-bearing call instead asks
`LiteLLMBackend._available_transports()`, computed independently of `mode`: `direct` is always
reachable (it needs nothing beyond a provider API key, already in env); `mistral-dispatch`/
`llm-dispatch` are reachable whenever `dispatch_url` is configured, regardless of `mode`. Gate 0
(admission) keeps every route with *any* transport in that reachable set as a candidate.

**2026-08-06 correction (review/41):** gate 0's *admission* is still "freely among every route whose
transport is reachable" as originally written, but that is not the same as *which* transport a
selected dual-transport route actually dispatches over — a distinction this section originally
elided and a real bug shipped from eliding it (a dual-transport route defaulted to the Worker
whenever `dispatch_url` was merely configured, not because it was the caller's actual choice). Once
admitted, a route offering both `direct` and a dispatch transport (today only Gemini) resolves to
`direct` by default; the dispatch transport is used only when the caller explicitly sets
`LLMRequestPolicy.allow_dispatch_overflow=True` (`_selected_transport`, `citypods/compute/
llm_scheduler.py`). A route with no `direct` alternative (Mistral) is unaffected — it always
resolves to its one dispatch transport regardless of this flag. See review/41 §3.3 and §2 (the
city-discovery incident this fixed) for the full account.

This matters concretely for the deferred-request sweep (§10.7): it services a mixed bag of pending
records from whatever callers originally submitted them, regardless of which transport backs each
one, so it constructs one `LiteLLMBackend` with `dispatch_url` set and reaches both. A caller that
only ever needs direct routes (most of them) simply doesn't set `dispatch_url` and gets exactly the
old single-transport behavior. The original draft treated "one backend, one transport, no mid-
request switching" as a hard scoping constraint deferred to a hypothetical future caller (see the
original §14); the sweep turned out to be exactly that caller, so it shipped now rather than later.

**2026-08-08 correction (PR review pass):** `select_and_reserve`'s inflight-reservation reuse path
(§10.3) matched the in-flight owner and returned the original route's dispatch transport without
checking whether that transport was still in `available_transports`. If the Worker was removed from
the backend config between the original reservation and the retry, the returned `SelectionResult`
had `transport=None`, which would propagate to `_owner_for` and the dispatch-vs-direct branch in
`llm.py`. Fixed by falling through to fresh selection when the reused transport is no longer
reachable.

## §3. Request contract

`InferenceJob` (`citypods/compute/base.py`) is pre-1.0-locked and stays unchanged. Scheduler metadata
travels through its existing open `inputs: Mapping[str, Any]` field — the same mechanism `llm.py`
already uses for `structured_output`/`messages`/`content` — as `job.inputs["llm_policy"]`:

```python
@dataclass(frozen=True)
class LLMRequestPolicy:
    allowed_models: tuple[str, ...] | None = None
    allow_paid: bool = False
    deadline_at: datetime | None = None
    purpose: str = ""
```

**Backward compatibility is the load-bearing property here:** `job.inputs.get("llm_policy")` is
`None` for every Stage that does not opt in, and `LiteLLMBackend.run_inference` must behave **exactly
as it does today** — same code path, same `self.config.model`, no ledger touched — whenever it is
`None`. Nothing in R13 is allowed to change existing behavior for a caller that doesn't ask for it.
This boundary is restated as an explicit test requirement in LLM-SCHED-4 (§12).

**Revised 2026-07-17 — no separate opt-in for a deferred result.** The original draft (and the first
implementation pass) gated "return a portable handle instead of raising" behind a
`defer_as_handle: bool` field. That field is gone: a policy-bearing call that isn't eligible right
now *always* returns a `JobHandle`, unconditionally — the same shape a genuine Mistral dispatch
already returns on a 202, uniformly, so a caller never has to reason about *why* it didn't get a
synchronous result. `JobHandle` (`base.py`) gained one new optional field, `deferred_request:
DeferredLLMRequest | None`, carrying everything `reconcile()` needs to retry a *deferred-direct*
request without the caller reconstructing it (see §6, §10.7):

```python
@dataclass(frozen=True)
class DeferredLLMRequest:
    messages: tuple[Mapping[str, Any], ...]
    policy: LLMRequestPolicy
```

A genuinely in-flight Mistral handle has `deferred_request=None`; `reconcile()` uses its presence,
not a separate flag, to decide whether to re-run selection or poll the Worker's URL (§10.7).

Four fields, not eight. The initial draft had `selection` (3-way enum), `cost_policy` (3-way enum),
`max_wait_seconds`, `batch_permitted`, and `priority` in addition to the above — each is either
redundant with one of these four fields or belongs one layer up, in the calling Stage:

| Original field | Why it's gone |
|---|---|
| `selection: auto_one/exact_one/each_model` | Fully derivable from `allowed_models` cardinality: `None` = auto, `(m,)` = exact. "Each model" fan-out is the tournament driver calling this contract once per model with a singleton allowlist — that's where the fan-out conceptually belongs (review/27 §6 already owns tournament orchestration), not a scheduler mode. |
| `cost_policy: free_only/free_when_feasible/paid_allowed` | Collapses into `allow_paid: bool`. "Free only" is `allow_paid=False`. "Free when feasible, paid past that point" is `allow_paid=True` + `deadline_at` set — selection always prefers free among eligible candidates (§5), so paid is only chosen when no free route survives the deadline gate. "Force this specific paid model regardless of free availability" (the evaluation case) is just `allowed_models=(paid_model,)` — the allowlist is exact, so a free model outside it was never a candidate. |
| `max_wait_seconds` | Redundant with `deadline_at` in v1 — deliberate cost-window delay (§8) never waits past `deadline_at` anyway, so one bound is enough. |
| `priority: normal/urgent` | Redundant with deadline proximity — a job with a near `deadline_at` already behaves as urgent (§5 gate 4's hard cutoff, §8's off-peak override). No separate flag needed. |
| `batch_permitted` | Batch capability is deferred (§9); nothing to permit yet. |

`purpose` is a plain telemetry tag for explainability (§11), not a scheduling input — the scheduler
must not branch on its value.

The request's recipe hash must still include the selected model, prompt/version, response schema, and
input fingerprint (unchanged from the original draft). Scheduling policy, price, quota state, and the
delivery deadline do **not** affect artifact identity: the same resolved model output is reusable
regardless of when it was run. A scorer's model-comparison outputs remain separate shadow artifacts
and never overwrite the canonical feature output.

## §4. Route capabilities

```python
@dataclass(frozen=True)
class PeakWindow:
    tz: str  # IANA zone the provider publishes the window in
    start: time  # local wall-clock time in `tz`
    end: time  # local wall-clock time in `tz`; may be < start (window crosses midnight)
    multiplier: float  # 0.5 = 50% off during this window; >1.0 = a surcharge


@dataclass(frozen=True)
class PricingPolicy:
    input_per_token: float = 0.0
    output_per_token: float = 0.0
    windows: tuple[PeakWindow, ...] = ()
    periods: tuple[PricingPeriod, ...] = ()
    cost_cap: float | None = (
        None  # soft $ cap per cycle (review/27 §8.1); None = untracked/uncapped
    )


@dataclass(frozen=True)
class QuotaPolicy:
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    concurrency: int | None = None
    reset_timezone: str = "UTC"  # IANA zone; only meaningful when `rpd` is set
    # rpm/tpm are always per-minute by definition and need no separate period field.


@dataclass(frozen=True)
class LLMRoute:
    model: str
    transport: Literal["direct", "mistral-dispatch"]
    free: bool
    quota: QuotaPolicy
    pricing: PricingPolicy
```

There is no separate `TimingPolicy` or `BatchCapability` type in v1 — timing remains pricing data.
As implemented, `PricingPolicy.periods` is an effective-dated route-local rate card; each period
can carry input/output rates and peak windows. Batch capability remains deferred (§9) until a
provider's submit/poll wire contract is confirmed.

### §4.1 The concrete route table

`citypods/compute/llm_policy.py` ships a module-level `ROUTES: dict[str, LLMRoute]` covering exactly
the five model strings already in `llm.py`'s `SUPPORTED_MODELS` — LLM-SCHED-1 (§12) asserts that
equality so the two tables cannot drift apart:

| `model` | `transport` | `free` | quota | pricing |
|---|---|---|---|---|
| `gemini/gemini-3-flash-preview` | `direct` | `True` | `rpm=10, rpd=1500, tpm=250_000, reset_timezone="America/Los_Angeles"` (review/27 §2) | `0.0 / 0.0`, no windows, no cap (free) |
| `deepseek/deepseek-v4-flash` | `direct` | `False` | `concurrency=5` (compiled safety ceiling) | Effective-dated YAML pricing: pre-cutover `0.14e-6 / 0.28e-6`; from `2026-08-16T16:00Z`, off-peak `0.22e-6 / 0.66e-6` and peak windows `01:00–04:00` + `06:00–10:00 UTC` at `2x` |
| `deepseek/deepseek-v4-pro` | `direct` | `False` | `concurrency=5` (compiled safety ceiling) | Effective-dated YAML pricing: pre-cutover `0.435e-6 / 0.87e-6`; from `2026-08-16T16:00Z`, off-peak `0.66e-6 / 1.98e-6` and the same peak windows at `2x` |
| `mistral/mistral-large-latest` | `mistral-dispatch` | `True` | `rpm=2` (review/27 §2, "~2 RPM (plan for 1/min)") | `0.0 / 0.0`, no windows — only a candidate for a backend with `dispatch_url` configured (§2) |
| `mistral/mistral-large-3` | `mistral-dispatch` | `True` | `rpm=2` | same as above |

The compiled `concurrency=5` is an internal safety ceiling, not a claim about DeepSeek's provider
maximum. DeepSeek is concurrency-based rather than RPM/RPD; replace this conservative ceiling only
after production telemetry establishes a safe value. DeepSeek is never free, so it never bypasses
`allow_paid`.

## §5. Selection policy

`select_route` (§6) builds the candidate set from `ROUTES` and narrows it through these gates, applied
in order. Each gate either drops a route or defers it; the function always returns *why* every
non-winning route was excluded (§11.5).

0. **Transport gate.** Keep only routes whose `transport` is in `available_transports` (§2) —
   computed from `dispatch_url` presence, not a fixed `mode`.
1. **Allowlist gate.** If `policy.allowed_models is not None`, keep only routes whose `model` is in it.
2. **Free-model-protection gate.** If `not policy.allow_paid`, drop routes where `free is False`.
3. **Quota/budget gate.** Drop routes where `LLMBudget.available(...)` (§10) is `False` for the
   estimated request (RPM/RPD/TPM/concurrency/$ cap, whichever dimensions the route declares, **plus
   `blocked_until`** — a real 429 response reactively overrides this gate's own proactive estimate,
   §7.1).
4. **Deadline gate.** If `policy.deadline_at` is set, drop routes whose predicted completion is after
   it. Predicted completion is `now` if gate 3 passed, or the route's next quota reset (from its
   ledger window keys, §10) if it didn't — never a fixed "assume it's always available eventually."
5. **Off-peak-preference gate.** For a route with an inactive discount window (`multiplier < 1`,
   §8) whose next active window would still finish before `deadline_at` (or `deadline_at` is unset),
   drop the route **for this call only** — it is not rejected forever, just not selected right now
   (§6). This is the only gate that can remove an otherwise-eligible route purely to save money, and
   it never fires once waiting would miss the deadline.
6. **Ranking.** Sort what's left by `(not free, current_effective_cost, predicted_completion, model
   name)` ascending and take the first. `current_effective_cost` applies any *active* peak-window
   multiplier for that route right now. **Reconciled 2026-07-17:** the original wording here said
   "configured priority," but `LLMRoute` (§4) never actually gained a `priority` field — every
   candidate that reaches ranking has `predicted_completion == now` (only immediately-available
   routes get this far), so the first three sort keys are frequently tied, and the implementation's
   final tiebreak is the model name string, purely for determinism (so ties don't depend on dict
   iteration order). This is correct and sufficient for the current five-route table, where ties are
   arbitrary anyway. Add a real `priority` field only if a future route genuinely needs one to win
   deliberately over an equally-ranked alternative — nothing in v1's route table needs that yet.

Gate 6 never rejects a route — it only orders whatever gates 0–5 left. If gates 0–5 leave nothing,
the function returns no route (ranking never runs) and `run_inference` returns a `JobHandle` (§6) —
not eligible this call, completed later either by the same caller asking again or by the deferred-
request sweep (§10.7).

**Revised 2026-07-17 — city discovery, the one live consumer, wants a synchronous answer, not a
deferred one.** It acts on the classification result immediately (continuing an issue-comment
cycle), so it deliberately asks for the narrowest policy that can never silently spend money or
wait days for a discount, and lets its own existing daily retry (`ClassificationDeferred` →
`DEFERRED_EXIT`, unchanged since before R13) own "not eligible right now" instead of anything R13
tracks across runs:

```python
LLMRequestPolicy(allow_paid=False, purpose="city-onboarding")
```

No `allowed_models` (today that resolves to whichever free+direct route is configured — just
Gemini, but a second free+direct route added later needs no change here) and no `deadline_at` (moot:
gate 3 already rejects a route outright when it's not available *this call*, regardless of what a
deadline says — deadline only ever governs whether gate 5's cheapest-window wait fires, and a free
route has no off-peak windows in the first place). A caller that instead wants R13 to hold onto a
request and complete it later — "cheapest response from model Y within X days, deferral is fine" —
sets `allow_paid`/`allowed_models` as needed and a real `deadline_at`; §10.7 covers how that
actually gets completed without the caller re-asking on a schedule of its own:

```python
LLMRequestPolicy(
    allowed_models=("deepseek/deepseek-v4-flash",),
    allow_paid=True,
    deadline_at=now + timedelta(days=3),
    purpose="evaluation",
)
```

## §6. A selection function, plus a small registry -- not a scheduler service

The selection logic itself is still exactly what the original draft argued for: one pure function
plus the CAS ledger it reads (§10), no independent process, no "control-plane tick."

```python
# citypods/compute/llm_scheduler.py


@dataclass(frozen=True)
class SelectionResult:
    model: str | None
    route: LLMRoute | None
    reason: str  # always populated, human-readable
    rejected: tuple[tuple[str, str], ...] = ()  # (model, reason) for every non-winner
    owner: str | None = None  # the ledger reservation owner, once selected


def select_route(
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute],
    ledger: LLMBudget,
    available_transports: Set[str],
    estimated_tokens: int,
    requests: int = 1,
    now: datetime,
) -> SelectionResult:
    """Pure — no I/O. Applies §5's gates 0–6 against an already-loaded, read-only `ledger`
    snapshot. Safe and fast to unit-test without any storage double."""
```

A calling Stage never calls `select_route` directly — it calls `LiteLLMBackend.run_inference` with
`job.inputs["llm_policy"]` set, and the backend calls `select_and_reserve` (§10.3, which loads the
freshest ledger via CAS, calls `select_route` against it, and reserves the winner) internally.

**Revised 2026-07-17 — what changed, and what deliberately didn't.** The original design's strongest
claim was "R13 does not remember that a job was deferred or when to wake it up" — when selection
found nothing eligible, `run_inference` raised `LLMNotEligibleError` and the *calling Stage's own*
durable state was the only thing tracking "this still needs doing." That was correct for city
discovery (§5), which already has its own daily retry loop. It broke down for the actual next
consumer (R5's per-verb quality comparisons): a caller with no natural short retry cadence of its
own, that shouldn't have to reconstruct its whole request (re-read a transcript, rebuild a prompt)
just to ask "is DeepSeek's discount window open yet."

So R13 now persists one more thing: a plain, listable key-value registry
(`citypods/compute/llm_deferred.py`, §10.7) mapping `recipe_hash → JobResult | pending JobHandle`.
This is *not* the central scheduler queue the original draft rejected — there is still no priority
ordering, no lifecycle state machine, no per-job scheduling logic. It is a cache with exactly two
states (pending, completed) that `run_inference` checks before doing anything else and writes to
before returning, so that (a) a caller can just call `run_inference` again with the same job and
transparently get whatever's ready, and (b) a **sweep** (§10.7) — not R13's selection logic, which
remains 100% stateless and re-evaluates every gate fresh on every call — can complete pending records
on a schedule no individual caller needs to own. Gate 0 through 6 did not change to support this;
only what happens with a `None` selection result changed (§5, §10.7).

The Mistral Worker's own internal pacing (R10) is unaffected — it still only claims work already
selected and handed to it; it does not gain cross-provider policy. What *did* change is who can
reach it: any backend with `dispatch_url` configured, not only a `mode="dispatch"` instance (§2).

## §7. Gemini — quota ledger, not a second Worker

The initial draft proposed a second Cloudflare Worker (its own R2-backed queue, atomic RPM/RPD/TPM
reservations, async enqueue/poll) purely to own Gemini's quota accounting. That's cut for v1.

R10's Worker exists for a specific, narrower reason (review/27 §9.1–§9.2): Mistral's ~1–2 RPM limit is
so tight that pacing it from inside a GitHub Actions run would leave the runner idle for most of its
wall-clock time between calls — wasted runner-minutes and borderline ToS territory. A Cloudflare
Worker's `fetch()`-await time doesn't count against its CPU cap, so it can hold that slow drip open for
free; a GitHub Actions runner can't do the same cheaply.

Gemini's free tier (10 RPM / 250K TPM / 1,500 RPD, review/27 §2) does not have that problem. A direct
call from inside an Actions run, gated by `select_and_reserve` against the CAS-backed ledger (§10), is
atomically safe across concurrent shards without needing a process that outlives the run. If quota is
exhausted, `run_inference` returns a `JobHandle` (§6) — the same uniform "not now" signal as every
other gate rejection, completed later by a caller asking again or by the sweep (§10.7).

**Only build a dedicated Gemini Worker later, and only if real usage shows the calling workflows'
cron cadence is too coarse relative to Gemini's RPD reset window** — that's an empirical question to
answer with ledger telemetry (§11.5), not a day-one design commitment.

**2026-08-06 addendum:** [`review/41`](41-multi-provider-llm-dispatch.md) did later add Gemini as a
Worker-reachable route (`transports=("direct","llm-dispatch")`) — not to replace this section's
decision, but to reach a *second configured account*'s capacity (`GEMINI_API_KEY_SECONDARY`) that a
single-account direct-only ledger entry can't see. That extension shipped once already, briefly, with
a bug that made the Worker the *default* for any dispatch-capable backend rather than an explicit
overflow — silently breaking city discovery's synchronous design, the concrete incident review/41
records. The fix restores this section's decision as the actual default: a dual-transport route goes
direct unless a caller explicitly opts in (`LLMRequestPolicy.allow_dispatch_overflow`).

### §7.1 Reactive rate-limiting (new, 2026-07-17)

The whole design above is *proactive*: the ledger predicts availability from what R13 itself has
reserved. It has no way to know about quota consumed outside its own accounting — a shared quota
pool, an unmodeled dimension, or simply an estimate that was wrong — so it had no way to react to a
real 429 either; before this revision, a rate-limit response just surfaced as a generic failure.

`LLMBudget` gained one more field per route, independent of the RPM/RPD/TPM counters:

```python
class RouteLedger:
    ...
    blocked_until: str = ""  # ISO datetime, UTC; "" means not blocked


def block(self, model: str, until: datetime, *, route: LLMRoute, now: datetime) -> None:
    """Never moves an existing block earlier -- the longer of the two wins."""
```

`available()` (§10.1) checks `blocked_until` before anything else. `run_inference` detects a 429
duck-typed (`getattr(exc, "status_code", None) == 429` for a raised LiteLLM exception; `response.
status_code == 429` directly for the dispatch-mode HTTP path — no specific exception class import,
since it varies by provider/version), extracts a `Retry-After` hint if the response carries one
(falls back to a fixed 60s), calls `block_route_until`, **settles** the reservation (§10.2 — the
request reached the provider regardless of the 429, so its rate-limit slot is genuinely spent), and
returns the same deferred `JobHandle` gate 3's exhaustion already returns. A real signal from the
provider is authoritative over this system's own estimate, and reuses the identical completion path
either way.

## §8. DeepSeek off-peak pricing

**In scope for v1** — DeepSeek V4's announced effective-dated rate card is compiled from
`config/provider_limits.yml`, rather than requiring a code edit for each price change. The periods
carry only input and output rates; cache-hit pricing is intentionally not modeled because the
pipeline cannot predict or control the hit ratio.

**Current card:** the pre-cutover period preserves the prior prices and historical off-peak behavior.
The period effective `2026-08-16T16:00:00Z` sets Flash off-peak to `$0.22` input / `$0.66` output
and Pro to `$0.66` input / `$1.98` output per million tokens. Its
two UTC peak windows (`01:00–04:00` and `06:00–10:00`) use a `2.0` multiplier. Actual settlement
uses the configured input/output rates; the scheduler uses the same rates for conservative
admission estimates.

Mechanism: entirely §5 gate 5. Flexible work is held when a route is currently in a more expensive
window, with `retry_at` pointing to the next cheapest window for paced callers; a deferred record still
re-evaluates fresh rather than persisting a price assumption. A deadline overrides the wait when the
cheaper window would be too late. Because DeepSeek overflow/tournament dispatch already tolerates "picked
up next run" (review/27 §5.3), this needs no new infrastructure — R13's selection function is simply the
shared place that logic now lives, matching the conclusion review/27's
own dispatch-coordinator design already reached for the same problem.

A job near its deadline overrides the preference and dispatches at full price; that override is
implicit in gate 5 (it only defers when deferring still meets the deadline), not a separate flag.

**Treat pricing as advisory until verified.** If a route's price/window data is stale or absent, gate 5
is skipped for that route (no deliberate delay), quota/deadline gates still apply, and a warning is
logged. A missing or wrong price fact must never cause a missed deadline or a stuck job.

## §9. Batch capability — deferred

No route in `SUPPORTED_MODELS` has a confirmed server-side batch API today. DeepSeek has no batch API;
its ordinary chat-completion transport is the only supported path. Anthropic is not currently a
configured provider either. Building a generic `BatchTransport` protocol for a capability nothing
uses yet is the same premature-abstraction risk review/27 §8.2 already argues against for cost
estimation — add provider-specific support only when a real submit/poll contract exists.

No generic batch abstraction was added. If a future configured provider offers a confirmed
submit/poll contract, add that provider-specific transport and its request/result lifecycle in a
follow-up review rather than treating a different URL as sufficient.

## §10. Shared ledger and durable state

**Per maintainer decision, review/27 §8's still-unbuilt soft $ budget ledger and R13's quota ledger
ship as one object, not two built at different times.** A new CAS-backed state object,
`state/llm_budget.json`, is a sibling of the ASR-scoped `compute_budget.json` — `compute/budget.py`
and its state file are H13-family and **pre-1.0-locked**, so this is a new module (`citypods/compute/
llm_budget.py`), not an edit to that one. It mirrors `Budget`'s reserve/settle/release/CAS-retry shape
directly rather than inventing a new persistence pattern.

### §10.1 Ledger schema

```python
@dataclass
class LLMReservation:
    cost: float
    requests: int
    tokens: int
    # The window keys the ledger was rolled to at reserve time (added in the code-review pass --
    # see the boxed warning after §10.2): settle()/release() compare these against the *current*
    # keys before correcting a counter, since a rollover between reserve and settle/release
    # already zeroed it, and reapplying a delta computed against the old reservation would
    # corrupt an unrelated window.
    minute_key: str = ""
    day_key: str = ""
    cost_cycle_key: str = ""
    cost_day_key: str = ""
    # Persisted schedule positions are restored only when they still equal the reservation's
    # post-reservation value, so releasing one reservation cannot erase a later reservation.
    tokens_available_at_before: str = ""
    tokens_available_at_after: str = ""
    tokens_minute_before: int = 0
    requests_available_at_before: str = ""
    requests_available_at_after: str = ""
    provider_key: str = ""
    provider_requests_available_at_before: str = ""
    provider_requests_available_at_after: str = ""
    reserved_at: str = ""
    expires_at: str = ""


@dataclass
class RouteLedger:
    cost_used: float = 0.0
    cost_cycle_key: str = ""
    cost_day_used: float = 0.0
    cost_day_key: str = ""
    requests_minute: int = 0
    requests_minute_key: str = ""  # minute_key(now), UTC "YYYY-MM-DDTHH:MM"
    requests_day: int = 0
    requests_day_key: str = ""  # daily_reset_key(now, route.quota.reset_timezone)
    tokens_minute: int = 0
    tokens_available_at: str = ""  # end of the persisted average-TPM schedule
    requests_available_at: str = ""  # end of the persisted continuous route-RPM schedule
    inflight: dict[str, LLMReservation] = field(default_factory=dict)
    blocked_until: str = ""  # ISO datetime, UTC; set by a real 429 (§7.1)

    @property
    def inflight_count(self) -> int:
        return len(self.inflight)


@dataclass
class ProviderLedger:
    requests_available_at: str = ""  # shared provider-wide RPM schedule


@dataclass
class LLMBudget:
    routes: dict[str, RouteLedger] = field(default_factory=dict)
    providers: dict[str, ProviderLedger] = field(default_factory=dict)

    def find_inflight_owner(self, owner: str) -> str | None:
        """The model `owner` currently has a reservation under, if any -- used to resolve a
        dispatch-mode retry to the same route it originally reserved (§10.3)."""
```

`minute_key(now: datetime) -> str` still truncates UTC time to the minute for the legacy
`requests_minute`/`tokens_minute` counters and for loading older state. New RPM reservations use the
continuous `requests_available_at` schedule instead: each request advances it by `60 / rpm` seconds,
so a declared RPM is an average spacing requirement rather than a burstable fixed-minute bucket.
When a route has `provider_rpm`, the same interval is applied to a shared `ProviderLedger` keyed by
provider, so models from that provider cannot collectively submit faster than the configured global
rate. State written before these schedule fields existed remains readable; until a new reservation
populates a schedule, `available()` conservatively falls back to the legacy minute counters. TPM uses
the analogous `tokens_available_at` average-throughput schedule and retains the minute counter for
compatibility/telemetry. `daily_reset_key(now, tz_name)` converts `now` into the named IANA zone
(`zoneinfo.ZoneInfo`) and returns that zone's local date as `"YYYY-MM-DD"` — this is what makes Gemini's
bucket roll over at Pacific midnight, not UTC midnight, including across DST. `cost_cycle_key` reuses
`citypods.compute.budget.cycle_key(now)` directly — **do not reimplement it.**

`LLMBudget._ledger(model, now, *, route)` rolls each window independently against its own key,
mirroring `Budget._ledger`'s rollover-on-access idiom — rolling the minute window must never also
zero `cost_used` or vice versa:

```python
def _ledger(self, model: str, now: datetime, *, route: LLMRoute) -> RouteLedger:
    led = self.routes.setdefault(model, RouteLedger())
    mk = minute_key(now)
    if led.requests_minute_key != mk:
        led.requests_minute = 0
        if not led.tokens_available_at:
            led.tokens_minute = 0
        led.requests_minute_key = mk
    if led.tokens_available_at and datetime.fromisoformat(led.tokens_available_at) <= now:
        led.tokens_available_at = ""
    if led.requests_available_at and datetime.fromisoformat(led.requests_available_at) <= now:
        led.requests_available_at = ""
    if route.quota.rpd is not None:
        dk = daily_reset_key(now, route.quota.reset_timezone)
        if led.requests_day_key != dk:
            led.requests_day = 0
            led.requests_day_key = dk
    ck = cycle_key(now)  # from citypods.compute.budget
    if led.cost_cycle_key != ck:
        led.cost_used = 0.0
        led.cost_cycle_key = ck
    return led
```

`available` / `reserve` / `settle` / `release` — same semantics as `Budget`'s, extended to the extra
dimensions (signatures below are current as of the code-review pass; `reserve` is idempotent for an
already-inflight owner, and `settle`/`release` take `route`/`now` now that they roll each window
before correcting it — see the boxed warning after §10.2):

```python
def available(
    self, model: str, *, route: LLMRoute, requests: int, tokens: int, cost: float, now: datetime
) -> bool: ...  # also checks blocked_until (§7.1) first
def block(self, model: str, until: datetime, *, route: LLMRoute, now: datetime) -> None: ...
def reserve(
    self,
    owner: str,
    model: str,
    *,
    route: LLMRoute,
    requests: int,
    tokens: int,
    cost: float,
    now: datetime,
) -> None: ...
def settle(
    self,
    owner: str,
    model: str,
    *,
    route: LLMRoute,
    now: datetime,
    actual_requests: int | None = None,
    actual_tokens: int | None = None,
    actual_cost: float | None = None,
) -> None: ...
def release(self, owner: str, model: str, *, route: LLMRoute, now: datetime) -> None: ...
```

`available` checks only the dimensions the route actually declares (`rpm`/`rpd`/`tpm`/`concurrency`/
`cost_cap` may each be `None`, meaning untracked for that route — DeepSeek's `concurrency=None`, §4.1,
skips that check entirely rather than treating `None` as zero).

**How `tokens`/`requests`/`cost` are computed before every `available`/`reserve` call** (both
`select_route`'s gate 3 and `select_and_reserve`'s reservation step use the same formula, so they can
never disagree): `tokens = (estimate_tokens(job.inputs["messages"]) + DEFAULT_OUTPUT_TOKEN_MARGIN) *
max_provider_attempts`, where `max_provider_attempts` is `2` for a structured call and `1` otherwise
(**revised in the code-review pass** — Instructor's corrective retry, §12's `_run_structured_direct`,
can send a second real provider request for one logical dispatch; reserving only 1 undercounted
RPM/RPD/TPM whenever that retry fired). `requests` is `max_provider_attempts` too, for the same
reason. `cost = tokens * (route.pricing.input_per_token + route.pricing.output_per_token) *
active_multiplier`, where `active_multiplier` is the `multiplier` of whichever `PeakWindow` in
`route.pricing.windows` currently contains `now` (in that window's own `tz`), or `1.0` if none does.
This treats the whole token estimate as if split evenly across input/output pricing — a deliberately
crude approximation that only feeds a soft $ cap and the ranking tie-break (§5 gate 6), not a billing
record; `settle`'s `actual_cost` (§10.2) prices prompt/completion tokens separately when the response
provides the split (`_priced_actual` in `llm.py`), and is what any real reporting should read.

### §10.2 The settle-vs-release distinction — safety-critical, read this before writing the caller

> **This is the one place a straightforward-looking implementation goes subtly wrong.**
> `compute/budget.py`'s dispatch caller (`dispatch.py` ~L219-236) does `reserve → try: submit except:
> release`. Copying that pattern verbatim for LLM quota is a real bug: `release()` returns the
> reservation to the pool, which is correct for `compute/budget.py` because a **failed submit never
> ran** — but an LLM request that reached the provider and then errored (a timeout, a 5xx, a malformed
> reply) **did** consume a real RPM/RPD/TPM slot at the provider, whether or not it produced a usable
> result. Releasing that reservation would silently undercount real usage against Gemini's actual RPD,
> risking exactly the kind of quota overspend `compute/budget.py`'s own "$0 guarantee" was built to
> prevent — except here it's a rate-limit violation instead of a dollar overspend.
>
> The rule: **`release()` only if the HTTP/completion call was never attempted at all** (a failure in
> payload construction, structured-output model lookup, or anything else before the network call).
> **`settle()` for every other outcome** — success, timeout, non-200, structured-output validation
> failure — because the provider's own counter already incremented regardless of the outcome.
> `settle(actual_tokens=None)` leaves the token estimate charged when no usage figure is available
> (mirrors `Budget.settle`'s `actual=None` branch: "a job that ran must never return budget"); when
> the response includes a `usage` field, pass the real token count instead.

> **A second place the implementation initially got wrong (found in code review, since fixed):**
> `mutate_llm_budget`'s CAS retry loop resolved `now` once, before the loop, and reused it on every
> attempt. A conflict means a sibling committed first — possibly advancing the ledger's window in
> the process — and retrying with that stale `now` made `_ledger()` see an *older* window key than
> the one already on the freshly-loaded ledger, rolling it backward and zeroing usage the winner had
> legitimately just recorded. `mutate_llm_budget`/`select_and_reserve` now resolve a fresh
> `datetime.now(UTC)` on every attempt when the caller didn't pin an explicit value (tests still get
> a deterministic one). This bites in practice more than the identical-looking pattern in
> `compute/budget.py` does, because that one rolls over monthly (a CAS retry storm essentially never
> straddles a month boundary) while this one rolls over every minute.

### §10.3 CAS functions, owner uniqueness, and the deferred-result contract

```python
LLM_BUDGET_STATE_KEY = "state/llm_budget.json"


def load_llm_budget_cas(storage) -> tuple[LLMBudget, str | None]: ...
def mutate_llm_budget(
    storage,
    mutate,
    *,
    now=None,
    max_attempts=8,
    base_sleep=0.05,
    max_sleep=1.0,
    sleep=time.sleep,
    rng=None,
) -> LLMBudget: ...


def settle_route_reservation(
    storage,
    owner: str,
    model: str,
    *,
    route: LLMRoute,
    now=None,
    actual_tokens=None,
    actual_cost=None,
    **retry,
) -> LLMBudget: ...
def release_route_reservation(
    storage, owner: str, model: str, *, route: LLMRoute, now=None, **retry
) -> LLMBudget: ...
def block_route_until(
    storage, model: str, until: datetime, *, route: LLMRoute, now=None, **retry
) -> LLMBudget: ...
```

These mirror `load_budget_cas`/`mutate_budget`/`settle_reservation`/`release_reservation` (same
CAS-conflict retry loop, same `If-Match`/`If-None-Match` semantics). Unlike `compute/budget.py`,
there is no bare `reserve_route_if_available(storage, owner, model, ...)` for a single pre-chosen
route — R13 must choose *among* routes, so selection and reservation happen together in one CAS
loop, in `citypods/compute/llm_scheduler.py` (not `llm_budget.py`, to keep the dependency direction
one-way: `llm_scheduler.py` imports `llm_budget.py`, never the reverse):

```python
# citypods/compute/llm_scheduler.py


def select_and_reserve(
    storage,
    recipe_hash: str,
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute] = ROUTES,
    available_transports: Set[str],
    estimated_tokens: int,
    requests: int = 1,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> SelectionResult:
    """On each attempt: load the freshest ledger via CAS, run `select_route` against it, and if it
    picks a route, reserve that route with a conditional write. A CASConflict (a sibling shard won)
    re-reads and re-selects from scratch — the winner can legitimately differ between attempts if
    the losing route's quota changed. Returns a `SelectionResult` with `model=None` either because
    no route was ever eligible or because every attempt lost the CAS race `max_attempts` times."""
```

**Revised in the code-review pass — `recipe_hash`, not a caller-supplied `owner`.** Owner uniqueness
depends on the *selected* route's transport, which isn't known until selection completes, so
`select_and_reserve` derives it internally rather than taking it as a parameter:

- **`mistral-dispatch`**: owner *is* `recipe_hash`. The Worker dedupes on `idempotency-key:
  recipe_hash`, so a retry before this reservation settles is the *same* underlying provider
  request — double-reserving under a fresh owner would count quota for a call the Worker itself
  never repeats. Before reselecting, `select_and_reserve` checks `ledger.find_inflight_owner
  (recipe_hash)`; if that owner is already reserved, it reuses that *same* route rather than
  running selection again — a fresh pass might now pick a different (e.g. just-recovered) route,
  but the Worker's idempotency key still resolves to whatever was originally reserved.
- **`direct`**: owner is `f"{recipe_hash}:{uuid4().hex}"`, unique per attempt. Direct transport has
  no server-side dedup — a retry, a concurrent call, or a later `reconcile()` retry of a deferred
  handle (§10.7) is a genuinely new request and must reserve its own independent slot.

**Revised in the interface-unification pass — no more `LLMNotEligibleError` for "not eligible."**
The original draft had `run_inference` raise this exception when `select_and_reserve` returned no
route. It now returns a `JobHandle` instead, unconditionally, the same shape a genuine Mistral
dispatch already returns — see §6 and §10.7 for why and how that gets completed later.

### §10.4 Storage routing registration

`state/llm_budget.json` must be added to **both** `COORDINATION_PREFIXES` and `_EPHEMERAL_R2_PREFIXES`
in `citypods/storage/routing.py` (§12, LLM-SCHED-5) — `assert_coordination_prefixes_ephemeral()` runs
at import time and hard-fails any test or run that imports `routing.py` if a coordination prefix is
missing its ephemeral declaration. **Both entries must land in the same commit.**

```python
# COORDINATION_PREFIXES gains:
"state/llm_budget.json",

# _EPHEMERAL_R2_PREFIXES gains:
"state/llm_budget.json": (
    "LLM quota/cost ledger; re-initializes to zero spend/quota-used if lost — worst case is one "
    "over-count window before the provider's own 429s correct it, never a lost artifact"
),
```

### §10.5 What is deliberately *not* persisted in the ledger

No job records, no lifecycle states, no batch IDs, no defer reasons kept beyond a normal log line.
The ledger only ever answers "how much of this route's quota/budget is available right now, and when
does more become available" — nothing about *which* jobs are waiting on it. **This is still true
after §10.7's registry**: the registry is a separate object, keyed by `recipe_hash`, holding at most
a request's own portable payload (messages + policy) and its terminal state — it has no priority
ordering, no cross-job scheduling logic, and nothing in `llm_budget.py` reads or writes it. The two
were kept as two objects on purpose, not merged into one: the ledger is genuinely contended
coordination state (CAS, R2, never listed); the registry is a plain cache (no CAS needed, must be
listable for the sweep) — conflating them would have forced the wrong storage/consistency model onto
at least one of the two.

### §10.6 Migration / backfill

`state/llm_budget.json` is new state with no prior schema — there is nothing to migrate and nothing to
backfill. `load_llm_budget_cas` returns an empty `LLMBudget()` when the object doesn't exist yet (same
`got is None` branch `load_budget_cas` already uses), and the first successful reservation creates it
via `if_none_match="*"`. No existing state file changes shape; `compute_budget.json` is untouched.
`state/llm_deferred/*.json` (§10.7) is likewise new state with nothing to migrate.

### §10.7 Deferred-request registry and sweep (new, 2026-07-17)

**What it's for.** §6 covers *why* this exists (a caller with no natural retry cadence of its own
shouldn't have to reconstruct its whole request). This subsection covers the concrete shape.

**Storage: B2, not R2 — the opposite choice from the ledger, deliberately.** `citypods/compute/
llm_deferred.py` keys records as `state/llm_deferred/<recipe_hash>.json`, written and read through
the *bulk* `StorageBackend` interface (`put_file`/`get_file`/`list_objects` — the same interface
`statesync.py` already uses for B2 records), not the CAS interface the ledger uses. Two reasons:
listability (the sweep must discover pending records without knowing recipe hashes in advance, and
R2 coordination prefixes are explicitly never listed in this codebase — `storage/routing.py`), and
the actual write pattern doesn't need CAS in the first place (only the original caller ever writes
a "pending" record for a given `recipe_hash`; only the sweep, or a later call for that same
`recipe_hash`, ever writes "completed" — no concurrent-writer race to arbitrate).

**Record shape.** Two states, `"pending"` and `"completed"`, sharing a `recipe_hash`/`task`/
`created_at`:

```python
def write_deferred(
    storage, recipe_hash: str, result: JobResult | JobHandle, *, now=None
) -> None: ...
def look_up_deferred(storage, recipe_hash: str) -> JobResult | JobHandle | None: ...
def list_pending_deferred(storage) -> list[JobHandle]: ...
def prune_expired_deferred(storage, *, now=None, ttl_days: float = 38) -> int: ...
```

A pending record round-trips a `JobHandle` in full, including `deferred_request` when present (a
deferred-direct handle) or the plain dispatch fields (`model`/`owner`/`input_per_token`/
`output_per_token`/`ref`) when absent (a genuine in-flight Mistral handle) — `reconcile()` (§10.7's
next paragraph) branches on that same `deferred_request is not None` check either way, so the
registry doesn't need its own separate marker for which kind of pending record it's holding.

**`write_deferred` never downgrades a completed record back to pending** (a stale writer racing
behind a completion, its own or the sweep's, must not make a finished result look unfinished again),
and **preserves `created_at` across re-defers** (a record re-written as still-pending keeps the
timestamp of its first write, not the retry's, so TTL cleanup measures the age of the whole request
lineage).

**How `run_inference` and `reconcile()` use it:**

- `run_inference`, for any policy-bearing call, checks `look_up_deferred` *first*. A completed
  record returns immediately — no selection, no dispatch, no second real provider call for a
  repeated ask with the same `recipe_hash`. A pending record returns as-is. Only if there's no
  record at all does it run `_run_policy_job` (§6/§10.3) and write whatever comes back (`JobResult`
  or a fresh `JobHandle`) before returning.
- `reconcile(handle)`: if `handle.deferred_request is not None`, reconstruct the original
  `InferenceJob` from it and re-run `_run_policy_job` fresh (same gates, same possibility of
  landing on a *different*, now-cheaper route); write the outcome back either way. Otherwise
  (a genuine Mistral handle), poll the Worker's URL as before, and now also write the completed
  result to the registry once settled, so a caller that only ever calls `run_inference` again
  (never explicitly reconciling) still transparently gets it.

**The sweep** (`scripts/llm_deferred_sweep.py`, `.github/workflows/llm-deferred-sweep.yml`):
constructs one `LiteLLMBackend` with `dispatch_url` configured (§2 — reaching both transports
regardless of which one originally claimed a given pending record), calls `list_pending_deferred`,
`reconcile()`s each, and finally `prune_expired_deferred`. Reconciling one bad record must not abort
the sweep for the rest — each `reconcile()` call is wrapped and its failure logged, not raised.

**Cadence: once daily, timed outside the known peak windows, not a tight cron.** Scheduled for 17:30
UTC — outside DeepSeek's current `01:00–04:00` and `06:00–10:00 UTC` peak windows (§8) — rather than
every few hours.
GitHub Actions cron minutes are worth conserving, and nothing configured today needs a finer wake-up:
an ~8-hour-wide window is comfortably caught by one daily check even accounting for GitHub's own
cron jitter. **This is a per-window decision, not a fixed cadence** — if a future route's discount
or quota-reset window can't be reasonably caught by a once-daily check (something narrower than a
few hours wide), add another cron entry timed to *that* window rather than tightening this one
globally for everything.

**TTL: 38 days, and never before a caller's own longer deadline.** The worst case a pending record
should ever need to sit unclaimed: a caller with no `deadline_at` (or one whose own identity changes
every run, so it never looks the same `recipe_hash` up again — see the city-discovery note below)
waiting out a full monthly cost-cycle reset (`cost_cycle_key`, §10.1, up to ~31 days if the cap was
hit right after a rollover), plus a few days' slack. `prune_expired_deferred` never deletes a record
before `max(created_at + 38 days, deadline_at)` — a caller that deliberately set a longer deadline
(waiting out something like that same monthly cap) must not have its still-pending request silently
vanish before that deadline arrives. Applies uniformly to completed and pending records; a completed
record nobody ever looks up again is just as much clutter.

**A known, accepted gap: not every caller's `recipe_hash` is stable enough to benefit fully.** City
discovery's `recipe_hash` folds in that day's Tavily search results, so it changes on every re-run —
the registry and sweep still work correctly for it (no bug), but it never gets a completed-record
cache hit or a same-day sweep completion, since by the time either would help it's already moved on
to a new hash. This is not a reason to weaken `recipe_hash`'s content-addressing (a stale
classification must not get served just because the identity was made cheaper to reuse) — it's a
property of that specific caller, not a flaw in the registry. City discovery also doesn't need the
registry's deferral behavior in the first place: it asks only for a free route with no deadline
(§5), so "not eligible" for it just means its own existing daily retry (`ClassificationDeferred` →
`DEFERRED_EXIT`) tries again tomorrow, unchanged since before R13.

## §11. Additional recommendations

1. **Versioned quota/pricing policy.** Store the policy version used for every decision; provider
   limits and prices change independently of code releases (this matters concretely for §8: DeepSeek's
   V4 window is expected to change within weeks).
2. **Cache by request fingerprint.** ✅ **Implemented (§10.7)** as the deferred-request registry:
   `run_inference` checks `look_up_deferred(recipe_hash)` before doing anything else, so an
   identical prompt/context/schema/model request never consumes quota twice. Keys on the same
   `recipe_hash` the Worker's own idempotency-key already uses — one fingerprint, not two.
3. **Separate evaluation from production, structurally, not by a flag.** A scorer request already gets
   this for free from §3: pass an explicit `allowed_models` singleton (or small list) and
   `allow_paid=True`; it can never silently consume the production free-route budget because the
   allowlist excludes it from ever being a candidate there.
4. **Starvation/aging is not R13's job.** The deferred-request registry (§10.7) is a cache, not a
   queue — it has no priority ordering, and the sweep processes every pending record it finds each
   run rather than picking favorites. Ordering among a Stage's own pending items is still that
   Stage's existing backlog policy (H5 windowed-recency, `ops/workqueue.py`; review/27's
   provider-priority ordering). R13 only ever answers "is a route eligible right now," not "whose
   turn is it."
5. **Expose explainable decisions.** Every rejection from §5 should log which gate rejected which
   candidate route and why (`SelectionResult.rejected`) — this is a log line at decision time, not a
   persisted "why" ledger entry, now that §6 removed the job-lifecycle table.
6. **Keep provider data out of feature stages.** Stages request capability and policy; they should not
   inspect Gemini reset times, DeepSeek windows, or Worker URLs. Unchanged from the initial draft.

## §12. Implementation sequence and acceptance criteria

### §12.1 Module dependency graph

```text
llm_policy.py  (no internal deps)
     │
     ├──────────────────────────────┐
     ▼                              ▼
llm_budget.py  (deps: llm_policy)  llm_deferred.py  (deps: base, llm_policy -- not llm_budget)
     │
     ▼
llm_scheduler.py  (deps: llm_policy, llm_budget)
     │
     ▼
llm.py  (deps: llm_scheduler, llm_budget, llm_policy, llm_deferred)   ← existing file, edited
```

`llm_deferred.py` (§10.7, new in the interface-unification pass) sits beside `llm_budget.py`, not
above or below it — the registry and the ledger are independent objects (§10.5) that only meet
inside `llm.py`'s `run_inference`/`reconcile()`.

Plus two independent, order-sensitive edits: `storage/routing.py` (LLM-SCHED-5), which must land no
later than LLM-SCHED-2 (its `assert_coordination_prefixes_ephemeral()` guard runs at import time);
and `citypods/discovery/classify.py` (LLM-SCHED-9), which can land any time after LLM-SCHED-4 since
it's just a policy value change at an existing call site.

### §12.2 Issues, in build order

**LLM-SCHED-1 — Route & policy types.** New file `citypods/compute/llm_policy.py`. Pure dataclasses
only, no I/O: `LLMRequestPolicy`, `PeakWindow`, `PricingPolicy`, `QuotaPolicy`, `LLMRoute` (§3–§4), the
concrete `ROUTES: dict[str, LLMRoute]` table (§4.1), `estimate_tokens(messages: list[Mapping[str,
Any]]) -> int` (sum `len(content)` across messages, divide by 4 as a chars-per-token heuristic, round
**up** — biasing the estimate high costs a little ledger slack; biasing low risks real overspend), and
`DEFAULT_OUTPUT_TOKEN_MARGIN = 1024` (tokens reserved in addition to the input estimate, to absorb
response length before settlement corrects it to actual usage). No dependency on any other new module.
**Test** `tests/test_compute_llm_policy.py`: construct each dataclass; assert `{r.model for r in
ROUTES.values()} == SUPPORTED_MODELS` (imported from `citypods.compute.llm`) so the two tables can't
silently drift; a few `estimate_tokens` cases.

**LLM-SCHED-2 — CAS quota+cost ledger.** New file `citypods/compute/llm_budget.py`. `LLMReservation`,
`RouteLedger`, `LLMBudget` with `_ledger`/`available`/`reserve`/`settle`/`release`/`to_dict`/`from_dict`
(§10.1), `minute_key`, `daily_reset_key` (§10.1), `load_llm_budget_cas`, `mutate_llm_budget`,
`settle_route_reservation`, `release_route_reservation` (§10.3). Depends on LLM-SCHED-1 for `LLMRoute`/
`QuotaPolicy`. Reuse `citypods.compute.budget.cycle_key` for the $ cycle key — do not reimplement it.
**Test** `tests/test_compute_llm_budget.py`, mirroring `TestBudgetCAS` in `tests/
test_compute_dispatch.py` (reuse that file's `_MemBucket` fake-CAS-storage pattern — an in-memory
object store with `get_bytes`/`put_cas` enforcing `If-Match`/`If-None-Match`, `cas_capable = True`):
reserve/settle round-trip; reserve/release round-trip (never both settle *and* release the same
`owner`); RPD rollover at Pacific midnight across both a spring-forward and a fall-back date (e.g.
2026-03-08 and 2026-11-01, using `zoneinfo.ZoneInfo("America/Los_Angeles")`); a concurrent-shard CAS
conflict retry (inject one `CASConflict` the way `TestBudgetCAS` already does) proving two shards can't
jointly overspend; `available()` returns `True` when a dimension is `None` on the route regardless of
ledger state (DeepSeek's unset `concurrency`).

**LLM-SCHED-3 — Pure selection function.** New file `citypods/compute/llm_scheduler.py`. `SelectionResult`,
`select_route` (§5, §6). Depends on LLM-SCHED-1 (types) and LLM-SCHED-2 (`LLMBudget`/`RouteLedger`
types and `.available()` — read-only, no CAS). **Test** `tests/test_compute_llm_scheduler.py`, all
against an in-memory `LLMBudget()` with no storage double needed (pure-function tests, fast): (a) the
24-hour city-onboarding scenario — a free route with exhausted RPD and a paid route both configured,
`deadline_at` close enough that only the paid route survives gate 4, assert it wins; (b) the same setup
with `deadline_at` far enough out that the free route survives — assert the paid route is never chosen
even though it's eligible (gate 6 ranking prefers free); (c) an evaluation caller with `allowed_models=
("deepseek/deepseek-v4-pro",)`, `allow_paid=True` while a free Gemini route is also configured and
eligible — assert DeepSeek is still selected, proving the allowlist is exact, not a preference; (d) a
DeepSeek price-window deferral case against a synthetic `now` inside a peak window with a distant
deadline — assert `SelectionResult.model is None`, `retry_at` is the peak-window end, and the rejection
reason names the price-window gate; (e) the same case with `now` inside the cheapest window — assert
it's selected; (f) the same peak case with `deadline_at` inside the current window — assert the deadline
override fires and DeepSeek is selected anyway, at the higher active price.

**LLM-SCHED-4 — CAS selection+reservation wrapper, and wiring into `LiteLLMBackend`.** Extends
`citypods/compute/llm_scheduler.py` with `select_and_reserve` (§10.3). Edits `citypods/compute/llm.py`:

- Add `LLMNotEligibleError(LLMBackendError)` (§10.3).
- `LiteLLMBackend.__init__` gains `storage=None` (a CAS-capable storage object). When `storage is
  None` and a job carries `llm_policy`, raise `LLMBackendError("LLM scheduler requires a CAS-capable
  storage backend")` immediately — an explicit failure, never a silent bypass to the old behavior.
- `run_inference`: read `policy = job.inputs.get("llm_policy")` first. **If `policy is None`, the rest
  of the method is byte-for-byte unchanged from today** — this is the regression boundary LLM-SCHED-4's
  tests must prove, not just assert informally. If `policy is not None`: call `select_and_reserve(...)`;
  if `result.model is None`, raise `LLMNotEligibleError(result.reason)`; otherwise use `resolved_model
  = result.model` for this call.
- **Exact call-site checklist — every one of these currently reads `self.config.model` and must
  instead take the resolved model as a parameter, or a DeepSeek/Gemini call silently gets the wrong
  Instructor mode or response-format shape:**
  - `_instructor_mode(self, model: str)` — was `self.config.model.startswith("deepseek/")`.
  - `_provider_options(self, job, model: str)` — was `{"model": self.config.model}`.
  - `_dispatch_response_format(self, model: ResponseModel, resolved_model: str)` — was
    `self.config.model.startswith("deepseek/")`.
  - `_payload(self, job, model=None, *, resolved_model: str)` — was `"model": self.config.model`.
  - `run_inference` computes `resolved_model` once (either `self.config.model` when `policy is None`,
    or `result.model` when set) and threads it through all four call sites above. The constructor's
    existing `self.config.model not in SUPPORTED_MODELS` validation is unchanged — it still validates
    the instance's *default* configured model, not the per-call resolved one.
- Settlement, per §10.2: reserve happens inside `select_and_reserve`, before the network call.
  Everything from payload construction up to (but not including) the actual `completion(...)` call or
  `self._session.post(...)` call, if it raises, calls `release_route_reservation`. Everything from that
  network call onward — success, exception, non-200 — calls `settle_route_reservation`, extracting
  `actual_tokens` from the response's `usage` field when present, `actual_cost` computed from
  `actual_tokens * route.pricing.{input,output}_per_token` when known, and leaving both `None`
  otherwise (keeping the estimate charged, §10.2).

**Test** extends `tests/test_compute_llm.py`, reusing its existing `job(...)` factory: a regression
test asserting the exact `calls[0]` payload from `test_direct_litellm_call_is_normalized` is unchanged
when no `llm_policy` is supplied; a policy-present + eligible-route case (fake CAS storage, mock
`completion`) asserting the resolved model appears in the outgoing payload and the ledger shows a
settled reservation; a policy-present + no-eligible-route case asserting a deferred `JobHandle` and
*no* ledger mutation survives (release, not a stuck reservation) -- **revised**: this originally
asserted `LLMNotEligibleError`, before the interface-unification pass removed it (§10.3); a simulated
failure *after* the mocked `completion`/`post` call raises, asserting `settle_route_reservation` was
called, not `release`.

**LLM-SCHED-5 — Storage routing registration.** Edits `citypods/storage/routing.py` (§10.4): add
`"state/llm_budget.json"` to both `COORDINATION_PREFIXES` and `_EPHEMERAL_R2_PREFIXES` in the same
commit. Must land no later than LLM-SCHED-2 lands in `main` (the import-time assertion in `routing.py`
will fail any test run otherwise); in practice, land it in the same PR as LLM-SCHED-2. **Test**: one
explicit assertion that both entries exist (a regression guard beyond the automatic import-time check).

**LLM-SCHED-6 — Live provider wiring + acceptance (Phase B).** No new files. Confirms `ROUTES`'
Gemini/DeepSeek figures against current provider docs immediately before merge (prices and windows
drift, §11.1); end-to-end test that a flexible DeepSeek job deferred outside the discount window
dispatches once `now` is re-run inside it; confirms the Mistral Worker's own test suite
(`workers/llm-dispatch-proxy`) is untouched — no new test needed there, an unmodified passing suite is
the proof.

**LLM-SCHED-7 — Code-review fixes (shipped 2026-07-17, same PR as LLM-SCHED-1…6).** All in existing
files, no new modules: the stale-CAS-timestamp fix and `LLMReservation` window-key tracking
(§10.1/§10.2's boxed warnings); `_usage_tokens` returning `None` instead of `0` for missing/invalid
usage; the worst-case `max_provider_attempts` reservation for structured calls (§10.1); the
transport-dependent owner-uniqueness split and `find_inflight_owner` reuse-on-retry (§10.3); split
prompt/completion-token pricing (`_priced_actual`) and pricing captured on the handle at reservation
time rather than re-read from live `ROUTES` at reconcile time (`JobHandle.input_per_token`/
`output_per_token`, §10.2). **Tests**: extends the LLM-SCHED-2/3/4 suites in place — see the actual
`tests/test_compute_llm_budget.py`/`test_compute_llm_scheduler.py`/`test_compute_llm.py` for the
concrete cases (a settle/release-across-a-minute-boundary regression, an owner-reuse-on-retry case
at the scheduler level, a `_priced_actual` split-vs-combined-rate comparison, and so on).

**LLM-SCHED-8 — `available_transports`, reactive rate-limiting, and the deferred-request registry
(shipped 2026-07-17).** The interface-unification pass. New file `citypods/compute/llm_deferred.py`
(§10.7): `write_deferred`/`look_up_deferred`/`list_pending_deferred`/`prune_expired_deferred`. New
file `scripts/llm_deferred_sweep.py` + `.github/workflows/llm-deferred-sweep.yml` (§10.7's cadence).
Edits: `llm_policy.py` gains `DeferredLLMRequest` (§3) and drops the short-lived `defer_as_handle`
flag (§3 — unconditional now); `llm_budget.py` gains `RouteLedger.blocked_until` + `LLMBudget.block`
+ `block_route_until` (§7.1); `llm_scheduler.py`'s `select_route`/`select_and_reserve` replace
`backend_mode: Literal["direct", "dispatch"]` with `available_transports: Set[str]` (§2, §5 gate 0)
and `select_and_reserve` takes `recipe_hash` instead of a caller-supplied `owner` (§10.3); `llm.py`
gains `_available_transports()` (§2), registry checks in `run_inference`/`reconcile()` (§10.7), 429
detection and the deferred-handle return path (§7.1), and `_run_policy_job` as the shared
selection-attempt-settle body used by both a first attempt and a deferred-handle retry. **Tests**:
new `tests/test_compute_llm_deferred.py` (registry round-trips, the never-downgrade and
`created_at`-preservation rules, TTL pruning including the longer-caller-deadline case) and
`tests/test_llm_deferred_sweep.py` (the sweep script's summary counts, one bad record not aborting
the rest); extends `tests/test_compute_llm.py` with 429-triggers-a-deferred-handle cases for both
transports and `tests/test_compute_llm_scheduler.py` with an `available_transports`-reaches-both
case.

**LLM-SCHED-9 — City discovery: free-only, no deadline (shipped 2026-07-17).** One-line policy
change at the only existing call site, `citypods/discovery/classify.py`'s `classify()`: from
`LLMRequestPolicy(allow_paid=True, deadline_at=now + timedelta(hours=24), purpose="city-onboarding")`
to `LLMRequestPolicy(allow_paid=False, purpose="city-onboarding")` (§5). City discovery acts on the
result immediately and must never silently spend money on a synchronous, human-visible cycle; it
already owns its own daily retry for "not eligible right now," so it doesn't need R13 to track a
deadline for it. **Test**: updates the existing prompt-construction assertion in
`tests/test_discovery.py` to check `allow_paid is False`/`deadline_at is None`.

### §12.3 Acceptance criteria

- A free-eligible job never selects a paid model when a free route can meet its deadline.
- An evaluation caller can force a specific model — free or paid — via an explicit allowlist,
  independent of production's free-route protection.
- Gemini quota reservations roll over at Pacific midnight (including across DST) and never overspend
  across concurrent shards — proven by the CAS ledger's conflict-retry test, not by luck.
- A reservation that is settled is never also released, and vice versa (§10.2) — proven by a test that
  simulates a post-dispatch failure and asserts the reservation stays counted.
- Flexible DeepSeek work waits for the configured cheapest window without ever missing a caller's
  deadline; urgent work may use the currently active price.
- A stale or missing price/quota policy disables deliberate delay and still lets the job dispatch — it
  never deadlocks work.
- A job with no `llm_policy` behaves identically to `LiteLLMBackend` before this item existed — proven
  by a byte-for-byte payload comparison, not an informal read-through.
- Mistral Worker pacing remains provider-wide and conservative; Python and Worker admission both
  space configured RPM continuously, while the Worker's queue/claim pacing remains unchanged.
- Every rejection is explainable from `SelectionResult.rejected`, with no separate persisted job-*
  lifecycle* table to keep in sync (the deferred-request registry, §10.7, is a plain two-state cache,
  not a scheduler's job table).
- A real 429 blocks that specific route until (at least) its `Retry-After` hint, without surfacing as
  a raw exception, and without the next attempt immediately retrying into the same error.
- A caller that submits a policy-bearing job, gets a deferred `JobHandle` back, and later calls
  `run_inference` again with the *same job* (never touching the handle itself) transparently gets
  the completed result once it's ready — proven end to end against the sweep, not just unit-level.
- A repeated ask with the same `recipe_hash` never triggers a second real provider call once the
  first has completed.
- A backend configured with `dispatch_url` set can select and complete either transport in one
  instance, regardless of `mode` — the concrete case the sweep needs.

## §13. What changed, and why

### §13.1 Initial draft → first L3 pass (2026-07-16)

| Cut or changed | Reason |
|---|---|
| Central durable scheduler queue + 7-state job lifecycle | Duplicated durable state every calling Stage already owns; replaced by a stateless selection function re-evaluated each scheduled run (§6). |
| Second "Gemini quota Worker" (own R2 queue, async enqueue/poll) | Solves a problem (idle-runner pacing) Gemini's generous free tier doesn't have; a CAS ledger check from inside the calling run is sufficient and matches the H14d/H17 precedent already in this codebase (§7). |
| `LLMRequestPolicy`'s `selection`/`cost_policy` enums, `max_wait_seconds`, `priority` | All derivable from `allowed_models` cardinality, `allow_paid`, and `deadline_at` — see the mapping table in §3. |
| `BatchTransport` protocol / generic batch capability | No confirmed batch-capable provider exists yet in `SUPPORTED_MODELS`; premature per review/27 §8.2's own stated philosophy (§9). |
| DeepSeek peak-surcharge window figures (09:00–12:00/14:00–18:00 `Asia/Shanghai`) | Unsourced in the initial draft and inconsistent with review/27 §5.3's independently researched, dated figure (16:30–00:30 UTC off-peak discount). Adopted review/27's figure as authoritative (§8). |
| review/27 §8's $ budget ledger treated as a separate, later concern | Per maintainer decision, ships as the same CAS object as R13's quota ledger, built once (§10). |
| Model selection implicitly assumed to be able to cross transports | Added an explicit gate-0 transport constraint (§2, §5): the scheduler picks a model, never a transport; a `direct`-mode backend never becomes a `dispatch`-mode call mid-request. **Reversed in §13.2 below** once a real caller (the sweep) needed exactly this. |
| Reservation release semantics on any failure | Added the explicit settle-vs-release distinction (§10.2): only release when the network call was never attempted; settle (keep quota charged) for every outcome after that, since the provider's own counter already moved. Not present at all in the initial draft, and easy to get backwards by copying `compute/budget.py`'s GPU-dispatch pattern verbatim. |
| **Kept unchanged** | Allowlist + free-model-protection semantics; RPM/RPD/TPM as independent per-route dimensions, with optional provider-wide RPM pacing; recipe-hash/artifact-identity independence from scheduling policy (§3); "no provider data in Stages" (§11.6); DeepSeek off-peak *preference*, not a hard constraint (§8, itself carried over from review/27 §5.3). |

### §13.2 First L3 pass → implementation + code review + interface unification (2026-07-17)

| Cut or changed | Reason |
|---|---|
| Fixed per-instance `mode` gating which transports the scheduler can pick from (§2) | Replaced with `available_transports`, computed from `dispatch_url` presence — needed the moment a real caller (the deferred-request sweep, §10.7) had to reach both transports from one instance to service a mixed bag of pending records. |
| `defer_as_handle: bool` opt-in on `LLMRequestPolicy` (a short-lived addition, never shipped to `main`) | Removed in favor of *unconditional* handle-return: dispatch-mode already returned a `JobHandle` on a 202 with no opt-in, and gating the direct-mode case behind a flag was exactly the "caller has to know which transport backs which model" leak this design otherwise removes (§3, §6). |
| `LLMNotEligibleError` raised for "nothing eligible right now" | Removed; `run_inference` returns a `JobHandle` instead, uniformly with every other "not done yet" reason (§6, §10.3). |
| `select_and_reserve(owner: str, ...)` — caller-supplied, single owner-uniqueness rule | Owner is now derived internally from the *selected* route's transport (`recipe_hash` for dispatch, a unique suffix per attempt for direct) — a single rule was actually two different correctness requirements wearing one name (§10.3). |
| No mechanism to complete a deferred request without the caller rebuilding it | Added the deferred-request registry + a daily sweep (§10.7) — the concrete answer to "guarantee a response from model Y at the lowest cost within X days, deferral is fine," which the original draft's `deadline_at` correctly *bounded* but never made hands-off for a caller with no retry cadence of its own. |
| No handling for a real provider 429 | Added reactive rate-limiting (§7.1): `RouteLedger.blocked_until`, duck-typed 429 detection, and the same deferred-handle completion path gate 3's proactive exhaustion already used. |
| Several ledger correctness bugs found in code review | Stale CAS-retry timestamps rolling a window backward (§10.2's second boxed warning); `_usage_tokens` treating missing data as zero instead of unknown; under-reserving Instructor's possible second attempt; naive combined-rate pricing instead of splitting prompt/completion tokens; pricing re-read from live config instead of the rate a reservation actually reserved under. All fixed in the same PR as the initial implementation (§12, LLM-SCHED-7). |
| City discovery's policy (`allow_paid=True`, a 24h deadline) | Changed to `allow_paid=False`, no deadline (§5, §12 LLM-SCHED-9) — it acts on results synchronously and must never silently spend money or wait days for a discount; it already owns its own daily retry for "not now." |
| **Kept unchanged** | The pure selection function and its six gates (§5); the shared ledger's core reserve/settle/release model (§10.1); the CAS-retry pattern mirroring `compute/budget.py` (§10.3); DeepSeek off-peak preference and its sourced figures (§8); the whole "no central scheduler queue" philosophy -- the registry is a cache, not a queue (§10.5, §10.7). |

### §13.3 Post-implementation correction — TPM burst/debt semantics and persisted rollover

The original implementation treated `tpm` like a fixed one-minute request bucket. That was too
strict for provider limits documented as average tokens-per-minute throughput: a request larger
than one minute's nominal TPM can be accepted, provided subsequent work is paced to repay the
token debt. The implementation now keeps ordinary burst capacity up to `tpm` and, when a request
pushes the route above that amount, records `tokens_available_at` at approximately
`now + (tokens_in_burst / tpm) * 60 seconds`. This is provider-neutral and applies to direct Python
reservations and the dispatch Worker's per-route ledger. A provider can still reject a request for
context-size, model-specific, or actual rate-limit reasons; those live signals remain authoritative.

The CAS selection path also persists window/day rollover changes when no route is reservable, so
an old `requests_day_key` cannot remain visible merely because every current candidate was blocked.
These changes affect only ephemeral coordination ledgers and do not invalidate durable LLM outputs.

The same correction now applies to request pacing. RPM is a rate, not a fixed-minute allowance:
`requests_available_at` advances by `60 / rpm` seconds for every reserved request. A configured
`provider_rpm` uses a shared provider ledger, so all models on that provider are spaced against one
global schedule; the Worker consumes the compiled `provider_rpm` value from `dispatch_limits.json`
and mirrors this state in `state/dispatch_budget.json`. Older ledgers without schedule fields remain
readable through a conservative minute-counter fallback, and new reservations migrate the affected
route/provider into continuous pacing. Releases and settlements restore a schedule only when its
current value still matches that reservation's recorded post-reservation value; token counters are
restored with the token schedule so a release after a minute rollover cannot leave stale TPM debt.

## §14. Open decisions

- DeepSeek's actual concurrency ceiling — unmeasured; ships as `None` (untracked), added once real
  429 telemetry establishes a number (§4.1). The new reactive `blocked_until` (§7.1) partially covers
  this gap in the meantime, but only after the fact (a real 429), not proactively.
- DeepSeek V4's actual off-peak window and multiplier, pending its exit from preview — the mechanism in
  §8 is built now; the specific numbers are a config update once confirmed.
- Whether DeepSeek's "batch" is a real submit/poll API or just off-peak-rate-via-async (review/27
  §5.3's own open item) — resolves independently of R13, and only matters once §9 is revisited.
- Whether city onboarding should prefer the newest eligible free job or oldest-deadline-first once
  concurrent onboarding volume makes that distinction matter (not yet, at current volume; also now
  somewhat moot for city discovery specifically, since §5/§12 LLM-SCHED-9 removed its deadline
  entirely).
- ~~Whether a single `LiteLLMBackend` instance should ever transparently choose between `direct` and
  `dispatch` transport for the same request~~ — **resolved 2026-07-17**: yes, via
  `_available_transports()` (§2), once the deferred-request sweep (§10.7) turned out to need exactly
  this.
- Whether the deferred-request registry (§10.7) needs a real claim/lock (mirroring H17's CAS lease
  ledger) rather than its current best-effort "check the registry, then attempt" pattern. Today a race
  between two concurrent callers submitting the *same* recipe_hash at nearly the same time can result
  in two real direct-transport dispatches (both correctly ledger-counted via unique owners, §10.3 —
  not a quota-safety bug, just a redundant provider call and a benign last-writer-wins registry
  overwrite). Left as best-effort deliberately: this codebase's LLM verb callers aren't sharded the
  way ASR work is, so the race is a genuine edge case, not the common path; revisit only if a real
  caller's usage pattern makes it one.
- Whether the sweep ever needs more than one daily cron entry — only once a future route's discount
  or quota-reset window is narrow enough that a once-daily check can't reasonably catch it (§10.7).
