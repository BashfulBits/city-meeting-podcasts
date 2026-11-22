import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from citypods.agenda_text import (
    OCR_FULL_SECONDS_PER_PAGE,
    AgendaTitleCandidate,
    OcrUnavailableError,
    _extract_pdf,
    _is_corrupted_native_text,
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


def test_pdf_quality_gate_rejects_raw_pdf_bytes_leaking_through_as_native_text(monkeypatch):
    """End-to-end regression: even if some extraction path leaks the PDF's own undecoded bytes as
    "native text" (as _extract_pdf's ImportError fallback used to), the quality gate must not
    accept it as a real agenda_text_artifact. Raw PDF container syntax and garbled
    compressed-stream bytes decode into plausible, keyword-bearing noise that would otherwise
    clear the alpha-char/agenda-content-score thresholds by chance."""
    import citypods.agenda_text as agenda_text

    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    raw_pdf_like = "%PDF-1.5\n1 0 obj\n<<\n>>\nendobj\n" + "agenda 1. item x" * 200
    monkeypatch.setattr(agenda_text, "extract_document", lambda *args, **kwargs: (raw_pdf_like, []))
    assessment, _ = assess_agenda_document(
        content,
        content_type="application/pdf",
        source_url="https://example.test/agenda.pdf",
        ocr_runner=lambda *args, **kwargs: ("", None),
    )
    assert assessment.status == "rejected"


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


def test_pdf_quality_gate_detects_corrupted_font_and_falls_back_to_ocr():
    fixture_path = FIXTURES / "fort_worth_budget_2026_08_11_corrupted_font.pdf"
    content = fixture_path.read_bytes()
    native, _ = _extract_pdf(content)
    assert _is_corrupted_native_text(native)

    assessment, _ = assess_agenda_document(
        content,
        content_type="application/pdf",
        source_url="https://example.test/fort_worth_budget.pdf",
    )
    assert assessment.status == "accepted"
    assert assessment.method == "ocr"
    assert assessment.reason == "ocr-materially-better"
    assert "FORT WORTH" in assessment.text.upper() or "BUDGET" in assessment.text.upper()


def test_pdf_quality_gate_rejects_corrupted_font_when_ocr_unavailable():
    fixture_path = FIXTURES / "fort_worth_budget_2026_08_11_corrupted_font.pdf"
    content = fixture_path.read_bytes()

    def unavailable_ocr(*_args, **_kwargs):
        raise OcrUnavailableError("ocr unavailable")

    assessment, _ = assess_agenda_document(
        content,
        content_type="application/pdf",
        source_url="https://example.test/fort_worth_budget.pdf",
        ocr_runner=unavailable_ocr,
    )
    assert assessment.status == "rejected"
    assert assessment.method == "none"
    assert assessment.reason == "corrupted-native-text"


def test_extract_agenda_title_candidates_strips_stray_bullets_and_preserves_proper_names():
    text = (
        "AGENDA\n"
        "Å 1. Call to Order\n"
        "• 2. Approval of Minutes\n"
        "Åsa Lindgren\n"
        "- 3. Public Hearing on Zoning\n"
        "• CALL TO ORDER\n"
        "Å APPROVAL OF MINUTES\n"
    )
    candidates = extract_agenda_title_candidates(text)
    titles = [c.title for c in candidates]
    assert "1. Call to Order" in titles
    assert "2. Approval of Minutes" in titles
    assert "3. Public Hearing on Zoning" in titles
    assert "CALL TO ORDER" in titles
    assert "APPROVAL OF MINUTES" in titles
    assert "sa Lindgren" not in titles


def test_is_corrupted_native_text_heuristics():
    # Glyph name sequence (pypdf unmapped font tokens)
    glyph_dump = "/i255 /1 /2 /3 /4 /5 /6 /7 /8 /9 /10 /11 /12 /13 /14"
    assert _is_corrupted_native_text(glyph_dump)

    # Unmapped replacement char flood
    unmapped = "meeting notice " + "\ufffd" * 20 + " items"
    assert _is_corrupted_native_text(unmapped)

    # Clean text with URLs should not be marked corrupted
    clean_text = (
        "City Council Agenda\n"
        "Item 1. Discussion on road infrastructure\n"
        "See https://example.gov/agenda/2026/08/11/packet.pdf for materials.\n"
        "Item 2. Consideration of municipal budget amendments\n"
    )
    assert not _is_corrupted_native_text(clean_text)


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


def test_extract_pdf_treats_missing_pypdf_as_extraction_failure_not_raw_bytes(monkeypatch):
    """Regression: pypdf is a required dependency (pyproject.toml declares `pypdf>=5.0`), so this
    should never happen in a correctly provisioned production run -- but when
    `from pypdf import PdfReader` fails, _extract_pdf used to decode the PDF's own raw bytes as
    UTF-8 and hand that back as if it were extracted text. Real PDF container syntax and garbled
    compressed-stream bytes decode into plausible, keyword-bearing noise rather than raising, so
    that corruption slipped past assess_agenda_document's quality gate and was persisted as a
    genuine agenda_text_artifact."""
    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    monkeypatch.setitem(sys.modules, "pypdf", None)
    text, links = _extract_pdf(content, source_url="https://example.test/agenda.pdf")
    assert text == ""
    assert links == []


def test_pdf_layout_text_treats_missing_pypdf_as_extraction_failure(monkeypatch):
    """Same failure mode as _extract_pdf's ImportError branch, for the parallel layout-mode
    extractor that shared the identical raw-bytes-decode anti-pattern."""
    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    monkeypatch.setitem(sys.modules, "pypdf", None)
    assert extract_pdf_layout_text(content) == ""


def test_classify_agenda_text_rejects_raw_undecoded_pdf_bytes():
    """Regression for the gap in placeholder detection: raw PDF container bytes decoded as UTF-8
    (see the _extract_pdf ImportError fallback bug) read as substantial, keyword-bearing prose to
    the placeholder heuristics below it -- decompressed-stream noise easily clears the alpha-char
    and agenda-content-score thresholds by chance. The %PDF- file-signature guard catches it
    structurally instead of relying on those score-based checks."""
    raw_pdf_like = "%PDF-1.5\n%\x93\x8c\x8b\x9e\n1 0 obj\n<<\n>>\nendobj\n" + (
        "agenda item xyz garbled endstream " * 40
    )
    assert classify_agenda_text(raw_pdf_like) == "viewer-placeholder"


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


def test_pdf_served_as_octet_stream_from_an_extensionless_url_is_read_as_a_pdf():
    content = (FIXTURES / "arlington_pz_2021_01_20_agenda.pdf").read_bytes()
    assessment, _ = assess_agenda_document(
        content,
        content_type="application/octet-stream",
        source_url="https://example.test/videos/46567/agenda",
        ocr_runner=lambda *args, **kwargs: ("", None),
    )
    assert assessment.status == "accepted"
    assert assessment.reason != "placeholder-or-low-quality-html"


def test_html_that_merely_mentions_pdf_is_not_sniffed_as_a_pdf():
    from citypods.agenda_text import _is_pdf

    assert not _is_pdf("text/html", "https://example.test/a", b"<html>%PDF- later</html>")
    assert _is_pdf("application/octet-stream", "https://example.test/a", b"\n %PDF-1.7\n...")


_GRANICUS_UNPUBLISHED = (
    b"<p style='font-size:18.0pt'>This agenda is not currently published.  Please try again later "
    b"or contact the publisher.</p><hr /><p>If you are authorized to publish the agenda, please "
    b"log in to view the draft agenda.</p>"
)


def test_publisher_unpublished_agenda_gets_its_own_reason():
    assessment, _ = assess_agenda_document(
        _GRANICUS_UNPUBLISHED,
        content_type="text/html; charset=utf-8",
        source_url="https://example.granicus.com/AgendaViewer.php?view_id=1&clip_id=2",
    )
    assert assessment.status == "rejected"
    assert assessment.reason == "agenda-not-published"


def test_other_low_quality_html_keeps_the_generic_reason():
    assessment, _ = assess_agenda_document(
        b"<html><body>Loading...</body></html>",
        content_type="text/html",
        source_url="https://example.test/viewer",
    )
    assert assessment.reason == "placeholder-or-low-quality-html"


def test_is_unpublished_agenda_recognizes_new_reason_and_the_legacy_granicus_hash():
    from citypods.agenda_text import is_unpublished_agenda

    legacy = {
        "reason": "placeholder-or-low-quality-html",
        "document_hash": "96075776f783fdd68189d7dfa02a8515d5697b6d76b632209342235e2f41e4a1",
    }
    assert is_unpublished_agenda({"reason": "agenda-not-published"})
    assert is_unpublished_agenda(legacy)
    assert not is_unpublished_agenda({"reason": "placeholder-or-low-quality-html"})
    assert not is_unpublished_agenda({"reason": "ambiguous-native-and-ocr"})
    assert not is_unpublished_agenda(None)


def test_a_real_agenda_mentioning_unpublished_items_is_not_marked_not_published():
    body = (
        "<html><body>"
        + "Item 1. Review reports not currently published on the city website. " * 3
        + "Loading... "
        + "x" * 600
        + "</body></html>"
    ).encode()
    assessment, _ = assess_agenda_document(
        body, content_type="text/html", source_url="https://example.test/viewer"
    )
    assert assessment.reason != "agenda-not-published"


def _names(rows, status="present"):
    return [row["name"] for row in rows if row["status"] == status]


def test_roster_reads_a_prose_attendance_sentence_and_the_presiding_member():
    text = (
        "City of Springfield\nMinutes - Final\nCity Council\nCall to Order1.\n"
        "Mayor Jane Doe called the meeting to order at 7:00pm. Mayor Pro Tem Ann Lee and\n"
        "Councilmembers Bob Roe, Cy Poe, and Di Moe were in attendance. Councilmember Ed Foe\n"
        "joined the meeting at 7:09pm. City staff in attendance: Al Noe, City Manager; Bo Zoe,\n"
        "City Attorney.\n"
    )
    rows = parse_roster(text)
    assert _names(rows) == ["Ann Lee", "Bob Roe", "Cy Poe", "Di Moe", "Jane Doe"]
    assert all(row.get("section") != "staff" for row in rows)


def test_prose_roster_ignores_guests_who_were_in_attendance_later_in_the_minutes():
    filler = "Item discussion continued. " * 150
    text = (
        "Mayor Jane Doe called the meeting to order at 7:00pm. Councilmembers Ann Lee and Bob Roe "
        "were in attendance. " + filler + "Amy Guest, Acme Executive Director, was in attendance."
    )
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Jane Doe"]


def test_prose_roster_drops_a_staffer_listed_beside_the_members():
    text = (
        "Vice Chair Jo Park called the meeting to order at 7:00 p.m. Commissioners Ann Lee, Bob "
        "Roe, and Cy Poe, City Attorney, were in attendance. Chair Di Moe was not present."
    )
    rows = parse_roster(text)
    assert _names(rows) == ["Ann Lee", "Bob Roe", "Jo Park"]  # Cy Poe is the City Attorney
    assert _names(rows, "absent") == ["Di Moe"]


def test_prose_roster_reads_a_qualified_in_attendance_list_but_not_the_staff_one():
    text = (
        "Chair Ann Lee called the meeting to order at 7:00pm. Commissioners in attendance: Ann "
        "Lee, Bob Roe, Cy Poe Commissioners absent: Di Moe. Staff in attendance: Al Noe, "
        "Director; Bo Zoe, Planner."
    )
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Cy Poe"]


def test_prose_roster_rejects_fragments_and_single_words():
    text = (
        "The meeting was called to order at 7:00pm. The motion carried separately and "
        "seperately were in attendance."
    )
    assert parse_roster(text) == []


def test_roster_reads_a_status_line_with_a_line_number_prefix():
    text = (
        "9\n10 PRESENT: Chair Ann Lee, Commissioners: Bob Roe, Cy Poe, and Di Moe\n11 ABSENT: None"
    )
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Cy Poe", "Di Moe"]


def test_roster_reads_in_person_and_virtual_headings():
    text = (
        "PRESENT IN PERSON: Mayor Ann Lee and Council Members Bob Roe, Cy Poe\n"
        "PRESENT VIRTUALLY: Di Moe"
    )
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Cy Poe", "Di Moe"]


def test_roster_reads_names_on_the_line_below_a_heading():
    text = (
        "PRESENT:\nMayor Ann Lee, Mayor Pro Tem Bob Roe, Council Member Cy Poe and Council Member "
        "Di Moe.\nABSENT:\nCouncil Member Ed Foe.\n1.\nRequests for clarification"
    )
    rows = parse_roster(text)
    assert _names(rows) == ["Ann Lee", "Bob Roe", "Cy Poe", "Di Moe"]
    assert _names(rows, "absent") == ["Ed Foe"]


def test_roster_leaves_a_flattened_heading_table_empty_rather_than_guess():
    """Every heading printed first, then one line of names per heading: the names cannot be
    matched to headings by position, and mis-filing the present members as absent would be worse
    than no roster."""
    text = (
        "PRESENT IN PERSON:\nPRESENT VIRTUALLY:\nABSENT:\n"
        "Mayor Ann Lee and Council Members Bob Roe, Cy Poe\n"
        "Mayor Pro Tem Di Moe\nNone\n"
    )
    assert parse_roster(text) == []


def test_roster_drops_office_fragments_and_ocr_mayor_pro_tern():
    text = "PRESENT: Mayor Ann Lee, Mayor Pro Tern Bob Roe and Council\nABSENT: Jr"
    rows = parse_roster(text)
    assert _names(rows) == ["Ann Lee", "Bob Roe"]
    assert _names(rows, "absent") == []


def test_roster_strips_an_inline_commissioners_label():
    text = "PRESENT: Chair Ann Lee, Commissioners: Bob Roe, Cy Poe"
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Cy Poe"]


def test_roster_joins_a_wrapped_attendance_line():
    text = (
        "PRESENT: Mayor Ann Lee, Mayor Pro Tem Bob Roe and Council Members Cy\n"
        "Poe, Di Moe, and Ed Foe\n"
        "ABSENT: None\n"
    )
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Cy Poe", "Di Moe", "Ed Foe"]


def test_roster_joins_a_wrapped_first_name_to_its_surname():
    text = "PRESENT: Mayor Ann Lee and Council Members Bob\nRoe, Cy Poe\n1. Citizen Comments\n"
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Cy Poe"]


def test_roster_does_not_swallow_the_section_that_follows_an_attendance_line():
    text = "PRESENT: Chair Ann Lee, Bob Roe\nABSENT: Commissioner Cy Poe\nWORK SESSION\n1. Items\n"
    rows = parse_roster(text)
    assert _names(rows) == ["Ann Lee", "Bob Roe"]
    assert _names(rows, "absent") == ["Cy Poe"]


def test_roster_does_not_read_a_vote_line_as_the_end_of_the_attendance_list():
    text = "Present: Alice Smith, Bob Jones\nVote: Yes: Alice Smith, Carol Outsider; No: Bob Jones"
    assert _names(parse_roster(text)) == ["Alice Smith", "Bob Jones"]


def test_roster_keeps_each_repeated_attendance_fact_once():
    block = "PRESENT: Mayor Ann Lee, Bob Roe\n"
    assert _names(parse_roster(block * 3)) == ["Ann Lee", "Bob Roe"]


def test_roster_strips_hyphenated_and_ocr_spaced_vice_chair_and_members_labels():
    text = "PRESENT: Vice-Chair Ann Lee, Vice- Chair Bob Roe, Members Cy Poe, Chair Pro Tem Di Moe"
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Cy Poe", "Di Moe"]


_SCANNED_PDF = b"%PDF-1.4 scanned"


def _scanned(monkeypatch, pages=3):
    import citypods.agenda_text as agenda_text

    monkeypatch.setattr(agenda_text, "extract_document", lambda *a, **k: ("", []))
    monkeypatch.setattr(agenda_text, "_pdf_page_count", lambda content: pages)
    return agenda_text


def test_minutes_ocr_reads_a_scanned_pdf_with_confident_text(monkeypatch):
    agenda_text = _scanned(monkeypatch)
    seen = {}

    def runner(content, pages, *, timeout, preserve_lines):
        seen.update(pages=pages, timeout=timeout, preserve_lines=preserve_lines)
        return "PRESENT: Mayor Ann Lee, Bob Roe\n" + "Minutes of the regular meeting. " * 20, 95.0

    text, _, method = agenda_text.extract_minutes_document(
        _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
    )
    assert method == "ocr"
    assert text.startswith("PRESENT:")
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe"]
    assert seen["pages"] == [1, 2, 3] and seen["preserve_lines"] is True
    assert seen["timeout"] >= agenda_text.OCR_FULL_SECONDS_PER_PAGE * 3


def test_minutes_ocr_discards_unconfident_text_but_keeps_failures_retryable(monkeypatch):
    agenda_text = _scanned(monkeypatch)
    body = "Minutes of the regular meeting. " * 20

    def blurry(content, pages, *, timeout, preserve_lines):
        return body, 60.0

    def missing(content, pages, *, timeout, preserve_lines):
        raise agenda_text.OcrUnavailableError

    def too_slow(content, pages, *, timeout, preserve_lines):
        raise subprocess.TimeoutExpired("ocr", timeout)

    outcomes = {}
    for name, runner in (("blurry", blurry), ("missing", missing), ("too_slow", too_slow)):
        text, _, method = agenda_text.extract_minutes_document(
            _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
        )
        outcomes[name] = (text, method)
    # A deliberate quality rejection is final; a tool failure must be retried, not cached.
    assert outcomes == {
        "blurry": ("", "none"),
        "missing": ("", "retry"),
        "too_slow": ("", "retry"),
    }


def test_minutes_ocr_is_deferred_when_the_run_budget_is_spent(monkeypatch):
    agenda_text = _scanned(monkeypatch)

    def runner(*args, **kwargs):
        raise AssertionError("OCR must not start once the budget is spent")

    text, _, method = agenda_text.extract_minutes_document(
        _SCANNED_PDF,
        content_type="application/pdf",
        ocr_runner=runner,
        ocr_allowed=lambda: False,
    )
    assert (text, method) == ("", "deferred")


def test_minutes_ocr_is_not_attempted_when_native_text_exists_or_the_document_is_html(monkeypatch):
    import citypods.agenda_text as agenda_text

    def runner(*args, **kwargs):
        raise AssertionError("OCR must not run")

    native = "Minutes of the regular meeting of the council. " * 60
    monkeypatch.setattr(agenda_text, "extract_document", lambda *a, **k: (native, []))
    monkeypatch.setattr(agenda_text, "_pdf_page_count", lambda content: 3)
    text, _, method = agenda_text.extract_minutes_document(
        _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
    )
    assert (text, method) == (native, "native")
    monkeypatch.setattr(agenda_text, "extract_document", lambda *a, **k: ("", []))
    _, _, method = agenda_text.extract_minutes_document(
        b"<html></html>", content_type="text/html", ocr_runner=runner
    )
    assert method == "none"


def test_ocr_can_keep_tesseract_line_breaks(monkeypatch):
    import citypods.agenda_text as agenda_text

    header = "\t".join(
        "level page_num block_num par_num line_num word_num left top width height conf text".split()
    )

    def row(block, line, word, conf, text):
        return f"5\t1\t{block}\t1\t{line}\t{word}\t0\t0\t1\t1\t{conf}\t{text}"

    tsv = "\n".join(
        [
            header,
            row(1, 1, 1, 96, "PRESENT:"),
            row(1, 1, 2, 96, "Ann"),
            row(1, 1, 3, 96, "Lee"),
            row(1, 2, 1, 94, "ABSENT:"),
            row(1, 2, 2, 94, "None"),
        ]
    )

    def fake_run(command, **kwargs):
        stdout = tsv if command[0] == "tesseract" else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(agenda_text.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(agenda_text.subprocess, "run", fake_run)
    flat, _ = agenda_text._ocr_pdf_pages(b"%PDF", [1], timeout=30)
    lined, confidence = agenda_text._ocr_pdf_pages(b"%PDF", [1], timeout=30, preserve_lines=True)
    assert flat == "PRESENT: Ann Lee ABSENT: None"
    assert lined == "PRESENT: Ann Lee\nABSENT: None"
    assert confidence == pytest.approx(95.2)


def test_prose_roster_checks_a_staff_or_guest_label_before_stripping_it():
    staff = (
        "Chair Ann Lee called the meeting to order. Staff: Jane Doe and Bob Roe were in attendance."
    )
    assert _names(parse_roster(staff)) == ["Ann Lee"]
    guests = (
        "Chair Ann Lee called the meeting to order. Guests Jane Doe and Bob Roe were in attendance."
    )
    assert _names(parse_roster(guests)) == ["Ann Lee"]
    members = (
        "Chair Ann Lee called the meeting to order. Commission Members: Jane Doe and Bob Roe "
        "were in attendance."
    )
    assert _names(parse_roster(members)) == ["Jane Doe", "Bob Roe", "Ann Lee"]
    plain = (
        "Chair Ann Lee called the meeting to order. Commissioners Jane Doe and Bob Roe were in "
        "attendance."
    )
    assert _names(parse_roster(plain)) == ["Jane Doe", "Bob Roe", "Ann Lee"]


def test_roster_does_not_append_an_unrelated_heading_to_a_members_name():
    assert _names(parse_roster("PRESENT: Alice Smith\nPublic Comment\n")) == ["Alice Smith"]
    assert _names(parse_roster("PRESENT: Alice Smith\nNew Business\nABSENT: None")) == [
        "Alice Smith"
    ]


def test_roster_keeps_the_lines_of_a_vertical_attendance_list_separate():
    rows = parse_roster("PRESENT:\nALICE SMITH\nBOB JONES\nCARY LEE\n")
    assert _names(rows) == ["ALICE SMITH", "BOB JONES", "CARY LEE"]


def test_prose_roster_keeps_excused_distinct_from_absent():
    text = (
        "Chair Ann Lee called the meeting to order. Commissioner Bob Roe was excused. "
        "Commissioner Cy Poe was not present. Commissioner Di Moe was absent."
    )
    rows = parse_roster(text)
    assert _names(rows, "excused") == ["Bob Roe"]
    assert _names(rows, "absent") == ["Cy Poe", "Di Moe"]


def test_roster_dedupe_keeps_names_that_differ_only_by_a_diacritic():
    assert _names(parse_roster("PRESENT: José Silva, Jose Silva, José Silva")) == [
        "José Silva",
        "Jose Silva",
    ]
    prose = (
        "Chair Ann Lee called the meeting to order. Commissioners José Silva and Jose Silva "
        "were in attendance."
    )
    assert sorted(_names(parse_roster(prose))) == ["Ann Lee", "Jose Silva", "José Silva"]


def test_roster_only_a_real_empty_attendance_heading_makes_the_next_heading_a_table():
    assert _names(parse_roster("ROLL CALL:\nPRESENT:\nAlice Smith, Bob Jones\n")) == [
        "Alice Smith",
        "Bob Jones",
    ]


def test_prose_roster_does_not_mistake_a_surname_for_a_staff_title():
    text = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith and Mark Manager "
        "were in attendance."
    )
    assert _names(parse_roster(text)) == ["Alice Smith", "Mark Manager", "Ann Lee"]
    staffer = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith, Cy Poe, City "
        "Attorney, were in attendance."
    )
    assert _names(parse_roster(staffer)) == ["Alice Smith", "Ann Lee"]


