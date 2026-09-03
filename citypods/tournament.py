"""Bounded, durable three-model pairwise tournament for LLM topic tags.

The runner deliberately records comparison evidence only.  It never changes the production
champion; a later human-review ticket is the sole routing authority (review/34).
"""

from __future__ import annotations

import argparse
import base64
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
    PerModelBatchingBackends,
    dispatch_job_batch,
)
from citypods.compute.llm_lanes import lane_for
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.compute.structured import register_response_model
from citypods.config import load_city_configs, load_site_config
from citypods.records import load_records, record_to_episode, source_key
from citypods.review_issues import render_decision_block
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage
from citypods.tags import (
    chapter_tag_inputs,
    episode_tag_inputs,  # noqa: F401 — compatibility hook; R5 run itself is chapter-only
    llm_tag_suggestions,
    load_taxonomy,
)

# Contestants and judge come from config/site_config.yml's `llm_lanes` (review/44 Phase 4) rather
# than being hard-coded here, so every dispatching lane's route choice lives in one place. The
# `tournament:tag` lane is declared `dispatch_shape: per_model` because these routes are being
# COMPARED: each contestant gets its own single-route job, and pooling them would let the
# scheduler answer "how does model X tag this chapter?" with model Y and void the comparison.
MODELS = lane_for("tournament:tag").models
JUDGE_MODEL = lane_for("tournament:tag-judge").primary_model
# Every unordered contestant pair, judged by the one configured judge. Derived rather than listed
# so adding a contestant to config extends the grid automatically instead of silently comparing a
# new model against only some of its peers -- the previous hand-written list had to be edited in
# lockstep with MODELS, and a missed pair produced a quietly incomplete tournament.
CONTESTS = tuple(
    (left, right, JUDGE_MODEL) for index, left in enumerate(MODELS) for right in MODELS[index + 1 :]
)
STATE = "llm_tournament.json"
TICKET_STATE = "llm_tournament_tickets.json"
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


