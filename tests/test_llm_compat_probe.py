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
    assert [row["check"] for row in llm_compat_probe.run("gemini-test")] == [
        "native-simple-schema",
        "native-r5-schema",
        "native-refs-only",
        "native-anyof-nullable-only",
        "native-default-only",
        "native-array-of-refs-only",
        "native-additional-properties-false-only",
        "litellm-json-object",
        "litellm-json-schema",
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


class _Contract:
    @staticmethod
    def model_json_schema():
        return {"type": "object"}