def test_prose_members_are_read_when_structured_parsing_found_only_staff():
    text = (
        "STAFF PRESENT: Al Noe, City Manager.\n"
        "Chair Ann Lee called the meeting to order at 7:00 p.m. Commissioners Bob Roe and Cy Poe "
        "were in attendance."
    )
    rows = parse_roster(text)
    assert any(row.get("section") == "staff" for row in rows)
    members = [row["name"] for row in rows if row.get("section") != "staff"]
    assert sorted(members) == ["Ann Lee", "Bob Roe", "Cy Poe"]


def test_roster_parsing_stays_fast_with_many_repeated_attendance_headings():
    import time

    text = ("PRESENT: Chair Ann Lee, Bob Roe\nABSENT: None\n" + "Item text. " * 40 + "\n") * 2500
    started = time.monotonic()
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe"]
    assert time.monotonic() - started < 5


def test_prose_roster_does_not_split_a_name_at_an_initial():
    text = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith and Robert J. Van "
        "Dyke were in attendance."
    )
    assert _names(parse_roster(text)) == ["Alice Smith", "Robert J. Van Dyke", "Ann Lee"]
    titled = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith, Mr. Bob Roe and "
        "Cy Poe Jr. were in attendance."
    )
    assert _names(parse_roster(titled)) == ["Alice Smith", "Bob Roe", "Cy Poe Jr", "Ann Lee"]


