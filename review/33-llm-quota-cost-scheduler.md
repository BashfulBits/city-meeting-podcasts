# review/33 — LLM Quota, Cost-Window & Batch Scheduler (R13)

**Maturity: L3 · revised 2026-07-16 (simplification pass on the initial L2 draft) · completed to
dev-ready depth 2026-07-16 · ROADMAP R13 · depends on R2 and R10 · co-ships the still-unbuilt
review/27 §8 budget ledger as one shared object**

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
3. prefer DeepSeek's off-peak discount window rather than dispatching at full price when the work
   isn't time-sensitive;
4. honor a caller's allowed-model constraint, including a scorer that must run the same job against
   several specifically selected models; and
5. never choose a paid model when the caller permits a free model and that free model can complete
   within the caller's deadline.

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

**Scoping constraint, stated up front because it shapes every gate in §5:** one `LiteLLMBackend`
instance is constructed with a fixed `mode` (`"direct"` or `"dispatch"`, `LLMBackendConfig.mode`).
R13's scheduler chooses **which model**, never which transport — it only ever selects among routes
whose `transport` matches the instance's already-configured `mode`. A `mode="direct"` backend's
scheduler-eligible candidates are therefore Gemini and DeepSeek today; Mistral (`transport
="mistral-dispatch"`) is declared in the route table (§4) for documentation and future use but is
never a candidate for a direct-mode instance. Making one backend instance transparently switch
transport per request is a real future capability, but it multiplies the risk of this item for no
capability anyone has asked for yet — it is explicitly out of scope (§14).

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
    tz: str                  # IANA zone the provider publishes the window in
    start: time               # local wall-clock time in `tz`
    end: time                  # local wall-clock time in `tz`; may be < start (window crosses midnight)
    multiplier: float          # 0.5 = 50% off during this window; >1.0 = a surcharge

@dataclass(frozen=True)
class PricingPolicy:
    input_per_token: float = 0.0
    output_per_token: float = 0.0
    windows: tuple[PeakWindow, ...] = ()
    cost_cap: float | None = None   # soft $ cap per cycle (review/27 §8.1); None = untracked/uncapped

@dataclass(frozen=True)
class QuotaPolicy:
    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    concurrency: int | None = None
    reset_timezone: str = "UTC"       # IANA zone; only meaningful when `rpd` is set
    # rpm/tpm are always per-minute by definition and need no separate period field.

@dataclass(frozen=True)
class LLMRoute:
    model: str
    transport: Literal["direct", "mistral-dispatch"]
    free: bool
    quota: QuotaPolicy
    pricing: PricingPolicy
