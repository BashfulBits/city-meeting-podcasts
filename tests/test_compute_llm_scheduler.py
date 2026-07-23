from datetime import UTC, datetime, timedelta

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
BOTH_TRANSPORTS = frozenset({"direct", "mistral-dispatch"})


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
        LLMRequestPolicy(allowed_models=("mistral/mistral-large-latest",), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=DIRECT,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model is None
    assert ("mistral/mistral-large-latest", "transport gate") in result.rejected


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


def test_a_caller_reaching_both_transports_can_select_either():
    """The sweep (and any caller configured with a dispatch Worker) can reach both -- this is
    what lets a single backend instance service pending records regardless of which provider
    originally claimed them."""
    result = select_route(
        LLMRequestPolicy(allowed_models=("mistral/mistral-large-latest",), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        available_transports=BOTH_TRANSPORTS,
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model == "mistral/mistral-large-latest"


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
    mistral = ROUTES["mistral/mistral-large-latest"]
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
