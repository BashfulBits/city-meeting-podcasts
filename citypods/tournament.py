"""Bounded, durable three-model pairwise tournament for LLM topic tags.

The runner deliberately records comparison evidence only.  It never changes the production
champion; a later human-review ticket is the sole routing authority (review/34).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citypods.compute.base import InferenceJob, JobHandle, JobResult
from citypods.compute.llm import LiteLLMBackend, LLMBackendConfig, LLMBackendError
from citypods.compute.llm_policy import LLMRequestPolicy
from citypods.compute.structured import register_response_model
from citypods.config import load_city_configs, load_site_config
from citypods.records import load_records, record_to_episode, source_key
from citypods.statesync import pull_state, push_state
from citypods.storage import make_storage
from citypods.tags import (
    chapter_tag_inputs,
    episode_tag_inputs,
    llm_tag_suggestions,
    load_taxonomy,
)

MODELS = (
    "gemini/gemini-3.1-flash-lite",
    "deepseek/deepseek-v4-flash",
    "mistral/mistral-large-latest",
)
CONTESTS = (
    ("deepseek/deepseek-v4-flash", "gemini/gemini-3.1-flash-lite", "mistral/mistral-large-latest"),
    ("deepseek/deepseek-v4-flash", "mistral/mistral-large-latest", "gemini/gemini-3.1-flash-lite"),
    ("gemini/gemini-3.1-flash-lite", "mistral/mistral-large-latest", "deepseek/deepseek-v4-flash"),
)
STATE = "llm_tournament.json"
JUDGE_CONTRACT = "tournament-tag-judge"
R5_FLASH_MODEL = "litellm:gemini/gemini-3.1-flash-lite"


def contest_plan() -> tuple[tuple[str, str, str], ...]:
    """The immutable 3-way round robin; a judge is never a contestant."""
    return CONTESTS


def persisted_r5_flash_output(record: dict[str, Any]) -> list[dict[str, Any]] | None:
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
    if all(
        item.get("provider_model") == R5_FLASH_MODEL and item.get("recipe_hash") == recipe
        for item in candidates
    ):
        return candidates
    return None


def judge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep tag substance while withholding route/recipe provenance from a blind judge."""
    fields = ("id", "chapter_id", "source", "confidence", "explanation", "evidence")
    return [{field: item[field] for field in fields if field in item} for item in candidates]


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(body).hexdigest()


def _judge_model():
    from pydantic import BaseModel, ConfigDict, Field

    class Decision(BaseModel):
        model_config = ConfigDict(extra="forbid")
        winner: str = Field(pattern="^(a|b|tie)$")
        rationale: str = Field(min_length=1, max_length=500)

    model = getattr(_judge_model, "model", None)
    if model is None:
        model = Decision
        register_response_model(JUDGE_CONTRACT, model)
        _judge_model.model = model
    return model


def _backend(model: str, storage) -> LiteLLMBackend:
    return LiteLLMBackend(
        LLMBackendConfig(
            model=model,
            mode="dispatch" if model.startswith("mistral/") else "direct",
            dispatch_url=__import__("os").environ.get("LLM_DISPATCH_URL"),
            dispatch_auth_token=__import__("os").environ.get("LLM_DISPATCH_AUTH_TOKEN"),
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
        return {"version": 1, "results": []}
    value = json.loads(path.read_text())
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        return value
    return {"version": 1, "results": []}


def run(*, site_config_path: str, config_dir: str, output_dir: str, samples: int) -> int:
    site = load_site_config(site_config_path)
    storage = make_storage(site, "", Path(output_dir))
    if storage is None or not getattr(storage, "cas_capable", False):
        raise RuntimeError("tournament requires configured CAS-capable storage")
    state_dir = Path(".citypods-state")
    pull_state(storage, state_dir)
    state_path = state_dir / STATE
    state = _state(state_path)
    done = {entry.get("episode_uid") for entry in state["results"]}
    taxonomy_path = (site.get("tagging") or {}).get("taxonomy_path", "config/taxonomy.yml")
    taxonomy = load_taxonomy(taxonomy_path)
    episodes: list[tuple[Any, dict[str, Any]]] = []
    for city in load_city_configs(config_dir, site["defaults"]):
        for rec in load_records(state_dir, source_key(city)).values():
            ep = record_to_episode(rec)
            if ep.uid and ep.uid not in done:
                episodes.append((ep, rec))
    episodes.sort(key=lambda item: (item[0].published, item[0].uid or ""), reverse=True)
    deadline = datetime.now(UTC) + timedelta(minutes=20)
    completed = 0
    for ep, record in episodes[:samples]:
        titles, agenda, transcript = episode_tag_inputs(ep, storage)
        if not (titles or agenda or transcript):
            continue
        chapters = chapter_tag_inputs(ep, storage)
        source = {
            "title": ep.title,
            "agenda": (titles + "\n" + agenda)[:12000],
            "transcript": transcript[:24000],
        }
        outputs: dict[str, Any] = {}
        contest_failed = False
        for model in MODELS:
            if model == "gemini/gemini-3.1-flash-lite":
                persisted = persisted_r5_flash_output(record)
                if persisted is not None:
                    outputs[model] = persisted
                    continue
            recipe = _digest({"v": 1, "uid": ep.uid, "model": model, "source": source})
            try:
                answer, _, pending, _ = llm_tag_suggestions(
                    _backend(model, storage),
                    taxonomy=taxonomy,
                    agenda_item_titles=titles,
                    agenda_text=agenda,
                    transcript_text=transcript,
                    chapter_inputs=chapters,
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
            outputs[model] = answer
        if contest_failed or len(outputs) != len(MODELS):
            continue
        decisions = []
        judge_pending = False
        for left, right, judge in CONTESTS:
            for first, second in ((left, right), (right, left)):
                prompt = {
                    "source": source,
                    "candidate_a": judge_candidates(outputs[first]),
                    "candidate_b": judge_candidates(outputs[second]),
                }
                _judge_model()
                job = InferenceJob(
                    task="tag",
                    recipe_hash=_digest({"v": 1, "judge": judge, "prompt": prompt}),
                    inputs={
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "Judge factual topic-tag quality only. Return a, b, or tie."
                                ),
                            },
                            {"role": "user", "content": json.dumps(prompt)},
                        ],
                        "structured_output": JUDGE_CONTRACT,
                        "llm_policy": LLMRequestPolicy(
                            allowed_models=(judge,),
                            allow_paid=True,
                            deadline_at=deadline,
                            purpose="tournament:tag-judge",
                        ),
                    },
                )
                try:
                    result = _backend(judge, storage).run_inference(job)
                except LLMBackendError as exc:
                    print(f"llm-tournament: skipping {ep.uid!r} judge {judge}: {exc}")
                    judge_pending = True
                    break
                if isinstance(result, JobHandle):
                    judge_pending = True
                    break
                decision = _judge_model().model_validate_json(_content(result))
                decisions.append(
                    {
                        "left": left,
                        "right": right,
                        "judge": judge,
                        "first": first,
                        "winner": decision.winner,
                        "rationale": decision.rationale,
                    }
                )
            if judge_pending:
                break
        if judge_pending or len(decisions) != 6:
            continue
        state["results"].append(
            {"episode_uid": ep.uid, "at": datetime.now(UTC).isoformat(), "decisions": decisions}
        )
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
