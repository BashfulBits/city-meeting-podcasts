from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from citypods.diarize import _mark_overlap, attach_transcript_words, has_valid_timed_words
from citypods.models import City, Episode
from citypods.naming import PrecisionTable
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
    body_membership,
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
from citypods.stages import SpeakerIdentityStage


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


def _review_state(**ledgers) -> dict:
    return {"reviews": [], **ledgers}


def _package(tmp_path, monkeypatch, state: dict, limit: int = 8):
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "evaluation.json").write_text(json.dumps(state))
    site = {
        "speakers": {
            "evaluation_state_path": "evaluation.json",
            "registry_path": "registry.json",
            "turn_evidence_path": "evidence.json",
            "weekly_review_limit": limit,
        }
    }
    monkeypatch.setattr("citypods.config.load_site_config", lambda _: site)
    monkeypatch.setattr("citypods.state.resolve_state_dir", lambda *_args: state_dir)
    monkeypatch.setattr("citypods.storage.make_storage", lambda *_args: object())
    monkeypatch.setattr("citypods.statesync.pull_state", lambda *_args, **_kwargs: None)
    out_dir = tmp_path / "review"
    assert speaker_review_main(["package", "--out-dir", str(out_dir)]) == 0
    return json.loads((out_dir / "review-batch.json").read_text()), out_dir


def _naming_row(candidate_id: str, *, signals: list[str], name: str = "Matt Bodine") -> dict:
    return {
        "kind": "naming",
        "candidate_id": candidate_id,
        "city_slug": "demo-tx",
        "body": "Council",
        "engine_recipe": "sherpa:v1",
        "capture_context": "chamber-v1",
        "episode_uid": "one",
        "cluster": "c1",
        "display_name": name,
        "tier": "staff",
        "signals": signals,
        "combination_key": f"staff:{'+'.join(signals)}",
    }


def test_naming_candidates_reach_the_weekly_review_queue(tmp_path, monkeypatch):
    """Without this the adaptive gate can never open: naming verdicts are the *only* input to the
    precision table, so a queue that cannot show a naming candidate means no combination ever
    becomes trusted and staff are never auto-named."""
    batch, out_dir = _package(
        tmp_path,
        monkeypatch,
        _review_state(
            naming_candidates={"r7-name-a": _naming_row("r7-name-a", signals=["self-introduction"])}
        ),
    )
    assert [row["kind"] for row in batch["children"]] == ["naming"]
    body = (out_dir / "r7-name-a.md").read_text()
    assert "r7-naming-candidate-b64" in body
    assert "Matt Bodine" in body


def test_self_introduction_candidates_round_trip_as_reference_reviews(tmp_path, monkeypatch):
    """`self-introduction` rows live in `reference_candidates` beside `chair-reference` ones. When
    only the latter string was recognised, a self-introduction rendered as a shadow-match issue
    and then failed ingest against a ledger it was never in."""
    row = {
        "kind": "self-introduction",
        "candidate_id": "r7-ref-intro",
        "city_slug": "demo-tx",
        "body": "Council",
        "engine_recipe": "sherpa:v1",
        "capture_context": "chamber-v1",
        "episode_uid": "one",
        "start": 20.0,
        "end": 40.0,
        "display_name": "Matt Bodine",
        "cue_start": 20.2,
        "cue_end": 21.7,
        "cue_text": "Matt Bodine, Assistant Planner",
        "cue_kind": "name-then-title",
        "cluster": "staff",
    }
    batch, out_dir = _package(
        tmp_path, monkeypatch, _review_state(reference_candidates={row["candidate_id"]: row})
    )
    assert [child["kind"] for child in batch["children"]] == ["self-introduction"]
    body = (out_dir / "r7-ref-intro.md").read_text()
    assert "r7-reference-candidate-b64" in body
    assert "Approve as a golden voice reference" in body
    assert "The speaker introduces themselves as" in body


def test_a_naming_verdict_round_trips_into_the_precision_table(tmp_path, monkeypatch):
    """End-to-end for the feedback loop the whole adaptive gate rests on: package a naming
    candidate, rule on it, and have that ruling come back as evidence about its *combination*."""
    row = _naming_row("r7-name-a", signals=["self-introduction", "title-cue"])
    state = _review_state(naming_candidates={row["candidate_id"]: row})
    _, out_dir = _package(tmp_path, monkeypatch, state)

    issue = tmp_path / "issue.md"
    body = (out_dir / "r7-name-a.md").read_text().replace("- [ ] Correct", "- [x] Correct", 1)
    issue.write_text(body)
    monkeypatch.setattr("citypods.statesync.push_state", lambda *_args, **_kwargs: 0)
    assert (
        speaker_review_main(
            [
                "ingest",
                "--issue-number",
                "7",
                "--issue-body-file",
                str(issue),
                "--actor",
                "maintainer",
            ]
        )
        == 0
    )

    stored = json.loads((tmp_path / "state" / "evaluation.json").read_text())
    assert [entry["candidate_id"] for entry in stored["reviews"]] == ["r7-name-a"]
    table = PrecisionTable.from_evaluation(stored)
    assert table.verdicts("staff:self-introduction+title-cue") == 1
    assert table.precision("staff:self-introduction+title-cue") == 1.0