def test_prose_roster_does_not_enrol_a_staff_titled_entry_as_a_member():
    text = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith and City Attorney "
        "Bob Roe were in attendance."
    )
    assert _names(parse_roster(text)) == ["Alice Smith", "Ann Lee"]
    elected = (
        "Mayor Ann Lee called the meeting to order. City Councilmembers Wayne Cooper and County "
        "Commissioner Jane Doe were in attendance."
    )
    assert _names(parse_roster(elected)) == ["Wayne Cooper", "Jane Doe", "Ann Lee"]


def test_roster_rejects_a_vertical_list_longer_than_the_bound_instead_of_truncating_it():
    from citypods.agenda_text import _MAX_ROSTER_CONTINUATION_LINES

    members = [f"Member{chr(65 + i)} Person" for i in range(_MAX_ROSTER_CONTINUATION_LINES + 3)]
    assert parse_roster("PRESENT:\n" + "\n".join(members) + "\n") == []
    fitting = members[:12]
    assert len(_names(parse_roster("PRESENT:\n" + "\n".join(fitting) + "\n"))) == 12


def test_roster_stops_a_vertical_list_at_the_next_section_heading():
    text = "PRESENT:\nAlice Smith\nBob Jones\nPUBLIC COMMENT\n1. Discussion\n"
    assert _names(parse_roster(text)) == ["Alice Smith", "Bob Jones"]
    caps = "PRESENT:\nALICE SMITH\nBOB JONES\nCALL TO ORDER\n"
    assert _names(parse_roster(caps)) == ["ALICE SMITH", "BOB JONES"]
    case_change = "PRESENT:\nAlice Smith\nNEW SECTION HEADING\n"
    assert _names(parse_roster(case_change)) == ["Alice Smith"]
    assert parse_roster("PRESENT:\nPUBLIC COMMENT\n") == []


