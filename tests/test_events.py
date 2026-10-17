"""Tests for structured event telemetry (Review/45 Initiative 9)."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from citypods.events import emit_event


def test_emit_event_shape(monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    lines: list[str] = []

    emit_event(
        "audio_pass_start",
        outcome="start",
        lane="audio",
        source_count=12,
        log=lines.append,
    )

    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("EVENT ")
    payload = json.loads(line[len("EVENT ") :])
    assert payload["event"] == "audio_pass_start"
    assert payload["outcome"] == "start"
    assert payload["lane"] == "audio"
    assert payload["source_count"] == 12
    # Verify timestamp is ISO-8601 UTC
    ts = datetime.fromisoformat(payload["ts"])
    assert ts.tzinfo is not None


def test_emit_event_includes_github_run_id(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "123456789")
    lines: list[str] = []

    emit_event("test_event", outcome="completed", log=lines.append)

    assert len(lines) == 1
    payload = json.loads(lines[0][len("EVENT ") :])
    assert payload["run_id"] == "123456789"


def test_emit_event_rejects_non_scalar_field():
    lines: list[str] = []

    with pytest.raises(TypeError, match="must be scalar"):
        emit_event(
            "bad_event",
            outcome="fail",
            nested={"a": 1},  # type: ignore[arg-type]
            log=lines.append,
        )

    with pytest.raises(TypeError, match="must be scalar"):
        emit_event(
            "bad_event",
            outcome="fail",
            items=[1, 2, 3],  # type: ignore[arg-type]
            log=lines.append,
        )

    with pytest.raises(TypeError, match="must be scalar"):
        emit_event(
            "bad_event",
            outcome="fail",
            obj=object(),  # type: ignore[arg-type]
            log=lines.append,
        )


def test_emit_event_never_contains_known_secret_patterns():
    lines: list[str] = []

    # OpenAI-like key
    with pytest.raises(ValueError, match="prohibited secret pattern"):
        emit_event(
            "leak_event",
            outcome="fail",
            token="sk-1234567890abcdef123456",
            log=lines.append,
        )

    # Bearer token
    with pytest.raises(ValueError, match="prohibited secret pattern"):
        emit_event(
            "leak_event",
            outcome="fail",
            auth="Bearer eyJhbGciOiJIUzI1NiJ9",
            log=lines.append,
        )

    # URL credentials
    with pytest.raises(ValueError, match="prohibited secret pattern"):
        emit_event(
            "leak_event",
            outcome="fail",
            url="https://user:password123@example.com/api",
            log=lines.append,
        )


def test_emit_event_ignores_none_fields():
    lines: list[str] = []

    emit_event(
        "clean_event",
        outcome="ok",
        lane=None,
        source_count=None,
        extra_none=None,
        log=lines.append,
    )

    payload = json.loads(lines[0][len("EVENT ") :])
    assert "lane" not in payload
    assert "source_count" not in payload
    assert "extra_none" not in payload
