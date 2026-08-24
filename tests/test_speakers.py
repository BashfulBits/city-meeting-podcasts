from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType

import pytest

from citypods.diarize import _attach_embeddings, _mark_overlap, attach_transcript_words
from citypods.models import City, Episode
from citypods.records import meeting_page_hash
from citypods.site import speaker_page_rows
from citypods.speaker_benchmark import _cases, compare
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
    pilot_capture_context,
    pilot_selected,
    profile_matches,
    public_turn,
    quote_attribution,
    refresh_membership_status,
    roster_person_ids,
    shadow_candidate_id,
)


def test_embedding_inference_receives_the_selected_diarization_device(monkeypatch, tmp_path):
    inference_instance = type("InferenceInstance", (), {})()
    inference_instance.crop = lambda *_args: [0.25, 0.75]
    received: dict[str, object] = {}

    class FakeInference:
        def __init__(self, _model, *, window, device):
            received.update({"window": window, "device": device})

        def crop(self, *_args):
            return inference_instance.crop()

    class FakeModel:
        @staticmethod
        def from_pretrained(model, *, token):
            received.update({"model": model, "token": token})
            return object()

    class FakeSegment:
        def __init__(self, start, end):
            self.start = start
            self.end = end

    pyannote = ModuleType("pyannote")
    audio = ModuleType("pyannote.audio")
    core = ModuleType("pyannote.core")
    audio.Inference = FakeInference
    audio.Model = FakeModel
    core.Segment = FakeSegment
    pyannote.audio = audio
    pyannote.core = core
    torch = ModuleType("torch")
    torch.device = lambda value: f"device:{value}"
    monkeypatch.setitem(sys.modules, "pyannote", pyannote)
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio)
    monkeypatch.setitem(sys.modules, "pyannote.core", core)
    monkeypatch.setitem(sys.modules, "torch", torch)

    turns = [{"start": 1.0, "end": 2.0}]
    _attach_embeddings(
        tmp_path / "meeting.m4a", turns, "embedding-v1", token="hf-test", device="cuda"
    )

    assert received == {
        "model": "embedding-v1",
        "token": "hf-test",
        "window": "whole",
        "device": "device:cuda",
    }
    assert turns[0]["embedding"] == [0.25, 0.75]


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
    candidate["embedding"] = [0.1, 0.2]
    candidate["match_score"] = 0.99
    assert "0.99" not in _review_body(candidate)


