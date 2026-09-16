"""Tests for the raw-PDF-bytes agenda_text_artifact survey tool."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import audit_raw_pdf_agenda_artifacts as audit  # noqa: E402


class _FakeStorage:
    """Minimal stand-in for S3CompatibleStorage.get_range, keyed by object key."""

    def __init__(self, responses: dict[str, bytes | Exception]):
        self._responses = responses

    def get_range(self, key: str, start: int, end: int) -> bytes | None:
        response = self._responses.get(key)
        if isinstance(response, Exception):
            raise response
        return response


def test_looks_like_raw_pdf_matches_only_the_file_signature():
    assert audit._looks_like_raw_pdf(b"%PDF-1.5\r%\r\n1 0 obj")
    assert audit._looks_like_raw_pdf(b"  %PDF-1.4\n%")  # tolerates leading whitespace
    assert not audit._looks_like_raw_pdf(b"1. Call to order\nAGENDA")
    assert not audit._looks_like_raw_pdf(b"")


def test_check_one_key_distinguishes_hit_clean_and_failed():
    episodes = [{"slug": "example-city", "uid": "abc123"}]
    storage = _FakeStorage(
        {
            "raw-pdf-key": b"%PDF-1.5\r%\r\n1 0 obj",
            "clean-key": b"1. Call to order\nAGENDA",
            "broken-key": RuntimeError("connection reset"),
        }
    )
    hit, failed = audit._check_one_key("raw-pdf-key", episodes, storage)
    assert failed is False
    assert hit == audit.RawPdfHit(
        key="raw-pdf-key", prefix="%PDF-1.5\r%\r\n1 0 obj", episodes=episodes
    )

    hit, failed = audit._check_one_key("clean-key", episodes, storage)
    assert hit is None
    assert failed is False

    hit, failed = audit._check_one_key("broken-key", episodes, storage)
    assert hit is None
    assert failed is True


def test_find_raw_pdf_artifacts_reports_failed_keys_separately_from_hits():
    """Regression: a failed read must never look like a clean "not corrupted" result -- a caller
    checking only `hits` on a run with real failures could otherwise report a false-clean survey."""
    by_key = {
        "raw-pdf-key": [{"slug": "example-city", "uid": "abc123"}],
        "clean-key": [{"slug": "example-city", "uid": "def456"}],
        "broken-key": [{"slug": "example-city", "uid": "ghi789"}],
    }
    storage = _FakeStorage(
        {
            "raw-pdf-key": b"%PDF-1.5\r%\r\n1 0 obj",
            "clean-key": b"1. Call to order\nAGENDA",
            "broken-key": RuntimeError("connection reset"),
        }
    )
    hits, failed_keys = audit.find_raw_pdf_artifacts(by_key, storage)
    assert [hit.key for hit in hits] == ["raw-pdf-key"]
    assert failed_keys == ["broken-key"]


def test_find_raw_pdf_artifacts_concurrent_path_matches_serial_results():
    by_key = {f"key-{i}": [{"slug": "example-city", "uid": f"uid-{i}"}] for i in range(6)}
    responses = {f"key-{i}": b"plain text" for i in range(6)}
    responses["key-3"] = b"%PDF-1.5\r%\r\n"
    responses["key-5"] = RuntimeError("boom")
    storage = _FakeStorage(responses)

    hits, failed_keys = audit.find_raw_pdf_artifacts(by_key, storage, concurrency=4)
    assert [hit.key for hit in hits] == ["key-3"]
    assert failed_keys == ["key-5"]
