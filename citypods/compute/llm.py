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

import importlib
import json
import math
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlsplit

import requests

from citypods.compute.base import Backend, InferenceJob, JobHandle, JobResult, Task
from citypods.compute.llm_budget import (
    block_route_until,
    release_route_reservation,
    settle_route_reservation,
)
from citypods.compute.llm_deferred import look_up_deferred, write_deferred
from citypods.compute.llm_policy import (
    DEFAULT_OUTPUT_TOKEN_MARGIN,
    ROUTES,
    DeferredLLMRequest,
    LLMRequestPolicy,
    estimate_tokens,
)
from citypods.compute.llm_scheduler import select_and_reserve
from citypods.compute.structured import ResponseModel, response_model
from citypods.security import SecurityError, validate_source_url

LLM_TASKS: frozenset[Task] = frozenset(
    {"summarize", "tag", "soundbite-select", "classify-civic-platforms"}
)
TASK_VERSIONS: dict[Task, str] = {
    "summarize": "1",
    "tag": "1",
    "soundbite-select": "1",
    "classify-civic-platforms": "6",
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
}

SUPPORTED_MODELS = frozenset(
    {
        "gemini/gemini-3-flash-preview",
        "gemini/gemini-3.1-flash-lite",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "mistral/mistral-large-latest",
        "mistral/mistral-large-3",
    }
)

# Fallback backoff when a 429 carries no parseable Retry-After hint.
_DEFAULT_BLOCK_SECONDS = 60.0
_SAFE_DIAGNOSTICS_ENV = "LLM_SAFE_DIAGNOSTICS"


class LLMBackendError(RuntimeError):
    """A safe, provider-agnostic adapter error (provider response bodies are not exposed)."""


class LLMStructuredOutputError(LLMBackendError):
    """A malformed model reply that a caller may safely defer and retry with fresh evidence."""


def _safe_structured_failure_diagnostic(
    exc: BaseException, job: InferenceJob, model: ResponseModel, resolved_model: str
) -> dict[str, Any]:
    """Return non-sensitive metadata for an opt-in structured-output failure log.

    Never include messages, headers, credentials, response bodies, or provider exception text:
    any of those can contain meeting material. The schema summary is intentionally boolean/count
    only, enough to distinguish a provider-schema rejection from an ordinary malformed reply.
    """
    schema = model.model_json_schema()
    raw = json.dumps(schema, sort_keys=True)
    attempts = getattr(exc, "failed_attempts", ()) or ()
    last = attempts[-1].exception if attempts else exc
    return {
        "event": "llm_structured_output_failure",
        "model": resolved_model,
        "task": job.task,
        "instructor_attempts": int(getattr(exc, "n_attempts", 0) or 0),
        "provider_exception_type": type(last).__name__,
        "provider_status": getattr(last, "status_code", None),
        "input_characters": sum(len(str(message.get("content", ""))) for message in _messages(job)),
        "schema_characters": len(raw),
        "schema_has_defs": '"$defs"' in raw,
        "schema_has_refs": '"$ref"' in raw,
        "schema_has_any_of": '"anyOf"' in raw,
        "schema_has_defaults": '"default"' in raw,
    }


# Gemini's native schema-constrained structured output (responseJsonSchema) rejects these size/
# range keywords -- confirmed against the live API by citypods/llm_compat_probe.py's subtractive
# bisection: stripping exactly this key set was the only one (of defaults, additionalProperties,
# these constraints, enum, and a fully inlined $ref/$defs chain) that turned the R5 tag
# contract's real schema from a 400 into a 200. Every other construct Pydantic emits for a
# nested-model contract -- $defs/$ref, anyOf-nullable typing, default values,
# additionalProperties: false -- is fine.
_GEMINI_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"}
)


def _strip_schema_keys(node: Any, keys: frozenset[str]) -> Any:
    """Deep copy of a JSON Schema node with every occurrence of the given object keys removed."""
    if isinstance(node, dict):
        return {
            key: _strip_schema_keys(value, keys) for key, value in node.items() if key not in keys
        }
    if isinstance(node, list):
        return [_strip_schema_keys(item, keys) for item in node]
    return node


