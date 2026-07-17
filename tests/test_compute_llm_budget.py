from datetime import UTC, datetime

import pytest

from citypods.compute.llm_budget import (
    LLMBudget,
    RouteLedger,
    daily_reset_key,
    load_llm_budget_cas,
    mutate_llm_budget,
    release_route_reservation,
    settle_route_reservation,
)
from citypods.compute.llm_policy import LLMRoute, PricingPolicy, QuotaPolicy
from tests._cas_fake import MemCAS

NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)
ROUTE = LLMRoute(
    model="test/model",
    transport="direct",
    free=True,
    quota=QuotaPolicy(rpm=2, rpd=3, tpm=100),
    pricing=PricingPolicy(cost_cap=1.0),
)


def test_reserve_settle_and_release_round_trip():
    budget = LLMBudget()
    assert budget.available(ROUTE.model, route=ROUTE, requests=1, tokens=20, cost=0.2, now=NOW)
    budget.reserve(
        "owner-1",
        ROUTE.model,
        route=ROUTE,
        requests=1,
        tokens=20,
        cost=0.2,
        now=NOW,
    )
    budget.settle("owner-1", ROUTE.model, actual_tokens=10, actual_cost=0.1)
    ledger = budget.routes[ROUTE.model]
    assert ledger.inflight == {}
    assert ledger.requests_minute == 1
    assert ledger.tokens_minute == 10
    assert ledger.cost_used == pytest.approx(0.1)

    budget.reserve(
        "owner-2",
        ROUTE.model,
        route=ROUTE,
        requests=1,
        tokens=20,
        cost=0.2,
        now=NOW,
    )
    budget.release("owner-2", ROUTE.model)
    assert ledger.requests_minute == 1
    assert ledger.tokens_minute == 10
    assert ledger.cost_used == pytest.approx(0.1)


def test_none_quota_dimensions_are_untracked():
    route = LLMRoute(
        model="unlimited/model",
        transport="direct",
        free=False,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(),
    )
    budget = LLMBudget()
    budget.routes[route.model] = RouteLedger(requests_minute=999, tokens_minute=999)
    assert budget.available(route.model, route=route, requests=1, tokens=1_000_000, cost=0, now=NOW)


def test_gemini_daily_key_uses_pacific_midnight_across_dst():
    spring_before = datetime(2026, 3, 8, 7, 59, tzinfo=UTC)
    spring_after = datetime(2026, 3, 8, 8, 1, tzinfo=UTC)
    fall_before = datetime(2026, 11, 1, 6, 59, tzinfo=UTC)
    fall_after = datetime(2026, 11, 1, 7, 1, tzinfo=UTC)
    assert daily_reset_key(spring_before, "America/Los_Angeles") == "2026-03-07"
    assert daily_reset_key(spring_after, "America/Los_Angeles") == "2026-03-08"
    assert daily_reset_key(fall_before, "America/Los_Angeles") == "2026-10-31"
    assert daily_reset_key(fall_after, "America/Los_Angeles") == "2026-11-01"


def test_cas_settle_and_release_helpers_persist_one_terminal_transition():
    storage = MemCAS()
    mutate_llm_budget(
        storage,
        lambda budget: budget.reserve(
            "owner",
            ROUTE.model,
            route=ROUTE,
            requests=1,
            tokens=20,
            cost=0.2,
            now=NOW,
        ),
        now=NOW,
    )
    settle_route_reservation(
        storage, "owner", ROUTE.model, actual_tokens=10, actual_cost=0.1, now=NOW
    )
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[ROUTE.model].inflight == {}
    settle_route_reservation(
        storage, "owner", ROUTE.model, actual_tokens=10, actual_cost=0.1, now=NOW
    )
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[ROUTE.model].cost_used == pytest.approx(0.1)

    mutate_llm_budget(
        storage,
        lambda budget: budget.reserve(
            "owner-2",
            ROUTE.model,
            route=ROUTE,
            requests=1,
            tokens=20,
            cost=0.2,
            now=NOW,
        ),
        now=NOW,
    )
    release_route_reservation(storage, "owner-2", ROUTE.model, now=NOW)
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[ROUTE.model].cost_used == pytest.approx(0.1)
