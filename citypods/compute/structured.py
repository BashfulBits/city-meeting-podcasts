"""Registry for serializable, typed LLM response contracts.

An :class:`~citypods.compute.base.InferenceJob` names a response contract rather than carrying a
Python class.  The name is safe to retain with a dispatched handle, while this registry supplies
the local Pydantic model needed for direct Instructor calls and reconciliation validation.
"""

from __future__ import annotations

import threading
from typing import Any

# Keep this registry importable on ASR-only installs.  Instructor supplies Pydantic when an LLM
# feature actually registers or consumes one of these models.
ResponseModel = type[Any]
_LOCK = threading.Lock()
_RESPONSE_MODELS: dict[str, ResponseModel] = {}


def register_response_model(name: str, model: ResponseModel) -> ResponseModel:
    """Register one stable, task-owned structured-output contract."""
    if not name:
        raise ValueError(f"duplicate or empty structured-output contract: {name!r}")
    with _LOCK:
        if name in _RESPONSE_MODELS:
            return _RESPONSE_MODELS[name]
        _RESPONSE_MODELS[name] = model
        return model


def response_model(name: str) -> ResponseModel:
    """Return the Pydantic model named by a job or durable dispatch handle."""
    with _LOCK:
        try:
            return _RESPONSE_MODELS[name]
        except KeyError as exc:
            raise ValueError(f"unknown structured-output contract: {name!r}") from exc


__all__ = ["ResponseModel", "register_response_model", "response_model"]
