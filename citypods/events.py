"""Structured event telemetry for runner phase transitions and operational events.

Review/45 Initiative 9: Enables programmatic log ingestion by emitting JSON-structured
event lines prefixed with ``EVENT `` alongside human-readable console output.
Strictly rejects non-scalar fields to mechanically prevent accidental leakage of
transcripts, prompts, or sensitive payloads into telemetry streams.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

# Scalar types permitted in structured event fields.
_ALLOWED_SCALAR_TYPES = (int, float, str, bool)

# Secret pattern regex for smoke testing field values.
_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_-]{10,}|Bearer\s+[A-Za-z0-9._~+/-]+|://[^:@\s]+:[^@\s]+@)"
)


def emit_event(
    event: str,
    *,
    outcome: str,
    lane: str | None = None,
    source_count: int | None = None,
    duration_s: float | None = None,
    error_class: str | None = None,
    log: Callable[[str], None] | None = None,
    **fields: int | float | str | bool | None,
) -> None:
    """Emit a single structured JSON event line prefixed with ``EVENT ``.

    Parameters:
        event: Event name (e.g. 'audio_pass_start', 'tag_batch_flush').
        outcome: Outcome status (e.g. 'start', 'completed', 'failed', 'hit', 'miss').
        lane: Optional processing lane identifier.
        source_count: Optional count of sources processed.
        duration_s: Optional duration in seconds.
        error_class: Optional error class name on failure.
        log: Optional callable taking a string (defaults to print with flush=True).
        **fields: Additional metadata. Must strictly be scalar (int, float, str, bool, or None).

    Raises:
        TypeError: If any field value is not a scalar or None.
        ValueError: If any field value matches known secret patterns.
    """
    emit = log or (lambda msg: print(msg, flush=True))

    data: dict[str, Any] = {
        "event": event,
        "outcome": outcome,
        "ts": datetime.now(UTC).isoformat(),
    }

    run_id = os.environ.get("GITHUB_RUN_ID")
    if run_id:
        data["run_id"] = run_id

    if lane is not None:
        data["lane"] = lane
    if source_count is not None:
        data["source_count"] = source_count
    if duration_s is not None:
        data["duration_s"] = duration_s
    if error_class is not None:
        data["error_class"] = error_class

    for key, value in fields.items():
        if value is None:
            continue
        if not isinstance(value, _ALLOWED_SCALAR_TYPES):
            raise TypeError(
                f"Field {key!r} must be scalar (int, float, str, bool, None), "
                f"got {type(value).__name__}"
            )
        if isinstance(value, str) and _SECRET_PATTERN.search(value):
            raise ValueError(f"Field {key!r} contains a prohibited secret pattern")
        data[key] = value

    payload = json.dumps(data, sort_keys=True)
    emit(f"EVENT {payload}")