def test_review_queue_is_ordered_by_expected_value_not_candidate_id(tmp_path, monkeypatch):
    """The weekly limit is small, so this ordering -- not backlog size -- decides how fast the
    gate learns. References mint reusable voice profiles; naming verdicts train the table;
    shadow matches teach the least. Within a class, better-corroborated candidates come first."""
    batch, _ = _package(
        tmp_path,
        monkeypatch,
        _review_state(
            candidates={
                "aaa-shadow": {
                    "candidate_id": "aaa-shadow",
                    "city_slug": "demo-tx",
                    "body": "Council",
                    "episode_uid": "one",
                    "start": 1,
                    "end": 2,
                    "speaker_id": "spk-a",
                    "display_name": "Alex",
                }
            },
            naming_candidates={
                "zzz-weak": _naming_row("zzz-weak", signals=["self-introduction"]),
                "yyy-strong": _naming_row(
                    "yyy-strong", signals=["self-introduction", "title-cue", "voice-print"]
                ),
            },
            reference_candidates={
                "mmm-ref": {
                    "kind": "chair-reference",
                    "candidate_id": "mmm-ref",
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
        ),
    )
    assert [row["candidate_id"] for row in batch["children"]] == [
        "mmm-ref",  # reference first, despite sorting last by id
        "yyy-strong",  # then naming, most-corroborated first
        "zzz-weak",
        "aaa-shadow",  # shadow last, despite sorting first by id
    ]


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
    # `approved_names` is what the naming gate cleared for this cluster; publication requires the
    # voice match to land on one of them, so clearance for one person cannot publish another.
    turn = assign_turn(
        {"start": 10.0, "end": 25.0, "overlap": False},
        matches,
        publish=True,
        approved_names={"Alex Rivera"},
    )
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
        approved_names={"Alexandra Rivera"},
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


def test_calibration_cell_still_scopes_the_review_ledger():
    """The per-cell *publish* gate is gone (`citypods.naming` owns admission now, and the engine
    choice it once guarded is a single global decision). The cell survives as the reviewer-facing
    scope label on ledger rows, so it must stay stable and capture-context-aware."""
    cell = calibration_cell(
        "demo-tx", "Council", "pyannote-v1", capture_context="council-chamber-v1"
    )
    assert cell == "demo-tx|council|pyannote-v1|council-chamber-v1"
    with pytest.raises(ValueError):
        calibration_cell("demo-tx", "Council", "pyannote-v1", capture_context="  ")


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


def _identity_stage_case(tmp_path):
    """One episode carrying both automatic naming signals, wired for `SpeakerIdentityStage`.

    Returns `(city, episode, ctx)`. The signal producers must be reachable *through the stage*,
    not merely defined: `self_introduction_candidates` shipped unwired once already -- the
    function and its unit tests passed while nothing in the pipeline ever called it.
    """
    import json as _json

    from citypods.models import City, Episode
    from citypods.stages import StageContext
    from citypods.storage.local import LocalStorage

    city = City(
        slug="denton-tx-city-council",
        city_entity="denton-tx",
        provider="swagit",
        source={"list_url": "x", "body": "City Council"},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
    )
    storage = LocalStorage(root=tmp_path / "s", url_prefix="https://cdn")

    # One turn named by the chair, one where the speaker names themselves.
    turns = [
        {"start": 0.0, "end": 2.0, "cluster": "chair", "overlap": False},
        {
            "start": 2.1,
            "end": 9.0,
            "cluster": "jane",
            "overlap": False,
            "embedding": [0.1, 0.2],
            "transcript_text_hash": "h1",
        },
        {
            "start": 20.0,
            "end": 40.0,
            "cluster": "staff",
            "overlap": False,
            "embedding": [0.3, 0.4],
            "transcript_text_hash": "h2",
        },
    ]
    speakers_payload = {
        "schema": "2",
        "engine": "sherpa-onnx",
        "model": "m",
        "embedding_recipe": "nemo-titanet-small",
        "clusters": [],
        "turns": turns,
    }
    speakers_file = tmp_path / "speakers.json"
    speakers_file.write_text(_json.dumps(speakers_payload))
    storage.put_file("speakers/ep.json", speakers_file, "application/json")

    words = {
        "segments": [
            {
                "words": [
                    # chair names the next speaker
                    {"w": "The", "s": 0.0, "e": 0.2},
                    {"w": "chair", "s": 0.2, "e": 0.4},
                    {"w": "recognizes", "s": 0.4, "e": 0.8},
                    {"w": "Council", "s": 0.8, "e": 1.0},
                    {"w": "Member", "s": 1.0, "e": 1.2},
                    {"w": "Jane", "s": 1.2, "e": 1.5},
                    {"w": "Doe.", "s": 1.5, "e": 1.8},
                    # staff presenter names themselves at the top of their own turn
                    {"w": "Matt", "s": 20.2, "e": 20.5},
                    {"w": "Bodine,", "s": 20.5, "e": 20.9},
                    {"w": "Assistant", "s": 21.0, "e": 21.3},
                    {"w": "Planner.", "s": 21.3, "e": 21.7},
                ]
            }
        ]
    }
    words_file = tmp_path / "words.json"
    words_file.write_text(_json.dumps(words))
    storage.put_file("words/ep.json", words_file, "application/json")

    ep = Episode(
        guid="g",
        uid="uid-ep",
        title="M",
        published=datetime(2026, 5, 20, tzinfo=UTC),
        video_url="https://src/x.m3u8",
        media_kind="hls",
        body="City Council",
    )
    ep.speakers_key = "speakers/ep.json"
    ep.speakers_spec_hash = "spec"
    ep.transcript_words_key = "words/ep.json"

    ctx = StageContext(
        storage=storage,
        ffmpeg=None,
        max_kbps=96,
        dry_run=False,
        speaker_registry_path=tmp_path / "registry.json",
        speaker_evaluation_state_path=tmp_path / "evaluation.json",
        speaker_turn_evidence_path=tmp_path / "evidence.json",
    )
    ctx.speaker_config = {
        "enabled": True,
        "pilot_bodies": [
            {"city": "denton-tx", "body": "City Council", "capture_context": "council-v1"}
        ],
    }

    return city, ep, ctx


def _run_identity_stage(tmp_path, city, ep, ctx):
    import json as _json

    from citypods.stages import SpeakerIdentityStage, StageContext

    assert isinstance(ctx, StageContext)
    SpeakerIdentityStage().process(None, city, [ep], ctx)
    return _json.loads((tmp_path / "evaluation.json").read_text())


def _count_storage_reads(ctx, monkeypatch) -> list[str]:
    """Record every object this stage pulls, so a 'skip' can be shown to be a real skip."""
    reads: list[str] = []
    import citypods.stages as stages_mod

    original = stages_mod._read_storage_bytes

    def _tracked(storage, key):
        reads.append(key)
        return original(storage, key)

    monkeypatch.setattr(stages_mod, "_read_storage_bytes", _tracked)
    return reads


def test_second_run_skips_an_episode_whose_naming_inputs_have_not_moved(tmp_path, monkeypatch):
    """`speaker_identity` is always-revisit by design (a human decision must not wait on a media
    mutation), so the cost control has to be inside the loop: no I/O for episodes where nothing
    that feeds a naming decision changed -- the common case for a six-hourly cron."""
    city, ep, ctx = _identity_stage_case(tmp_path)
    _run_identity_stage(tmp_path, city, ep, ctx)

    reads = _count_storage_reads(ctx, monkeypatch)
    _run_identity_stage(tmp_path, city, ep, ctx)
    assert reads == []


def test_new_minutes_or_a_new_profile_reopen_a_skipped_episode(tmp_path, monkeypatch):
    """The skip must never outlive its inputs. Minutes landing weeks later, and a newly approved
    voice profile, are exactly the two events §C.5 exists to let through."""
    city, ep, ctx = _identity_stage_case(tmp_path)
    _run_identity_stage(tmp_path, city, ep, ctx)

    ep.minutes_roster = [{"name": "Jane Doe", "status": "present", "section": "members"}]
    reads = _count_storage_reads(ctx, monkeypatch)
    _run_identity_stage(tmp_path, city, ep, ctx)
    assert reads, "minutes arriving must reopen the episode"

    # Settled again...
    reads.clear()
    _run_identity_stage(tmp_path, city, ep, ctx)
    assert reads == []

    # ...until a reviewer approves a voice profile, which can match any episode in the backlog.
    registry = json.loads(ctx.speaker_registry_path.read_text())
    person = next(iter(registry["people"].values()))
    person["references"] = [{"embedding": [0.1, 0.2], "embedding_recipe": "nemo-titanet-small"}]
    ctx.speaker_registry_path.write_text(json.dumps(registry))
    reads.clear()
    _run_identity_stage(tmp_path, city, ep, ctx)
    assert reads, "a new voice profile must reopen every episode"


def test_a_lost_attribution_reopens_the_episode(tmp_path, monkeypatch):
    """The projection's only durable mutation is pull-quote attribution. If a record push is lost
    or rolled back, a fingerprint that outlived its own output would skip the episode forever."""
    city, ep, ctx = _identity_stage_case(tmp_path)
    ep.moment_pullquote_candidates = [{"candidate_id": "q1", "start": 22.0, "end": 30.0}]
    key = "staff:self-introduction+title-cue"
    ctx.speaker_evaluation_state_path.write_text(
        json.dumps(
            {
                "naming_candidates": {
                    f"seed-{index}": {"combination_key": key, "city_slug": "denton-tx"}
                    for index in range(20)
                },
                "reviews": [
                    {"candidate_id": f"seed-{index}", "correct": True} for index in range(20)
                ],
            }
        )
    )
    _run_identity_stage(tmp_path, city, ep, ctx)
    assert ep.moment_pullquote_candidates[0]["speaker_attribution"]["display_name"] == (
        "Matt Bodine"
    )

    ep.moment_pullquote_candidates[0].pop("speaker_attribution")  # a push that never landed
    reads = _count_storage_reads(ctx, monkeypatch)
    _run_identity_stage(tmp_path, city, ep, ctx)
    assert reads, "a missing attribution must reopen the episode"
    assert ep.moment_pullquote_candidates[0]["speaker_attribution"]["display_name"] == (
        "Matt Bodine"
    )


def test_body_membership_carries_the_roster_forward_scoped_to_its_own_body():
    """Minutes for a meeting land weeks after the recording, so a new episode has no roster at
    all. Standing membership fills that window -- but only from the same body."""
    registry = empty_registry()
    now = datetime(2026, 5, 1, tzinfo=UTC)
    observe_attendance(
        registry,
        city_slug="demo-tx",
        body="City Council",
        episode_uid="one",
        published=now,
        roster=[{"name": "Jane Doe"}, {"name": "Ann Chair"}],
    )
    observe_attendance(
        registry,
        city_slug="demo-tx",
        body="Board of Ethics",
        episode_uid="two",
        published=now,
        roster=[{"name": "Ethics Person"}],
    )
    refresh_membership_status(registry, now=now)
    council = {
        row["name"] for row in body_membership(registry, city_slug="demo-tx", body="City Council")
    }
    assert council == {"Jane Doe", "Ann Chair"}
    assert "Ethics Person" not in council


def test_body_membership_drops_people_who_stopped_appearing():
    """Turnover expires on its own -- no N to pick, and no election to special-case."""
    registry = empty_registry()
    observe_attendance(
        registry,
        city_slug="demo-tx",
        body="City Council",
        episode_uid="one",
        published=datetime(2024, 1, 1, tzinfo=UTC),
        roster=[{"name": "Former Member"}],
    )
    refresh_membership_status(registry, now=datetime(2026, 5, 1, tzinfo=UTC))
    assert body_membership(registry, city_slug="demo-tx", body="City Council") == []


def test_speaker_identity_stage_collects_both_automatic_naming_signals(tmp_path):
    evaluation = _run_identity_stage(tmp_path, *_identity_stage_case(tmp_path))
    kinds = {row["kind"] for row in evaluation["reference_candidates"].values()}
    names = {row["display_name"] for row in evaluation["reference_candidates"].values()}
    assert kinds == {"chair-reference", "self-introduction"}
    assert names == {"Jane Doe", "Matt Bodine"}
    # Still evidence only -- neither signal may assign a name to a turn.
    assert all(
        row.get("status") != "confirmed" for row in evaluation["reference_candidates"].values()
    )


def test_speaker_identity_stage_ledgers_fused_candidates_with_their_combination(tmp_path):
    """The gate has to be reachable through the stage too, and each review-bound candidate must
    carry the `combination_key` that a later verdict feeds back into the precision table -- that
    join is the whole adaptive loop."""
    evaluation = _run_identity_stage(tmp_path, *_identity_stage_case(tmp_path))
    by_name = {row["display_name"]: row for row in evaluation["naming_candidates"].values()}
    assert by_name["Matt Bodine"]["tier"] == "staff"
    assert by_name["Matt Bodine"]["combination_key"] == "staff:self-introduction+title-cue"
    assert by_name["Matt Bodine"]["reason"] == "combination-untrusted"  # fail-closed cold start
    # Jane is recognised by the chair with an elected title, which tiers her as a member -- but a
    # title cue is not a countable second signal for members, and with no minutes roster and no
    # voice print yet she has only one. She stays out of the *naming* queue (nothing there for a
    # human to rule on that would teach the table anything) while remaining in the existing
    # golden-reference queue, where approving her creates the voice profile she is missing.
    assert "Jane Doe" not in by_name
    assert any(
        row["display_name"] == "Jane Doe" for row in evaluation["reference_candidates"].values()
    )


def test_speaker_identity_stage_names_staff_once_the_combination_is_trusted(tmp_path):
    """A staff presenter has no voice profile and never will from one meeting, so a trusted
    combination has to name the cluster directly -- otherwise "staff are named automatically"
    silently means "staff are named after a human approves a reference"."""
    import json as _json

    city, ep, ctx = _identity_stage_case(tmp_path)
    # A quote wholly inside the staff turn: attribution is how a published name actually reaches
    # the world, so assert there rather than on an intermediate.
    ep.moment_pullquote_candidates = [
        {"candidate_id": "q1", "start": 22.0, "end": 30.0},
        {"candidate_id": "q2", "start": 3.0, "end": 8.0},
    ]
    key = "staff:self-introduction+title-cue"
    ctx.speaker_evaluation_state_path.write_text(
        _json.dumps(
            {
                "naming_candidates": {
                    f"seed-{index}": {"combination_key": key, "city_slug": "denton-tx"}
                    for index in range(20)
                },
                "reviews": [
                    {"candidate_id": f"seed-{index}", "correct": True} for index in range(20)
                ],
            }
        )
    )
    _run_identity_stage(tmp_path, city, ep, ctx)

    staff_quote, jane_quote = ep.moment_pullquote_candidates
    assert staff_quote["speaker_attribution"]["display_name"] == "Matt Bodine"
    assert staff_quote["speaker_attribution"]["method"] == "cue-fusion"
    # Jane is a member: no amount of signal agreement names her without a human.
    assert "speaker_attribution" not in jane_quote


def test_a_roster_sharing_nobody_with_prior_meetings_is_flagged(tmp_path, capsys):
    """Name-shape validation rejects "a quorum was established", but not a well-formed name
    lifted from the wrong part of the document -- and a plausible-but-wrong roster still narrows
    `allowed_ids`. A real body does not replace its whole membership between meetings, so a
    disjoint roster is the signature of a parse that succeeded on the wrong text."""
    city, ep, ctx = _identity_stage_case(tmp_path)
    ep.minutes_roster = [{"name": "Jane Doe"}, {"name": "Bob Chair"}]
    _run_identity_stage(tmp_path, city, ep, ctx)  # establishes the membership
    capsys.readouterr()

    ep.uid = "uid-ep2"
    ep.minutes_roster = [{"name": "Wrong Person"}, {"name": "Other Wrong"}]
    stats = SpeakerIdentityStage().process(None, city, [ep], ctx)
    assert stats.quality_counts.get("minutes-roster-disjoint-from-membership") == 1
    assert "shares no member with prior meetings" in capsys.readouterr().out


def test_a_first_ever_roster_is_not_flagged_as_disjoint(tmp_path):
    """Onboarding a new body has no established membership to be disjoint from; flagging it would
    make the signal fire loudest exactly when new cities are added."""
    city, ep, ctx = _identity_stage_case(tmp_path)
    ep.minutes_roster = [{"name": "Jane Doe"}]
    stats = SpeakerIdentityStage().process(None, city, [ep], ctx)
    assert "minutes-roster-disjoint-from-membership" not in stats.quality_counts


def test_a_roster_overlapping_prior_meetings_is_not_flagged(tmp_path):
    """Ordinary turnover keeps most of the body; only a *total* replacement is suspicious."""
    city, ep, ctx = _identity_stage_case(tmp_path)
    ep.minutes_roster = [{"name": "Jane Doe"}, {"name": "Bob Chair"}]
    _run_identity_stage(tmp_path, city, ep, ctx)

    ep.uid = "uid-ep2"
    ep.minutes_roster = [{"name": "Jane Doe"}, {"name": "New Member"}]
    stats = SpeakerIdentityStage().process(None, city, [ep], ctx)
    assert "minutes-roster-disjoint-from-membership" not in stats.quality_counts


def test_a_misspelled_cue_name_fuses_with_its_official_roster_spelling(tmp_path):
    """The correction path this project assumed it had. `fuse_proposals` groups on the normalized
    name, so a cue heard as "Gerrard Hudspeth" and a roster reading "Gerard Hudspeth" were two
    candidates carrying one signal each -- both failing the agreement rule, so the member was
    silently never named and the minutes could not correct a spelling they never met."""
    city, ep, ctx = _identity_stage_case(tmp_path)
    # The stage's fixture words say "Council Member Jane Doe"; the official roster carries a
    # doubled-letter variant, the shape OCR and ASR both routinely produce on civic surnames.
    ep.minutes_roster = [{"name": "Jane Doee", "status": "present", "section": "members"}]
    evaluation = _run_identity_stage(tmp_path, city, ep, ctx)
    rows = {row["display_name"]: row for row in evaluation["naming_candidates"].values()}
    # One candidate, under the *official* spelling, carrying both signals -- not two orphans that
    # each fail the agreement rule.
    assert "Jane Doee" in rows
    assert set(rows["Jane Doee"]["signals"]) >= {"chair-reference", "roster"}
    assert "Jane Doe" not in rows


def test_canonicalization_never_collapses_two_similar_officials():
    """Two officials on one body must never be merged into each other -- the misattribution this
    whole mechanism has to avoid buying."""
    from citypods.speakers import canonical_name

    roster = ["John Smith", "Jane Smith"]
    assert canonical_name("John Smith", roster) == "John Smith"
    assert canonical_name("Jane Smith", roster) == "Jane Smith"
    assert canonical_name("Totally Different", roster) == "Totally Different"
    # A realistic ASR variant still resolves to the one official spelling it is close to.
    assert canonical_name("Gerrard Hudspeth", ["Gerard Hudspeth"]) == "Gerard Hudspeth"


def test_canonicalization_leaves_a_spelling_it_cannot_safely_merge():
    """Known limitations, all failing in the safe direction -- a review item, never a wrong name.
    Homophones diverge too far to score as one name, and short names carry so little signal per
    character that a single letter costs more ratio than the threshold allows."""
    from citypods.speakers import canonical_name

    assert canonical_name("Vicky Bird", ["Vicki Byrd"]) == "Vicky Bird"
    assert canonical_name("Jane Doh", ["Jane Doe"]) == "Jane Doh"


def _spoken(text: str) -> dict:
    words, clock = [], 0.0
    for word in text.split():
        words.append({"w": word, "s": clock, "e": clock + 0.2})
        clock += 0.25
    return {"segments": [{"words": words}]}


_CUE_TURNS = [
    {"start": 0.0, "end": 5.0, "cluster": "chair", "overlap": False},
    {
        "start": 5.1,
        "end": 9.0,
        "cluster": "next",
        "overlap": False,
        "embedding": [0.1],
        "transcript_text_hash": "h",
    },
]


@pytest.mark.parametrize(
    "text",
    [
        "Councilor Jane Doe.",
        "Council Person Jane Doe.",
        "Councilperson Jane Doe.",
        "Alderman Jane Doe.",
        "Alderperson Jane Doe.",
        "Commissioner Jane Doe.",
        "Selectman Jane Doe.",
        "Freeholder Jane Doe.",
        "Board Member Jane Doe.",
        "Vice Chair Jane Doe.",
    ],
)
def test_title_announcements_cover_the_common_civic_office_names(text):
    """Councils, commissions, boards, New England selectboards and NJ freeholder boards all name
    the same role differently; covering only "Council Member" would name members in one
    convention and silently skip every other."""
    found = chair_reference_candidates(_spoken(text), _CUE_TURNS)
    assert [row["display_name"] for row in found] == ["Jane Doe"]
    assert found[0]["title_cue"] == "title-announcement"


@pytest.mark.parametrize(
    "text",
    [
        "The chair recognizes Jane Doe.",
        "The mayor recognizes Jane Doe.",
        "The Mayor Pro Tem recognizes Jane Doe.",
        "The vice mayor recognizes Jane Doe.",
        "The presiding officer recognizes Jane Doe.",
        "Chairman Smith calls on Jane Doe.",
        "The president yields to Jane Doe.",
        "I now recognize Jane Doe.",
    ],
)
def test_recognition_cues_cover_whoever_is_presiding(text):
    """Only "the chair recognizes" was covered, so a mayor-led council -- the common arrangement
    -- produced no recognition cue at all. Mayor Pro Tem is its own office, the member who
    presides in the mayor's absence, so matching only "mayor" misses exactly those meetings."""
    found = chair_reference_candidates(_spoken(text), _CUE_TURNS)
    assert [row["display_name"] for row in found] == ["Jane Doe"]


def test_a_title_before_a_recognition_verb_never_becomes_a_name():
    """Two ways this misfires, both live before the fix: the title branch would start scanning at
    the verb and yield "recognizes Jane Doe", and a presider naming themselves ("Chairman Smith
    calls on...") would be proposed as the name for the *next* speaker's turn."""
    assert [
        row["display_name"]
        for row in chair_reference_candidates(_spoken("The mayor recognizes Jane Doe."), _CUE_TURNS)
    ] == ["Jane Doe"]
    found = chair_reference_candidates(
        _spoken("Chairman Smith calls on Councilor Jane Doe."), _CUE_TURNS
    )
    assert [row["display_name"] for row in found] == ["Jane Doe"]


def test_a_multi_word_office_is_not_split_by_its_shorter_prefix():
    """ "Mayor Pro Tem" is its own office, not a qualified "Mayor" -- so it must consume all three
    tokens. Matching ("mayor",) as well would emit a second candidate named "Pro Tem Brian Beck"
    that dedup cannot collapse, because the two names differ."""
    found = chair_reference_candidates(_spoken("Mayor Pro Tem Brian Beck."), _CUE_TURNS)
    assert [row["display_name"] for row in found] == ["Brian Beck"]


def test_a_minutes_spelling_change_does_not_orphan_an_approved_profile(tmp_path):
    """Canonicalization rewrites a candidate to the *official* spelling, so every comparison made
    against a stored name has to be canonicalized on the same basis. Otherwise a member whose
    registry entry predates a minutes spelling change fails the established check forever --
    silently, and despite holding an approved voice profile."""
    from citypods.naming import (
        SIGNAL_CHAIR_CUE,
        SIGNAL_ROSTER,
        FusedCandidate,
        PrecisionTable,
        decide,
    )
    from citypods.speakers import TIER_MEMBER, canonical_name

    table = PrecisionTable()
    candidate = FusedCandidate(
        "c1", "Gerard Hudspeth", TIER_MEMBER, (SIGNAL_CHAIR_CUE, SIGNAL_ROSTER)
    )
    for _ in range(20):
        table.record(candidate.combination_key, city_slug="denton-tx", agreed=True)

    stored = ["Gerrard Hudspeth"]  # registry spelling, from before the minutes were corrected
    raw = decide(candidate, table, city_slug="denton-tx", body="Council", confirmed_names=stored)
    assert raw.reason == "member-awaiting-confirmation"  # the defect, if left uncanonicalized

    official = [canonical_name(name, ["Gerard Hudspeth"]) for name in stored]
    fixed = decide(
        candidate, table, city_slug="denton-tx", body="Council", confirmed_names=official
    )
    assert fixed.publish
    assert fixed.reason == "member-established"


def test_stage_emits_membership_not_roster_when_minutes_are_pending(tmp_path):
    """Provenance through the stage, not just in a hand-built candidate: standing membership must
    arrive as SIGNAL_MEMBERSHIP so it lands in its own precision bucket, and a published roster
    must suppress it rather than stacking a second untimed signal on the same claim."""
    city, ep, ctx = _identity_stage_case(tmp_path)
    ep.minutes_roster = [{"name": "Jane Doe", "section": "members"}]
    _run_identity_stage(tmp_path, city, ep, ctx)  # establishes standing membership

    ep.uid, ep.minutes_roster = "uid-ep2", []  # next meeting: minutes weeks away
    evaluation = _run_identity_stage(tmp_path, city, ep, ctx)
    jane = next(
        row
        for row in evaluation["naming_candidates"].values()
        if row["display_name"] == "Jane Doe" and row["episode_uid"] == "uid-ep2"
    )
    assert "membership" in jane["signals"]
    assert "roster" not in jane["signals"]

    ep.uid, ep.minutes_roster = "uid-ep3", [{"name": "Jane Doe", "section": "members"}]
    evaluation = _run_identity_stage(tmp_path, city, ep, ctx)
    jane = next(
        row
        for row in evaluation["naming_candidates"].values()
        if row["display_name"] == "Jane Doe" and row["episode_uid"] == "uid-ep3"
    )
    assert "roster" in jane["signals"]
    assert "membership" not in jane["signals"]


def test_stage_canonicalizes_registry_names_before_the_established_check(tmp_path):
    """Exercises the *stage's* canonicalization of active registry names, which a test that
    canonicalizes its own input would not: delete that call and this fails. A member holding an
    approved profile under an older spelling must still publish once the minutes correct it."""
    import json as _json

    city, ep, ctx = _identity_stage_case(tmp_path)
    ep.minutes_roster = [{"name": "Jane Doee", "section": "members"}]

    # An approved voice profile stored under the pre-correction spelling.
    from citypods.speakers import body_key, speaker_id

    ident = speaker_id("denton-tx", "City Council", "Jane Doe")
    ctx.speaker_registry_path.write_text(
        _json.dumps(
            {
                "version": 1,
                "people": {
                    ident: {
                        "speaker_id": ident,
                        "display_name": "Jane Doe",
                        "aliases": [],
                        "body_key": body_key("denton-tx", "City Council"),
                        "membership": {
                            "first_seen": "2026-01-01T00:00:00+00:00",
                            # `refresh_membership_status` runs first and demotes anyone with no
                            # `last_seen` to review_only, which would drop them from
                            # `confirmed_names` before the check under test is reached.
                            "last_seen": datetime.now(UTC).isoformat(),
                        },
                        "references": [{"embedding": [0.1], "embedding_recipe": "x"}],
                        "status": "active",
                    }
                },
                "history": [],
            }
        )
    )
    # Trust the combination the roster+cue pair produces, so only the person check is in play.
    key = "member:chair-reference+roster+title-cue"
    ctx.speaker_evaluation_state_path.write_text(
        _json.dumps(
            {
                "naming_candidates": {
                    f"seed-{i}": {"combination_key": key, "city_slug": "denton-tx"}
                    for i in range(20)
                },
                "reviews": [{"candidate_id": f"seed-{i}", "correct": True} for i in range(20)],
            }
        )
    )
    evaluation = _run_identity_stage(tmp_path, city, ep, ctx)
    # Published, so it never reaches the review ledger under the corrected spelling.
    # Positive first: the stage must actually have produced output, or the negative below is
    # satisfied by the seeded rows alone and would pass even if the stage did nothing.
    produced = {
        row.get("display_name")
        for row in (evaluation.get("naming_candidates") or {}).values()
        if row.get("display_name")
    }
    assert "Matt Bodine" in produced
    # ...and the established member published, so she never reached the review ledger.
    assert "Jane Doee" not in produced


def test_clearance_for_one_name_cannot_publish_another():
    """The gate approves a (cluster, name) pair, but `assign_turn` picks its own best voice
    match. Applying a bare "this cluster was approved" flag to whatever that match returned let
    clearance earned by one person publish someone else entirely."""
    matches = [{"speaker_id": "spk-x", "display_name": "Wrong Person", "score": 0.99}]
    turn = assign_turn(
        {"start": 1.0, "end": 2.0}, matches, publish=True, approved_names={"Matt Bodine"}
    )
    # Still recorded -- that is what puts the disagreement in front of a reviewer -- but not named.
    assert turn["identity"]["display_name"] == "Wrong Person"
    assert turn["identity"]["status"] == "shadow"

    agreeing = [{"speaker_id": "spk-m", "display_name": "Matt Bodine", "score": 0.99}]
    assert (
        assign_turn(
            {"start": 1.0, "end": 2.0}, agreeing, publish=True, approved_names={"Matt Bodine"}
        )["identity"]["status"]
        == "provisional"
    )


def test_no_approval_information_publishes_nothing():
    matches = [{"speaker_id": "spk-x", "display_name": "Anyone", "score": 0.99}]
    turn = assign_turn({"start": 1.0, "end": 2.0}, matches, publish=True, approved_names=None)
    assert turn["identity"]["status"] == "shadow"


def test_a_conflicting_voice_match_cannot_borrow_a_cleared_name(tmp_path):
    """Finding 1, end to end: the gate clears (cluster, name) pairs, but `assign_turn` picks its
    own best voice match. Reduced to a per-cluster boolean, clearance earned by Matt Bodine's
    introduction authorized publishing whoever the voice print ranked first."""
    import json as _json

    from citypods.speakers import body_key, speaker_id

    city, ep, ctx = _identity_stage_case(tmp_path)
    ep.moment_pullquote_candidates = [{"candidate_id": "q1", "start": 22.0, "end": 30.0}]

    # A profile for a *different* person that the staff cluster's embedding matches.
    ident = speaker_id("denton-tx", "City Council", "Wrong Person")
    ctx.speaker_registry_path.write_text(
        _json.dumps(
            {
                "version": 1,
                "people": {
                    ident: {
                        "speaker_id": ident,
                        "display_name": "Wrong Person",
                        "aliases": [],
                        "body_key": body_key("denton-tx", "City Council"),
                        "membership": {
                            "first_seen": "2026-01-01T00:00:00+00:00",
                            "last_seen": datetime.now(UTC).isoformat(),
                        },
                        # Two distinct meetings, or `qualified_profile` refuses to match at all
                        # and no conflict would occur.
                        "references": [
                            {
                                "episode_uid": "m1",
                                "embedding": [0.3, 0.4],
                                "embedding_recipe": "nemo-titanet-small",
                            },
                            {
                                "episode_uid": "m2",
                                "embedding": [0.3, 0.4],
                                "embedding_recipe": "nemo-titanet-small",
                            },
                        ],
                        "status": "active",
                    }
                },
                "history": [],
            }
        )
    )
    key = "staff:self-introduction+title-cue"
    ctx.speaker_evaluation_state_path.write_text(
        _json.dumps(
            {
                "naming_candidates": {
                    f"seed-{i}": {"combination_key": key, "city_slug": "denton-tx"}
                    for i in range(20)
                },
                "reviews": [{"candidate_id": f"seed-{i}", "correct": True} for i in range(20)],
            }
        )
    )
    _run_identity_stage(tmp_path, city, ep, ctx)

    # The voice print disagrees with the introduction that earned the clearance. Whatever else
    # happens, the *unapproved* name must never be published: naming the wrong official is worse
    # than naming nobody.
    attribution = ep.moment_pullquote_candidates[0].get("speaker_attribution") or {}
    assert attribution.get("display_name") != "Wrong Person"


def test_another_bodys_profile_cannot_confirm_a_local_member(tmp_path):
    """Finding 9: voice matching was body-scoped but member confirmation and id resolution were
    not, so an approved "Jane Doe" on another city's Board of Ethics could satisfy the local
    confirmation check and lend her opaque id to the published attribution."""
    from citypods.naming import (
        SIGNAL_CHAIR_CUE,
        SIGNAL_ROSTER,
        FusedCandidate,
        PrecisionTable,
        decide,
    )
    from citypods.speakers import TIER_MEMBER

    table = PrecisionTable()
    candidate = FusedCandidate("c1", "Jane Doe", TIER_MEMBER, (SIGNAL_CHAIR_CUE, SIGNAL_ROSTER))
    for _ in range(20):
        table.record(candidate.combination_key, city_slug="denton-tx", agreed=True)

    # A foreign Jane Doe must not satisfy the local check -- the stage now filters
    # `confirmed_names` by body_key, so she never reaches `decide` at all.
    assert (
        decide(candidate, table, city_slug="denton-tx", body="City Council").reason
        == "member-awaiting-confirmation"
    )
    assert decide(
        candidate,
        table,
        city_slug="denton-tx",
        body="City Council",
        confirmed_names=["Jane Doe"],
    ).publish


def test_a_reprojected_candidate_still_accepts_its_verdict(tmp_path, monkeypatch):
    """Finding 2's real consequence. `naming_candidate_id` excludes the signal set on purpose, so
    a projection between packaging and ingest can change a candidate's combination under the same
    id. Verifying that field against the ledger rejected the human's ruling as tampering, and
    snapshotting from the ledger recorded a combination they never saw. The payload the reviewer
    judged is the only thing that answers either question."""
    import json as _json

    row = _naming_row("r7-name-a", signals=["self-introduction", "title-cue"])
    state = _review_state(naming_candidates={row["candidate_id"]: row})
    _, out_dir = _package(tmp_path, monkeypatch, state)

    # A later projection finds a voice print too: same candidate id, different combination.
    reprojected = dict(row, signals=[*row["signals"], "voice-print"])
    reprojected["combination_key"] = "staff:self-introduction+title-cue+voice-print"
    state["naming_candidates"][row["candidate_id"]] = reprojected
    (tmp_path / "state" / "evaluation.json").write_text(_json.dumps(state))

    issue = tmp_path / "issue.md"
    issue.write_text(
        (out_dir / "r7-name-a.md").read_text().replace("- [ ] Correct", "- [x] Correct", 1)
    )
    monkeypatch.setattr("citypods.statesync.push_state", lambda *_a, **_k: 0)
    # The issue body is editable; a re-projected combination must not be allowed to carry
    # attacker-controlled calibration fields into the private evaluation ledger.
    with pytest.raises(ValueError, match="payload differs"):
        speaker_review_main(
            ["ingest", "--issue-number", "7", "--issue-body-file", str(issue), "--actor", "m"]
        )


def test_naming_reviews_get_reserved_capacity_against_a_reference_backlog(tmp_path, monkeypatch):
    """References rank first because one approval mints a reusable voice profile -- but *only*
    naming verdicts populate the precision table, so a steady arrival of more references than the
    weekly limit would fill every batch and no combination could ever become trusted. The ranking
    would then be self-defeating: approved profiles cannot publish anything while the combination
    they would publish under stays untrusted."""
    from collections import Counter

    references = {
        f"ref-{i}": {
            "kind": "self-introduction",
            "candidate_id": f"ref-{i}",
            "city_slug": "demo-tx",
            "body": "Council",
            "engine_recipe": "sherpa:v1",
            "capture_context": "chamber-v1",
            "episode_uid": "one",
            "start": 1.0,
            "end": 2.0,
            "display_name": f"Person {i}",
            "cue_start": 1.0,
            "cue_end": 1.5,
            "cue_text": "text",
            "cue_kind": "name-then-title",
            "cluster": "c",
        }
        for i in range(12)
    }
    naming = {
        f"name-{i}": _naming_row(f"name-{i}", signals=["self-introduction", "title-cue"])
        for i in range(6)
    }
    batch, _ = _package(
        tmp_path,
        monkeypatch,
        _review_state(reference_candidates=references, naming_candidates=naming),
    )
    kinds = Counter(row["kind"] for row in batch["children"])
    assert sum(kinds.values()) == 8
    assert kinds["naming"] == 4  # half the batch, held against a backlog twice the limit
    assert kinds["self-introduction"] == 4


def test_a_reference_only_queue_still_fills_the_batch():
    """The reserve must not idle capacity when there is nothing to reserve it for."""
    from citypods.speaker_review import _select_batch

    references = [{"kind": "chair-reference", "candidate_id": f"r{i}"} for i in range(12)]
    assert len(_select_batch(references, 8)) == 8
