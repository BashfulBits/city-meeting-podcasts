from datetime import UTC, datetime, timedelta

from citypods.agenda_text import parse_roster, parse_votes
from citypods.models import Episode
from citypods.records import episode_to_record, merge_persisted, record_to_episode
from citypods.stages import _previous_same_body


def test_minutes_parser_keeps_explicit_member_votes_and_roster():
    text = "Present: Alice Smith, Bob Jones\nVote: Yes: Alice Smith; No: Bob Jones"
    roster = parse_roster(text)
    votes = parse_votes(text, roster=roster)
    assert {row["name"] for row in roster} == {"Alice Smith", "Bob Jones"}
    assert {(row["member"], row["value"]) for row in votes} == {
        ("Alice Smith", "yes"),
        ("Bob Jones", "no"),
    }


def test_agenda_minutes_can_target_previous_same_body_only():
    older = Episode("old", "Older", datetime.now(UTC) - timedelta(days=7), "video", body="Council")
    newer = Episode("new", "Newer", datetime.now(UTC), "video", body="Council")
    other = Episode("other", "Other", datetime.now(UTC), "video", body="Planning")
    assert _previous_same_body(newer, [older, newer, other]) is older


def test_record_round_trip_preserves_document_artifacts():
    episode = Episode("g", "Meeting", datetime.now(UTC), "video", body="Council")
    episode.minutes_text_url = "https://example.test/minutes.pdf"
    episode.minutes_votes = [{"member": "Alice Smith", "value": "yes"}]
    episode.minutes_roster = [{"name": "Alice Smith", "status": "present"}]
    restored = record_to_episode(episode_to_record(episode))
    assert restored.minutes_text_url == episode.minutes_text_url
    assert restored.minutes_votes == episode.minutes_votes
    assert restored.minutes_roster == episode.minutes_roster


def test_provider_minutes_link_overrides_persisted_agenda_derived_link():
    persisted = Episode("g", "Meeting", datetime.now(UTC), "video", body="Council")
    persisted.links = {
        "minutes": "https://example.test/agenda-derived.pdf",
        "minutes_source": "agenda_link",
    }
    fresh = Episode("g", "Meeting", persisted.published, "video", body="Council")
    fresh.uid = persisted.uid = "uid"
    fresh.links = {"minutes": "https://example.test/provider-minutes.pdf"}
    merge_persisted([fresh], {"uid": episode_to_record(persisted)})
    assert fresh.links["minutes"] == "https://example.test/provider-minutes.pdf"
    assert "minutes_source" not in fresh.links
