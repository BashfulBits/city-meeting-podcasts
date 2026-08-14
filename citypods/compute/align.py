"""Shared known-text alignment job dispatch for local compute adapters."""

from __future__ import annotations


def run_alignment(asr, inputs: dict):
    """Run one ``align`` job, retaining the narrow legacy-double compatibility path."""
    align_known_text = getattr(asr, "align_known_text", None)
    sections = inputs.get("sections")
    if sections is None:
        # The external provider-align worker calls these ``timed_segments`` because that is the
        # name used by the provider-ingestion seam. They are already clean, served-time
        # WhisperX sections; do not discard them before entering the process-local backend.
        sections = inputs.get("timed_segments")
    if sections is not None and callable(align_known_text):
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
    text = inputs.get("text") or " ".join(
        str(section.get("text") or "") for section in sections or []
    )
    align_kwargs = {}
    if inputs.get("timed_segments") is not None:
        align_kwargs["timed_segments"] = inputs["timed_segments"]
    return asr.align(
        inputs["audio_path"],
        text,
        inputs["model"],
        inputs["language"],
        inputs["cpu_threads"],
        inputs.get("compute_type", "int8"),
        **align_kwargs,
    )
