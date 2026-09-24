"""Shape a structured-output request for one route's method (review/48 R10).

Producers never choose how to ask a provider for JSON. A durable v2 job carries only its response
schema (``{"name", "schema"}``); the v2 Worker shapes the request for the route it dispatches to,
and Python's direct path shapes it here. The two implementations -- this module and
``workers/llm-dispatch-v2/src/structured_output.js`` -- are held to one contract by the shared
fixture ``tests/fixtures/structured_output_shaping.json``, which both test suites assert, so a
change to either side that the other does not mirror fails CI.

A method is the compiled route's ``structured_output_response_format`` (``json_schema``,
``json_object`` or ``none``), ``structured_output_include_schema_in_prompt`` and
``structured_output_schema_strip_keys``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA_INSTRUCTION = (
    "Return one JSON object only (no Markdown or commentary) matching this JSON Schema:\n"
)


def canonical_schema_json(schema: Any) -> str:
    """Serialize a schema the way the Worker does: sorted keys, compact, integral floats as ints.

    JavaScript prints ``1.0`` as ``1``; normalizing integral floats first keeps the prompt text
    byte-identical between the two implementations.
    """
    return json.dumps(
        _integral_floats_as_ints(schema), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _integral_floats_as_ints(node: Any) -> Any:
    if isinstance(node, dict):
        return {key: _integral_floats_as_ints(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_integral_floats_as_ints(item) for item in node]
    if isinstance(node, float) and node.is_integer():
        return int(node)
    return node


def strip_schema_keys(node: Any, keys: Iterable[str]) -> Any:
    """Deep copy of a JSON Schema node with every object key in ``keys`` removed."""
    keys = frozenset(keys)
    if not keys:
        return node
    if isinstance(node, dict):
        return {k: strip_schema_keys(v, keys) for k, v in node.items() if k not in keys}
    if isinstance(node, list):
        return [strip_schema_keys(item, keys) for item in node]
    return node


def messages_with_schema(messages: list[dict[str, Any]], schema: Any) -> list[dict[str, Any]]:
    """Append the schema instruction to the first string system message, or prepend one."""
    instruction = SCHEMA_INSTRUCTION + canonical_schema_json(schema)
    enriched = [dict(message) for message in messages]
    for message in enriched:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] = f"{message['content']}\n\n{instruction}"
            return enriched
    return [{"role": "system", "content": instruction}, *enriched]


def shape_structured_request(
    messages: list[dict[str, Any]],
    *,
    name: str,
    schema: Mapping[str, Any],
    response_format: str,
    include_schema_in_prompt: bool,
    strip_keys: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return ``(messages, response_format)`` for one method; ``None`` means send no format."""
    request_schema = strip_schema_keys(dict(schema), strip_keys)
    shaped = (
        messages_with_schema(messages, request_schema)
        if include_schema_in_prompt
        else [dict(message) for message in messages]
    )
    if response_format == "json_schema":
        return shaped, {
            "type": "json_schema",
            "json_schema": {"name": name, "schema": request_schema},
        }
    if response_format == "json_object":
        return shaped, {"type": "json_object"}
    if response_format == "none":
        return shaped, None
    raise ValueError(f"unsupported structured-output response_format: {response_format!r}")


def shape_for_route(
    messages: list[dict[str, Any]], *, name: str, schema: Mapping[str, Any], route: Any
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Shape for a compiled ``LLMRoute`` (or a route mapping from the compiled catalog)."""

    def field(key: str, default: Any) -> Any:
        if isinstance(route, Mapping):
            return route.get(key, default)
        return getattr(route, key, default)

    return shape_structured_request(
        messages,
        name=name,
        schema=schema,
        response_format=str(field("structured_output_response_format", "json_schema")),
        include_schema_in_prompt=bool(field("structured_output_include_schema_in_prompt", False)),
        strip_keys=tuple(field("structured_output_schema_strip_keys", ()) or ()),
    )
