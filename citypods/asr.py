"""ASR (Automatic Speech Recognition) helpers for transcript generation.

Two public entry points:

  transcribe(audio_path, ...)  — Path B: fresh Whisper generation.
  align(audio_path, text, ...) — Path A: forced alignment via stable-ts, preserving
                                  the exact wording of an existing source transcript.

Both return WebVTT bytes in *served time* (caller downloads the hosted M4A before calling).

All third-party imports (faster_whisper, stable_whisper) are lazy — the module loads
cleanly without the [asr] optional extras; a missing import surfaces only when
transcribe() / align() is actually called.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# ── VTT helpers ─────────────────────────────────────────────────────────────


def _fmt_ts(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm for WebVTT cue timestamps."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _to_vtt(segments) -> bytes:
    """Convert faster-whisper segment iterable to WebVTT bytes."""
    lines = ["WEBVTT", ""]
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).encode("utf-8")


# ── Public API ───────────────────────────────────────────────────────────────


def load_model(model: str, compute_type: str, cpu_threads: int):
    """Load and return a faster-whisper ``WhisperModel``.

    Callers should load the model **once** (outside the episode loop) and pass it
    to :func:`transcribe` / :func:`align` to avoid re-downloading weights on every
    episode and to surface model-loading failures (e.g. HuggingFace Hub rate-limits)
    as a single batch-level error rather than one per episode.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster-whisper is required for ASR. Install it with: pip install 'citypods[asr]'"
        ) from exc

    return WhisperModel(model, device="cpu", compute_type=compute_type, cpu_threads=cpu_threads)


def transcribe(
    audio_path: Path,
    model_or_name: object,
    language: str | None,
    compute_type: str,
    beam_size: int,
    initial_prompt: str | None,
    cpu_threads: int,
) -> bytes:
    """Run faster-whisper transcription on *audio_path*; return WebVTT bytes.

    *model_or_name* may be a pre-loaded ``WhisperModel`` instance (preferred — avoids
    reloading weights on every call) or a model-name string (loads inline, kept for
    backward compatibility with tests that pass a name directly).
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster-whisper is required for ASR transcription. "
            "Install it with: pip install 'citypods[asr]'"
        ) from exc

    if isinstance(model_or_name, str):
        wm = WhisperModel(
            model_or_name,
            device="cpu",
            compute_type=compute_type,
            cpu_threads=cpu_threads,
        )
    else:
        wm = model_or_name  # pre-loaded instance

    segments, _ = wm.transcribe(
        str(audio_path),
        language=language or None,
        beam_size=beam_size,
        initial_prompt=initial_prompt or None,
    )
    return _to_vtt(segments)


def align(
    audio_path: Path,
    text: str,
    model_or_name: object,
    language: str | None,
    cpu_threads: int,
) -> bytes:
    """Force-align *text* to *audio_path* using stable-ts; return WebVTT bytes.

    *model_or_name* may be a pre-loaded stable-ts model instance or a name string.
    Preserves the official transcript wording exactly; faster than generation.
    """
    try:
        import stable_whisper
    except ImportError as exc:
        raise ImportError(
            "stable-ts is required for forced alignment. "
            "Install it with: pip install 'citypods[asr]'"
        ) from exc

    if isinstance(model_or_name, str):
        wm = stable_whisper.load_faster_whisper(model_or_name, cpu_threads=cpu_threads)
    else:
        wm = model_or_name  # pre-loaded instance

    result = wm.align(str(audio_path), text, language=language or "en")
    vtt_str: str = result.to_vtt()
    if not vtt_str.startswith("WEBVTT"):
        vtt_str = "WEBVTT\n\n" + vtt_str
    return vtt_str.encode("utf-8")


# ── Spec hash ────────────────────────────────────────────────────────────────


def asr_spec_hash(
    audio_spec_hash: str,
    model: str,
    align_text_hash: str | None,
    version: str,
) -> str:
    """Recipe hash for an ASR transcript: changes when audio, model, or source text changes.

    Keyed on *inputs* (not output bytes) so the storage key can be computed before
    running inference, enabling a cheap ``_present(key)`` reuse check.

    ``align_text_hash`` is the SHA-1 prefix of the source text used for alignment
    (Path A); ``None`` means fresh transcription (Path B).
    """
    spec = {
        "v": version,
        "audio": audio_spec_hash,
        "model": model,
        "align": align_text_hash,
    }
    blob = json.dumps(spec, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()[:12]


# ── Text extraction from VTT/SRT (for bench WER) ────────────────────────────


def vtt_to_text(vtt: str) -> str:
    """Strip WebVTT cue headers and return concatenated cue text (for WER comparison)."""
    lines = []
    in_cue = False
    for line in vtt.splitlines():
        line = line.strip()
        if not line:
            in_cue = False
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if "-->" in line:
            in_cue = True
            continue
        if in_cue and line:
            lines.append(line)
    return " ".join(lines)


def srt_to_text(srt: str) -> str:
    """Strip SRT cue headers and return concatenated cue text (for WER comparison)."""
    import re

    lines = []
    for line in srt.splitlines():
        line = line.strip()
        if not line or line.isdigit():
            continue
        if re.match(r"\d{2}:\d{2}:\d{2},\d{3}\s*-->", line):
            continue
        lines.append(line)
    return " ".join(lines)
