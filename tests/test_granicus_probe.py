from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

from citypods.security import SecurityError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_granicus_sustained.py"
SPEC = importlib.util.spec_from_file_location("probe_granicus_sustained", SCRIPT)
assert SPEC and SPEC.loader
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)

TRANSPORT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_granicus_transport.py"
TRANSPORT_SPEC = importlib.util.spec_from_file_location(
    "probe_granicus_transport", TRANSPORT_SCRIPT
)
assert TRANSPORT_SPEC and TRANSPORT_SPEC.loader
transport = importlib.util.module_from_spec(TRANSPORT_SPEC)
sys.modules[TRANSPORT_SPEC.name] = transport
TRANSPORT_SPEC.loader.exec_module(transport)


@pytest.mark.parametrize(
    ("returncode", "stderr", "size", "expected"),
    [
        (0, "", 100, "success"),
        (8, "Server returned 403 Forbidden", 0, "http_403"),
        (8, "HTTP error 429 Too Many Requests", 0, "http_429"),
        (124, "timeout after 90s", 0, "timeout"),
        (183, "moov atom not found; Invalid data found", 0, "invalid"),
        (1, "unknown failure", 0, "error"),
    ],
)
def test_outcome_classification(returncode, stderr, size, expected):
    assert probe._outcome(returncode, stderr, size) == expected


def test_default_probe_urls_are_stable_redacted_archive_paths():
    for _name, url in probe.DEFAULT_CLIPS:
        assert url.startswith("https://archive-video.granicus.com/")
        assert "?" not in url


def test_parse_clip_rejects_unapproved_host():
    with pytest.raises(SecurityError):
        probe._parse_clip("bad=https://example.com/video.mp4")


def test_transport_defaults_are_exact_redacted_archive_objects():
    names = {name for name, _url in transport.DEFAULT_CLIPS}
    assert {
        "arlington-audio40",
        "pflugerville-audio40",
        "fort-worth-audio40",
        "arlington-audio33-control",
    } == names
    for _name, url in transport.DEFAULT_CLIPS:
        assert url.startswith("https://archive-video.granicus.com/")
        assert "?" not in url


def test_transport_header_parser_uses_final_redirect_block(tmp_path):
    headers = tmp_path / "headers.txt"
    headers.write_text(
        "HTTP/1.1 302 Found\r\n"
        "Location: https://archive-video.granicus.com/x/y.mp4\r\n\r\n"
        "HTTP/2 206\r\n"
        "content-range: bytes 0-99/123456\r\n"
        "content-length: 100\r\n"
        "accept-ranges: bytes\r\n\r\n"
    )
    parsed = transport._parse_headers(headers)
    assert parsed["content-range"] == "bytes 0-99/123456"
    assert parsed["content-length"] == "100"
    assert parsed["accept-ranges"] == "bytes"


def test_transport_total_size_prefers_content_range():
    result = transport.ProbeResult(
        sequence=1,
        clip="clip",
        tenant="tenant",
        transport="curl_range",
        request_shape="archive_browser_ua",
        order_in_pair=1,
        started_at="2026-06-20T00:00:00Z",
        elapsed_seconds=1.0,
        host="archive-video.granicus.com",
        path="/tenant/object.mp4",
        final_host="archive-video.granicus.com",
        final_path="/tenant/object.mp4",
        remote_ip="",
        http_status=206,
        redirect_count=0,
        content_type="video/mp4",
        content_length=100,
        content_range="bytes 0-99/123456",
        accept_ranges="bytes",
        connect_seconds=0.1,
        first_byte_seconds=0.2,
        total_seconds=1.0,
        output_bytes=100,
        ok=True,
        outcome="success",
        returncode=0,
        ffprobe_ok=None,
        ffprobe_summary="",
        local_ffmpeg_ok=None,
        local_output_bytes=0,
        stderr_tail="",
    )
    assert transport._total_size(result) == 123456


@pytest.mark.parametrize(
    ("returncode", "stderr", "size", "status", "expected"),
    [
        (0, "", 100, 206, "success"),
        (22, "curl failure", 0, 403, "http_403"),
        (22, "curl failure", 0, 429, "http_429"),
        (63, "maximum file size exceeded", 0, 200, "size_limit"),
        (28, "operation timed out", 0, None, "timeout"),
    ],
)
def test_transport_outcome_classification(returncode, stderr, size, status, expected):
    assert transport._outcome(returncode, stderr, size, status) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("16.0", 16 * 1024 * 1024),
        ("512.0", 512 * 1024 * 1024),
        ("0.5", 512 * 1024),
    ],
)
def test_transport_converts_decimal_mib_inputs(raw, expected):
    assert transport._mib_to_bytes(raw) == expected


def test_transport_rejects_nonpositive_mib_inputs():
    with pytest.raises(argparse.ArgumentTypeError, match="MiB values must be positive"):
        transport._mib_to_bytes("0.0")


@pytest.mark.parametrize(("raw", "expected"), [("1.0", 1), ("0.0", 0), ("2", 2)])
def test_transport_accepts_integral_decimal_counts(raw, expected):
    assert transport._nonnegative_integer(raw) == expected


@pytest.mark.parametrize("raw", ["-1.0", "1.5", "nan", "not-a-number"])
def test_transport_rejects_invalid_counts(raw):
    with pytest.raises(argparse.ArgumentTypeError, match="non-negative integer"):
        transport._nonnegative_integer(raw)
