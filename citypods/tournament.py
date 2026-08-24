"""Bounded, durable three-model pairwise tournament for LLM topic tags.

The runner deliberately records comparison evidence only.  It never changes the production
champion; a later human-review ticket is the sole routing authority (review/34).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import (
    LiteLLMBackend,
    LLMBackendConfig,
    LLMBackendError,
    dispatch_job_batch,
)
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.compute.structured import register_response_model
from citypods.config import load_city_configs, load_site_config
from citypods.records import load_records, record_to_episode, source_key
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage
from citypods.tags import (
    chapter_tag_inputs,
    episode_tag_inputs,  # noqa: F401 — compatibility hook; R5 run itself is chapter-only
    llm_tag_suggestions,
    load_taxonomy,
)

MODELS = (
    "gemini/gemini-3.1-flash-lite",
    "deepseek/deepseek-v4-flash",
    # Hotfix follow-up (Mistral's account-wide monthly budget is exhausted, see
    # config/provider_limits.yml): mistral/mistral-large-2512 replaced with zai/glm-4.7-flash,
    # plus google/gemma-4-26b-a4b-it added as a 4th contestant. Both are free routes.
    "zai/glm-4.7-flash",
    "google/gemma-4-26b-a4b-it",
)
JUDGE_MODEL = "google/gemma-4-31b-it"
CONTESTS = (
    ("deepseek/deepseek-v4-flash", "gemini/gemini-3.1-flash-lite", JUDGE_MODEL),
    ("deepseek/deepseek-v4-flash", "zai/glm-4.7-flash", JUDGE_MODEL),
    ("deepseek/deepseek-v4-flash", "google/gemma-4-26b-a4b-it", JUDGE_MODEL),
    ("gemini/gemini-3.1-flash-lite", "zai/glm-4.7-flash", JUDGE_MODEL),
    ("gemini/gemini-3.1-flash-lite", "google/gemma-4-26b-a4b-it", JUDGE_MODEL),
    ("zai/glm-4.7-flash", "google/gemma-4-26b-a4b-it", JUDGE_MODEL),
)
STATE = "llm_tournament.json"
JUDGE_CONTRACT = "tournament-tag-judge"
R5_FLASH_MODEL = "litellm:gemini/gemini-3.1-flash-lite"


def contest_plan() -> tuple[tuple[str, str, str], ...]:
    """The immutable 4-way round robin (6 pairs); a judge is never a contestant."""
    return CONTESTS


def persisted_r5_flash_output(
    record: dict[str, Any], chapter_id: str | None = None
) -> list[dict[str, Any]] | None:
    """Return the current R5 Flash-Lite shadow output when it is safe to reuse.

    ``llm_tag_candidates`` is deliberately shadow-only, but its per-candidate recipe/model
    provenance is exactly what the tournament needs. Empty output is not reusable because older
    record shapes cannot identify which model produced an empty list; spending one bounded call is
    safer than assigning that ambiguity to Flash-Lite.
    """
    candidates = record.get("llm_tag_candidates")
    recipe = record.get("tags_llm_recipe_hash")
    if not isinstance(candidates, list) or not candidates or not isinstance(recipe, str):
        return None
    if not all(isinstance(item, dict) for item in candidates):
        return None
    filtered = [
        item
        for item in candidates
        if item.get("source_kind", "llm") != "rule"
        and (chapter_id is None or item.get("chapter_id") == chapter_id)
    ]
    if not filtered:
        return None
    if all(
        item.get("provider_model") == R5_FLASH_MODEL and item.get("recipe_hash") == recipe
        for item in filtered
    ):
        return filtered
    return None


@dataclass(frozen=True)
class PairwiseEvaluatorSpec:
    """Task-agnostic pairwise comparison contract shared by R5 and future R6 evaluators."""

    task: str
    purpose: str
    contract: str = JUDGE_CONTRACT
    criteria: str = "Compare source support, omissions, overreach, evidence quality, and fit."


@dataclass(frozen=True)
class _PendingComparison:
    """One comparison awaiting batched dispatch in `run()` -- everything the result-processing
    pass needs to know once `dispatch_job_batch` returns, keyed back to its slot in `decisions`
    and its `comparison_store` entry."""

    slot: int
    comparison_key: str
    job: InferenceJob
    left: str
    right: str
    judge: str
    first: str


def blind_candidate(candidate: Any, *, fields: tuple[str, ...] | None = None) -> Any:
    """Remove route/recipe provenance before presenting a candidate to an independent judge."""
    if not isinstance(candidate, dict):
        return candidate
    fields = fields or ("id", "chapter_id", "source", "confidence", "explanation", "evidence")
    return {field: candidate[field] for field in fields if field in candidate}


def judge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """R5 compatibility wrapper for the generic blind-candidate projection."""
    return [blind_candidate(item) for item in candidates]


def order_swapped_pairs(left: str, right: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Return both presentation orders so positional judge bias can be measured."""
    return (left, right), (right, left)


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(body).hexdigest()