def test_packaged_chair_reference_can_be_approved_and_ingested(tmp_path, monkeypatch):
    candidate = {
        "kind": "chair-reference",
        "candidate_id": "r7-ref-test",
        "city_slug": "demo-tx",
        "body": "Council",
        "engine_recipe": "rss:pyannote:1:model:embedding",
        "capture_context": "council-chamber-v1",
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
        "embedding_recipe": "embedding",
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "evaluation.json").write_text(
        json.dumps({"reference_candidates": {candidate["candidate_id"]: candidate}})
    )
    (state_dir / "evidence.json").write_text(
        json.dumps(
            {
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
        )
    )
    issue = tmp_path / "issue.md"
    issue.write_text(
        _review_body(candidate).replace(
            "- [ ] Approve as a golden voice reference",
            "- [x] Approve as a golden voice reference",
        )
    )
    site = {
        "speakers": {
            "evaluation_state_path": "evaluation.json",
            "registry_path": "registry.json",
            "turn_evidence_path": "evidence.json",
        }
    }
    monkeypatch.setattr("citypods.config.load_site_config", lambda _: site)
    monkeypatch.setattr("citypods.state.resolve_state_dir", lambda *_args: state_dir)
    monkeypatch.setattr("citypods.storage.make_storage", lambda *_args: object())
    monkeypatch.setattr("citypods.statesync.pull_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("citypods.statesync.push_state", lambda *_args, **_kwargs: 0)
    assert (
        speaker_review_main(
            [
                "ingest",
                "--issue-number",
                "1",
                "--issue-body-file",
                str(issue),
                "--actor",
                "maintainer",
            ]
        )
        == 0
    )
    stored = json.loads((state_dir / "registry.json").read_text())
    assert next(iter(stored["people"].values()))["references"][0]["embedding"] == [0.1, 0.2]


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
        {"episode_uid": "two", "embedding": [0.99, 0.01], "embedding_recipe": "recipe-v1"},
    ]
    person["references"][0]["embedding_recipe"] = "recipe-v1"
    refresh_membership_status(registry, now=published)
    matches = profile_matches(registry, [1.0, 0.0], embedding_recipe="recipe-v1")
    turn = assign_turn({"start": 10.0, "end": 25.0, "overlap": False}, matches, publish=True)
    attributed = quote_attribution({"start": 12.0, "end": 20.0}, [turn])
    assert attributed == {
        "speaker_id": person["speaker_id"],
        "display_name": "Alex Rivera",
        "status": "provisional",
        "method": "voice-profile",
    }


def test_profile_matches_only_uses_the_active_embedding_recipe():
    registry = empty_registry()
    observe_attendance(
        registry,
        city_slug="demo-tx",
        body="Council",
        episode_uid="one",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        roster=[{"name": "Alex Rivera"}],
    )
    person = next(iter(registry["people"].values()))
    person["references"] = [
        {"episode_uid": "one", "embedding": [1.0, 0.0], "embedding_recipe": "old"},
        {"episode_uid": "two", "embedding": [1.0, 0.0], "embedding_recipe": "old"},
    ]
    refresh_membership_status(registry, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert profile_matches(registry, [1.0, 0.0], embedding_recipe="new") == []


def test_profile_needs_two_distinct_meetings_for_the_active_embedding_recipe():
    registry = empty_registry()
    observe_attendance(
        registry,
        city_slug="demo-tx",
        body="Council",
        episode_uid="one",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        roster=[{"name": "Alex Rivera"}],
    )
    person = next(iter(registry["people"].values()))
    person["references"] = [
        {"episode_uid": "one", "embedding": [1.0, 0.0], "embedding_recipe": "old"},
        {"episode_uid": "two", "embedding": [1.0, 0.0], "embedding_recipe": "old"},
        {"episode_uid": "three", "embedding": [1.0, 0.0], "embedding_recipe": "new"},
    ]
    refresh_membership_status(registry, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert profile_matches(registry, [1.0, 0.0], embedding_recipe="new") == []


def test_attendance_aliases_do_not_merge_people_across_bodies():
    registry = empty_registry()
    published = datetime(2026, 1, 1, tzinfo=UTC)
    for body in ("Council", "Airport Board"):
        observe_attendance(
            registry,
            city_slug="demo-tx",
            body=body,
            episode_uid=body,
            published=published,
            roster=[{"name": "Alex Rivera"}],
        )
    assert len(registry["people"]) == 2


def test_roster_constraint_accepts_confirmed_aliases_and_preserves_unparseable_minutes():
    registry = empty_registry()
    observe_attendance(
        registry,
        city_slug="demo-tx",
        body="Council",
        episode_uid="one",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        roster=[{"name": "Alexandra Rivera"}],
    )
    ident, person = next(iter(registry["people"].items()))
    person["aliases"] = ["Alex Rivera"]
    assert roster_person_ids(registry, [{"name": "Alex Rivera"}]) == {ident}
    assert roster_person_ids(registry, [{"not_name": "broken"}]) is None
    turn = assign_turn(
        {"start": 1.0, "end": 2.0},
        [{"speaker_id": ident, "display_name": "Alexandra Rivera", "score": 0.99}],
        publish=True,
        confirmed=True,
    )
    assert turn["identity"]["status"] == "confirmed"


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


def test_timed_words_prefer_top_level_rows_over_duplicate_segment_rows():
    turn = {"start": 10.0, "end": 20.0}
    attach_transcript_words(
        [turn],
        {
            "words": [{"w": "once", "s": 11.0, "e": 12.0}],
            "segments": [{"words": [{"w": "once", "s": 11.0, "e": 12.0}]}],
        },
    )
    assert turn["transcript_word_count"] == 1
    assert len(turn["transcript_text_hash"]) == 64


def test_identity_calibration_requires_30_days_30_reviews_and_95_percent():
    now = datetime(2026, 3, 1, tzinfo=UTC)
    cell = calibration_cell(
        "demo-tx", "Council", "pyannote-v1", capture_context="council-chamber-v1"
    )
    state = {
        "benchmarks": [{"cell": cell, "selected_engine": "pyannote"}],
        "reviews": [
            {
                "cell": cell,
                "correct": index != 0,
                "reviewed_at": (now - timedelta(days=31)).isoformat(),
            }
            for index in range(30)
        ],
    }
    assert auto_publish_allowed(state, cell=cell, engine="pyannote", now=now)
    state["reviews"][1]["correct"] = False
    assert not auto_publish_allowed(state, cell=cell, engine="pyannote", now=now)


def test_identity_calibration_requires_a_private_benchmark_decision():
    now = datetime(2026, 3, 1, tzinfo=UTC)
    cell = calibration_cell(
        "demo-tx", "Council", "pyannote-v1", capture_context="council-chamber-v1"
    )
    state = {
        "reviews": [
            {"cell": cell, "correct": True, "reviewed_at": (now - timedelta(days=31)).isoformat()}
            for _ in range(30)
        ]
    }
    assert not auto_publish_allowed(state, cell=cell, engine="pyannote", now=now)


def test_identity_calibration_requires_the_benchmarked_engine():
    now = datetime(2026, 3, 1, tzinfo=UTC)
    cell = calibration_cell(
        "demo-tx", "Council", "pyannote-v1", capture_context="council-chamber-v1"
    )
    state = {
        "benchmarks": [{"cell": cell, "selected_engine": "wespeaker"}],
        "reviews": [
            {"cell": cell, "correct": True, "reviewed_at": (now - timedelta(days=31)).isoformat()}
            for _ in range(30)
        ],
    }
    assert not auto_publish_allowed(state, cell=cell, engine="pyannote", now=now)


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


def test_benchmark_overlap_recall_does_not_count_one_gold_region_twice():
    report = compare(
        [{"start": 10.0, "end": 20.0, "speaker": "Alex", "overlap": True}],
        [
            {"start": 10.0, "end": 15.0, "cluster": "A", "overlap": True},
            {"start": 15.0, "end": 20.0, "cluster": "B", "overlap": True},
        ],
    )
    assert report["overlap_precision"] == 1.0
    assert report["overlap_recall"] == 1.0


def test_benchmark_cases_reject_invalid_turn_shapes():
    with pytest.raises(ValueError, match="case 'demo' turn 0 needs numeric start and end"):
        _cases({"cases": [{"id": "demo", "turns": [{"start": 1}]}]})


def test_pilot_selection_is_explicit_and_body_scoped():
    config = {
        "pilot_bodies": [
            {
                "city": "denton-tx",
                "body": "City Council",
                "capture_context": "council-chamber-v1",
            }
        ]
    }
    assert pilot_selected(config, "denton-tx", "City Council")
    assert pilot_capture_context(config, "denton-tx", "City Council") == "council-chamber-v1"
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


def test_speaker_pages_skip_attribution_without_a_meeting_page_destination():
    episode = Episode(
        guid="",
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
                "speaker_id": "spk-0123456789abcdef",
                "display_name": "Alex",
                "status": "provisional",
            },
        }
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
    assert speaker_page_rows(city, [episode], "https://example.test") == {}


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
