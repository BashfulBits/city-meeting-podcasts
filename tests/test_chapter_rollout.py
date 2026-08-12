"""Tests for bounded, disabled-by-default chapter rollout controls."""

import pytest

from citypods.chapter_rollout import ChapterRolloutPolicy


def test_policy_defaults_to_disabled_and_never_allows_episode():
    policy = ChapterRolloutPolicy.from_mapping(None)
    assert policy.mode == "disabled"
    assert not policy.allows_episode(provider="swagit", body="Council", duration_seconds=60)
    assert policy.effective_mode(shadow_gate_status="pass", eligible=True) == "disabled"


def test_policy_bounds_provider_body_and_duration():
    policy = ChapterRolloutPolicy.from_mapping(
        {
            "mode": "overlay",
            "providers": ["Swagit"],
            "bodies": ["City Council"],
            "max_duration_seconds": 7200,
            "max_episodes_per_run": 8,
        }
    )
    assert policy.allows_episode(provider="swagit", body="city council", duration_seconds=7200)
    assert not policy.allows_episode(provider="granicus", body="city council", duration_seconds=60)
    assert not policy.allows_episode(provider="swagit", body="planning", duration_seconds=60)
    assert not policy.allows_episode(provider="swagit", body="city council", duration_seconds=7201)
    assert policy.effective_mode(shadow_gate_status="not_configured", eligible=True) == "shadow"
    assert policy.effective_mode(shadow_gate_status="pass", eligible=True) == "overlay"


def test_policy_enforces_max_episodes_per_run_via_selected_count():
    policy = ChapterRolloutPolicy.from_mapping(
        {
            "mode": "overlay",
            "providers": ["Swagit"],
            "max_episodes_per_run": 2,
        }
    )
    assert policy.allows_episode(provider="swagit", selected_count=0)
    assert policy.allows_episode(provider="swagit", selected_count=1)
    assert not policy.allows_episode(provider="swagit", selected_count=2)
    assert not policy.allows_episode(provider="swagit", selected_count=3)
    assert not policy.allows_episode(provider="swagit", selected_count=-1)
    assert not policy.allows_episode(provider="swagit", selected_count=True)  # type: ignore[arg-type]


def test_allows_episode_rejects_non_finite_and_invalid_durations():
    policy = ChapterRolloutPolicy.from_mapping(
        {
            "mode": "shadow",
            "max_duration_seconds": 3600,
        }
    )
    assert policy.allows_episode(provider="swagit", duration_seconds=1800)
    assert not policy.allows_episode(provider="swagit", duration_seconds=None)
    assert not policy.allows_episode(provider="swagit", duration_seconds=float("nan"))
    assert not policy.allows_episode(provider="swagit", duration_seconds=float("inf"))
    assert not policy.allows_episode(provider="swagit", duration_seconds=float("-inf"))
    assert not policy.allows_episode(provider="swagit", duration_seconds=True)  # type: ignore[arg-type]
    assert not policy.allows_episode(provider="swagit", duration_seconds=False)  # type: ignore[arg-type]
    assert not policy.allows_episode(provider="swagit", duration_seconds=-10)


@pytest.mark.parametrize(
    "raw",
    [
        {"mode": "publish"},
        {"providers": 4},
        {"providers": [123]},
        {"providers": [None]},
        {"providers": [True]},
        {"bodies": [456]},
        {"max_duration_seconds": 0},
        {"max_duration_seconds": -5},
        {"max_duration_seconds": float("nan")},
        {"max_duration_seconds": float("inf")},
        {"max_duration_seconds": float("-inf")},
        {"max_duration_seconds": True},
        {"max_duration_seconds": "not-a-number"},
        {"max_episodes_per_run": 0},
        {"max_episodes_per_run": -1},
        {"max_episodes_per_run": True},
        {"max_episodes_per_run": 1.5},
        {"max_episodes_per_run": "two"},
    ],
)
def test_policy_rejects_invalid_controls(raw):
    with pytest.raises(ValueError):
        ChapterRolloutPolicy.from_mapping(raw)