def test_prose_roster_keeps_semicolon_separated_names():
    text = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith; Bob Roe were "
        "present."
    )
    assert _names(parse_roster(text)) == ["Alice Smith", "Bob Roe", "Ann Lee"]


def test_roster_keeps_reading_a_vertical_list_past_a_suffix_period():
    text = "PRESENT:\nAlice Smith Jr.\nBob Jones\nABSENT: None\n"
    assert _names(parse_roster(text)) == ["Alice Smith Jr", "Bob Jones"]
    initial = "PRESENT:\nRobert J.\nBob Jones\n"
    assert "Bob Jones" in _names(parse_roster(initial))


def test_prose_roster_keeps_a_full_name_that_ends_in_a_role_word_after_a_comma():
    text = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith, Mark Manager, and "
        "Bob Roe were present."
    )
    assert _names(parse_roster(text)) == ["Alice Smith", "Mark Manager", "Bob Roe", "Ann Lee"]
    staff = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith, Shane Mize, Parks "
        "Director, and Bob Roe were present."
    )
    assert _names(parse_roster(staff)) == ["Alice Smith", "Bob Roe", "Ann Lee"]
    acronym = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith, Amy Madison, PCDC "
        "Executive Director, and Bob Roe were present."
    )
    assert _names(parse_roster(acronym)) == ["Alice Smith", "Bob Roe", "Ann Lee"]


