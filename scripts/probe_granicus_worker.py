#!/usr/bin/env python
"""Compare direct Granicus access with an authenticated Cloudflare Worker stream.

Run this only from the isolated ``granicus-probe.yml`` workflow. The proxy token is read from
``GRANICUS_PROXY_TOKEN`` and is never written to output. Each request is bounded; at most one object
under the configured ceiling is downloaded fully and validated through local ffprobe/ffmpeg.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    from scripts import probe_granicus_transport as transport
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    import probe_granicus_transport as transport

from citypods.media import PODCAST_SPEECH_PROFILE, CommandFfmpeg, SourceCache
from citypods.security import validate_source_url


def _redact_proxy_endpoint(result, archive_url: str):
    result.final_host = "worker-proxy"
    result.final_path = transport._redacted_parts(archive_url)[1]
    return result


def _normalize_proxy_base_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    if "://" not in base_url:
        base_url = f"https://{base_url}"

    base_parts = urlsplit(base_url)
    if (
        base_parts.scheme != "https"
        or not base_parts.hostname
        or base_parts.username
        or base_parts.password
        or base_parts.port not in {None, 443}
        or base_parts.path not in {"", "/"}
        or base_parts.query
        or base_parts.fragment
    ):
        raise ValueError("proxy base URL must be an HTTPS origin without a path or query")
    return f"https://{base_parts.hostname}"


def _proxy_url(base_url: str, archive_url: str) -> str:
    base = _normalize_proxy_base_url(base_url)
    parts = urlsplit(archive_url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if parts.hostname != "archive-video.granicus.com" or len(segments) != 2:
        raise ValueError(f"not a canonical Granicus archive URL: {archive_url}")
    tenant, filename = segments
    return f"{base}/v1/archive/{quote(tenant, safe='')}/{quote(filename, safe='')}"


def _run_production_encode(
    *,
    clip: str,
    archive_url: str,
    ffmpeg: str,
    ffprobe: str,
    timeout: float,
) -> dict:
    """Exercise the production direct-first source cache and speech-mastering recipe end to end."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="granicus-worker-production-encode-") as tmp:
        tmpdir = Path(tmp)
        with SourceCache(ffmpeg_binary=ffmpeg, timeout_seconds=timeout) as cache:
            source = cache.get_or_fetch(clip, archive_url)
            if source is None:
                return {
                    "clip": clip,
                    "ok": False,
                    "outcome": "source_cache_failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            output = tmpdir / "production.m4a"
            try:
                runner = CommandFfmpeg(
                    binary=ffmpeg,
                    max_kbps=96,
                    timeout_seconds=timeout,
                    threads=1,
                )
                runner.extract_audio(
                    None,
                    {"s0": str(source)},
                    output,
                    loudness_profile="ebuR128:-16LUFS",
                    processing_profile=PODCAST_SPEECH_PROFILE,
                )
                probe = subprocess.run(
                    [
                        ffprobe,
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration:stream=codec_type,codec_name",
                        "-of",
                        "json",
                        str(output),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=min(timeout, 120),
                    check=False,
                )
            except Exception as exc:  # noqa: BLE001 - this probe step must not abort the whole run
                return {
                    "clip": clip,
                    "ok": False,
                    "outcome": "extract_or_probe_failed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": str(exc),
                }
            output_bytes = output.stat().st_size if output.exists() else 0
            ok = probe.returncode == 0 and output_bytes > 0
            return {
                "clip": clip,
                "ok": ok,
                "outcome": "success" if ok else "local_ffprobe_failed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "output_bytes": output_bytes,
                "ffprobe_summary": " ".join(probe.stdout.strip().split())[:500],
            }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--curl", default="curl")
    parser.add_argument("--proxy-base-url", default=os.environ.get("GRANICUS_PROXY_BASE_URL"))
    parser.add_argument("--range-mib", type=transport._mib_to_bytes, default=16 * 1024 * 1024)
    parser.add_argument(
        "--full-download-max-mib",
        type=transport._mib_to_bytes,
        default=512 * 1024 * 1024,
    )
    parser.add_argument("--full-download-count", type=transport._nonnegative_integer, default=1)
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--production-encode-clip",
        choices=["none", "arlington-audio40", "pflugerville-audio40"],
        default="none",
        help="Run one full production-recipe encode through the direct-first Worker fallback",
    )
    parser.add_argument("--output", type=Path, default=Path("granicus-worker-results.json"))
    args = parser.parse_args()

    token = os.environ.get("GRANICUS_PROXY_TOKEN", "")
    if not args.proxy_base_url:
        parser.error("--proxy-base-url or GRANICUS_PROXY_BASE_URL is required")
    if not token:
        parser.error("GRANICUS_PROXY_TOKEN is required")
    try:
        args.proxy_base_url = _normalize_proxy_base_url(args.proxy_base_url)
    except ValueError as exc:
        parser.error(str(exc))
    validate_source_url(args.proxy_base_url)
    # Requests to the worker proxy aren't *.granicus.com, so they can't use transport's default
    # ALLOWED_HOSTS -- but allowed_hosts=None would disable the allowlist entirely. Pin it to the
    # configured proxy host instead so the bearer token is still never sent anywhere else.
    proxy_allowed_hosts = (urlsplit(args.proxy_base_url).hostname or "",)

    results = []
    comparisons = []
    proxy_curl = {}
    production_encode = None
    sequence = 0
    with tempfile.TemporaryDirectory(prefix="granicus-worker-probe-") as tmp:
        tmpdir = Path(tmp)
        for clip, archive_url in transport.DEFAULT_CLIPS:
            proxy_url = _proxy_url(args.proxy_base_url, archive_url)

            sequence += 1
            direct = transport.run_curl(
                sequence=sequence,
                clip=clip,
                url=archive_url,
                curl=args.curl,
                request_shape="direct_archive_control",
                timeout=args.timeout,
                output=tmpdir / f"{sequence}-direct.bin",
                max_bytes=args.range_mib,
                byte_range=f"0-{args.range_mib - 1}",
                order_in_pair=1,
            )
            results.append(direct)
            transport._emit(direct)

            sequence += 1
            proxied = transport.run_curl(
                sequence=sequence,
                clip=clip,
                url=proxy_url,
                curl=args.curl,
                request_shape="cloudflare_worker_range",
                timeout=args.timeout,
                output=tmpdir / f"{sequence}-proxy.bin",
                max_bytes=args.range_mib,
                byte_range=f"0-{args.range_mib - 1}",
                order_in_pair=2,
                authorization=token,
                allowed_hosts=proxy_allowed_hosts,
                transport="curl_worker_range",
                source_identity_url=archive_url,
            )
            proxied = _redact_proxy_endpoint(proxied, archive_url)
            proxy_curl[clip] = proxied
            results.append(proxied)
            transport._emit(proxied)

            sequence += 1
            ffmpeg = transport.run_ffmpeg_remote(
                sequence=sequence,
                clip=clip,
                url=proxy_url,
                ffmpeg=args.ffmpeg,
                seconds=args.sample_seconds,
                timeout=args.timeout,
                order_in_pair=3,
                authorization=token,
                allowed_hosts=proxy_allowed_hosts,
                transport="ffmpeg_worker_remote",
                request_shape="cloudflare_worker_audio",
                source_identity_url=archive_url,
            )
            ffmpeg = _redact_proxy_endpoint(ffmpeg, archive_url)
            results.append(ffmpeg)
            transport._emit(ffmpeg)
            comparisons.append(
                {
                    "clip": clip,
                    "direct_outcome": direct.outcome,
                    "worker_curl_outcome": proxied.outcome,
                    "worker_ffmpeg_outcome": ffmpeg.outcome,
                    "worker_bypassed_direct_403": (
                        direct.outcome == "http_403" and proxied.ok and ffmpeg.ok
                    ),
                }
            )

        full_downloads = 0
        full_download_skips = []
        candidates = sorted(
            transport.DEFAULT_CLIPS,
            key=lambda item: not proxy_curl[item[0]].ok,
        )
        for clip, archive_url in candidates:
            if full_downloads >= args.full_download_count:
                break
            ranged = proxy_curl[clip]
            total_size = transport._total_size(ranged)
            if not ranged.ok:
                full_download_skips.append({"clip": clip, "reason": "worker_range_failed"})
                continue
            if total_size is None:
                full_download_skips.append({"clip": clip, "reason": "unknown_total_size"})
                continue
            if total_size > args.full_download_max_mib:
                full_download_skips.append(
                    {
                        "clip": clip,
                        "reason": "too_large",
                        "total_bytes": total_size,
                        "limit_bytes": args.full_download_max_mib,
                    }
                )
                continue

            sequence += 1
            source = tmpdir / f"{sequence}-full.mp4"
            full = transport.run_curl(
                sequence=sequence,
                clip=clip,
                url=_proxy_url(args.proxy_base_url, archive_url),
                curl=args.curl,
                request_shape="cloudflare_worker_full_then_local_media",
                timeout=max(args.timeout, 900),
                output=source,
                max_bytes=args.full_download_max_mib,
                byte_range=None,
                order_in_pair=None,
                authorization=token,
                allowed_hosts=proxy_allowed_hosts,
                transport="curl_worker_full",
                source_identity_url=archive_url,
            )
            full = _redact_proxy_endpoint(full, archive_url)
            if full.ok:
                full = transport.prove_local_processing(
                    result=full,
                    source=source,
                    ffmpeg=args.ffmpeg,
                    ffprobe=args.ffprobe,
                    timeout=args.timeout,
                )
                full.transport = "curl_worker_full_ffmpeg_local"
            results.append(full)
            transport._emit(full)
            full_downloads += 1

        if args.production_encode_clip != "none":
            archive_url = dict(transport.DEFAULT_CLIPS)[args.production_encode_clip]
            production_encode = _run_production_encode(
                clip=args.production_encode_clip,
                archive_url=archive_url,
                ffmpeg=args.ffmpeg,
                ffprobe=args.ffprobe,
                timeout=max(args.timeout, 7200),
            )
            print(
                "PRODUCTION_ENCODE "
                f"clip={production_encode['clip']} "
                f"outcome={production_encode['outcome']} "
                f"seconds={production_encode['elapsed_seconds']}",
                flush=True,
            )

    payload = {
        "environment": {
            "runner_image": os.environ.get("ImageOS", ""),
            "runner_image_version": os.environ.get("ImageVersion", ""),
            "curl_version": transport._version(args.curl),
            "ffmpeg_version": transport._version(args.ffmpeg),
            "ffprobe_version": transport._version(args.ffprobe),
        },
        "settings": {
            "range_bytes": args.range_mib,
            "full_download_max_bytes": args.full_download_max_mib,
            "full_download_count": args.full_download_count,
        },
        "results": [asdict(result) for result in results],
        "comparisons": comparisons,
        "full_download_skips": full_download_skips,
        "production_encode": production_encode,
        "summary": {
            "cases": len(results),
            "successes": sum(result.ok for result in results),
            "worker_access_successes": sum(
                comparison["worker_curl_outcome"]
                in {"success", "range_ignored", "range_unsupported"}
                and comparison["worker_ffmpeg_outcome"] == "success"
                for comparison in comparisons
            ),
            "worker_ranges_unsupported": sum(
                comparison["worker_curl_outcome"] == "range_unsupported"
                for comparison in comparisons
            ),
            "worker_bypasses": sum(
                comparison["worker_bypassed_direct_403"] for comparison in comparisons
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"RESULTS path={args.output} cases={len(results)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
