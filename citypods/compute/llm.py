"""LiteLLM routing plus Instructor/Pydantic structured outputs for reserved LLM verbs.

Direct calls use Instructor for provider-mode selection, parsing, Pydantic validation, and one
bounded corrective retry.  The R10 Worker remains an asynchronous transport: it durably stores the
Pydantic-generated response format, and reconciliation validates the completed reply locally.
The v2 queue also supports one queue-owned corrective retry without changing a task's response
contract.

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
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
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
        "moment-extraction",
        "moment-judge",
    }
)
TASK_VERSIONS: dict[Task, str] = {
    "summarize": "1",
    "tag": "1",
    "soundbite-select": "1",
    "classify-civic-platforms": "6",
    "agenda-item-extract": "1",
    "agenda-chapter-locate": "1",
    "moment-extraction": "1",
    "moment-judge": "1",
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
    "moment-extraction": (
        "Extract grounded chapter summaries, pull quotes, and discussion directions from the "
        "supplied "
        "meeting material. Never invent transcript quotes or official outcomes."
    ),
    "moment-judge": (
        "Score a supplied grounded candidate only. Never rewrite, repair, or create a candidate."
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
# The ingress Worker caps both enqueue and poll payloads at this many jobs.
_WORKER_BATCH_LIMIT = 1000
# Small recovery sets are cheaper and easier to diagnose one at a time. Larger sets stay batched
# so a partial/uncertain response cannot turn into one Worker invocation per unresolved item.
BATCH_RETRY_ISOLATION_THRESHOLD = 5

# Completed-result resolution is pure B2 I/O wait (one result GET plus write_deferred's own
# round trips per job), so a small pool cuts the deferred sweep's runtime without adding
# meaningful client CPU or risking provider-side rate limits -- no LLM call happens here.
_POLL_RESULT_MAX_WORKERS = 8

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


class _LLMBatchItemError(LLMBackendError):
    """An error attached to one batch item, optionally eligible for one isolated retry."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


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