def comparison_id(
    *,
    run_id: str,
    task: str,
    subject_id: str,
    first_model: str,
    second_model: str,
    judge_model: str,
) -> str:
    """Stable durable identity for an R5 chapter or future R6 freeform comparison."""
    return _digest(
        {
            "run_id": run_id,
            "task": task,
            "subject_id": subject_id,
            "first_model": first_model,
            "second_model": second_model,
            "judge_model": judge_model,
        }
    )


def _judge_model(contract: str = JUDGE_CONTRACT):
    from pydantic import BaseModel, ConfigDict, Field

    class Decision(BaseModel):
        model_config = ConfigDict(extra="forbid")
        winner: str = Field(pattern="^(a|b|tie|both_poor)$")
        rationale: str = Field(min_length=1, max_length=500)

    models = getattr(_judge_model, "models", {})
    model = models.get(contract)
    if model is None:
        model = register_response_model(contract, Decision)
        models[contract] = model
        _judge_model.models = models
    return model


def _build_pairwise_judge_job(
    *,
    spec: PairwiseEvaluatorSpec,
    source: Any,
    candidate_a: Any,
    candidate_b: Any,
    judge_model: str,
    recipe_hash: str,
    allow_paid: bool = True,
    candidate_models: tuple[str, ...] = (),
) -> InferenceJob:
    """Build one blinded pairwise-judgment InferenceJob without dispatching it -- shared by
    `pairwise_judge` (the single-job entry point) and `run()`'s batched judge dispatch (see
    review/44's 2026-08-18 incident retrospective: no caller ever batched more than one job per
    call before this, which was exactly the per-comparison Worker-request volume that incident
    flagged as worth fixing next)."""
    if judge_model in set(candidate_models):
        raise ValueError("pairwise judge route must be independent from candidate routes")
    _judge_model(spec.contract)
    return InferenceJob(
        task=spec.task,
        recipe_hash=recipe_hash,
        inputs={
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"Compare candidate A and candidate B for {spec.task}. "
                        f"{spec.criteria} Return a, b, tie, or both_poor with a short rationale."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source": source,
                            "candidate_a": candidate_a,
                            "candidate_b": candidate_b,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "structured_output": spec.contract,
            "llm_policy": LLMRequestPolicy(
                allowed_models=(judge_model,),
                allow_paid=allow_paid,
                purpose=spec.purpose,
                # Tournament state records pending comparisons by recipe and re-enters this
                # function on the next run, so the Worker can drain without a runner deadline.
                queue_only=True,
            ),
        },
    )


def _finalize_pairwise_judge(result: JobResult, spec: PairwiseEvaluatorSpec) -> dict[str, Any]:
    """Parse a resolved judge JobResult into a decision dict -- the other half of the old
    single-call `pairwise_judge`, split out so `run()`'s batched dispatch can build every
    comparison's job up front, submit them all in one call, and finalize each result separately."""
    decision = _judge_model(spec.contract).model_validate_json(_content(result))
    return {"winner": decision.winner, "rationale": decision.rationale}


