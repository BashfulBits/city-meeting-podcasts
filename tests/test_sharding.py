from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from citypods.models import City, Episode
from citypods.records import episode_to_record, save_records, source_key
from citypods.sharding import (
    ShardPlan,
    create_shard_plan,
    load_shard_plan,
    save_shard_plan,
    sources_for_shard,
)


def _city(slug: str, feed_url: str) -> City:
    return City(
        slug=slug,
        provider="fake",
        source={"feed_url": feed_url},
        podcast_title=slug,
        podcast_author="City",
        podcast_email="",
        podcast_description="",
    )


def _hosted_episode(uid: str, duration: float) -> Episode:
    ep = Episode(
        guid=uid,
        uid=uid,
        title=uid,
        published=datetime(2026, 6, 1, tzinfo=UTC),
        video_url="https://example.test/video",
        duration=duration,
    )
    ep.audio_key = f"audio/{uid}.m4a"
    ep.audio_spec_hash = "spec"
    ep.hosted_audio_url = f"https://cdn.test/{uid}.m4a"
    ep.audio_duration_served = duration
    return ep


def test_transcribe_plan_is_serializable_and_uses_snapshot_weights(tmp_path):
    cities = [_city("a", "https://a"), _city("b", "https://b")]
    key_a, key_b = (source_key(city) for city in cities)
    save_records(
        tmp_path,
        key_a,
        {"a1": episode_to_record(_hosted_episode("a1", 3 * 3600))},
    )
    save_records(
        tmp_path,
        key_b,
        {"b1": episode_to_record(_hosted_episode("b1", 30 * 60))},
    )

    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="transcribe",
        num_shards=2,
        defaults={"asr_local_max_duration_hours": 4},
        asr_pipeline_version="3",
    )
    path = tmp_path / "plan.json"
    save_shard_plan(path, plan)

    loaded = load_shard_plan(path)
    assert loaded == plan
    assert loaded.weights[key_a] == 3 * 3600
    assert loaded.weights[key_b] == 30 * 60
    assert set(loaded.assignment) == {key_a, key_b}
    assert set(loaded.assignment.values()) == {0, 1}


def test_plan_validation_fails_closed_on_lane_shards_or_source_drift():
    plan = ShardPlan(
        lane="transcribe",
        num_shards=2,
        assignment={"a": 0, "b": 1},
        weights={"a": 1, "b": 1},
    )
    with pytest.raises(ValueError, match="lane"):
        sources_for_shard(
            plan,
            lane="align",
            shard_index=0,
            num_shards=2,
            expected_sources={"a", "b"},
        )
    with pytest.raises(ValueError, match="has 2 shards"):
        sources_for_shard(
            plan,
            lane="transcribe",
            shard_index=0,
            num_shards=4,
            expected_sources={"a", "b"},
        )
    with pytest.raises(ValueError, match="shard index 2 out of range"):
        sources_for_shard(
            plan,
            lane="transcribe",
            shard_index=2,
            num_shards=2,
            expected_sources={"a", "b"},
        )
    with pytest.raises(ValueError, match="source set"):
        sources_for_shard(
            plan,
            lane="transcribe",
            shard_index=0,
            num_shards=2,
            expected_sources={"a", "c"},
        )


def test_load_plan_rejects_mismatched_assignment_and_weights(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "lane": "transcribe",
                "num_shards": 2,
                "assignment": {"a": 0},
                "weights": {"b": 1},
            }
        )
    )
    with pytest.raises(ValueError, match="source sets differ"):
        load_shard_plan(path)
