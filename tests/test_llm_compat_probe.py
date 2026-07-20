import json

import requests

from citypods import llm_compat_probe


def test_native_check_reports_request_exception_instead_of_crashing(monkeypatch):
    """A network-level failure (e.g. a read timeout) must produce a reportable result like any
    other check, not an unhandled exception that aborts the rest of the probe matrix -- this is
    the same guarantee `_litellm()` already documents for its own failures."""

    def raise_timeout(*_args, **_kwargs):
        raise requests.exceptions.ReadTimeout("read timed out")

    monkeypatch.setattr(llm_compat_probe.requests, "post", raise_timeout)

    result = llm_compat_probe._native("gemini-test", {"type": "object"}, "not-a-real-key")

    assert result == {"ok": False, "exception_type": "ReadTimeout", "exception_status": None}


def test_safe_error_excludes_request_material():
    secret = "meeting transcript must not leak"

    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {"error": {"status": "INVALID_ARGUMENT", "message": "invalid argument"}}

    value = llm_compat_probe._safe_error(response=Response())
    assert value == {
        "ok": False,
        "status": 400,
        "provider_code": "INVALID_ARGUMENT",
        "provider_message": "invalid argument",
    }
    assert secret not in str(value)


def test_safe_error_captures_field_violation_details():
    """`message` alone is often just the generic "Request contains an invalid argument." --
    the field-violation detail naming the actually-rejected schema path lives in `details` and
    was previously discarded entirely."""

    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {
                "error": {
                    "status": "INVALID_ARGUMENT",
                    "message": "invalid argument",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.BadRequest",
                            "fieldViolations": [
                                {
                                    "field": "generation_config.response_json_schema",
                                    "description": "schema too deeply nested",
                                }
                            ],
                        }
                    ],
                }
            }

    value = llm_compat_probe._safe_error(response=Response())
    assert "provider_details" in value
    assert "response_json_schema" in value["provider_details"]
    assert "schema too deeply nested" in value["provider_details"]


def test_safe_error_caps_oversized_details():
    class Response:
        status_code = 400

        @staticmethod
        def json():
            return {"error": {"details": [{"description": "x" * 5000}]}}

    value = llm_compat_probe._safe_error(response=Response())
    assert len(value["provider_details"]) == 2000


def test_run_executes_the_named_paths(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(llm_compat_probe, "ensure_llm_contract", lambda: _Contract())
    monkeypatch.setattr(llm_compat_probe, "_native", lambda *_: {"ok": True})
    monkeypatch.setattr(llm_compat_probe, "_litellm", lambda *_: {"ok": True})
    monkeypatch.setattr(llm_compat_probe, "_litellm_backend_fix", lambda *_: {"ok": True})
    assert [row["check"] for row in llm_compat_probe.run("gemini-test")] == [
        "native-simple-schema",
        "native-r5-schema",
        "native-refs-only",
        "native-anyof-nullable-only",
        "native-default-only",
        "native-array-of-refs-only",
        "native-additional-properties-false-only",
        "native-r5-schema-minus-defaults",
        "native-r5-schema-minus-additional-properties",
        "native-r5-schema-minus-constraints",
        "native-r5-schema-minus-enum",
        "native-r5-schema-minus-refs",
        "litellm-json-object",
        "litellm-json-schema",
        "litellm-backend-gemini-fix",
    ]


_CONSTRUCT_MARKERS = {
    "$defs": '"$defs"',
    "$ref": '"$ref"',
    "anyOf": '"anyOf"',
    "default": '"default"',
    "additionalProperties": '"additionalProperties"',
}


def _present_constructs(schema: dict) -> set[str]:
    raw = json.dumps(schema)
    return {name for name, marker in _CONSTRUCT_MARKERS.items() if marker in raw}


def test_bisection_schemas_isolate_one_construct_family_each():
    """Each bisection schema must add only its own target construct(s) to an otherwise-minimal
    schema, or a pass/fail on it wouldn't actually isolate anything from the R5 tag contract's
    combined native-r5-schema failure. $defs/$ref are one inseparable family (a $ref always
    needs a $defs entry to point at); REFS_SCHEMA and ARRAY_OF_REFS_SCHEMA differ only in
    whether the $ref sits directly on a property or inside an array's items, matching how the
    real contract's list[Suggestion]/list[Evidence] fields use it."""
    assert _present_constructs(llm_compat_probe.REFS_SCHEMA) == {"$defs", "$ref"}
    assert _present_constructs(llm_compat_probe.ARRAY_OF_REFS_SCHEMA) == {"$defs", "$ref"}
    assert _present_constructs(llm_compat_probe.ANY_OF_NULLABLE_SCHEMA) == {"anyOf"}
    assert _present_constructs(llm_compat_probe.DEFAULT_VALUE_SCHEMA) == {"default"}
    assert _present_constructs(llm_compat_probe.ADDITIONAL_PROPERTIES_FALSE_SCHEMA) == {
        "additionalProperties"
    }

    assert llm_compat_probe.REFS_SCHEMA["properties"]["inner"] == {"$ref": "#/$defs/Inner"}
    assert llm_compat_probe.ARRAY_OF_REFS_SCHEMA["properties"]["items"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/Inner"},
    }


def test_strip_keys_removes_matching_keys_at_every_depth():
    schema = {
        "type": "object",
        "default": {},
        "properties": {
            "a": {"type": "string", "default": "x", "minLength": 1},
            "b": {"type": "array", "items": {"type": "integer", "default": 0}},
        },
    }
    stripped = llm_compat_probe._strip_keys(schema, frozenset({"default"}))
    assert stripped == {
        "type": "object",
        "properties": {
            "a": {"type": "string", "minLength": 1},
            "b": {"type": "array", "items": {"type": "integer"}},
        },
    }
    assert schema["default"] == {}, "must not mutate the caller's schema"


def test_schema_without_refs_inlines_two_level_nested_chain():
    """The real contract nests $ref inside $ref (Response -> Suggestion -> Evidence), unlike
    the shallow one-level REFS_SCHEMA/ARRAY_OF_REFS_SCHEMA reproductions above -- this must
    flatten both levels, not just the outer one."""
    schema = {
        "$defs": {
            "Evidence": {"type": "object", "properties": {"quote": {"type": "string"}}},
            "Suggestion": {
                "type": "object",
                "properties": {
                    "evidence": {"type": "array", "items": {"$ref": "#/$defs/Evidence"}}
                },
            },
        },
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"$ref": "#/$defs/Suggestion"}}},
    }
    inlined = llm_compat_probe._schema_without_refs(schema)
    assert "$defs" not in inlined
    assert inlined == {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "evidence": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"quote": {"type": "string"}},
                            },
                        }
                    },
                },
            }
        },
    }


