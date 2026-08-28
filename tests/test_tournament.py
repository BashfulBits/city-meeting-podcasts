from datetime import UTC, datetime
from types import SimpleNamespace

from citypods import tournament
from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm import LLMStructuredOutputError
from citypods.tournament import (
    R5_FLASH_MODEL,
    PairwiseEvaluatorSpec,
    contest_plan,
    judge_candidates,
    pairwise_judge,
    persisted_r5_flash_output,
)
from scripts.tournament_route_proposal import parse_ticket


def test_round_robin_has_six_independent_judges():
    assert len(contest_plan()) == 6
    assert all(judge not in {left, right} for left, right, judge in contest_plan())


def test_champion_ticket_requires_a_strictly_greater_than_sixty_percent_gate():
    now = datetime(2026, 8, 28, tzinfo=UTC)
    results = [
        {
            "at": now.isoformat(),
            "decisions": [
                {
                    "left": "current",
                    "right": "challenger",
                    "first": "current",
                    "winner": winner,
                }
                for winner in ("b", "b", "b", "a", "a")
            ],
        }
    ]
    stats = tournament.champion_stats(results, current_model="current", now=now)
    body = tournament.render_champion_ticket(
        task="tag", current_model="current", stats=stats, required_win_rate=0.60
    )

    assert stats["challenger"]["win_rate"] == 0.60
    assert "FYI-only" in body
    assert "- [ ] Switch" not in body


def test_ticket_parser_accepts_only_one_registered_switch_choice():
    body = tournament.render_champion_ticket(
        task="tag",
        current_model="current",
        stats={
            "challenger": {
                "wins": 7,
                "losses": 3,
                "ties": 0,
                "comparisons": 10,
                "win_rate": 0.7,
            }
        },
        required_win_rate=0.60,
    ).replace("- [ ] Switch", "- [x] Switch", 1)

    decision = parse_ticket(body)

    assert decision["action"] == "switch"
    assert decision["model"] == "challenger"
    assert decision["backfill"] is False


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


def test_backend_wires_dispatch_v2_url_from_env(monkeypatch):
    """Regression test for the 2026-08-18 incident: _backend() used to hand-roll only
    dispatch_url/dispatch_auth_token, leaving dispatch_v2_url/dispatch_v2_auth_token at
    LLMBackendConfig's None default regardless of the environment -- so pairwise_judge's own
    queue_only=True policy (see test_pairwise_judge_uses_durable_queue_policy above) always fell
    through to the legacy v1 dispatch branch. Building from LLMBackendConfig.from_env() fixes
    this and any future field added there."""
    monkeypatch.setenv("LLM_DISPATCH_URL", "https://dispatch-v1.example.com")
    monkeypatch.setenv("LLM_DISPATCH_AUTH_TOKEN", "v1-token")
    monkeypatch.setenv("LLM_DISPATCH_V2_URL", "https://dispatch-v2.example.com")
    monkeypatch.setenv("LLM_DISPATCH_V2_AUTH_TOKEN", "v2-token")

    backend = tournament._backend("gemini/gemini-3-flash-preview", storage=None)

    assert backend.config.dispatch_url == "https://dispatch-v1.example.com"
    assert backend.config.dispatch_v2_url == "https://dispatch-v2.example.com"
    assert backend.config.dispatch_v2_auth_token == "v2-token"
    assert backend.config.model == "gemini/gemini-3-flash-preview"
    assert backend.config.mode == "direct"


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


def test_run_batches_all_judge_comparisons_into_one_enqueue_call(tmp_path, monkeypatch):
    """The actual point of this refactor (see review/44's 2026-08-18 incident retrospective):
    one sample's pairwise-judge comparisons (CONTESTS x 2 order-swapped pairs, all sharing
    JUDGE_MODEL) must produce exactly one enqueue_batch call carrying all jobs, not separate
    single-job calls."""
    import json as json_lib

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
        tournament,
        "chapter_tag_inputs",
        lambda *_a, **_k: [{"chapter_id": "c1", "title": "Item 1"}],
    )

    def fake_llm_tag_suggestions(_backend, **kwargs):
        chapter_id = kwargs["chapter_inputs"][0]["chapter_id"]
        return [], {chapter_id: [{"id": "housing", "confidence": 0.9}]}, False, "model"

    monkeypatch.setattr(tournament, "llm_tag_suggestions", fake_llm_tag_suggestions)

    class FakeJudgeBackend:
        def __init__(self):
            self.enqueue_calls: list[list] = []

        def enqueue_batch(self, jobs):
            jobs = list(jobs)
            self.enqueue_calls.append(jobs)
            decision_body = json_lib.dumps({"winner": "a", "rationale": "clear support"})
            return [
                JobResult(
                    task=job.task,
                    recipe_hash=job.recipe_hash,
                    output={"choices": [{"message": {"content": decision_body}}]},
                )
                for job in jobs
            ]

        def poll_batch(self, handles):
            return {}

    judge_backend = FakeJudgeBackend()
    monkeypatch.setattr(tournament, "_backend", lambda _model, _storage: judge_backend)

    exit_code = tournament.run(
        site_config_path="config/site_config.yml",
        config_dir="config",
        output_dir=str(tmp_path),
        samples=1,
    )

    assert exit_code == 0
    assert len(judge_backend.enqueue_calls) == 1  # one call, not per-comparison calls
    assert len(judge_backend.enqueue_calls[0]) == len(tournament.CONTESTS) * 2


