"""Provider plugins classify real recorded responses as the evidence established (review/48).

The fixture was recorded 2026-09-24 under provider-scoped v2 dispatch pauses (Slice 1 step 0):
statuses, rate-limit headers and <=160-char error snippets. Every non-2xx row is pinned to the
verdict that evidence supports, so a plugin edit that changes a documented meaning fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from citypods.provider_catalog.classify import classify
from citypods.provider_catalog.probe import _first_event_error
from citypods.provider_catalog.registry import all_rules
from citypods.provider_catalog.rules import Response

FIXTURE = Path(__file__).parent / "fixtures" / "provider_catalog" / "evidence_2026_09_24.json"
EVIDENCE = json.loads(FIXTURE.read_text(encoding="utf-8"))["providers"]

# (provider, model) -> verdict, for every failed canary in the recording.
EXPECTED = {
    ("airforce", "codestral-latest"): "inconclusive",  # 503 every channel unavailable
    ("airforce", "kimi-k2.7-code"): "not_entitled",  # 402 subscription required
    ("airforce", "ministral-14b-2512"): "inconclusive",
    ("airforce", "ministral-14b-latest"): "quota_exhausted",  # 429 global 1 rps
    ("airforce", "nano-banana-2"): "not_entitled",
    ("airforce", "kimi-k3"): "not_entitled",
    ("gemini", "gemini-3.8-flash"): "inconclusive",  # 503 high demand
    ("gemini", "gemini-2.5-pro"): "retired",  # 404 no longer available
    ("gemini", "gemini-2.5-flash"): "retired",
    ("gemini", "gemini-pro-latest"): "not_entitled",  # free-tier quotas with no value
    ("gemini", "gemini-3.1-pro-preview"): "not_entitled",
    ("gemini", "gemini-flash-latest"): "inconclusive",
    ("groq", "qwen/qwen3.6-27b"): "retired",  # 404 does not exist (and gone from /models)
    ("kilo", "z-ai/glm-5.2:free"): "inconclusive",  # 429 upstream rate-limited
    ("nvidia", "deepseek-ai/deepseek-v4.1-flash"): "inconclusive",  # 504 after 302 s
    ("nvidia", "nvidia/llama-3.1-nemoguard-8b-topic-control"): "inconclusive",  # 502
    ("nvidia", "nvidia/llama-nemotron-embed-vl-1b-v2"): "inconclusive",  # bare 404 page
    ("nvidia", "nvidia/nemotron-parse"): "inconclusive",  # 400 no text input
    ("openrouter", "thinkingmachines/inkling:free"): "not_entitled",  # agentic-only
    ("openrouter", "poolside/laguna-xs-2.1:free"): "inconclusive",
    ("sambanova", "DeepSeek-V3.2"): "inconclusive",  # 429 high demand
    ("sambanova", "gpt-oss-120b"): "inconclusive",
    ("zai", "glm-4.7-flash"): "inconclusive",  # 1305 overloaded
    ("zai", "glm-4.5"): "not_entitled",  # 1113 insufficient balance
    ("zai", "glm-4.5-air"): "not_entitled",
    ("zai", "glm-4.6"): "not_entitled",
}


def _response(row: dict) -> Response:
    body = row.get("body", "")
    if row.get("quota_violations"):
        # The snippet is truncated; rebuild the structured part Gemini's classifier reads.
        body = json.dumps(
            [
                {
                    "error": {
                        "code": row["status"],
                        "details": [{"violations": row["quota_violations"]}],
                    }
                }
            ]
        )
    return Response(status=row["status"], headers=row.get("headers") or {}, body=body)


def _rows():
    for provider, record in sorted(EVIDENCE.items()):
        for row in record.get("canaries") or []:
            if "status" in row:
                yield provider, row


@pytest.mark.parametrize(
    ("provider", "row"), list(_rows()), ids=lambda v: v if isinstance(v, str) else v["model"]
)
def test_recorded_response_classifies_as_the_evidence_established(provider, row):
    rules = all_rules()[provider]
    result = classify(_response(row), rules)
    if row["status"] is not None and 200 <= row["status"] < 300 and not row["first_event_error"]:
        assert result.verdict == "proven"
    elif row["status"] is not None and row["status"] >= 300:
        expected = "not_served" if row["group"] == "no_card" else EXPECTED[(provider, row["model"])]
        assert result.verdict == expected, row["body"]


def test_every_nvidia_model_without_a_build_card_is_not_served():
    rows = [r for r in EVIDENCE["nvidia"]["canaries"] if r["group"] == "no_card"]
    assert len(rows) == 8
    for row in rows:
        assert classify(_response(row), all_rules()["nvidia"]).verdict == "not_served"


def test_every_failed_recorded_canary_has_a_pinned_verdict():
    failed = {(p, r["model"]) for p, r in _rows() if r["status"] and r["status"] >= 300}
    nvidia_no_card = {
        ("nvidia", r["model"]) for r in EVIDENCE["nvidia"]["canaries"] if r["group"] == "no_card"
    }
    assert failed - nvidia_no_card == set(EXPECTED)


def test_first_event_parsing_is_structural_not_substring():
    # A 200 stream whose first event is an error is not a completion (NVIDIA, 2026-09-24).
    overloaded = 'data: {"error":{"message":"Service temporarily overloaded","code":503}}\n\n'
    assert _first_event_error(overloaded)
    # An ordinary chunk may carry `"error": null`; keep-alive comments are skipped.
    chunk = ': OPENROUTER PROCESSING\n\ndata: {"id":"x","choices":[{"delta":{}}],"error":null}\n\n'
    assert not _first_event_error(chunk)
    assert not _first_event_error("data: [DONE]\n\n")


def test_timeouts_and_transport_errors_are_never_evidence():
    for rules in all_rules().values():
        assert classify(Response(status=None, timed_out=True), rules).verdict == "inconclusive"
        assert (
            classify(Response(status=None, transport_error="ConnectionError"), rules).verdict
            == "inconclusive"
        )


def test_a_200_whose_first_event_is_an_error_is_not_proven():
    response = Response(status=200, body="data: {}", first_event_error=True)
    for rules in all_rules().values():
        assert classify(response, rules).verdict != "proven"


@pytest.mark.parametrize(
    ("content", "finish", "expected"),
    [
        ('{"answer":"ok"}', "stop", "valid"),
        ('{"answer":"wrong"}', "stop", "invalid"),
        ('{"answer":"ok","extra":1}', "stop", "invalid"),
        ("not JSON", "stop", "invalid"),
        ("", "stop", "empty"),
        ('{"answer":"ok"}', "length", "inconclusive"),
    ],
)
def test_structured_canary_requires_visible_completed_schema_valid_json(content, finish, expected):
    from citypods.provider_catalog.probe import STRUCTURED_METHODS, structured_canary

    class Stream:
        status_code = 200
        ok = True
        closed = False
        headers = {"x-request-id": "request-123"}

        def iter_content(self, **kwargs):
            for line in self.iter_lines(decode_unicode=False, delimiter=b"\n"):
                yield (line.encode() if isinstance(line, str) else line) + b"\n"

        def iter_lines(self, **kwargs):
            # BeatAPI's reasoning-only first event is legitimate but is not JSON evidence.
            yield "data: " + json.dumps(
                {
                    "id": "chatcmpl-test",
                    "choices": [{"delta": {"reasoning_content": "thinking"}}],
                }
            )
            yield ""
            yield "data: " + json.dumps({"choices": [{"delta": {"content": content}}]})
            yield ""
            yield "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]})
            yield ""
            yield "data: " + json.dumps(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 4,
                        "total_tokens": 16,
                        "completion_tokens_details": {"reasoning_tokens": 2},
                        "prompt_tokens_details": {"cached_tokens": 3},
                    },
                }
            )
            yield ""
            yield "data: [DONE]"

        def close(self):
            self.closed = True

    class Session:
        def __init__(self):
            self.requests, self.responses = [], []

        def post(self, url, **kwargs):
            self.requests.append(kwargs["json"])
            response = Stream()
            self.responses.append(response)
            return response

    methods = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config/provider_limits.yml").read_text()
    )["structured_output_methods"]
    session = Session()
    result = structured_canary(
        all_rules()["beatapi"],
        {"api_base": "https://beat.test/v1"},
        "deepseek-v4.1-flash-free",
        "test-key",
        session,
        before_attempt=lambda: True,
        methods=methods,
    )
    assert tuple(result) == STRUCTURED_METHODS
    assert {r["outcome"] for r in result.values()} == {expected}
    assert all(r.closed for r in session.responses)
    assert all(r["max_tokens"] > 16 for r in session.requests)
    expected_events = 5 if finish == "stop" else 3
    assert all(result["events"] == expected_events for result in result.values())
    assert all(result["content_bytes"] == len(content.encode()) for result in result.values())
    assert all(result["finish_reason"] == finish for result in result.values())
    assert all(result["stream_terminal"] for result in result.values())
    assert all(result["provider_request_id"] == "request-123" for result in result.values())
    assert all(result["response_id"] == "chatcmpl-test" for result in result.values())
    if finish == "stop":
        assert all(
            result["usage"]
            == {
                "prompt_tokens": 12,
                "completion_tokens": 4,
                "total_tokens": 16,
                "reasoning_tokens": 2,
                "cached_tokens": 3,
            }
            for result in result.values()
        )
    else:
        assert all("usage" not in result for result in result.values())
    assert "response_format" not in session.requests[-1]
    assert (
        session.requests[0]["response_format"]["json_schema"]["schema"]["properties"]["answer"][
            "maxLength"
        ]
        == 2
    )
    assert (
        "maxLength"
        not in session.requests[1]["response_format"]["json_schema"]["schema"]["properties"][
            "answer"
        ]
    )
    assert all("first_byte_seconds" in r and "completion_seconds" in r for r in result.values())
    if expected == "valid":
        assert all(r["diagnostic"] == "fixture_json_valid" for r in result.values())
        assert all("completion_preview" not in r for r in result.values())
    elif finish != "stop":
        assert all(r["diagnostic"] == f"finish_reason_{finish}" for r in result.values())
        assert all("completion_preview" in r for r in result.values())
    elif not content:
        assert all(r["diagnostic"] == "empty_content" for r in result.values())
    else:
        assert all(r["diagnostic"] == "fixture_json_mismatch" for r in result.values())


def test_structured_canary_joins_multiline_sse_data_fields():
    from citypods.provider_catalog.probe import STRUCTURED_METHODS, structured_canary

    class Stream:
        status_code = 200
        ok = True
        headers = {}

        def iter_content(self, **kwargs):
            for line in self.iter_lines(decode_unicode=False, delimiter=b"\n"):
                yield (line.encode() if isinstance(line, str) else line) + b"\n"

        def iter_lines(self, **kwargs):
            content_frame = json.dumps(
                {"choices": [{"delta": {"content": '{"answer":"ok"}'}}]}, indent=2
            )
            for line in content_frame.splitlines():
                yield "data: " + line
            yield ""
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield ""
            yield "data: [DONE]"
            yield ""

        def close(self):
            pass

    class Session:
        def post(self, *args, **kwargs):
            return Stream()

    methods = {
        method: {"response_format": "none", "include_schema_in_prompt": True}
        for method in STRUCTURED_METHODS
    }
    result = structured_canary(
        all_rules()["openrouter"],
        {"api_base": "https://openrouter.test/api/v1"},
        "creator/model:free",
        "test-key",
        Session(),
        before_attempt=lambda: True,
        methods=methods,
    )

    assert {row["outcome"] for row in result.values()} == {"valid"}
    assert {row["events"] for row in result.values()} == {3}


def test_malformed_sse_records_bounded_parse_and_encoding_diagnostics():
    from citypods.provider_catalog.probe import structured_canary

    class Stream:
        status_code = 200
        ok = True
        closed = False
        encoding = "ISO-8859-1"
        headers = {
            "content-type": "text/event-stream; charset=utf-8",
            "content-encoding": "identity",
            "x-request-id": "req-debug-123",
        }

        def iter_content(self, **kwargs):
            for line in self.iter_lines(decode_unicode=False, delimiter=b"\n"):
                yield (line.encode() if isinstance(line, str) else line) + b"\n"

        def iter_lines(self, **kwargs):
            malformed = (
                '{"choices":[{"delta":{"reasoning_content":"' + ("x" * 180) + '"}}]} trailing'
            )
            yield "data: " + malformed
            yield ""

        def close(self):
            self.closed = True

    class Session:
        def __init__(self):
            self.response = Stream()

        def post(self, *args, **kwargs):
            return self.response

    methods = {
        "json_schema": {
            "response_format": "json_schema",
            "include_schema_in_prompt": False,
        }
    }
    attempts = iter((True, False))
    session = Session()
    result = structured_canary(
        all_rules()["openrouter"],
        {"api_base": "https://openrouter.test/api/v1"},
        "creator/model:free",
        "test-key",
        session,
        before_attempt=lambda: next(attempts),
        methods=methods,
    )["json_schema"]

    assert result["outcome"] == "inconclusive"
    assert result["diagnostic"] == "malformed_sse_json"
    assert result["json_error"] == "Extra data"
    assert result["json_error_line"] == 1
    assert result["json_error_offset"] > 128
    assert result["last_event_data_lines"] == 1
    assert result["sse_data_lines"] == 1
    assert result["content_type"] == "text/event-stream; charset=utf-8"
    assert result["content_encoding"] == "identity"
    assert result["response_encoding"] == "ISO-8859-1"
    assert result["provider_request_id"] == "req-debug-123"
    assert result["utf8_redecode_parses"] is False
    preview = json.loads(result["malformed_event_preview"])
    assert len(preview) <= 128
    assert "trailing" in preview
    assert result["malformed_event_preview_start"] > 0
    assert "test-key" not in str(result)
    assert session.response.closed


@pytest.mark.parametrize(("reasoning_events", "expected"), [(80, "valid"), (140, "inconclusive")])
def test_structured_canary_allows_reasoning_stream_within_bound_and_rejects_overflow(
    reasoning_events, expected
):
    from citypods.provider_catalog.probe import STRUCTURED_METHODS, structured_canary

    class Stream:
        status_code = 200
        ok = True
        headers = {}

        def iter_content(self, **kwargs):
            for line in self.iter_lines(decode_unicode=False, delimiter=b"\n"):
                yield (line.encode() if isinstance(line, str) else line) + b"\n"

        def iter_lines(self, **kwargs):
            for _ in range(reasoning_events):
                frame = json.dumps({"choices": [{"delta": {"reasoning_content": "x" * 900}}]})
                yield "data: " + frame
                yield ""
            yield 'data: {"choices":[{"delta":{"content":"{\\"answer\\":\\"ok\\"}"}}]}'
            yield ""
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield ""
            yield "data: [DONE]"

        def close(self):
            pass

    class Session:
        def post(self, *args, **kwargs):
            return Stream()

    methods = {
        method: {"response_format": "none", "include_schema_in_prompt": True}
        for method in STRUCTURED_METHODS
    }
    result = structured_canary(
        all_rules()["openrouter"],
        {"api_base": "https://openrouter.test/api/v1"},
        "creator/model:free",
        "test-key",
        Session(),
        before_attempt=lambda: True,
        methods=methods,
    )

    assert {row["outcome"] for row in result.values()} == {expected}
    if expected == "valid":
        assert all(row["event_bytes"] > 65_536 for row in result.values())
        assert all(row["diagnostic"] == "fixture_json_valid" for row in result.values())
    else:
        assert {row["diagnostic"] for row in result.values()} == {"event_data_limit_exceeded"}


def test_structured_probe_preview_is_bounded_and_safe_for_issue_markdown():
    from citypods.provider_catalog.probe import (
        _MAX_STRUCTURED_PREVIEW_CHARS,
        _safe_structured_preview,
    )

    source = "`@owner <tag> [label](url) !#|*_~\n" + ("x" * 500)
    preview = _safe_structured_preview(source)
    assert len(json.loads(preview)) == _MAX_STRUCTURED_PREVIEW_CHARS
    assert all(char not in preview for char in "`@<>[]()!#|*_~")
    assert "\\u0060" in preview and "\\u0040" in preview


def test_structured_probe_records_a_bounded_http_error_preview():
    from citypods.provider_catalog.probe import STRUCTURED_METHODS, structured_canary

    class Failed:
        status_code = 400
        ok = False
        closed = False
        headers = {"x-request-id": "req-400"}

        def iter_content(self, chunk_size):
            yield (
                b'{"error":{"message":"unsupported response_format","echo":"test-key"}}'
                + b"x" * 2_000
            )

        def close(self):
            self.closed = True

    class Session:
        def post(self, *args, **kwargs):
            return response

    response = Failed()
    result = structured_canary(
        all_rules()["beatapi"],
        {"api_base": "https://beat.test/v1"},
        "chat-free",
        "test-key",
        Session(),
        before_attempt=lambda: True,
        methods={
            method: {
                "response_format": "json_schema",
                "include_schema_in_prompt": False,
                "strip_schema_keys": [],
            }
            for method in STRUCTURED_METHODS
        },
    )
    assert response.closed
    assert result["json_schema"]["outcome"] == "rejected"
    assert result["json_schema"]["diagnostic"] == "http_400"
    assert result["json_schema"]["provider_request_id"] == "req-400"
    error_preview = json.loads(result["json_schema"]["error_preview"])
    assert "unsupported response_format" in error_preview
    assert "test-key" not in error_preview
    assert "[redacted]" in error_preview
    assert result["json_schema"]["error_body_bytes"] == 1_024
    assert result["json_schema"]["error_body_truncated"]


def test_beatapi_plugin_excludes_jev_and_classifies_account_window_exhaustion():
    rules = all_rules()["beatapi"]
    assert not rules.chat_filter("jev-1.13-free", {"owned_by": "task plugin"})
    assert rules.free_evidence("deepseek-v4.1-flash-free", {}, {})
    assert not rules.free_evidence("deepseek-v4.1-flash", {}, {})
    response = Response(status=429, body='{"error":{"code":"rate_limit_exceeded"}}')
    assert classify(response, rules).verdict == "quota_exhausted"
    assert rules.canary_interval_seconds == 65


@pytest.mark.parametrize("status", [402, 403, 404, 410, 429])
def test_schema_capacity_or_access_failure_stops_probing_without_a_method_verdict(status):
    from citypods.provider_catalog.probe import structured_canary

    class Failed:
        status_code = status
        ok = False
        closed = False

        def close(self):
            self.closed = True

    class Session:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return response

    response, session = Failed(), Session()
    methods = {"json_schema": {"response_format": "json_schema", "include_schema_in_prompt": False}}
    result = structured_canary(
        all_rules()["beatapi"],
        {"api_base": "https://beat.test/v1"},
        "chat-free",
        "test-key",
        session,
        before_attempt=lambda: True,
        methods=methods,
    )
    assert session.calls == 1 and response.closed
    assert result["json_schema"]["outcome"] == "inconclusive"
    assert "completion_seconds" in result["json_schema"]


def test_schema_timeout_is_inconclusive_and_every_response_is_closed():
    import requests

    from citypods.provider_catalog.probe import STRUCTURED_METHODS, structured_canary

    class Stream:
        status_code = 200
        ok = True
        closed = False

        def iter_content(self, **kwargs):
            for line in self.iter_lines(decode_unicode=False, delimiter=b"\n"):
                yield (line.encode() if isinstance(line, str) else line) + b"\n"

        def iter_lines(self, **kwargs):
            raise requests.Timeout()

        def close(self):
            self.closed = True

    class Session:
        def post(self, *args, **kwargs):
            response = Stream()
            responses.append(response)
            return response

    responses = []
    methods = {
        m: {"response_format": "none", "include_schema_in_prompt": True} for m in STRUCTURED_METHODS
    }
    result = structured_canary(
        all_rules()["beatapi"],
        {"api_base": "https://beat.test/v1"},
        "chat-free",
        "test-key",
        Session(),
        before_attempt=lambda: True,
        methods=methods,
    )
    assert {r["outcome"] for r in result.values()} == {"inconclusive"}
    assert len(responses) == 4 and all(r.closed for r in responses)


@pytest.mark.parametrize("encoding", ["ISO-8859-1", "utf-8"])
@pytest.mark.parametrize("reasoning", ["ⅅ", "\u2028", "\u2029"])
@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"])
def test_structured_canary_frames_real_requests_bytes_before_unicode(encoding, reasoning, newline):
    """Exercise Requests itself: Unicode splitlines can split inside a valid JSON event."""
    import requests

    from citypods.provider_catalog.probe import structured_canary

    frames = [
        {"choices": [{"delta": {"reasoning_content": reasoning}}]},
        {"choices": [{"delta": {"content": '{"answer":"ok"}'}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    wire = (
        b"".join(
            b"data: " + json.dumps(frame, ensure_ascii=False).encode() + newline * 2
            for frame in frames
        )
        + b"data: [DONE]"
        + newline * 2
    )

    class Raw:
        def stream(self, *args, **kwargs):
            # Split within multibyte text and CRLF as well as ordinary JSON tokens.
            for index in range(0, len(wire), 7):
                yield wire[index : index + 7]

        def close(self):
            pass

        def release_conn(self):
            pass

    def response():
        result = requests.Response()
        result.status_code = 200
        result.headers["content-type"] = "text/event-stream"
        result.encoding = encoding
        result.raw = Raw()
        return result

    # Demonstrate the previous implementation's failure with the same response bytes.
    old_lines = list(response().iter_lines(decode_unicode=True))
    first_payload = json.dumps(frames[0], ensure_ascii=False).encode()
    legacy_extra = len(first_payload.decode(encoding).splitlines()) - 1
    if legacy_extra:
        with pytest.raises(json.JSONDecodeError):
            json.loads(old_lines[0][6:])

    class Session:
        def post(self, *args, **kwargs):
            return response()

    attempts = iter((True, False))
    result = structured_canary(
        all_rules()["nvidia"],
        {"api_base": "https://nvidia.test/v1"},
        "nvidia/test-model",
        "test-key",
        Session(),
        before_attempt=lambda: next(attempts),
        methods={"json_schema": {"response_format": "none", "include_schema_in_prompt": True}},
    )["json_schema"]
    assert result["outcome"] == "valid"
    assert result["stream_terminal"] is True
    assert result["last_event_delimited"] is True
    assert result["events"] == 4
    assert result["legacy_unicode_extra_line_breaks"] == legacy_extra
    assert result["stream_decoder"] == "utf8_byte_lines_v1"
    assert result["runtime"]["requests"] == requests.__version__


@pytest.mark.parametrize("delimited", [True, False])
def test_structured_canary_retains_exact_bounded_failure_bytes(delimited):
    import base64

    from citypods.provider_catalog.probe import structured_canary

    malformed = b'{"key":"test-key", "reasoning_content":"' + b"x" * 200

    class Stream:
        status_code = 200
        ok = True
        headers = {}
        encoding = "ISO-8859-1"

        def iter_content(self, **kwargs):
            for line in self.iter_lines(decode_unicode=False, delimiter=b"\n"):
                yield (line.encode() if isinstance(line, str) else line) + b"\n"

        def iter_lines(self, **kwargs):
            assert kwargs == {"decode_unicode": False, "delimiter": b"\n"}
            yield b"data: " + malformed
            if delimited:
                yield b""

        def close(self):
            pass

    class Session:
        def post(self, *args, **kwargs):
            return Stream()

    attempts = iter((True, False))
    result = structured_canary(
        all_rules()["nvidia"],
        {"api_base": "https://nvidia.test/v1"},
        "nvidia/test-model",
        "test-key",
        Session(),
        before_attempt=lambda: next(attempts),
        methods={"json_schema": {"response_format": "none", "include_schema_in_prompt": True}},
    )["json_schema"]
    assert result["diagnostic"] == "malformed_sse_json"
    assert result["last_event_delimited"] is delimited
    preview = base64.b64decode(result["malformed_event_byte_preview_base64"])
    assert len(preview) <= 128
    assert b"test-key" not in preview
    assert b"[redacted]" in preview
    assert result["malformed_event_utf8_bytes"] == len(malformed)


def test_structured_canary_reports_invalid_utf8_without_replacement_or_secret_leak():
    import base64

    from citypods.provider_catalog.probe import structured_canary

    class Stream:
        status_code = 200
        ok = True
        headers = {}
        encoding = "ISO-8859-1"
        closed = False

        def iter_content(self, **kwargs):
            for line in self.iter_lines(decode_unicode=False, delimiter=b"\n"):
                yield (line.encode() if isinstance(line, str) else line) + b"\n"

        def iter_lines(self, **kwargs):
            yield b'data: {"key":"test-key","reasoning_content":"\xe2'

        def close(self):
            self.closed = True

    response = Stream()

    class Session:
        def post(self, *args, **kwargs):
            return response

    attempts = iter((True, False))
    result = structured_canary(
        all_rules()["nvidia"],
        {"api_base": "https://nvidia.test/v1"},
        "nvidia/test-model",
        "test-key",
        Session(),
        before_attempt=lambda: next(attempts),
        methods={"json_schema": {"response_format": "none", "include_schema_in_prompt": True}},
    )["json_schema"]
    assert result["outcome"] == "inconclusive"
    assert result["diagnostic"] == "invalid_sse_utf8"
    assert result["utf8_error_reason"] == "unexpected end of data"
    assert result["utf8_error_end"] == result["invalid_utf8_line_bytes"]
    excerpt = base64.b64decode(result["invalid_utf8_preview_base64"])
    assert len(excerpt) <= 64
    assert b"test-key" not in excerpt
    assert excerpt.endswith(b"\xe2")
    assert response.closed