def test_prose_roster_rejects_a_prefixed_guest_label():
    text = (
        "Chair Ann Lee called the meeting to order. Invited guests Alice Smith and Bob Roe were "
        "in attendance."
    )
    assert _names(parse_roster(text)) == ["Ann Lee"]
    staff = (
        "Chair Ann Lee called the meeting to order. City staff Alice Smith and Bob Roe were in "
        "attendance."
    )
    assert _names(parse_roster(staff)) == ["Ann Lee"]


def test_prose_roster_drops_a_leading_department_title():
    text = (
        "Chair Ann Lee called the meeting to order. Commissioners Alice Smith and Parks Director "
        "Bob Roe were present."
    )
    assert _names(parse_roster(text)) == ["Alice Smith", "Ann Lee"]


def test_roster_reads_an_all_caps_list_that_continues_after_a_comma():
    text = "PRESENT: ALICE SMITH,\nBOB JONES, CAROL BROWN\nABSENT: NONE\n"
    assert _names(parse_roster(text)) == ["ALICE SMITH", "BOB JONES", "CAROL BROWN"]


def test_roster_ignores_printed_line_numbers_on_a_vertical_list():
    text = "10 PRESENT:\n11 Alice Smith\n12 Bob Jones\n13 ABSENT: None\n"
    assert _names(parse_roster(text)) == ["Alice Smith", "Bob Jones"]


