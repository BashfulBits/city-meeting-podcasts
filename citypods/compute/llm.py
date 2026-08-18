"""LiteLLM routing plus Instructor/Pydantic structured outputs for reserved LLM verbs.

Direct calls use Instructor for provider-mode selection, parsing, Pydantic validation, and one
bounded corrective retry.  The R10 Worker remains an asynchronous transport: it durably stores the
Pydantic-generated response format, and reconciliation validates the completed reply locally.  A
future queue-owned corrective retry can be added without changing a task's response contract.

Every policy-bearing call that can't complete synchronously -- nothing eligible right now, a real
429 from the provider, or a genuinely in-flight dispatch to the R10 Worker -- returns the same
``JobHandle`` shape and is completed the same way: hold it (or don't -- see ``llm_deferred.py``)
and call ``reconcile()``/``run_inference()`` again later. The caller never needs to know which
transport or reason produced it.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlsplit

import requests

from citypods.compute.base import Backend, InferenceJob, JobHandle, JobResult, Task
from citypods.compute.llm_budget import (
    block_route_until,
    load_llm_budget_cas,
    release_route_reservation,
    settle_route_reservation,
)
from citypods.compute.llm_deferred import (
    look_up_deferred,
    terminal_failure_retry_allowed,
    write_deferred,
)
from citypods.compute.llm_policy import (
    DEFAULT_OUTPUT_TOKEN_MARGIN,
    ROUTE_CANDIDATES,
    ROUTE_REGISTRY,
    DeferredLLMRequest,
    LLMRequestPolicy,
    LLMRoute,
    PricingPolicy,
    QuotaPolicy,
    canonical_model,
    estimate_tokens,
)
from citypods.compute.llm_scheduler import SelectionResult, select_and_reserve, select_route
from citypods.compute.structured import ResponseModel, response_model
from citypods.security import SecurityError, validate_source_url
from citypods.storage.s3 import b2_from_env

LLM_TASKS: frozenset[Task] = frozenset(
    {
        "summarize",
        "tag",
        "soundbite-select",
        "classify-civic-platforms",
        "agenda-item-extract",
        "agenda-chapter-locate",
    }
)
TASK_VERSIONS: dict[Task, str] = {
    "summarize": "1",
    "tag": "1",
    "soundbite-select": "1",
    "classify-civic-platforms": "6",
    "agenda-item-extract": "1",
    "agenda-chapter-locate": "1",
}

TASK_PROMPTS: dict[Task, str] = {
    "summarize": "Summarize the supplied meeting material accurately and concisely.",
    "tag": "Extract a small list of factual topic tags from the supplied meeting material.",
    "soundbite-select": (
        "Select the strongest bounded soundbite candidates from the supplied material."
    ),
    "classify-civic-platforms": (
        "Classify civic meeting platforms only from the supplied retrieved evidence. "
        "Never invent a URL or a platform not supported by that evidence, and reject evidence for "
        "a different municipality."
    ),
    "agenda-item-extract": (
        "Extract source-grounded agenda action items with exact line evidence and concise titles."
    ),
    "agenda-chapter-locate": (
        "Locate agenda items in a complete timed transcript using only supplied unit IDs."
    ),
}

SUPPORTED_MODELS = frozenset(ROUTE_CANDIDATES)

# Fallback backoff when a 429 carries no parseable Retry-After hint.
_DEFAULT_BLOCK_SECONDS = 60.0
_SAFE_DIAGNOSTICS_ENV = "LLM_SAFE_DIAGNOSTICS"

# Longest single sleep the pacing loop takes between capacity re-checks. This bounds responsiveness
# to continuous RPM/TPM schedules (and daily resets); the loop keeps sleeping until a route frees or
# the deadline passes.
_PACING_POLL_CAP_SECONDS = 10.0
# Tiny floor so an "eligible now, but the reserve just lost a race" retry can't hot-spin the CPU.
_PACING_MIN_SLEEP_SECONDS = 0.2

# --- Cloudflare AI Gateway (direct transport only) ---------------------------------------------
# The gateway is an observability shim, not a routing decision: it proxies to the same upstream
# and returns the same body, so enabling it must never change an LLM reply. It is therefore on by
# default and disabled with `LLM_AI_GATEWAY=0` (kill switch for a gateway outage).
#
# It applies to the *direct* transport only. The `llm-dispatch` Worker already fronts its own
# provider calls with the gateway on its side, and the payload we send it is a provider-neutral
# job description rather than a LiteLLM call -- injecting an `api_base`/`cf-aig-authorization`
# there would either double-proxy the request or hand the Worker an endpoint it does not own.
_AI_GATEWAY_ENV = "LLM_AI_GATEWAY"
_AI_GATEWAY_DISABLED_VALUES = frozenset({"0", "false", "off", "no"})
_DEFAULT_AI_GATEWAY_ID = "citypods-dispatch"
# LiteLLM appends this to `api_base` itself, so a route's gateway path contributes only the part
# *before* it (Gemini's `/v1beta/openai`, Mistral's `/v1`, nothing for a plain OpenAI shape).
_AI_GATEWAY_CHAT_SUFFIX = "/chat/completions"


def _ai_gateway_enabled() -> bool:
    return os.environ.get(_AI_GATEWAY_ENV, "").strip().lower() not in _AI_GATEWAY_DISABLED_VALUES


def _ai_gateway_base() -> str:
    """The gateway root, or ``""`` when this runner has no gateway configured.

    ``AI_GATEWAY_BASE_URL`` overrides outright (a self-hosted proxy, or a test double); otherwise
    the standard Cloudflare URL is derived from the account and gateway id.
    """
    explicit = os.environ.get("AI_GATEWAY_BASE_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not account_id:
        return ""
    gateway_id = os.environ.get("AI_GATEWAY_ID", "").strip() or _DEFAULT_AI_GATEWAY_ID
    return f"https://gateway.ai.cloudflare.com/v1/{account_id}/{gateway_id}"


def _ai_gateway_path_prefix(route: LLMRoute) -> str:
    """The provider path segment the gateway needs but LiteLLM will not supply.

    A route's ``ai_gateway_chat_path`` is the whole path after the provider slug (Gemini's
    ``/v1beta/openai/chat/completions``). LiteLLM appends ``/chat/completions`` on its own, so
    only the prefix belongs in ``api_base``; dropping it is what would send Gemini and Mistral
    gateway calls to a 404. A path that does not end in the usual suffix is used verbatim.
    """
    chat_path = (route.ai_gateway_chat_path or "").strip()
    if chat_path.endswith(_AI_GATEWAY_CHAT_SUFFIX):
        return chat_path[: -len(_AI_GATEWAY_CHAT_SUFFIX)]
    return chat_path


def _pacing_wait_seconds(
    retry_at: datetime | None, deadline_at: datetime | None, now: datetime
) -> float | None:
    """How long to sleep before re-checking dispatch capacity, or ``None`` to give up pacing.

    ``None`` when nothing will free up (``retry_at is None``) or when the soonest a route frees up
    is at/after the run's ``deadline_at`` (a daily-quota reset hours away, vs a ~15-minute job
    budget) -- in that case the caller returns the deferred handle for the sweep or a future run.
    A single wait is capped at ``_PACING_POLL_CAP_SECONDS`` so the loop re-reads the freshest
    ledger and re-checks the deadline regularly rather than blindly sleeping a whole minute."""
    if retry_at is None:
        return None
    if deadline_at is not None and retry_at >= deadline_at:
        return None
    wait = (retry_at - now).total_seconds()
    if wait <= 0:
        return 0.0
    return min(wait, _PACING_POLL_CAP_SECONDS)


class LLMBackendError(RuntimeError):
    """A safe, provider-agnostic adapter error (provider response bodies are not exposed)."""


class LLMStructuredOutputError(LLMBackendError):
    """A malformed model reply that a caller may safely defer and retry with fresh evidence."""


class LLMDispatchTerminalError(LLMBackendError):
    """The dispatch Worker recorded a terminal failure for this one request."""


@dataclass(frozen=True)
class _FailedAttempt:
    """One failed structured-output attempt, in Instructor's own ``failed_attempts`` shape --
    used to describe a native-schema retry exhaustion (see
    ``_NativeStructuredRetryExhausted``) through the same diagnostic function Instructor's own
    ``InstructorRetryException`` already uses, without special-casing which path raised it."""

    exception: BaseException


class _NativeStructuredRetryExhausted(RuntimeError):
    """A native-schema reply failed local Pydantic validation on both the original attempt
    and the one corrective retry -- the direct-call twin of Instructor's
    ``InstructorRetryException`` (see ``_run_native_structured_direct``). Carries the same
    ``failed_attempts``/``n_attempts`` shape so ``_safe_structured_failure_diagnostic`` needs no
    branch for which path failed."""

    def __init__(self, failed_attempts: list[_FailedAttempt]):
        super().__init__("Native structured response failed schema validation after 1 retry")
        self.failed_attempts = tuple(failed_attempts)
        self.n_attempts = len(self.failed_attempts)


def _safe_structured_failure_diagnostic(
    exc: BaseException, job: InferenceJob, model: ResponseModel, resolved_model: str
) -> dict[str, Any]:
    """Return non-sensitive metadata for an opt-in structured-output failure log.

    Never include messages, headers, credentials, response bodies, or provider exception text:
    any of those can contain meeting material. The schema summary is intentionally boolean/count
    only, enough to distinguish a provider-schema rejection from an ordinary malformed reply.
    Shared by both structured-output paths (Instructor's ``InstructorRetryException`` for non-
    native-profile routes, ``_NativeStructuredRetryExhausted`` for the direct native-schema path) --
    both expose the same ``failed_attempts``/``n_attempts`` shape.
    """
    schema = model.model_json_schema()
    raw = json.dumps(schema, sort_keys=True)
    attempts = getattr(exc, "failed_attempts", ()) or ()
    last = attempts[-1].exception if attempts else exc
    return {
        "event": "llm_structured_output_failure",
        "model": resolved_model,
        "task": job.task,
        "attempts": int(getattr(exc, "n_attempts", 0) or 0),
        "provider_exception_type": type(last).__name__,
        "provider_status": getattr(last, "status_code", None),
        "input_characters": sum(len(str(message.get("content", ""))) for message in _messages(job)),
        "schema_characters": len(raw),
        "schema_has_defs": '"$defs"' in raw,
        "schema_has_refs": '"$ref"' in raw,
        "schema_has_any_of": '"anyOf"' in raw,
        "schema_has_defaults": '"default"' in raw,
    }


def _strip_schema_keys(node: Any, keys: frozenset[str]) -> Any:
    """Deep copy of a JSON Schema node with every occurrence of the given object keys removed."""
    if isinstance(node, dict):
        return {
            key: _strip_schema_keys(value, keys) for key, value in node.items() if key not in keys
        }
    if isinstance(node, list):
        return [_strip_schema_keys(item, keys) for item in node]
    return node


def _schema_variant_model(model: ResponseModel, strip_keys: frozenset[str]) -> ResponseModel:
    """Return a same-named request-schema variant without changing response validation.

    Instructor/LiteLLM derive the request schema by calling the response model's
    ``model_json_schema`` classmethod, with no supported hook to hand them an already-built schema.
    A same-named subclass is therefore the narrowest way to apply a configured profile while
    retaining the original model's Pydantic validation for the provider response.

    Response *validation* is unaffected: `model`'s fields and their min_length/max_length/ge/le
    constraints are inherited unchanged, so a reply that violates them still fails local Pydantic
    validation and still triggers the configured corrective retry. Built fresh per call rather than
    cached: this is one cheap class-creation call per structured request, not a hot path.
    """
    if not strip_keys:
        return model
    base_schema = model.model_json_schema.__func__

    def _relaxed_schema(cls: type, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = base_schema(cls, *args, **kwargs)
        return _strip_schema_keys(schema, strip_keys)

    return type(
        model.__name__,
        (model,),
        {"model_json_schema": classmethod(_relaxed_schema), "__module__": model.__module__},
    )


@dataclass(frozen=True)
class LLMBackendConfig:
    """Runtime routing configuration; secrets are read from the environment, never persisted."""

    model: str = "gemini/gemini-3-flash-preview"
    mode: Literal["direct", "dispatch"] = "direct"
    dispatch_url: str | None = None
    dispatch_auth_token: str | None = None
    dispatch_v2_url: str | None = None
    dispatch_v2_auth_token: str | None = None
    daily_ingest_cap: int | None = None
    timeout_seconds: float = 30.0
    # Extra routes a policy-bearing call may spill onto once ``model``'s own per-minute/daily quota
    # window fills -- e.g. tagging pins ``gemini-3.1-flash-lite`` as the recipe/calibration route
    # but lets the scheduler also draw on ``gemini-3.5-flash-lite``'s independent free-tier pool for
    # throughput. Empty (default) keeps the historical single-route behavior. ``model`` stays the
    # stable route string for recipe hashing/calibration; each candidate still records the model
    # that actually answered.
    additional_models: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> LLMBackendConfig:
        """Build configuration from environment variables without reading provider keys.

        ``LLM_MODE`` is read as a plain string and passed through unvalidated here — an invalid
        value (anything but ``"direct"``/``"dispatch"``) surfaces as a clear ``ValueError`` from
        ``LiteLLMBackend.__init__``'s existing runtime check, which is the right place to reject
        it. The ``Literal`` annotation on ``mode`` documents the two valid values; it does not
        (and must not) silently coerce an invalid environment value into one of them.
        """
        daily_cap_str = os.environ.get("CITYPODS_LLM_DAILY_INGEST_CAP") or os.environ.get(
            "LLM_DAILY_INGEST_CAP"
        )
        daily_cap = int(daily_cap_str) if daily_cap_str else None
        return cls(
            model=os.environ.get("LLM_MODEL") or cls.model,
            mode=cast("Literal['direct', 'dispatch']", os.environ.get("LLM_MODE") or cls.mode),
            dispatch_url=os.environ.get("LLM_DISPATCH_URL"),
            dispatch_auth_token=os.environ.get("LLM_DISPATCH_AUTH_TOKEN"),
            dispatch_v2_url=os.environ.get("CITYPODS_LLM_DISPATCH_V2_URL")
            or os.environ.get("LLM_DISPATCH_V2_URL"),
            dispatch_v2_auth_token=os.environ.get("CITYPODS_LLM_DISPATCH_V2_AUTH_TOKEN")
            or os.environ.get("LLM_DISPATCH_V2_AUTH_TOKEN")
            or os.environ.get("LLM_DISPATCH_AUTH_TOKEN"),
            daily_ingest_cap=daily_cap,
            timeout_seconds=float(os.environ.get("LLM_TIMEOUT_SECONDS", cls.timeout_seconds)),
        )


def _response_mapping(response: Any) -> dict[str, Any]:
    """Convert LiteLLM/OpenAI response objects to a JSON-safe mapping."""
    if isinstance(response, Mapping):
        return dict(response)
    for method in ("model_dump", "to_dict", "dict"):
        converter = getattr(response, method, None)
        if callable(converter):
            value = converter()
            if isinstance(value, Mapping):
                return dict(value)
    raise LLMBackendError("LLM provider returned an unsupported response shape")


def _usage_tokens(output: Mapping[str, Any]) -> int | None:
    """Actual token usage from a response, or ``None`` when it can't be trusted.

    Missing/malformed usage data must settle as "unknown," never as zero -- ``settle()`` treats
    ``actual_tokens=None`` as "keep the reservation's estimate charged." A response with
    ``usage={}`` estimating to 0 would instead erase the whole reservation despite having no real
    usage figure, and a malformed negative total would corrupt an unrelated window's count.
    """
    usage = output.get("usage")
    if not isinstance(usage, Mapping):
        return None
    total = usage.get("total_tokens")
    try:
        if total is None:
            if "prompt_tokens" not in usage and "completion_tokens" not in usage:
                return None
            total = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
        value = int(total)
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def _sum_usage_fields(first: Any, second: Any) -> dict[str, Any] | None:
    """Sum two response ``usage`` mappings field-by-field (``total_tokens``/``prompt_tokens``/
    ``completion_tokens``) -- for combining a failed first structured-output attempt's real
    provider usage with a successful retry's, so settlement prices the true combined cost
    instead of only the final attempt's (see ``_run_native_structured_direct``). Returns
    ``second`` unchanged if either side isn't a usable mapping, and bails out (returns ``second``
    unmerged) on the first field that doesn't parse as a number rather than guess -- an
    under-priced retry is a smaller, already-accepted imprecision (``_usage_tokens`` above treats
    missing usage as "keep the estimate"); a wrongly-combined total would be a new one."""
    if not isinstance(second, Mapping):
        return None
    if not isinstance(first, Mapping):
        return dict(second)
    merged: dict[str, Any] = dict(second)
    for key in ("total_tokens", "prompt_tokens", "completion_tokens"):
        a, b = first.get(key), second.get(key)
        if a is None and b is None:
            continue
        try:
            merged[key] = int(a or 0) + int(b or 0)
        except (TypeError, ValueError):
            return dict(second)
    return merged


def _priced_actual(
    output: Mapping[str, Any],
    *,
    input_per_token: float,
    output_per_token: float,
) -> tuple[int | None, float | None]:
    """Actual ``(tokens, cost)`` for a completed response, pricing prompt and completion tokens
    at their own rates rather than charging the combined rate to every token (which over- or
    under-charges whenever input and output prices differ, as they do for every paid route here).
    Falls back to the combined-rate approximation only when the response doesn't break the split
    out (some total but no prompt/completion pair)."""
    actual_tokens = _usage_tokens(output)
    if actual_tokens is None:
        return None, None
    usage = output.get("usage")
    prompt = usage.get("prompt_tokens") if isinstance(usage, Mapping) else None
    completion = usage.get("completion_tokens") if isinstance(usage, Mapping) else None
    try:
        if prompt is not None and completion is not None:
            prompt_n, completion_n = int(prompt), int(completion)
            if prompt_n >= 0 and completion_n >= 0:
                cost = prompt_n * input_per_token + completion_n * output_per_token
                return actual_tokens, cost
    except (TypeError, ValueError):
        pass
    return actual_tokens, actual_tokens * (input_per_token + output_per_token)


def _format_rejected(rejected: tuple[tuple[str, str], ...]) -> str:
    """Render a ``SelectionResult.rejected`` list as a short, log-friendly summary."""
    return ", ".join(f"{model}: {reason}" for model, reason in rejected) or "no allowed route"


def _is_rate_limited(exc: BaseException) -> bool:
    """Duck-typed 429 detection: LiteLLM wraps provider errors in OpenAI-shaped exception classes
    that expose ``status_code``, but the exact class varies by provider/version, so this checks
    the attribute rather than importing a specific type."""
    return getattr(exc, "status_code", None) == 429


def _retry_after_seconds(source: Any) -> float | None:
    """Best-effort ``Retry-After`` extraction from a raised exception (checked via its wrapped
    ``response`` attribute) or a real ``requests.Response`` (checked directly). Returns ``None``
    on anything unparseable -- or non-finite, or non-positive, since ``nan``/``inf`` would raise
    inside the caller's ``timedelta(seconds=...)`` and a negative value would immediately unblock
    the route -- so the caller falls back to a fixed default rather than guessing."""
    headers = getattr(source, "headers", None)
    if headers is None:
        response = getattr(source, "response", None)
        headers = getattr(response, "headers", None)
    if not headers:
        return None
    value = headers.get("retry-after") if hasattr(headers, "get") else None
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _messages(job: InferenceJob) -> list[dict[str, Any]]:
    """Return caller-supplied messages or construct the task's default structured prompt."""
    supplied = job.inputs.get("messages")
    if supplied is not None:
        if not isinstance(supplied, list) or not supplied:
            raise ValueError("LLM inputs.messages must be a non-empty list")
        return [dict(message) for message in supplied]
    content = job.inputs.get("content", job.inputs.get("text", ""))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM jobs require inputs.messages or non-empty inputs.content/text")
    return [
        {"role": "system", "content": TASK_PROMPTS[job.task]},
        {"role": "user", "content": content},
    ]