def _gemini_schema_safe_model(model: ResponseModel) -> ResponseModel:
    """A same-named subclass of `model` whose model_json_schema() drops the keywords Gemini's
    native mode rejects -- Instructor derives the request schema by calling this classmethod,
    with no supported hook to hand it an already-built schema instead, so this is the narrowest
    way to relax only what Gemini's request needs while still routing through Instructor.

    Response *validation* is unaffected: `model`'s fields and their min_length/max_length/ge/le
    constraints are inherited unchanged, so a reply that violates them still fails Pydantic
    validation locally (and still triggers Instructor's one corrective retry) exactly as before.
    Only Gemini's copy of the *request* schema loses server-side enforcement of this one keyword
    family. Built fresh per call rather than cached: it's one cheap class-creation call per
    structured Gemini request, not a hot path where that matters.
    """
    base_schema = model.model_json_schema.__func__

    def _relaxed_schema(cls: type, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = base_schema(cls, *args, **kwargs)
        return _strip_schema_keys(schema, _GEMINI_UNSUPPORTED_SCHEMA_KEYS)

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
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> LLMBackendConfig:
        """Build configuration from environment variables without reading provider keys.

        ``LLM_MODE`` is read as a plain string and passed through unvalidated here — an invalid
        value (anything but ``"direct"``/``"dispatch"``) surfaces as a clear ``ValueError`` from
        ``LiteLLMBackend.__init__``'s existing runtime check, which is the right place to reject
        it. The ``Literal`` annotation on ``mode`` documents the two valid values; it does not
        (and must not) silently coerce an invalid environment value into one of them.
        """
        return cls(
            model=os.environ.get("LLM_MODEL") or cls.model,
            mode=cast("Literal['direct', 'dispatch']", os.environ.get("LLM_MODE") or cls.mode),
            dispatch_url=os.environ.get("LLM_DISPATCH_URL"),
            dispatch_auth_token=os.environ.get("LLM_DISPATCH_AUTH_TOKEN"),
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


def _priced_actual(
    output: Mapping[str, Any], *, input_per_token: float, output_per_token: float
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
        if self.config.model not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported LLM model route: {self.config.model!r}")
        if self.config.mode not in {"direct", "dispatch"}:
            raise ValueError("LLM mode must be 'direct' or 'dispatch'")
        if self.config.mode == "dispatch" and not self.config.dispatch_url:
            raise ValueError("dispatch mode requires LLM_DISPATCH_URL")
        self._completion = completion
        self._session = http_session or requests.Session()
        self.storage = storage

    def _available_transports(self) -> frozenset[str]:
        """Which transports a policy-bearing call from *this instance* can reach right now.

        Independent of ``self.config.mode``, which only governs the legacy static-model path
        (``_run_without_policy``): ``direct`` needs nothing beyond a provider API key (already in
        env), so it's always reachable; ``mistral-dispatch`` needs a configured dispatch Worker.
        A backend built with both configured (as the deferred-request sweep's is) can select
        freely across every route regardless of which transport backs it.
        """
        transports = {"direct"}
        if self.config.dispatch_url:
            transports.add("mistral-dispatch")
        return frozenset(transports)

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
        """Choose Instructor's provider-neutral structured-output mode for this route."""
        try:
            from instructor import Mode
        except ImportError as exc:
            raise LLMBackendError("install the 'llm' extra to use structured LLM output") from exc
        # DeepSeek public chat supports only valid-JSON mode.  Instructor includes the Pydantic
        # schema in the prompt and supplies its field-specific validation feedback on a retry.
        # Gemini keeps native JSON_SCHEMA mode -- see _gemini_schema_safe_model() for why its
        # request schema needs one targeted adjustment first.
        return Mode.JSON if resolved_model.startswith("deepseek/") else Mode.JSON_SCHEMA

    def _structured_output_model(self, model: ResponseModel, resolved_model: str) -> ResponseModel:
        """The Pydantic contract Instructor should build a request schema from for this route."""
        return _gemini_schema_safe_model(model) if resolved_model.startswith("gemini/") else model

    def _provider_options(self, job: InferenceJob, resolved_model: str) -> dict[str, Any]:
        options: dict[str, Any] = {"model": resolved_model}
        for field in ("temperature", "max_tokens", "tools", "tool_choice"):
            if field in job.inputs:
                options[field] = job.inputs[field]
        return options

    def _dispatch_response_format(
        self, model: ResponseModel, resolved_model: str
    ) -> Mapping[str, Any]:
        """Serialize the same Pydantic contract used by Instructor for the R10 queue."""
        if resolved_model.startswith("deepseek/"):
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {"name": model.__name__, "schema": model.model_json_schema()},
        }

    def _payload(
        self,
        job: InferenceJob,
        model: ResponseModel | None = None,
        *,
        resolved_model: str,
    ) -> dict[str, Any]:
        """Build the provider-neutral OpenAI-shaped request sent by the dispatch transport."""
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": _messages(job),
            "stream": False,
        }
        if model is not None:
            payload["response_format"] = self._dispatch_response_format(model, resolved_model)
        payload.update(self._provider_options(job, resolved_model))
        return payload

    def _run_structured_direct(
        self,
        job: InferenceJob,
        model: ResponseModel,
        *,
        resolved_model: str,
        completion: Callable[..., Any] | None = None,
    ) -> JobResult:
        """Use Instructor for typed parsing and exactly one validation-feedback retry."""
        try:
            import instructor
            from instructor.core.exceptions import InstructorRetryException
        except ImportError as exc:
            raise LLMBackendError("install the 'llm' extra to use structured LLM output") from exc
        try:
            typed, raw = instructor.from_litellm(
                completion if completion is not None else self._completion_fn(),
                mode=self._instructor_mode(resolved_model),
            ).create_with_completion(
                response_model=self._structured_output_model(model, resolved_model),
                messages=_messages(job),
                max_retries=1,
                **self._provider_options(job, resolved_model),
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
            deferred_request=DeferredLLMRequest(messages=tuple(messages), policy=policy),
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
        if self.storage is None or not getattr(self.storage, "cas_capable", False):
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

        messages = _messages(job)
        result = self._run_policy_job(job, policy, structured, messages)
        # Cache the outcome either way: a completed result means a *later* call with the same
        # recipe_hash never has to pay for a real provider call again; a deferred handle means
        # the sweep (or a later call) can pick up exactly where this one left off.
        write_deferred(self.storage, job.recipe_hash, result)
        return result

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
        available_transports = self._available_transports()
        # Instructor's `max_retries=1` (see `_run_structured_direct`) can send up to two real
        # provider requests for one logical dispatch. Reserve the worst case up front so RPM/RPD/
        # TPM can never be breached even when both attempts happen; settling afterwards to the
        # single terminal response's actual usage only ever releases back what wasn't needed.
        max_provider_attempts = 2 if structured else 1
        per_attempt_tokens = estimate_tokens(messages) + DEFAULT_OUTPUT_TOKEN_MARGIN
        selection = select_and_reserve(
            self.storage,
            job.recipe_hash,
            policy,
            routes=ROUTES,
            available_transports=available_transports,
            estimated_tokens=per_attempt_tokens * max_provider_attempts,
            requests=max_provider_attempts,
        )
        if selection.model is None or selection.route is None:
            return self._deferred_handle(job, structured, messages, policy)
        resolved_model = selection.model
        route = selection.route
        owner = selection.owner
        assert owner is not None  # always set when a route was selected
        attempted = False

        def _cleanup() -> None:
            if attempted:
                # The call reached the provider (or the Worker's queue) regardless of outcome, so
                # its rate-limit slot is genuinely spent -- settle (keep it charged), never
                # release. See review/33 §10.2.
                settle_route_reservation(self.storage, owner, resolved_model, route=route)
            else:
                release_route_reservation(self.storage, owner, resolved_model, route=route)

        def _rate_limited(retry_after: float | None) -> JobHandle:
            until = datetime.now(UTC) + timedelta(seconds=retry_after or _DEFAULT_BLOCK_SECONDS)
            block_route_until(self.storage, resolved_model, until, route=route)
            settle_route_reservation(self.storage, owner, resolved_model, route=route)
            return self._deferred_handle(job, structured, messages, policy)

        try:
            if route.transport == "direct":
                if structured:
                    completion_fn = self._completion_fn()

                    def guarded_completion(**kwargs):
                        nonlocal attempted
                        attempted = True
                        return completion_fn(**kwargs)

                    result = self._run_structured_direct(
                        job,
                        structured[1],
                        resolved_model=resolved_model,
                        completion=guarded_completion,
                    )
                else:
                    payload = self._payload(job, resolved_model=resolved_model)
                    completion_fn = self._completion_fn()
                    attempted = True
                    response = completion_fn(**payload)
                    result = JobResult(
                        task=job.task,
                        recipe_hash=job.recipe_hash,
                        output=_response_mapping(response),
                        model=resolved_model,
                    )
            else:
                structured_name, model = structured if structured else (None, None)
                payload = self._payload(job, model, resolved_model=resolved_model)
                headers = {"content-type": "application/json"}
                if self.config.dispatch_auth_token:
                    headers["authorization"] = f"Bearer {self.config.dispatch_auth_token}"
                headers["idempotency-key"] = job.recipe_hash
                attempted = True
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
                    # leaves a stale inflight entry -- harmless today since no configured route
                    # declares `concurrency` (nothing checks `inflight_count`), but a real reap
                    # mechanism (mirroring H17's lease reap) would be needed before that changes.
                    return JobHandle(
                        task=job.task,
                        recipe_hash=job.recipe_hash,
                        backend=self.name,
                        ref=ref,
                        structured_output=structured_name,
                        model=resolved_model,
                        owner=owner,
                        input_per_token=route.pricing.input_per_token,
                        output_per_token=route.pricing.output_per_token,
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

        actual_tokens, actual_cost = _priced_actual(
            result.output,
            input_per_token=route.pricing.input_per_token,
            output_per_token=route.pricing.output_per_token,
        )
        settle_route_reservation(
            self.storage,
            owner,
            resolved_model,
            route=route,
            actual_tokens=actual_tokens,
            actual_cost=actual_cost,
        )
        return result

    def _reconcile_deferred(self, handle: JobHandle) -> JobResult | None:
        """Retry a deferred handle: re-run the same gates fresh, complete if eligible now, or
        return another deferred handle (persisted back to the registry either way)."""
        if self.storage is None or not getattr(self.storage, "cas_capable", False):
            raise LLMBackendError("LLM scheduler requires a CAS-capable storage backend")
        deferred = handle.deferred_request
        assert deferred is not None
        messages = [dict(message) for message in deferred.messages]
        inputs: dict[str, Any] = {"messages": messages}
        if handle.structured_output:
            inputs["structured_output"] = handle.structured_output
        job = InferenceJob(task=handle.task, inputs=inputs, recipe_hash=handle.recipe_hash)
        structured = self._response_model(job)
        result = self._run_policy_job(job, deferred.policy, structured, messages)
        write_deferred(self.storage, handle.recipe_hash, result)
        return None if isinstance(result, JobHandle) else result

    def _run_without_policy(
        self, job: InferenceJob, structured: tuple[str, ResponseModel] | None
    ) -> JobResult | JobHandle:
        """Preserve the pre-R13 request/response path exactly for callers without scheduler
        metadata (the returned ``model`` field is new, additive, and asserted by no pre-R13
        test)."""
        if self.config.mode == "direct":
            if structured:
                return self._run_structured_direct(
                    job, structured[1], resolved_model=self.config.model
                )
            response = self._completion_fn()(**self._payload(job, resolved_model=self.config.model))
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output=_response_mapping(response),
                model=self.config.model,
            )

        structured_name, model = structured if structured else (None, None)
        payload = self._payload(job, model, resolved_model=self.config.model)
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
                    model=self.config.model,
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
                model=self.config.model,
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch request failed") from exc

    def reconcile(self, handle: JobHandle) -> JobResult | None:
        """Return a validated result when ready, or ``None`` while still pending -- uniformly for
        a deferred-direct handle (re-runs selection) or a genuine Worker dispatch (polls it)."""
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
                return None
            if response.status_code != 200:
                raise LLMBackendError(f"LLM dispatch poll returned HTTP {response.status_code}")
            result = self._completed_dispatch_result(
                task=handle.task,
                recipe_hash=handle.recipe_hash,
                output=response.json(),
                structured_output=handle.structured_output,
                model=handle.model,
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch poll failed") from exc

        # A policy-tracked handle (§10.2/§10 in review/33) left its reservation inflight at
        # dispatch time specifically so it could be settled to *actual* usage here, once the
        # Worker's terminal response is available, instead of staying frozen at the estimate for
        # the job's entire lifetime. A handle from `_run_without_policy` has no `owner` and is a
        # no-op here, unchanged from the pre-R13 behavior.
        if handle.owner is not None and handle.model is not None and self.storage is not None:
            route = ROUTES.get(handle.model)
            if route is not None and getattr(self.storage, "cas_capable", False):
                # Price against the rate captured on the handle at reservation time, not
                # whatever `ROUTES` says right now -- config can change between a dispatch and
                # its eventual reconciliation. Fall back to an uncosted token-only settlement for
                # a handle that predates this field rather than guessing a rate.
                if handle.input_per_token is not None and handle.output_per_token is not None:
                    actual_tokens, actual_cost = _priced_actual(
                        result.output,
                        input_per_token=handle.input_per_token,
                        output_per_token=handle.output_per_token,
                    )
                else:
                    actual_tokens, actual_cost = _usage_tokens(result.output), None
                settle_route_reservation(
                    self.storage,
                    handle.owner,
                    handle.model,
                    route=route,
                    actual_tokens=actual_tokens,
                    actual_cost=actual_cost,
                )
                write_deferred(self.storage, handle.recipe_hash, result)
        return result

    poll = reconcile


__all__ = [
    "LLMBackendConfig",
    "LLMBackendError",
    "LLMStructuredOutputError",
    "LLM_TASKS",
    "LiteLLMBackend",
    "SUPPORTED_MODELS",
    "TASK_PROMPTS",
    "TASK_VERSIONS",
]
