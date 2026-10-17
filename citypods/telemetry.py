"""OpenTelemetry tracing helpers for multi-stage runner spans.

Review/45 Initiative 10: Provides non-invasive span instrumentation across stage boundaries.
Instrumentation is completely inert by default: if ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset,
or if opentelemetry is unavailable, zero network calls are made and overhead is near zero.
Span attributes are strictly validated to prevent accidental leakage of sensitive or complex
data structures.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Allowed scalar types for span attributes (matching Initiative 9's redaction policy).
_ALLOWED_SCALAR_TYPES = (int, float, str, bool)


def validate_span_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Validate that all attribute values are scalars, rejecting complex types."""
    validated: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if not isinstance(value, _ALLOWED_SCALAR_TYPES):
            raise TypeError(
                f"Span attribute {key!r} must be scalar (int, float, str, bool), "
                f"got {type(value).__name__}"
            )
        validated[key] = value
    return validated


@contextmanager
def trace_stage_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Context manager for tracing stage spans.

    Yields an active span if ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set and OpenTelemetry
    is configured; otherwise yields None with zero network overhead.
    """
    valid_attrs = validate_span_attributes(attributes or {})
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        yield None
        return

    try:
        from opentelemetry import trace  # type: ignore[import-untyped]

        tracer = trace.get_tracer("citypods")
        with tracer.start_as_current_span(name, attributes=valid_attrs) as span:
            yield span
    except (ImportError, Exception):
        yield None
