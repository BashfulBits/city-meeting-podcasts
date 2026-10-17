"""The provider-plugin contract for catalog reconciliation (review/48 R2).

Each provider's knowledge -- how to list its models, what counts as free-tier evidence, what its
error responses mean, where a human can research a model -- lives in one
``providers/<name>.py`` file that exports ``RULES = ProviderRules(...)``. The reconciler core never
branches on a provider name, so adding a provider is one file plus its fixture test, and removing
one is deleting that file.

Everything here is declarative or a small pure predicate built from the shared helpers below, so a
provider file reads as a table rather than as code paths.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import quote

# What one canary response proves about one model on one account. Only `proven`, `retired` and
# `not_served` can ever change config (add / remove); everything else is reported, never acted on.
Verdict = Literal[
    "proven",  # the model served a completion on this account
    "retired",  # the provider says the model is gone (e.g. 410 end of life)
    "not_served",  # the provider has no endpoint for it on this account (listed but dead)
    "not_entitled",  # a paid model, or one outside this account's plan/tier
    "account_blocked",  # the account's plan provisions zero capacity for it
    "quota_exhausted",  # a real, temporarily spent quota: defer, never a verdict on the model
    "inconclusive",  # anything else, including every timeout and transport error
]
ACTIONABLE_VERDICTS = frozenset({"proven", "retired", "not_served"})


@dataclass(frozen=True)
class Response:
    """What a canary observed; raw bodies are transient and manual error excerpts are bounded."""

    status: int | None
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""
    first_event_error: bool = False
    timed_out: bool = False
    transport_error: str | None = None
    context_diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        name = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == name), None)

    def json(self) -> Any:
        try:
            data = json.loads(self.body)
        except (TypeError, ValueError):
            return None
        return data[0] if isinstance(data, list) and data else data


Matcher = Callable[[Response], bool]


def always(_: Response) -> bool:
    return True


def body_contains(*needles: str) -> Matcher:
    """Every needle appears in the body, case-insensitively."""
    lowered = tuple(n.lower() for n in needles)
    return lambda r: all(n in r.body.lower() for n in lowered)


def body_matches(pattern: str) -> Matcher:
    compiled = re.compile(pattern, re.I)
    return lambda r: bool(compiled.search(r.body))


def json_error_code(code: str) -> Matcher:
    """The provider's own error code, wherever its envelope puts it (`error.code` or `code`)."""

    def match(r: Response) -> bool:
        data = r.json()
        if not isinstance(data, dict):
            return False
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        return str(error.get("code", data.get("code", ""))) == code

    return match


def header_equals(name: str, value: str) -> Matcher:
    return lambda r: r.header(name) == value


def all_of(*matchers: Matcher) -> Matcher:
    return lambda r: all(m(r) for m in matchers)


@dataclass(frozen=True)
class Signal:
    """One documented response shape and what it means. The first matching signal wins."""

    verdict: Verdict
    status: int | None  # None matches any status
    match: Matcher = always
    note: str = ""  # human-readable evidence reference (e.g. "live 2026-09-24")

    def matches(self, response: Response) -> bool:
        return (self.status is None or response.status == self.status) and self.match(response)


# ---- Free-tier evidence ------------------------------------------------------------------------
# A predicate over (model_id, catalog record, provider context) returning True (free evidence),
# False (explicitly not free) or None (the evidence source itself was unavailable -> inconclusive).
FreeEvidence = Callable[[str, Mapping[str, Any], Mapping[str, Any]], bool | None]


def _is_zero(value: Any) -> bool:
    try:
        return float(str(value)) == 0
    except (TypeError, ValueError):
        return False


def zero_price(*keys: str) -> FreeEvidence:
    """Explicit zero prices for every key under `pricing`; absent pricing is not free."""

    def check(_model: str, record: Mapping[str, Any], _ctx: Mapping[str, Any]) -> bool:
        pricing = record.get("pricing")
        return isinstance(pricing, Mapping) and all(
            k in pricing and _is_zero(pricing[k]) for k in keys
        )

    return check


def id_suffix(suffix: str) -> FreeEvidence:
    return lambda model, _r, _c: model.endswith(suffix)


def name_contains(text: str) -> FreeEvidence:
    return lambda _m, record, _c: text.lower() in str(record.get("name") or "").lower()


def field_equals(key: str, value: str) -> FreeEvidence:
    return lambda _m, record, _c: str(record.get(key) or "").lower() == value.lower()


def account_level() -> FreeEvidence:
    """The account's plan is free as a whole; the canary proves per-model access."""
    return lambda _m, _r, _c: True


