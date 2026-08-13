"""Shared known-text alignment job dispatch for local compute adapters."""

from __future__ import annotations


def run_alignment(asr, inputs: dict):
    """Run one ``align`` job, retaining the narrow legacy-double compatibility path."""
    align_known_text = getattr(asr, "align_known_text", None)
    sections = inputs["sections"]
    if callable(align_known_text):
        kwargs = {}
        if "interpolate_method" in inputs:
            kwargs["interpolate_method"] = inputs["interpolate_method"]
        return align_known_text(
            inputs["audio_path"],
            sections,
            inputs["model"],
            inputs["language"],
            inputs["cpu_threads"],
            **kwargs,
        )
    text = " ".join(str(section.get("text") or "") for section in sections)
    return asr.align(
        inputs["audio_path"], text, inputs["model"], inputs["language"], inputs["cpu_threads"]
    )