def _emit_v2_dispatch_event(operation: str, *, batch_size: int, **counts: int | str) -> None:
    """Emit bounded, payload-free v2 dispatch telemetry for workflow/Worker logs."""
    event: dict[str, int | str] = {
        "event": "llm_dispatch_v2_batch",
        "operation": operation,
        "batch_size": batch_size,
        "request_count": 1,
        **counts,
    }
    print(json.dumps(event, sort_keys=True), flush=True)


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
        # The UTC day _daily_ingest_exhausted was set on, so it can be cleared once the
        # coordinator's own admission window has rolled over -- without this, a long-running
        # process (or one started shortly before UTC midnight) that hits the cap once would
        # short-circuit every job for the rest of its run, even long after the server-side cap
        # reset and had headroom again.
        self._daily_ingest_exhausted_day: str | None = None

    def _reset_daily_ingest_state_if_new_day(self) -> None:
        today = datetime.now(UTC).date().isoformat()
        stale_day = (
            self._daily_ingest_exhausted_day is not None
            and self._daily_ingest_exhausted_day != today
        )
        if stale_day:
            self._daily_ingest_exhausted = False
            self._daily_ingest_admitted = 0
        self._daily_ingest_exhausted_day = today

    def _mark_daily_ingest_exhausted(self) -> None:
        self._daily_ingest_exhausted = True
        self._daily_ingest_exhausted_day = datetime.now(UTC).date().isoformat()

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
        """The client v2's ``payloads/``/``results/`` staging writes/reads through.

        This data is B2-resident by design (workers/llm-dispatch-v2's own SigV4 client and
        wrangler.jsonc's "B2-only payload storage" -- the coordinator never gets R2 credentials),
        and is written with an unconditional ``put_cas`` (job_id is a fresh UUID per call, so
        there is no CAS race to protect -- see enqueue_batch's comment). Production's
        ``self.storage`` is usually a ``RoutingStorage`` whose ``COORDINATION_PREFIXES``
        deliberately excludes ``payloads/``/``results/`` (they aren't R2 coordination state), so
        it routes those keys to its B2 primary and then -- correctly, per its own invariant --
        refuses the put_cas/get_bytes call outright, because B2 is deliberately marked
        non-cas_capable there (review/17 §5: B2 doesn't enforce real If-Match/If-None-Match, so
        the router won't let a caller assume atomicity from it). That gate protects callers who
        need real compare-and-swap; it must not block this unconditional write/read. Go straight
        to the routed primary so v2 submissions actually reach B2 instead of raising
        NotImplementedError on every attempt. A non-routing storage (e.g. a test double, or a
        plain B2/R2 backend from a non-``routing`` deployment) is used exactly as given.
        """
        if self.storage is not None:
            if getattr(self.storage, "name", None) == "routing":
                primary = getattr(self.storage, "primary", None)
                if primary is not None:
                    return primary
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
        # "llm-dispatch-v2" is included even though no compiled route currently advertises it (v2
        # is only reached today through _enqueue_durable_policy_job's own short-circuit, not
        # select_route) -- so a future route-catalog change that does advertise it can never
        # silently fall through to a direct provider call using a runner-side API key, which is
        # exactly what this dispatch/direct split exists to prevent (review/41).
        is_dispatch = selection.transport in {"mistral-dispatch", "llm-dispatch", "llm-dispatch-v2"}
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
            result = poll_res.get(handle.ref)
            if isinstance(result, Exception):
                raise result
            return result
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

    def enqueue_batch(
        self, jobs: Sequence[InferenceJob]
    ) -> list[JobResult | JobHandle | Exception]:
        """Submit a batch of InferenceJobs to v2 dispatch with B2 payload staging.

        Directly writes B2 payload keys from the client before submitting the batch metadata
        to the ingress Worker. Applies client-side throttling to avoid submitting requests
        when daily caps or rate limits have been exhausted.
        """
        if not jobs:
            return []

        if self.storage is None or not getattr(self.storage, "cas_capable", False):
            # run_inference() raises the same error for the analogous direct-dispatch case;
            # enqueue_batch reaches the deferred registry (look_up_deferred/write_deferred)
            # unconditionally below and must fail the same clear way, not with an opaque
            # AttributeError deep inside it.
            raise LLMBackendError(
                "LLM dispatch v2 batch enqueue requires a CAS-capable storage backend"
            )

        self._reset_daily_ingest_state_if_new_day()

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

            # Deliberately NOT `policy=policy`/`output_token_budget=...` here, unlike v1's call
            # sites: those two kwargs are what makes _payload() fold allow_paid/allow_batch/
            # submit_next/timeout_class/allowed_models/output_token_budget/deadline_at into the
            # SAME dict as model/messages -- v1's own ingress (normalizeChatRequest in
            # workers/llm-dispatch-proxy/src/index.js) then strips those policy-only fields back
            # out before ever building an upstream provider request, via its own field-by-field
            # allowlist. v2 has no equivalent step: gateway.js's upstreamRequestForRoute() just
            # spreads whatever this stored payload contains straight into the provider request
            # body. v2's protocol already carries every one of those fields it actually needs
            # (allowed_models/allow_paid) separately, in policy_json below -- so passing `policy`
            # here just leaked router-only bookkeeping into the literal HTTP body sent to Gemini/
            # Mistral/etc., which every provider correctly rejected as unrecognized fields
            # (Mistral: "Extra inputs are not permitted"; Groq: "property 'allow_batch' is
            # unsupported") -- a 100% dispatch failure rate invisible until AI Gateway routing was
            # fixed and its request/response logging became the first thing to ever show it.
            payload = self._payload(
                job,
                model,
                resolved_model=logical_model,
            )
            canonical_payload_str = json.dumps(payload, sort_keys=True)
            request_digest = hashlib.sha256(canonical_payload_str.encode("utf-8")).hexdigest()
            idempotency_key = f"{job.recipe_hash}:durable-queue-v2" if job.recipe_hash else job_id

            if storage is not None:
                # Unconditional PUT (no if_none_match/if_match): job_id is a fresh UUID for
                # every call, so there is no concurrent-writer race to guard against here.
                storage.put_cas(
                    payload_key,
                    canonical_payload_str.encode("utf-8"),
                    "application/json",
                )

            messages = _messages(job)
            in_tokens = estimate_tokens(messages)
            out_tokens = self._output_token_budget(job)
            priority = policy.priority if policy else 1

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
            _emit_v2_dispatch_event(
                "enqueue-batch", batch_size=len(prepared_jobs), unknown=len(prepared_jobs)
            )
            raise LLMBackendError("LLM dispatch v2 enqueue-batch request failed") from exc

        if response.status_code == 429:
            self._mark_daily_ingest_exhausted()
            _emit_v2_dispatch_event(
                "enqueue-batch",
                batch_size=len(prepared_jobs),
                deferred=len(job_meta),
                http_status=response.status_code,
            )
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
            _emit_v2_dispatch_event(
                "enqueue-batch",
                batch_size=len(prepared_jobs),
                unknown=len(prepared_jobs),
                http_status=response.status_code,
            )
            raise LLMBackendError(
                f"LLM dispatch v2 enqueue-batch returned HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            _emit_v2_dispatch_event(
                "enqueue-batch", batch_size=len(prepared_jobs), unknown=len(prepared_jobs)
            )
            raise LLMBackendError("LLM dispatch v2 enqueue-batch returned malformed JSON") from exc
        if not isinstance(data, Mapping):
            _emit_v2_dispatch_event(
                "enqueue-batch", batch_size=len(prepared_jobs), unknown=len(prepared_jobs)
            )
            raise LLMBackendError("LLM dispatch v2 enqueue-batch returned a malformed response")

        accepted_entries = data.get("accepted")
        rejected_entries = data.get("rejected")
        if not isinstance(accepted_entries, list) or not isinstance(rejected_entries, list):
            _emit_v2_dispatch_event(
                "enqueue-batch", batch_size=len(prepared_jobs), unknown=len(prepared_jobs)
            )
            raise LLMBackendError("LLM dispatch v2 enqueue-batch omitted per-job outcomes")

        # The coordinator echoes back each entry's `submitted_id` (this client's own job_id)
        # rather than requiring the caller to match on `id` -- on an idempotent replay, `id` is
        # the ORIGINAL row's canonical id, which differs from the fresh job_id a retry always
        # generates locally (coordinator.js's enqueueBatch). Matching on submitted_id (which is
        # always present and always equal to what this call sent) makes acceptance detection
        # correct for both a fresh insert and a replay; the canonical `id` becomes this job's
        # JobHandle ref so later poll_batch calls resolve it.
        accepted_by_submitted_id: dict[str, str] = {}
        rejected_by_id: dict[str, str] = {}
        try:
            for accepted in accepted_entries:
                if (
                    not isinstance(accepted, Mapping)
                    or not isinstance(accepted.get("submitted_id"), str)
                    or not isinstance(accepted.get("id"), str)
                ):
                    raise ValueError("malformed accepted entry")
                submitted_id = accepted["submitted_id"]
                if submitted_id in accepted_by_submitted_id:
                    raise ValueError("duplicate accepted entry")
                accepted_by_submitted_id[submitted_id] = accepted["id"]
            for rejected in rejected_entries:
                if (
                    not isinstance(rejected, Mapping)
                    or not isinstance(rejected.get("id"), str)
                    or not isinstance(rejected.get("reason"), str)
                ):
                    raise ValueError("malformed rejected entry")
                rejected_id = rejected["id"]
                if rejected_id in rejected_by_id:
                    raise ValueError("duplicate rejected entry")
                rejected_by_id[rejected_id] = rejected["reason"]
        except ValueError as exc:
            _emit_v2_dispatch_event(
                "enqueue-batch", batch_size=len(prepared_jobs), unknown=len(prepared_jobs)
            )
            raise LLMBackendError(
                "LLM dispatch v2 enqueue-batch returned malformed outcomes"
            ) from exc

        accepted_count = 0
        replayed_count = 0
        deferred_count = 0
        rejected_count = 0
        unknown_count = 0
        for idx, job, structured_name, logical_model, job_id in job_meta:
            if job_id in accepted_by_submitted_id:
                canonical_id = accepted_by_submitted_id[job_id]
                accepted_count += 1
                if canonical_id == job_id:
                    self._daily_ingest_admitted += 1
                else:
                    replayed_count += 1
                handle = JobHandle(
                    task=job.task,
                    recipe_hash=job.recipe_hash,
                    backend="llm-dispatch-v2",
                    ref=canonical_id,
                    structured_output=structured_name,
                    model=logical_model,
                )
                write_deferred(storage, job.recipe_hash, handle)
                out[idx] = handle
            else:
                reason = rejected_by_id.get(job_id, "unknown")
                if reason == "daily_cap_exceeded":
                    deferred_count += 1
                    self._mark_daily_ingest_exhausted()
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
                    rejected_count += 1
                    out[idx] = LLMBackendError(
                        f"LLM dispatch v2 idempotency conflict for job {job_id}"
                    )
                elif reason == "unknown":
                    unknown_count += 1
                    out[idx] = _LLMBatchItemError(
                        f"LLM dispatch v2 enqueue-batch omitted job {job_id}", retryable=True
                    )
                else:
                    rejected_count += 1
                    out[idx] = LLMBackendError(f"LLM dispatch v2 rejected job {job_id}: {reason}")

        _emit_v2_dispatch_event(
            "enqueue-batch",
            batch_size=len(prepared_jobs),
            accepted=accepted_count,
            replayed=replayed_count,
            deferred=deferred_count,
            rejected=rejected_count,
            unknown=unknown_count,
        )

        return [cast("JobResult | JobHandle | Exception", r) for r in out]

    def poll_batch(self, handles: Sequence[JobHandle]) -> dict[str, JobResult | None | Exception]:
        """Poll multiple v2 handles in a single request and fetch completed results directly
        from B2."""
        v2_handles = [h for h in handles if h.backend == "llm-dispatch-v2"]
        if not v2_handles:
            return {}
        if not self.config.dispatch_v2_url:
            return {h.ref: None for h in v2_handles}

        # The ingress Worker caps both enqueue and poll payloads. Callers such as
        # the daily deferred sweep deliberately hand us their whole pending registry, so enforce
        # that transport limit here rather than depending on every caller to remember it.
        results: dict[str, JobResult | None | Exception] = {}
        for start in range(0, len(v2_handles), _WORKER_BATCH_LIMIT):
            chunk = v2_handles[start : start + _WORKER_BATCH_LIMIT]
            try:
                results.update(self._poll_batch_chunk(chunk))
            except Exception as exc:  # noqa: BLE001 -- isolate one failed chunk from its siblings
                error = _LLMBatchItemError(
                    "LLM dispatch v2 poll-batch chunk was unavailable", retryable=True
                )
                error.__cause__ = exc
                results.update({handle.ref: error for handle in chunk})
        return results

    def _poll_batch_chunk(
        self, v2_handles: Sequence[JobHandle]
    ) -> dict[str, JobResult | None | Exception]:
        """Poll one Worker-sized v2 status batch.

        Kept separate from :meth:`poll_batch` so each Worker-sized response can retain its
        per-handle outcomes. A bad terminal item becomes an exception for that handle; siblings
        remain authoritative and callers may isolate only the bad or unknown handles.
        """

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
            _emit_v2_dispatch_event(
                "poll-batch", batch_size=len(v2_handles), unknown=len(v2_handles)
            )
            raise LLMBackendError("LLM dispatch v2 poll-batch request failed") from exc

        if response.status_code != 200:
            _emit_v2_dispatch_event(
                "poll-batch",
                batch_size=len(v2_handles),
                unknown=len(v2_handles),
                http_status=response.status_code,
            )
            raise LLMBackendError(
                f"LLM dispatch v2 poll-batch returned HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except (TypeError, ValueError) as exc:
            _emit_v2_dispatch_event(
                "poll-batch", batch_size=len(v2_handles), unknown=len(v2_handles)
            )
            raise LLMBackendError("LLM dispatch v2 poll-batch returned malformed JSON") from exc
        if not isinstance(data, Mapping) or not isinstance(data.get("statuses"), list):
            _emit_v2_dispatch_event(
                "poll-batch", batch_size=len(v2_handles), unknown=len(v2_handles)
            )
            raise LLMBackendError("LLM dispatch v2 poll-batch omitted per-handle outcomes")
        statuses: dict[str, Mapping[str, Any]] = {}
        try:
            for status in data["statuses"]:
                if not isinstance(status, Mapping) or not isinstance(status.get("id"), str):
                    raise ValueError("malformed status entry")
                status_id = status["id"]
                if status_id in statuses:
                    raise ValueError("duplicate status entry")
                statuses[status_id] = status
        except ValueError as exc:
            _emit_v2_dispatch_event(
                "poll-batch", batch_size=len(v2_handles), unknown=len(v2_handles)
            )
            raise LLMBackendError("LLM dispatch v2 poll-batch returned malformed outcomes") from exc
        results: dict[str, JobResult | None | Exception] = {}
        storage = self._storage_client()

        def _resolve_completed(h: JobHandle, result_key: str):
            """Fetch, validate and persist one completed job's result.

            Runs on a worker thread. Each handle touches only keys derived from its own
            ``recipe_hash`` (the result object, plus write_deferred's canonical record and index
            pointers), so concurrent handles never write the same key. Returns one of
            ``("pending", None)``, ``("error", exc)`` or ``("done", JobResult)`` rather than
            mutating shared state, so the caller merges everything on the main thread.
            """
            try:
                raw = storage.get_bytes(result_key) if storage is not None else None
                body = raw[0] if raw else None
                if not body:
                    # Never persist an empty/unreadable result as completed: per
                    # citypods/compute/llm_deferred.py's write_deferred, a completed record is
                    # terminal and never downgraded back to pending, so caching {} here would be
                    # permanent. Treat it as still pending; a later poll (once B2
                    # read-after-write consistency catches up) can resolve it correctly.
                    return "pending", None
                output = json.loads(body.decode("utf-8"))
                res = self._completed_dispatch_result(
                    task=h.task,
                    recipe_hash=h.recipe_hash,
                    output=output,
                    structured_output=h.structured_output,
                    model=h.model,
                )
                write_deferred(storage, h.recipe_hash, res)
            except LLMBackendError as exc:
                return "error", exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                # A stored result that isn't valid UTF-8/JSON must fail only THIS job. Before the
                # parallel rewrite an uncaught exception here would abort the serial for-loop
                # partway through, losing every handle after it in iteration order; under
                # list(pool.map(...)) the blast radius is worse -- ALL results are collected
                # before any are merged, so one bad body would silently lose every sibling's
                # already-resolved outcome too. Wrap and return it exactly like a validation
                # failure instead.
                return "error", LLMBackendError(
                    f"LLM dispatch v2 unreadable result body for job {h.ref}: {exc}"
                )
            except Exception as exc:  # noqa: BLE001 -- isolate storage failure to this job
                error = LLMBackendError(f"LLM dispatch v2 result resolution failed for job {h.ref}")
                error.__cause__ = exc
                return "error", error
            return "done", res

        # Partition first, so the B2-bound work is a single parallel phase. Everything else here
        # is pure bookkeeping over the already-fetched statuses.
        completed: list[tuple[JobHandle, str]] = []
        for h in v2_handles:
            st = statuses.get(h.ref)
            if not st:
                # The v2 protocol deliberately omits unknown IDs. It is not safe to treat absence
                # as an authoritative pending observation because doing so would strand a handle
                # forever; return a per-handle error so the caller can isolate/reconcile it.
                results[h.ref] = _LLMBatchItemError(
                    f"LLM dispatch v2 poll-batch omitted job {h.ref}", retryable=True
                )
                continue
            state = st.get("state")
            if state == "completed" and st.get("result_key"):
                completed.append((h, st["result_key"]))
            elif state == "failed":
                results[h.ref] = LLMDispatchTerminalError(
                    f"LLM dispatch v2 job {h.ref} failed permanently"
                )
            else:
                results[h.ref] = None

        # Each completed job costs several sequential B2 round trips (the result GET, plus
        # write_deferred's own read/pointer/canonical writes), and they are pure I/O wait. Running
        # them serially made the daily sweep's runtime scale with the number of completions; a
        # small pool cuts that several-fold without changing any per-handle semantics. Ordering is
        # irrelevant -- results are keyed by ref, and the handles are independent.
        resolved: list[tuple[JobHandle, str, Any]] = []
        if completed:
            workers = min(_POLL_RESULT_MAX_WORKERS, len(completed))
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    outcomes = list(pool.map(lambda item: _resolve_completed(*item), completed))
            else:
                outcomes = [_resolve_completed(*item) for item in completed]
            for (h, _key), (kind, value) in zip(completed, outcomes, strict=True):
                resolved.append((h, kind, value))

        acked_refs: list[str] = []
        for h, kind, value in resolved:
            if kind == "done":
                results[h.ref] = value
                acked_refs.append(h.ref)
            elif kind == "error":
                results[h.ref] = value
            else:
                results[h.ref] = None

        # Only now, with each result durably in the deferred registry, tell the coordinator it may
        # retire those jobs and their B2 objects. Deliberately excludes the "error" handles: a
        # result that failed structured-output validation is exactly what the sweep's
        # schema-correction path re-reads, so its row must survive.
        self._ack_batch(acked_refs)

        _emit_v2_dispatch_event(
            "poll-batch",
            batch_size=len(v2_handles),
            completed=sum(isinstance(value, JobResult) for value in results.values()),
            pending=sum(value is None for value in results.values()),
            failed=sum(
                isinstance(value, Exception) and not isinstance(value, _LLMBatchItemError)
                for value in results.values()
            ),
            unknown=sum(isinstance(value, _LLMBatchItemError) for value in results.values()),
        )

        return results

    def _ack_batch(self, refs: Sequence[str]) -> None:
        """Tell the v2 coordinator these jobs' results are durably persisted client-side.

        The coordinator retires an acked job immediately instead of holding it for
        ``COMPLETED_RETENTION_DAYS`` (38), which is what keeps its SQLite `jobs` table and the
        matching B2 payload/result objects small -- see review/44 "Consumption ack". Call this
        only after ``write_deferred`` has succeeded for every ref.

        Best-effort by design: the result is already durable on our side, so a failed ack costs
        nothing but a later purge. It must never turn a successful poll into an error.
        """
        if not refs or not self.config.dispatch_v2_url:
            return
        headers = {"content-type": "application/json"}
        if self.config.dispatch_v2_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_v2_auth_token}"
        url = urljoin(self.config.dispatch_v2_url.rstrip("/") + "/", "v2/jobs:ack-batch")
        for start in range(0, len(refs), _WORKER_BATCH_LIMIT):
            chunk = list(refs[start : start + _WORKER_BATCH_LIMIT])
            try:
                self._session.post(
                    url, json={"ids": chunk}, headers=headers, timeout=self.config.timeout_seconds
                )
            except requests.RequestException:
                return

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
        """Clone a completed request with one corrective instruction."""
        if handle.backend == "llm-dispatch-v2":
            return self._retry_malformed_dispatched_v2(handle)
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

    def _retry_malformed_dispatched_v2(self, handle: JobHandle) -> JobHandle:
        """Stage and enqueue one v2 schema-correction payload.

        The v2 ingress cannot read B2, so the corrected request is written first and its digest
        and token estimate are sent alongside the payload key. The coordinator clones the source
        job's policy and output budget into a new idempotency namespace, then the malformed source
        is consumption-acked by the caller after the replacement handle and correction marker are
        durable.
        """
        if not self.config.dispatch_v2_url:
            raise LLMBackendError("schema correction requires LLM_DISPATCH_V2_URL")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", handle.ref):
            raise LLMBackendError(
                "LLM schema-correction requires a valid dispatch v2 job reference"
            )

        storage = self._storage_client()
        if storage is None:
            raise LLMBackendError("LLM schema correction requires a storage backend")
        original_key = f"payloads/{handle.ref}/request.json"
        raw = storage.get_bytes(original_key)
        body = raw[0] if raw else None
        if body is None:
            raise LLMBackendError("LLM schema-correction could not read the original v2 payload")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise LLMBackendError(
                "LLM schema-correction found an invalid original v2 payload"
            ) from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("messages"), list):
            raise LLMBackendError("LLM schema-correction found no valid messages in the v2 payload")

        corrected_messages = [dict(message) for message in payload["messages"]]
        corrected_messages.append(
            {
                "role": "user",
                "content": (
                    "Retry this task. Your previous response failed local schema validation. "
                    "Return only one JSON object that exactly matches the requested response "
                    "schema."
                ),
            }
        )
        corrected_payload = dict(payload)
        corrected_payload["messages"] = corrected_messages
        canonical_payload = json.dumps(corrected_payload, sort_keys=True)
        # Keep this key stable across a lost HTTP response and a client retry. The corrected
        # payload is deterministic, so overwriting this one source-specific object cannot change
        # an already-accepted correction and avoids orphaning a fresh object on every retry.
        corrected_key = f"payloads/{handle.ref}/schema-correction.json"
        request_digest = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        input_token_estimate = estimate_tokens(corrected_messages)

        try:
            storage.put_cas(
                corrected_key,
                canonical_payload.encode("utf-8"),
                "application/json",
            )
        except Exception as exc:
            raise LLMBackendError("LLM schema-correction payload staging failed") from exc

        headers = {"content-type": "application/json"}
        if self.config.dispatch_v2_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_v2_auth_token}"
        url = urljoin(
            self.config.dispatch_v2_url.rstrip("/") + "/",
            f"v2/jobs/{handle.ref}:schema-retry",
        )
        body = {
            "corrected_payload_key": corrected_key,
            "corrected_request_digest": request_digest,
            "corrected_input_token_estimate": input_token_estimate,
        }
        try:
            response = self._session.post(
                url,
                json=body,
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
            if response.status_code != 200:
                raise LLMBackendError(
                    f"LLM schema-correction enqueue returned HTTP {response.status_code}"
                )
            response_body = response.json()
            ref = response_body.get("id") if isinstance(response_body, Mapping) else None
            if not isinstance(ref, str) or not ref:
                raise LLMBackendError("LLM schema-correction response omitted a v2 job reference")
            _emit_v2_dispatch_event(
                "schema-retry", batch_size=1, schema_retry=1, singleton_fallback=0
            )
        except requests.RequestException as exc:
            # The request may have reached the Worker before the connection failed. Keep the
            # deterministic payload so a retry can safely reuse it if the clone was accepted.
            raise LLMBackendError("LLM schema-correction enqueue failed") from exc
        except (LLMBackendError, TypeError, ValueError):
            raise

        return JobHandle(
            task=handle.task,
            recipe_hash=handle.recipe_hash,
            backend="llm-dispatch-v2",
            ref=ref,
            structured_output=handle.structured_output,
            model=handle.model,
        )

    def ack_dispatched_ref(self, handle: JobHandle) -> None:
        """Ack a corrected dispatch after its replacement handle is durably persisted."""
        if handle.backend == "llm-dispatch-v2":
            self._ack_batch([handle.ref])

    poll = reconcile


class BatchingDispatchBackend:
    """Run-scoped collector for independent v2 queue-only jobs.

    Callers that already process their inputs concurrently can use this adapter without changing
    their per-item finalize path: a new queue-only job receives an ordinary pending ``JobHandle``
    immediately, while :meth:`flush` later submits all collected jobs through the v2 bulk API.
    Completed or already-pending deferred records still pass through the wrapped backend unchanged,
    so a retry can finalize cached work in the same run as before.

    This is deliberately a narrow adapter rather than a new transport.  It only collects jobs that
    are both ``queue_only`` and configured for v2; direct calls, v1 calls, and every non-policy
    task retain their existing timing and error behavior.
    """

    def __init__(self, backend: LiteLLMBackend):
        self._backend = backend
        self._queued: dict[str, tuple[InferenceJob, JobHandle]] = {}
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._backend.name

    @property
    def config(self) -> LLMBackendConfig:
        return self._backend.config

    @property
    def storage(self):
        return self._backend.storage

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._queued)

    def _can_batch(self, job: InferenceJob) -> bool:
        policy = job.inputs.get("llm_policy") if isinstance(job.inputs, Mapping) else None
        return bool(
            isinstance(policy, LLMRequestPolicy)
            and policy.queue_only
            and self._backend.config.dispatch_v2_url
            and self._backend.storage is not None
            and getattr(self._backend.storage, "cas_capable", False)
            and job.recipe_hash
        )

    def run_inference(self, job: InferenceJob) -> JobResult | JobHandle:
        if not self._can_batch(job):
            return self._backend.run_inference(job)

        existing = look_up_deferred(self._backend.storage, job.recipe_hash)
        if existing is not None:
            return existing
        if not terminal_failure_retry_allowed(self._backend.storage, job.recipe_hash):
            # Preserve the wrapped backend's specific terminal-failure error message.
            return self._backend.run_inference(job)

        with self._lock:
            queued = self._queued.get(job.recipe_hash)
            if queued is not None:
                return queued[1]
            handle = JobHandle(
                task=job.task,
                recipe_hash=job.recipe_hash,
                backend="llm-dispatch-v2",
                ref=f"batch-pending:{job.recipe_hash}",
                model=canonical_model(self._backend.config.model),
            )
            self._queued[job.recipe_hash] = (job, handle)
            return handle

    def enqueue_batch(
        self, jobs: Sequence[InferenceJob]
    ) -> list[JobResult | JobHandle | Exception]:
        """Collect a stage's existing ``dispatch_job_batch`` inputs without submitting yet."""
        return [self.run_inference(job) for job in jobs]

    def poll_batch(self, handles: Sequence[JobHandle]) -> dict[str, JobResult | None | Exception]:
        """Keep newly collected handles provisional until the run-level :meth:`flush`.

        ``dispatch_job_batch`` calls this after ``enqueue_batch`` as its normal immediate
        reconcile pass.  Returning no resolutions here leaves a provisional handle in the stage;
        the runner replays that episode after flush against the wrapped backend's durable record.
        """
        return {}

    def flush(self) -> list[BatchDispatchOutcome]:
        """Submit every collected job in bounded enqueue/poll batches.

        The collector is emptied before network I/O.  A failed bulk request therefore follows
        ``dispatch_job_batch``'s existing per-job fallback, while jobs added after a flush starts
        remain queued for a subsequent flush instead of being accidentally discarded.
        """
        with self._lock:
            jobs = [job for job, _handle in self._queued.values()]
            self._queued.clear()
        results = dispatch_job_batch(self._backend, jobs)
        return [
            BatchDispatchOutcome(job=job, result=result)
            for job, result in zip(jobs, results, strict=True)
        ]


