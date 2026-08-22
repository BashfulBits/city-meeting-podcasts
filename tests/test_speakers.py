from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from citypods.diarize import _mark_overlap, attach_transcript_words
from citypods.models import City, Episode
from citypods.records import meeting_page_hash
from citypods.site import speaker_page_rows
from citypods.speaker_benchmark import compare
from citypods.speaker_review import (
    _record_reference_review,
    _review_body,
)
from citypods.speaker_review import (
    main as speaker_review_main,
)
from citypods.speakers import (
    assign_turn,
    auto_publish_allowed,
    calibration_cell,
    chair_reference_candidates,
    empty_registry,
    observe_attendance,
    pilot_selected,
    profile_matches,
    public_turn,
    quote_attribution,
    refresh_membership_status,
    shadow_candidate_id,
)


def test_chair_reference_candidates_cover_formal_and_title_led_announcements():
    words = {
        "segments": [
            {
                "words": [
                    {"w": "The", "s": 0.0, "e": 0.2},
                    {"w": "chair", "s": 0.2, "e": 0.4},
                    {"w": "recognizes", "s": 0.4, "e": 0.8},
                    {"w": "Council", "s": 0.8, "e": 1.0},
                    {"w": "Member", "s": 1.0, "e": 1.2},
                    {"w": "Jane", "s": 1.2, "e": 1.5},
                    {"w": "Doe.", "s": 1.5, "e": 1.8},
                ]
            }
        ]
    }
    turns = [
        {"start": 0.0, "end": 2.0, "cluster": "chair", "overlap": False},
        {
            "start": 2.1,
            "end": 5.0,
            "cluster": "jane",
            "overlap": False,
            "embedding": [0.1, 0.2],
            "transcript_text_hash": "hash",
        },
    ]
    formal = chair_reference_candidates(words, turns)
    assert len(formal) == 1
    assert formal[0]["display_name"] == "Jane Doe"
    assert formal[0]["cue_kind"] == "chair-recognition"

    title_words = {
        "segments": [
            {
                "words": [
                    {"w": "Commissioner", "s": 10.0, "e": 10.3},
                    {"w": "Jane", "s": 10.3, "e": 10.5},
                    {"w": "Doe", "s": 10.5, "e": 10.7},
                ]
            }
        ]
    }
    title_turns = [
        {"start": 10.0, "end": 11.0, "cluster": "chair", "overlap": False},
        {"start": 11.1, "end": 13.0, "cluster": "jane", "overlap": False, "embedding": [0.2]},
    ]
    title = chair_reference_candidates(title_words, title_turns)
    assert len(title) == 1
    assert title[0]["display_name"] == "Jane Doe"
    assert title[0]["cue_kind"] == "title-announcement"


def test_approved_chair_reference_adds_private_embedding_only():
    candidate = {
        "kind": "chair-reference",
        "candidate_id": "r7-ref-test",
        "city_slug": "demo-tx",
        "body": "Council",
        "engine_recipe": "rss:pyannote",
        "episode_uid": "one",
        "display_name": "Jane Doe",
        "start": 2.1,
        "end": 5.0,
        "cluster": "jane",
        "cue_start": 0.0,
        "cue_end": 1.8,
        "cue_text": "The chair recognizes Jane Doe",
        "cue_kind": "chair-recognition",
        "transcript_text_hash": "hash",
    }
    state = {"reference_candidates": {candidate["candidate_id"]: candidate}}
    evidence = {
        "episodes": {
            "one": {
                "turns": [
                    {
                        "start": 2.1,
                        "end": 5.0,
                        "cluster": "jane",
                        "embedding": [0.1, 0.2],
                    }
                ]
            }
        }
    }
    registry = empty_registry()
    _record_reference_review(
        state,
        registry,
        evidence,
        candidate,
        approved=True,
        reviewer="maintainer",
        review_id="github-issue-1",
    )
    person = next(iter(registry["people"].values()))
    assert person["display_name"] == "Jane Doe"
    assert person["references"][0]["embedding"] == [0.1, 0.2]
    body = _review_body(candidate)
    assert "r7-reference-candidate-b64" in body
    assert "Approve as a golden voice reference" in body


def test_speaker_review_package_groups_reference_and_shadow_children(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "evaluation.json").write_text(
        json.dumps(
            {
                "reviews": [],
                "candidates": {
                    "r7-shadow": {
                        "candidate_id": "r7-shadow",
                        "city_slug": "demo-tx",
                        "body": "Council",
                        "episode_uid": "one",
                        "start": 1,
                        "end": 2,
                        "speaker_id": "spk-a",
                        "display_name": "Alex",
                    }
                },
                "reference_candidates": {
                    "r7-ref": {
                        "kind": "chair-reference",
                        "candidate_id": "r7-ref",
                        "city_slug": "demo-tx",
                        "body": "Council",
                        "episode_uid": "one",
                        "start": 3,
                        "end": 4,
                        "display_name": "Jane Doe",
                        "cue_start": 2,
                        "cue_end": 2.5,
                        "cue_text": "Commissioner Jane Doe",
                        "cue_kind": "title-announcement",
                    }
                },
            }
        )
    )
    site = {
        "speakers": {
            "evaluation_state_path": "evaluation.json",
            "registry_path": "registry.json",
            "turn_evidence_path": "evidence.json",
            "weekly_review_limit": 8,
        }
    }
    monkeypatch.setattr("citypods.config.load_site_config", lambda _: site)
    monkeypatch.setattr("citypods.state.resolve_state_dir", lambda *_args: state_dir)
    monkeypatch.setattr("citypods.storage.make_storage", lambda *_args: object())
    monkeypatch.setattr("citypods.statesync.pull_state", lambda *_args, **_kwargs: None)
    out_dir = tmp_path / "review"
    assert speaker_review_main(["package", "--out-dir", str(out_dir)]) == 0
    batch = json.loads((out_dir / "review-batch.json").read_text())
    assert {row["kind"] for row in batch["children"]} == {"shadow-match", "chair-reference"}
    assert (out_dir / "parent.md").exists()


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