def test_run_wires_stripped_schemas_into_the_subtractive_checks(monkeypatch):
    """A typo in run()'s per-check lambda (wrong key set, or probing tag_schema unmodified)
    would silently turn a subtractive check into a duplicate of native-r5-schema -- this drives
    the real run() and inspects what it actually handed to _native() for each check, by name,
    rather than re-deriving the expected schemas separately from run()'s own implementation."""
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    schema = {
        "$defs": {"Inner": {"type": "object", "properties": {"note": {"type": "string"}}}},
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"$ref": "#/$defs/Inner"}},
            "count": {"type": "integer", "default": 0},
        },
        "additionalProperties": False,
    }
    monkeypatch.setattr(llm_compat_probe, "ensure_llm_contract", lambda: _Contract(schema))
    monkeypatch.setattr(llm_compat_probe, "_litellm", lambda *_: {"ok": True})
    monkeypatch.setattr(llm_compat_probe, "_litellm_backend_fix", lambda *_: {"ok": True})

    captured: list[dict] = []

    def fake_native(_model, passed_schema, _api_key):
        captured.append(passed_schema)
        return {"ok": True}

    monkeypatch.setattr(llm_compat_probe, "_native", fake_native)

    names = [row["check"] for row in llm_compat_probe.run("gemini-test")]
    native_names = [name for name in names if name.startswith("native-")]
    schema_by_check = dict(zip(native_names, captured, strict=True))

    minus_defaults = schema_by_check["native-r5-schema-minus-defaults"]
    assert "default" not in json.dumps(minus_defaults)
    assert minus_defaults["properties"]["count"] == {"type": "integer"}

    minus_additional_properties = schema_by_check["native-r5-schema-minus-additional-properties"]
    assert "additionalProperties" not in minus_additional_properties

    minus_refs = schema_by_check["native-r5-schema-minus-refs"]
    assert "$defs" not in minus_refs
    assert minus_refs["properties"]["tags"]["items"] == {
        "type": "object",
        "properties": {"note": {"type": "string"}},
    }

    # The plain native-r5-schema check must still receive the untouched schema.
    assert schema_by_check["native-r5-schema"] == schema


class _Contract:
    def __init__(self, schema: dict | None = None):
        self._schema = schema if schema is not None else {"type": "object"}

    def model_json_schema(self):
        return self._schema
