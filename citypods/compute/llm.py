"""LiteLLM routing plus Instructor/Pydantic structured outputs for reserved LLM verbs.

Direct calls use Instructor for provider-mode selection, parsing, Pydantic validation, and one
bounded corrective retry.  The R10 Worker remains an asynchronous transport: it durably stores the
Pydantic-generated response format, and reconciliation validates the completed reply locally.  A
future queue-owned corrective retry can be added without changing a task's response contract.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from citypods.compute.base import Backend, InferenceJob, JobHandle, JobResult, Task
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


@dataclass(frozen=True)
class LLMBackendConfig:
    """Runtime routing configuration; secrets are read from the environment, never persisted."""

    model: str = "gemini/gemini-3-flash-preview"
    mode: str = "direct"  # ``direct`` or ``dispatch``
    dispatch_url: str | None = None
    dispatch_auth_token: str | None = None
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> LLMBackendConfig:
        """Build configuration from environment variables without reading provider keys."""
        return cls(
            model=os.environ.get("LLM_MODEL") or cls.model,
            mode=os.environ.get("LLM_MODE") or cls.mode,
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

    def _instructor_mode(self):
        """Choose Instructor's provider-neutral structured-output mode for this route."""
        try:
            from instructor import Mode
        except ImportError as exc:
            raise LLMBackendError("install the 'llm' extra to use structured LLM output") from exc
        # DeepSeek public chat supports only valid-JSON mode.  Instructor includes the Pydantic
        # schema in the prompt and supplies its field-specific validation feedback on a retry.
        return Mode.JSON if self.config.model.startswith("deepseek/") else Mode.JSON_SCHEMA

    def _provider_options(self, job: InferenceJob) -> dict[str, Any]:
        options: dict[str, Any] = {"model": self.config.model}
        for field in ("temperature", "max_tokens", "tools", "tool_choice"):
            if field in job.inputs:
                options[field] = job.inputs[field]
        return options

    def _dispatch_response_format(self, model: ResponseModel) -> Mapping[str, Any]:
        """Serialize the same Pydantic contract used by Instructor for the R10 queue."""
        if self.config.model.startswith("deepseek/"):
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {"name": model.__name__, "schema": model.model_json_schema()},
        }

    def _payload(self, job: InferenceJob, model: ResponseModel | None = None) -> dict[str, Any]:
        """Build the provider-neutral OpenAI-shaped request sent by the dispatch transport."""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": _messages(job),
            "stream": False,
        }
        if model is not None:
            payload["response_format"] = self._dispatch_response_format(model)
        payload.update(self._provider_options(job))
        return payload

    def _run_structured_direct(self, job: InferenceJob, model: ResponseModel) -> JobResult:
        """Use Instructor for typed parsing and exactly one validation-feedback retry."""
        try:
            import instructor
            from instructor.core.exceptions import InstructorRetryException
        except ImportError as exc:
            raise LLMBackendError("install the 'llm' extra to use structured LLM output") from exc
        try:
            typed, raw = instructor.from_litellm(
                self._completion_fn(), mode=self._instructor_mode()
            ).create_with_completion(
                response_model=model,
                messages=_messages(job),
                max_retries=1,
                **self._provider_options(job),
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
        return JobResult(task=job.task, recipe_hash=job.recipe_hash, output=_response_mapping(raw))

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
    ) -> JobResult:
        """Validate and normalize a terminal Worker response from either POST or poll."""
        if not isinstance(output, Mapping):
            raise LLMBackendError("LLM dispatch returned a non-object response")
        self._validate_reconciled(output, structured_output)
        return JobResult(task=task, recipe_hash=recipe_hash, output=output)

    def run_inference(self, job: InferenceJob) -> JobResult | JobHandle:
        """Run directly through LiteLLM or enqueue through the asynchronous dispatch Worker."""
        if job.task not in LLM_TASKS:
            raise ValueError(f"LiteLLM backend does not handle task {job.task!r}")
        structured = self._response_model(job)
        if self.config.mode == "direct":
            if structured:
                return self._run_structured_direct(job, structured[1])
            response = self._completion_fn()(**self._payload(job))
            return JobResult(
                task=job.task, recipe_hash=job.recipe_hash, output=_response_mapping(response)
            )

        structured_name, model = structured if structured else (None, None)
        payload = self._payload(job, model)
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
            return self._completed_dispatch_result(
                task=handle.task,
                recipe_hash=handle.recipe_hash,
                output=response.json(),
                structured_output=handle.structured_output,
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch poll failed") from exc

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
