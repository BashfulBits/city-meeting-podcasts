from __future__ import annotations

from citypods.compute.policy import backend_policy


def test_backend_policy_loads_legacy_flat_shape():
    policy = backend_policy(
        {
            "defaults": {
                "compute_backends": {
                    "modal": {
                        "monthly_gpu_seconds": 108000,
                        "max_inflight": 8,
                        "max_claims": 2,
                        "max_scan": 15,
                    }
                }
            }
        },
        "modal",
    )

    assert policy.budget.monthly_units == 108000
    assert policy.budget.spendable_units == 108000
    assert policy.dispatch.max_inflight == 8
    assert policy.dispatch.min_claims_per_run == 1
    assert policy.dispatch.max_claims_per_run == 2
    assert policy.dispatch.max_scan == 15
    assert policy.dispatch.preferred_days == "all"
    assert policy.task.prefer_min_duration_hours == 0


def test_backend_policy_prefers_nested_h14d_shape():
    policy = backend_policy(
        {
            "defaults": {
                "compute_backends": {
                    "beam": {
                        "monthly_gpu_seconds": 1,
                        "max_inflight": 1,
                        "budget": {
                            "monthly_units": 400,
                            "reserve_units": 25,
                            "unit_label": "credit-unit",
                        },
                        "dispatch": {
                            "max_inflight": 4,
                            "min_claims_per_run": 2,
                            "max_claims_per_run": 3,
                            "preferred_days": "odd",
                        },
                        "tasks": {
                            "transcript-asr": {
                                "prefer_min_duration_hours": 4,
                                "fresh_within_days": 3,
                                "budget_units_per_audio_second": 0.5,
                                "min_budget_units": 90,
                                "fixed_budget_units_per_run": 8,
                                "fixed_budget_units_per_claim": 12,
                            }
                        },
                    }
                }
            }
        },
        "beam",
    )

    assert policy.budget.monthly_units == 400
    assert policy.budget.reserve_units == 25
    assert policy.budget.spendable_units == 375
    assert policy.budget.unit_label == "credit-unit"
    assert policy.dispatch.max_inflight == 4
    assert policy.dispatch.min_claims_per_run == 2
    assert policy.dispatch.max_claims_per_run == 3
    assert policy.dispatch.preferred_days == "odd"
    assert policy.task.prefer_min_duration_hours == 4
    assert policy.task.fresh_within_days == 3
    assert policy.task.budget_units_per_audio_second == 0.5
    assert policy.task.min_budget_units == 90
    assert policy.task.fixed_budget_units_per_run == 8
    assert policy.task.fixed_budget_units_per_claim == 12