@dataclass(frozen=True)
class BatchDispatchOutcome:
    """One run-batched submission result paired with its original inference job."""

    job: InferenceJob
    result: JobResult | JobHandle | Exception


def _batch_item_retryable(value: object) -> bool:
    """Return whether a batch outcome is an unknown item-level result worth recovering."""
    return isinstance(value, _LLMBatchItemError) and value.retryable


def _enqueue_batch_with_retry(
    backend: LiteLLMBackend, jobs: Sequence[InferenceJob]
) -> list[JobResult | JobHandle | Exception]:
    """Submit one chunk and retry only unknown outcomes in one bounded recovery round."""
    try:
        initial = list(backend.enqueue_batch(jobs))
    except Exception as exc:  # noqa: BLE001 -- the whole response is unavailable
        initial = []
        retry_positions = list(range(len(jobs)))
        whole_failure = exc
    else:
        whole_failure = None
        if len(initial) != len(jobs):
            retry_positions = list(range(len(jobs)))
            initial = []
        else:
            retry_positions = [
                index for index, value in enumerate(initial) if _batch_item_retryable(value)
            ]

    if not retry_positions:
        return cast("list[JobResult | JobHandle | Exception]", initial)

    retry_jobs = [jobs[index] for index in retry_positions]
    if len(retry_jobs) > BATCH_RETRY_ISOLATION_THRESHOLD:
        _emit_v2_dispatch_event(
            "enqueue-batch-retry",
            batch_size=len(retry_jobs),
            batch_retry=1,
            singleton_fallback=0,
        )
        try:
            retry_results = list(backend.enqueue_batch(retry_jobs))
        except Exception as exc:  # noqa: BLE001 -- no further retry round is allowed
            retry_results = [
                LLMBackendError("LLM dispatch v2 enqueue recovery batch failed")
                for _job in retry_jobs
            ]
            if whole_failure is None:
                whole_failure = exc
        if len(retry_results) != len(retry_jobs):
            retry_results = [
                LLMBackendError("LLM dispatch v2 enqueue recovery omitted per-job outcomes")
                for _job in retry_jobs
            ]
    else:
        _emit_v2_dispatch_event(
            "enqueue-singleton-fallback",
            batch_size=len(retry_jobs),
            batch_retry=0,
            singleton_fallback=len(retry_jobs),
            request_count=len(retry_jobs),
        )
        retry_results = []
        for job in retry_jobs:
            try:
                one = list(backend.enqueue_batch([job]))
                retry_results.append(
                    one[0]
                    if len(one) == 1
                    else LLMBackendError("LLM dispatch v2 singleton enqueue omitted its outcome")
                )
            except Exception as exc:  # noqa: BLE001 -- isolate only this job
                retry_results.append(exc)

    if initial:
        merged = list(initial)
    else:
        fallback_error = (
            "LLM dispatch v2 enqueue recovery unavailable"
            if whole_failure is not None
            else "LLM dispatch v2 enqueue response omitted per-job outcomes"
        )
        merged = [LLMBackendError(fallback_error) for _job in jobs]
    for index, value in zip(retry_positions, retry_results, strict=True):
        merged[index] = value
    return merged