def all_free(*checks: FreeEvidence) -> FreeEvidence:
    def check(model: str, record: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool | None:
        results = [c(model, record, ctx) for c in checks]
        if any(r is None for r in results):
            return None
        return all(results)

    return check


# ---- Catalog listing ---------------------------------------------------------------------------
CatalogStyle = Literal["openai", "google"]
AuthStyle = Literal["bearer", "google_api_key"]


@dataclass(frozen=True)
class CatalogSpec:
    path: str = "/models"  # appended to the provider's api_base unless `url` is set
    style: CatalogStyle = "openai"
    auth: AuthStyle = "bearer"
    url: str | None = None  # an absolute listing URL when it is not under api_base


NON_CHAT = re.compile(
    r"embed|rerank|guard|safety|moderation|whisper|\btts\b|-tts|speech|transcri|ocr|parse|"
    r"voxtral|orpheus|playai|retriever|reward|deplot|clip|compound",
    re.I,
)


def default_chat_filter(model: str, _record: Mapping[str, Any]) -> bool:
    return not NON_CHAT.search(model)


def default_links(model: str) -> tuple[tuple[str, str], ...]:
    name = model.split("/")[-1].removesuffix(":free").removesuffix("-free")
    return (
        ("Hugging Face search", f"https://huggingface.co/models?search={quote(name)}"),
        ("Artificial Analysis models", "https://artificialanalysis.ai/models"),
    )


def namespaced_identity(suffix: str = ""):
    """Use a plugin-authorized publisher/model ID verbatim; bare/ambiguous IDs stay unknown."""

    def identify(model, _record):
        value = model.removesuffix(suffix) if suffix else model
        return value if re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.:-]+", value) else None

    return identify


@dataclass(frozen=True)
class ProviderRules:
    """Everything the reconciler knows about one provider. See module docstring."""

    name: str
    catalog: CatalogSpec = field(default_factory=CatalogSpec)
    # None means observation-only: the catalog is inspected, but nothing is ever proposed.
    free_evidence: FreeEvidence | None = None
    signals: tuple[Signal, ...] = ()
    chat_filter: Callable[[str, Mapping[str, Any]], bool] = default_chat_filter
    # A decoration the provider adds to the publisher's model ID to mark its free variant; it is
    # stripped before matching the model to Artificial Analysis.
    free_suffix: str = ""
    # The model creator for bare (un-namespaced) IDs, e.g. Gemini's `gemini-3.5-flash` -> google.
    creator: str | None = None
    research_links: Callable[[str], tuple[tuple[str, str], ...]] = default_links
    # Optional per-run context builder (e.g. NVIDIA's Build Catalog labels); its result is passed
    # to `free_evidence` as the third argument.
    prepare: Callable[[Any], Mapping[str, Any]] | None = None
    # Chat path override for canaries when the provider's compiled `chat_path` is not directly
    # appendable to `api_base` (Airforce's api_base already ends in /v1).
    canary_path: str | None = None
    # False when the provider's list omits models it actually serves (z.ai's free flash models):
    # a configured model's absence from the catalog is then not reported as drift.
    catalog_is_complete: bool = True
    # Minimum seconds between this provider's canaries (Airforce enforces a global 1 req/s limit;
    # back-to-back canaries there all came back 429 in the 2026-09-24 dry run).
    canary_interval_seconds: float = 0.0

    # Pure plugin callbacks; unknown identity/scope fails closed when proposing config.
    model_identity: Callable[[str, Mapping[str, Any]], str | None] = lambda _m, _r: None
    limit_observations: Callable[[Response], tuple[tuple[str, int, str], ...]] = lambda _r: ()

    context_observation: Callable = lambda response, request: unsupported_context(response, request)
    context_rate_diagnostics: Callable = lambda _headers: {}
    discovery_axes: tuple[str, ...] = ()
    # Opt in only for a documented and verified endpoint/model combination. This is a
    # discovery transport capability, not a model limit or a production dispatch control.
    discovery_output_min_tokens: Callable[[str, str], bool] = lambda _endpoint, _model: False
    discovery_output_stream_usage: Callable[[str, str], bool] = lambda _endpoint, _model: False
    discovery_parser_version: str = "unsupported-v1"
    discovery_observation: Callable = lambda response, request, transient: unsupported_discovery(
        response, request, transient
    )

    @property
    def observation_only(self) -> bool:
        return self.free_evidence is None


# ---- Shared signals (provider-neutral wording seen on several OpenAI-compatible APIs) ----------
# A provider opts in by listing these in its own `signals`; nothing is applied implicitly.
END_OF_LIFE = Signal("retired", 410, body_contains("end of life"), "410 end of life")
MODEL_DOES_NOT_EXIST = Signal(
    "retired",
    404,
    body_matches(r"does not exist|model[_ ]not[_ ]found|no such model|invalid model"),
    "404 model does not exist",
)


def unsupported_context(response, request):
    """Undocumented endpoint envelopes supply no token or boundary authority."""
    from citypods.provider_catalog.evidence import ContextObservation

    return ContextObservation.from_request(request)


