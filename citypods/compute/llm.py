"""LiteLLM routing plus Instructor/Pydantic structured outputs for reserved LLM verbs.

Direct calls use Instructor for provider-mode selection, parsing, Pydantic validation, and one
bounded corrective retry.  The R10 Worker remains an asynchronous transport: it durably stores the
Pydantic-generated response format, and reconciliation validates the completed reply locally.  A
future queue-owned corrective retry can be added without changing a task's response contract.
"""

from __future__ import annotations

import importlib
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urljoin, urlsplit

import requests

from citypods.compute.base import Backend, InferenceJob, JobHandle, JobResult, Task
from citypods.compute.llm_budget import release_route_reservation, settle_route_reservation
from citypods.compute.llm_policy import (
    DEFAULT_OUTPUT_TOKEN_MARGIN,
    ROUTES,
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
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "mistral/mistral-large-latest",
        "mistral/mistral-large-3",
    }
)


class LLMBackendError(RuntimeError):
    """A safe, provider-agnostic adapter error (provider response bodies are not exposed)."""


class LLMStructuredOutputError(LLMBackendError):
    """A malformed model reply that a caller may safely defer and retry with fresh evidence."""


class LLMNotEligibleError(LLMBackendError):
    """No configured route is eligible; the caller can retry on its next scheduled run."""


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
        return Mode.JSON if resolved_model.startswith("deepseek/") else Mode.JSON_SCHEMA

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
                response_model=model,
                messages=_messages(job),
                max_retries=1,
                **self._provider_options(job, resolved_model),
            )
        except InstructorRetryException:
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

        messages = _messages(job)
        # Owner uniqueness depends on transport, not a single rule for both:
        # - dispatch: the Worker dedupes on `idempotency-key: job.recipe_hash` (below), so a
        #   retry before this reservation settles is the *same* underlying provider request --
        #   owner must be that same deterministic recipe_hash, or it would double-reserve quota
        #   for a call the Worker itself never repeats. That guarantee requires a real recipe_hash
        #   (an empty one would let unrelated jobs collide under the same owner).
        # - direct: there is no server-side dedup at all. A retry or a concurrent call genuinely
        #   sends a second real request, so it must reserve its own, independent slot -- a unique
        #   owner per invocation, same as before R13.
        if self.config.mode == "dispatch":
            if not job.recipe_hash:
                raise LLMBackendError("dispatch-mode LLM jobs require a non-empty recipe_hash")
            owner = job.recipe_hash
        else:
            owner = f"{job.recipe_hash}:{uuid.uuid4().hex}"
        # Instructor's `max_retries=1` (see `_run_structured_direct`) can send up to two real
        # provider requests for one logical dispatch. Reserve the worst case up front so RPM/RPD/
        # TPM can never be breached even when both attempts happen; settling afterwards to the
        # single terminal response's actual usage only ever releases back what wasn't needed.
        max_provider_attempts = 2 if structured else 1
        per_attempt_tokens = estimate_tokens(messages) + DEFAULT_OUTPUT_TOKEN_MARGIN
        selection = select_and_reserve(
            self.storage,
            owner,
            policy,
            routes=ROUTES,
            backend_mode=self.config.mode,
            estimated_tokens=per_attempt_tokens * max_provider_attempts,
            requests=max_provider_attempts,
        )
        if selection.model is None or selection.route is None:
            raise LLMNotEligibleError(selection.reason)
        resolved_model = selection.model
        route = selection.route
        attempted = False

        def _cleanup() -> None:
            if attempted:
                # The call reached the provider (or the Worker's queue) regardless of outcome, so
                # its rate-limit slot is genuinely spent -- settle (keep it charged), never
                # release. See review/33 §10.2.
                settle_route_reservation(self.storage, owner, resolved_model, route=route)
            else:
                release_route_reservation(self.storage, owner, resolved_model, route=route)

        try:
            if self.config.mode == "direct":
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
                if job.recipe_hash:
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
                else:
                    raise LLMBackendError(f"LLM dispatch returned HTTP {response.status_code}")
        except requests.RequestException as exc:
            _cleanup()
            raise LLMBackendError("LLM request failed") from exc
        except BaseException:
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
        """Return a validated result when ready, or ``None`` while the Worker request is pending."""
        if handle.backend != self.name:
            raise ValueError(f"cannot reconcile handle for backend {handle.backend!r}")
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
        return result

    poll = reconcile


__all__ = [
    "LLMBackendConfig",
    "LLMBackendError",
    "LLMNotEligibleError",
    "LLMStructuredOutputError",
    "LLM_TASKS",
    "LiteLLMBackend",
    "SUPPORTED_MODELS",
    "TASK_PROMPTS",
    "TASK_VERSIONS",
]
