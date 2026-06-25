from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from citypods.asr import asr_initial_prompt, asr_spec_hash
from citypods.models import City, Episode
from citypods.records import episode_to_record, save_records, source_key, transcript_media_hash
from citypods.sharding import (
    ShardPlan,
    create_shard_plan,
    episodes_for_shard,
    load_shard_plan,
    save_shard_plan,
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


def _entity_city(slug: str, feed_url: str) -> City:
    city = _city(slug, feed_url)
    city.city_entity = "shared-city"
    return city


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


def _asr_recipe(city: City, ep: Episode, version: str = "3") -> str:
    return asr_spec_hash(
        transcript_media_hash(ep),
        city.asr_model,
        None,
        version,
        language=city.asr_language or None,
        compute_type=city.asr_compute_type,
        beam_size=city.asr_beam_size,
        initial_prompt=asr_initial_prompt(city.podcast_author, ep.body, ep.title),
    )


def _planned_episode_keys(keys: list[str], uid: str) -> list[str]:
    return [f"{key}/{uid}" for key in keys]


def test_transcribe_plan_is_per_episode_and_uses_snapshot_weights(tmp_path):
    cities = [_city("a", "https://a"), _city("b", "https://b")]
    key_a, key_b = (source_key(city) for city in cities)
    save_records(tmp_path, key_a, {"a1": episode_to_record(_hosted_episode("a1", 3 * 3600))})
    save_records(tmp_path, key_b, {"b1": episode_to_record(_hosted_episode("b1", 30 * 60))})

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
    assert loaded.unit == "episode"
    assert loaded.weights[f"{key_a}/a1"] == 3 * 3600
    assert loaded.weights[f"{key_b}/b1"] == 30 * 60
    assert set(loaded.assignment) == {f"{key_a}/a1", f"{key_b}/b1"}
    assert set(loaded.assignment.values()) == {0, 1}


def test_transcribe_plan_spreads_one_skewed_source_across_all_shards(tmp_path):
    # One hot source with many pending episodes must not pin to a single shard (review/18 §1.2).
    cities = [_city("hot", "https://hot")]
    key = source_key(cities[0])
    save_records(
        tmp_path,
        key,
        {f"u{i}": episode_to_record(_hosted_episode(f"u{i}", 1800)) for i in range(8)},
    )
    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="transcribe",
        num_shards=4,
        defaults={"asr_local_max_duration_hours": 4},
        asr_pipeline_version="3",
    )
    assert plan.unit == "episode"
    assert len(plan.assignment) == 8
    # Equal-weight items balance across all four shards.
    assert set(plan.assignment.values()) == {0, 1, 2, 3}

    # episodes_for_shard partitions the source's uids disjointly + exhaustively across shards.
    seen: set[str] = set()
    for k in range(4):
        owned, owned_uids = episodes_for_shard(
            plan, lane="transcribe", shard_index=k, num_shards=4, expected_sources={key}
        )
        assert owned == {key}
        assert owned_uids is not None
        seen |= owned_uids[key]
    assert seen == {f"u{i}" for i in range(8)}


def test_transcribe_plan_colocates_and_charges_duplicate_stable_meeting_once(tmp_path):
    cities = [_city("primary", "https://primary"), _city("committee", "https://committee")]
    keys = [source_key(city) for city in cities]
    for key in keys:
        save_records(
            tmp_path,
            key,
            {"shared": episode_to_record(_hosted_episode("shared", 2 * 3600))},
        )

    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="transcribe",
        num_shards=4,
        defaults={"asr_local_max_duration_hours": 4},
        asr_pipeline_version="3",
    )

    episode_keys = _planned_episode_keys(keys, "shared")
    assert {plan.assignment[key] for key in episode_keys} == {plan.assignment[episode_keys[0]]}
    assert sorted(plan.weights[key] for key in episode_keys) == [0.0, 2 * 3600]


