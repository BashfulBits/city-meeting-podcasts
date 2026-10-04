"""Registry for serializable, typed LLM response contracts.

An :class:`~citypods.compute.base.InferenceJob` names a response contract rather than carrying a
Python class.  The name is safe to retain with a dispatched handle, while this registry supplies
the local Pydantic model needed for direct Instructor calls and reconciliation validation.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

# Keep this registry importable on ASR-only installs.  Instructor supplies Pydantic when an LLM
# feature actually registers or consumes one of these models.
ResponseModel = type[Any]
_LOCK = threading.Lock()
_RESPONSE_MODELS: dict[str, ResponseModel] = {}
_THOUGHT_BLOCK_RE = re.compile(
    r"^\s*<(?P<tag>think|thought)>.*?</(?P=tag)>", re.IGNORECASE | re.DOTALL
)


def parse_structured_json(content: str | bytes, *, context: str = "structured response") -> Any:
    """Parse JSON from common provider wrappers without weakening schema validation.

    Some otherwise-valid structured-output routes occasionally add a Markdown JSON fence or a
    closed reasoning block before the JSON document.  The caller still validates the returned
    value against its Pydantic contract; this helper only removes those transport wrappers.  It
    rejects trailing prose and never evaluates model-produced text as code.
    """
    if isinstance(content, bytes):
        text = content.decode("utf-8-sig", errors="strict")
    elif isinstance(content, str):
        text = content.lstrip("\ufeff")
    else:
        raise ValueError(f"{context} was not text")

    text = _THOUGHT_BLOCK_RE.sub("", text, count=1).strip()
    fence_start = text.find("```")
    if fence_start >= 0:
        closing = text.find("```", fence_start + 3)
        if closing < 0:
            raise ValueError(f"{context} had an unterminated JSON fence")
        fenced = text[fence_start + 3 : closing].lstrip()
        if fenced.lower().startswith("json"):
            fenced = fenced[4:].lstrip(" \t\r\n")
        text = fenced.strip()

    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(text) if char in "[{]"]
    for start in starts:
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if text[start + end :].strip():
            continue
        return value
    raise ValueError(f"{context} was not valid JSON")


def _equivalent_response_models(left: ResponseModel, right: ResponseModel) -> bool:
    """Return whether separately constructed Pydantic models expose the same schema."""
    if left is right:
        return True
    left_schema = getattr(left, "model_json_schema", None)
    right_schema = getattr(right, "model_json_schema", None)
    if not callable(left_schema) or not callable(right_schema):
        return False
    try:
        return left_schema() == right_schema()
    except (TypeError, ValueError):
        return False


def register_response_model(name: str, model: ResponseModel) -> ResponseModel:
    """Register one stable, task-owned structured-output contract.

    Concurrent lazy helpers may construct equivalent Pydantic classes independently.  Reuse the
    first registered class in that case, but reject incompatible schemas so an accidental contract
    name collision cannot silently validate a response against the wrong model.
    """
    if not name:
        raise ValueError(f"duplicate or empty structured-output contract: {name!r}")
    with _LOCK:
        existing = _RESPONSE_MODELS.get(name)
        if existing is not None:
            if _equivalent_response_models(existing, model):
                return existing
            raise ValueError(f"conflicting structured-output contract: {name!r}")
        _RESPONSE_MODELS[name] = model
        return model


def response_model(name: str) -> ResponseModel:
    """Return the Pydantic model named by a job or durable dispatch handle."""
    with _LOCK:
        try:
            return _RESPONSE_MODELS[name]
        except KeyError as exc:
            raise ValueError(f"unknown structured-output contract: {name!r}") from exc


__all__ = [
    "ResponseModel",
    "parse_structured_json",
    "register_response_model",
    "response_model",
]
