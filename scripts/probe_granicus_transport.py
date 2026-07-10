#!/usr/bin/env python
"""Compare curl and ffmpeg Granicus media admission on one GitHub-hosted runner.

The probe is intentionally low-volume and diagnostic. It pairs curl and ffmpeg against the same
stable archive objects, alternates which client goes first, and optionally proves the proposed
curl-download -> local ffprobe -> local ffmpeg path for a bounded number of objects.

Results redact query strings and retain only selected response metadata. The workflow that invokes
this script shares Audio's concurrency group, so production Audio cannot overlap the experiment.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from citypods.http import USER_AGENT
from citypods.security import redact_subprocess_text, validate_source_url

ALLOWED_HOSTS = ("*.granicus.com",)
DEFAULT_RANGE_BYTES = 16 * 1024 * 1024
DEFAULT_FULL_DOWNLOAD_MAX_BYTES = 512 * 1024 * 1024

# Exact archive objects seen in Audio #40, plus one historically successful control from Audio #33.
DEFAULT_CLIPS = (
    (
        "arlington-audio40",
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_f65c7a2f-9c73-4d9b-b7b7-205f7c12c0bf.mp4",
    ),
    (
        "pflugerville-audio40",
        "https://archive-video.granicus.com/pflugerville/"
        "pflugerville_49f62777-4364-465e-91ea-07745c6c33ce.mp4",
    ),
    (
        "fort-worth-audio40",
        "https://archive-video.granicus.com/fortworthgov/"
        "fortworthgov_e4cc067f-6b2d-11f1-9494-005056a89546.mp4",
    ),
    (
        "arlington-audio33-control",
        "https://archive-video.granicus.com/arlingtontx/"
        "arlingtontx_51a84e48-2b01-4d82-8c44-fb5502a985b0.mp4",
    ),
)

_HTTP_STATUS_RE = re.compile(
    r"(?:HTTP error|returned|error opening input: server returned)\s+(\d{3})", re.I
)
_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+|\*)", re.I)


@dataclass
class ProbeResult:
    sequence: int
    clip: str
    tenant: str
    transport: str
    request_shape: str
    order_in_pair: int | None
    started_at: str
    elapsed_seconds: float
    host: str
    path: str
    final_host: str
    final_path: str
    remote_ip: str
    http_status: int | None
    redirect_count: int | None
    content_type: str
    content_length: int | None
    content_range: str
    accept_ranges: str
    connect_seconds: float | None
    first_byte_seconds: float | None
    total_seconds: float | None
    output_bytes: int
    ok: bool
    outcome: str
    returncode: int
    ffprobe_ok: bool | None
    ffprobe_summary: str
    local_ffmpeg_ok: bool | None
    local_output_bytes: int
    stderr_tail: str


def _redacted_parts(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return parts.hostname or "", parts.path


def _tenant(url: str) -> str:
    host, path = _redacted_parts(url)
    if host == "archive-video.granicus.com":
        segments = [segment for segment in path.split("/") if segment]
        return segments[0] if segments else host
    if host.endswith(".granicus.com"):
        return host.removesuffix(".granicus.com")
    return host


def _stderr_tail(stderr: str) -> str:
    # CR2-SC-12: share the redaction implementation with probe_granicus_sustained.py (and the
    # rest of the codebase's subprocess-diagnostics redaction) instead of maintaining a second,
    # narrower bearer/URL-only regex pair that risks drifting from the shared one.
    redacted = redact_subprocess_text(stderr)
    return " ".join(str(redacted or "").strip().split())[-500:]


def _http_status(stderr: str) -> int | None:
    match = _HTTP_STATUS_RE.search(stderr)
    return int(match.group(1)) if match else None


def _outcome(returncode: int, stderr: str, output_bytes: int, http_status: int | None) -> str:
    text = stderr.lower()
    if http_status == 403 or "403 forbidden" in text:
        return "http_403"
    if http_status == 429 or "429 too many" in text:
        return "http_429"
    if returncode == 63:
        return "size_limit"
    if "timed out" in text or "timeout" in text or returncode == 28:
        return "timeout"
    if returncode == 0 and output_bytes > 0:
        return "success"
    if "invalid data" in text or "moov atom not found" in text:
        return "invalid"
    return "error"


def _parse_headers(path: Path) -> dict[str, str]:
    """Return selected headers from the final response block."""
    if not path.exists():
        return {}
    blocks = re.split(r"\r?\n\r?\n", path.read_text(errors="replace").strip())
    for block in reversed(blocks):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("HTTP/"):
            continue
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return headers
    return {}


def _as_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_float(raw: Any) -> float | None:
    try:
        return round(float(raw), 6)
    except (TypeError, ValueError):
        return None


def _total_size(result: ProbeResult) -> int | None:
    match = _CONTENT_RANGE_RE.fullmatch(result.content_range.strip())
    if match and match.group(1) != "*":
        return int(match.group(1))
    if result.http_status == 200:
        return result.content_length
    return None


def _base_result(
    *,
    sequence: int,
    clip: str,
    url: str,
    transport: str,
    request_shape: str,
    order_in_pair: int | None,
    started_at: str,
    elapsed: float,
) -> dict[str, Any]:
    host, path = _redacted_parts(url)
    return {
        "sequence": sequence,
        "clip": clip,
        "tenant": _tenant(url),
        "transport": transport,
        "request_shape": request_shape,
        "order_in_pair": order_in_pair,
        "started_at": started_at,
        "elapsed_seconds": round(elapsed, 3),
        "host": host,
        "path": path,
    }


def run_ffmpeg_remote(
    *,
    sequence: int,
    clip: str,
    url: str,
    ffmpeg: str,
    seconds: float,
    timeout: float,
    order_in_pair: int,
    authorization: str | None = None,
    allowed_hosts: tuple[str, ...] | None = ALLOWED_HOSTS,
    transport: str = "ffmpeg_remote",
    request_shape: str = "archive_browser_ua",
    source_identity_url: str | None = None,
) -> ProbeResult:
    validate_source_url(url, allowed_hosts=allowed_hosts)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="granicus-transport-ffmpeg-") as tmp:
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
            *(["-headers", f"Authorization: Bearer {authorization}\r\n"] if authorization else []),
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
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stderr = f"timeout after {timeout}s: {exc}"
        output_bytes = output.stat().st_size if output.exists() else 0
    elapsed = time.monotonic() - started
    status = _http_status(stderr)
    outcome = _outcome(returncode, stderr, output_bytes, status)
    return ProbeResult(
        **_base_result(
            sequence=sequence,
            clip=clip,
            url=source_identity_url or url,
            transport=transport,
            request_shape=request_shape,
            order_in_pair=order_in_pair,
            started_at=started_at,
            elapsed=elapsed,
        ),
        final_host=_redacted_parts(url)[0],
        final_path=_redacted_parts(url)[1],
        remote_ip="",
        http_status=status,
        redirect_count=None,
        content_type="",
        content_length=None,
        content_range="",
        accept_ranges="",
        connect_seconds=None,
        first_byte_seconds=None,
        total_seconds=None,
        output_bytes=output_bytes,
        ok=outcome == "success",
        outcome=outcome,
        returncode=returncode,
        ffprobe_ok=None,
        ffprobe_summary="",
        local_ffmpeg_ok=None,
        local_output_bytes=0,
        stderr_tail=_stderr_tail(stderr),
    )


def run_curl(
    *,
    sequence: int,
    clip: str,
    url: str,
    curl: str,
    request_shape: str,
    timeout: float,
    output: Path,
    max_bytes: int,
    byte_range: str | None,
    order_in_pair: int | None,
    browser_context: bool = False,
    authorization: str | None = None,
    allowed_hosts: tuple[str, ...] | None = ALLOWED_HOSTS,
    transport: str | None = None,
    source_identity_url: str | None = None,
) -> ProbeResult:
    validate_source_url(url, allowed_hosts=allowed_hosts)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    started = time.monotonic()
    headers_path = output.with_suffix(output.suffix + ".headers")
    cmd = [
        curl,
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--retry",
        "0",
        "--max-redirs",
        "0",
        "--connect-timeout",
        "20",
        "--max-time",
        str(timeout),
        "--max-filesize",
        str(max_bytes),
        "--user-agent",
        USER_AGENT,
        "--dump-header",
        str(headers_path),
        "--output",
        str(output),
        "--write-out",
        "%{json}",
    ]
    if byte_range:
        cmd.extend(["--range", byte_range])
    if authorization:
        cmd.extend(["--header", f"Authorization: Bearer {authorization}"])
    if browser_context:
        cmd.extend(
            ["--referer", "https://granicus.com/", "--header", "Origin: https://granicus.com"]
        )
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 10, check=False
        )
        returncode = proc.returncode
        stderr = proc.stderr
        try:
            metrics = json.loads(proc.stdout) if proc.stdout else {}
        except json.JSONDecodeError:
            metrics = {}
            stderr = f"{stderr} invalid curl metrics: {proc.stdout[-200:]}"
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stderr = f"timeout after {timeout + 10}s: {exc}"
        metrics = {}
    elapsed = time.monotonic() - started
    headers = _parse_headers(headers_path)
    output_bytes = output.stat().st_size if output.exists() else 0
    status = _as_int(metrics.get("http_code"))
    effective_url = str(metrics.get("url_effective") or url)
    final_host, final_path = _redacted_parts(effective_url)
    content_length = _as_int(headers.get("content-length"))
    outcome = _outcome(returncode, stderr, output_bytes, status)
    if outcome == "success" and byte_range and status == 200:
        outcome = "range_ignored"
    elif (
        outcome == "size_limit"
        and byte_range
        and status == 200
        and content_length is not None
        and content_length > max_bytes
    ):
        # curl rejects the response before writing it because Granicus ignored Range and announced
        # the entire (often GiB-sized) object. Authentication/access succeeded; only partial fetch
        # is unsupported for this object.
        outcome = "range_unsupported"
    return ProbeResult(
        **_base_result(
            sequence=sequence,
            clip=clip,
            url=source_identity_url or url,
            transport=transport or ("curl_full" if byte_range is None else "curl_range"),
            request_shape=request_shape,
            order_in_pair=order_in_pair,
            started_at=started_at,
            elapsed=elapsed,
        ),
        final_host=final_host,
        final_path=final_path,
        remote_ip=str(metrics.get("remote_ip") or ""),
        http_status=status,
        redirect_count=_as_int(metrics.get("num_redirects")),
        content_type=headers.get("content-type", str(metrics.get("content_type") or "")),
        content_length=content_length,
        content_range=headers.get("content-range", ""),
        accept_ranges=headers.get("accept-ranges", ""),
        connect_seconds=_as_float(metrics.get("time_connect")),
        first_byte_seconds=_as_float(metrics.get("time_starttransfer")),
        total_seconds=_as_float(metrics.get("time_total")),
        output_bytes=output_bytes,
        ok=outcome in {"success", "range_ignored", "range_unsupported"},
        outcome=outcome,
        returncode=returncode,
        ffprobe_ok=None,
        ffprobe_summary="",
        local_ffmpeg_ok=None,
        local_output_bytes=0,
        stderr_tail=_stderr_tail(stderr),
    )


def prove_local_processing(
    *,
    result: ProbeResult,
    source: Path,
    ffmpeg: str,
    ffprobe: str,
    timeout: float,
) -> ProbeResult:
    result.transport = "curl_full_ffmpeg_local"
    probe_cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name",
        "-of",
        "json",
        str(source),
    ]
    try:
        probed = subprocess.run(
            probe_cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        ffprobe_ok = probed.returncode == 0
    except subprocess.TimeoutExpired:
        probed = subprocess.CompletedProcess(probe_cmd, 124, "", "ffprobe timeout")
        ffprobe_ok = False
    result.ffprobe_summary = " ".join(probed.stdout.strip().split())[:500]

    output = source.with_suffix(".mka")
    encode_cmd = [
        ffmpeg,
        "-y",
        "-nostats",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-t",
        "30",
        "-vn",
        "-c:a",
        "copy",
        "-f",
        "matroska",
        str(output),
    ]
    try:
        encoded = subprocess.run(
            encode_cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        local_ffmpeg_ok = encoded.returncode == 0 and output.exists() and output.stat().st_size > 0
    except subprocess.TimeoutExpired:
        encoded = subprocess.CompletedProcess(encode_cmd, 124, "", "local ffmpeg timeout")
        local_ffmpeg_ok = False

    result.ffprobe_ok = ffprobe_ok
    result.local_ffmpeg_ok = local_ffmpeg_ok
    result.local_output_bytes = output.stat().st_size if output.exists() else 0
    result.ok = result.ok and ffprobe_ok and local_ffmpeg_ok
    if not ffprobe_ok:
        result.outcome = "local_ffprobe_failed"
    elif not local_ffmpeg_ok:
        result.outcome = "local_ffmpeg_failed"
    stderr = f"{result.stderr_tail} {probed.stderr} {encoded.stderr}".strip()
    result.stderr_tail = _stderr_tail(stderr)
    return result


def _parse_named_url(raw: str) -> tuple[str, str]:
    try:
        name, url = raw.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be NAME=https://host/path") from exc
    validate_source_url(url, allowed_hosts=ALLOWED_HOSTS)
    return name, url


def _mib_to_bytes(value: str | float) -> int:
    try:
        mib = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("MiB values must be numeric") from exc
    if not math.isfinite(mib) or mib <= 0:
        raise argparse.ArgumentTypeError("MiB values must be positive")
    return int(mib * 1024 * 1024)


def _nonnegative_integer(value: str | float) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("value must be a non-negative integer") from exc
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise argparse.ArgumentTypeError("value must be a non-negative integer")
    return int(number)


def _emit(result: ProbeResult) -> None:
    print(
        f"{'PASS' if result.ok else 'FAIL'} sequence={result.sequence} clip={result.clip} "
        f"transport={result.transport} shape={result.request_shape} "
        f"status={result.http_status or '-'} outcome={result.outcome} "
        f"elapsed={result.elapsed_seconds:.3f} bytes={result.output_bytes} "
        f"final={result.final_host}{result.final_path}",
        flush=True,
    )


def _version(binary: str) -> str:
    try:
        proc = subprocess.run(
            [binary, "--version" if Path(binary).name == "curl" else "-version"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return f"<unavailable: {exc}>"
    output = (proc.stdout or proc.stderr).splitlines()
    return output[0][:300] if output else "<unavailable: empty output>"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--curl", default="curl")
    parser.add_argument("--clip", action="append", type=_parse_named_url, dest="clips")
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--pair-delay-seconds", type=float, default=5.0)
    range_group = parser.add_mutually_exclusive_group()
    range_group.add_argument("--range-bytes", type=int, default=DEFAULT_RANGE_BYTES)
    range_group.add_argument(
        "--range-mib",
        type=_mib_to_bytes,
        dest="range_bytes",
        default=argparse.SUPPRESS,
    )
    full_group = parser.add_mutually_exclusive_group()
    full_group.add_argument(
        "--full-download-max-bytes",
        type=int,
        default=DEFAULT_FULL_DOWNLOAD_MAX_BYTES,
    )
    full_group.add_argument(
        "--full-download-max-mib",
        type=_mib_to_bytes,
        dest="full_download_max_bytes",
        default=argparse.SUPPRESS,
    )
    parser.add_argument("--full-download-count", type=_nonnegative_integer, default=1)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output", type=Path, default=Path("granicus-transport-results.json"))
    args = parser.parse_args()
    if (
        args.sample_seconds <= 0
        or args.pair_delay_seconds < 0
        or args.range_bytes <= 0
        or args.full_download_max_bytes <= 0
        or args.full_download_count < 0
        or args.timeout <= 0
    ):
        parser.error("durations/byte limits must be positive; delay/count must be non-negative")

    clips = args.clips or list(DEFAULT_CLIPS)

    results: list[ProbeResult] = []
    direct_curl: dict[str, ProbeResult] = {}
    direct_ffmpeg: dict[str, ProbeResult] = {}
    sequence = 0

    with tempfile.TemporaryDirectory(prefix="granicus-transport-") as tmp:
        tmpdir = Path(tmp)
        for index, (clip, url) in enumerate(clips):
            curl_first = index % 2 == 1
            transports = ("curl", "ffmpeg") if curl_first else ("ffmpeg", "curl")
            for order, transport in enumerate(transports, start=1):
                sequence += 1
                if transport == "ffmpeg":
                    result = run_ffmpeg_remote(
                        sequence=sequence,
                        clip=clip,
                        url=url,
                        ffmpeg=args.ffmpeg,
                        seconds=args.sample_seconds,
                        timeout=args.timeout,
                        order_in_pair=order,
                    )
                    direct_ffmpeg[clip] = result
                else:
                    result = run_curl(
                        sequence=sequence,
                        clip=clip,
                        url=url,
                        curl=args.curl,
                        request_shape="archive_browser_ua",
                        timeout=args.timeout,
                        output=tmpdir / f"{sequence}-range.bin",
                        max_bytes=args.range_bytes,
                        byte_range=f"0-{args.range_bytes - 1}",
                        order_in_pair=order,
                    )
                    direct_curl[clip] = result
                results.append(result)
                _emit(result)
                if args.pair_delay_seconds:
                    time.sleep(args.pair_delay_seconds)

            sequence += 1
            context_result = run_curl(
                sequence=sequence,
                clip=clip,
                url=url,
                curl=args.curl,
                request_shape="archive_browser_context",
                timeout=args.timeout,
                output=tmpdir / f"{sequence}-context.bin",
                max_bytes=args.range_bytes,
                byte_range=f"0-{args.range_bytes - 1}",
                order_in_pair=None,
                browser_context=True,
            )
            results.append(context_result)
            _emit(context_result)
            if args.pair_delay_seconds:
                time.sleep(args.pair_delay_seconds)

        full_downloads = 0
        full_download_skips: list[dict[str, Any]] = []
        full_candidates = sorted(
            clips,
            key=lambda item: not (direct_curl[item[0]].ok and not direct_ffmpeg[item[0]].ok),
        )
        for clip, url in full_candidates:
            if full_downloads >= args.full_download_count:
                break
            curl_result = direct_curl[clip]
            total_size = _total_size(curl_result)
            if not curl_result.ok:
                full_download_skips.append({"clip": clip, "reason": "curl_range_failed"})
                continue
            if total_size is None:
                full_download_skips.append({"clip": clip, "reason": "unknown_total_size"})
                continue
            if total_size > args.full_download_max_bytes:
                full_download_skips.append(
                    {
                        "clip": clip,
                        "reason": "too_large",
                        "total_bytes": total_size,
                        "limit_bytes": args.full_download_max_bytes,
                    }
                )
                continue

            sequence += 1
            source = tmpdir / f"{sequence}-full.mp4"
            full_result = run_curl(
                sequence=sequence,
                clip=clip,
                url=url,
                curl=args.curl,
                request_shape="archive_full_then_local_media",
                timeout=max(args.timeout, 900),
                output=source,
                max_bytes=args.full_download_max_bytes,
                byte_range=None,
                order_in_pair=None,
            )
            if full_result.ok:
                full_result = prove_local_processing(
                    result=full_result,
                    source=source,
                    ffmpeg=args.ffmpeg,
                    ffprobe=args.ffprobe,
                    timeout=args.timeout,
                )
            results.append(full_result)
            _emit(full_result)
            full_downloads += 1

    comparisons = []
    for clip, _url in clips:
        curl_result = direct_curl[clip]
        ffmpeg_result = direct_ffmpeg[clip]
        if curl_result.ok and not ffmpeg_result.ok:
            conclusion = "curl_only"
        elif ffmpeg_result.ok and not curl_result.ok:
            conclusion = "ffmpeg_only"
        elif curl_result.ok and ffmpeg_result.ok:
            conclusion = "both_succeeded"
        else:
            conclusion = "both_failed"
        comparisons.append(
            {
                "clip": clip,
                "conclusion": conclusion,
                "curl_outcome": curl_result.outcome,
                "ffmpeg_outcome": ffmpeg_result.outcome,
            }
        )

    payload = {
        "environment": {
            "runner_image": os.environ.get("ImageOS", ""),
            "runner_image_version": os.environ.get("ImageVersion", ""),
            "curl_version": _version(args.curl),
            "ffmpeg_version": _version(args.ffmpeg),
            "ffprobe_version": _version(args.ffprobe),
        },
        "settings": {
            "sample_seconds": args.sample_seconds,
            "pair_delay_seconds": args.pair_delay_seconds,
            "range_bytes": args.range_bytes,
            "full_download_max_bytes": args.full_download_max_bytes,
            "full_download_count": args.full_download_count,
        },
        "results": [asdict(result) for result in results],
        "comparisons": comparisons,
        "full_download_skips": full_download_skips,
        "summary": {
            "cases": len(results),
            "successes": sum(result.ok for result in results),
            "outcomes": {
                outcome: sum(result.outcome == outcome for result in results)
                for outcome in sorted({result.outcome for result in results})
            },
        },
    }
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"RESULTS path={args.output} cases={len(results)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
