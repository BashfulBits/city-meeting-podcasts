from datetime import UTC, datetime, timedelta

from citypods.agenda_text import (
    attribute_links_to_chapters,
    extract_agenda_text,
    parse_roster,
    parse_votes,
)
from citypods.models import Episode
from citypods.records import (
    agenda_text_backoff_until,
    episode_to_record,
    merge_persisted,
    record_to_episode,
)
from citypods.stages import _previous_same_body


def test_minutes_parser_keeps_explicit_member_votes_and_roster():
    text = "Present: Alice Smith, Bob Jones\nVote: Yes: Alice Smith, Carol Outsider; No: Bob Jones"
    roster = parse_roster(text)
    votes = parse_votes(text, roster=roster)
    assert {row["name"] for row in roster} == {"Alice Smith", "Bob Jones"}
    assert {(row["member"], row["value"]) for row in votes} == {
        ("Alice Smith", "yes"),
        ("Bob Jones", "no"),
    }
    assert all(row["member"] != "Carol Outsider" for row in votes)


def test_agenda_minutes_can_target_previous_same_body_only():
    older = Episode("old", "Older", datetime.now(UTC) - timedelta(days=7), "video", body="Council")
    newer = Episode("new", "Newer", datetime.now(UTC), "video", body="Council")
    other = Episode("other", "Other", datetime.now(UTC), "video", body="Planning")
    assert _previous_same_body(newer, [older, newer, other]) is older
    same_day_a = Episode(
        "same-a", "Earlier", newer.published - timedelta(hours=2), "video", body="Council"
    )
    same_day_b = Episode(
        "same-b", "Later", newer.published - timedelta(hours=1), "video", body="Council"
    )
    assert _previous_same_body(newer, [same_day_a, same_day_b, newer]) is None
    no_body_old = Episode("none-old", "Older", older.published, "video")
    no_body_new = Episode("none-new", "Newer", newer.published, "video")
    assert _previous_same_body(no_body_new, [no_body_old, no_body_new]) is None


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


def test_portal_is_preferred_and_noise_is_removed():
    class Response:
        headers = {"Content-Type": "text/html"}
        content = (
            b"<nav>noise</nav><main>Official agenda text for council</main><script>bad</script>"
        )

        def raise_for_status(self):
            return None

    class Session:
        def __init__(self):
            self.urls = []

        def get(self, url, timeout):
            self.urls.append(url)
            return Response()

    session = Session()
    text = extract_agenda_text(
        "https://example.test/agenda.pdf", "https://example.test/portal", session
    )
    assert "Official agenda text" in text
    assert session.urls == ["https://example.test/portal"]


def test_link_attribution_is_bounded_and_chapter_aware():
    result = attribute_links_to_chapters(
        [(1, "https://example.test/a.pdf"), (8, "https://example.test/b.pdf")],
        [{"title": "Item A"}, {"title": "Item B"}],
        10,
    )
    assert [row[0] for row in result] == [0, 1]


def test_agenda_text_backoff_is_nonzero_after_failure():
    episode = Episode("g", "Meeting", datetime.now(UTC), "video")
    episode.agenda_text_attempts = 1
    episode.agenda_text_last_attempt = datetime.now(UTC).isoformat()
    assert agenda_text_backoff_until(episode) is not None
