"""Constrained LLM classification for retrieved civic-platform evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from citypods.compute.base import Backend, InferenceJob, JobHandle, JobResult
from citypods.compute.llm import TASK_VERSIONS
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.compute.structured import register_response_model
from citypods.discovery.models import (
    KNOWN_PLATFORMS,
    Classification,
    DiscoveryRequest,
    SearchResult,
)


class ClassificationError(RuntimeError):
    """The classifier result was malformed or did not complete synchronously."""


class ClassificationDeferred(ClassificationError):
    """A queued classification that will be collected by a later scheduled discovery run."""


PlatformName = Enum(
    "PlatformName",
    {platform.replace("-", "_").upper(): platform for platform in sorted(KNOWN_PLATFORMS)},
    type=str,
)


class PlatformSource(BaseModel):
    """Closed, provider-neutral source slots; post-LLM verification supplies their semantics."""

    model_config = ConfigDict(extra="forbid")

    feed_url: str | None
    list_url: str | None
    api_base: str | None
    portal_url: str | None
    calendar_url: str | None
    granicus_base: str | None
    backfill_since: str | None
    agenda_url: str | None
    minutes_url: str | None
    body: str | None


class CivicPlatformClassificationResponse(BaseModel):
    """Pydantic contract used by Instructor and by queued-result reconciliation."""

    model_config = ConfigDict(extra="forbid")

    city_identity: Literal["confirmed", "unconfirmed", "mismatch"]
    video_platform: PlatformName | None
    agenda_platform: PlatformName | None
    candidate_urls: list[str]
    video_source: PlatformSource | None
    agenda_source: PlatformSource | None
    bodies_mentioned: list[str]
    confidence: Literal["low", "medium", "high"]
    reasoning: str


STRUCTURED_OUTPUT = "civic-platform-classification"
register_response_model(STRUCTURED_OUTPUT, CivicPlatformClassificationResponse)


def _evidence(results: list[SearchResult]) -> list[dict[str, str]]:
    return [
        {"url": row.url, "title": row.title[:300], "content": row.content[:2000]} for row in results
    ]


def recipe_hash(request: DiscoveryRequest, results: list[SearchResult]) -> str:
    payload = {
        "task_version": TASK_VERSIONS["classify-civic-platforms"],
        "prompt": _prompt(request, results),
        "response_model": CivicPlatformClassificationResponse.model_json_schema(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _prompt(request: DiscoveryRequest, results: list[SearchResult]) -> list[dict[str, str]]:
    context = {
        "mode": request.mode,
        "city": f"{request.city_name}, {request.state}",
        "known_video_provider": request.known_provider,
        "hints_to_verify": {
            "city_website": request.city_website,
            "meeting_url": request.meeting_url_hint,
            "provider": request.provider_hint,
            "notes": request.notes,
        },
        "retrieved_results": _evidence(results),
    }
    source_schemas = {
        "granicus": {"feed_url": "retrieved full ViewPublisherRSS.php URL"},
        "swagit": {"list_url": "retrieved public archive/list page URL"},
        "civicplus": {"feed_url": "retrieved RSSFeed.aspx URL"},
        "civicclerk": {"api_base": "retrieved https://<tenant>.api.civicclerk.com URL"},
        "onemeeting": {"portal_url": "retrieved https://.../public/portal URL"},
        "legistar": {
            "calendar_url": "retrieved Calendar.aspx URL",
            "granicus_base": "retrieved Granicus origin/base URL",
            "backfill_since": "ISO date only when evidence supplies an explicit historic boundary",
        },
        "civicengage": {
            "agenda_url": "retrieved official agenda archive URL",
            "minutes_url": "retrieved official minutes archive URL",
            "body": "body name stated in retrieved evidence",
        },
    }
    response_fields = (
        "city_identity (confirmed|unconfirmed|mismatch), video_platform, agenda_platform, "
        "candidate_urls, video_source, agenda_source, "
        "bodies_mentioned, confidence (low|medium|high), and reasoning"
    )
    identity_instruction = ""
    if request.mode == "new-city":
        identity_instruction = (
            " For new-city mode, city_identity is confirmed only when retrieved evidence "
            "identifies the requested city and state. Set it to mismatch when results identify "
            "another municipality, and to unconfirmed when the retrieved evidence cannot "
            "establish the requested municipality. When identity is mismatch or unconfirmed, "
            "set both platforms to null and return no candidate URLs, source mappings, or bodies."
        )
    else:
        identity_instruction = " In auxiliary mode, set city_identity to confirmed."
    instruction = (
        "Classify civic video and agenda platforms only from retrieved_results. "
        "Do not invent URLs, and only put a URL in candidate_urls when it appears exactly "
        "in retrieved_results. "
        "Use a platform key from this allowlist or null: "
        f"{', '.join(sorted(KNOWN_PLATFORMS))}. In auxiliary mode, copy "
        "known_video_provider to "
        f"video_platform unchanged. Return one JSON object with {response_fields}. "
        "source fields must follow these provider schemas: "
        + json.dumps(source_schemas, sort_keys=True)
        + ". A source mapping is optional; use null, or include every source key with null for "
        "unknown values, rather than guessing. "
        "Every URL "
        "inside a source mapping must appear exactly in retrieved_results." + identity_instruction
    )
    return [
        {"role": "system", "content": instruction},
        {"role": "user", "content": json.dumps(context)},
    ]


def _content(output: Any) -> str:
    if isinstance(output, Mapping):
        choices = output.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            message = choices[0].get("message")
            if isinstance(message, Mapping) and isinstance(message.get("content"), str):
                return message["content"]
    raise ClassificationError("LLM classification response did not contain message content")


def _json_object(value: str) -> Mapping[str, Any]:
    value = value.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ClassificationError("LLM classification response was not valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise ClassificationError("LLM classification response must be a JSON object")
    return decoded


def _platform(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in KNOWN_PLATFORMS:
        return None
    return value


def _strings(value: Any, *, allowed: set[str] | None = None, limit: int = 20) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if (
            isinstance(item, str)
            and item
            and (allowed is None or item in allowed)
            and item not in out
        ):
            out.append(item)
        if len(out) >= limit:
            break
    return tuple(out)


def _source(value: Any, *, retrieved_urls: set[str]) -> dict[str, Any] | None:
    """Keep only a shallow JSON source mapping whose URLs are grounded in search evidence."""
    if not isinstance(value, Mapping):
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            return None
        if item is None:
            continue
        if not isinstance(item, (str, int, float, bool)):
            return None
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            if item not in retrieved_urls:
                return None
        out[key] = item
    return out or None


def parse_classification(
    output: Any, request: DiscoveryRequest, results: list[SearchResult]
) -> Classification:
    data = _json_object(_content(output))
    retrieved_urls = {result.url for result in results}
    video = _platform(data.get("video_platform"))
    if request.mode == "auxiliary":
        video = request.known_provider
    confidence = data.get("confidence")
    city_identity = data.get("city_identity")
    if request.mode == "auxiliary":
        city_identity = "confirmed"
    elif city_identity not in {"confirmed", "unconfirmed", "mismatch"}:
        city_identity = "unconfirmed"
    if request.mode == "new-city" and city_identity != "confirmed":
        # Foreign or ambiguous retrieval must not become a provider/backlog signal merely because
        # the model classified the unrelated page accurately.
        return Classification(
            video_platform=None,
            agenda_platform=None,
            candidate_urls=(),
            video_source=None,
            agenda_source=None,
            bodies_mentioned=(),
            city_identity=city_identity,
            confidence="low",
            reasoning=str(data.get("reasoning", ""))[:500],
        )
    return Classification(
        video_platform=video,
        agenda_platform=_platform(data.get("agenda_platform")),
        candidate_urls=_strings(data.get("candidate_urls"), allowed=retrieved_urls),
        video_source=_source(data.get("video_source"), retrieved_urls=retrieved_urls),
        agenda_source=_source(data.get("agenda_source"), retrieved_urls=retrieved_urls),
        bodies_mentioned=_strings(data.get("bodies_mentioned"), limit=50),
        city_identity=city_identity,
        confidence=confidence if confidence in {"low", "medium", "high"} else "low",
        reasoning=str(data.get("reasoning", ""))[:500],
    )


def classify(
    backend: Backend, request: DiscoveryRequest, results: list[SearchResult]
) -> Classification:
    """Classify evidence through the backend's Instructor/Pydantic output contract.

    City discovery acts on the result immediately (continuing an issue-comment cycle) rather than
    tolerating a later async completion, so this asks only for a *free* route -- `allow_paid=False`
    -- with no `deadline_at`: there is nothing to wait out or fall back to paid for.
    `require_direct=True` is explicit and load-bearing, not redundant with the default: the
    Worker's dispatch transport is *always* asynchronous (a 202 plus a later poll, by design --
    review/27 §9.3), so a policy that let this call dispatch could never complete within this same
    process even when the target route has ample quota. A prior version of this code relied on
    `allow_dispatch_overflow` defaulting to False rather than stating the requirement here
    directly; that left the workflow's `LLM_DISPATCH_URL` env var (set for a different reason) free
    to silently flip this call onto the Worker the moment a future change made overflow opt-in the
    default somewhere upstream. Stating `require_direct=True` here means this call can never
    dispatch regardless of what the backend's transport configuration or global defaults do. If
    nothing free is eligible right now (today that's just Gemini; a future free+direct route
    would also qualify with no code change here), `run_inference` returns a `JobHandle` the same
    as it would for a genuinely in-flight dispatch -- this defers the *whole* discovery cycle to
    the next scheduled run, exactly as it already did before R13.
    """
    job = InferenceJob(
        task="classify-civic-platforms",
        inputs={
            "messages": _prompt(request, results),
            "structured_output": STRUCTURED_OUTPUT,
            "llm_policy": LLMRequestPolicy(
                allow_paid=False,
                require_direct=True,
                purpose="city-onboarding",
                timeout_class="fast",
            ),
        },
        recipe_hash=recipe_hash(request, results),
    )
    outcome = backend.run_inference(job)
    if isinstance(outcome, JobHandle):
        raise ClassificationDeferred("classification is queued; retry it on the next discovery run")
    if not isinstance(outcome, JobResult):
        raise ClassificationError("classification backend returned an unsupported result")
    return parse_classification(outcome.output, request, results)