def pairwise_judge(
    backend: Any,
    *,
    spec: PairwiseEvaluatorSpec,
    source: Any,
    candidate_a: Any,
    candidate_b: Any,
    judge_model: str,
    recipe_hash: str,
    deadline_at: datetime | None = None,
    allow_paid: bool = True,
    candidate_models: tuple[str, ...] = (),
) -> tuple[dict[str, Any] | None, bool]:
    """Run one blinded pairwise judgment, returning ``(decision, pending)``.

    The engine accepts arbitrary JSON-serializable source/candidate payloads, so an R6 summary
    comparison can use the same order-swapping and durable-state envelope without pretending that
    summaries are discrete taxonomy candidates. Kept as the single-comparison entry point (used
    directly by tests and any other caller); `run()`'s own loop uses `_build_pairwise_judge_job`
    plus `citypods.compute.llm.dispatch_job_batch` directly for real multi-comparison batching.
    """
    job = _build_pairwise_judge_job(
        spec=spec,
        source=source,
        candidate_a=candidate_a,
        candidate_b=candidate_b,
        judge_model=judge_model,
        recipe_hash=recipe_hash,
        allow_paid=allow_paid,
        candidate_models=candidate_models,
    )
    result = backend.run_inference(job)
    if isinstance(result, JobHandle):
        return None, True
    return _finalize_pairwise_judge(result, spec), False


def _backend(model: str, storage) -> LiteLLMBackend:
    # Start from LLMBackendConfig.from_env() -- the complete, single source of truth for every
    # dispatch-relevant environment variable -- and override only model/mode. This used to
    # hand-roll dispatch_url/dispatch_auth_token alone: LLMBackendConfig has no env-reading
    # __post_init__, so the omitted dispatch_v2_url/dispatch_v2_auth_token fields were always
    # None, and pairwise_judge's queue_only=True policy always fell through to
    # _enqueue_durable_policy_job's legacy v1 branch regardless of LLM_DISPATCH_V2_URL being set.
    # Building from .from_env() means a future field added there can't silently miss this call
    # site again. See the 2026-08-18 incident notes in review/44.
    from dataclasses import replace

    return LiteLLMBackend(
        replace(
            LLMBackendConfig.from_env(),
            model=model,
            mode="dispatch" if model.startswith("mistral/") else "direct",
        ),
        storage=storage,
    )


def _content(result: JobResult) -> str:
    choices = result.output.get("choices") if isinstance(result.output, dict) else None
    value = None
    if isinstance(choices, list) and choices:
        value = choices[0].get("message", {}).get("content")
    if not isinstance(value, str):
        raise ValueError("tournament judge returned no structured content")
    return value


def _state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "results": [], "comparisons": {}}
    value = json.loads(path.read_text())
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        value.setdefault("comparisons", {})
        return value
    return {"version": 1, "results": [], "comparisons": {}}


