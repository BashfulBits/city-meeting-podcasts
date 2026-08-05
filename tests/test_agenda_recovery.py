"""Tests for the read-only rejected-agenda recovery audit."""

from scripts.research.agenda_chapters.audit_agenda_recovery import (
    _subsequence_span,
    find_exact_spans,
    reference_kind,
    resolve_reference,
    tightest_spans,
)


def test_find_exact_spans_maps_multiline_quote_to_source_lines():
    lines = ["II.", "C.", "Long action", "continued evidence"]
    assert find_exact_spans(lines, "Long action continued evidence") == [(3, 4)]


def test_tightest_spans_reports_ambiguous_source_matches():
    assert tightest_spans([(2, 2), (4, 4), (1, 3)]) == [(2, 2), (4, 4)]


def test_hierarchical_reference_can_be_resolved_from_prefix_lines():
    lines = ["II.", "C.", "2.", "Action text"]
    result = resolve_reference("II.C.2", lines, line_start=4, line_end=4)
    assert result["resolved"] is True
    assert result["method"] == "hierarchical_prefix"


def test_descriptive_reference_is_not_treated_as_formal_identifier():
    assert reference_kind("Replat - GSID") == "descriptive_label"
    result = resolve_reference("Replat - GSID", ["Action text"], line_start=1, line_end=1)
    assert result["resolved"] is False
    assert result["method"] == "descriptive_not_identifier"


def test_common_case_and_hierarchical_ids_are_formal_references():
    assert reference_kind("SUP14-6") == "formal"
    assert reference_kind("IX.a") == "formal"


def test_subsequence_span_flags_omitted_parenthetical_source_text():
    lines = [
        "Specific Use Permit SUP14-6",
        "(Aloft-Arlington by W Hotels)",
        "Application for approval of a Specific Use Permit (SUP) for a Boutique Hotel.",
    ]
    quote = (
        "Specific Use Permit SUP14-6 Application for approval of a Specific Use Permit (SUP) "
        "for a Boutique Hotel."
    )
    spans = _subsequence_span(
        lines,
        quote,
        line_start=3,
        line_end=3,
    )
    assert spans == [(1, 3)]