def test_roster_rejects_a_vertical_list_cut_off_by_the_lookahead_limit():
    from citypods.agenda_text import _HEADING_LOOKAHEAD_CHARS

    clipped = (
        "PRESENT:\n" + " " * (_HEADING_LOOKAHEAD_CHARS - 4) + "Alice Smith\nBob Jones\n" + "x" * 50
    )
    assert parse_roster(clipped) == []
    short = "PRESENT:\nAlice Smith\nBob Jones\n"
    assert _names(parse_roster(short)) == ["Alice Smith", "Bob Jones"]


def test_roster_splits_an_ampersand_between_wrapped_names():
    assert _names(parse_roster("PRESENT: Alice Smith &\nBob Jones\nABSENT: None\n")) == [
        "Alice Smith",
        "Bob Jones",
    ]


def test_prose_roster_does_not_infer_membership_from_presence_alone():
    visitors = (
        "Chair Ann Lee called the meeting to order. Jane Doe and Bob Roe were present to give "
        "public comment."
    )
    assert _names(parse_roster(visitors)) == ["Ann Lee"]
    untitled = "Chair Ann Lee called the meeting to order. Jane Doe and Bob Roe were in attendance."
    assert _names(parse_roster(untitled)) == ["Ann Lee"]
    members = (
        "Chair Ann Lee called the meeting to order. Commissioners Jane Doe and Bob Roe were "
        "present."
    )
    assert _names(parse_roster(members)) == ["Jane Doe", "Bob Roe", "Ann Lee"]


def test_roster_keeps_reading_after_a_members_initial():
    text = "PRESENT: Council Member Robert J.\nSmith, Alice Brown\nABSENT: None\n"
    assert _names(parse_roster(text)) == ["Robert J. Smith", "Alice Brown"]


def test_roster_rejects_the_whole_heading_when_a_wrapped_list_is_truncated():
    from citypods.agenda_text import _MAX_ROSTER_CONTINUATION_LINES

    lines = [f"Member{chr(65 + i)} Person," for i in range(_MAX_ROSTER_CONTINUATION_LINES + 2)]
    assert parse_roster("PRESENT: Alice Smith,\n" + "\n".join(lines) + "\n") == []


def test_minutes_ocr_runs_when_the_only_selectable_text_is_a_repeated_page_header(monkeypatch):
    import citypods.agenda_text as agenda_text

    header = "City Council Meeting Minutes - Regular Meeting\n" * 8
    monkeypatch.setattr(agenda_text, "extract_document", lambda *a, **k: (header, []))
    monkeypatch.setattr(agenda_text, "_pdf_page_count", lambda content: 8)
    assert agenda_text._alpha_chars(header) >= agenda_text.OCR_MIN_ALPHA_CHARS  # clears the floor

    def runner(content, pages, *, timeout, preserve_lines):
        return "PRESENT: Mayor Ann Lee, Bob Roe\n" + "Minutes of the regular meeting. " * 20, 95.0

    text, _, method = agenda_text.extract_minutes_document(
        _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
    )
    assert method == "ocr" and text.startswith("PRESENT:")


def test_minutes_text_sparseness_uses_pages_and_repeated_lines():
    from citypods.agenda_text import minutes_text_is_sparse

    real = "The council discussed the budget and approved the consent agenda items. " * 40
    assert not minutes_text_is_sparse(real, 3)
    assert minutes_text_is_sparse(real, 40)  # a page or two of text spread over 40 pages
    assert minutes_text_is_sparse("City Council Minutes\n" * 20 + "x" * 10)
    assert minutes_text_is_sparse("City Council Minutes\nPage 1")


