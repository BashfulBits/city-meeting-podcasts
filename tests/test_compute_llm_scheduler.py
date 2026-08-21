from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from citypods.compute import llm_scheduler
from citypods.compute.llm_budget import (
    LLMBudget,
    LLMReservation,
    ProviderLedger,
    RouteLedger,
    load_llm_budget_cas,
    mutate_llm_budget,
    serialize_llm_budget,
)
from citypods.compute.llm_policy import (
    ROUTE_CANDIDATES,
    ROUTE_REGISTRY,
    ROUTES,
    LLMRequestPolicy,
    LLMRoute,
    PeakWindow,
    PricingPeriod,
    PricingPolicy,
    QuotaPolicy,
)
from citypods.compute.llm_scheduler import select_and_reserve, select_route
from citypods.storage import CASConflict
from tests._cas_fake import MemCAS

NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)
DIRECT = frozenset({"direct"})
BOTH_TRANSPORTS = frozenset({"direct", "mistral-dispatch", "llm-dispatch"})


def _all_free_direct_routes_exhausted() -> LLMBudget:
    budget = LLMBudget()

    for route_id, route in ROUTE_REGISTRY.items():
        if not route.free or "direct" not in route.transports:
            continue
        ledger = budget._ledger(route_id, NOW, route=route)
        if route.quota.rpm is not None:
            ledger.requests_minute = route.quota.rpm
        if route.quota.rpd is not None:
            ledger.requests_day = route.quota.rpd
        if route.quota.tpm is not None:
            ledger.tokens_minute = route.quota.tpm
        if route.quota.concurrency is not None:
            ledger.inflight = {
                f"owner-{n}": LLMReservation(cost=0, requests=1, tokens=1)
                for n in range(route.quota.concurrency)
            }
    return budget


def _deepseek_direct_route(model: str) -> LLMRoute:
    return next(route for route in ROUTE_CANDIDATES[model] if route.provider == "deepseek")


def test_direct_selection_skips_physical_routes_with_insufficient_context():
    small = LLMRoute(
        model="test/model",
        transport="direct",
        free=True,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(),
        input_context_limit=10_000,
        output_context_limit=1_024,
    )
    large = LLMRoute(
        model="test/model",
        transport="direct",
        free=True,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(),
        input_context_limit=100_000,
        output_context_limit=8_192,
    )
    result = select_route(
        LLMRequestPolicy(allowed_models=("test/model",)),
        routes={"small": small, "large": large},
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=22_048,
        input_tokens=20_000,
        output_tokens=2_048,
        now=NOW,
    )
    assert result.route is large
    assert ("test/model", "input context limit") in result.rejected

    output_only = select_route(
        LLMRequestPolicy(allowed_models=("test/model",)),
        routes={"small": small, "large": large},
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=3_000,
        input_tokens=2_000,
        output_tokens=2_048,
        now=NOW,
    )
    assert output_only.route is large
    assert ("test/model", "output context limit") in output_only.rejected


def test_paid_route_wins_when_free_quota_cannot_reset_before_deadline():
    result = select_route(
        LLMRequestPolicy(allow_paid=True, deadline_at=NOW + timedelta(hours=1)),
        routes=ROUTES,
        ledger=_all_free_direct_routes_exhausted(),
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
        LLMRequestPolicy(
            allow_paid=True,
            allowed_models=(
                "gemini/gemini-3-flash-preview",
                "deepseek/deepseek-v4-flash",
                "deepseek/deepseek-v4-pro",
            ),
        ),
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


def test_no_route_is_selected_when_free_routes_are_exhausted_and_paid_is_disallowed():
    result = select_route(
        LLMRequestPolicy(allow_paid=False, deadline_at=NOW + timedelta(hours=24)),
        routes=ROUTES,
        ledger=_all_free_direct_routes_exhausted(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model is None
    assert any("paid model disallowed" in reason for _, reason in result.rejected)


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
    route = _deepseek_direct_route(model)
    routes = {route.route_id or route.model: route}
    outside = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_paid=True),
        routes=routes,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert outside.model is None
    assert "price-window" in outside.rejected[0][1]

    inside = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_paid=True),
        routes=routes,
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
        routes=routes,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert urgent.model == model


def test_deepseek_peak_waits_for_the_next_cheapest_window():
    model = "deepseek/deepseek-v4-flash"
    route = _deepseek_direct_route(model)
    routes = {route.route_id or route.model: route}
    peak = datetime(2026, 8, 17, 2, tzinfo=UTC)

    deferred = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_paid=True),
        routes=routes,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=peak,
    )
    assert deferred.model is None
    assert deferred.retry_at == datetime(2026, 8, 17, 4, tzinfo=UTC)

    urgent = select_route(
        LLMRequestPolicy(
            allowed_models=(model,), allow_paid=True, deadline_at=peak + timedelta(minutes=30)
        ),
        routes=routes,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=peak,
    )
    assert urgent.model == model


