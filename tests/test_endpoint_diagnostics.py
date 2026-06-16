"""Offline tests for live endpoint-contract diagnostics."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from citypods.contracts import CheckResult, _media_fetch_detail

# Load check_endpoints from scripts/ without making it a package.
_script = Path(__file__).parent.parent / "scripts" / "check_endpoints.py"
_spec = importlib.util.spec_from_file_location("check_endpoints", _script)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_media_fetch_detail_includes_url_and_ffmpeg_log_on_failure():
    detail = _media_fetch_detail(
        resolved_url="https://archive-video.granicus.com/arlingtontx/sample.mp4",
        size=0,
        seconds=3.0,
        ok=False,
        logs=[
            "[enrich] ffmpeg source-cache start",
            "[enrich] ffmpeg source-cache error returncode=8 stderr=Server returned 403 Forbidden",
        ],
    )

    assert "0B from first 3s" in detail
    assert "url=https://archive-video.granicus.com/arlingtontx/sample.mp4" in detail
    assert "Server returned 403 Forbidden" in detail


def test_media_fetch_detail_stays_compact_on_success():
    detail = _media_fetch_detail(
        resolved_url="https://archive-video.granicus.com/arlingtontx/sample.mp4",
        size=51943,
        seconds=3.0,
        ok=True,
        logs=["[enrich] ffmpeg source-cache done"],
    )

    assert detail == "51943B from first 3s"


def test_endpoint_issue_body_explains_media_fetch_failures():
    body = _mod._issue_body(
        CheckResult(
            provider="granicus",
            slug="arlington-tx",
            endpoint="media-fetch",
            ok=False,
            detail=(
                "0B from first 3s\n"
                "url=https://archive-video.granicus.com/arlingtontx/sample.mp4\n"
                "ffmpeg=Server returned 403 Forbidden"
            ),
        )
    )

    assert "Provider:" in body
    assert "Endpoint:" in body
    assert "What this means" in body
    assert "production ffmpeg download path" in body
    assert "Observed detail" in body
    assert "Server returned 403 Forbidden" in body