def test_roster_stops_a_vertical_list_at_a_standalone_none():
    assert parse_roster("PRESENT:\nNone\nMotion Carried\n") == []
    assert _names(parse_roster("PRESENT:\nAlice Smith\nNone\nMotion Carried\n")) == ["Alice Smith"]
    assert _names(parse_roster("PRESENT:\nAlice Smith\nBob Jones\n")) == [
        "Alice Smith",
        "Bob Jones",
    ]


def test_roster_evidence_for_a_vertical_list_includes_the_source_lines():
    rows = parse_roster("PRESENT:\nAlice Smith\nBob Jones\nPUBLIC COMMENT\n")
    assert [row["name"] for row in rows] == ["Alice Smith", "Bob Jones"]
    assert all("Alice Smith" in row["evidence"] and "Bob Jones" in row["evidence"] for row in rows)
    assert all("PUBLIC COMMENT" not in row["evidence"] for row in rows)
    inline = parse_roster("PRESENT: Alice Smith, Bob Jones\nABSENT: None\n")
    assert inline[0]["evidence"] == "PRESENT: Alice Smith, Bob Jones"


def test_prose_window_anchors_on_a_real_call_to_order():
    front = "A motion to order supplies was filed. " + "Background item text. " * 150
    text = (
        front
        + "Chair Ann Lee called the meeting to order at 7:00pm. Commissioners Bob Roe and Cy Poe "
        "were in attendance."
    )
    assert _names(parse_roster(text)) == ["Bob Roe", "Cy Poe", "Ann Lee"]
    heading = "Call to Order1. Chair Ann Lee opened it. Commissioners Bob Roe were in attendance."
    assert "Bob Roe" in _names(parse_roster(heading))


def test_prose_roster_checks_a_guest_label_that_encloses_a_qualified_list():
    text = (
        "Chair Ann Lee called the meeting to order. Guests: Commissioners in attendance: Jane "
        "Doe and Bob Roe."
    )
    assert _names(parse_roster(text)) == ["Ann Lee"]
    plain = (
        "Chair Ann Lee called the meeting to order. Commissioners in attendance: Jane Doe and "
        "Bob Roe."
    )
    assert sorted(_names(parse_roster(plain))) == ["Ann Lee", "Bob Roe", "Jane Doe"]


def test_minutes_ocr_keeps_a_native_roster_the_scan_body_missed(monkeypatch):
    import citypods.agenda_text as agenda_text

    monkeypatch.setattr(
        agenda_text, "extract_document", lambda *a, **k: ("PRESENT: Mayor Ann Lee, Bob Roe", [])
    )
    monkeypatch.setattr(agenda_text, "_pdf_page_count", lambda content: 3)

    def runner(content, pages, *, timeout, preserve_lines):
        return "Minutes of the regular meeting. " * 30, 95.0

    text, _, method = agenda_text.extract_minutes_document(
        _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
    )
    assert method == "ocr"
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe"]
    assert "Minutes of the regular meeting." in text


def test_structured_roster_does_not_enrol_a_staff_titled_entry_under_a_member_heading():
    text = "PRESENT:\nAlice Smith\nCity Attorney Bob Roe\nABSENT: None\n"
    assert _names(parse_roster(text)) == ["Alice Smith"]
    inline = "PRESENT: Alice Smith, Deputy City Manager Cy Poe, Di Moe\n"
    assert _names(parse_roster(inline)) == ["Alice Smith", "Di Moe"]
    staff = "ALSO PRESENT: City Attorney Bob Roe\n"
    assert [r["name"] for r in parse_roster(staff) if r.get("section") == "staff"] == ["Bob Roe"]


def test_an_all_caps_given_name_is_not_a_staff_acronym():
    text = (
        "Chair Ann Lee called the meeting to order. Commissioners ALICE SMITH, MARK MANAGER, and "
        "BOB ROE were present."
    )
    assert _names(parse_roster(text)) == ["ALICE SMITH", "MARK MANAGER", "BOB ROE", "Ann Lee"]


def test_roster_keeps_reading_an_all_caps_line_after_a_comma_in_a_mixed_case_list():
    text = "PRESENT:\nAlice Smith,\nBOB JONES, CAROL BROWN\nABSENT: None\n"
    assert _names(parse_roster(text)) == ["Alice Smith", "BOB JONES", "CAROL BROWN"]


def test_a_boards_own_secretary_is_a_member_but_a_city_secretary_is_staff():
    text = (
        "ABSENT: Chair Ann Lee, Secretary Thomas Plock, Bob Roe\n"
        "PRESENT: City Secretary Di Moe, Cy Poe\n"
    )
    rows = parse_roster(text)
    assert _names(rows, "absent") == ["Ann Lee", "Thomas Plock", "Bob Roe"]
    assert _names(rows) == ["Cy Poe"]


def test_minutes_ocr_keeps_a_native_member_roster_when_ocr_finds_only_staff(monkeypatch):
    import citypods.agenda_text as agenda_text

    monkeypatch.setattr(
        agenda_text, "extract_document", lambda *a, **k: ("PRESENT: Mayor Ann Lee, Bob Roe", [])
    )
    monkeypatch.setattr(agenda_text, "_pdf_page_count", lambda content: 3)

    def runner(content, pages, *, timeout, preserve_lines):
        return "STAFF PRESENT: Jane Doe\n" + "Minutes of the regular meeting. " * 30, 95.0

    text, _, method = agenda_text.extract_minutes_document(
        _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
    )
    assert method == "ocr"
    members = [r["name"] for r in parse_roster(text) if r.get("section") != "staff"]
    assert members == ["Ann Lee", "Bob Roe"]


