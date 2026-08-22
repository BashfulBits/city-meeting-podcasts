from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from citypods.diarize import _mark_overlap, attach_transcript_words
from citypods.models import City, Episode
from citypods.records import meeting_page_hash
from citypods.site import speaker_page_rows
from citypods.speaker_benchmark import compare
from citypods.speaker_review import main as speaker_review_main
from citypods.speakers import (
    assign_turn,
    auto_publish_allowed,
    calibration_cell,
    empty_registry,
    observe_attendance,
    pilot_selected,
    profile_matches,
    public_turn,
    quote_attribution,
    refresh_membership_status,
    shadow_candidate_id,
)


def test_two_meeting_profile_can_attribute_a_single_speaker_quote():
    registry = empty_registry()
    published = datetime(2026, 1, 1, tzinfo=UTC)
    observe_attendance(
        registry,
        city_slug="demo-tx",
        body="Council",
        episode_uid="one",
        published=published,
        roster=[{"name": "Alex Rivera"}],
    )
    person = next(iter(registry["people"].values()))
    person["references"] = [
        {"episode_uid": "one", "embedding": [1.0, 0.0]},
        {"episode_uid": "two", "embedding": [0.99, 0.01]},
    ]
    refresh_membership_status(registry, now=published)
    matches = profile_matches(registry, [1.0, 0.0])
    turn = assign_turn({"start": 10.0, "end": 25.0, "overlap": False}, matches, publish=True)
    attributed = quote_attribution({"start": 12.0, "end": 20.0}, [turn])
    assert attributed == {
        "speaker_id": person["speaker_id"],
        "display_name": "Alex Rivera",
        "status": "provisional",
        "method": "voice-profile",
    }


def test_quote_attribution_rejects_crosstalk_and_partial_turns():
    identity = {"speaker_id": "spk-a", "display_name": "Alex", "status": "confirmed"}
    assert (
        quote_attribution(
            {"start": 10, "end": 20},
            [{"start": 10, "end": 20, "overlap": True, "identity": identity}],
        )
        is None
    )
    assert (
        quote_attribution(
            {"start": 10, "end": 20}, [{"start": 12, "end": 20, "identity": identity}]
        )
        is None
    )


def test_diarization_turns_receive_only_timed_word_evidence_and_overlap_flags():
    turns = [
        {"start": 10.0, "end": 20.0, "cluster": "A", "overlap": False},
        {"start": 19.5, "end": 24.0, "cluster": "B", "overlap": False},
    ]
    _mark_overlap(turns)
    attach_transcript_words(
        turns,
        {
            "segments": [
                {
                    "words": [
                        {"w": "public", "s": 11.0, "e": 12.0},
                        {"w": "record", "s": 12.0, "e": 13.0},
                        {"w": "later", "s": 22.0, "e": 23.0},
                    ]
                }
            ]
        },
    )
    assert all(turn["overlap"] for turn in turns)
    assert turns[0]["transcript_word_count"] == 2
    assert len(turns[0]["transcript_text_hash"]) == 64
    assert "public" not in turns[0].values()
    assert "embedding" not in public_turn({"start": 1, "embedding": [0.1]})


def test_identity_calibration_requires_30_days_30_reviews_and_95_percent():
    now = datetime(2026, 3, 1, tzinfo=UTC)
    cell = calibration_cell("demo-tx", "Council", "pyannote-v1")
    state = {
        "reviews": [
            {
                "cell": cell,
                "correct": index != 0,
                "reviewed_at": (now - timedelta(days=31)).isoformat(),
            }
            for index in range(30)
        ]
    }
    assert auto_publish_allowed(state, cell=cell, now=now)
    state["reviews"][1]["correct"] = False
    assert not auto_publish_allowed(state, cell=cell, now=now)


def test_shadow_candidate_and_offline_benchmark_are_engine_neutral():
    turn = {"start": 10.0, "end": 20.0, "cluster": "A", "identity": {"speaker_id": "spk-a"}}
    candidate = shadow_candidate_id(
        city_slug="demo-tx", body="Council", episode_uid="one", recipe="rss:pyannote", turn=turn
    )
    assert candidate.startswith("r7-")
    report = compare(
        [{"start": 10.0, "end": 20.0, "speaker": "Alex", "speaker_id": "spk-a"}],
        [turn],
    )
    assert report["turn_cluster_accuracy"] == 1.0
    assert report["identity_precision"] == 1.0


def test_pilot_selection_is_explicit_and_body_scoped():
    config = {"pilot_bodies": [{"city": "denton-tx", "body": "City Council"}]}
    assert pilot_selected(config, "denton-tx", "City Council")
    assert not pilot_selected(config, "denton-tx", "Planning and Zoning Commission")
    assert not pilot_selected(config, "austin-tx", "City Council")


def test_speaker_pages_only_include_admitted_named_quotes():
    episode = Episode(
        guid="one",
        uid="one",
        title="Council meeting",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="https://example.test/video",
    )
    episode.moment_pullquote_candidates = [
        {
            "admission": "admitted",
            "quote": "A grounded quote.",
            "start": 12,
            "speaker_attribution": {
                "speaker_id": "spk-a",
                "display_name": "Alex",
                "status": "provisional",
            },
        },
        {
            "admission": "shadow",
            "quote": "Private candidate.",
            "start": 13,
            "speaker_attribution": {
                "speaker_id": "spk-b",
                "display_name": "Blair",
                "status": "provisional",
            },
        },
    ]
    city = City(
        slug="demo-tx",
        source={},
        podcast_title="Demo",
        podcast_author="Demo",
        podcast_email="demo@example.test",
        podcast_description="Demo",
        provider="rss",
    )
    rows = speaker_page_rows(city, [episode], "https://example.test")
    assert list(rows) == ["spk-a"]
    assert rows["spk-a"]["quotes"][0]["url"].endswith("/demo-tx/one/#t=12")


def test_meeting_page_hash_changes_when_quote_gains_speaker_attribution():
    episode = Episode(
        guid="one",
        uid="one",
        title="Council meeting",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="https://example.test/video",
    )
    episode.moment_pullquote_candidates = [
        {"admission": "admitted", "quote": "A quote.", "start": 1}
    ]
    before = meeting_page_hash(episode)
    episode.moment_pullquote_candidates[0]["speaker_attribution"] = {
        "speaker_id": "spk-a",
        "display_name": "Alex",
        "status": "provisional",
    }
    assert meeting_page_hash(episode) != before


def test_speaker_review_requires_real_reference_and_is_idempotent(tmp_path):
    embedding = tmp_path / "embedding.json"
    embedding.write_text("[1.0, 0.0]")
    registry = tmp_path / "registry.json"
    arguments = [
        "approve-reference",
        "--registry",
        str(registry),
        "--city",
        "demo-tx",
        "--body",
        "Council",
        "--name",
        "Alex Rivera",
        "--episode-uid",
        "one",
        "--start",
        "10",
        "--end",
        "20",
        "--text-hash",
        "abc",
        "--embedding",
        str(embedding),
        "--embedding-recipe",
        "pyannote-v1",
        "--reviewer",
        "maintainer",
    ]
    assert speaker_review_main(arguments) == 0
    assert speaker_review_main(arguments) == 0
    stored = json.loads(registry.read_text())
    assert len(next(iter(stored["people"].values()))["references"]) == 1