def test_price_gate_rechecks_when_a_new_rate_card_starts_before_the_next_window():
    route = LLMRoute(
        model="test/scheduled",
        transport="direct",
        free=False,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(
            periods=(
                PricingPeriod(
                    effective_at=datetime(1970, 1, 1, tzinfo=UTC),
                    input_per_token=1e-3,
                    windows=(PeakWindow("UTC", time(1), time(4), 2),),
                ),
                PricingPeriod(
                    effective_at=datetime(2026, 8, 17, 3, tzinfo=UTC),
                    input_per_token=5e-4,
                ),
            )
        ),
    )
    result = select_route(
        LLMRequestPolicy(allowed_models=(route.model,), allow_paid=True),
        routes={route.model: route},
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=100,
        now=datetime(2026, 8, 17, 2, tzinfo=UTC),
    )
    assert result.model is None
    assert result.retry_at == datetime(2026, 8, 17, 3, tzinfo=UTC)


def test_price_gate_precedes_peak_rate_daily_cost_admission():
    route = LLMRoute(
        model="test/capped-scheduled",
        transport="direct",
        free=False,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(
            input_per_token=1e-3,
            daily_cost_cap=0.15,
            windows=(PeakWindow("UTC", time(1), time(4), 2),),
        ),
    )
    budget = LLMBudget()
    budget._ledger(route.model, NOW, route=route)
    result = select_route(
        LLMRequestPolicy(allowed_models=(route.model,), allow_paid=True),
        routes={route.model: route},
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=100,
        now=datetime(2026, 8, 17, 2, tzinfo=UTC),
    )
    assert result.model is None
    assert result.retry_at == datetime(2026, 8, 17, 4, tzinfo=UTC)
    assert "price-window" in result.rejected[0][1]