def _poll_batch_with_retry(
    backend: LiteLLMBackend, handles: Sequence[JobHandle]
) -> dict[str, JobResult | None | Exception]:
    """Poll one chunk and retry only unknown handles in one bounded recovery round."""
    try:
        initial = dict(backend.poll_batch(handles))
    except Exception:  # noqa: BLE001 -- the whole response is unavailable
        initial = {}
        retry_handles = [h for h in handles if not h.ref.startswith("batch-pending:")]
        whole_failure = True
    else:
        whole_failure = False
        retry_handles = [
            handle
            for handle in handles
            if not handle.ref.startswith("batch-pending:")
            and isinstance(backend, LiteLLMBackend)
            and (handle.ref not in initial or _batch_item_retryable(initial[handle.ref]))
        ]

    if not retry_handles:
        return initial

    if len(retry_handles) > BATCH_RETRY_ISOLATION_THRESHOLD:
        _emit_v2_dispatch_event(
            "poll-batch-retry",
            batch_size=len(retry_handles),
            batch_retry=1,
            singleton_fallback=0,
        )
        try:
            retry_results = dict(backend.poll_batch(retry_handles))
        except Exception:  # noqa: BLE001 -- no further retry round is allowed
            retry_results = {}
    else:
        _emit_v2_dispatch_event(
            "poll-singleton-fallback",
            batch_size=len(retry_handles),
            batch_retry=0,
            singleton_fallback=len(retry_handles),
            request_count=len(retry_handles),
        )
        retry_results = {}
        for handle in retry_handles:
            try:
                one = dict(backend.poll_batch([handle]))
            except Exception:  # noqa: BLE001 -- leave this handle pending for a later sweep
                continue
            if handle.ref in one:
                retry_results[handle.ref] = one[handle.ref]

    for handle in retry_handles:
        if handle.ref in retry_results:
            outcome = retry_results[handle.ref]
            if _batch_item_retryable(outcome):
                # A second unknown response is still not evidence of failure. Leave the handle
                # absent so dispatch_job_batch preserves it and the deferred sweep can retry it.
                initial.pop(handle.ref, None)
            else:
                initial[handle.ref] = outcome
        else:
            # Neither poll returned trustworthy status for this handle. Absence is not a failure:
            # leave it pending so the caller keeps its durable JobHandle.
            initial.pop(handle.ref, None)
    if whole_failure and not retry_results:
        # Keep the existing pending handle when neither the original nor the recovery poll gave
        # trustworthy status data. The deferred sweep will make the next bounded attempt.
        return {}
    return initial


