#!/usr/bin/env python3
"""Per-source output-drift smoke for output-affecting dependency bumps (review/22).

Runs one short golden clip from every provider family (granicus, swagit, civicplus,
civicclerk) through the ffmpeg-driven materialization metrics — encode SHA-256,
duration, integrated loudness — and diffs against the committed golden manifest. The
point is to prove, per source, whether an ffmpeg / base-image bump actually changed
produced bytes — so the reviewer's whole job is reading the emitted table.

The ASR column is a planned extension (a whisper transcribe + WER diff for
inference-lib/model bumps); it currently reports ``n/a`` rather than a blank cell so a
not-yet-implemented check is never mistaken for "checked, no drift".

Layout (fixtures are added out-of-band; this is tolerant of their absence so the
workflow can be wired before they land):

    tests/fixtures/smoke/<family>/clip.<ext>      # short input media, one per family
    tests/fixtures/smoke/golden.json              # {family: {sha256,duration_s,lufs,...}}

Writes a GitHub-flavored Markdown table to the path in $SMOKE_TABLE_OUT (default
smoke-table.md). Exit code is always 0 — this is an informational gate whose output
drives a human decision; it does not fail the build.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SMOKE_DIR = ROOT / "tests" / "fixtures" / "smoke"
GOLDEN = SMOKE_DIR / "golden.json"
FAMILIES = ("granicus", "swagit", "civicplus", "civicclerk")
OUT = Path(os.environ.get("SMOKE_TABLE_OUT", "smoke-table.md"))


def _find_clip(family: str) -> Path | None:
    d = SMOKE_DIR / family
    if not d.is_dir():
        return None
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix.lower() in {".mp3", ".m4a", ".mp4", ".wav", ".ts", ".aac"}:
            return p
    return None


def _ffmpeg() -> str:
    return os.environ.get("FFMPEG_BIN", "ffmpeg")


def _ffprobe() -> str:
    """Resolve ffprobe next to the pinned ffmpeg binary, so both come from the same build."""
    ffmpeg = _ffmpeg()
    if os.sep in ffmpeg or (os.altsep and os.altsep in ffmpeg):
        sibling = Path(ffmpeg).with_name("ffprobe")
        if sibling.exists():
            return str(sibling)
    return "ffprobe"


def _encode_metrics(clip: Path) -> dict:
    """Re-encode to a normalized AAC m4a (as the audio lane does) and measure it."""
    tmp = clip.with_suffix(".smoke.m4a")
    try:
        subprocess.run(
            [
                _ffmpeg(),
                "-y",
                "-i",
                str(clip),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                str(tmp),
            ],
            check=True,
            capture_output=True,
        )
        data = tmp.read_bytes()
        sha = hashlib.sha256(data).hexdigest()[:16]
        probe = subprocess.run(
            [
                _ffprobe(),
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(tmp),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        duration = round(float(json.loads(probe.stdout)["format"]["duration"]), 2)
        lufs = _integrated_loudness(tmp)
    finally:
        tmp.unlink(missing_ok=True)  # never leak the temp encode, even if ffprobe fails
    return {"sha256": sha, "duration_s": duration, "lufs": lufs}


def _integrated_loudness(path: Path) -> float | None:
    proc = subprocess.run(
        [_ffmpeg(), "-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', proc.stderr)
    return round(float(m.group(1)), 2) if m else None


def main() -> int:
    golden = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    rows: list[str] = []
    any_changed = False
    any_fixture = False

    for fam in FAMILIES:
        clip = _find_clip(fam)
        if clip is None:
            rows.append(f"| {fam} | _no fixture yet_ | — | n/a | ⏳ pending |")
            continue
        any_fixture = True
        try:
            got = _encode_metrics(clip)
        except (subprocess.CalledProcessError, OSError) as exc:
            # Never fail the step (docstring guarantees exit 0) — a missing/broken ffmpeg
            # binary or a probe failure surfaces as an error row, not a crash.
            stderr = getattr(exc, "stderr", None)
            rows.append(f"| {fam} | ffmpeg/ffprobe error | — | n/a | ✗ error |")
            print(stderr.decode(errors="replace") if isinstance(stderr, bytes) else exc)
            continue
        ref = golden.get(fam)
        if ref is None:
            rows.append(
                f"| {fam} | `{got['sha256']}` | {got['duration_s']}s / {got['lufs']} LUFS "
                f"| n/a | 🆕 baseline |"
            )
            continue
        changed = any(got.get(k) != ref.get(k) for k in ("sha256", "duration_s", "lufs"))
        any_changed = any_changed or changed
        verdict = "⚠️ CHANGED" if changed else "✅ unchanged"
        detail = (
            f"{ref.get('duration_s')}→{got['duration_s']}s / {ref.get('lufs')}→{got['lufs']} LUFS"
            if changed
            else f"{got['duration_s']}s / {got['lufs']} LUFS"
        )
        rows.append(
            f"| {fam} | `{ref.get('sha256')}`→`{got['sha256']}` | {detail} | n/a | {verdict} |"
        )

    header = (
        "### Dependency-bump output-drift smoke (per source)\n\n"
        f"ffmpeg: `{_ffmpeg()}`\n\n"
        "| Source | encode SHA-256 (16) | duration / loudness | ASR | verdict |\n"
        "|---|---|---|---|---|\n"
    )
    footer = ""
    if not any_fixture:
        footer = (
            "\n> ⏳ **Golden smoke fixtures not committed yet.** Add one short clip per family "
            "under `tests/fixtures/smoke/<family>/` and a `golden.json` baseline. Until then this "
            "is a wiring placeholder (see review/22)."
        )
    elif any_changed:
        footer = (
            "\n> ⚠️ **Output changed for at least one source.** Per the `AGENTS.md` bump "
            "contract, this PR must either bump the relevant pipeline version + state the backfill "
            "story, or hold. If the change is intended, regenerate `golden.json` in this PR."
        )
    else:
        footer = "\n> ✅ **No output drift** — safe to merge as a reproducibility bump."

    OUT.write_text(header + "\n".join(rows) + "\n" + footer + "\n")
    print(OUT.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
