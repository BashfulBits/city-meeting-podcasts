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


@pytest.mark.parametrize(
    "raw",
    [
        {"mode": "publish"},
        {"providers": 4},
        {"max_duration_seconds": 0},
        {"max_episodes_per_run": 0},
    ],
)
def test_policy_rejects_invalid_controls(raw):
    with pytest.raises(ValueError):
        ChapterRolloutPolicy.from_mapping(raw)
