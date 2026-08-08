from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from citypods.compute.llm_budget import LLMBudget, load_llm_budget_cas, mutate_llm_budget
from citypods.compute.llm_policy import (
    ROUTES,
    LLMRequestPolicy,
    LLMRoute,
    PricingPolicy,
    QuotaPolicy,
)
from citypods.compute.llm_scheduler import select_and_reserve, select_route
from citypods.storage import CASConflict
from tests._cas_fake import MemCAS

NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)
DIRECT = frozenset({"direct"})
BOTH_TRANSPORTS = frozenset({"direct", "mistral-dispatch", "llm-dispatch"})


def _gemini_exhausted() -> LLMBudget:
    budget = LLMBudget()
    for model, route in ROUTES.items():
        if not route.free or route.transport != "direct" or route.quota.rpd is None:
            continue
        ledger = budget._ledger(model, NOW, route=route)
        ledger.requests_day = route.quota.rpd
    return budget


def test_paid_route_wins_when_free_quota_cannot_reset_before_deadline():
    result = select_route(
        LLMRequestPolicy(allow_paid=True, deadline_at=NOW + timedelta(hours=1)),
        routes=ROUTES,
        ledger=_gemini_exhausted(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model in {
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    }
    assert any(model == "gemini/gemini-3-flash-preview" for model, _ in result.rejected)


def test_ranking_prefers_free_route_over_a_simultaneously_eligible_paid_route():
    """Distinct from the exhausted-Gemini scenarios above: here Gemini has full quota *and*
    DeepSeek is inside its own off-peak window (so it's cheap and immediately eligible too) --
    ranking (§5 gate 6) must still pick the free route over an equally-eligible paid one."""
    inside_deepseek_window = datetime(2026, 7, 16, 18, tzinfo=UTC)
    result = select_route(
        LLMRequestPolicy(allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=inside_deepseek_window,
    )
    assert result.model == "gemini/gemini-3-flash-preview"
    assert any(
        model in {"deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro"}
        and reason == "lower-ranked eligible route"
        for model, reason in result.rejected
    )


def test_free_route_is_not_replaced_by_paid_route_when_off_peak_wait_is_safe():
    result = select_route(
        LLMRequestPolicy(allow_paid=True, deadline_at=NOW + timedelta(hours=24)),
        routes=ROUTES,
        ledger=_gemini_exhausted(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model is None
    assert any("off-peak" in reason for _, reason in result.rejected)


def test_allowlist_can_force_one_paid_evaluation_model():
    model = "deepseek/deepseek-v4-pro"
    result = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=datetime(2026, 7, 16, 18, tzinfo=UTC),
    )
    assert result.model == model


def test_deepseek_off_peak_preference_and_deadline_override():
    model = "deepseek/deepseek-v4-flash"
    outside = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert outside.model is None
    assert "off-peak" in outside.rejected[0][1]

    inside = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=datetime(2026, 7, 16, 18, tzinfo=UTC),
    )
    assert inside.model == model

    urgent = select_route(
        LLMRequestPolicy(
            allowed_models=(model,), allow_paid=True, deadline_at=NOW + timedelta(hours=1)
        ),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert urgent.model == model


def test_transport_gate_hides_dispatch_routes_from_a_direct_only_caller():
    result = select_route(
        LLMRequestPolicy(allowed_models=("mistral/mistral-large-2512",), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model is None
    assert ("mistral/mistral-large-2512", "transport gate") in result.rejected


def test_mistral_large_policy_matches_the_deployed_dispatch_worker_ceiling():
    """The Worker claims one Large request per Cron minute, below the upstream 0.07-RPS cap."""
    route = ROUTES["mistral/mistral-large-2512"]
    assert route.transport == "llm-dispatch"
    assert route.transports == ("llm-dispatch",)
    assert route.quota.rpm == 1


def test_owner_for_keys_off_the_selected_transport_not_route_capability():
    """The owner must key off the *actually selected* transport for this call, not merely whether
    the route is capable of dispatch -- a Gemini route can dispatch over `llm-dispatch`, but a
    *direct* call to that same route (the default, `allow_dispatch_overflow=False`) has no
    server-side dedup and must reserve its own unique slot. Keying off route capability alone was
    a real bug (CodeRabbit, review/41): it deduped two genuinely concurrent direct calls sharing a
    `recipe_hash` into one reservation, undercounting real API calls -- the mirror image of the
    original double-reservation bug this whole mechanism exists to prevent."""
    from citypods.compute.llm_scheduler import _owner_for

    assert _owner_for("abc123", "llm-dispatch") == "abc123"
    assert _owner_for("abc123", "mistral-dispatch") == "abc123"
    owner = _owner_for("abc123", "direct")
    assert owner != "abc123"
    assert owner.startswith("abc123:")
    # No selection (nothing reachable) must never accidentally collide with a real dispatch owner.
    assert _owner_for("abc123", None).startswith("abc123:")


def test_selected_transport_prefers_direct_unless_overflow_is_explicit():
    from citypods.compute.llm_scheduler import _selected_transport

    gemini_route = ROUTES["gemini/gemini-3-flash-preview"]
    both = frozenset({"direct", "llm-dispatch"})
    assert _selected_transport(gemini_route, both, allow_dispatch_overflow=False) == "direct"
    assert _selected_transport(gemini_route, both, allow_dispatch_overflow=True) == "llm-dispatch"
    # Overflow requested but the Worker isn't actually reachable -- direct is all there is.
    assert _selected_transport(gemini_route, DIRECT, allow_dispatch_overflow=True) == "direct"

    mistral_route = ROUTES["mistral/mistral-large-2512"]
    dispatch_only = frozenset({"llm-dispatch"})
    # Mistral has no direct alternative -- always dispatches regardless of the overflow flag.
    assert (
        _selected_transport(mistral_route, dispatch_only, allow_dispatch_overflow=False)
        == "llm-dispatch"
    )

    direct_only_route = ROUTES["deepseek/deepseek-v4-flash"]
    assert _selected_transport(direct_only_route, DIRECT, allow_dispatch_overflow=True) == "direct"

    # Nothing reachable at all.
    assert _selected_transport(gemini_route, frozenset(), allow_dispatch_overflow=True) is None


def test_select_and_reserve_dual_transport_direct_vs_overflow_owner():
    """End-to-end through `select_and_reserve`, both Gemini paths: a plain direct selection
    reserves under a unique owner (no policy opt-in), while `allow_dispatch_overflow=True`
    reserves under `recipe_hash` -- matching the Worker's own idempotency-key dedup."""
    storage = MemCAS()
    both_transports = frozenset({"direct", "llm-dispatch"})

    direct_selection = select_and_reserve(
        storage,
        "recipe-direct",
        LLMRequestPolicy(allowed_models=("gemini/gemini-3-flash-preview",)),
        available_transports=both_transports,
        estimated_tokens=1024,
        now=NOW,
    )
    assert direct_selection.transport == "direct"
    assert direct_selection.owner != "recipe-direct"
    assert direct_selection.owner.startswith("recipe-direct:")

    overflow_selection = select_and_reserve(
        storage,
        "recipe-overflow",
        LLMRequestPolicy(
            allowed_models=("gemini/gemini-3-flash-preview",),
            allow_dispatch_overflow=True,
        ),
        available_transports=both_transports,
        estimated_tokens=1024,
        now=NOW,
    )
    assert overflow_selection.transport == "llm-dispatch"
    assert overflow_selection.owner == "recipe-overflow"


def test_research_only_mistral_medium_is_not_an_implicit_pipeline_fallback():
    model = "mistral/mistral-medium-2508"
    excluded = select_route(
        LLMRequestPolicy(allowed_models=(model,)),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert excluded.model is None
    # Medium is now the pinned production agenda route, but it is dispatch-only. A direct-only
    # caller must still be rejected rather than silently bypassing the shared Worker pacing.
    assert (model, "transport gate") in excluded.rejected

    included = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_experimental=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=BOTH_TRANSPORTS,
        estimated_tokens=1024,
        now=NOW,
    )
    assert included.model == model
    assert included.transport == "llm-dispatch"


def test_retry_at_is_next_minute_when_only_the_per_minute_window_is_full():
    """Pacing signal: with the per-minute window full but the daily quota still open, `retry_at`
    is the next minute boundary -- the paced caller waits that out and keeps dispatching, draining
    the daily quota across successive minutes instead of stopping at the first RPM burst."""
    budget = LLMBudget()
    model = "gemini/gemini-3.1-flash-lite"
    route = ROUTES[model]
    led = budget._ledger(model, NOW, route=route)
    led.requests_minute = route.quota.rpm  # this minute is spent; the day still has room

    result = select_route(
        LLMRequestPolicy(allowed_models=(model,)),
        routes=ROUTES,
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model is None
    assert result.retry_at == NOW.replace(second=0, microsecond=0) + timedelta(minutes=1)


def test_retry_at_is_tomorrows_reset_when_only_the_daily_quota_is_exhausted():
    """The inverse of the per-minute case above: with RPM/TPM completely fresh this minute and
    only RPD exhausted, `retry_at` must be tomorrow's reset, not a bogus "next minute" guess.
    Before this fix, `_next_quota_reset` offered next-minute unconditionally whenever the
    ledger's minute window had merely been touched (true on nearly every check, since checking
    availability itself stamps that key) -- so a route genuinely exhausted for the whole day
    would mispredict retry_at as seconds away, and `_run_policy_job_paced` (which never gives up
    on a non-None retry_at) would busy-retry every few seconds for the rest of its deadline
    instead of correctly recognizing the day is spent."""
    budget = LLMBudget()
    model = "gemini/gemini-3.1-flash-lite"
    route = ROUTES[model]
    led = budget._ledger(model, NOW, route=route)
    led.requests_day = route.quota.rpd  # today's quota is fully spent; this minute is untouched

    result = select_route(
        LLMRequestPolicy(allowed_models=(model,)),
        routes=ROUTES,
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )

    assert result.model is None
    zone = ZoneInfo(route.quota.reset_timezone)
    tomorrow = NOW.astimezone(zone).date() + timedelta(days=1)
    expected = datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).astimezone(UTC)
    assert result.retry_at == expected


def test_retry_at_ignores_a_stale_blocked_until_and_falls_back_to_the_daily_reset():
    """`blocked_until` only ever moves forward (`LLMBudget.block()` extends it, never clears it),
    so a route that was 429-blocked earlier today still carries that (now past) timestamp in its
    ledger entry long after the block itself expired. Before this fix, `_next_quota_reset` added
    it to the reset candidates unconditionally, and a past timestamp always wins `min()` over a
    correctly-computed future "tomorrow" RPD reset -- reproducing the exact same busy-retry-forever
    failure mode the "next minute" fix above addressed, just via a different unconditional axis."""
    budget = LLMBudget()
    model = "gemini/gemini-3.1-flash-lite"
    route = ROUTES[model]
    led = budget._ledger(model, NOW, route=route)
    led.requests_day = route.quota.rpd  # today's quota is fully spent
    led.blocked_until = (NOW - timedelta(hours=2)).isoformat()  # stale: expired 2h ago

    result = select_route(
        LLMRequestPolicy(allowed_models=(model,)),
        routes=ROUTES,
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )

    assert result.model is None
    zone = ZoneInfo(route.quota.reset_timezone)
    tomorrow = NOW.astimezone(zone).date() + timedelta(days=1)
    expected = datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).astimezone(UTC)
    assert result.retry_at == expected


def test_retry_at_honors_a_still_active_blocked_until():
    """The inverse: a `blocked_until` still in the future *is* a real constraint and must still
    win when it's the soonest reset -- this fix must not stop honoring genuine, active blocks."""
    budget = LLMBudget()
    model = "gemini/gemini-3.1-flash-lite"
    route = ROUTES[model]
    led = budget._ledger(model, NOW, route=route)
    led.blocked_until = (NOW + timedelta(minutes=5)).isoformat()

    result = select_route(
        LLMRequestPolicy(allowed_models=(model,)),
        routes=ROUTES,
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )

    assert result.model is None
    assert result.retry_at == NOW + timedelta(minutes=5)


def test_retry_at_is_tomorrows_reset_when_only_the_daily_cost_cap_is_exhausted():
    """`daily_cost_cap` resets on the same daily boundary as RPD (`daily_reset_key` in
    `llm_budget.py`'s `_ledger()`) -- `_next_quota_reset` must predict tomorrow's reset for it
    too, not fall into the imprecise one-minute fallback reserved for axes it can't model
    (concurrency, the currently-unused monthly `cost_cap`)."""
    budget = LLMBudget()
    model = "test/daily-cost-cap"
    route = LLMRoute(
        model=model,
        transport="direct",
        free=False,
        quota=QuotaPolicy(reset_timezone="UTC"),
        pricing=PricingPolicy(input_per_token=1e-3, daily_cost_cap=0.25),
    )
    led = budget._ledger(model, NOW, route=route)
    led.cost_day_used = route.pricing.daily_cost_cap  # today's $ cap is fully spent

    result = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_paid=True),
        routes={model: route},
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )

    assert result.model is None
    zone = ZoneInfo(route.quota.reset_timezone)
    tomorrow = NOW.astimezone(zone).date() + timedelta(days=1)
    expected = datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).astimezone(UTC)
    assert result.retry_at == expected


def test_retry_at_is_none_when_a_route_is_available_now():
    result = select_route(
        LLMRequestPolicy(allowed_models=("gemini/gemini-3.1-flash-lite",)),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model == "gemini/gemini-3.1-flash-lite"
    assert result.retry_at is None


def test_retry_at_spans_to_the_second_route_when_the_first_is_daily_exhausted():
    """With both flash-lite routes allowed, a run that has spent 3.1's whole day but not 3.5's
    still gets an immediate selection (3.5), so throughput spills across the two independent pools
    rather than deferring."""
    budget = LLMBudget()
    spent = "gemini/gemini-3.1-flash-lite"
    led = budget._ledger(spent, NOW, route=ROUTES[spent])
    led.requests_day = ROUTES[spent].quota.rpd  # 3.1 fully spent for the day

    result = select_route(
        LLMRequestPolicy(
            allowed_models=("gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite")
        ),
        routes=ROUTES,
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model == "gemini/gemini-3.5-flash-lite"
    assert result.retry_at is None


def test_ranking_prefers_the_less_utilized_of_two_free_equal_cost_routes():
    """Two free, equally-costed, simultaneously-eligible routes (the two flash-lite pools) must
    not always resolve to whichever sorts first alphabetically -- 3.1 winning every tie regardless
    of how close it is to its own RPM/RPD ceiling is exactly why 3.5's independent quota pool sat
    almost entirely unused in practice. Once 3.1 has *some* usage and 3.5 has none, 3.5 should win
    the tie so load actually spreads across both pools instead of serializing through one."""
    budget = LLMBudget()
    busier = "gemini/gemini-3.1-flash-lite"
    led = budget._ledger(busier, NOW, route=ROUTES[busier])
    led.requests_minute = 5  # 5 of 15 rpm used -- well short of exhausted, just busier

    result = select_route(
        LLMRequestPolicy(
            allowed_models=("gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite")
        ),
        routes=ROUTES,
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model == "gemini/gemini-3.5-flash-lite"


def test_ranking_falls_back_to_model_name_when_utilization_ties():
    """With both flash-lite pools equally (un)used, selection is still deterministic -- the
    model-name comparison remains the final tie-break once cost and utilization agree."""
    result = select_route(
        LLMRequestPolicy(
            allowed_models=("gemini/gemini-3.1-flash-lite", "gemini/gemini-3.5-flash-lite")
        ),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model == "gemini/gemini-3.1-flash-lite"


def test_a_caller_reaching_both_transports_can_select_either():
    """The sweep (and any caller configured with a dispatch Worker) can reach both -- this is
    what lets a single backend instance service pending records regardless of which provider
    originally claimed them."""
    result = select_route(
        LLMRequestPolicy(allowed_models=("mistral/mistral-large-2512",), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=BOTH_TRANSPORTS,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model == "mistral/mistral-large-2512"


def test_select_and_reserve_retries_after_one_cas_conflict():
    class ConflictOnce(MemCAS):
        def __init__(self):
            super().__init__()
            self.conflicted = False

        def put_cas(self, *args, **kwargs):
            if not self.conflicted:
                self.conflicted = True
                raise CASConflict("injected")
            return super().put_cas(*args, **kwargs)

    route = LLMRoute(
        model="test/model",
        transport="direct",
        free=True,
        quota=QuotaPolicy(rpm=1),
        pricing=PricingPolicy(),
    )
    storage = ConflictOnce()
    result = select_and_reserve(
        storage,
        "recipe-1",
        LLMRequestPolicy(),
        routes={route.model: route},
        available_transports=DIRECT,
        estimated_tokens=10,
        now=NOW,
        sleep=lambda _: None,
    )

    assert result.model == route.model
    assert result.owner is not None and result.owner.startswith("recipe-1:")
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[route.model].inflight_count == 1


def test_select_and_reserve_honors_a_requests_worst_case():
    route = LLMRoute(
        model="test/model",
        transport="direct",
        free=True,
        quota=QuotaPolicy(rpm=2),
        pricing=PricingPolicy(),
    )
    storage = MemCAS()
    select_and_reserve(
        storage,
        "recipe-1",
        LLMRequestPolicy(),
        routes={route.model: route},
        available_transports=DIRECT,
        estimated_tokens=10,
        requests=2,
        now=NOW,
    )
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[route.model].requests_minute == 2


def test_select_and_reserve_owner_is_unique_per_direct_attempt():
    """Direct transport has no server-side dedup -- two calls sharing a recipe_hash must reserve
    independently, not collapse into one owner."""
    route = LLMRoute(
        model="test/model",
        transport="direct",
        free=True,
        quota=QuotaPolicy(rpm=5),
        pricing=PricingPolicy(),
    )
    storage = MemCAS()
    first = select_and_reserve(
        storage,
        "recipe-1",
        LLMRequestPolicy(),
        routes={route.model: route},
        available_transports=DIRECT,
        estimated_tokens=10,
        now=NOW,
    )
    second = select_and_reserve(
        storage,
        "recipe-1",
        LLMRequestPolicy(),
        routes={route.model: route},
        available_transports=DIRECT,
        estimated_tokens=10,
        now=NOW,
    )
    assert first.owner != second.owner
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[route.model].requests_minute == 2


def test_select_and_reserve_reuses_route_for_an_already_inflight_dispatch_owner():
    """A dispatch-mode retry -- reserved deterministically under `recipe_hash` itself -- must
    resolve to the model it originally reserved, even if a fresh selection pass, run against
    updated ledger state, would now pick differently (e.g. a previously-exhausted free route
    recovering)."""
    gemini = ROUTES["gemini/gemini-3-flash-preview"]
    mistral = ROUTES["mistral/mistral-large-2512"]
    routes = {gemini.model: gemini, mistral.model: mistral}
    storage = MemCAS()
    # Seed an existing in-flight dispatch reservation for "recipe-1" (the deterministic dispatch
    # owner), simulating a prior attempt that got a 202 and hasn't settled yet.
    mutate_llm_budget(
        storage,
        lambda budget, attempt_now: budget.reserve(
            "recipe-1",
            mistral.model,
            route=mistral,
            requests=1,
            tokens=10,
            cost=0.0,
            now=attempt_now,
        ),
        now=NOW,
    )
    # A fresh selection pass would now pick Gemini (empty ledger, free, no allowlist
    # restriction) -- but the retry must still resolve to Mistral, the route "recipe-1" is
    # already reserved under.
    result = select_and_reserve(
        storage,
        "recipe-1",
        LLMRequestPolicy(allow_paid=True),
        routes=routes,
        available_transports=BOTH_TRANSPORTS,
        estimated_tokens=10,
        now=NOW,
    )
    assert result.model == mistral.model
    assert result.owner == "recipe-1"
    budget, _ = load_llm_budget_cas(storage)
    # No new reservation was created -- still exactly the one seeded above.
    assert budget.routes[mistral.model].inflight_count == 1
    assert gemini.model not in budget.routes or budget.routes[gemini.model].inflight_count == 0
