from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
from citypods.storage import CASConflict
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
    budget.settle("owner-1", ROUTE.model, route=ROUTE, now=NOW, actual_tokens=10, actual_cost=0.1)
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
    budget.release("owner-2", ROUTE.model, route=ROUTE, now=NOW)
    assert ledger.requests_minute == 1
    assert ledger.tokens_minute == 10
    assert ledger.cost_used == pytest.approx(0.1)


def test_daily_cost_cap_rolls_over_and_never_exceeds_its_limit():
    route = LLMRoute(
        model="paid/daily-capped",
        transport="direct",
        free=False,
        quota=QuotaPolicy(rpd=20),
        pricing=PricingPolicy(daily_cost_cap=0.10),
    )
    budget = LLMBudget()
    assert budget.available(route.model, route=route, requests=1, tokens=1, cost=0.10, now=NOW)
    budget.reserve("owner", route.model, route=route, requests=1, tokens=1, cost=0.10, now=NOW)
    assert not budget.available(route.model, route=route, requests=1, tokens=1, cost=0.001, now=NOW)
    tomorrow = NOW + timedelta(days=1)
    assert budget.available(route.model, route=route, requests=1, tokens=1, cost=0.10, now=tomorrow)


def test_reserve_is_idempotent_for_a_still_inflight_owner():
    """A caller retrying the same logical dispatch under the same owner (R13's deterministic
    ``recipe_hash``-derived owner) before the first reservation settles must not be double-counted
    -- the provider-facing request was not actually sent twice."""
    budget = LLMBudget()
    budget.reserve("owner-1", ROUTE.model, route=ROUTE, requests=1, tokens=20, cost=0.2, now=NOW)
    budget.reserve("owner-1", ROUTE.model, route=ROUTE, requests=1, tokens=20, cost=0.2, now=NOW)
    ledger = budget.routes[ROUTE.model]
    assert ledger.requests_minute == 1
    assert ledger.tokens_minute == 20
    assert ledger.cost_used == pytest.approx(0.2)
    assert ledger.inflight_count == 1


def test_settle_across_a_minute_boundary_does_not_corrupt_the_new_bucket():
    """Regression test: settling a reservation after its per-minute window has already rolled
    over must not apply a stale delta to the *new* bucket (previously this could drive
    ``tokens_minute`` negative and silently loosen the TPM cap for the rest of that minute)."""
    budget = LLMBudget()
    reserved_at = datetime(2026, 7, 16, 12, 0, 30, tzinfo=UTC)
    settled_at = datetime(2026, 7, 16, 12, 1, 5, tzinfo=UTC)  # crossed into the next minute

    budget.reserve(
        "owner-1", ROUTE.model, route=ROUTE, requests=1, tokens=90, cost=0.5, now=reserved_at
    )
    # A concurrent request lands in the *new* minute before the first one settles.
    budget.reserve(
        "owner-2", ROUTE.model, route=ROUTE, requests=1, tokens=5, cost=0.1, now=settled_at
    )
    ledger = budget.routes[ROUTE.model]
    assert ledger.tokens_minute == 5  # only owner-2's reservation is in the current window

    budget.settle(
        "owner-1", ROUTE.model, route=ROUTE, now=settled_at, actual_tokens=10, actual_cost=0.05
    )
    # owner-1's token usage belongs to the *old* (already-rolled) per-minute bucket and must not
    # touch the new one at all -- neither corrupting it toward negative nor double-counting.
    assert ledger.tokens_minute == 5
    # cost_used is scoped to the monthly cycle key, not the per-minute one, so a correction that
    # only crosses a minute boundary (not a cycle boundary) correctly still applies: owner-2's 0.1
    # reservation plus owner-1's corrected actual (0.5 reserved -> 0.05 actual).
    assert ledger.cost_used == pytest.approx(0.1 + 0.05)
    assert "owner-1" not in ledger.inflight


def test_release_across_a_minute_boundary_does_not_touch_the_new_bucket():
    budget = LLMBudget()
    reserved_at = datetime(2026, 7, 16, 12, 0, 30, tzinfo=UTC)
    released_at = datetime(2026, 7, 16, 12, 1, 5, tzinfo=UTC)

    budget.reserve(
        "owner-1", ROUTE.model, route=ROUTE, requests=1, tokens=90, cost=0.5, now=reserved_at
    )
    budget.reserve(
        "owner-2", ROUTE.model, route=ROUTE, requests=1, tokens=5, cost=0.1, now=released_at
    )
    ledger = budget.routes[ROUTE.model]

    budget.release("owner-1", ROUTE.model, route=ROUTE, now=released_at)
    assert ledger.requests_minute == 1  # only owner-2's request remains in the current window
    assert ledger.tokens_minute == 5
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