def champion_stats(
    results: list[dict[str, Any]], *, current_model: str, now: datetime, window_days: int = 28
) -> dict[str, dict[str, float]]:
    """Aggregate order-swapped wins against the configured champion over a rolling window."""
    cutoff = now - timedelta(days=window_days)
    rows: dict[str, dict[str, float]] = {}
    for result in results:
        try:
            at = datetime.fromisoformat(str(result.get("at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if at < cutoff:
            continue
        for decision in result.get("decisions") or []:
            if not isinstance(decision, dict):
                continue
            left, right = str(decision.get("left", "")), str(decision.get("right", ""))
            if current_model not in {left, right}:
                continue
            challenger = right if left == current_model else left
            stats = rows.setdefault(challenger, {"wins": 0.0, "losses": 0.0, "ties": 0.0})
            winner = str(decision.get("winner", "tie"))
            first = str(decision.get("first", left))
            if winner == "a":
                winner_model = first
            elif winner == "b":
                winner_model = right if first == left else left
            else:
                winner_model = ""
            if winner in {"tie", "both_poor"}:
                stats["ties"] += 1
            elif winner_model == challenger:
                stats["wins"] += 1
            else:
                stats["losses"] += 1
    for stats in rows.values():
        total = stats["wins"] + stats["losses"] + stats["ties"]
        stats["win_rate"] = (stats["wins"] + stats["ties"] * 0.5) / total if total else 0.0
        stats["comparisons"] = total
    return rows


def _ticket_marker(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "<!-- citypods:tournament-ticket " + base64.urlsafe_b64encode(raw).decode() + " -->"


def render_champion_ticket(
    *,
    task: str,
    current_model: str,
    stats: dict[str, dict[str, float]],
    required_win_rate: float,
    estimates: dict[str, dict[str, float]] | None = None,
    window_days: int = 28,
) -> str:
    eligible = [
        model
        for model, value in sorted(stats.items())
        if value["comparisons"] and value["win_rate"] > required_win_rate
    ]
    marker = _ticket_marker(
        {
            "version": 1,
            "task": task,
            "current_model": current_model,
            "challengers": eligible,
            "required_win_rate": required_win_rate,
        }
    )
    lines = [
        marker,
        f"# Tournament champion ticket: {task}",
        "",
        f"Current route: `{current_model}`. Rolling window: {window_days} days.",
        f"A challenger needs a strict win rate above {required_win_rate:.0%} to be actionable.",
        "",
        "| Challenger | Wins | Losses | Ties | Win rate | Settled monthly cost | "
        "Retained chapters |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for model, value in sorted(stats.items()):
        estimate = (estimates or {}).get(model, {})
        lines.append(
            f"| `{model}` | {value['wins']:.0f} | {value['losses']:.0f} | "
            f"{value['ties']:.0f} | {value['win_rate']:.1%} | "
            f"${estimate.get('monthly_cost_usd', 0.0):.2f} | "
            f"{estimate.get('retained_chapters', 0.0):.0f} |"
        )
    if not eligible:
        lines += [
            "",
            "No challenger clears the configured gate this week; this ticket is FYI-only.",
        ]
    else:
        choices = ["Keep current route"]
        for model in eligible:
            choices.append(f"Switch to `{model}` (normal gradual refresh)")
            choices.append(f"Switch to `{model}` (retained-catalog backfill)")
        lines += [
            "",
            "Choose exactly one routing decision:",
            "",
            render_decision_block(tuple(choices)),
        ]
    lines += [
        "",
        "Cost and back-catalog estimates use settled telemetry when it is available; no issue "
        "decision changes production directly. A checked switch opens a scoped configuration PR.",
    ]
    return "\n".join(lines) + "\n"


def ticket_estimates(state_dir: Path, models: set[str]) -> dict[str, dict[str, float]]:
    """Return settled ledger cost and recipe-different retained chapter counts per route."""
    estimates = {model: {"monthly_cost_usd": 0.0, "retained_chapters": 0.0} for model in models}
    try:
        budget = json.loads((state_dir / "llm_budget.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        budget = {}
    for model, value in (budget.get("routes") or {}).items():
        normalized = str(model).removeprefix("litellm:")
        if normalized in estimates and isinstance(value, dict):
            estimates[normalized]["monthly_cost_usd"] = float(value.get("cost_used") or 0.0)
    for path in state_dir.glob("sources/*/episodes.json"):
        try:
            episodes = json.loads(path.read_text(encoding="utf-8")).get("episodes") or {}
        except (OSError, ValueError):
            continue
        prior_by_chapter: dict[str, set[str]] = {}
        for record in episodes.values():
            candidates = record.get("llm_tag_candidates") if isinstance(record, dict) else None
            chapter_id = str(record.get("chapter_id") or "") if isinstance(record, dict) else ""
            prior_models = {
                str(candidate.get("provider_model") or "").removeprefix("litellm:")
                for candidate in candidates or []
                if isinstance(candidate, dict) and candidate.get("source_kind", "llm") != "rule"
            }
            if chapter_id and prior_models:
                prior_by_chapter.setdefault(chapter_id, set()).update(prior_models)
        for model in estimates:
            estimates[model]["retained_chapters"] += sum(
                1 for prior_models in prior_by_chapter.values() if model not in prior_models
            )
    return estimates


def package_ticket(*, site_config_path: str, output_dir: str, out_dir: str) -> int:
    site = load_site_config(site_config_path)
    storage = make_storage(site, "", Path(output_dir))
    if storage is None:
        raise RuntimeError("tournament ticket requires configured storage")
    state_dir = Path(".citypods-state")
    pull_state(storage, state_dir)
    state = _state(state_dir / STATE)
    config = site.get("tournament") or {}
    required = float(config.get("challenger_win_rate", 0.60))
    window = int(config.get("window_days", 28))
    # The reigning production tag route, i.e. what a challenger must beat. Reads the lane
    # registry rather than the removed `tagging.llm_model` key.
    current = lane_for("topic-tags:tagger", site).primary_model
    stats = champion_stats(
        state["results"], current_model=current, now=datetime.now(UTC), window_days=window
    )
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    estimates = ticket_estimates(state_dir, set(stats))
    (target / "ticket.md").write_text(
        render_champion_ticket(
            task="tag",
            current_model=current,
            stats=stats,
            required_win_rate=required,
            estimates=estimates,
            window_days=window,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "task": "tag",
                "challengers": sorted(stats),
                "eligible": sum(value["win_rate"] > required for value in stats.values()),
            }
        )
    )
    return 0


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
    # Run-scoped, per-model backends. Every queue-only job this run creates -- candidate
    # generation for each contestant and each pairwise comparison -- is collected here and
    # submitted in one bounded batch per model at the end, instead of one Worker request per job.
    backends = PerModelBatchingBackends(lambda model: _backend(model, storage))
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
                    backends.collecting(model),
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
            results = dispatch_job_batch(backends.collecting(judge), [item.job for item in group])
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
    # One bounded ingress batch per model for everything this run collected. Newly submitted work
    # resolves on a later run from its durable record, exactly as a pending job always has.
    queued = backends.queued_count
    outcomes = backends.flush()
    submit_errors = PerModelBatchingBackends.submission_errors(outcomes)
    print(
        f"llm-tournament: LLM batch flush: jobs={len(outcomes)} queued={queued} "
        f"errors={len(submit_errors)}",
        flush=True,
    )

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    push_state(storage, state_dir, only_paths=[STATE])
    print(f"llm-tournament: completed {completed} sample(s)")
    if submit_errors:
        # A failed submission is not a deferral: nothing is queued, so no later run picks it up
        # unless this one says so. Report it as a run failure rather than exiting 0 with the work
        # silently dropped.
        for outcome in submit_errors[:10]:
            print(f"llm-tournament: submission failed: {outcome.result}", flush=True)
        print(
            f"llm-tournament: {len(submit_errors)} job(s) failed to submit; "
            "no work was queued for them",
            flush=True,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("ticket",))
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--output-dir", default="docs")
    # Default to the lane's own budget; --samples remains a manual downward override for a
    # one-off run. A value above the configured budget is clamped, never honored.
    parser.add_argument("--samples", type=int, default=0)
    parser.add_argument("--out-dir", default="tournament-ticket")
    args = parser.parse_args(argv)
    if args.command == "ticket":
        return package_ticket(
            site_config_path=args.site_config, output_dir=args.output_dir, out_dir=args.out_dir
        )
    # The per-run sample budget comes from `llm_lanes["tournament:tag"].max_dispatches_per_run`,
    # not a magic constant. It used to be hard-clamped to 2 regardless of --samples, which meant
    # the lane could never dispatch more than ~2 samples' worth of work per day (8 candidate jobs
    # plus 24 comparisons) against a budget sized for far more -- one of the concrete reasons the
    # research lanes never came close to filling their quota. Each sample costs one candidate job
    # per contestant plus one comparison per ordered pair, so convert the job budget into samples
    # rather than comparing it to a sample count directly.
    lane = lane_for("tournament:tag")
    jobs_per_sample = len(MODELS) + len(CONTESTS) * 2
    sample_budget = max(1, lane.max_dispatches_per_run // max(1, jobs_per_sample))
    return run(
        site_config_path=args.site_config,
        config_dir=args.config_dir,
        output_dir=args.output_dir,
        samples=sample_budget if args.samples <= 0 else max(1, min(args.samples, sample_budget)),
    )


if __name__ == "__main__":
    raise SystemExit(main())
