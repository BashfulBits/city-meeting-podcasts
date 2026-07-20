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


def test_run_executes_the_four_named_paths(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "not-a-real-key")
    monkeypatch.setattr(llm_compat_probe, "ensure_llm_contract", lambda: _Contract())
    monkeypatch.setattr(llm_compat_probe, "_native", lambda *_: {"ok": True})
    monkeypatch.setattr(llm_compat_probe, "_litellm", lambda *_: {"ok": True})
    assert [row["check"] for row in llm_compat_probe.run("gemini-test")] == [
        "native-simple-schema",
        "native-r5-schema",
        "litellm-json-object",
        "litellm-json-schema",
    ]


class _Contract:
    @staticmethod
    def model_json_schema():
        return {"type": "object"}
