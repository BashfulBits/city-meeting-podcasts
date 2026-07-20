"""Manual, non-sensitive Gemini structured-output compatibility probe.

This intentionally uses a fixed prompt and never opens storage, records, or the LLM budget
ledger. It distinguishes Gemini's native schema API from the LiteLLM/Instructor adapter path,
and bisects which specific JSON Schema construct in the R5 tag contract a native-schema
failure is actually caused by -- first additively (synthetic minimal schemas, each adding one
construct: refs, array-of-refs, anyOf-nullable typing, default values, additionalProperties:
false), then subtractively (the real contract's schema with one construct category stripped
at a time: defaults, additionalProperties, size/range constraints, enums, or $ref/$defs). The
bisection identified size/range constraints (minLength/maxLength/minimum/maximum/minItems/
maxItems) as the actual cause; the final check exercises the production fix for that finding
(citypods.compute.llm._gemini_schema_safe_model, via LiteLLMBackend._run_structured_direct)
against the live API directly, rather than only against the unit tests' fake completion.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import requests

from citypods.compute.base import InferenceJob
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
# several constructs for a nested-model contract like this one: $defs/$ref for the nested
# Suggestion/Evidence models (including $ref used inside an array's items, since both are
# list[...] fields), anyOf for Optional fields, default values for fields with defaults, and
# additionalProperties: false from every model's ConfigDict(extra="forbid"). Each schema below
# isolates one of those constructs against an otherwise-minimal schema, so a per-check pass/fail
# bisects the actual culprit instead of guessing. A first round (refs/anyOf/default, each as a
# plain object property) all passed individually against gemini-3.1-flash-lite even though the
# combined R5 schema still 400s -- these two add the constructs that round didn't cover.
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
ARRAY_OF_REFS_SCHEMA = {
    "$defs": {
        "Inner": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        }
    },
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/Inner"}}},
    "required": ["items"],
}
ADDITIONAL_PROPERTIES_FALSE_SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
    "additionalProperties": False,
}

# Constraint keywords Pydantic's Field(min_length=..., max_length=..., ge=..., le=...) emits,
# never isolated by the additive checks above and never tried against a synthetic schema, only
# ever present (untested) in the real R5 contract's Evidence/Suggestion fields.
_CONSTRAINT_KEYS = frozenset(
    {"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"}
)


def _strip_keys(node: Any, keys: frozenset[str]) -> Any:
    """Deep copy of node with every occurrence of the given object keys removed."""
    if isinstance(node, dict):
        return {key: _strip_keys(value, keys) for key, value in node.items() if key not in keys}
    if isinstance(node, list):
        return [_strip_keys(item, keys) for item in node]
    return node


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """Deep copy of node with every "$ref": "#/$defs/X" replaced by defs["X"], resolved
    recursively so a $ref inside another $ref's own content (the real contract's two-level
    Response -> Suggestion -> Evidence chain) is fully flattened, not just one level deep."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return _inline_refs(defs[ref.removeprefix("#/$defs/")], defs)
        return {key: _inline_refs(value, defs) for key, value in node.items() if key != "$ref"}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


def _schema_without_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """The real R5 schema with $ref/$defs fully inlined -- unlike REFS_SCHEMA/ARRAY_OF_REFS_SCHEMA
    above, this exercises the actual two-level nested reference chain, not a one-level synthetic
    reproduction of it."""
    inlined = _inline_refs(schema, schema.get("$defs", {}))
    inlined.pop("$defs", None)
    return inlined


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
        details = error.get("details")
        if details:
            # A validation failure's `details` (typically a google.rpc.BadRequest with
            # per-field fieldViolations) is what actually names the rejected schema path --
            # `message` alone is often just the generic "Request contains an invalid argument."
            # Safe for this fixed synthetic prompt/schema, which never carries meeting content;
            # capped defensively like provider_message since it's still provider-controlled.
            result["provider_details"] = json.dumps(details, sort_keys=True)[:2000]
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


def _litellm_backend_fix(model: str) -> dict[str, Any]:
    """Exercise the actual production fix path -- LiteLLMBackend._run_structured_direct(),
    which applies _gemini_schema_safe_model() for a Gemini route -- rather than reimplementing
    the Instructor call the way `_litellm()` above does. This is the exact method
    llm_tag_suggestions() calls in production; a green result here is evidence the real fix
    works against the live API, not just evidence the unit tests' fake completion function
    accepts what we send it."""
    contract = ensure_llm_contract()
    backend = LiteLLMBackend(LLMBackendConfig(model=f"gemini/{model}"))
    job = InferenceJob(task="tag", inputs={"content": PROMPT}, recipe_hash="probe")
    try:
        backend._run_structured_direct(job, contract, resolved_model=f"gemini/{model}")
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
        ("native-array-of-refs-only", lambda: _native(model, ARRAY_OF_REFS_SCHEMA, api_key)),
        (
            "native-additional-properties-false-only",
            lambda: _native(model, ADDITIONAL_PROPERTIES_FALSE_SCHEMA, api_key),
        ),
        # Additive bisection (synthetic minimal schemas, each adding one construct) ran out of
        # leads: every construct above passes alone, yet the real combined schema still 400s
        # with no further detail from the provider. These instead take the real schema and strip
        # exactly one construct category at a time, so whichever strip makes it pass names the
        # actual culprit directly rather than hoping a synthetic reproduction captures it.
        (
            "native-r5-schema-minus-defaults",
            lambda: _native(model, _strip_keys(tag_schema, frozenset({"default"})), api_key),
        ),
        (
            "native-r5-schema-minus-additional-properties",
            lambda: _native(
                model, _strip_keys(tag_schema, frozenset({"additionalProperties"})), api_key
            ),
        ),
        (
            "native-r5-schema-minus-constraints",
            lambda: _native(model, _strip_keys(tag_schema, _CONSTRAINT_KEYS), api_key),
        ),
        (
            "native-r5-schema-minus-enum",
            lambda: _native(model, _strip_keys(tag_schema, frozenset({"enum"})), api_key),
        ),
        (
            "native-r5-schema-minus-refs",
            lambda: _native(model, _schema_without_refs(tag_schema), api_key),
        ),
        ("litellm-json-object", lambda: _litellm(model, "json")),
        ("litellm-json-schema", lambda: _litellm(model, "json-schema")),
        ("litellm-backend-gemini-fix", lambda: _litellm_backend_fix(model)),
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