def test_run_handles_pending_job_handles_and_skips_sample_finalization(tmp_path, monkeypatch):
    """When judge comparisons return JobHandle, state records them as pending and does not
    finalize."""
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
        tournament,
        "chapter_tag_inputs",
        lambda *_a, **_k: [{"chapter_id": "c1", "title": "Item 1"}],
    )

    def fake_llm_tag_suggestions(_backend, **kwargs):
        chapter_id = kwargs["chapter_inputs"][0]["chapter_id"]
        return [], {chapter_id: [{"id": "housing", "confidence": 0.9}]}, False, "model"

    monkeypatch.setattr(tournament, "llm_tag_suggestions", fake_llm_tag_suggestions)

    class PendingJudgeBackend:
        def enqueue_batch(self, jobs):
            return [
                JobHandle(
                    task=job.task,
                    ref=f"handle-{job.recipe_hash}",
                    backend="llm-dispatch-v2",
                    recipe_hash=job.recipe_hash,
                )
                for job in jobs
            ]

        def poll_batch(self, handles):
            return {}

    monkeypatch.setattr(tournament, "_backend", lambda _model, _storage: PendingJudgeBackend())

    exit_code = tournament.run(
        site_config_path="config/site_config.yml",
        config_dir="config",
        output_dir=str(tmp_path),
        samples=1,
    )

    assert exit_code == 0
    state_file = tmp_path / ".citypods-state" / "llm_tournament.json"
    assert state_file.exists()
    import json as json_lib

    state = json_lib.loads(state_file.read_text())
    assert len(state.get("results", [])) == 0  # not finalized because comparisons are pending
    assert len(state.get("comparisons", {})) == len(tournament.CONTESTS) * 2
    assert all(c["status"] == "pending" for c in state["comparisons"].values())


def test_run_reuses_prior_resolved_comparison_without_dispatch(tmp_path, monkeypatch):
    """When comparison_store already contains a resolved comparison, it is reused without
    dispatch."""
    import json as json_lib

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
        tournament,
        "chapter_tag_inputs",
        lambda *_a, **_k: [{"chapter_id": "c1", "title": "Item 1"}],
    )

    def fake_llm_tag_suggestions(_backend, **kwargs):
        chapter_id = kwargs["chapter_inputs"][0]["chapter_id"]
        return [], {chapter_id: [{"id": "housing", "confidence": 0.9}]}, False, "model"

    monkeypatch.setattr(tournament, "llm_tag_suggestions", fake_llm_tag_suggestions)

    # Pre-populate state with resolved decisions for all comparisons
    initial_comparisons = {}
    source = {
        "episode_title": "Meeting",
        "chapter": {"chapter_id": "c1", "title": "Item 1"},
        "taxonomy": [],
    }
    for left, right, judge in tournament.CONTESTS:
        for first, second in tournament.order_swapped_pairs(left, right):
            comp_id = tournament.comparison_id(
                run_id="ep-1",
                task="tag",
                subject_id=f"c1:{tournament._digest(source)}",
                first_model=first,
                second_model=second,
                judge_model=judge,
            )
            initial_comparisons[comp_id] = {
                "status": "resolved",
                "comparison_id": comp_id,
                "task": "tag",
                "subject_id": "ep-1:c1",
                "decision_record": {
                    "left": left,
                    "right": right,
                    "judge": judge,
                    "first": first,
                    "comparison_id": comp_id,
                    "winner": "a",
                    "rationale": "prior cached",
                },
            }

    state_dir = tmp_path / ".citypods-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "llm_tournament.json"
    state_file.write_text(
        json_lib.dumps({"version": 1, "comparisons": initial_comparisons, "results": []})
    )

    class NoOpJudgeBackend:
        def __init__(self):
            self.enqueue_calls = []

        def enqueue_batch(self, jobs):
            self.enqueue_calls.append(jobs)
            return []

        def poll_batch(self, handles):
            return {}

    judge_backend = NoOpJudgeBackend()
    monkeypatch.setattr(tournament, "_backend", lambda _model, _storage: judge_backend)

    exit_code = tournament.run(
        site_config_path="config/site_config.yml",
        config_dir="config",
        output_dir=str(tmp_path),
        samples=1,
    )

    assert exit_code == 0
    assert len(judge_backend.enqueue_calls) == 0  # no jobs needed to be dispatched
    state = json_lib.loads(state_file.read_text())
    assert len(state.get("results", [])) == 1  # completed and recorded