def test_audio_plan_colocates_distinct_source_keys_for_one_entity(tmp_path):
    cities = [
        _entity_city("combined", "https://example.test/view/1"),
        _entity_city("board", "https://example.test/view/2"),
    ]
    cities[0].source = {
        "feed_urls": [
            "https://example.test/view/1",
            "https://example.test/view/2",
        ]
    }
    cities[1].source = {
        "feed_urls": [
            "https://example.test/view/1",
            "https://example.test/view/2",
            "https://example.test/view/3",
        ],
        "body": "Audit Committee",
    }
    keys = [source_key(city) for city in cities]
    assert keys[0] != keys[1]

    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="audio",
        num_shards=4,
        defaults={},
        asr_pipeline_version="3",
    )

    assert {plan.assignment[key] for key in keys} == {plan.assignment[keys[0]]}


def test_transcribe_plan_does_not_dedupe_same_uid_with_different_transcript_media(tmp_path):
    cities = [_city("a", "https://a"), _city("b", "https://b")]
    keys = [source_key(city) for city in cities]
    ep_a = _hosted_episode("shared", 1800)
    ep_b = _hosted_episode("shared", 1800)
    ep_b.video_url = "https://example.test/other-video"
    save_records(tmp_path, keys[0], {"shared": episode_to_record(ep_a)})
    save_records(tmp_path, keys[1], {"shared": episode_to_record(ep_b)})

    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="transcribe",
        num_shards=2,
        defaults={"asr_local_max_duration_hours": 4},
        asr_pipeline_version="3",
    )

    episode_keys = _planned_episode_keys(keys, "shared")
    assert sorted(plan.weights[key] for key in episode_keys) == [1800, 1800]
    assert sum(plan.weights.values()) == 3600


def test_transcribe_plan_does_not_dedupe_same_uid_with_different_prompts(tmp_path):
    cities = [
        _city("a", "https://a"),
        _city("b", "https://b"),
    ]
    cities[1].podcast_author = "Different County"
    keys = [source_key(city) for city in cities]
    for key in keys:
        save_records(
            tmp_path,
            key,
            {"shared": episode_to_record(_hosted_episode("shared", 1800))},
        )

    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="transcribe",
        num_shards=2,
        defaults={"asr_local_max_duration_hours": 4},
        asr_pipeline_version="3",
    )

    episode_keys = _planned_episode_keys(keys, "shared")
    assert sorted(plan.weights[key] for key in episode_keys) == [1800, 1800]


def test_audio_plan_stays_source_atomic(tmp_path):
    cities = [_city("a", "https://a"), _city("b", "https://b")]
    key_a = source_key(cities[0])
    save_records(tmp_path, key_a, {"a1": episode_to_record(_hosted_episode("a1", 3600))})
    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="audio",
        num_shards=2,
        defaults={},
        asr_pipeline_version="3",
    )
    assert plan.unit == "source"
    assert set(plan.assignment) == {source_key(c) for c in cities}
    owned, owned_uids = episodes_for_shard(
        plan,
        lane="audio",
        shard_index=0,
        num_shards=2,
        expected_sources={source_key(c) for c in cities},
    )
    assert owned_uids is None  # source-atomic: own every uid in each owned source


def test_audio_plan_weights_never_crawled_source_as_unknown_duration(tmp_path):
    from citypods.records import AUDIO_UNKNOWN_DURATION_WEIGHT_SECONDS

    city = _city("new", "https://new")
    key = source_key(city)
    plan = create_shard_plan(
        [city],
        tmp_path,
        lane="audio",
        num_shards=1,
        defaults={},
        asr_pipeline_version="3",
    )

    assert plan.weights[key] == AUDIO_UNKNOWN_DURATION_WEIGHT_SECONDS


