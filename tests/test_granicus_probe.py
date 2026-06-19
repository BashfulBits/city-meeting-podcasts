from __future__ import annotations

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