def chat_context_observation(
    response,
    request,
    *,
    reasoning_basis="unknown",
    rate_limit_requested_tokens=None,
    rate_limit_parser_version="not_recorded",
):
    """Strict documented chat usage; size/quota error numbers never become context bounds.

    Opt-in plugins own reasoning semantics. See their endpoint references; compatibility alone
    does not enable this helper. Input success requires all three exact positioned sentinels.
    """
    from citypods.provider_catalog.evidence import ContextObservation

    def make(*, diagnostic_code=None, **values):
        diagnostic_fields = {
            "diagnostic_code",
            "http_status",
            "http_error_class",
            "provider_error_class",
            "response_media_type",
            "response_bytes",
            "duration_ms",
            "first_byte_ms",
            "admitted_at_ms",
            "request_started_at_ms",
            "request_finished_at_ms",
            "rate_limit_limit_tokens",
            "rate_limit_requested_tokens",
            "rate_limit_remaining_tokens",
            "rate_limit_reset_tokens_ms",
            "rate_limit_limit_requests",
            "rate_limit_remaining_requests",
            "rate_limit_reset_requests_ms",
            "retry_after_ms",
            "error_response_body",
            "error_response_body_truncated",
        }
        diagnostics = {
            key: value
            for key, value in response.context_diagnostics.items()
            if key in diagnostic_fields
        }
        diagnostics.setdefault("http_status", response.status)
        diagnostics.setdefault("http_error_class", _context_http_error_class(response.status))
        diagnostics.setdefault("provider_error_class", "none")
        diagnostics.setdefault("response_media_type", "unknown")
        diagnostics.setdefault("response_bytes", None)
        diagnostics.setdefault("duration_ms", None)
        diagnostics.setdefault("first_byte_ms", None)
        diagnostics.setdefault("admitted_at_ms", None)
        diagnostics.setdefault("request_started_at_ms", None)
        diagnostics.setdefault("request_finished_at_ms", None)
        diagnostics.setdefault("rate_limit_limit_tokens", None)
        diagnostics.setdefault("rate_limit_requested_tokens", rate_limit_requested_tokens)
        diagnostics.setdefault("rate_limit_parser_version", rate_limit_parser_version)
        diagnostics.setdefault("rate_limit_remaining_tokens", None)
        diagnostics.setdefault("rate_limit_reset_tokens_ms", None)
        diagnostics.setdefault("rate_limit_limit_requests", None)
        diagnostics.setdefault("rate_limit_remaining_requests", None)
        diagnostics.setdefault("rate_limit_reset_requests_ms", None)
        diagnostics.setdefault("retry_after_ms", None)
        diagnostics.setdefault("error_response_body", "")
        diagnostics.setdefault("error_response_body_truncated", False)
        diagnostics["diagnostic_code"] = (
            diagnostic_code or diagnostics.get("diagnostic_code") or "unclassified_response"
        )
        return ContextObservation.from_request(request, **(diagnostics | values))

    if response.timed_out or response.transport_error:
        return make(
            diagnostic_code=response.context_diagnostics.get("diagnostic_code", "transport_error"),
            outcome="transport",
        )
    if (
        response.status == 429
        or response.context_diagnostics.get("provider_error_class") == "rate_limited"
    ):
        code = "http_429_rate_limited" if response.status == 429 else "provider_rate_limited"
        return make(diagnostic_code=code, outcome="quota")
    if response.status != 200:
        return make(diagnostic_code="http_non_success", outcome="inconclusive")

    try:
        data = strict_context_json(response.body)
    except (TypeError, ValueError):
        return make(diagnostic_code="invalid_json")
    if not isinstance(data, dict) or data.get("object") != "chat.completion" or data.get("error"):
        return make(diagnostic_code="unexpected_response_envelope")
    usage = data.get("usage")
    choices = data.get("choices")
    if not isinstance(usage, dict):
        return make(diagnostic_code="usage_missing_or_invalid")
    if not isinstance(choices, list) or len(choices) != 1:
        return make(diagnostic_code="completion_shape_invalid")
    counts = {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    if any(
        type(counts[k]) is not int or not 0 <= counts[k] <= 2**53 - 1
        for k in ("prompt_tokens", "completion_tokens")
    ):
        return make(diagnostic_code="usage_counts_invalid")
    total = counts["total_tokens"]
    if total is not None and (
        type(total) is not int or total != counts["prompt_tokens"] + counts["completion_tokens"]
    ):
        return make(diagnostic_code="usage_total_inconsistent")
    observed_counts = dict(
        reported_input=counts["prompt_tokens"],
        reported_output=counts["completion_tokens"],
        reported_total=total,
        count_basis=request.dimension,
        reasoning_basis=reasoning_basis,
    )
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("error"):
        return make(diagnostic_code="completion_shape_invalid")
    message = choice.get("message")
    if not isinstance(message, dict):
        return make(
            diagnostic_code="completion_message_missing"
            if "message" not in choice
            else "completion_message_not_object",
            outcome="inconclusive",
            **observed_counts,
        )
    if message.get("refusal"):
        return make(
            diagnostic_code="completion_refusal",
            outcome="inconclusive",
            **observed_counts,
        )
    if message.get("tool_calls"):
        return make(
            diagnostic_code="completion_tool_call",
            outcome="inconclusive",
            **observed_counts,
        )
    content, finish = message.get("content"), choice.get("finish_reason")
    if "content" not in message:
        return make(
            diagnostic_code="completion_content_missing",
            outcome="inconclusive",
            **observed_counts,
        )
    if content is None:
        return make(
            diagnostic_code="completion_content_null",
            outcome="inconclusive",
            **observed_counts,
        )
    if not isinstance(content, str):
        return make(
            diagnostic_code="completion_content_not_text",
            outcome="inconclusive",
            **observed_counts,
        )
    if not content:
        return make(
            diagnostic_code="completion_content_empty",
            outcome="inconclusive",
            **observed_counts,
        )
    if "finish_reason" not in choice:
        return make(
            diagnostic_code="finish_reason_missing",
            outcome="inconclusive",
            **observed_counts,
        )
    if finish is None:
        return make(
            diagnostic_code="finish_reason_null",
            outcome="inconclusive",
            **observed_counts,
        )
    if finish not in ("stop", "length"):
        return make(
            diagnostic_code="finish_reason_unsupported",
            outcome="inconclusive",
            **observed_counts,
        )
    detail = usage.get("completion_tokens_details")
    if detail is not None and not isinstance(detail, dict):
        return make(diagnostic_code="reasoning_details_invalid")
    reasoning = (detail or {}).get("reasoning_tokens")
    if reasoning is not None and (
        type(reasoning) is not int or not 0 <= reasoning <= counts["completion_tokens"]
    ):
        return make(diagnostic_code="reasoning_count_invalid")
    values = dict(
        reported_input=counts["prompt_tokens"],
        reported_output=counts["completion_tokens"],
        reported_total=total,
        count_basis=request.dimension,
        reasoning_basis=reasoning_basis,
        finish_reason=finish,
        outcome="inconclusive",
        diagnostic_code="fixture_not_verified",
    )
    if request.dimension == "input":
        expected = dict(
            re.findall(
                r"\b(start|middle|tail)_sentinel=([a-f0-9]{32})\b",
                str(request.messages[0]["content"]),
            )
        )
        try:
            actual = strict_context_json(content)
        except ValueError:
            actual = None
        if (
            len(expected) == 3
            and actual == expected
            and counts["prompt_tokens"] > 0
            and finish == "stop"
        ):
            values.update(
                evidence_kind="processed_input",
                outcome="verified",
                diagnostic_code="verified_input_fixture",
            )
        elif finish != "stop":
            values["diagnostic_code"] = "input_fixture_not_completed"
        elif not counts["prompt_tokens"]:
            values["diagnostic_code"] = "input_count_not_positive"
        else:
            values["diagnostic_code"] = "input_sentinel_mismatch"
    elif (
        reasoning_basis != "unknown"
        and counts["completion_tokens"] > 0
        and reasoning != counts["completion_tokens"]
        and finish == "length"
    ):
        # Truncation may leave one partial number at the tail. Every complete element must
        # follow the fixture's exact sequence; refusal/early EOS/reasoning-only replies cannot pass.
        parts = content.splitlines()
        if len(parts) < 3:
            values["diagnostic_code"] = "output_fixture_not_reached"
        else:
            complete = parts[:-1]
            tail = parts[-1]
            if (
                all(value == str(index) for index, value in enumerate(complete, 1))
                and tail
                and (str(len(complete) + 1).startswith(tail))
            ):
                values.update(
                    evidence_kind="generated_output",
                    outcome="verified",
                    diagnostic_code="verified_output_fixture",
                )
            else:
                values["diagnostic_code"] = "output_fixture_not_reached"
    return make(**values)


def _context_http_error_class(status):
    if status is None:
        return "unknown"
    if status == 429:
        return "rate_limited"
    if status in {401, 403}:
        return "authentication"
    if status == 400:
        return "invalid_request"
    if status >= 500:
        return "server_error"
    if status >= 400:
        return "client_error"
    return "none"


def context_parser_support(rules, request):
    """Offline shape capability only; this generated envelope is never serving evidence."""
    prompt = min(100, request.reserved_input)
    completion = min(100, request.requested_output)
    content = (
        json.dumps(
            dict(
                re.findall(
                    r"(start|middle|tail)_sentinel=([a-f0-9]{32})",
                    str(request.messages[0]["content"]),
                )
            )
        )
        if request.dimension == "input"
        else "1\n2\n3"
    )
    response = Response(
        200,
        body=json.dumps(
            {
                "object": "chat.completion",
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                },
                "choices": [
                    {
                        "finish_reason": "stop" if request.dimension == "input" else "length",
                        "message": {"content": content},
                    }
                ],
            }
        ),
    )
    observed = rules.context_observation(response, request)
    return (observed.outcome == "verified", "unsupported endpoint/count or reasoning basis")