```

There is no separate `TimingPolicy` or `BatchCapability` type in v1 — the only timing concept that
exists today is price-window preference, which is pricing data (`PricingPolicy.windows`), and batch
capability is deferred (§9).

### §4.1 The concrete route table

`citypods/compute/llm_policy.py` ships a module-level `ROUTES: dict[str, LLMRoute]` covering exactly
the five model strings already in `llm.py`'s `SUPPORTED_MODELS` — LLM-SCHED-1 (§12) asserts that
equality so the two tables cannot drift apart:

| `model` | `transport` | `free` | quota | pricing |
|---|---|---|---|---|
| `gemini/gemini-3-flash-preview` | `direct` | `True` | `rpm=10, rpd=1500, tpm=250_000, reset_timezone="America/Los_Angeles"` (review/27 §2) | `0.0 / 0.0`, no windows, no cap (free) |
| `deepseek/deepseek-v4-flash` | `direct` | `False` | `concurrency=None` (§14 — no measured ceiling yet) | `input=0.14e-6, output=0.28e-6` (review/27 §2), `windows=(PeakWindow("UTC", time(16,30), time(0,30), 0.5),)` (review/27 §5.3, **V3/R1-confirmed, V4 provisional** — see §8) |
| `deepseek/deepseek-v4-pro` | `direct` | `False` | `concurrency=None` | `input=0.435e-6, output=0.87e-6` (review/27 §2.1), same off-peak window as flash |
| `mistral/mistral-large-latest` | `mistral-dispatch` | `True` | `rpm=2` (review/27 §2, "~2 RPM (plan for 1/min)") | `0.0 / 0.0`, no windows — never a scheduler candidate for a `direct`-mode backend (§2) |
| `mistral/mistral-large-3` | `mistral-dispatch` | `True` | `rpm=2` | same as above |

DeepSeek's `concurrency=None` is an honest gap, not a placeholder: review/27 §2 records DeepSeek as
"concurrency-based, not RPM/RPD" but never measured the actual ceiling. Ship Phase A with no
concurrency cap for DeepSeek (the pricing/off-peak dimensions still apply); add a real number once
production 429s establish one (§14). This does not weaken the free-tier guarantee anywhere — DeepSeek
is never free, so it never bypasses `allow_paid`.

## §5. Selection policy

`select_route` (§6) builds the candidate set from `ROUTES` and narrows it through these gates, applied
in order. Each gate either drops a route or defers it; the function always returns *why* every
non-winning route was excluded (§11.5).

0. **Transport gate.** Keep only routes whose `transport` matches this backend instance's configured
   `mode` (§2) — `direct` routes for a `mode="direct"` instance, `mistral-dispatch` for `mode
   ="dispatch"`.
1. **Allowlist gate.** If `policy.allowed_models is not None`, keep only routes whose `model` is in it.
2. **Free-model-protection gate.** If `not policy.allow_paid`, drop routes where `free is False`.
3. **Quota/budget gate.** Drop routes where `LLMBudget.available(...)` (§10) is `False` for the
   estimated request (RPM/RPD/TPM/concurrency/$ cap, whichever dimensions the route declares).
4. **Deadline gate.** If `policy.deadline_at` is set, drop routes whose predicted completion is after
   it. Predicted completion is `now` if gate 3 passed, or the route's next quota reset (from its
   ledger window keys, §10) if it didn't — never a fixed "assume it's always available eventually."
5. **Off-peak-preference gate.** For a route with an inactive discount window (`multiplier < 1`,
   §8) whose next active window would still finish before `deadline_at` (or `deadline_at` is unset),
   drop the route **for this call only** — it is not rejected forever, just not selected right now
   (§6). This is the only gate that can remove an otherwise-eligible route purely to save money, and
   it never fires once waiting would miss the deadline.
6. **Ranking.** Sort what's left by `(not free, current_effective_cost, predicted_completion,
   configured priority)` ascending and take the first. `current_effective_cost` applies any *active*
   peak-window multiplier for that route right now.

If nothing survives gate 0–5, the function returns no route and the caller (§6) treats this exactly
like quota exhaustion: not eligible this call, retried on the Stage's own next scheduled run.

The initial city-onboarding consumer submits:

```python
LLMRequestPolicy(
    allowed_models=None,
    allow_paid=True,
    deadline_at=now + timedelta(hours=24),
    purpose="city-onboarding",
)
```

## §6. A selection function, not a scheduler service

R13 is one pure function plus the CAS ledger it reads (§10). It has no independent process, no
"control-plane tick," and no persisted job queue of its own.

```python
# citypods/compute/llm_scheduler.py

@dataclass(frozen=True)
class SelectionResult:
    model: str | None
    route: LLMRoute | None
    reason: str                                   # always populated, human-readable
    rejected: tuple[tuple[str, str], ...] = ()     # (model, reason) for every non-winner

def select_route(
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute],
    ledger: LLMBudget,
    backend_mode: Literal["direct", "dispatch"],
    estimated_tokens: int,
    now: datetime,
) -> SelectionResult:
    """Pure — no I/O. Applies §5's gates 0–6 against an already-loaded, read-only `ledger`
    snapshot. Safe and fast to unit-test without any storage double."""
