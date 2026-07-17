from datetime import UTC, datetime, timedelta

from citypods.compute.llm_budget import LLMBudget, load_llm_budget_cas
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


def _gemini_exhausted() -> LLMBudget:
    budget = LLMBudget()
    route = ROUTES["gemini/gemini-3-flash-preview"]
    ledger = budget._ledger(route.model, NOW, route=route)
    ledger.requests_day = route.quota.rpd
    return budget


def test_paid_route_wins_when_free_quota_cannot_reset_before_deadline():
    result = select_route(
        LLMRequestPolicy(allow_paid=True, deadline_at=NOW + timedelta(hours=1)),
        routes=ROUTES,
        ledger=_gemini_exhausted(),
        backend_mode="direct",
        estimated_tokens=1024,
        now=NOW,
    )
    assert result.model in {
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    }
    assert any(model == "gemini/gemini-3-flash-preview" for model, _ in result.rejected)


def test_free_route_is_not_replaced_by_paid_route_when_off_peak_wait_is_safe():
    result = select_route(
        LLMRequestPolicy(allow_paid=True, deadline_at=NOW + timedelta(hours=24)),
        routes=ROUTES,
        ledger=_gemini_exhausted(),
        backend_mode="direct",
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
        backend_mode="direct",
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
        backend_mode="direct",
        estimated_tokens=1024,
        now=NOW,
    )
    assert outside.model is None
    assert "off-peak" in outside.rejected[0][1]

    inside = select_route(
        LLMRequestPolicy(allowed_models=(model,), allow_paid=True),
        routes=ROUTES,
        ledger=LLMBudget(),
        backend_mode="direct",
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
        backend_mode="direct",
        estimated_tokens=1024,
        now=NOW,
    )
    assert urgent.model == model


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
        "owner",
        LLMRequestPolicy(),
        routes={route.model: route},
        backend_mode="direct",
        estimated_tokens=10,
        now=NOW,
        sleep=lambda _: None,
    )

    assert result.model == route.model
    budget, _ = load_llm_budget_cas(storage)
    assert budget.routes[route.model].inflight_count == 1