def test_direct_transport_selects_a_direct_capable_route():
    result = select_route(
        LLMRequestPolicy(allowed_models=("mistral/mistral-large-2512",), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model == "mistral/mistral-large-2512"
    assert result.transport == "direct"


def test_transport_gate_rejects_a_dispatch_only_route_from_a_direct_caller():
    route = LLMRoute(
        model="example/dispatch-only",
        transport="llm-dispatch",
        transports=("llm-dispatch",),
        free=True,
        quota=QuotaPolicy(rpm=1),
        pricing=PricingPolicy(),
    )
    result = select_route(
        LLMRequestPolicy(allowed_models=(route.model,)),
        routes={route.model: route},
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model is None
    assert (route.model, "transport gate") in result.rejected


def test_mistral_large_policy_matches_the_deployed_dispatch_worker_ceiling():
    """Mistral Large route matches the upstream 0.07-RPS (4 RPM) ceiling."""
    route = ROUTES["mistral/mistral-large-2512"]
    assert route.transport == "direct"
    assert set(route.transports) == {"direct", "llm-dispatch"}
    assert route.quota.rpm == 4


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
    assert _selected_transport(mistral_route, dispatch_only, allow_dispatch_overflow=False) == (
        "llm-dispatch"
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


def test_production_mistral_medium_is_available_directly_after_catalog_expansion():
    model = "mistral/mistral-medium-2508"
    direct = select_route(
        LLMRequestPolicy(allowed_models=(model,)),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert direct.model == model
    assert direct.transport == "direct"

    included = select_route(
        LLMRequestPolicy(allowed_models=(model,)),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=BOTH_TRANSPORTS,
        estimated_tokens=1024,
        now=NOW,
    )
    assert included.model == model
    assert included.transport == "direct"


def _model_routing_fixture_routes() -> dict[str, LLMRoute]:
    requested = LLMRoute(
        model="requested/model",
        transport="direct",
        transports=("direct",),
        free=True,
        quota=QuotaPolicy(rpm=10),
        pricing=PricingPolicy(),
    )
    overflow = LLMRoute(
        model="overflow/model",
        transport="direct",
        transports=("direct",),
        free=True,
        quota=QuotaPolicy(rpm=10),
        pricing=PricingPolicy(),
    )
    return {requested.model: requested, overflow.model: overflow}


def test_requested_model_wins_over_a_busier_but_still_eligible_overflow_target(monkeypatch):
    """CodeRabbit, PR #1268: utilization/cost must not let a merely-busier requested model lose to
    an idle overflow model -- overflow should only win once the requested model has no capacity
    left at all, matching the Worker's `selectRoute`, which fully exhausts one model's own routes
    before ever trying the next model in `allowed_models`."""
    routes = _model_routing_fixture_routes()
    monkeypatch.setattr(llm_scheduler, "MODEL_ROUTING", {"requested/model": ("overflow/model",)})

    budget = LLMBudget()
    ledger = budget._ledger("requested/model", NOW, route=routes["requested/model"])
    ledger.requests_minute = 8  # 80% utilized, but still under the rpm=10 cap

    selection = select_route(
        LLMRequestPolicy(allowed_models=("requested/model",)),
        routes=routes,
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=64,
        now=NOW,
    )
    assert selection.model == "requested/model"


def test_overflow_target_is_used_once_the_requested_model_has_no_capacity(monkeypatch):
    routes = _model_routing_fixture_routes()
    monkeypatch.setattr(llm_scheduler, "MODEL_ROUTING", {"requested/model": ("overflow/model",)})

    budget = LLMBudget()
    ledger = budget._ledger("requested/model", NOW, route=routes["requested/model"])
    ledger.requests_minute = 10  # fully exhausted (rpm=10)

    selection = select_route(
        LLMRequestPolicy(allowed_models=("requested/model",)),
        routes=routes,
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=64,
        now=NOW,
    )
    assert selection.model == "overflow/model"


def test_a_model_with_no_configured_overflow_is_unaffected_by_model_routing(monkeypatch):
    routes = _model_routing_fixture_routes()
    monkeypatch.setattr(llm_scheduler, "MODEL_ROUTING", {"requested/model": ("overflow/model",)})

    selection = select_route(
        LLMRequestPolicy(allowed_models=("overflow/model",)),
        routes=routes,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=64,
        now=NOW,
    )
    assert selection.model == "overflow/model"
    # `model_routing` is directional: routing "requested/model" -> "overflow/model" does not
    # implicitly make "overflow/model" callers eligible for "requested/model" too.
    assert ("requested/model", "allowlist gate") in selection.rejected


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


def test_tpm_retry_at_is_token_schedule_not_next_minute():
    route = LLMRoute(
        model="burst/model",
        transport="direct",
        free=True,
        quota=QuotaPolicy(tpm=100),
        pricing=PricingPolicy(),
    )
    budget = LLMBudget()
    budget.reserve("burst", route.model, route=route, requests=1, tokens=950, cost=0, now=NOW)

    result = select_route(
        LLMRequestPolicy(allowed_models=(route.model,)),
        routes={route.model: route},
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1,
        now=NOW,
    )

    assert result.model is None
    assert result.retry_at == NOW + timedelta(minutes=9, seconds=30)


def test_retry_at_waits_for_all_blocking_quota_axes():
    route = LLMRoute(
        model="provider/daily-limit",
        transport="direct",
        free=True,
        provider="provider",
        provider_rpm=60,
        quota=QuotaPolicy(rpd=3),
        pricing=PricingPolicy(),
    )
    budget = LLMBudget(
        providers={
            "provider": ProviderLedger(
                requests_available_at=(NOW + timedelta(seconds=5)).isoformat()
            )
        }
    )
    led = budget._ledger(route.model, NOW, route=route)
    led.requests_day = route.quota.rpd

    result = select_route(
        LLMRequestPolicy(allowed_models=(route.model,)),
        routes={route.model: route},
        ledger=budget,
        available_transports=DIRECT,
        estimated_tokens=1,
        now=NOW,
    )

    assert result.model is None
    zone = ZoneInfo(route.quota.reset_timezone)
    tomorrow = NOW.astimezone(zone).date() + timedelta(days=1)
    expected = datetime.combine(tomorrow, datetime.min.time(), tzinfo=zone).astimezone(UTC)
    assert result.retry_at == expected


def test_no_selection_persists_window_rollover():
    route = LLMRoute(
        model="rollover/model",
        transport="direct",
        free=True,
        quota=QuotaPolicy(rpm=1, rpd=3, tpm=100),
        pricing=PricingPolicy(),
    )
    budget = LLMBudget(
        routes={
            route.model: RouteLedger(
                requests_minute=1,
                requests_minute_key="2026-07-15T11:59",
                requests_day=3,
                requests_day_key="2026-07-15",
                blocked_until=(NOW + timedelta(hours=1)).isoformat(),
            )
        }
    )
    storage = MemCAS()
    storage.seed("state/llm_budget.json", serialize_llm_budget(budget))

    result = select_and_reserve(
        storage,
        "rollover-recipe",
        LLMRequestPolicy(allowed_models=(route.model,)),
        routes={route.model: route},
        available_transports=DIRECT,
        estimated_tokens=1,
        now=NOW,
    )

    assert result.model is None
    rolled, _ = load_llm_budget_cas(storage)
    ledger = rolled.routes[route.model]
    assert ledger.requests_minute == 0
    assert ledger.requests_minute_key == "2026-07-16T12:00"
    assert ledger.requests_day == 0
    assert ledger.requests_day_key == "2026-07-16"


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
    it to the reset candidates unconditionally, allowing a stale timestamp to be selected instead
    of a correctly-computed future "tomorrow" RPD reset -- reproducing the exact same
    busy-retry-forever failure mode the "next minute" fix above addressed, just via a different
    unconditional axis."""
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
    be included among the required reset times -- this fix must not stop honoring genuine, active
    blocks."""
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
        now=NOW + timedelta(seconds=12),
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
    routes = {
        gemini.route_id or gemini.model: gemini,
        mistral.route_id or mistral.model: mistral,
    }
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
    assert budget.routes[mistral.route_id or mistral.model].inflight_count == 1
    assert (gemini.route_id or gemini.model) not in budget.routes or budget.routes[
        gemini.route_id or gemini.model
    ].inflight_count == 0