def _messages_with_schema(
    messages: list[dict[str, Any]], model: ResponseModel
) -> list[dict[str, Any]]:
    """Add the response contract to a JSON-mode provider's prompt.

    DeepSeek's ``json_object`` mode guarantees valid JSON syntax, not conformance to a schema.
    Keeping the schema in the initial prompt makes that first attempt obey the same contract that
    local Pydantic validation enforces, including when callers supplied a custom prompt.
    """
    schema = json.dumps(model.model_json_schema(), sort_keys=True)
    instruction = (
        "Return one JSON object only (no Markdown or commentary) matching this JSON Schema:\n"
        f"{schema}"
    )
    enriched = [dict(message) for message in messages]
    for message in enriched:
        if message.get("role") == "system":
            content = message.get("content", "")
            message["content"] = f"{content}\n\n{instruction}"
            return enriched
    return [{"role": "system", "content": instruction}, *enriched]


class LiteLLMBackend(Backend):
    """Run an :class:`InferenceJob` through LiteLLM or the R10 async Worker."""

    name = "litellm"

    def __init__(
        self,
        config: LLMBackendConfig | None = None,
        *,
        completion: Callable[..., Any] | None = None,
        http_session: requests.Session | None = None,
        storage=None,
    ) -> None:
        self.config = config or LLMBackendConfig.from_env()
        if canonical_model(self.config.model) not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported LLM model route: {self.config.model!r}")
        unsupported_extra = [
            m for m in self.config.additional_models if canonical_model(m) not in SUPPORTED_MODELS
        ]
        if unsupported_extra:
            raise ValueError(f"unsupported additional LLM model route(s): {unsupported_extra!r}")
        if self.config.mode not in {"direct", "dispatch"}:
            raise ValueError("LLM mode must be 'direct' or 'dispatch'")
        if self.config.mode == "dispatch" and not self.config.dispatch_url:
            raise ValueError("dispatch mode requires LLM_DISPATCH_URL")
        self._completion = completion
        self._session = http_session or requests.Session()
        self.storage = storage
        self._daily_ingest_admitted: int = 0
        self._daily_ingest_exhausted: bool = False

    def _available_transports(self) -> frozenset[str]:
        """Which transports a policy-bearing call from *this instance* can reach right now.

        Independent of ``self.config.mode``, which only governs the legacy static-model path
        (``_run_without_policy``): ``direct`` needs nothing beyond a provider API key (already in
        env), so it's always reachable; ``mistral-dispatch`` / ``llm-dispatch`` needs a
        configured dispatch Worker.
        A backend built with both configured (as the deferred-request sweep's is) can select
        freely across every route regardless of which transport backs it.
        """
        if self.config.mode == "dispatch":
            transports = {"mistral-dispatch", "llm-dispatch"}
            if self.config.dispatch_v2_url:
                transports.add("llm-dispatch-v2")
            return frozenset(transports)
        transports = {"direct"}
        if self.config.dispatch_url:
            transports.add("mistral-dispatch")
            transports.add("llm-dispatch")
        if self.config.dispatch_v2_url:
            transports.add("llm-dispatch-v2")
        return frozenset(transports)

    def _storage_client(self):
        if self.storage is not None:
            return self.storage
        return b2_from_env()

    def _completion_fn(self) -> Callable[..., Any]:
        """Resolve LiteLLM lazily so users of ASR-only paths need not install the extra."""
        if self._completion is not None:
            return self._completion
        try:
            return importlib.import_module("litellm").completion
        except (ImportError, AttributeError) as exc:
            raise LLMBackendError(
                "install the 'llm' extra to use the direct LiteLLM backend"
            ) from exc

    @staticmethod
    def _structured_output(job: InferenceJob) -> str | None:
        value = job.inputs.get("structured_output")
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("LLM inputs.structured_output must be a non-empty string")
        return value

    def _response_model(self, job: InferenceJob) -> tuple[str, ResponseModel] | None:
        name = self._structured_output(job)
        return (name, response_model(name)) if name else None

    def _instructor_mode(self, resolved_model: str):
        """Choose Instructor's provider-neutral structured-output mode for standard profiles.

        Routes whose compiled profile requires native handling never reach this method. Keeping
        the mode decision here profile-agnostic prevents provider/model naming conventions from
        becoming a second source of structured-output behavior.
        """
        try:
            from instructor import Mode
        except ImportError as exc:
            raise LLMBackendError("install the 'llm' extra to use structured LLM output") from exc
        # The route's structured-output profile selects native JSON-object handling before this
        # method is reached. Remaining Instructor routes use its schema mode.
        return Mode.JSON_SCHEMA

    @staticmethod
    def _route_for_resolved_model(resolved_model: str, route=None):
        """Resolve route capabilities without interpreting provider/model naming conventions."""
        if route is not None:
            return route
        logical_model = canonical_model(resolved_model)
        candidates = ROUTE_CANDIDATES.get(logical_model)
        if candidates:
            return candidates[0]
        return next(
            (
                candidate
                for candidate in ROUTE_REGISTRY.values()
                if candidate.direct_model == resolved_model
            ),
            None,
        )

    def _response_format_for_route(
        self, model: ResponseModel, *, resolved_model: str, route=None
    ) -> dict[str, Any]:
        """Build the configured structured-output format for a physical route."""
        route = self._route_for_resolved_model(resolved_model, route)
        response_format = getattr(route, "structured_output_response_format", "json_schema")
        if response_format == "json_object":
            return {"type": "json_object"}
        if response_format != "json_schema":
            raise LLMBackendError(f"unsupported structured-output format: {response_format!r}")
        strip_keys = frozenset(getattr(route, "structured_output_schema_strip_keys", ()) or ())
        schema_model = _schema_variant_model(model, strip_keys)
        return {
            "type": "json_schema",
            "json_schema": {"name": model.__name__, "schema": schema_model.model_json_schema()},
        }

    def _resolve_api_base_and_headers(
        self, route: LLMRoute | None, *, direct: bool
    ) -> tuple[str | None, dict[str, str]]:
        """Resolve the provider endpoint and gateway headers for one call.

        Returns the route's own upstream unchanged unless this is a direct call *and* the gateway
        is both enabled and configured. Any missing piece (no account id, no slug) degrades to
        calling the provider directly rather than to a broken URL: losing gateway analytics is a
        strictly better failure than losing the request.
        """
        if route is None:
            return None, {}
        api_base = route.api_base or None
        if not direct or not _ai_gateway_enabled():
            return api_base, {}
        gateway_base = _ai_gateway_base()
        gateway_slug = route.ai_gateway_slug or route.provider
        if not gateway_base or not gateway_slug:
            return api_base, {}
        extra_headers: dict[str, str] = {}
        auth_token = os.environ.get("AI_GATEWAY_AUTH_TOKEN", "").strip()
        if auth_token:
            extra_headers["cf-aig-authorization"] = f"Bearer {auth_token}"
        return f"{gateway_base}/{gateway_slug}{_ai_gateway_path_prefix(route)}", extra_headers

    def _provider_options(
        self, job: InferenceJob, resolved_model: str, *, route=None, direct: bool = False
    ) -> dict[str, Any]:
        """Build the LiteLLM ``completion()`` kwargs for one route.

        ``direct`` marks a call this process makes to the provider itself. It defaults to False so
        the dispatch payload -- which shares this builder but is consumed by the Worker rather
        than by LiteLLM -- never picks up gateway routing.
        """
        options: dict[str, Any] = {"model": resolved_model}
        if route is not None:
            api_base, extra_headers = self._resolve_api_base_and_headers(route, direct=direct)
            if api_base:
                options["api_base"] = api_base
            if extra_headers:
                options["extra_headers"] = extra_headers
            if route.api_key_env:
                api_key = os.environ.get(route.api_key_env)
                if api_key:
                    options["api_key"] = api_key
        for field in ("temperature", "max_tokens", "tools", "tool_choice"):
            if field in job.inputs:
                options[field] = job.inputs[field]
        return options

    def _dispatch_response_format(
        self, model: ResponseModel, resolved_model: str, *, route=None
    ) -> Mapping[str, Any]:
        """Serialize the configured structured-output profile for the R10 queue."""
        return self._response_format_for_route(model, resolved_model=resolved_model, route=route)

    def _payload(
        self,
        job: InferenceJob,
        model: ResponseModel | None = None,
        *,
        resolved_model: str,
        policy: LLMRequestPolicy | None = None,
        estimated_tokens: int | None = None,
        input_tokens_estimate: int | None = None,
        output_token_budget: int | None = None,
        route=None,
        direct: bool = False,
    ) -> dict[str, Any]:
        """Build the provider-neutral OpenAI-shaped request sent by the dispatch transport.

        The unstructured direct path reuses this builder to shape its own LiteLLM call, and passes
        ``direct=True`` so that call (and only that call) is routed via the AI Gateway.
        """
        selected_route = self._route_for_resolved_model(resolved_model, route)
        include_profile_schema = model is not None and bool(
            getattr(selected_route, "structured_output_include_schema_in_prompt", False)
        )
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": _messages_with_schema(_messages(job), model)
            if include_profile_schema
            else _messages(job),
            "stream": False,
        }
        if model is not None:
            payload["response_format"] = self._dispatch_response_format(
                model, resolved_model, route=route
            )
        if policy is not None:
            payload["allow_paid"] = policy.allow_paid
            payload["allow_batch"] = policy.allow_batch
            payload["submit_next"] = policy.submit_next
            payload["timeout_class"] = policy.timeout_class
            if policy.queue_only and policy.allowed_models:
                # Durable requests are routed by the Worker.  Retain every permitted logical
                # model there so independent quota pools can be used when the first is full.
                payload["allowed_models"] = list(policy.allowed_models)
            if input_tokens_estimate is not None:
                payload["input_tokens_estimate"] = input_tokens_estimate
            if output_token_budget is not None:
                payload["output_token_budget"] = output_token_budget
            if policy.deadline_at is not None:
                payload["deadline_at"] = policy.deadline_at.isoformat()
        if estimated_tokens is not None:
            payload["estimated_tokens"] = estimated_tokens
        payload.update(self._provider_options(job, resolved_model, route=route, direct=direct))
        return payload

    @staticmethod
    def _output_token_budget(job: InferenceJob) -> int:
        value = job.inputs.get("max_tokens", DEFAULT_OUTPUT_TOKEN_MARGIN)
        return max(1, int(value)) if isinstance(value, int) else DEFAULT_OUTPUT_TOKEN_MARGIN

    @staticmethod
    def _admission_messages(
        messages: list[dict[str, Any]],
        structured: tuple[str, ResponseModel] | None,
        policy: LLMRequestPolicy,
    ) -> list[dict[str, Any]]:
        """Return the conservative request form used before physical-route selection."""
        if structured and any(
            any(
                getattr(candidate_route, "structured_output_include_schema_in_prompt", False)
                for candidate_route in ROUTE_CANDIDATES.get(canonical_model(candidate), ())
            )
            for candidate in (policy.allowed_models or ())
        ):
            return _messages_with_schema(messages, structured[1])
        return messages

    @staticmethod
    def _assert_route_context(
        route: LLMRoute | None, messages: list[dict[str, Any]], output_tokens: int
    ) -> None:
        """Fail locally before a direct provider call that cannot fit its physical route."""
        if route is None:
            return
        input_tokens = estimate_tokens(messages)
        if input_tokens > route.input_context_limit:
            raise LLMBackendError(
                "LLM input exceeds route context limit "
                f"({input_tokens}>{route.input_context_limit})"
            )
        if output_tokens > route.output_context_limit:
            raise LLMBackendError(
                f"LLM output budget exceeds route context limit "
                f"({output_tokens}>{route.output_context_limit})"
            )

    def _run_structured_direct(
        self,
        job: InferenceJob,
        model: ResponseModel,
        *,
        resolved_model: str,
        route=None,
        completion: Callable[..., Any] | None = None,
    ) -> JobResult:
        """Dispatch to whichever structured-output path this route actually works with.

        Routes with a native structured-output profile use the local parse/validate/retry path;
        standard profiles retain Instructor's typed parsing. The profile, not the provider/model
        name, determines which branch runs.
        """
        completion_fn = completion if completion is not None else self._completion_fn()
        route = self._route_for_resolved_model(resolved_model, route)
        if getattr(route, "structured_output_direct_handler", "instructor") == "native":
            return self._run_native_structured_direct(
                job, model, resolved_model=resolved_model, route=route, completion=completion_fn
            )
        try:
            import instructor
            from instructor.core.exceptions import InstructorRetryException
        except ImportError as exc:
            raise LLMBackendError("install the 'llm' extra to use structured LLM output") from exc
        try:
            typed, raw = instructor.from_litellm(
                completion_fn,
                mode=self._instructor_mode(resolved_model),
            ).create_with_completion(
                response_model=model,
                messages=_messages(job),
                max_retries=1,
                **self._provider_options(job, resolved_model, route=route, direct=True),
            )
        except InstructorRetryException as exc:
            if os.environ.get(_SAFE_DIAGNOSTICS_ENV) == "1":
                print(
                    "llm-safe-diagnostic: "
                    + json.dumps(
                        _safe_structured_failure_diagnostic(exc, job, model, resolved_model),
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                )
            # Do not expose model text or validation feedback in workflow logs.  The caller safely
            # defers and will obtain fresh evidence on its next scheduled run.
            raise LLMStructuredOutputError(
                "structured LLM response failed Pydantic validation"
            ) from None
        # Instructor returned a validated object; retain the normalized raw response contract so
        # existing task parsers and result storage remain provider-neutral.
        _ = typed
        return JobResult(
            task=job.task,
            recipe_hash=job.recipe_hash,
            output=_response_mapping(raw),
            model=resolved_model,
        )

    def _run_native_structured_direct(
        self,
        job: InferenceJob,
        model: ResponseModel,
        *,
        resolved_model: str,
        completion: Callable[..., Any],
        response_format: Mapping[str, Any] | None = None,
        route=None,
    ) -> JobResult:
        """Use a provider-native structured response with local validation and one retry.

        Gemini's REST API natively supports schema-constrained JSON output
        (``responseJsonSchema``) -- confirmed against the live API by
        ``citypods/llm_compat_probe.py``. Routing that through ``instructor.from_litellm()``
        doesn't reach it: Instructor's own (provider, mode) compatibility table (pinned
        instructor==1.15.4, confirmed its latest release) has no entry for
        ``(Provider.GEMINI, Mode.JSON_SCHEMA)`` -- only MD_JSON/TOOLS -- so it rejects the call
        before any request reaches Gemini. That's independent of LiteLLM's provider
        auto-detection: two live runs on two different LiteLLM versions both failed with the
        identical error. Rather than switch away from native schema mode (Gemini genuinely
        supports it) or add runtime fallback/re-probing logic, this calls LiteLLM directly with
        the same OpenAI-shaped ``response_format`` LiteLLM already translates into Gemini's native
        mechanism selected by the compiled profile, also used by the R10 dispatch payload.
        DeepSeek uses its documented JSON-object mode here because the installed Instructor registry
        treats custom DeepSeek routes as OpenAI and rejects them before a request. Both paths parse
        and validate against ``model``, then retry once with validation feedback.
        Both attempts' usage is billed: a first attempt that fails validation still reached
        Gemini and spent real tokens/quota, so on a retry-then-succeed outcome the returned
        ``output["usage"]`` is the *sum* of both responses' usage, not just the second's --
        otherwise ``_run_policy_job()``'s settle step would price only the final attempt and
        silently release the first attempt's already-spent reservation back to the ledger.
        """
        from pydantic import ValidationError

        messages = list(_messages(job))
        if route is not None and route.structured_output_include_schema_in_prompt:
            messages = _messages_with_schema(messages, model)
        options = self._provider_options(job, resolved_model, route=route, direct=True)
        response_format = response_format or self._response_format_for_route(
            model, resolved_model=resolved_model, route=route
        )
        failed_attempts: list[_FailedAttempt] = []
        first_attempt_usage: Any = None
        for attempt in range(2):  # original attempt + exactly one corrective retry
            raw = completion(messages=messages, response_format=response_format, **options)
            content = raw.choices[0].message.content
            try:
                parsed = json.loads(content)
                model.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as exc:
                failed_attempts.append(_FailedAttempt(exception=exc))
                if attempt == 0:
                    # A failed first attempt still reached the provider and consumed real
                    # tokens; keep its usage so a later successful retry's settlement can bill
                    # both, not just the retry.
                    try:
                        first_attempt_usage = _response_mapping(raw).get("usage")
                    except LLMBackendError:
                        first_attempt_usage = None
                    # Feed the invalid reply back as corrective feedback, mirroring Instructor's
                    # own retry contract -- never re-derive the schema, just point at what failed.
                    messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "Your last reply was not valid JSON matching the required schema "
                                f"({exc}). Reply again with corrected JSON only."
                            ),
                        },
                    ]
                    continue
                if os.environ.get(_SAFE_DIAGNOSTICS_ENV) == "1":
                    print(
                        "llm-safe-diagnostic: "
                        + json.dumps(
                            _safe_structured_failure_diagnostic(
                                _NativeStructuredRetryExhausted(failed_attempts),
                                job,
                                model,
                                resolved_model,
                            ),
                            sort_keys=True,
                        ),
                        file=sys.stderr,
                    )
                # Do not expose model text or validation feedback in workflow logs.  The caller
                # safely defers and will obtain fresh evidence on its next scheduled run.
                raise LLMStructuredOutputError(
                    "structured LLM response failed Pydantic validation"
                ) from None
            output = _response_mapping(raw)
            if first_attempt_usage is not None:
                merged = _sum_usage_fields(first_attempt_usage, output.get("usage"))
                if merged is not None:
                    output = {**output, "usage": merged}
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output=output,
                model=resolved_model,
            )
        raise AssertionError("unreachable: loop always returns or raises")

    @staticmethod
    def _structured_content(output: Mapping[str, Any]) -> str:
        choices = output.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            message = choices[0].get("message")
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                return message["content"]
        raise LLMStructuredOutputError("structured LLM response did not contain message content")

    def _validate_reconciled(
        self, output: Mapping[str, Any], structured_output: str | None
    ) -> None:
        if not structured_output:
            return
        model = response_model(structured_output)
        try:
            model.model_validate_json(self._structured_content(output))
        except (ValueError, TypeError):
            raise LLMStructuredOutputError(
                "structured dispatched response failed Pydantic validation"
            ) from None

    def _completed_dispatch_result(
        self,
        *,
        task: Task,
        recipe_hash: str,
        output: Any,
        structured_output: str | None,
        model: str | None = None,
    ) -> JobResult:
        """Validate and normalize a terminal Worker response from either POST or poll."""
        if not isinstance(output, Mapping):
            raise LLMBackendError("LLM dispatch returned a non-object response")
        self._validate_reconciled(output, structured_output)
        return JobResult(task=task, recipe_hash=recipe_hash, output=output, model=model)

    def _deferred_handle(
        self,
        job: InferenceJob,
        structured: tuple[str, ResponseModel] | None,
        messages: list[dict[str, Any]],
        policy: LLMRequestPolicy,
    ) -> JobHandle:
        """A portable "not done yet" handle -- either nothing is eligible right now, or a real
        429 just forced a backoff. Identical shape either way; ``reconcile()`` re-runs the same
        gates fresh regardless of which produced it."""
        return JobHandle(
            task=job.task,
            recipe_hash=job.recipe_hash,
            backend=self.name,
            ref=f"deferred:{job.recipe_hash}",
            structured_output=(structured[0] if structured else None),
            deferred_request=DeferredLLMRequest(
                messages=tuple(messages),
                policy=policy,
                output_token_budget=self._output_token_budget(job),
            ),
        )

    def run_inference(self, job: InferenceJob) -> JobResult | JobHandle:
        """Run directly through LiteLLM or enqueue through the asynchronous dispatch Worker."""
        if job.task not in LLM_TASKS:
            raise ValueError(f"LiteLLM backend does not handle task {job.task!r}")
        policy = job.inputs.get("llm_policy")
        structured = self._response_model(job)
        if policy is None:
            return self._run_without_policy(job, structured)
        if not isinstance(policy, LLMRequestPolicy):
            raise ValueError("LLM inputs.llm_policy must be an LLMRequestPolicy")
        if self.storage is None or (
            not policy.queue_only and not getattr(self.storage, "cas_capable", False)
        ):
            raise LLMBackendError("LLM scheduler requires a CAS-capable storage backend")
        if not job.recipe_hash:
            raise LLMBackendError("policy-bearing LLM jobs require a non-empty recipe_hash")

        # Someone -- a prior call for this exact recipe_hash, or the deferred-request sweep --
        # may already have an answer (or a still-pending record) waiting. Calling `run_inference`
        # again with the same job is a complete, valid way to check: no need to hold or reconcile
        # a handle explicitly.
        existing = look_up_deferred(self.storage, job.recipe_hash)
        if existing is not None:
            return existing
        if not terminal_failure_retry_allowed(self.storage, job.recipe_hash):
            raise LLMBackendError(
                "LLM terminal failure retry limit reached for this recipe; "
                "change the input/recipe, clear its failure marker after investigating, "
                "or wait for the marker's audit retention to expire"
            )

        messages = _messages(job)
        result = self._run_policy_job_paced(job, policy, structured, messages)
        # Cache the outcome either way: a completed result means a *later* call with the same
        # recipe_hash never has to pay for a real provider call again; a deferred handle means
        # the sweep (or a later call) can pick up exactly where this one left off.
        write_deferred(self.storage, job.recipe_hash, result)
        return result

    def _enqueue_durable_policy_job(
        self,
        job: InferenceJob,
        policy: LLMRequestPolicy,
        structured: tuple[str, ResponseModel] | None,
        messages: list[dict[str, Any]],
    ) -> JobResult | JobHandle:
        """Submit durable backlog work without reserving runner-side provider capacity."""
        if self.config.dispatch_v2_url:
            return self.enqueue_batch([job])[0]
        if not self.config.dispatch_url:
            raise LLMBackendError("durable queue policy requires LLM_DISPATCH_URL")
        allowed = policy.allowed_models or (self.config.model,)
        resolved_model = canonical_model(allowed[0])
        structured_name, model = structured if structured else (None, None)
        payload = self._payload(
            job,
            model,
            resolved_model=resolved_model,
            policy=policy,
            estimated_tokens=estimate_tokens(self._admission_messages(messages, structured, policy))
            + self._output_token_budget(job),
            input_tokens_estimate=estimate_tokens(
                self._admission_messages(messages, structured, policy)
            ),
            output_token_budget=self._output_token_budget(job),
        )
        # Older calls used the recipe hash directly when they entered the Worker (and carried a
        # producer-run deadline in their policy). A Worker idempotency record quite properly
        # rejects a later request with that same key but a different durable policy. Keep this
        # migration namespace stable: it makes all durable submissions idempotent with one
        # another without conflating them with a legacy, deadline-bound submission.
        headers = {
            "content-type": "application/json",
            "idempotency-key": f"{job.recipe_hash}:durable-queue-v1",
        }
        if self.config.dispatch_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_auth_token}"
        try:
            response = self._session.post(
                urljoin(self.config.dispatch_url.rstrip("/") + "/", "v1/chat/completions"),
                json=payload,
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch enqueue failed") from exc
        if response.status_code == 200:
            return self._completed_dispatch_result(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output=response.json(),
                structured_output=structured_name,
                model=resolved_model,
            )
        if response.status_code != 202:
            raise LLMBackendError(f"LLM dispatch enqueue returned HTTP {response.status_code}")
        body = response.json()
        ref = response.headers.get("location")
        if not ref and isinstance(body, Mapping):
            ref = body.get("id")
        if not ref:
            raise LLMBackendError("LLM dispatch response omitted a request reference")
        return JobHandle(
            task=job.task,
            recipe_hash=job.recipe_hash,
            backend=self.name,
            ref=ref,
            structured_output=structured_name,
            model=resolved_model,
        )

    def _run_policy_job(
        self,
        job: InferenceJob,
        policy: LLMRequestPolicy,
        structured: tuple[str, ResponseModel] | None,
        messages: list[dict[str, Any]],
    ) -> JobResult | JobHandle:
        """The full select-reserve-attempt-settle flow for a policy-bearing job. Shared by
        `run_inference` (first attempt) and `_reconcile_deferred` (retrying a deferred handle) --
        a retry re-evaluates every gate fresh, exactly like a first attempt, rather than polling
        something already submitted."""
        if policy.queue_only:
            return self._enqueue_durable_policy_job(job, policy, structured, messages)
        available_transports = self._available_transports()
        if policy.require_direct:
            # ``require_direct`` is an explicit capability override for GH Actions callers.  It
            # remains valid even when the backend's normal mode is dispatch; absent this opt-in,
            # dispatch mode never silently calls a provider key from the runner.
            available_transports = frozenset({"direct"})
        # Instructor's `max_retries=1` (see `_run_structured_direct`) can send up to two real
        # provider requests for one logical dispatch. Reserve the worst case up front so RPM/RPD/
        # TPM can never be breached even when both attempts happen; settling afterwards to the
        # single terminal response's actual usage only ever releases back what wasn't needed.
        max_provider_attempts = 2 if structured else 1
        admission_messages = self._admission_messages(messages, structured, policy)
        input_tokens = estimate_tokens(admission_messages)
        output_tokens = self._output_token_budget(job)
        per_attempt_tokens = input_tokens + output_tokens
        selection = select_and_reserve(
            self.storage,
            job.recipe_hash,
            policy,
            routes=ROUTE_REGISTRY,
            available_transports=available_transports,
            estimated_tokens=per_attempt_tokens * max_provider_attempts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            requests=max_provider_attempts,
        )
        if selection.model is None or selection.route is None:
            return self._deferred_handle(job, structured, messages, policy)
        resolved_model = selection.model
        route = selection.route
        owner = selection.owner
        assert owner is not None  # always set when a route was selected
        attempted = False
        attempted_requests = 0

        def _cleanup() -> None:
            if attempted:
                # The call reached the provider (or the Worker's queue) regardless of outcome, so
                # its rate-limit slot is genuinely spent -- settle (keep it charged), never
                # release. See review/33 §10.2.
                settle_route_reservation(
                    self.storage,
                    owner,
                    resolved_model,
                    route=route,
                    actual_requests=max(attempted_requests, 1),
                )
            else:
                release_route_reservation(self.storage, owner, resolved_model, route=route)

        def _rate_limited(retry_after: float | None) -> JobHandle:
            until = datetime.now(UTC) + timedelta(seconds=retry_after or _DEFAULT_BLOCK_SECONDS)
            print(
                f"llm rate limit: 429 from model={resolved_model} "
                f"(retry_after={retry_after if retry_after is not None else 'unspecified'}s), "
                f"blocked until {until.isoformat()}",
                flush=True,
            )
            block_route_until(self.storage, resolved_model, until, route=route)
            settle_route_reservation(
                self.storage,
                owner,
                resolved_model,
                route=route,
                # The most recent attempt is the one that got rejected as already-over-quota --
                # it never reached the model, so it shouldn't count against our own proactive
                # ledger (`block_route_until` above is what actually stops us re-hammering this
                # route; the request counters aren't load-bearing for that). Any *earlier* attempt
                # within this same call (e.g. a structured retry's first pass, which got a real
                # response that merely failed validation) did reach the provider and stays
                # charged -- so subtract exactly the rejected attempt, not the whole count.
                actual_requests=max(attempted_requests - 1, 0),
            )
            return self._deferred_handle(job, structured, messages, policy)

        # `selection.transport` is the single source of truth for which transport *this call*
        # actually uses -- resolved once, in `select_route` (`llm_scheduler.py`), from the same
        # inputs (`route.transports`, `available_transports`, `policy.allow_dispatch_overflow`)
        # this branch used to re-derive independently. That duplication was itself a bug
        # (CodeRabbit, review/41): `_owner_for` and this branch could disagree about which
        # transport a given call used, since one read `route.transports` (the route's
        # *capability*) while the other combined it with policy/config afresh -- a direct call
        # under `allow_dispatch_overflow=False` still keyed its ledger reservation as if it always
        # dispatched, silently deduping two genuinely concurrent direct calls sharing a
        # `recipe_hash` into one reservation. Reading the same resolved value here, rather than
        # recomputing it, makes the two impossible to disagree.
        #
        # Every compiled route offers both transports. Direct is preferred for a direct-capable
        # runner; dispatch mode selects the Worker, and `allow_dispatch_overflow` explicitly opts
        # a direct-capable runner into the Worker to reach its independent provider/account pool.
        is_dispatch = selection.transport in {"mistral-dispatch", "llm-dispatch"}
        direct_model = route.direct_model or resolved_model
        try:
            if not is_dispatch:
                if structured:
                    completion_fn = self._completion_fn()

                    def guarded_completion(**kwargs):
                        nonlocal attempted, attempted_requests
                        attempted = True
                        attempted_requests += 1
                        return completion_fn(**kwargs)

                    result = self._run_structured_direct(
                        job,
                        structured[1],
                        resolved_model=direct_model,
                        route=route,
                        completion=guarded_completion,
                    )
                else:
                    # `policy` is intentionally omitted here: `_payload()` attaches
                    # allow_paid/allow_batch/submit_next/deadline_at only for the Worker's
                    # dispatch payload (see the `is_dispatch` branch below) -- they are scheduler-
                    # internal fields the direct LiteLLM `completion()` call does not accept.
                    # Passing `policy=policy` on this branch was a real bug (CodeRabbit,
                    # review/41): those keys reached `completion_fn(**payload)` on every direct
                    # policy-bearing call.
                    payload = self._payload(
                        job, resolved_model=direct_model, route=route, direct=True
                    )
                    completion_fn = self._completion_fn()
                    attempted = True
                    attempted_requests += 1
                    response = completion_fn(**payload)
                    result = JobResult(
                        task=job.task,
                        recipe_hash=job.recipe_hash,
                        output=_response_mapping(response),
                        model=resolved_model,
                    )
            else:
                structured_name, model = structured if structured else (None, None)
                payload = self._payload(
                    job,
                    model,
                    resolved_model=resolved_model,
                    route=route,
                    policy=policy,
                    estimated_tokens=per_attempt_tokens * max_provider_attempts,
                    input_tokens_estimate=input_tokens,
                    output_token_budget=output_tokens,
                )
                headers = {"content-type": "application/json"}
                if self.config.dispatch_auth_token:
                    headers["authorization"] = f"Bearer {self.config.dispatch_auth_token}"
                headers["idempotency-key"] = job.recipe_hash
                attempted = True
                attempted_requests += 1
                response = self._session.post(
                    urljoin(self.config.dispatch_url.rstrip("/") + "/", "v1/chat/completions"),
                    json=payload,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                )
                if response.status_code == 200:
                    result = self._completed_dispatch_result(
                        task=job.task,
                        recipe_hash=job.recipe_hash,
                        output=response.json(),
                        structured_output=structured_name,
                        model=resolved_model,
                    )
                elif response.status_code == 202:
                    body = response.json()
                    ref = response.headers.get("location")
                    if not ref and isinstance(body, Mapping):
                        ref = body.get("id")
                    if not ref:
                        raise LLMBackendError("LLM dispatch response omitted a request reference")
                    # Deliberately not settled here: the reservation stays inflight until
                    # `reconcile()` observes the Worker's terminal response and can settle it to
                    # actual usage (see `reconcile()`). A job whose handle is never reconciled
                    # leaves an inflight entry until the reservation expiry is reaped. The shared
                    # ledger's expiry is what keeps concurrency-only routes from being stuck.
                    input_rate, output_rate, _ = route.pricing.rates_at(datetime.now(UTC))
                    return JobHandle(
                        task=job.task,
                        recipe_hash=job.recipe_hash,
                        backend=self.name,
                        ref=ref,
                        structured_output=structured_name,
                        model=resolved_model,
                        owner=owner,
                        route_id=route.route_id or None,
                        input_per_token=input_rate,
                        output_per_token=output_rate,
                        attempted_requests=attempted_requests,
                    )
                elif response.status_code == 429:
                    return _rate_limited(_retry_after_seconds(response))
                else:
                    raise LLMBackendError(f"LLM dispatch returned HTTP {response.status_code}")
        except requests.RequestException as exc:
            _cleanup()
            raise LLMBackendError("LLM request failed") from exc
        except BaseException as exc:
            if _is_rate_limited(exc):
                return _rate_limited(_retry_after_seconds(exc))
            _cleanup()
            raise

        input_rate, output_rate, _ = route.pricing.rates_at(datetime.now(UTC))
        actual_tokens, actual_cost = _priced_actual(
            result.output,
            input_per_token=input_rate,
            output_per_token=output_rate,
        )
        if result.model != resolved_model:
            result = JobResult(
                task=result.task,
                recipe_hash=result.recipe_hash,
                output=result.output,
                model=resolved_model,
            )
        settle_route_reservation(
            self.storage,
            owner,
            resolved_model,
            route=route,
            actual_tokens=actual_tokens,
            actual_cost=actual_cost,
            actual_requests=max(attempted_requests, 1),
        )
        return result

    # Overridable for deterministic tests (no real wall-clock sleeps / clock).
    _sleep = staticmethod(time.sleep)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _run_policy_job_paced(
        self,
        job: InferenceJob,
        policy: LLMRequestPolicy,
        structured: tuple[str, ResponseModel] | None,
        messages: list[dict[str, Any]],
    ) -> JobResult | JobHandle:
        """`_run_policy_job` with within-run rate pacing.

        A single logical dispatch that can't be placed *right now* only because a route's continuous
        RPM/TPM schedule is momentarily full, or because a real 429 briefly blocked it, is not given
        up on: this waits until a route is predicted to free up and retries, so one run drains its
        full *daily* quota while respecting average-rate spacing instead of bursting and stopping.
        It gives up (returns the deferred handle for the sweep / a later
        run) only when nothing frees up before ``policy.deadline_at`` -- i.e. the daily quota is
        genuinely spent, or the run's own wall-clock budget would elapse first. With no
        ``deadline_at`` set there is nothing to pace against, so it behaves exactly like the
        unpaced path (one attempt, then defer). Reservations are settled/released inside each
        ``_run_policy_job`` attempt exactly as before; no intermediate deferred record is written
        between retries (the caller writes only the final outcome), so a paced retry never leaves a
        stale pending handle behind."""
        while True:
            result = self._run_policy_job(job, policy, structured, messages)
            if isinstance(result, JobResult) or policy.deadline_at is None:
                return result
            selection = self._next_dispatch_eligibility(job, policy, structured, messages)
            if selection.model is None:
                wait = _pacing_wait_seconds(selection.retry_at, policy.deadline_at, self._now())
                if wait is None:
                    # Daily quota spent on every allowed route, or the soonest one to free up
                    # would do so after the run's own deadline -- give up pacing and let the
                    # caller's deferred handle stand (retried by the sweep / a later run).
                    print(
                        f"llm rate limit: {job.task} recipe={job.recipe_hash[:12]} giving up -- "
                        f"no allowed route frees up before the deadline "
                        f"({_format_rejected(selection.rejected)})",
                        flush=True,
                    )
                    return result
                print(
                    f"llm rate limit: {job.task} recipe={job.recipe_hash[:12]} pacing "
                    f"{wait:.0f}s ({_format_rejected(selection.rejected)})",
                    flush=True,
                )
                self._sleep(max(wait, _PACING_MIN_SLEEP_SECONDS))
            else:
                # A route freed up between the deferral and this check (the minute rolled). Retry
                # immediately, but with a tiny floor so a persistent reserve race can't hot-spin.
                self._sleep(_PACING_MIN_SLEEP_SECONDS)

    def _next_dispatch_eligibility(
        self,
        job: InferenceJob,
        policy: LLMRequestPolicy,
        structured: tuple[str, ResponseModel] | None,
        messages: list[dict[str, Any]],
    ) -> SelectionResult:
        """Read-only probe of the freshest ledger: which route (if any) is eligible right now.

        ``.model`` is set when some allowed route can be reserved right now (so a paced caller
        should retry immediately). Otherwise ``.retry_at`` is when the soonest allowed route is
        predicted to free up (``None`` if none ever will -- no eligible route), and ``.rejected``
        names why each allowed route was passed over (pacing log visibility)."""
        ledger, _ = load_llm_budget_cas(self.storage)
        max_provider_attempts = 2 if structured else 1
        admission_messages = self._admission_messages(messages, structured, policy)
        input_tokens = estimate_tokens(admission_messages)
        output_tokens = self._output_token_budget(job)
        per_attempt_tokens = input_tokens + output_tokens
        return select_route(
            policy,
            routes=ROUTE_REGISTRY,
            ledger=ledger,
            available_transports=self._available_transports(),
            estimated_tokens=per_attempt_tokens * max_provider_attempts,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            requests=max_provider_attempts,
            now=self._now(),
        )

    def _reconcile_deferred(self, handle: JobHandle) -> JobResult | None:
        """Retry a deferred handle: re-run the same gates fresh, complete if eligible now, or
        return another deferred handle (persisted back to the registry either way)."""
        if self.storage is None or not getattr(self.storage, "cas_capable", False):
            raise LLMBackendError("LLM scheduler requires a CAS-capable storage backend")
        deferred = handle.deferred_request
        assert deferred is not None
        messages = [dict(message) for message in deferred.messages]
        inputs: dict[str, Any] = {"messages": messages, "max_tokens": deferred.output_token_budget}
        if handle.structured_output:
            inputs["structured_output"] = handle.structured_output
        job = InferenceJob(task=handle.task, inputs=inputs, recipe_hash=handle.recipe_hash)
        structured = self._response_model(job)
        result = self._run_policy_job_paced(job, deferred.policy, structured, messages)
        write_deferred(self.storage, handle.recipe_hash, result)
        return None if isinstance(result, JobHandle) else result

    def _run_without_policy(
        self, job: InferenceJob, structured: tuple[str, ResponseModel] | None
    ) -> JobResult | JobHandle:
        """Preserve the pre-R13 request/response path exactly for callers without scheduler
        metadata (the returned ``model`` field is new, additive, and asserted by no pre-R13
        test)."""
        logical_model = canonical_model(self.config.model)
        route = (ROUTE_CANDIDATES.get(logical_model) or (None,))[0]
        if self.config.mode == "direct":
            direct_messages = (
                _messages_with_schema(_messages(job), structured[1])
                if structured
                and route is not None
                and route.structured_output_include_schema_in_prompt
                else _messages(job)
            )
            self._assert_route_context(route, direct_messages, self._output_token_budget(job))
            direct_model = route.direct_model if route is not None else logical_model
            if structured:
                result = self._run_structured_direct(
                    job, structured[1], resolved_model=direct_model, route=route
                )
                return JobResult(
                    task=result.task,
                    recipe_hash=result.recipe_hash,
                    output=result.output,
                    model=logical_model,
                )
            response = self._completion_fn()(
                **self._payload(job, resolved_model=direct_model, route=route, direct=True)
            )
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output=_response_mapping(response),
                model=logical_model,
            )

        structured_name, model = structured if structured else (None, None)
        payload = self._payload(job, model, resolved_model=logical_model, route=route)
        headers = {"content-type": "application/json"}
        if self.config.dispatch_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_auth_token}"
        if job.recipe_hash:
            headers["idempotency-key"] = job.recipe_hash
        try:
            response = self._session.post(
                urljoin(self.config.dispatch_url.rstrip("/") + "/", "v1/chat/completions"),
                json=payload,
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
            if response.status_code == 200:
                # An idempotent re-submit can observe a completed request after a prior process
                # returned a handle.  R12 does not persist that handle, so consume the terminal
                # response here rather than failing its next scheduled discovery attempt.
                return self._completed_dispatch_result(
                    task=job.task,
                    recipe_hash=job.recipe_hash,
                    output=response.json(),
                    structured_output=structured_name,
                    model=logical_model,
                )
            if response.status_code != 202:
                raise LLMBackendError(f"LLM dispatch returned HTTP {response.status_code}")
            body = response.json()
            ref = response.headers.get("location")
            if not ref and isinstance(body, Mapping):
                ref = body.get("id")
            if not ref:
                raise LLMBackendError("LLM dispatch response omitted a request reference")
            return JobHandle(
                task=job.task,
                recipe_hash=job.recipe_hash,
                backend=self.name,
                ref=ref,
                structured_output=structured_name,
                model=logical_model,
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch request failed") from exc

    def reconcile(self, handle: JobHandle) -> JobResult | None:
        """Return a validated result when ready, or ``None`` while still pending -- uniformly for
        a deferred-direct handle (re-runs selection) or a genuine Worker dispatch (polls it)."""
        if handle.backend == "llm-dispatch-v2":
            if handle.deferred_request is not None:
                return self._reconcile_deferred(handle)
            poll_res = self.poll_batch([handle])
            return poll_res.get(handle.ref)
        if handle.backend != self.name:
            raise ValueError(f"cannot reconcile handle for backend {handle.backend!r}")
        if handle.deferred_request is not None:
            return self._reconcile_deferred(handle)

        base = self.config.dispatch_url.rstrip("/") + "/"
        base_parts = urlsplit(base)
        if handle.ref.startswith("http"):
            candidate = handle.ref
            candidate_parts = urlsplit(candidate)
            if (candidate_parts.scheme, candidate_parts.netloc) != (
                base_parts.scheme,
                base_parts.netloc,
            ):
                raise LLMBackendError("LLM dispatch location points to an unexpected host")
        elif handle.ref.startswith("/"):
            candidate = urljoin(base, handle.ref.lstrip("/"))
        else:
            candidate = urljoin(base, f"v1/requests/{handle.ref}")
        try:
            validate_source_url(
                candidate, allowed_hosts=(base_parts.hostname or "",), resolve=False
            )
        except SecurityError as exc:
            raise LLMBackendError("LLM dispatch location is not an allowed HTTPS URL") from exc
        headers = {}
        if self.config.dispatch_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_auth_token}"
        try:
            response = self._session.get(
                candidate, headers=headers, timeout=self.config.timeout_seconds
            )
            if response.status_code == 202:
                try:
                    body = response.json()
                    if isinstance(body, dict) and body.get("last_error"):
                        last_err = body["last_error"]
                        if (
                            isinstance(last_err, dict)
                            and last_err.get("code") == "upstream_timeout"
                        ):
                            dur = last_err.get("duration_seconds")
                            dur_label = (
                                f"{dur}s" if isinstance(dur, (int, float)) else "unknown duration"
                            )
                            attempts = body.get("attempts", 1)
                            attempts_label = (
                                str(attempts) if isinstance(attempts, int) else "unknown"
                            )
                            route_id = last_err.get("route_id", "unknown")
                            avail = body.get("available_at", "soon")
                            warn_msg = (
                                f"::warning title=LLM Upstream Timeout Warning::"
                                f"Request {handle.ref} for model '{handle.model}' timed out "
                                f"after {dur_label} on route '{route_id}' "
                                f"(attempt {attempts_label}). "
                                f"Next retry at {avail}."
                            )
                            print(warn_msg)
                except (AttributeError, TypeError, ValueError):
                    pass
                return None
            if response.status_code != 200:
                err_code = "unknown"
                try:
                    err_json = response.json().get("error", {})
                    if isinstance(err_json, Mapping):
                        err_code = str(err_json.get("code", "unknown"))
                    if err_code == "upstream_timeout":
                        dur = err_json.get("duration_seconds")
                        dur_label = (
                            f"{dur}s" if isinstance(dur, (int, float)) else "unknown duration"
                        )
                        attempts = err_json.get("attempts")
                        attempts_label = str(attempts) if isinstance(attempts, int) else "unknown"
                        route_id = err_json.get("route_id", "unknown")
                        err_annotation = (
                            f"::error title=LLM Terminal Timeout Failure::"
                            f"Request {handle.ref} for model '{handle.model}' failed permanently "
                            f"after {attempts_label} attempts exceeding {dur_label} timeout "
                            f"on route '{route_id}'."
                        )
                        print(err_annotation)
                except (AttributeError, TypeError, ValueError):
                    pass
                msg = f"LLM dispatch poll returned HTTP {response.status_code}"
                if err_code != "unknown":
                    msg += f" ({err_code})"
                if err_code == "upstream_timeout":
                    msg += f" timed out after {dur_label}"
                if response.status_code in {404, 410, 502}:
                    raise LLMDispatchTerminalError(msg)
                raise LLMBackendError(msg)
            output = response.json()
            try:
                result = self._completed_dispatch_result(
                    task=handle.task,
                    recipe_hash=handle.recipe_hash,
                    output=output,
                    structured_output=handle.structured_output,
                    model=handle.model,
                )
            except LLMStructuredOutputError:
                # The deferred sweep owns the one schema-correction retry. Leave the completed
                # Worker record intact here so it can clone the exact original request before
                # replacing this handle with the corrective attempt.
                self._settle_dispatched_reservation(handle, output)
                raise
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch poll failed") from exc

        self._settle_dispatched_reservation(handle, result.output)

        # A policy-tracked handle (§10.2/§10 in review/33) left its reservation inflight at
        # dispatch time specifically so it could be settled to *actual* usage here, once the
        # Worker's terminal response is available, instead of staying frozen at the estimate for
        # the job's entire lifetime. A handle from `_run_without_policy` has no `owner` and is a
        # no-op here, unchanged from the pre-R13 behavior.
        if (
            handle.owner is not None
            and handle.model is not None
            and self.storage is not None
            and getattr(self.storage, "cas_capable", False)
        ):
            write_deferred(self.storage, handle.recipe_hash, result)
            # Layer 1 Verified Consumption: purge R2 object only after B2 write succeeds
            try:
                self._session.delete(
                    candidate, headers=headers, timeout=self.config.timeout_seconds
                )
            except Exception:
                pass
        elif self.storage is not None and getattr(self.storage, "cas_capable", False):
            write_deferred(self.storage, handle.recipe_hash, result)
            try:
                self._session.delete(
                    candidate, headers=headers, timeout=self.config.timeout_seconds
                )
            except Exception:
                pass
        return result

    def enqueue_batch(self, jobs: Sequence[InferenceJob]) -> list[JobResult | JobHandle]:
        """Submit a batch of InferenceJobs to v2 dispatch with B2 payload staging.

        Directly writes B2 payload keys from the client before submitting the batch metadata
        to the ingress Worker. Applies client-side throttling to avoid submitting requests
        when daily caps or rate limits have been exhausted.
        """
        if not jobs:
            return []

        out: list[JobResult | JobHandle | None] = [None] * len(jobs)
        uncached_indices: list[int] = []
        for i, job in enumerate(jobs):
            cached = look_up_deferred(self.storage, job.recipe_hash)
            if isinstance(cached, JobResult):
                out[i] = cached
            else:
                uncached_indices.append(i)

        if not uncached_indices:
            return [cast("JobResult | JobHandle", r) for r in out]

        if self._daily_ingest_exhausted or (
            self.config.daily_ingest_cap is not None
            and self._daily_ingest_admitted >= self.config.daily_ingest_cap
        ):
            for idx in uncached_indices:
                job = jobs[idx]
                policy = (
                    job.inputs.get("llm_policy") if isinstance(job.inputs, Mapping) else None
                ) or getattr(job, "policy", None)
                out[idx] = JobHandle(
                    task=job.task,
                    recipe_hash=job.recipe_hash,
                    backend="llm-dispatch-v2",
                    ref=f"deferred-daily-cap-{job.recipe_hash}",
                    deferred_request=DeferredLLMRequest(
                        messages=tuple(_messages(job)),
                        policy=policy or LLMRequestPolicy(),
                        output_token_budget=self._output_token_budget(job),
                    ),
                )
            return [cast("JobResult | JobHandle", r) for r in out]

        if not self.config.dispatch_v2_url:
            for idx in uncached_indices:
                job = jobs[idx]
                out[idx] = self.run_inference(job)
            return [cast("JobResult | JobHandle", r) for r in out]

        prepared_jobs: list[dict[str, Any]] = []
        job_meta: list[tuple[int, InferenceJob, str | None, str, str]] = []
        storage = self._storage_client()

        for idx in uncached_indices:
            job = jobs[idx]
            policy = (
                job.inputs.get("llm_policy") if isinstance(job.inputs, Mapping) else None
            ) or getattr(job, "policy", None)
            structured = self._response_model(job)
            structured_name, model = structured if structured else (None, None)
            allowed = (policy.allowed_models if policy and policy.allowed_models else None) or (
                self.config.model,
            )
            logical_model = canonical_model(allowed[0])
            job_id = str(uuid.uuid4())
            payload_key = f"payloads/{job_id}/request.json"

            payload = self._payload(
                job,
                model,
                resolved_model=logical_model,
                policy=policy,
                output_token_budget=self._output_token_budget(job),
            )
            canonical_payload_str = json.dumps(payload, sort_keys=True)
            request_digest = hashlib.sha256(canonical_payload_str.encode("utf-8")).hexdigest()
            idempotency_key = f"{job.recipe_hash}:durable-queue-v2" if job.recipe_hash else job_id

            if storage is not None:
                storage.write_bytes(
                    payload_key,
                    canonical_payload_str.encode("utf-8"),
                    content_type="application/json",
                )

            messages = _messages(job)
            in_tokens = estimate_tokens(messages)
            out_tokens = self._output_token_budget(job)
            priority = 0 if policy and getattr(policy, "priority", 1) == 0 else 1

            prepared_jobs.append(
                {
                    "id": job_id,
                    "idempotency_key": idempotency_key,
                    "request_digest": request_digest,
                    "prompt_family": job.task,
                    "input_token_estimate": in_tokens,
                    "max_output_token_estimate": out_tokens,
                    "payload_key": payload_key,
                    "priority": priority,
                    "policy_json": json.dumps(
                        {
                            "allowed_models": allowed,
                            "allow_paid": (
                                getattr(policy, "allow_paid", False) if policy else False
                            ),
                        }
                    ),
                }
            )
            job_meta.append((idx, job, structured_name, logical_model, job_id))

        headers = {"content-type": "application/json"}
        if self.config.dispatch_v2_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_v2_auth_token}"

        url = urljoin(self.config.dispatch_v2_url.rstrip("/") + "/", "v2/jobs:enqueue-batch")
        try:
            response = self._session.post(
                url,
                json={"jobs": prepared_jobs},
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch v2 enqueue-batch request failed") from exc

        if response.status_code == 429:
            self._daily_ingest_exhausted = True
            for idx, job, _sname, _lmodel, _jid in job_meta:
                policy = (
                    job.inputs.get("llm_policy") if isinstance(job.inputs, Mapping) else None
                ) or getattr(job, "policy", None)
                out[idx] = JobHandle(
                    task=job.task,
                    recipe_hash=job.recipe_hash,
                    backend="llm-dispatch-v2",
                    ref=f"deferred-429-{job.recipe_hash}",
                    deferred_request=DeferredLLMRequest(
                        messages=tuple(_messages(job)),
                        policy=policy or LLMRequestPolicy(),
                        output_token_budget=self._output_token_budget(job),
                    ),
                )
            return [cast("JobResult | JobHandle", r) for r in out]

        if response.status_code != 200:
            raise LLMBackendError(
                f"LLM dispatch v2 enqueue-batch returned HTTP {response.status_code}: "
                f"{response.text}"
            )

        data = response.json()
        accepted_ids = {a["id"] for a in data.get("accepted", []) if isinstance(a, dict)}
        rejected_by_id = {
            r["id"]: r.get("reason") for r in data.get("rejected", []) if isinstance(r, dict)
        }

        for idx, job, structured_name, logical_model, job_id in job_meta:
            if job_id in accepted_ids:
                self._daily_ingest_admitted += 1
                handle = JobHandle(
                    task=job.task,
                    recipe_hash=job.recipe_hash,
                    backend="llm-dispatch-v2",
                    ref=job_id,
                    structured_output=structured_name,
                    model=logical_model,
                )
                write_deferred(self.storage, job.recipe_hash, handle)
                out[idx] = handle
            else:
                reason = rejected_by_id.get(job_id, "unknown")
                if reason == "daily_cap_exceeded":
                    self._daily_ingest_exhausted = True
                    policy = (
                        job.inputs.get("llm_policy") if isinstance(job.inputs, Mapping) else None
                    ) or getattr(job, "policy", None)
                    out[idx] = JobHandle(
                        task=job.task,
                        recipe_hash=job.recipe_hash,
                        backend="llm-dispatch-v2",
                        ref=f"deferred-cap-{job.recipe_hash}",
                        deferred_request=DeferredLLMRequest(
                            messages=tuple(_messages(job)),
                            policy=policy or LLMRequestPolicy(),
                            output_token_budget=self._output_token_budget(job),
                        ),
                    )
                elif reason == "idempotency_conflict":
                    raise LLMBackendError(f"LLM dispatch v2 idempotency conflict for job {job_id}")
                else:
                    raise LLMBackendError(f"LLM dispatch v2 rejected job {job_id}: {reason}")

        return [cast("JobResult | JobHandle", r) for r in out]

    def poll_batch(self, handles: Sequence[JobHandle]) -> dict[str, JobResult | None]:
        """Poll multiple v2 handles in a single request and fetch completed results directly
        from B2."""
        v2_handles = [h for h in handles if h.backend == "llm-dispatch-v2"]
        if not v2_handles:
            return {}
        if not self.config.dispatch_v2_url:
            return {h.ref: None for h in v2_handles}

        ids = [h.ref for h in v2_handles]
        headers = {"content-type": "application/json"}
        if self.config.dispatch_v2_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_v2_auth_token}"

        url = urljoin(self.config.dispatch_v2_url.rstrip("/") + "/", "v2/jobs:poll-batch")
        try:
            response = self._session.post(
                url,
                json={"ids": ids},
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch v2 poll-batch request failed") from exc

        if response.status_code != 200:
            raise LLMBackendError(
                f"LLM dispatch v2 poll-batch returned HTTP {response.status_code}: {response.text}"
            )

        data = response.json()
        statuses = {s["id"]: s for s in data.get("statuses", []) if isinstance(s, dict)}
        results: dict[str, JobResult | None] = {}
        storage = self._storage_client()

        for h in v2_handles:
            st = statuses.get(h.ref)
            if not st:
                results[h.ref] = None
                continue

            state = st.get("state")
            if state == "completed" and st.get("result_key"):
                result_key = st["result_key"]
                output: dict[str, Any] = {}
                if storage is not None:
                    raw_bytes = storage.read_bytes(result_key)
                    if raw_bytes:
                        output = json.loads(raw_bytes.decode("utf-8"))
                res = JobResult(
                    task=h.task,
                    recipe_hash=h.recipe_hash,
                    output=output,
                    model=h.model,
                )
                write_deferred(self.storage, h.recipe_hash, res)
                results[h.ref] = res
            elif state == "failed":
                raise LLMBackendError(
                    f"LLM dispatch v2 job {h.ref} failed permanently: {st.get('error')}"
                )
            else:
                results[h.ref] = None

        return results

    def _settle_dispatched_reservation(self, handle: JobHandle, output: Mapping[str, Any]) -> None:
        """Settle a Worker-owned reservation from its terminal raw response.

        This deliberately precedes structured-output validation at reconciliation time: malformed
        output still consumed the provider request and must not leave its quota reservation
        inflight while the sweep submits its bounded corrective retry.
        """
        if handle.owner is not None and handle.model is not None and self.storage is not None:
            route = ROUTE_REGISTRY.get(handle.route_id) if handle.route_id else None
            if route is None:
                # A physical route can be retired between queue submission and reconciliation.
                # Locate its persisted reservation before falling back to a currently configured
                # logical candidate: settlement/release must use the physical ledger key that was
                # actually charged, not whichever provider happens to rank first today.
                ledger, _ = load_llm_budget_cas(self.storage)
                reservation_route_id = ledger.find_inflight_owner(handle.owner)
                if reservation_route_id:
                    route = LLMRoute(
                        model=handle.model,
                        transport="direct",
                        free=True,
                        quota=QuotaPolicy(),
                        pricing=PricingPolicy(),
                        route_id=reservation_route_id,
                    )
                else:
                    route = (ROUTE_CANDIDATES.get(canonical_model(handle.model)) or (None,))[0]
            if route is not None and getattr(self.storage, "cas_capable", False):
                # Price against the rate captured on the handle at reservation time, not
                # whatever `ROUTES` says right now -- config can change between a dispatch and
                # its eventual reconciliation. Fall back to an uncosted token-only settlement for
                # a handle that predates this field rather than guessing a rate.
                if handle.input_per_token is not None and handle.output_per_token is not None:
                    actual_tokens, actual_cost = _priced_actual(
                        output,
                        input_per_token=handle.input_per_token,
                        output_per_token=handle.output_per_token,
                    )
                else:
                    actual_tokens, actual_cost = _usage_tokens(output), None
                settle_route_reservation(
                    self.storage,
                    handle.owner,
                    handle.model,
                    route=route,
                    actual_tokens=actual_tokens,
                    actual_cost=actual_cost,
                    # `None` for a handle written before this field existed -- `settle()` leaves
                    # the request count untouched rather than guessing, same as it already does
                    # for `actual_tokens`/`actual_cost` on an unpriced legacy handle.
                    actual_requests=handle.attempted_requests,
                )

    def delete_dispatched_ref(self, ref: str) -> None:
        """Best-effort deletion of a remote dispatched request from R2."""
        if not ref or not self.config.dispatch_url:
            return
        # Normalize the ref: handles store either a bare ID ("chatcmpl-..."), a path
        # ("/v1/requests/chatcmpl-..."), or a full URL. Extract the bare ID for all cases.
        match = re.search(r"(chatcmpl-[A-Za-z0-9-]{8,96})", ref)
        if not match:
            return
        bare_id = match.group(1)
        base = self.config.dispatch_url.rstrip("/") + "/"
        url = urljoin(base, f"v1/requests/{bare_id}")
        headers = {}
        if self.config.dispatch_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_auth_token}"
        try:
            self._session.delete(url, headers=headers, timeout=self.config.timeout_seconds)
        except Exception:
            pass

    def retry_malformed_dispatched(self, handle: JobHandle) -> JobHandle:
        """Ask the Worker to clone a completed request with one corrective schema instruction."""
        if not self.config.dispatch_url:
            raise LLMBackendError("schema correction requires LLM_DISPATCH_URL")
        base = self.config.dispatch_url.rstrip("/") + "/"
        match = re.search(r"(chatcmpl-[A-Za-z0-9-]{8,96})", handle.ref)
        if not match:
            raise LLMBackendError(
                "LLM schema-correction requires a valid dispatch request reference"
            )
        request_id = match.group(1)
        url = urljoin(base, f"v1/requests/{request_id}/schema-retry")
        headers = {
            "content-type": "application/json",
            # Keep B2's logical recipe stable, but make this one correction idempotent without
            # colliding with the original Worker submission.
            "idempotency-key": f"{handle.recipe_hash}:schema-correction-v1",
        }
        if self.config.dispatch_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_auth_token}"
        try:
            response = self._session.post(
                url, json={}, headers=headers, timeout=self.config.timeout_seconds
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM schema-correction enqueue failed") from exc
        if response.status_code not in {200, 202}:
            raise LLMBackendError(
                f"LLM schema-correction enqueue returned HTTP {response.status_code}"
            )
        body = response.json()
        ref = response.headers.get("location") or (
            body.get("id") if isinstance(body, Mapping) else None
        )
        if not isinstance(ref, str) or not ref:
            raise LLMBackendError("LLM schema-correction response omitted a request reference")
        return JobHandle(
            task=handle.task,
            recipe_hash=handle.recipe_hash,
            backend=self.name,
            ref=ref,
            structured_output=handle.structured_output,
            model=handle.model,
        )

    poll = reconcile


__all__ = [
    "LLMBackendConfig",
    "LLMBackendError",
    "LLMDispatchTerminalError",
    "LLMStructuredOutputError",
    "LLM_TASKS",
    "LiteLLMBackend",
    "SUPPORTED_MODELS",
    "TASK_PROMPTS",
    "TASK_VERSIONS",
]
