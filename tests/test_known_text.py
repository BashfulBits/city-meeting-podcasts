from __future__ import annotations

from citypods.known_text import (
    PROVIDER_ALIGN_PIPELINE_VERSION,
    clean_provider_text,
    provider_sections,
)
from citypods.timeline import Segment, Timeline


def test_provider_sections_remove_bracketed_markers_and_keep_windows():
    text = """[00:00:10]
    THE AGENDA
    Council approves the consent agenda unanimously.
    [00:00:20]
    Mayor thanks everyone for attending.
    """
    sections = provider_sections(text, duration=30)
    assert [section["start"] for section in sections] == [10.0, 20.0]
    assert "[00:00:10]" not in sections[0]["text"]
    assert "THE AGENDA" not in sections[0]["text"]
    assert "Council approves" in sections[0]["text"]


def test_provider_sections_accept_inline_bracketed_markers():
    sections = provider_sections(
        "[00:00:10] Council opens the meeting.\n[00:00:20] Council adjourns.", duration=30
    )
    assert [(section["start"], section["end"]) for section in sections] == [
        (10.0, 20.0),
        (20.0, 30),
    ]
    assert [section["text"] for section in sections] == [
        "Council opens the meeting.",
        "Council adjourns.",
    ]


def test_provider_align_version_is_explicit():
    assert PROVIDER_ALIGN_PIPELINE_VERSION == "5"


def test_clean_provider_text_falls_back_for_plain_transcript():
    text = "Hello everyone. We are beginning the meeting today."
    assert clean_provider_text(text) == text


def test_clean_provider_text_preserves_short_speech_and_unmatched_brackets():
    text = """[00:00:01]
    AGENDA
    Motion carries.
    All in favor?
    [malformed note
    The vote is unanimous.
    """
    cleaned = clean_provider_text(text)
    assert "Motion carries." in cleaned
    assert "All in favor?" in cleaned
    assert "The vote is unanimous." in cleaned
    assert "AGENDA" not in cleaned


def test_provider_markers_use_source_clock_before_served_remap():
    timeline = Timeline(
        version="cut",
        segments=(
            Segment(0, 10, "source", "s0", 0, 10),
            Segment(10, 20, "source", "s0", 30, 40),
        ),
    )
    text = "[00:00:00]\nFirst spoken section here.\n[00:00:30]\nSecond spoken section here."
    sections = provider_sections(text, duration=20, timeline=timeline)
    assert [(section["start"], section["end"]) for section in sections] == [(0, 10), (10, 20)]


def test_unknown_duration_keeps_open_final_section_for_audio_bounding():
    sections = provider_sections("[00:00:05]\nThe meeting begins now.")
    assert sections == [{"start": 5.0, "end": None, "text": "The meeting begins now."}]