def test_a_cached_extract_below_the_cached_floor_is_sparse_but_a_fresh_one_may_not_be():
    from citypods.agenda_text import minutes_text_is_sparse

    one_text_page = "The council discussed the budget and approved the consent agenda. " * 5
    assert not minutes_text_is_sparse(one_text_page, 1)  # a real one-page document
    assert minutes_text_is_sparse(one_text_page, 5)  # the same text spread over a 5-page scan
    assert minutes_text_is_sparse(one_text_page, cached=True)  # page count lost: re-fetch
    real = "The council discussed the budget and approved the consent agenda. " * 40
    assert not minutes_text_is_sparse(real, cached=True)


def test_prose_presider_does_not_swallow_the_header_line_above_it():
    text = (
        "City Council Minutes\nMayor Jane Doe called the meeting to order at 7:00pm. "
        "Councilmembers Ann Lee and Bob Roe were in attendance.\n"
    )
    assert _names(parse_roster(text)) == ["Ann Lee", "Bob Roe", "Jane Doe"]
    wrapped = (
        "City of Springfield Minutes\nMayor Pro Tem\nKimberly Holiday called the meeting to "
        "order. Councilmembers Ann Lee were in attendance."
    )
    assert "Kimberly Holiday" in _names(parse_roster(wrapped))


def test_roster_does_not_read_a_motion_result_as_a_continued_name():
    text = "PRESENT: Alice Smith,\nMotion Carried.\nABSENT: None\n"
    assert _names(parse_roster(text)) == ["Alice Smith"]
    vertical = "PRESENT:\nAlice Smith\nMotion Carried\n"
    assert _names(parse_roster(vertical)) == ["Alice Smith"]


def test_directors_stay_members_under_an_explicit_member_label():
    text = "Directors Present: Director Alice Smith, Bob Roe\nAlso Present: City Attorney Cy Poe\n"
    rows = parse_roster(text)
    assert [r["name"] for r in rows if r.get("section") == "members"] == ["Alice Smith", "Bob Roe"]
    unlabeled = "PRESENT: City Attorney Cy Poe, Di Moe\n"
    assert _names(parse_roster(unlabeled)) == ["Di Moe"]


def test_minutes_ocr_keeps_native_members_that_ocr_only_partly_recovered(monkeypatch):
    import citypods.agenda_text as agenda_text

    monkeypatch.setattr(
        agenda_text,
        "extract_document",
        lambda *a, **k: ("PRESENT: Alice Smith, Bob Roe", []),
    )
    monkeypatch.setattr(agenda_text, "_pdf_page_count", lambda content: 3)

    def runner(content, pages, *, timeout, preserve_lines):
        return "PRESENT: Alice Smith\n" + "Minutes of the regular meeting. " * 30, 95.0

    text, _, method = agenda_text.extract_minutes_document(
        _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
    )
    assert method == "ocr"
    assert _names(parse_roster(text)) == ["Alice Smith", "Bob Roe"]


def test_roster_stops_a_member_list_at_an_explicit_staff_label():
    text = "PRESENT: Alice Smith,\nStaff: Jane Doe\nABSENT: None\n"
    assert _names(parse_roster(text)) == ["Alice Smith"]
    vertical = "PRESENT:\nAlice Smith\nBob Jones\nAlso present: Jane Doe, Clerk\n"
    members = [r["name"] for r in parse_roster(vertical) if r.get("section") != "staff"]
    assert members == ["Alice Smith", "Bob Jones"]  # Jane Doe is read, but as staff
    guests = "PRESENT: Alice Smith,\nGuests: Jane Doe\n"
    assert _names(parse_roster(guests)) == ["Alice Smith"]


def test_minutes_ocr_keeps_native_statuses_that_ocr_read_with_opposite_headings(monkeypatch):
    import citypods.agenda_text as agenda_text

    monkeypatch.setattr(
        agenda_text,
        "extract_document",
        lambda *a, **k: ("PRESENT: Alice Smith\nABSENT: Bob Jones", []),
    )
    monkeypatch.setattr(agenda_text, "_pdf_page_count", lambda content: 3)

    def runner(content, pages, *, timeout, preserve_lines):
        return "PRESENT: Bob Jones\nABSENT: Alice Smith\n" + "Minutes of the meeting. " * 40, 95.0

    text, _, _ = agenda_text.extract_minutes_document(
        _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
    )
    assert text.startswith("PRESENT: Alice Smith")  # the native facts are kept, not replaced


def test_minutes_ocr_keeps_a_native_vote_line_ocr_missed(monkeypatch):
    import citypods.agenda_text as agenda_text

    native = "Present: Alice Smith, Bob Jones\nVote: Yes: Alice Smith; No: Bob Jones"
    monkeypatch.setattr(agenda_text, "extract_document", lambda *a, **k: (native, []))
    monkeypatch.setattr(agenda_text, "_pdf_page_count", lambda content: 3)

    def runner(content, pages, *, timeout, preserve_lines):
        return "Present: Alice Smith, Bob Jones\n" + "Minutes of the meeting. " * 40, 95.0

    text, _, method = agenda_text.extract_minutes_document(
        _SCANNED_PDF, content_type="application/pdf", ocr_runner=runner
    )
    assert method == "ocr"
    votes = {(v["member"], v["value"]) for v in agenda_text.parse_votes(text)}
    assert ("Alice Smith", "yes") in votes and ("Bob Jones", "no") in votes
