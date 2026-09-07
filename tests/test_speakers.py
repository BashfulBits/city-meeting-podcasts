from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from citypods.diarize import _mark_overlap, attach_transcript_words, has_valid_timed_words
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
    self_introduction_candidates,
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


def test_self_introduction_candidates_finds_a_stated_name():
    """Matches a real pattern from this project's own transcripts: a public commenter stating
    their name near the start of their turn ("MY NAME IS REZA")."""
    words = {
        "segments": [
            {
                "words": [
                    {"w": "My", "s": 0.5, "e": 0.7},
                    {"w": "name", "s": 0.7, "e": 0.9},
                    {"w": "is", "s": 0.9, "e": 1.0},
                    {"w": "Reza.", "s": 1.0, "e": 1.4},
                    {"w": "To", "s": 1.5, "e": 1.7},
                    {"w": "answer", "s": 1.7, "e": 2.0},
                ]
            }
        ]
    }
    turns = [{"start": 0.0, "end": 20.0, "cluster": "0", "embedding": [0.1], "overlap": False}]
    candidates = self_introduction_candidates(words, turns)
    assert len(candidates) == 1
    assert candidates[0]["display_name"] == "Reza"
    assert candidates[0]["cue_kind"] == "self-stated"
    assert candidates[0]["kind"] == "self-introduction"
    # Unlike chair_reference_candidates, the corroborated span is the turn itself.
    assert candidates[0]["start"] == 0.0
    assert candidates[0]["end"] == 20.0


def test_self_introduction_candidates_finds_a_name_then_staff_title():
    """Matches this project's other observed real pattern: a staff presenter naming themselves
    with no framing phrase before their title ("MATT BODINE, ASSISTANT PLANNER")."""
    words = {
        "segments": [
            {
                "words": [
                    {"w": "Matt", "s": 2.0, "e": 2.3},
                    {"w": "Bodine,", "s": 2.3, "e": 2.7},
                    {"w": "Assistant", "s": 2.8, "e": 3.1},
                    {"w": "Planner.", "s": 3.1, "e": 3.5},
                ]
            }
        ]
    }
    turns = [{"start": 2.0, "end": 30.0, "cluster": "1", "embedding": [0.2], "overlap": False}]
    candidates = self_introduction_candidates(words, turns)
    assert len(candidates) == 1
    assert candidates[0]["display_name"] == "Matt Bodine"
    assert candidates[0]["cue_kind"] == "name-then-title"


def test_self_introduction_candidates_ignores_turns_without_embeddings_or_overlap():
    words = {
        "segments": [
            {
                "words": [
                    {"w": "My", "s": 0.0, "e": 0.1},
                    {"w": "name", "s": 0.1, "e": 0.2},
                    {"w": "is", "s": 0.2, "e": 0.3},
                    {"w": "Sam.", "s": 0.3, "e": 0.5},
                ]
            }
        ]
    }
    no_embedding = [{"start": 0.0, "end": 10.0, "cluster": "0", "overlap": False}]
    overlapped = [{"start": 0.0, "end": 10.0, "cluster": "0", "embedding": [0.1], "overlap": True}]
    assert self_introduction_candidates(words, no_embedding) == []
    assert self_introduction_candidates(words, overlapped) == []


def test_self_introduction_candidates_ignores_a_name_stated_outside_the_ten_second_window():
    words = {
        "segments": [
            {
                "words": [
                    {"w": "My", "s": 15.0, "e": 15.1},
                    {"w": "name", "s": 15.1, "e": 15.2},
                    {"w": "is", "s": 15.2, "e": 15.3},
                    {"w": "Sam.", "s": 15.3, "e": 15.5},
                ]
            }
        ]
    }
    turns = [{"start": 0.0, "end": 30.0, "cluster": "0", "embedding": [0.1], "overlap": False}]
    assert self_introduction_candidates(words, turns) == []


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


def test_pilot_selection_accepts_explicit_provider_body_prefix():
    config = {
        "pilot_bodies": [
            {
                "city": "denton-tx",
                "body": "City Council",
                "body_prefixes": ["City Council", "Special Called City Council"],
                "capture_context": "council-chamber-v1",
            }
        ]
    }
    assert pilot_selected(config, "denton-tx", "City Council Regular Meeting")
    assert pilot_selected(config, "denton-tx", "Special Called City Council Meeting")
    assert pilot_capture_context(config, "denton-tx", "City Council on 2026-08-18 2:00 PM") == (
        "council-chamber-v1"
    )
    assert not pilot_selected(config, "denton-tx", "Planning and Zoning Commission")
    assert not pilot_selected(config, "denton-tx", "City Council Joint Meeting")
    assert not pilot_selected(config, "denton-tx", "City Council - Section 1")