def test_caught_up_source_contributes_no_episode_entries(tmp_path):
    # A synced source at the current ASR version is not pending — it drops out of the plan.
    cities = [_city("done", "https://done")]
    key = source_key(cities[0])
    ep = _hosted_episode("d1", 1800)
    recipe = _asr_recipe(cities[0], ep)
    ep.transcript_synced = True
    ep.transcript_key = f"transcripts/{key}/d1-asr-{recipe}.vtt"
    ep.transcript_spec_hash = recipe
    ep.transcript_pipeline_version = "3"
    save_records(tmp_path, key, {"d1": episode_to_record(ep)})
    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="transcribe",
        num_shards=2,
        defaults={"asr_local_max_duration_hours": 4},
        asr_pipeline_version="3",
    )
    assert plan.assignment == {}


def test_old_shape_synced_asr_key_is_planned_for_migration(tmp_path):
    cities = [_city("done", "https://done")]
    key = source_key(cities[0])
    ep = _hosted_episode("d1", 1800)
    old_recipe = asr_spec_hash(
        ep.audio_spec_hash,
        cities[0].asr_model,
        None,
        "3",
        language=cities[0].asr_language or None,
        compute_type=cities[0].asr_compute_type,
        beam_size=cities[0].asr_beam_size,
        initial_prompt=asr_initial_prompt(cities[0].podcast_author, ep.body, ep.title),
    )
    ep.transcript_synced = True
    ep.transcript_key = f"transcripts/{key}/d1-asr-{old_recipe}.vtt"
    ep.transcript_spec_hash = old_recipe
    ep.transcript_pipeline_version = "3"
    save_records(tmp_path, key, {"d1": episode_to_record(ep)})

    plan = create_shard_plan(
        cities,
        tmp_path,
        lane="transcribe",
        num_shards=2,
        defaults={"asr_local_max_duration_hours": 4},
        asr_pipeline_version="3",
    )

    assert set(plan.assignment) == {f"{key}/d1"}
    assert plan.weights[f"{key}/d1"] == 1


def test_episode_plan_rejects_unconfigured_source():
    plan = ShardPlan(
        lane="transcribe",
        num_shards=2,
        assignment={"src1/u1": 0, "src2/u2": 1},
        weights={"src1/u1": 1.0, "src2/u2": 1.0},
        unit="episode",
    )
    with pytest.raises(ValueError, match="extra"):
        episodes_for_shard(
            plan, lane="transcribe", shard_index=0, num_shards=2, expected_sources={"src1"}
        )


def test_plan_validation_fails_closed_on_lane_shards_or_source_drift():
    plan = ShardPlan(
        lane="transcribe",
        num_shards=2,
        assignment={"a": 0, "b": 1},
        weights={"a": 1, "b": 1},
        unit="source",
    )
    with pytest.raises(ValueError, match="lane"):
        episodes_for_shard(
            plan, lane="align", shard_index=0, num_shards=2, expected_sources={"a", "b"}
        )
    with pytest.raises(ValueError, match="has 2 shards"):
        episodes_for_shard(
            plan, lane="transcribe", shard_index=0, num_shards=4, expected_sources={"a", "b"}
        )
    with pytest.raises(ValueError, match="shard index 2 out of range"):
        episodes_for_shard(
            plan, lane="transcribe", shard_index=2, num_shards=2, expected_sources={"a", "b"}
        )
    with pytest.raises(ValueError, match="source set"):
        episodes_for_shard(
            plan, lane="transcribe", shard_index=0, num_shards=2, expected_sources={"a", "c"}
        )


def test_load_plan_rejects_v1(tmp_path):
    path = tmp_path / "v1.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "lane": "transcribe",
                "num_shards": 2,
                "assignment": {"a": 0, "b": 1},
                "weights": {"a": 1, "b": 1},
            }
        )
    )
    with pytest.raises(ValueError, match="unsupported shard plan version"):
        load_shard_plan(path)


def test_load_plan_rejects_mismatched_assignment_and_weights(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "lane": "transcribe",
                "unit": "episode",
                "num_shards": 2,
                "assignment": {"a/u1": 0},
                "weights": {"b/u1": 1},
            }
        )
    )
    with pytest.raises(ValueError, match="key sets differ"):
        load_shard_plan(path)
