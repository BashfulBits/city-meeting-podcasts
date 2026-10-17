"""Tests for OpenTelemetry tracing helpers (Review/45 Initiative 10)."""

from __future__ import annotations

import pytest

from citypods.telemetry import trace_stage_span, validate_span_attributes


def test_trace_stage_span_inert_by_default(monkeypatch):
    """When OTEL_EXPORTER_OTLP_ENDPOINT is unset, trace_stage_span yields None without
    network calls.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    with trace_stage_span("stage.audio", attributes={"source_key": "cambridge"}) as span:
        assert span is None


def test_trace_stage_span_rejects_non_scalar_attributes():
    """Span attributes must strictly reject non-scalar types (lists, dicts, custom objects)."""
    with pytest.raises(TypeError, match="must be scalar"):
        validate_span_attributes({"nested": {"key": "val"}})

    with pytest.raises(TypeError, match="must be scalar"):
        validate_span_attributes({"items": [1, 2, 3]})

    with pytest.raises(TypeError, match="must be scalar"):
        validate_span_attributes({"custom": object()})


def test_trace_stage_span_accepts_valid_scalars():
    attrs = {
        "source_key": "boston",
        "episode_count": 5,
        "duration_s": 12.34,
        "is_active": True,
        "ignored_none": None,
    }
    validated = validate_span_attributes(attrs)
    assert validated == {
        "source_key": "boston",
        "episode_count": 5,
        "duration_s": 12.34,
        "is_active": True,
    }


def test_trace_stage_span_when_configured(monkeypatch):
    """When endpoint is configured, attempts to initialize span if opentelemetry is present."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")

    with trace_stage_span("stage.audio", attributes={"source_key": "boston"}):
        pass
