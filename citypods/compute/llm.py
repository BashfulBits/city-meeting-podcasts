"""LiteLLM-backed execution for the reserved LLM task verbs.

The adapter deliberately has two transports: direct LiteLLM completion, and the
asynchronous R10 dispatch Worker.  Both transports return the same ``JobResult``
shape; the Worker path returns a ``JobHandle`` until its request is ready.
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
from citypods.security import SecurityError, validate_source_url

LLM_TASKS: frozenset[Task] = frozenset({"summarize", "tag", "soundbite-select"})
TASK_VERSIONS: dict[Task, str] = {
    "summarize": "1",
    "tag": "1",
    "soundbite-select": "1",
}

# Shared structured prompts are intentionally provider-neutral.  Calling stages may supply a
# complete ``messages`` list when they need a task-specific prompt; these are only defaults.
TASK_PROMPTS: dict[Task, str] = {
    "summarize": "Summarize the supplied meeting material accurately and concisely.",
    "tag": "Extract a small list of factual topic tags from the supplied meeting material.",
    "soundbite-select": (
        "Select the strongest bounded soundbite candidates from the supplied material."
    ),
}

SUPPORTED_MODELS = frozenset(
    {
        "gemini/gemini-3-flash-preview",
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "mistral/mistral-large-latest",
        "mistral/mistral-large-3",  # compatibility with earlier Worker configurations
    }
)


class LLMBackendError(RuntimeError):
    """A safe, provider-agnostic adapter error (provider response bodies are not exposed)."""


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
            model=os.environ.get("LLM_MODEL", cls.model),
            mode=os.environ.get("LLM_MODE", cls.mode),
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
    prompt = TASK_PROMPTS[job.task]
    return [
        {"role": "system", "content": prompt},
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

    def _payload(self, job: InferenceJob) -> dict[str, Any]:
        """Build the provider-neutral OpenAI-shaped request sent by either transport."""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": _messages(job),
            "stream": False,
        }
        for field in ("response_format", "temperature", "max_tokens", "tools", "tool_choice"):
            if field in job.inputs:
                payload[field] = job.inputs[field]
        return payload

    def run_inference(self, job: InferenceJob) -> JobResult | JobHandle:
        """Run directly through LiteLLM or enqueue through the asynchronous dispatch Worker."""
        if job.task not in LLM_TASKS:
            raise ValueError(f"LiteLLM backend does not handle task {job.task!r}")
        payload = self._payload(job)
        if self.config.mode == "direct":
            response = self._completion_fn()(**payload)
            return JobResult(
                task=job.task,
                recipe_hash=job.recipe_hash,
                output=_response_mapping(response),
            )

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
            if response.status_code != 202:
                raise LLMBackendError(f"LLM dispatch returned HTTP {response.status_code}")
            body = response.json()
            ref = response.headers.get("location")
            if not ref and isinstance(body, Mapping):
                ref = body.get("id")
            if not ref:
                raise LLMBackendError("LLM dispatch response omitted a request reference")
            return JobHandle(task=job.task, recipe_hash=job.recipe_hash, backend=self.name, ref=ref)
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch request failed") from exc

    def reconcile(self, handle: JobHandle) -> JobResult | None:
        """Return a result when ready, or ``None`` while the Worker request is pending."""
        if handle.backend != self.name:
            raise ValueError(f"cannot reconcile handle for backend {handle.backend!r}")
        ref = handle.ref
        base = self.config.dispatch_url.rstrip("/") + "/"
        base_parts = urlsplit(base)
        if ref.startswith("http"):
            candidate = ref
            candidate_parts = urlsplit(candidate)
            if (candidate_parts.scheme, candidate_parts.netloc) != (
                base_parts.scheme,
                base_parts.netloc,
            ):
                raise LLMBackendError("LLM dispatch location points to an unexpected host")
        elif ref.startswith("/"):
            candidate = urljoin(base, ref.lstrip("/"))
        else:
            candidate = urljoin(base, f"v1/requests/{ref}")
        try:
            validate_source_url(
                candidate,
                allowed_hosts=(base_parts.hostname or "",),
                resolve=False,
            )
        except SecurityError as exc:
            raise LLMBackendError("LLM dispatch location is not an allowed HTTPS URL") from exc
        url = candidate
        headers = {}
        if self.config.dispatch_auth_token:
            headers["authorization"] = f"Bearer {self.config.dispatch_auth_token}"
        try:
            response = self._session.get(url, headers=headers, timeout=self.config.timeout_seconds)
            if response.status_code == 202:
                return None
            if response.status_code != 200:
                raise LLMBackendError(f"LLM dispatch poll returned HTTP {response.status_code}")
            return JobResult(
                task=handle.task,
                recipe_hash=handle.recipe_hash,
                output=response.json(),
            )
        except requests.RequestException as exc:
            raise LLMBackendError("LLM dispatch poll failed") from exc

    poll = reconcile


__all__ = [
    "LLMBackendConfig",
    "LLMBackendError",
    "LLM_TASKS",
    "LiteLLMBackend",
    "SUPPORTED_MODELS",
    "TASK_PROMPTS",
    "TASK_VERSIONS",
]
