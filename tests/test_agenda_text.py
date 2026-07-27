from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.agenda_text import (
    _extract_pdf,
    attribute_links_by_content,
    attribute_links_to_chapters,
    chapter_text_matches,
    extract_agenda_text,
    extract_html,
    item_identifiers,
    parse_roster,
    parse_votes,
    resolve_chapter_spans,
)
from citypods.models import Episode
from citypods.records import (
    agenda_text_backoff_until,
    episode_to_record,
    merge_persisted,
    record_to_episode,
)
from citypods.stages import _previous_same_body

FIXTURES = Path(__file__).parent / "fixtures" / "agenda_text"


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
    # Midday keeps the same-day fixtures on the same calendar date in every CI timezone/run time.
    newer_at = datetime(2026, 1, 15, 12, tzinfo=UTC)
    older = Episode("old", "Older", newer_at - timedelta(days=7), "video", body="Council")
    newer = Episode("new", "Newer", newer_at, "video", body="Council")
    other = Episode("other", "Other", newer_at, "video", body="Planning")
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


def test_extract_html_no_longer_gates_backup_links_on_english_keywords():
    """Regression guard for the fix: a link with no "agenda/packet/backup/attachment/supporting"
    keyword in its label or href (e.g. a bare Legistar-style "File #" link) must still be
    discovered -- different cities' agenda platforms label these links differently, or not with
    words at all, and gating on a fixed English keyword set silently dropped them."""
    html = (
        b"<html><body>"
        b'<a href="https://example.test/LegislationDetail.aspx?ID=123">2026-0071</a>'
        b"</body></html>"
    )
    _, links = extract_html(html, source_url="https://example.test/MeetingDetail.aspx")
    assert any(link.url.endswith("ID=123") for link in links)


# Real, currently-live agenda PDFs fetched during investigation (public government records):
# the Arlington Planning & Zoning Commission agenda that produced the real GH #1057/#1068
# zoning-reform/neighborhood-engagement false positives, and a real Legistar attachment PDF
# (Pflugerville). Used here instead of synthetic fixtures so this logic is validated against
# actual production documents, not a hand-built approximation.
_ARLINGTON_CHAPTER_TITLES = [
    "I. Call to Order",
    "II. Approval of Minutes",
    "III.A Zoning Case PD20-25",
    "III.B Specific Use Permit SUP20-6",
    "III.C Zoning Case ZA20-8",
    "IV. Miscellaneous",
]


def _load_arlington_agenda():
    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    return _extract_pdf(content)


def test_item_identifiers_extracts_real_zoning_case_numbers():
    assert item_identifiers("III.A Zoning Case PD20-25") == ["PD20-25"]
    assert item_identifiers("III.B Specific Use Permit SUP20-6") == ["SUP20-6"]


def test_chapter_text_matches_real_backup_filenames_by_case_identifier():
    # Real backup-document filenames from the fixture agenda embed the case number.
    assert chapter_text_matches(
        "FINAL_PD20-25_The_Mark_at_Arlington_Staff_Report.pdf", "III.A Zoning Case PD20-25"
    )
    assert not chapter_text_matches("FINAL_SUP20-6_Staff_Report.pdf", "III.A Zoning Case PD20-25")


def test_chapter_text_matches_does_not_let_a_shorter_case_number_match_a_longer_one():
    """CodeRabbit regression: raw substring containment let "PD20-2" match inside the unrelated,
    longer case number "PD20-25" (one case number is a prefix of another). The match must be
    boundary-aware."""
    assert not chapter_text_matches(
        "FINAL_PD20-25_The_Mark_at_Arlington_Staff_Report.pdf", "III.A Zoning Case PD20-2"
    )
    assert chapter_text_matches(
        "FINAL_PD20-25_The_Mark_at_Arlington_Staff_Report.pdf", "III.A Zoning Case PD20-25"
    )


def test_attribute_links_by_content_on_real_granicus_agenda():
    """Validates chapter attribution against the actual real agenda that produced GH #1057 --
    each zoning case's real staff-report/case-info backup documents resolve to that case's own
    chapter; documents with no case number in their filename (e.g. a generic location map) are
    left unattributed rather than guessed."""
    _, links = _load_arlington_agenda()
    chapters = [{"title": title} for title in _ARLINGTON_CHAPTER_TITLES]
    attributed = attribute_links_by_content(links, chapters)
    by_filename = {link.url.rsplit("/", 1)[-1]: (index, title) for index, title, link in attributed}
    assert by_filename["FINAL_PD20-25_The_Mark_at_Arlington_Staff_Report.pdf"] == (
        2,
        "III.A Zoning Case PD20-25",
    )
    assert by_filename["PD20-25_Case_Info.pdf"] == (2, "III.A Zoning Case PD20-25")
    assert by_filename["FINAL_SUP20-6_Staff_Report.pdf"] == (
        3,
        "III.B Specific Use Permit SUP20-6",
    )
    assert by_filename["Final_Staff_Report_ZA20-8.pdf"] == (4, "III.C Zoning Case ZA20-8")
    # A generic, identifier-less filename is not guessed into any chapter.
    assert by_filename["ZONING_LOCATION_MAP.pdf"] == (None, None)


def test_resolve_chapter_spans_excludes_boilerplate_preamble_on_real_agenda():
    """Validates the boilerplate-stripping premise (4c) against the real document behind GH
    #1068: the standard hearing sign-up instructions sit before the first resolved chapter title
    and nowhere else, so slicing on chapter-title position drops exactly that text."""
    text, _ = _load_arlington_agenda()
    norm_text, spans = resolve_chapter_spans(text, _ARLINGTON_CHAPTER_TITLES)
    assert [span is not None for span in spans] == [True] * len(_ARLINGTON_CHAPTER_TITLES)
    # Spans are ordered and non-overlapping.
    starts = [span[0] for span in spans]
    assert starts == sorted(starts)
    for span, next_span in zip(spans, spans[1:], strict=False):
        assert span[1] == next_span[0]
    first_start = spans[0][0]
    preamble = norm_text[:first_start]
    body = norm_text[first_start:]
    assert "817-459-6652" in preamble  # the real #1068 boilerplate phone-number instructions
    assert "817-459-6652" not in body
    assert body.startswith("i. call to order")


def test_real_legistar_attachment_pdf_extracts_cleanly():
    content = (FIXTURES / "pflugerville_legistar_attachment.pdf").read_bytes()
    text, _ = _extract_pdf(content)
    assert "PARKS AND RECREATION MONTH" in text