```

A calling Stage never calls `select_route` directly — it calls `LiteLLMBackend.run_inference` with
`job.inputs["llm_policy"]` set, and the backend calls `select_and_reserve` (§10, which loads the
freshest ledger via CAS, calls `select_route` against it, and reserves the winner) internally.

When selection returns no route, `run_inference` raises `LLMNotEligibleError` (§10.3) — a distinct,
safely-retryable exception, exactly like the existing `LLMStructuredOutputError`. The Stage catches it
and does what every other piece of deferred work in this codebase already does: it doesn't dispatch
this run, and the item stays pending in the Stage's own durable state (manifest/catalog) for the next
scheduled run to retry. **R13 does not remember that a job was deferred or when to wake it up** — the
calling workflow's existing cron cadence is the wake-up mechanism, and every gate in §5 is
re-evaluated fresh on every call. This is a deliberate correction from the initial draft's central
durable scheduler queue with a 7-state job lifecycle (`ready → delayed → reserved → batched/submitted
→ running → completed`, plus retry/failure branches) — that machinery duplicated state every calling
Stage already owns. The only state R13 itself persists is the quota/cost ledger (§10), which is
provider/model-scoped, not per-job.

The Mistral Worker's own internal pacing (R10) is unaffected — it still only claims work already
selected and handed to it by the existing `mode="dispatch"` path; it does not gain cross-provider
policy, and R13's scheduler never selects a `mistral-dispatch` route for a `mode="direct"` instance
(§2 gate 0).

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
exhausted, `LLMNotEligibleError` is raised and the request is retried on the Stage's next scheduled
run — the same mechanism every other deferred item in this codebase already uses.

**Only build a dedicated Gemini Worker later, and only if real usage shows the calling workflows'
cron cadence is too coarse relative to Gemini's RPD reset window** — that's an empirical question to
answer with ledger telemetry (§11.5), not a day-one design commitment.

## §8. DeepSeek off-peak pricing

**In scope for v1** — DeepSeek V4 pricing is expected to leave preview and confirm its off-peak window
within the next 1–2 weeks, and the mechanism should already be operating (on the best currently-known
figures) when that happens, not built reactively afterward.

**Correction from the initial draft:** the initial draft described DeepSeek "peak surcharge windows" of
09:00–12:00 and 14:00–18:00 `Asia/Shanghai`. That figure doesn't match review/27 §5.3, which is the
more carefully sourced fact already in this doc set: DeepSeek has historically discounted 50–75% during
**16:30–00:30 UTC** (V3/R1 figures, confirmed as of 2026-07-14; **V4's off-peak window was not yet
officially confirmed** at that time). §4.1's route table adopts review/27's figure — a single
`PeakWindow(tz="UTC", start=time(16,30), end=time(0,30), multiplier=0.5)` per DeepSeek model — as the
working default, discarding the initial draft's unsourced one. Re-verify against DeepSeek's live
pricing docs once V4 exits preview and update `ROUTES` — this is versioned configuration, not code, so
the update is a data change in `llm_policy.py`, not new logic.

Mechanism: entirely §5 gate 5. There's no separate `eligible_at` field or persisted defer record: the
next scheduled run of the calling workflow re-evaluates fresh, and because DeepSeek overflow/tournament
dispatch already tolerates "picked up next run" (review/27 §5.3), this needs no new infrastructure — R13's
selection function is simply the shared place that logic now lives, matching the conclusion review/27's
own dispatch-coordinator design already reached for the same problem.

A job near its deadline overrides the preference and dispatches at full price; that override is
implicit in gate 5 (it only defers when deferring still meets the deadline), not a separate flag.

**Treat pricing as advisory until verified.** If a route's price/window data is stale or absent, gate 5
is skipped for that route (no deliberate delay), quota/deadline gates still apply, and a warning is
logged. A missing or wrong price fact must never cause a missed deadline or a stuck job.

## §9. Batch capability — deferred

No route in `SUPPORTED_MODELS` has a confirmed server-side batch API today. Two candidates exist in the
surrounding docs, at different maturity: DeepSeek's batch + off-peak stacking (review/27 §5.3, "up to
~75% combined," but the exact endpoint shape — a real batch-submit/poll API vs. "async already gets the
off-peak rate" — was not conclusively researched) and Anthropic (not currently a configured provider at
all). Building a generic `BatchTransport` protocol for a capability nothing uses yet, for providers
whose actual API shape isn't confirmed, is the same premature-abstraction risk review/27 §8.2 already
argues against for cost estimation — let the real shape resolve first.

When DeepSeek's batch endpoint shape is confirmed (review/27 §5.3's own open item), add batch
capability sized to that specific API rather than a speculative generic one — a natural extension of
the same route/ledger model in §4/§10, but a follow-up review, not part of R13 v1.

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

@dataclass
class RouteLedger:
    cost_used: float = 0.0
    cost_cycle_key: str = ""
    requests_minute: int = 0
    requests_minute_key: str = ""      # minute_key(now), UTC "YYYY-MM-DDTHH:MM"
    requests_day: int = 0
    requests_day_key: str = ""         # daily_reset_key(now, route.quota.reset_timezone)
    tokens_minute: int = 0
    inflight: dict[str, LLMReservation] = field(default_factory=dict)

    @property
    def inflight_count(self) -> int:
        return len(self.inflight)

@dataclass
class LLMBudget:
    routes: dict[str, RouteLedger] = field(default_factory=dict)
```

