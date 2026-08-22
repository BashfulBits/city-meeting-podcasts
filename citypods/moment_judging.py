"""R6 judge configuration kept separate from candidate generation."""

from __future__ import annotations

from collections.abc import Mapping

from citypods.compute.llm_policy import LLMRequestPolicy

JUDGE_MODELS = (
    "meta-llama/llama-4-maverick",
    "zai/glm-4.7",
    "gemini/gemini-3.6-flash",
    "openai/gpt-oss-120b",
)
JUDGE_PROMPT_VERSION = "1"
JUDGE_SCHEMA_VERSION = "1"


def judge_models(configured: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Return configured judge routes without inventing a paid fallback."""
    if configured is None:
        return JUDGE_MODELS
    return tuple(model for model in JUDGE_MODELS if model in configured)


def judge_policy(configured: list[str] | tuple[str, ...] | None = None) -> LLMRequestPolicy:
    return LLMRequestPolicy(
        allowed_models=judge_models(configured),
        allow_paid=False,
        purpose="r6-judge",
        queue_only=True,
        timeout_class="long",
    )


def judge_input(candidate: Mapping[str, object]) -> dict[str, object]:
    """Build immutable judge evidence; no generated candidate fields are accepted here."""
    return {
        "candidate_id": candidate.get("candidate_id"),
        "quote": candidate.get("quote"),
        "transcript_range": {
            "start": candidate.get("start"),
            "end": candidate.get("end"),
        },
        "scores": [
            "grounding",
            "quote_usefulness",
            "factual_faithfulness",
            "timing_plausibility",
            "publication_readiness",
        ],
        "prompt_version": JUDGE_PROMPT_VERSION,
        "schema_version": JUDGE_SCHEMA_VERSION,
    }


__all__ = [
    "JUDGE_MODELS",
    "JUDGE_PROMPT_VERSION",
    "JUDGE_SCHEMA_VERSION",
    "judge_input",
    "judge_models",
    "judge_policy",
]
