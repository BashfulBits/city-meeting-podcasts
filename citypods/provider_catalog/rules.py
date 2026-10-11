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
from dataclasses import dataclass, field
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
    """What a canary observed. `body` is used only for classification and is never reported."""

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


def chat_context_observation(response, request, *, reasoning_basis="unknown"):
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
            "rate_limit_remaining_tokens",
            "rate_limit_reset_tokens_ms",
            "rate_limit_limit_requests",
            "rate_limit_remaining_requests",
            "rate_limit_reset_requests_ms",
            "retry_after_ms",
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
        diagnostics.setdefault("rate_limit_remaining_tokens", None)
        diagnostics.setdefault("rate_limit_reset_tokens_ms", None)
        diagnostics.setdefault("rate_limit_limit_requests", None)
        diagnostics.setdefault("rate_limit_remaining_requests", None)
        diagnostics.setdefault("rate_limit_reset_requests_ms", None)
        diagnostics.setdefault("retry_after_ms", None)
        diagnostics["diagnostic_code"] = (
            diagnostic_code or diagnostics.get("diagnostic_code") or "unclassified_response"
        )
        return ContextObservation.from_request(request, **(diagnostics | values))

    if response.timed_out or response.transport_error:
        return make(
            diagnostic_code=response.context_diagnostics.get("diagnostic_code", "transport_error"),
            outcome="transport",
        )
    if response.status == 429:
        return make(diagnostic_code="http_429_rate_limited", outcome="quota")
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
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("error"):
        return make(diagnostic_code="completion_shape_invalid")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("refusal") or message.get("tool_calls"):
        return make(diagnostic_code="refusal_or_tool_call", outcome="inconclusive")
    content, finish = message.get("content"), choice.get("finish_reason")
    if not isinstance(content, str) or finish not in {"stop", "length"}:
        return make(diagnostic_code="completion_content_or_finish_invalid", outcome="inconclusive")
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