def strict_context_json(raw):
    """Duplicate fields are conflicting evidence, including inside usage or streamed frames."""

    def unique_fields(pairs):
        fields = {}
        for key, value in pairs:
            if key in fields:
                raise ValueError("duplicate context response field")
            fields[key] = value
        return fields

    return json.loads(raw, object_pairs_hook=unique_fields)


def unsupported_discovery(response, request, transient):
    """No endpoint capability is inferred from OpenAI compatibility or HTTP status."""
    from datetime import UTC, datetime

    from citypods.provider_catalog.discovery import DiscoveryObservation

    return DiscoveryObservation(request, datetime.now(UTC).isoformat())


def _chat_discovery_size_refusal(response, request, payload, *, reasoning_basis):
    """Recognize explicit token coordinates in documented chat/vLLM size errors.

    Exact input counts, fixed output reservation and consistent totals are mandatory.
    Character counts, tokenizer lower bounds and arbitrary error numbers supply no authority.
    https://docs.vllm.ai/en/v0.31.0/api/vllm/renderers/params/
    """
    if response.status not in {400, 422} or response.timed_out or response.transport_error:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if reasoning_basis == "included" and isinstance(detail, list) and set(payload) == {"detail"}:
        # FastAPI/Pydantic numeric field validation is typed, not free-text token guessing.
        # https://docs.pydantic.dev/latest/errors/validation_errors/#less_than_equal
        if len(detail) != 1 or not isinstance(detail[0], dict):
            return None
        issue = detail[0]
        field = issue.get("loc")
        if field not in (["body", "max_tokens"], ["body", "max_completion_tokens"]):
            return None
        kind = issue.get("type")
        context = issue.get("ctx")
        if kind not in ("less_than", "less_than_equal") or not isinstance(context, dict):
            return None
        limit = context.get("lt" if kind == "less_than" else "le")
        count = issue.get("input")
        if (
            type(limit) is not int
            or not 0 < limit <= 2**53 - 1
            or type(count) is not int
            or count != request.output_tokens
            or count < limit
            or (kind == "less_than_equal" and count == limit)
        ):
            return None
        return {
            "coordinate": count,
            "count_basis": (
                "completion_including_reasoning" if request.axis == "output" else "requested_output"
            ),
            "diagnostic": "verified_output_size_refusal",
        }
    if response.status != 400:
        return None
    if detail is not None:
        return None
    error = payload.get("error", payload)
    if not isinstance(error, dict):
        return None
    code, kind = error.get("code"), error.get("type")
    if code not in (None, 400, "context_length_exceeded", "maximum_context_length_exceeded"):
        return None
    if kind not in (None, "invalid_request_error", "BadRequestError", "context_length_exceeded"):
        return None
    untyped = code is None and kind is None and error.get("object") != "error"
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 8192:
        return None
    prefix = r"This (?:model|endpoint)'s maximum context length is (?P<limit>[0-9]+) tokens\. "
    patterns = (
        prefix + r"However, your messages resulted in (?P<input>[0-9]+) tokens\.",
        prefix
        + r"However, you requested (?P<output>[0-9]+) output tokens and your prompt "
        + r"contains (?P<input>[0-9]+) input tokens, for a total of (?P<total>[0-9]+) tokens\.",
        prefix
        + r"However, you requested (?P<total>[0-9]+) tokens "
        + r"\((?P<input>[0-9]+) in the messages, (?P<output>[0-9]+) in the completion\)\.",
        prefix + r"However, your request has (?P<input>[0-9]+) input tokens\.",
    )
    for index, pattern in enumerate(patterns):
        match = re.match(pattern, message)
        if match is None:
            continue
        # Captured NVIDIA responses use a message-only envelope. Permit only this exact
        # input-token wording without an error type/code, not arbitrary untyped failures.
        if untyped:
            envelope = set(payload) == {"message"} or (
                set(payload) == {"error"} and set(error) == {"message"}
            )
            if index != 0 or not envelope:
                return None
        # An appended approximation qualifier invalidates the exact-count claim. Permit
        # only the empty suffix or standard request-shortening advice, not extra prose.
        suffix = message[match.end() :]
        if suffix not in {
            "",
            " Please reduce the length of the messages.",
            " Please reduce the length of your messages.",
            " Please reduce the length of the messages or completion.",
            " Please reduce the length of the input prompt or the number of requested output "
            "tokens.",
        } and not re.fullmatch(
            r" Please reduce (?:the length of )?(?:the |your )?"
            r"(?:messages|input prompt|prompt|input)(?: or (?:the |your )?"
            r"(?:completion|output|requested output tokens))?\.",
            suffix,
        ):
            return None
        if any(len(value) > 16 for value in match.groupdict().values()):
            return None
        counts = {key: int(value) for key, value in match.groupdict().items()}
        if any(not 0 < value <= 2**53 - 1 for value in counts.values()):
            return None
        input_count, limit = counts["input"], counts["limit"]
        if "output" in counts:
            if (
                counts["output"] != request.output_tokens
                or counts["total"] != input_count + counts["output"]
                or counts["total"] <= limit
            ):
                return None
        elif input_count <= limit:
            return None
        if request.axis.startswith("total_") and "output" in counts:
            return {
                "coordinate": counts["total"],
                "reported_input": input_count,
                "count_basis": "prompt_plus_requested_output",
                "diagnostic": "verified_combined_reservation_refusal",
            }
        # A combined overflow with fixed output bounds the conditional input search only.
        # It is not an independent output cap or a proven combined-count contract.
        if request.axis == "input":
            return {
                "coordinate": input_count,
                "reported_input": input_count,
                "count_basis": "input",
                "diagnostic": "verified_input_size_refusal",
            }
        return None
    return None