def test_timed_word_validation_rejects_empty_or_invalid_sidecars():
    assert not has_valid_timed_words(b'{"segments": [{"words": []}]}')
    assert not has_valid_timed_words(b'{"segments": [{"words": [{"w": "hello", "s": 1, "e": 1}]}]}')
    assert has_valid_timed_words(b'{"segments": [{"words": [{"w": "hello", "s": 1, "e": 1.5}]}]}')
    assert not has_valid_timed_words(
        b'{"segments": [{"words": [{"w": null, "s": false, "e": true}]}]}'
    )
    assert not has_valid_timed_words(
        json.dumps(
            {"segments": [{"words": [{"w": "hello", "s": 10**400, "e": 10**400 + 1}]}]}
        ).encode()
    )


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


def test_pilot_selection_matches_feed_slug_and_entity_slug():
    config = {
        "pilot_bodies": [
            {
                "city": "denton-tx",
                "body": "City Council",
                "capture_context": "council-chamber-v1",
            }
        ]
    }
    # Matches entity slug directly
    assert pilot_selected(config, "denton-tx", "City Council")
    # Matches feed slugs that start with entity slug followed by a hyphen
    assert pilot_selected(config, "denton-tx-city-council", "City Council")
    assert pilot_selected(config, "denton-tx-board-of-ethics", "City Council")
    # Capture context matches both entity slug and feed slug
    assert pilot_capture_context(config, "denton-tx", "City Council") == "council-chamber-v1"
    assert (
        pilot_capture_context(config, "denton-tx-city-council", "City Council")
        == "council-chamber-v1"
    )
    # Does not match unrelated cities or unhyphenated overlaps
    assert not pilot_selected(config, "austin-tx", "City Council")
    assert not pilot_selected(config, "denton", "City Council")
    assert not pilot_selected(config, "denton-tx-other", "Planning Commission")


def test_pilot_selection_wildcards_and_allow_all():
    # Wildcard city
    wildcard_city_config = {
        "pilot_bodies": [
            {
                "city": "*",
                "body": "City Council",
                "capture_context": "universal-chamber-v1",
            }
        ]
    }
    assert pilot_selected(wildcard_city_config, "denton-tx", "City Council")
    assert pilot_selected(wildcard_city_config, "austin-tx-city-council", "City Council")
    assert not pilot_selected(wildcard_city_config, "denton-tx", "Library Board")
    assert (
        pilot_capture_context(wildcard_city_config, "austin-tx", "City Council")
        == "universal-chamber-v1"
    )

    # Wildcard body and all_bodies flag
    wildcard_body_config = {
        "pilot_bodies": [
            {
                "city": "denton-tx",
                "body": "*",
            }
        ]
    }
    assert pilot_selected(wildcard_body_config, "denton-tx", "City Council")
    assert pilot_selected(wildcard_body_config, "denton-tx-city-council", "Ethics Board")
    assert not pilot_selected(wildcard_body_config, "austin-tx", "Ethics Board")
    # Context falls back to city slug audio context when capture_context omitted with wildcard
    assert pilot_capture_context(wildcard_body_config, "denton-tx", "Any Body") == (
        "denton-tx-audio-v1"
    )

    all_bodies_flag_config = {
        "pilot_bodies": [
            {
                "city": "denton-tx",
                "all_bodies": True,
                "capture_context": "denton-v1",
            }
        ]
    }
    assert pilot_selected(all_bodies_flag_config, "denton-tx", "Any Body")
    assert pilot_capture_context(all_bodies_flag_config, "denton-tx", "Any Body") == "denton-v1"

    # Global allow_all_cities flag
    allow_all_config = {"allow_all_cities": True}
    assert pilot_selected(allow_all_config, "any-city", "Any Body")
    assert (
        pilot_capture_context(allow_all_config, "springfield-il", "Any Body")
        == "springfield-il-audio-v1"
    )

    # Fail-closed default when pilot_bodies is empty or missing
    empty_config: dict[str, object] = {"pilot_bodies": []}
    assert not pilot_selected(empty_config, "denton-tx", "City Council")
    assert pilot_capture_context(empty_config, "denton-tx", "City Council") is None
    assert not pilot_selected({}, "denton-tx", "City Council")
    assert pilot_capture_context({}, "denton-tx", "City Council") is None


def test_pilot_selection_real_site_config_matches_denton():
    from pathlib import Path

    from citypods.config import load_site_config

    site_config_path = Path(__file__).resolve().parent.parent / "config" / "site_config.yml"
    if not site_config_path.exists():
        pytest.skip("config/site_config.yml not found")
    site_config = load_site_config(site_config_path)
    speaker_cfg = site_config.get("speakers", {})

    # Entity slug match
    assert pilot_selected(speaker_cfg, "denton-tx", "City Council")
    assert pilot_selected(speaker_cfg, "denton-tx", "Special Called City Council Meeting")
    # Feed slug match
    assert pilot_selected(speaker_cfg, "denton-tx-city-council", "City Council")
    assert pilot_selected(speaker_cfg, "denton-tx-board-of-ethics", "City Council")
    # Non-pilot bodies should still be rejected
    assert not pilot_selected(speaker_cfg, "denton-tx", "Planning and Zoning Commission")
    assert not pilot_selected(speaker_cfg, "austin-tx", "City Council")