def dispatch_job_batch(
    backend: LiteLLMBackend, jobs: Sequence[InferenceJob]
) -> list[JobResult | JobHandle | Exception]:
    """Submit a whole run's jobs in one `enqueue_batch` call, then give any v2-backed pending
    handle one immediate reconcile pass via a single batched `poll_batch` call, instead of a
    caller submitting (and separately reconciling) one job at a time in a loop.

    Shared by every caller that used to build N `InferenceJob`s and call `run_inference` on each
    one individually -- `citypods/stages.py`'s chapter-agenda/chapter-locator stages and
    `citypods/tournament.py`'s pairwise-judge comparisons, as of 2026-08-18. See review/44's
    2026-08-18 incident retrospective: the dispatch v2 protocol/DO/`enqueue_batch`/`poll_batch`
    were always batch-capable end to end, but no caller ever actually accumulated more than one
    job per call before this -- every one submitted and reconciled a single job per iteration of
    its own loop, which is exactly the per-job Worker-request volume that incident flagged as the
    thing worth fixing next.

    `enqueue_batch(jobs)` is a strict, behavior-preserving generalization of calling
    `run_inference` once per job for a `queue_only=True` policy: both check `look_up_deferred`/
    write the resulting handle back per job, and both fall through the same
    `_enqueue_durable_policy_job` dispatch path -- `enqueue_batch` just does it for N jobs in one
    SQLite transaction and one Worker request instead of N of each. `poll_batch` already filters
    to `backend == "llm-dispatch-v2"` handles internally, so passing it every pending handle
    (v1-backed ones included) is exactly as safe as an explicit per-handle backend check would be
    -- see `citypods/stages.py`'s prior 2026-08-18 fix for that exact guard, now subsumed here.

    Returns one entry per input job, in the same order, each either a `JobResult`, a `JobHandle`
    still pending, or an `Exception` if that specific job's own submission failed (the caller is
    expected to check for this and attribute it to that job, same as it would a `run_inference`
    raise in the old per-job code). Known per-job rejection errors are not retried. Unknown
    outcomes get one recovery round: sets larger than five stay batched, and smaller sets use
    isolated calls; a failed recovery round never recursively fans out.
    """
    if not jobs:
        return []

    results: list[JobResult | JobHandle | Exception] = []
    for chunk_start in range(0, len(jobs), _WORKER_BATCH_LIMIT):
        chunk = jobs[chunk_start : chunk_start + _WORKER_BATCH_LIMIT]
        results.extend(_enqueue_batch_with_retry(backend, chunk))

    pending_handles = [r for r in results if isinstance(r, JobHandle)]
    if not pending_handles:
        return results
    polled = _poll_batch_with_retry(backend, pending_handles)
    return [
        (
            polled[r.ref]
            if isinstance(r, JobHandle) and r.ref in polled and polled[r.ref] is not None
            else r
        )
        for r in results
    ]


__all__ = [
    "BatchDispatchOutcome",
    "BatchingDispatchBackend",
    "LLMBackendConfig",
    "LLMBackendError",
    "LLMDispatchTerminalError",
    "LLMStructuredOutputError",
    "LLM_TASKS",
    "LiteLLMBackend",
    "SUPPORTED_MODELS",
    "TASK_PROMPTS",
    "TASK_VERSIONS",
    "dispatch_job_batch",
]