def _chat_input_capacity(payload, response, *, require_content=True):
    """API input admission; text retrieval accuracy is deliberately a separate signal."""
    if response.status != 200 or response.timed_out or response.transport_error:
        return False
    if not isinstance(payload, dict) or payload.get("object") != "chat.completion":
        return False
    if payload.get("error"):
        return False
    usage, choices = payload.get("usage"), payload.get("choices")
    if not isinstance(usage, dict) or not isinstance(choices, list) or len(choices) != 1:
        return False
    counts = [usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if any(type(count) is not int or not 0 <= count <= 2**53 - 1 for count in counts):
        return False
    if counts[0] <= 0 or counts[1] <= 0 or counts[2] != counts[0] + counts[1]:
        return False
    for key, token_key, limit in (
        ("completion_tokens_details", "reasoning_tokens", counts[1]),
        ("prompt_tokens_details", "cached_tokens", counts[0]),
    ):
        details = usage.get(key)
        if details is None:
            continue
        if not isinstance(details, dict):
            return False
        count = details.get(token_key)
        if count is not None and (type(count) is not int or not 0 <= count <= limit):
            return False
    choice = choices[0]
    if (
        not isinstance(choice, dict)
        or choice.get("error")
        or choice.get("finish_reason") not in ("stop", "length")
    ):
        return False
    message = choice.get("message")
    return (
        isinstance(message, dict)
        and not message.get("refusal")
        and not message.get("tool_calls")
        and (
            isinstance(message.get("content"), str) and bool(message["content"])
            if require_content
            else message.get("content") is None or isinstance(message.get("content"), str)
        )
    )


def _discovery_sentinel_verification(request, transient, payload):
    """Grade completed retrieval independently of the provider's usage-count evidence."""
    if (
        request.axis != "input"
        or not isinstance(payload, dict)
        or payload.get("object") != "chat.completion"
        or payload.get("error")
    ):
        return "not_checked"
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return "not_checked"
    choice = choices[0]
    message = choice.get("message")
    if (
        choice.get("error")
        or choice.get("finish_reason") != "stop"
        or not isinstance(message, dict)
    ):
        return "not_checked"
    content = message.get("content")
    if (
        not isinstance(content, str)
        or not content
        or message.get("refusal")
        or message.get("tool_calls")
    ):
        return "not_checked"
    messages = getattr(transient, "messages", ())
    if not messages or not isinstance(messages[0], Mapping):
        return "not_checked"
    expected = dict(
        re.findall(
            r"\b(start|middle|tail)_sentinel=([a-f0-9]{32})\b", messages[0].get("content", "")
        )
    )
    if len(expected) != 3:
        return "not_checked"
    try:
        actual = strict_context_json(content)
    except ValueError:
        return "failed"
    return "passed" if actual == expected else "failed"


def chat_discovery_observation(
    response, request, transient, *, reasoning_basis="unknown", combined=False
):
    """Opt-in actual chat counts; undocumented size-error numbers supply no upper boundary.

    Combined reservation semantics are not implied by usage.total_tokens. Until an adapter proves
    its two request mixes and typed refusal coordinate, combined axes remain unsupported.
    """
    import math
    from datetime import UTC, datetime

    from citypods.provider_catalog.discovery import DiscoveryObservation

    stamp = datetime.now(UTC).isoformat()
    if request.axis.startswith("total_") and not combined:
        return DiscoveryObservation(request, stamp, diagnostic="combined_semantics_unverified")
    # Reuse capacity parsing without projecting discovery transport timings into the
    # configured-context evidence schema, whose deadline remains 120 seconds.
    capacity_response = replace(
        response,
        context_diagnostics={
            key: value
            for key, value in response.context_diagnostics.items()
            if key not in {"duration_ms", "first_byte_ms"}
        },
    )
    observation = chat_context_observation(
        capacity_response, transient, reasoning_basis=reasoning_basis
    )
    stream_failure = response.context_diagnostics.get("diagnostic_code", "")
    if (
        not response.body
        and isinstance(stream_failure, str)
        and (
            stream_failure.startswith(("invalid_sse_", "stream_", "conflicting_sse_"))
            or stream_failure in {"provider_sse_error", "incomplete_sse", "invalid_final_sse_usage"}
        )
    ):
        observation = replace(observation, diagnostic_code=stream_failure)
    try:
        payload = strict_context_json(response.body)
        payload = payload if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        payload = None
    usage = payload.get("usage") if payload is not None else None
    choices = payload.get("choices") if payload is not None else None
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else None
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    content_state = (
        "not_applicable"
        if payload is None or not isinstance(choice, dict) or not isinstance(message, dict)
        else "missing"
        if "content" not in message
        else "null"
        if content is None
        else "other"
        if not isinstance(content, str)
        else "empty"
        if not content
        else "text"
    )
    finish_state = (
        "not_applicable"
        if payload is None or not isinstance(choice, dict)
        else "missing"
        if "finish_reason" not in choice
        else "null"
        if finish is None
        else finish
        if finish in ("stop", "length")
        else "other"
    )
    usage_state = "missing" if payload is not None and "usage" not in payload else "unknown"
    if isinstance(usage, dict):
        if "prompt_tokens" not in usage:
            usage_state = "prompt_missing"
        elif (
            type(usage.get("prompt_tokens")) is not int
            or not 0 <= usage["prompt_tokens"] <= 2**53 - 1
        ):
            usage_state = "prompt_invalid"
        elif "completion_tokens" not in usage:
            usage_state = "completion_missing"
        elif (
            type(usage.get("completion_tokens")) is not int
            or not 0 <= usage["completion_tokens"] <= 2**53 - 1
        ):
            usage_state = "completion_invalid"
        elif "total_tokens" not in usage:
            usage_state = "total_missing"
        elif (
            type(usage["total_tokens"]) is not int
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
        ):
            usage_state = "total_invalid"
        else:
            usage_state = "valid"
    elif usage is not None:
        usage_state = "not_object"
    response_id = payload.get("id") if payload is not None else None
    if isinstance(response_id, str) and response_id.isascii():
        response_id = re.sub(r"[^A-Za-z0-9._:-]", "", response_id[:128]) or None
    else:
        response_id = None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    reasoning = details.get("reasoning_tokens") if isinstance(details, dict) else None
    cached = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
    if type(reasoning) is not int or not 0 <= reasoning <= (observation.reported_output or 0):
        reasoning = None
    if type(cached) is not int or not 0 <= cached <= (observation.reported_input or 0):
        cached = None
    values = dict(
        reasoning_tokens=reasoning,
        cached_tokens=cached,
        reported_input=observation.reported_input,
        reported_output=observation.reported_output,
        finish_reason=(
            finish
            if isinstance(finish, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", finish)
            else ""
        ),
        diagnostic=observation.diagnostic_code,
        response_status=response.status,
        response_media_type=diagnostics_value(response.context_diagnostics, "response_media_type"),
        response_bytes=diagnostics_value(response.context_diagnostics, "response_bytes"),
        message_content_state=content_state,
        finish_reason_state=finish_state,
        usage_state=usage_state,
        provider_request_id=_safe_discovery_id(
            diagnostics_value(response.context_diagnostics, "provider_request_id")
        ),
        response_id=response_id,
        stream_bytes=diagnostics_value(response.context_diagnostics, "stream_bytes"),
        stream_events=diagnostics_value(response.context_diagnostics, "stream_events"),
        sentinel_verification=_discovery_sentinel_verification(request, transient, payload),
    )
    if observation.outcome != "verified":
        values["response_preview"] = (
            diagnostics_value(response.context_diagnostics, "response_preview") or ""
        )
    diagnostics = response.context_diagnostics
    retry = diagnostics.get("retry_after_ms")
    if type(retry) is int and retry > 0:
        values["retry_after_seconds"] = math.ceil(retry / 1000)
    error = payload.get("error") if isinstance(payload, dict) else None
    metadata = error.get("metadata") if isinstance(error, dict) else None
    if (
        response.status == 400
        and isinstance(error, dict)
        and error.get("type") == "invalid_request_error"
        and isinstance(metadata, dict)
        and metadata.get("reason") == "err_free_prompt_cap"
        and isinstance(error.get("message"), str)
        and error["message"].startswith(
            "This prompt is longer than the free tier allows for a single request."
        )
    ):
        # Explicit input-size semantics take precedence over a broad rate-limited code.
        values["diagnostic"] = "input_size_refused_without_exact_counts"
        return DiscoveryObservation(request, stamp, **values)
    if observation.outcome == "quota":
        text = error.get("message") if isinstance(error, dict) else None
        if isinstance(text, str) and re.search(
            r"\b(?:tokens|requests) per (?:day|month)\s*(?:\([^)]{1,8}\))?:\s*Limit",
            text[:8192],
            re.IGNORECASE,
        ):
            values["diagnostic"] = "daily_or_monthly_quota_deferred"
            return DiscoveryObservation(request, stamp, outcome="daily", **values)
        return DiscoveryObservation(request, stamp, outcome="quota", **values)
    if response.status is not None and response.status >= 500:
        return DiscoveryObservation(request, stamp, outcome="outage", **values)
    refusal = _chat_discovery_size_refusal(
        response, request, payload, reasoning_basis=reasoning_basis
    )
    if refusal is not None:
        return DiscoveryObservation(
            request, stamp, outcome="refused", verified=True, **(values | refusal)
        )
    if response.status == 400 and isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            text = error["message"]
            proxy_size = re.match(
                r"This (?:model|endpoint)'s maximum context length is [0-9]+ tokens\. "
                r"However, you requested about [0-9]+ tokens ",
                text,
            )
            qualitative_size = (
                error.get("type") == "invalid_request_error"
                and error.get("param") in {"messages", "max_tokens", "max_completion_tokens"}
                and text == "Please reduce the length of the messages or completion."
            )
            negative_budget = re.fullmatch(
                r"(?P<field>max_tokens|max_completion_tokens) must be at least 1, "
                r"got (?P<value>-[1-9][0-9]*)\. \(parameter=(?P=field), value=(?P=value)\)",
                text,
            )
            if proxy_size or qualitative_size or negative_budget:
                # Approximate proxy counts and count-free refusals can guide a smaller request,
                # but cannot manufacture an exact provider-counted boundary.
                values["diagnostic"] = (
                    "computed_input_budget_exhausted"
                    if negative_budget
                    else "size_refused_without_exact_counts"
                )
                return DiscoveryObservation(request, stamp, **values)
    if request.axis.startswith("total_"):
        if (
            response.status == 200
            and not response.timed_out
            and not response.transport_error
            and usage_state == "valid"
            and finish == "length"
            and usage["completion_tokens"] < request.output_tokens
        ):
            values["diagnostic"] = "combined_output_clipped:actual_total=" + str(
                usage["total_tokens"]
            )
            return DiscoveryObservation(request, stamp, **values)
        if _chat_input_capacity(payload, response, require_content=False):
            actual = usage["prompt_tokens"] + usage["completion_tokens"]
            if finish == "length" and usage["completion_tokens"] < request.output_tokens:
                values["diagnostic"] = f"combined_output_clipped:actual_total={actual}"
                return DiscoveryObservation(request, stamp, **values)
            # A short answer proves consumed context, not reserved output capacity.
            if finish == "length" and usage["completion_tokens"] >= request.output_tokens:
                return DiscoveryObservation(
                    request,
                    stamp,
                    outcome="admitted",
                    verified=True,
                    coordinate=usage["prompt_tokens"] + request.output_tokens,
                    count_basis="prompt_plus_requested_output",
                    **(
                        values
                        | {"diagnostic": "verified_combined_output_filled", "response_preview": ""}
                    ),
                )
            values["diagnostic"] = f"combined_output_stopped_early:actual_total={actual}"
        return DiscoveryObservation(request, stamp, **values)
    if request.axis == "input":
        if _chat_input_capacity(payload, response):
            return DiscoveryObservation(
                request,
                stamp,
                outcome="admitted",
                coordinate=usage["prompt_tokens"],
                count_basis="input",
                verified=True,
                **(values | {"diagnostic": "api_input_admitted"}),
            )
        if observation.outcome == "verified" or usage_state in {"total_missing", "total_invalid"}:
            values["diagnostic"] = "api_input_usage_incomplete"
        return DiscoveryObservation(request, stamp, **values)
    if (
        request.axis == "output"
        and _chat_input_capacity(payload, response, require_content=False)
        and finish == "stop"
    ):
        values["diagnostic"] = "output_stopped_early"
    if (
        request.axis == "output"
        and request.control.split(":opposite=", 1)[0] in {"output:v4", "output:v4:min_tokens"}
        and reasoning_basis == "included"
        and _chat_input_capacity(payload, response, require_content=False)
        and finish == "length"
    ):
        return DiscoveryObservation(
            request,
            stamp,
            outcome="admitted",
            coordinate=usage["completion_tokens"],
            count_basis="completion_including_reasoning",
            verified=True,
            **(values | {"diagnostic": "verified_generated_output", "response_preview": ""}),
        )
    if request.axis == "output" and request.control.startswith("output:v4"):
        return DiscoveryObservation(request, stamp, **values)
    if observation.outcome == "verified":
        count = (
            observation.reported_input if request.axis == "input" else observation.reported_output
        )
        return DiscoveryObservation(
            request,
            stamp,
            outcome="admitted",
            coordinate=count,
            count_basis=(
                "completion_including_reasoning"
                if request.axis == "output" and reasoning_basis == "included"
                else request.axis
            ),
            verified=True,
            **values,
        )
    return DiscoveryObservation(request, stamp, **values)


def diagnostics_value(diagnostics, name):
    """Read a collector diagnostic only when its type and bounds are expected."""
    value = diagnostics.get(name)
    if name == "response_media_type":
        allowed = {"unknown", "application/json", "text/event-stream", "other"}
        return value if value in allowed else "unknown"
    if name in {"stream_bytes", "stream_events"}:
        bound = 64 * 1024 * 1024 if name == "stream_bytes" else 1_000_000
        return value if type(value) is int and 0 <= value <= bound else None
    if name == "response_bytes":
        return value if type(value) is int and 0 <= value <= 4 * 1024 * 1024 else None
    if name == "provider_request_id":
        return value if isinstance(value, str) and len(value) <= 128 else None
    if name == "response_preview":
        return value if isinstance(value, str) and len(value) <= 1_024 else ""
    return None


def _safe_discovery_id(value):
    if not isinstance(value, str) or not value:
        return None
    safe = re.sub(r"[^A-Za-z0-9._:-]", "", value[:128])
    return safe or None


def general_discovery_observation(response, request, transient):
    """Interpret common chat evidence for an unseen route, without model-specific opt-in.

    The recognized chat protocol defines completion_tokens as the completion allowance,
    with reasoning details a breakdown where supplied. Only explicit field arithmetic and
    refusal semantics supply proof; this is not a provider-name or model-name inference.
    Unknown/native formats
    remain diagnostic rather than inheriting a guessed capacity from their provider name.
    """
    # Some compatible gateways omit the optional discriminator but retain the complete
    # choices/usage schema. Do not normalize envelopes with an explicit different type.
    try:
        payload = strict_context_json(response.body)
    except (TypeError, ValueError):
        payload = None
    if (
        isinstance(payload, dict)
        and "object" not in payload
        and not payload.get("error")
        and isinstance(payload.get("usage"), dict)
        and isinstance(payload.get("choices"), list)
    ):
        response = replace(response, body=json.dumps(payload | {"object": "chat.completion"}))
    return chat_discovery_observation(
        response, request, transient, reasoning_basis="included", combined=True
    )


def general_discovery_rules(rules):
    """Keep transport/auth capabilities; remove the need for a pre-registered size parser."""
    from citypods.provider_catalog.discovery import AXES

    if rules.discovery_parser_version.startswith("shared-chat-v1:"):
        return rules

    def interpret(response, request, transient):
        shared = general_discovery_observation(response, request, transient)
        if shared.verified or shared.outcome != "inconclusive":
            return shared
        extension = rules.discovery_observation(response, request, transient)
        return extension if extension.verified or extension.outcome != "inconclusive" else shared

    return replace(
        rules,
        discovery_axes=AXES,
        discovery_observation=interpret,
        discovery_parser_version="shared-chat-v1:" + rules.discovery_parser_version,
    )
