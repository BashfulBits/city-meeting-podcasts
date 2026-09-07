import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from citypods.agenda_text import (
    OCR_FULL_SECONDS_PER_PAGE,
    AgendaTitleCandidate,
    _extract_pdf,
    agenda_title_similarity,
    assess_agenda_document,
    attribute_links_by_content,
    attribute_links_to_chapters,
    chapter_text_matches,
    classify_agenda_text,
    extract_agenda_outline,
    extract_agenda_text,
    extract_agenda_title_candidates,
    extract_html,
    extract_pdf_layout_text,
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


def test_record_round_trip_preserves_agenda_quality_metadata():
    episode = Episode("g", "Meeting", datetime.now(UTC), "video", body="Council")
    episode.agenda_text_url = "https://example.test/agenda.pdf"
    episode.agenda_text_quality = {
        "status": "rejected",
        "eligibility": "unknown",
        "method": "none",
        "reason": "ambiguous-native-and-ocr",
        "pipeline_version": "2",
        "document_hash": "abc",
        "assessment_attempts": 3,
    }
    restored = record_to_episode(episode_to_record(episode))
    assert restored.agenda_text_quality == episode.agenda_text_quality


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


def test_quality_classifier_preserves_short_notice_and_rejects_viewer_shell():
    assert classify_agenda_text("Meeting canceled due to weather") == "short-notice"
    assert classify_agenda_text("Loading…") == "viewer-placeholder"
    assert classify_agenda_text("https://example.test/documentviewer.php") == "viewer-placeholder"


def test_quality_classifier_preserves_line_structure_for_real_agenda_text():
    text = (
        "Public meeting information\n"
        "1. Housing and neighborhood improvements\n"
        "2. Budget and transportation planning\n"
        "3. Public safety capital program update\n"
        + ("The loading dock report is included in the supporting materials.\n" * 12)
    )
    assert classify_agenda_text(text) == "complete"


def test_pdf_quality_gate_keeps_good_native_text_without_ocr():
    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    calls = []

    def ocr(*args, **kwargs):
        calls.append((args, kwargs))
        return "", None

    assessment, _ = assess_agenda_document(
        content,
        content_type="application/pdf",
        source_url="https://example.test/agenda.pdf",
        ocr_runner=ocr,
    )
    assert assessment.status == "accepted"
    assert assessment.method == "native"
    assert calls == []


def test_pdf_quality_gate_accepts_ocr_when_embedded_text_is_a_placeholder(monkeypatch):
    import citypods.agenda_text as agenda_text

    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    monkeypatch.setattr(agenda_text, "extract_document", lambda *args, **kwargs: ("Loading…", []))
    calls = []

    def ocr(_content, pages, *, timeout):
        calls.append((pages, timeout))
        return (
            "AGENDA\n1. Housing approval and neighborhood improvements\n"
            "2. Street improvements and transportation planning\n"
            "3. Public safety capital program update",
            91.0,
        )

    assessment, _ = assess_agenda_document(
        content,
        content_type="application/pdf",
        source_url="https://example.test/agenda.pdf",
        ocr_runner=ocr,
    )
    assert assessment.status == "accepted"
    assert assessment.method == "ocr"
    assert assessment.reason == "ocr-materially-better"
    assert len(calls) == 2
    assert calls[1][1] >= OCR_FULL_SECONDS_PER_PAGE * len(calls[1][0])


def test_pdf_quality_gate_fails_closed_when_ocr_is_ambiguous(monkeypatch):
    import citypods.agenda_text as agenda_text

    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    monkeypatch.setattr(
        agenda_text,
        "extract_document",
        lambda *args, **kwargs: ("Short embedded message", []),
    )
    assessment, _ = assess_agenda_document(
        content,
        content_type="application/pdf",
        source_url="https://example.test/agenda.pdf",
        ocr_runner=lambda *args, **kwargs: ("Unclear", 42.0),
    )
    assert assessment.status == "rejected"
    assert assessment.reason == "ambiguous-native-and-ocr"


def test_pdf_quality_gate_records_ocr_unavailability(monkeypatch):
    import citypods.agenda_text as agenda_text

    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    monkeypatch.setattr(agenda_text, "extract_document", lambda *args, **kwargs: ("Loading…", []))
    monkeypatch.setattr(agenda_text.shutil, "which", lambda name: None)
    assessment, _ = assess_agenda_document(
        content,
        content_type="application/pdf",
        source_url="https://example.test/agenda.pdf",
    )
    assert assessment.status == "rejected"
    assert assessment.reason == "ocr-unavailable"


def test_pdf_quality_gate_fails_closed_when_full_ocr_times_out(monkeypatch):
    import citypods.agenda_text as agenda_text

    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    monkeypatch.setattr(agenda_text, "extract_document", lambda *args, **kwargs: ("Loading…", []))
    calls = 0

    def ocr(_content, pages, *, timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.TimeoutExpired("ocr", timeout)
        return (
            "AGENDA\n1. Housing approval and neighborhood improvements\n"
            "2. Street improvements and transportation planning\n"
            "3. Public safety capital program update",
            91.0,
        )

    assessment, _ = assess_agenda_document(
        content,
        content_type="application/pdf",
        source_url="https://example.test/agenda.pdf",
        ocr_runner=ocr,
    )
    assert assessment.status == "rejected"
    assert assessment.reason == "ocr-timeout"


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


def test_extract_agenda_title_candidates_handles_numbered_and_split_pdf_lines():
    text = """Denton County
Agenda
CALL TO ORDER
2.
CONSENT AGENDA
6.
A.
Approval of annual purchase of tires, and any appropriate action.
Attachment:
Agenda Memo
"""

    assert extract_agenda_title_candidates(text) == [
        AgendaTitleCandidate("CALL TO ORDER", 3),
        AgendaTitleCandidate("2. CONSENT AGENDA", 5),
        AgendaTitleCandidate(
            "6. A. Approval of annual purchase of tires, and any appropriate action.", 8
        ),
    ]


def test_extract_agenda_title_candidates_starts_all_caps_sections_at_agenda_marker():
    text = """CITY OF EXAMPLE
AGENDA
CALL TO ORDER
PUBLIC HEARINGS
1. Rezoning: Example Case
PERMANENT COMMITTEE UPDATES
"""

    assert [item.title for item in extract_agenda_title_candidates(text)] == [
        "CALL TO ORDER",
        "PUBLIC HEARINGS",
        "1. Rezoning: Example Case",
        "PERMANENT COMMITTEE UPDATES",
    ]


def test_agenda_title_similarity_tolerates_numbering_but_not_unrelated_items():
    assert agenda_title_similarity("2. CONSENT AGENDA", "Consent Agenda") == 1.0
    assert agenda_title_similarity("Approve zoning case", "Budget adoption") < 0.5


def test_extract_agenda_title_candidates_carries_parent_number_to_lettered_siblings():
    text = """AGENDA
6. A. First purchase item
C.
Third purchase item
"""

    assert [item.title for item in extract_agenda_title_candidates(text)] == [
        "6. A. First purchase item",
        "6. C. Third purchase item",
    ]


def test_html_outline_preserves_semantic_and_granicus_agenda_headings():
    outline = extract_agenda_outline(
        b"""<h1>Commissioners Court</h1>
        <div><a class='Agenda Agenda0'>CALL TO ORDER</a></div>
        <div><a class='Document'>Agenda Memo</a></div>
        <h2>Consent Agenda</h2>""",
        content_type="text/html",
        source_url="https://example.test/agenda",
    )

    assert outline.splitlines() == [
        "# Commissioners Court",
        "## CALL TO ORDER",
        "## Consent Agenda",
    ]
    assert [item.title for item in extract_agenda_title_candidates(outline)] == [
        "Commissioners Court",
        "CALL TO ORDER",
        "Consent Agenda",
    ]


def test_pdf_layout_outline_uses_existing_pypdf_layout_mode():
    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()

    plain, _ = _extract_pdf(content)
    layout = extract_pdf_layout_text(content)
    outline = extract_agenda_outline(
        content,
        content_type="application/pdf",
        source_url="https://example.test/agenda.pdf",
    )

    assert "CALL TO ORDER" in layout
    assert layout != plain
    assert outline == layout


def test_roster_rejects_spans_that_are_not_name_shaped():
    """A roster entry is not merely displayed: it enrols a person in the body registry, carries
    forward as standing membership, tiers as a *member*, and acts as a correction constraint that
    removes voice matches outside it. One junk entry therefore suppresses correct naming for the
    whole meeting -- strictly worse than extracting nothing, since an empty roster already means
    "make no correction"."""
    junk = [
        "Present: a quorum of the Council was established at 6:02 p.m.",
        "Present: 7",
        "Members Present: Jane Doe Bob Chair Ann Lee",  # run-on: one 6-word "name"
        "MEMBERS PRESENT: J4ne D0e, ., B",
        "Present: Yes.  Absent: None",
        "Others Present: Random Citizen",  # in the room, not on the body
    ]
    for text in junk:
        assert parse_roster(text) == [], text


def test_roster_strips_leading_titles_so_names_can_corroborate_cues():
    """Corroboration compares the roster name to the *spoken* name, so a stored "Mayor Gerard
    Hudspeth" would never corroborate a chair cue proposing "Gerard Hudspeth" -- failing for
    exactly the people who speak most."""
    rows = parse_roster(
        "PRESENT: Mayor Gerard Hudspeth, Council Member Vicki Byrd, Mayor Pro Tem Brian Beck"
    )
    assert [row["name"] for row in rows] == ["Gerard Hudspeth", "Vicki Byrd", "Brian Beck"]


def test_roster_labels_member_and_staff_sections():
    members = parse_roster("MEMBERS PRESENT: Jane Doe")
    staff = parse_roster("City Staff Present: Matt Bodine")
    flat = parse_roster("Present: Alice Smith")
    assert members[0]["section"] == "members"
    assert staff[0]["section"] == "staff"
    # A flat list carries no section; tiering falls back to spoken-title vocabulary.
    assert "section" not in flat[0]
    # Staff wins a mixed qualifier: "members" is filler there, "staff" discriminates.
    assert parse_roster("Staff Members Present: Sam Staffer")[0]["section"] == "staff"


def test_also_present_is_a_staff_section_not_an_unsectioned_member_row():
    """ "ALSO PRESENT:" is the conventional heading for the clerk, attorney and manager. It matched
    no vocabulary, so it fell through as an unsectioned row -- and an unsectioned roster hit tiers
    as `member`, which would hand the City Manager the speaker page reserved for officials."""
    rows = parse_roster("ALSO PRESENT: City Manager Sara Hensley, City Attorney Mack Reinwand")
    assert [(row["name"], row["section"]) for row in rows] == [
        ("Sara Hensley", "staff"),
        ("Mack Reinwand", "staff"),
    ]


def test_staff_titles_are_stripped_from_roster_names():
    """Stripped word-by-word from the front, because real staff titles are open-ended phrases.
    Leaving the title in place stores it as part of the person's name, where it matches no spoken
    name and corroborates nothing."""
    rows = parse_roster("Also Present: Interim Assistant City Manager Jane Doe")
    assert [row["name"] for row in rows] == ["Jane Doe"]


def test_a_surname_that_is_also_a_role_word_survives():
    """Leading-only stripping: "Manager" is a title at the front and a surname at the back."""
    assert [row["name"] for row in parse_roster("Present: Mark Manager")] == ["Mark Manager"]


def test_an_office_listed_without_a_name_is_not_a_person():
    """Leading-only role-word stripping always spares the last word, so "City Manager, City
    Attorney" would enrol people called "Manager" and "Attorney" -- who then narrow
    `roster_person_ids` and remove correct voice matches for the whole meeting."""
    assert parse_roster("ALSO PRESENT: City Manager, City Attorney") == []
    # A real name after the office still resolves, and a role-word surname still survives.
    assert [r["name"] for r in parse_roster("ALSO PRESENT: City Manager Sara Hensley")] == [
        "Sara Hensley"
    ]
    assert [r["name"] for r in parse_roster("Present: Mark Manager")] == ["Mark Manager"]
