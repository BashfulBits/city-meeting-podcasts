#!/usr/bin/env python
"""Measure Granicus behavior across repeated requests, transfer volume, and cooldown.

This manual probe intentionally runs outside production Audio. It uses unsigned, stable archive
paths only, writes redacted JSON (host/path, never query strings), and keeps every fetch bounded.
The default set combines clips that succeeded in Audio #33 with the exact Fort Worth archive
objects that triggered Audio #37's circuit.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from citypods.http import USER_AGENT
from citypods.security import validate_source_url

ALLOWED_HOSTS = ("*.granicus.com",)

DEFAULT_CLIPS = (
    (
        "arlington-known-good",
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_51a84e48-2b01-4d82-8c44-fb5502a985b0.mp4",
    ),
    (
        "pflugerville-known-good",
        "https://archive-video.granicus.com/pflugerville/"
        "pflugerville_6ab2ea93-3e15-449b-8ce4-37c5e5f90a2f.mp4",
    ),
    (
        "fort-worth-audio37-a",
        "https://archive-video.granicus.com/fortworthgov/"
        "fortworthgov_e4cc067f-6b2d-11f1-9494-005056a89546.mp4",
    ),
    (
        "fort-worth-audio37-b",
        "https://archive-video.granicus.com/fortworthgov/"
        "fortworthgov_993eba4a-6b2d-11f1-9494-005056a89546.mp4",
    ),
    (
        "fort-worth-audio37-c",
        "https://archive-video.granicus.com/fortworthgov/"
        "fortworthgov_dba6b50a-d6c6-4dab-b4c1-9ed29c2dbf34.mp4",
    ),
)


@dataclass
class ProbeResult:
    sequence: int
    phase: str
    clip: str
    host: str
    path: str
    requested_seconds: float
    started_at: str
    elapsed_seconds: float
    output_bytes: int
    ok: bool
    outcome: str
    returncode: int
    stderr_tail: str


def _outcome(returncode: int, stderr: str, output_bytes: int) -> str:
    text = stderr.lower()
    if returncode == 0 and output_bytes > 0:
        return "success"
    if "403 forbidden" in text or "server returned 403" in text or "http error 403" in text:
        return "http_403"
    if "429 too many" in text or "server returned 429" in text or "http error 429" in text:
        return "http_429"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "invalid data" in text or "moov atom not found" in text:
        return "invalid"
    return "error"


def run_probe(
    *,
    sequence: int,
    phase: str,
    clip: str,
    url: str,
    ffmpeg: str,
    seconds: float,
    timeout: float,
) -> ProbeResult:
    validate_source_url(url, allowed_hosts=ALLOWED_HOSTS)
    parts = urlsplit(url)
    started_wall = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="granicus-sustained-") as tmp:
        output = Path(tmp) / "sample.mka"
        cmd = [
            ffmpeg,
            "-y",
            "-nostats",
            "-loglevel",
            "error",
            "-user_agent",
            USER_AGENT,
            "-rw_timeout",
            "120000000",
            "-i",
            url,
            "-t",
            str(seconds),
            "-vn",
            "-c:a",
            "copy",
            "-f",
            "matroska",
            str(output),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
            returncode = proc.returncode
            stderr = " ".join(proc.stderr.strip().split())
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stderr = f"timeout after {timeout}s: {exc}"
        elapsed = time.monotonic() - started
        output_bytes = output.stat().st_size if output.exists() else 0
    outcome = _outcome(returncode, stderr, output_bytes)
    return ProbeResult(
        sequence=sequence,
        phase=phase,
        clip=clip,
        host=parts.hostname or "",
        path=parts.path,
        requested_seconds=seconds,
        started_at=started_wall,
        elapsed_seconds=round(elapsed, 3),
        output_bytes=output_bytes,
        ok=outcome == "success",
        outcome=outcome,
        returncode=returncode,
        stderr_tail=stderr[-500:],
    )


def _parse_clip(raw: str) -> tuple[str, str]:
    try:
        name, url = raw.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("clip must be NAME=https://host/path") from exc
    validate_source_url(url, allowed_hosts=ALLOWED_HOSTS)
    return name, url


def _parse_durations(raw: str) -> list[float]:
    values = [float(value) for value in raw.split(",") if value.strip()]
    if not values or any(value <= 0 for value in values):
        raise argparse.ArgumentTypeError("durations must be positive comma-separated seconds")
    return values


def _emit(result: ProbeResult) -> None:
    print(
        f"{'PASS' if result.ok else 'FAIL'} sequence={result.sequence} phase={result.phase} "
        f"clip={result.clip} host={result.host} path={result.path} "
        f"requested_seconds={result.requested_seconds:g} elapsed={result.elapsed_seconds:.3f} "
        f"bytes={result.output_bytes} outcome={result.outcome}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--clip", action="append", type=_parse_clip, dest="clips")
    parser.add_argument("--repeat-count", type=int, default=8)
    parser.add_argument("--repeat-seconds", type=float, default=5.0)
    parser.add_argument("--durations", type=_parse_durations, default=[30.0, 120.0, 600.0])
    parser.add_argument("--cooldown-seconds", type=float, default=1800.0)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--output", type=Path, default=Path("granicus-sustained-results.json"))
    args = parser.parse_args()
    if args.repeat_count < 0 or args.concurrency < 1 or args.cooldown_seconds < 0:
        parser.error("repeat-count/cooldown must be non-negative and concurrency must be positive")
    if args.timeout <= 0 or args.repeat_seconds <= 0:
        parser.error("timeout and repeat-seconds must be positive")

    clips = args.clips or list(DEFAULT_CLIPS)
    results: list[ProbeResult] = []
    sequence = 0

    def probe(phase: str, clip: tuple[str, str], seconds: float) -> ProbeResult:
        nonlocal sequence
        sequence += 1
        result = run_probe(
            sequence=sequence,
            phase=phase,
            clip=clip[0],
            url=clip[1],
            ffmpeg=args.ffmpeg,
            seconds=seconds,
            timeout=args.timeout,
        )
        results.append(result)
        _emit(result)
        return result

    # Request-count pressure: round-robin prevents one tenant from consuming the whole serial phase.
    for _ in range(args.repeat_count):
        for clip in clips:
            probe("serial-repeat", clip, args.repeat_seconds)

    # Byte/transfer-duration pressure. Stop escalating one clip after its first failure so the probe
    # measures a boundary without needlessly hammering a throttled endpoint.
    for clip in clips:
        for duration in args.durations:
            if not probe("volume-ramp", clip, duration).ok:
                break

    if args.cooldown_seconds:
        print(f"COOLDOWN seconds={args.cooldown_seconds:g}", flush=True)
        time.sleep(args.cooldown_seconds)
        for clip in clips:
            probe("post-cooldown", clip, args.repeat_seconds)

    # Concurrency comes last so serial request/volume evidence remains interpretable.
    if args.concurrency > 1:
        selected = clips[: args.concurrency]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = []
            for clip in selected:
                sequence += 1
                futures.append(
                    pool.submit(
                        run_probe,
                        sequence=sequence,
                        phase=f"concurrency-{args.concurrency}",
                        clip=clip[0],
                        url=clip[1],
                        ffmpeg=args.ffmpeg,
                        seconds=args.repeat_seconds,
                        timeout=args.timeout,
                    )
                )
            for future in futures:
                result = future.result()
                results.append(result)
                _emit(result)

    summary = {
        "results": [asdict(result) for result in results],
        "summary": {
            "cases": len(results),
            "successes": sum(result.ok for result in results),
            "outcomes": {
                outcome: sum(result.outcome == outcome for result in results)
                for outcome in sorted({result.outcome for result in results})
            },
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(f"RESULTS path={args.output} cases={len(results)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