def _persist_state(state: dict[str, Any], state_path: Path, storage: Any, state_dir: Path) -> None:
    """Persist the generic comparison store after each completed/pending/error judgment."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    push_state(storage, state_dir, only_paths=[STATE])


def run(*, site_config_path: str, config_dir: str, output_dir: str, samples: int) -> int:
    site = load_site_config(site_config_path)
    storage = make_storage(site, "", Path(output_dir))
    if storage is None or not getattr(storage, "cas_capable", False):
        raise RuntimeError("tournament requires configured CAS-capable storage")
    state_dir = Path(".citypods-state")
    pull_state(storage, state_dir)
    state_path = state_dir / STATE
    state = _state(state_path)
    # Old episode-level records are intentionally not considered complete: R5 now compares one
    # chapter at a time, and the same generic state can later carry R6 summary comparisons.
    done = {
        (entry.get("episode_uid"), entry.get("chapter_id"))
        for entry in state["results"]
        if entry.get("chapter_id")
    }
    taxonomy_path = (site.get("tagging") or {}).get("taxonomy_path", "config/taxonomy.yml")
    taxonomy = load_taxonomy(taxonomy_path)
    episodes: list[tuple[Any, dict[str, Any], dict[str, Any]]] = []
    for city in load_city_configs(config_dir, site["defaults"]):
        for rec in load_records(state_dir, source_key(city)).values():
            ep = record_to_episode(rec)
            if not ep.uid:
                continue
            chapters = chapter_tag_inputs(ep, storage)
            if not chapters:
                print(f"llm-tournament: skipping {ep.uid!r} (no usable chapters)")
                continue
            for chapter in chapters:
                if chapter.get("chapter_id") and (ep.uid, chapter["chapter_id"]) not in done:
                    episodes.append((ep, rec, chapter))
    episodes.sort(key=lambda item: (item[0].published, item[0].uid or ""), reverse=True)
    deadline = datetime.now(UTC) + timedelta(minutes=20)
    completed = 0
    for ep, record, chapter in episodes[:samples]:
        chapter_id = str(chapter["chapter_id"])
        if not (
            chapter.get("title") or chapter.get("agenda_text") or chapter.get("transcript_text")
        ):
            print(f"llm-tournament: skipping {ep.uid!r} (chapter has no usable source)")
            continue
        source = {
            "episode_title": ep.title,
            "chapter": chapter,
            "taxonomy": [
                {"id": tag.id, "label": tag.label, "description": tag.description}
                for tag in taxonomy.tags
            ],
        }
        outputs: dict[str, Any] = {}
        contest_failed = False
        for model in MODELS:
            if model == "gemini/gemini-3.1-flash-lite":
                persisted = persisted_r5_flash_output(record, chapter_id)
                if persisted is not None:
                    outputs[model] = persisted
                    continue
            recipe = _digest(
                {"v": 2, "uid": ep.uid, "chapter_id": chapter_id, "model": model, "source": source}
            )
            try:
                _, chapter_tags, pending, _ = llm_tag_suggestions(
                    _backend(model, storage),
                    taxonomy=taxonomy,
                    agenda_item_titles="",
                    agenda_text="",
                    transcript_text="",
                    chapter_inputs=[chapter],
                    recipe_hash=recipe,
                    allow_paid=True,
                    purpose="tournament:tag",
                    deadline_at=deadline,
                )
            except LLMBackendError as exc:
                # Per its own docstring, LLMBackendError's whole family (a malformed reply the
                # provider produced, a scheduler/storage guard, ...) is a safe, provider-agnostic
                # adapter error -- exactly the kind of transient/per-episode issue this bounded,
                # durable runner is meant to skip and pick up on a later scheduled run, not a
                # reason to crash the whole tournament (see scripts/city_discovery.py for the
                # same pattern). A genuine config bug still raises unhandled, failing loudly.
                print(f"llm-tournament: skipping {ep.uid!r} ({model}): {exc}")
                contest_failed = True
                break
            if pending:
                contest_failed = True
                break
            outputs[model] = chapter_tags.get(chapter_id, [])
        if contest_failed or len(outputs) != len(MODELS):
            continue
        # Pass 1: for each of the 6 comparisons (3 CONTESTS x 2 order-swapped pairs), reuse a
        # prior resolved decision from state if there is one, otherwise build its job without
        # dispatching yet -- so the whole sample's still-outstanding comparisons can be submitted
        # in one batch call below instead of up to 6 separate ones (see review/44's 2026-08-18
        # incident retrospective: per-comparison dispatch was exactly the Worker-request volume
        # that incident flagged as worth fixing next).
        judge_spec = PairwiseEvaluatorSpec(
            task="tag",
            purpose="tournament:tag-judge",
            criteria=(
                "Judge source support, omissions, over-tagging, evidence quality, and taxonomy fit."
            ),
        )
        decisions: list[dict[str, Any] | None] = []
        to_dispatch: list[_PendingComparison] = []
        comparison_store = state.setdefault("comparisons", {})
        for left, right, judge in CONTESTS:
            for first, second in order_swapped_pairs(left, right):
                comparison_key = comparison_id(
                    run_id=str(ep.uid),
                    task="tag",
                    subject_id=f"{chapter_id}:{_digest(source)}",
                    first_model=first,
                    second_model=second,
                    judge_model=judge,
                )
                slot = len(decisions)
                decisions.append(None)
                prior_comparison = comparison_store.get(comparison_key)
                if (
                    isinstance(prior_comparison, dict)
                    and prior_comparison.get("status") == "resolved"
                ):
                    prior_record = prior_comparison.get("decision_record")
                    if isinstance(prior_record, dict):
                        decisions[slot] = dict(prior_record)
                        continue
                    # A hand-repaired or partially-written state entry must not make a sample
                    # permanently incomplete. Re-run only this missing comparison and replace the
                    # malformed envelope with a complete decision record below.
                job = _build_pairwise_judge_job(
                    spec=judge_spec,
                    source=source,
                    candidate_a=judge_candidates(outputs[first]),
                    candidate_b=judge_candidates(outputs[second]),
                    judge_model=judge,
                    recipe_hash=comparison_key,
                    candidate_models=(left, right),
                )
                to_dispatch.append(
                    _PendingComparison(slot, comparison_key, job, left, right, judge, first)
                )

        # Pass 2: one batch dispatch per distinct judge model among this sample's outstanding
        # comparisons (today that's always just JUDGE_MODEL -- CONTESTS pins one judge for all
        # three contests -- but grouping by judge rather than assuming a single one keeps this
        # correct if that ever changes).
        judge_pending = False
        for judge in {item.judge for item in to_dispatch}:
            group = [item for item in to_dispatch if item.judge == judge]
            results = dispatch_job_batch(_backend(judge, storage), [item.job for item in group])
            for item, result in zip(group, results, strict=True):
                if isinstance(result, JobHandle):
                    comparison_store[item.comparison_key] = {
                        "status": "pending",
                        "comparison_id": item.comparison_key,
                        "task": "tag",
                        "subject_id": f"{ep.uid}:{chapter_id}",
                    }
                    judge_pending = True
                elif isinstance(result, JobResult):
                    decision = _finalize_pairwise_judge(result, judge_spec)
                    decision_record = {
                        "left": item.left,
                        "right": item.right,
                        "judge": item.judge,
                        "first": item.first,
                        "comparison_id": item.comparison_key,
                        "winner": decision["winner"] if decision else "tie",
                        "rationale": decision["rationale"] if decision else "",
                    }
                    decisions[item.slot] = decision_record
                    comparison_store[item.comparison_key] = {
                        "status": "resolved",
                        "comparison_id": item.comparison_key,
                        "task": "tag",
                        "subject_id": f"{ep.uid}:{chapter_id}",
                        "decision_record": decision_record,
                    }
                else:
                    # dispatch_job_batch's own contract: anything that isn't a JobResult/JobHandle
                    # is the Exception sentinel for this one comparison's own failed submission
                    # (e.g. LLMBackendError) -- same per-comparison isolation the old code's
                    # `except LLMBackendError` gave a single pairwise_judge call.
                    print(f"llm-tournament: skipping {ep.uid!r} judge {judge}: {result}")
                    comparison_store[item.comparison_key] = {
                        "status": "error",
                        "comparison_id": item.comparison_key,
                        "task": "tag",
                        "subject_id": f"{ep.uid}:{chapter_id}",
                        "error": str(result)[:500],
                    }
                    judge_pending = True
            _persist_state(state, state_path, storage, state_dir)
        if (
            judge_pending
            or len(decisions) != len(CONTESTS) * 2
            or any(d is None for d in decisions)
        ):
            continue
        state["results"].append(
            {
                "task": "tag",
                "episode_uid": ep.uid,
                "chapter_id": chapter_id,
                "scope": "chapter",
                "judge_model": JUDGE_MODEL,
                "at": datetime.now(UTC).isoformat(),
                "decisions": decisions,
            }
        )
        # A resolved comparison is needed only to resume an interrupted six-way sample. Once the
        # result has been durably appended, retain its decision in that result and release the
        # per-comparison checkpoint so the state file cannot grow for every historical sample.
        comparison_store = state.setdefault("comparisons", {})
        for decision in decisions:
            comparison_store.pop(str(decision["comparison_id"]), None)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        push_state(storage, state_dir, only_paths=[STATE])
        completed += 1
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    push_state(storage, state_dir, only_paths=[STATE])
    print(f"llm-tournament: completed {completed} sample(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output-dir", default="docs")
    parser.add_argument("--samples", type=int, default=2)
    args = parser.parse_args(argv)
    return run(
        site_config_path=args.site_config,
        config_dir=args.config_dir,
        output_dir=args.output_dir,
        samples=max(1, min(args.samples, 2)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
