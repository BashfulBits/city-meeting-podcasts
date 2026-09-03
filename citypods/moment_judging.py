"""R6 judge configuration kept separate from candidate generation."""

from __future__ import annotations

from collections.abc import Mapping

from citypods.compute.llm_lanes import lane_for
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.compute.structured import register_response_model, response_model

# Routes come from `llm_lanes["r6-judge"]` (review/44 Phase 4), the same block the ingress
# Worker's reservation map is compiled from. `judge_models()` below still intersects this with a
# caller-supplied allowlist, so a deployment can narrow the panel without widening it.
JUDGE_MODELS = lane_for("r6-judge").models
JUDGE_PROMPT_VERSION = "1"
JUDGE_SCHEMA_VERSION = "1"
JUDGE_CONTRACT = "moment-judge"


def ensure_judge_contract():
    """Register the judge-only response schema without giving it candidate authority."""
    cached = getattr(ensure_judge_contract, "model", None)
    if cached is not None:
        return cached

    try:
        model = response_model(JUDGE_CONTRACT)
        ensure_judge_contract.model = model
        return model
    except ValueError:
        pass

    from pydantic import BaseModel, ConfigDict, Field

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")
        grounding: float = Field(ge=0.0, le=1.0)
        quote_usefulness: float = Field(ge=0.0, le=1.0)
        factual_faithfulness: float = Field(ge=0.0, le=1.0)
        timing_plausibility: float = Field(ge=0.0, le=1.0)
        publication_readiness: float = Field(ge=0.0, le=1.0)
        admission_score: float = Field(ge=0.0, le=1.0)
        rationale: str = Field(default="", max_length=400)

    model = register_response_model(JUDGE_CONTRACT, Response)
    ensure_judge_contract.model = model
    return model


def judge_models(configured: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Return the judge routes to use, without inventing a paid fallback.

    ``configured`` is an optional narrowing allowlist, not the source of the routes: the panel
    itself is ``llm_lanes["r6-judge"]``. An absent OR EMPTY allowlist therefore means "the whole
    configured panel", not "no judges at all" -- the empty case matters because
    ``MomentJudgeStage`` reads ``judges.models`` from site config, which no longer carries a list.
    Treating empty as an empty intersection would silently disable judging with no error, which is
    precisely the class of quiet failure the lane registry exists to remove.
    """
    if not configured:
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


def judge_input(candidate: Mapping[str, object], transcript: str) -> dict[str, object]:
    """Build immutable judge evidence; no generated candidate fields are accepted here."""
    return {
        "candidate_id": candidate.get("candidate_id"),
        "quote": candidate.get("quote"),
        "transcript_evidence": transcript,
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
    "JUDGE_CONTRACT",
    "JUDGE_PROMPT_VERSION",
    "JUDGE_SCHEMA_VERSION",
    "judge_input",
    "ensure_judge_contract",
    "judge_models",
    "judge_policy",
]