`minute_key(now: datetime) -> str` truncates UTC time to the minute — a **fixed-minute bucket**, not a
sliding window. This is a deliberate simplification: a burst straddling a minute boundary can admit
slightly more than the declared RPM in a short window, but the provider's own 429 is the real backstop
(§8's "advisory" principle applies here too), and a sliding window is meaningfully more code to get
right for a benefit this system doesn't need. `daily_reset_key(now, tz_name)` converts `now` into the
named IANA zone (`zoneinfo.ZoneInfo`) and returns that zone's local date as `"YYYY-MM-DD"` — this is
what makes Gemini's bucket roll over at Pacific midnight, not UTC midnight, including across DST.
`cost_cycle_key` reuses `citypods.compute.budget.cycle_key(now)` directly — **do not reimplement it.**

`LLMBudget._ledger(model, now, *, route)` rolls each window independently against its own key,
mirroring `Budget._ledger`'s rollover-on-access idiom — rolling the minute window must never also
zero `cost_used` or vice versa:

```python
def _ledger(self, model: str, now: datetime, *, route: LLMRoute) -> RouteLedger:
    led = self.routes.setdefault(model, RouteLedger())
    mk = minute_key(now)
    if led.requests_minute_key != mk:
        led.requests_minute = 0
        led.tokens_minute = 0
        led.requests_minute_key = mk
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

`available` / `reserve` / `settle` / `release` — same signatures and semantics as `Budget`'s, extended
to the extra dimensions:

```python
def available(self, model: str, *, route: LLMRoute, requests: int, tokens: int, cost: float,
              now: datetime) -> bool: ...
def reserve(self, owner: str, model: str, *, route: LLMRoute, requests: int, tokens: int,
            cost: float, now: datetime) -> None: ...
def settle(self, owner: str, model: str, *, actual_tokens: int | None = None,
           actual_cost: float | None = None) -> None: ...
def release(self, owner: str, model: str) -> None: ...
```

`available` checks only the dimensions the route actually declares (`rpm`/`rpd`/`tpm`/`concurrency`/
`cost_cap` may each be `None`, meaning untracked for that route — DeepSeek's `concurrency=None`, §4.1,
skips that check entirely rather than treating `None` as zero).

**How `tokens` and `cost` are computed before every `available`/`reserve` call** (both `select_route`'s
gate 3 and `select_and_reserve`'s reservation step use the same formula, so they can never disagree):
`tokens = estimate_tokens(job.inputs["messages"]) + DEFAULT_OUTPUT_TOKEN_MARGIN` (LLM-SCHED-1, §12).
`cost = tokens * (route.pricing.input_per_token + route.pricing.output_per_token) *
active_multiplier`, where `active_multiplier` is the `multiplier` of whichever `PeakWindow` in
`route.pricing.windows` currently contains `now` (in that window's own `tz`), or `1.0` if none does.
This treats the whole token estimate as if split evenly across input/output pricing — a deliberately
crude approximation that only feeds a soft $ cap and the ranking tie-break (§5 gate 6), not a billing
record; `settle`'s `actual_cost` (§10.2) is what any real reporting should read.

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

### §10.3 CAS functions and exceptions

```python
LLM_BUDGET_STATE_KEY = "state/llm_budget.json"

def load_llm_budget_cas(storage) -> tuple[LLMBudget, str | None]: ...
def mutate_llm_budget(storage, mutate, *, now=None, max_attempts=8, base_sleep=0.05,
                       max_sleep=1.0, sleep=time.sleep, rng=None) -> LLMBudget: ...

def settle_route_reservation(storage, owner: str, model: str, *, actual_tokens=None,
                              actual_cost=None, **retry) -> LLMBudget: ...
def release_route_reservation(storage, owner: str, model: str, **retry) -> LLMBudget: ...
```

These three mirror `load_budget_cas`/`mutate_budget`/`settle_reservation`/`release_reservation`
exactly (same CAS-conflict retry loop, same `If-Match`/`If-None-Match` semantics). Unlike
`compute/budget.py`, there is no bare `reserve_route_if_available(storage, owner, model, ...)` for a
single pre-chosen route — R13 must choose *among* routes, so selection and reservation happen together
in one CAS loop, in `citypods/compute/llm_scheduler.py` (not `llm_budget.py`, to keep the dependency
direction one-way: `llm_scheduler.py` imports `llm_budget.py`, never the reverse):

```python
# citypods/compute/llm_scheduler.py

def select_and_reserve(
    storage,
    owner: str,
    policy: LLMRequestPolicy,
    *,
    routes: Mapping[str, LLMRoute] = ROUTES,
    backend_mode: Literal["direct", "dispatch"],
    estimated_tokens: int,
    now: datetime | None = None,
    max_attempts: int = 8,
    base_sleep: float = 0.05,
    max_sleep: float = 1.0,
    sleep=time.sleep,
    rng: random.Random | None = None,
) -> SelectionResult:
    """On each attempt: load the freshest ledger via CAS, run `select_route` against it, and if it
    picks a route, reserve that route with a conditional write. A CASConflict (a sibling shard won)
    re-reads and re-selects from scratch — the winner can legitimately differ between attempts if the
    losing route's quota changed. Returns a `SelectionResult` with `model=None` either because no
    route was ever eligible or because every attempt lost the CAS race `max_attempts` times."""
```

`LiteLLMBackend.run_inference` (§12, LLM-SCHED-4) calls this, and on `result.model is None` raises:

```python
class LLMNotEligibleError(LLMBackendError):
    """No configured route is eligible for this request right now (quota, deadline, or off-peak
    preference). Safe for the caller to defer and retry on its next scheduled run — same contract
    as LLMStructuredOutputError."""
```

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

### §10.5 What is deliberately *not* persisted here

No job records, no lifecycle states, no batch IDs, no defer reasons kept beyond a normal log line. A
job's existence, its deadline, and its retry ownership belong to the calling Stage's own durable state
(its manifest/catalog), exactly as they do today for every other kind of deferred work. The ledger only
ever answers "how much of this route's quota/budget is available right now, and when does more become
available" — nothing about *which* jobs are waiting on it.

### §10.6 Migration / backfill

`state/llm_budget.json` is new state with no prior schema — there is nothing to migrate and nothing to
backfill. `load_llm_budget_cas` returns an empty `LLMBudget()` when the object doesn't exist yet (same
`got is None` branch `load_budget_cas` already uses), and the first successful reservation creates it
via `if_none_match="*"`. No existing state file changes shape; `compute_budget.json` is untouched.

## §11. Additional recommendations

1. **Versioned quota/pricing policy.** Store the policy version used for every decision; provider
   limits and prices change independently of code releases (this matters concretely for §8: DeepSeek's
   V4 window is expected to change within weeks).
2. **Cache by request fingerprint.** Identical prompt/context/schema/model requests should reuse a
   completed result before consuming quota. `llm.py`'s dispatch mode already keys on an
   idempotency-key derived from `recipe_hash` for its Worker path — extend that same fingerprint, don't
   invent a second one.
3. **Separate evaluation from production, structurally, not by a flag.** A scorer request already gets
   this for free from §3: pass an explicit `allowed_models` singleton (or small list) and
   `allow_paid=True`; it can never silently consume the production free-route budget because the
   allowlist excludes it from ever being a candidate there.
4. **Starvation/aging is not R13's job.** Because R13 persists no job queue (§6), there's no scheduler-
   level starvation to prevent — ordering among a Stage's own pending items is that Stage's existing
   backlog policy (H5 windowed-recency, `ops/workqueue.py`; review/27's provider-priority ordering).
   R13 only ever answers "is a route eligible right now," not "whose turn is it."
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
     ▼
llm_budget.py  (deps: llm_policy)
     │
     ▼
llm_scheduler.py  (deps: llm_policy, llm_budget)
     │
     ▼
llm.py  (deps: llm_scheduler, llm_budget, llm_policy)   ← existing file, edited
```

Plus one independent, order-sensitive edit: `storage/routing.py` (LLM-SCHED-5), which must land no
later than LLM-SCHED-2 (its `assert_coordination_prefixes_ephemeral()` guard runs at import time).

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
DeepSeek off-peak deferral case against a synthetic `now` outside the discount window with a distant
deadline — assert `SelectionResult.model is None` and the rejection reason names the off-peak gate;
(e) the same case with `now` inside the discount window — assert it's selected; (f) the same off-peak
case with `deadline_at` inside the current (full-price) window — assert the deadline override fires and
DeepSeek is selected anyway, at full price.

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
settled reservation; a policy-present + no-eligible-route case asserting `LLMNotEligibleError` and *no*
ledger mutation survives (release, not a stuck reservation); a simulated failure *after* the mocked
`completion`/`post` call raises, asserting `settle_route_reservation` was called, not `release`.

**LLM-SCHED-5 — Storage routing registration.** Edits `citypods/storage/routing.py` (§10.4): add
`"state/llm_budget.json"` to both `COORDINATION_PREFIXES` and `_EPHEMERAL_R2_PREFIXES` in the same
commit. Must land no later than LLM-SCHED-2 lands in `main` (the import-time assertion in `routing.py`
will fail any test run otherwise); in practice, land it in the same PR as LLM-SCHED-2. **Test**: one
explicit assertion that both entries exist (a regression guard beyond the automatic import-time check).

**LLM-SCHED-6 — Live provider wiring + acceptance (Phase B).** No new files. Confirms `ROUTES`'
Gemini/DeepSeek figures against current provider docs immediately before merge (prices and windows
drift, §11.1); wires the city-onboarding Stage (or whichever Stage is first, per §5's example) to pass
`job.inputs["llm_policy"]`; end-to-end test that a flexible DeepSeek job deferred outside the discount
window dispatches once `now` is re-run inside it; confirms the Mistral Worker's own test suite
(`workers/llm-dispatch-proxy`) is untouched — no new test needed there, an unmodified passing suite is
the proof.

### §12.3 Acceptance criteria

- A free-eligible job never selects a paid model when a free route can meet its deadline.
- An evaluation caller can force a specific model — free or paid — via an explicit allowlist,
  independent of production's free-route protection.
- Gemini quota reservations roll over at Pacific midnight (including across DST) and never overspend
  across concurrent shards — proven by the CAS ledger's conflict-retry test, not by luck.
- A reservation that is settled is never also released, and vice versa (§10.2) — proven by a test that
  simulates a post-dispatch failure and asserts the reservation stays counted.
- Flexible DeepSeek work prefers the configured off-peak window without ever missing a caller's
  deadline.
- A stale or missing price/quota policy disables deliberate delay and still lets the job dispatch — it
  never deadlocks work.
- A job with no `llm_policy` behaves identically to `LiteLLMBackend` before this item existed — proven
  by a byte-for-byte payload comparison, not an informal read-through.
- Mistral Worker pacing remains unchanged.
- Every rejection is explainable from `SelectionResult.rejected`, with no separate persisted job-state
  table to keep in sync.

## §13. What changed from the initial draft, and why

| Cut or changed | Reason |
|---|---|
| Central durable scheduler queue + 7-state job lifecycle | Duplicated durable state every calling Stage already owns; replaced by a stateless selection function re-evaluated each scheduled run (§6). |
| Second "Gemini quota Worker" (own R2 queue, async enqueue/poll) | Solves a problem (idle-runner pacing) Gemini's generous free tier doesn't have; a CAS ledger check from inside the calling run is sufficient and matches the H14d/H17 precedent already in this codebase (§7). |
| `LLMRequestPolicy`'s `selection`/`cost_policy` enums, `max_wait_seconds`, `priority` | All derivable from `allowed_models` cardinality, `allow_paid`, and `deadline_at` — see the mapping table in §3. |
| `BatchTransport` protocol / generic batch capability | No confirmed batch-capable provider exists yet in `SUPPORTED_MODELS`; premature per review/27 §8.2's own stated philosophy (§9). |
| DeepSeek peak-surcharge window figures (09:00–12:00/14:00–18:00 `Asia/Shanghai`) | Unsourced in the initial draft and inconsistent with review/27 §5.3's independently researched, dated figure (16:30–00:30 UTC off-peak discount). Adopted review/27's figure as authoritative (§8). |
| review/27 §8's $ budget ledger treated as a separate, later concern | Per maintainer decision, ships as the same CAS object as R13's quota ledger, built once (§10). |
| Model selection implicitly assumed to be able to cross transports | Added an explicit gate-0 transport constraint (§2, §5): the scheduler picks a model, never a transport; a `direct`-mode backend never becomes a `dispatch`-mode call mid-request. |
| Reservation release semantics on any failure | Added the explicit settle-vs-release distinction (§10.2): only release when the network call was never attempted; settle (keep quota charged) for every outcome after that, since the provider's own counter already moved. Not present at all in the initial draft, and easy to get backwards by copying `compute/budget.py`'s GPU-dispatch pattern verbatim. |
| **Kept unchanged** | Allowlist + free-model-protection semantics; RPM/RPD/TPM as independent per-route dimensions; recipe-hash/artifact-identity independence from scheduling policy (§3); "no provider data in Stages" (§11.6); DeepSeek off-peak *preference*, not a hard constraint (§8, itself carried over from review/27 §5.3). |

## §14. Open decisions before implementation

- DeepSeek's actual concurrency ceiling — unmeasured; ships as `None` (untracked) in Phase A/B, added
  once real 429 telemetry establishes a number (§4.1).
- DeepSeek V4's actual off-peak window and multiplier, pending its exit from preview — the mechanism in
  §8 is built now; the specific numbers are a config update once confirmed.
- Whether DeepSeek's "batch" is a real submit/poll API or just off-peak-rate-via-async (review/27
  §5.3's own open item) — resolves independently of R13, and only matters once §9 is revisited.
- Whether a single `LiteLLMBackend` instance should ever transparently choose between `direct` and
  `dispatch` transport for the same request — explicitly out of scope for v1 (§2); revisit only if a
  real caller needs one request to be able to land on either Gemini/DeepSeek *or* Mistral.
- Whether city onboarding should prefer the newest eligible free job or oldest-deadline-first once
  concurrent onboarding volume makes that distinction matter (not yet, at current volume).
