from types import SimpleNamespace

from citypods import tournament
from citypods.compute.base import JobHandle
from citypods.compute.llm import LLMStructuredOutputError
from citypods.tournament import (
    R5_FLASH_MODEL,
    PairwiseEvaluatorSpec,
    contest_plan,
    judge_candidates,
    pairwise_judge,
    persisted_r5_flash_output,
)


def test_round_robin_has_three_independent_judges():
    assert len(contest_plan()) == 3
    assert all(judge not in {left, right} for left, right, judge in contest_plan())


def test_reuses_only_provenanced_r5_flash_candidates():
    candidate = {"provider_model": R5_FLASH_MODEL, "recipe_hash": "r5-recipe", "id": "housing"}
    assert persisted_r5_flash_output(
        {"tags_llm_recipe_hash": "r5-recipe", "llm_tag_candidates": [candidate]}
    ) == [candidate]
    assert (
        persisted_r5_flash_output({"tags_llm_recipe_hash": "r5-recipe", "llm_tag_candidates": []})
        is None
    )
    assert (
        persisted_r5_flash_output(
            {
                "tags_llm_recipe_hash": "new-recipe",
                "llm_tag_candidates": [{**candidate, "recipe_hash": "old-recipe"}],
            }
        )
        is None
    )
    assert (
        persisted_r5_flash_output(
            {
                "tags_llm_recipe_hash": "r5-recipe",
                "llm_tag_candidates": [{**candidate, "provider_model": "litellm:old-model"}],
            }
        )
        is None
    )


def test_judge_prompt_omits_candidate_provenance():
    assert judge_candidates(
        [{"id": "housing", "confidence": 0.9, "provider_model": R5_FLASH_MODEL, "recipe_hash": "x"}]
    ) == [{"id": "housing", "confidence": 0.9}]


def test_pairwise_judge_uses_durable_queue_policy():
    seen = []

    class Backend:
        def run_inference(self, job):
            seen.append(job)
            return JobHandle(task="tag", recipe_hash=job.recipe_hash, backend="test", ref="pending")

    decision, pending = pairwise_judge(
        Backend(),
        spec=PairwiseEvaluatorSpec(task="tag", purpose="tournament:tag-judge", criteria="support"),
        source={"chapter": "source"},
        candidate_a=[],
        candidate_b=[],
        judge_model="google/gemma-4-31b-it",
        recipe_hash="comparison-1",
        candidate_models=("gemini/gemini-3.1-flash-lite",),
    )

    assert decision is None
    assert pending is True
    assert seen[0].inputs["llm_policy"].queue_only is True
    assert seen[0].inputs["llm_policy"].deadline_at is None


def test_run_skips_episode_on_llm_backend_error(tmp_path, monkeypatch, capsys):
    """A provider/schema failure for one model must not crash the whole tournament -- the
    episode is left undone for a later scheduled run instead, matching this runner's own
    "durable... incomplete/deferred contests resume idempotently" design (and the pattern
    scripts/city_discovery.py already uses for the same LLMBackendError family)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tournament, "load_site_config", lambda *_a: {"defaults": {}, "tagging": {}})
    monkeypatch.setattr(
        tournament, "make_storage", lambda *_a, **_k: SimpleNamespace(cas_capable=True)
    )
    monkeypatch.setattr(tournament, "pull_state", lambda *_a, **_k: 0)
    monkeypatch.setattr(tournament, "push_state", lambda *_a, **_k: 0)
    monkeypatch.setattr(tournament, "load_taxonomy", lambda *_a, **_k: SimpleNamespace(tags=()))
    monkeypatch.setattr(tournament, "load_city_configs", lambda *_a, **_k: [SimpleNamespace()])
    monkeypatch.setattr(tournament, "source_key", lambda _city: "city")
    episode = SimpleNamespace(uid="ep-1", published="2026-01-01", title="Meeting")
    monkeypatch.setattr(tournament, "load_records", lambda *_a, **_k: {"ep-1": {}})
    monkeypatch.setattr(tournament, "record_to_episode", lambda _rec: episode)
    monkeypatch.setattr(
        tournament, "episode_tag_inputs", lambda *_a, **_k: ("titles", "agenda", "transcript")
    )
    monkeypatch.setattr(tournament, "chapter_tag_inputs", lambda *_a, **_k: [])

    def failing_llm_tag_suggestions(*_args, **_kwargs):
        raise LLMStructuredOutputError("structured LLM response failed Pydantic validation")

    monkeypatch.setattr(tournament, "llm_tag_suggestions", failing_llm_tag_suggestions)

    exit_code = tournament.run(
        site_config_path="config/site_config.yml",
        config_dir="config",
        output_dir=str(tmp_path),
        samples=1,
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "skipping 'ep-1'" in out
    assert "completed 0 sample(s)" in out
