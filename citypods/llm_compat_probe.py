"""Manual, non-sensitive Gemini structured-output compatibility probe.

This intentionally uses a fixed prompt and never opens storage, records, or the LLM budget
ledger. It distinguishes Gemini's native schema API from the LiteLLM/Instructor adapter path,
and bisects which specific JSON Schema construct in the R5 tag contract (schema references,
anyOf-nullable typing, or default values) a native-schema failure is actually caused by.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import requests

from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig
from citypods.tags import ensure_llm_contract

PROMPT = "Return exactly one factual topic tag for a meeting about a housing zoning amendment."
SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {"tag": {"type": "string"}},
    "required": ["tag"],
}

# The R5 tag contract's native-r5-schema check above only reports a generic 400 with no detail
# on which part of the schema Gemini actually rejected. Pydantic's model_json_schema() combines
# three constructs for a nested-model contract like this one: $defs/$ref for the nested
# Suggestion/Evidence models, anyOf for Optional fields, and default values for fields with
# defaults. Each schema below isolates exactly one of those three against an otherwise-minimal
# schema, so a per-check pass/fail bisects the actual culprit instead of guessing.
REFS_SCHEMA = {
    "$defs": {
        "Inner": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        }
    },
    "type": "object",
    "properties": {"inner": {"$ref": "#/$defs/Inner"}},
    "required": ["inner"],
}
ANY_OF_NULLABLE_SCHEMA = {
    "type": "object",
    "properties": {"note": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
}
DEFAULT_VALUE_SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer", "default": 0}},
}


def _safe_error(
    response: requests.Response | None = None, exc: BaseException | None = None
) -> dict[str, Any]:
    """Return only fixed-prompt failure metadata; never surface headers or request bodies."""
    result: dict[str, Any] = {"ok": False}
    if response is not None:
        result["status"] = response.status_code
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        result["provider_code"] = error.get("status")
        # Gemini's short API error message is safe for this fixed prompt. Cap it defensively.
        result["provider_message"] = str(error.get("message") or "")[:300]
    if exc is not None:
        result["exception_type"] = type(exc).__name__
        result["exception_status"] = getattr(exc, "status_code", None)
    return result


def _native(model: str, schema: dict[str, Any], api_key: str) -> dict[str, Any]:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "maxOutputTokens": 128,
        },
    }
    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": api_key},
            json=payload,
            timeout=30,
        )
    except requests.RequestException as exc:
        # One fixed-prompt probe must report, not abort the matrix.
        return _safe_error(exc=exc)
    if not response.ok:
        return _safe_error(response=response)
    return {"ok": True, "status": response.status_code}


def _litellm(model: str, mode: str) -> dict[str, Any]:
    """Exercise Instructor without printing its request/response objects."""
    from instructor import Mode

    contract = ensure_llm_contract()
    backend = LiteLLMBackend(LLMBackendConfig(model=f"gemini/{model}"))
    try:
        typed, _raw = (
            __import__("instructor")
            .from_litellm(
                backend._completion_fn(), mode=Mode.JSON if mode == "json" else Mode.JSON_SCHEMA
            )
            .create_with_completion(
                response_model=contract,
                messages=[{"role": "user", "content": PROMPT}],
                model=f"gemini/{model}",
                max_retries=0,
                max_tokens=128,
            )
        )
        _ = typed
    except Exception as exc:  # one fixed-prompt probe must report, not abort the matrix
        return _safe_error(exc=exc)
    return {"ok": True}


def run(model: str) -> list[dict[str, Any]]:
    """Run one-attempt checks and return JSON-safe, non-sensitive summaries."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required")
    tag_schema = ensure_llm_contract().model_json_schema()
    checks = [
        ("native-simple-schema", lambda: _native(model, SIMPLE_SCHEMA, api_key)),
        ("native-r5-schema", lambda: _native(model, tag_schema, api_key)),
        ("native-refs-only", lambda: _native(model, REFS_SCHEMA, api_key)),
        ("native-anyof-nullable-only", lambda: _native(model, ANY_OF_NULLABLE_SCHEMA, api_key)),
        ("native-default-only", lambda: _native(model, DEFAULT_VALUE_SCHEMA, api_key)),
        ("litellm-json-object", lambda: _litellm(model, "json")),
        ("litellm-json-schema", lambda: _litellm(model, "json-schema")),
    ]
    results = []
    for name, check in checks:
        result = check()
        results.append({"check": name, "model": model, **result})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gemini-3.1-flash-lite")
    args = parser.parse_args(argv)
    for result in run(args.model):
        print("llm-compat-probe: " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