def test_requests_day_is_never_incremented_for_a_route_without_rpd():
    """A route that doesn't declare ``rpd`` must not grow an unbounded, never-reset counter."""
    route = LLMRoute(
        model="no-rpd/model",
        transport="direct",
        free=True,
        quota=QuotaPolicy(),
        pricing=PricingPolicy(),
    )
    budget = LLMBudget()
    for i in range(5):
        budget.reserve(
            f"owner-{i}", route.model, route=route, requests=1, tokens=1, cost=0, now=NOW
        )
    assert budget.routes[route.model].requests_day == 0


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
        lambda budget, _now: budget.reserve(
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
        storage, "owner", ROUTE.model, route=ROUTE, actual_tokens=10, actual_cost=0.1, now=NOW
    )
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[ROUTE.model].inflight == {}
    settle_route_reservation(
        storage, "owner", ROUTE.model, route=ROUTE, actual_tokens=10, actual_cost=0.1, now=NOW
    )
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[ROUTE.model].cost_used == pytest.approx(0.1)

    mutate_llm_budget(
        storage,
        lambda budget, _now: budget.reserve(
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
    release_route_reservation(storage, "owner-2", ROUTE.model, route=ROUTE, now=NOW)
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[ROUTE.model].cost_used == pytest.approx(0.1)


def test_mutate_llm_budget_retries_after_a_cas_conflict():
    """Proves two concurrent shards serialize through CAS rather than one silently clobbering the
    other's reservation -- the same guarantee `compute/budget.py`'s `mutate_budget` gives, applied
    to the LLM ledger's own reserve path."""

    class ConflictOnce(MemCAS):
        def __init__(self) -> None:
            super().__init__()
            self.conflicted = False

        def put_cas(self, *args, **kwargs):
            if not self.conflicted:
                self.conflicted = True
                raise CASConflict("injected")
            return super().put_cas(*args, **kwargs)

    storage = ConflictOnce()
    mutate_llm_budget(
        storage,
        lambda budget, _now: budget.reserve(
            "owner", ROUTE.model, route=ROUTE, requests=1, tokens=20, cost=0.2, now=NOW
        ),
        now=NOW,
        sleep=lambda _: None,
    )
    assert storage.conflicted is True
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[ROUTE.model].requests_minute == 1
    assert budget.routes[ROUTE.model].cost_used == pytest.approx(0.2)


def test_mutate_llm_budget_refreshes_now_per_attempt_when_not_pinned(monkeypatch):
    """Regression test for the stale-timestamp bug: `now` used to be resolved once before the
    retry loop and reused on every attempt, even though real wall-clock time (and potentially a
    sibling writer's ledger window) had moved on by the time a conflict forced a retry. With
    `now=None` (the caller not pinning a fixed time), each attempt must ask for the current time
    again -- proven here by a fake clock returning a different value on each of two calls."""
    clock_values = iter([NOW, NOW + timedelta(minutes=1, seconds=5)])
    monkeypatch.setattr(
        "citypods.compute.llm_budget.datetime",
        SimpleNamespace(now=lambda tz=None: next(clock_values)),
    )

    class ConflictOnce(MemCAS):
        def __init__(self) -> None:
            super().__init__()
            self.conflicted = False

        def put_cas(self, *args, **kwargs):
            if not self.conflicted:
                self.conflicted = True
                raise CASConflict("injected")
            return super().put_cas(*args, **kwargs)

    storage = ConflictOnce()
    observed: list[datetime] = []
    mutate_llm_budget(
        storage,
        lambda budget, attempt_now: observed.append(attempt_now),
        now=None,
        sleep=lambda _: None,
    )
    assert observed == [NOW, NOW + timedelta(minutes=1, seconds=5)]


def test_mutate_llm_budget_pinned_now_does_not_refresh_across_retries():
    """The opposite of the previous test: an explicitly supplied `now` (as every deterministic
    test in this file, and every caller in llm.py, already does) must NOT be refreshed -- it's
    honored unchanged on every attempt."""

    class ConflictOnce(MemCAS):
        def __init__(self) -> None:
            super().__init__()
            self.conflicted = False

        def put_cas(self, *args, **kwargs):
            if not self.conflicted:
                self.conflicted = True
                raise CASConflict("injected")
            return super().put_cas(*args, **kwargs)

    storage = ConflictOnce()
    observed: list[datetime] = []
    mutate_llm_budget(
        storage,
        lambda budget, attempt_now: observed.append(attempt_now),
        now=NOW,
        sleep=lambda _: None,
    )
    assert observed == [NOW, NOW]


def test_find_inflight_owner():
    budget = LLMBudget()
    assert budget.find_inflight_owner("owner-1") is None
    budget.reserve("owner-1", ROUTE.model, route=ROUTE, requests=1, tokens=1, cost=0.0, now=NOW)
    assert budget.find_inflight_owner("owner-1") == ROUTE.model
    assert budget.find_inflight_owner("owner-2") is None
    budget.settle("owner-1", ROUTE.model, route=ROUTE, now=NOW)
    assert budget.find_inflight_owner("owner-1") is None


def test_block_overrides_proactive_availability_until_the_given_time():
    """A real 429 from the provider overrides our own RPM/RPD/TPM estimate, which might be wrong
    (shared quota, an unmodeled monthly pool) -- available() must respect it regardless of what
    the counters say."""
    budget = LLMBudget()
    assert budget.available(ROUTE.model, route=ROUTE, requests=1, tokens=1, cost=0.0, now=NOW)
    until = NOW + timedelta(minutes=5)
    budget.block(ROUTE.model, until, route=ROUTE, now=NOW)
    assert not budget.available(
        ROUTE.model, route=ROUTE, requests=1, tokens=1, cost=0.0, now=NOW + timedelta(minutes=4)
    )
    assert budget.available(
        ROUTE.model, route=ROUTE, requests=1, tokens=1, cost=0.0, now=NOW + timedelta(minutes=5)
    )


def test_block_never_moves_an_existing_block_earlier():
    budget = LLMBudget()
    far = NOW + timedelta(hours=1)
    near = NOW + timedelta(minutes=1)
    budget.block(ROUTE.model, far, route=ROUTE, now=NOW)
    budget.block(ROUTE.model, near, route=ROUTE, now=NOW)
    assert not budget.available(
        ROUTE.model, route=ROUTE, requests=1, tokens=1, cost=0.0, now=NOW + timedelta(minutes=30)
    )
