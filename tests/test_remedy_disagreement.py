"""The adjudication diagnostic must not turn candidate suggestions into ground truth."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT / "evals/remedy/holdout/disagreement-2026-10-14"
RESULTS = ROOT / "evals/remedy/results/disagreement-2026-10-14"


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def prep():
    return module(PREP / "prepare.py", "disagreement_prepare_test")


@pytest.fixture
def runner():
    return module(RESULTS / "run.py", "disagreement_runner_test")


def test_exact_eight_selected_without_gold(prep):
    _, cases = prep.load_manifest()
    rows = [json.loads(p.read_text()) for p in (prep.BASE / "batches").glob("*.json")]
    selected = prep.disagreements(cases, rows)
    assert len(selected) == 8
    assert {c.city for c, _ in selected} == {
        "addison-tx",
        "arlington-tx",
        "dallas-tx",
        "fort-worth-tx",
    }
    assert "gold" not in (PREP / "prepare.py").read_text().split('"""', 2)[-1]


def test_retrieval_uses_union_and_three_algorithmic_peers(prep):
    _, cases = prep.load_manifest()
    rows = [json.loads(p.read_text()) for p in (prep.BASE / "batches").glob("*.json")]
    case, pair = prep.disagreements(cases, rows)[0]
    selected = prep.selected_feeds(case, pair)
    proposed = set().union(*(set(r["answer"]["feed_slugs"]) for r in pair))
    assert proposed <= {f["slug"] for f in selected}
    assert len(selected) == len(proposed) + 3
    assert prep.selected_feeds(case, pair) == selected


def test_history_excludes_study_targets(prep):
    feed = {"slug": "a"}
    retrieval = {
        "url": "https://www.citymeetings.fyi/a/audio_feed.xml",
        "error": None,
        "text": "<rss><channel><item><title>Target</title><guid>target</guid></item>"
        "<item><title>Other</title><guid>other</guid></item></channel></rss>",
    }
    result = prep.title_examples(feed, retrieval, {"target"}, {"Target"}, {"other"})
    assert result["examples"] == [{"title": "Other", "guid": "other"}]
    assert (
        prep.title_examples(feed, retrieval | {"error": "unavailable"}, set(), set(), set())[
            "retrieval_error"
        ]
        == "unavailable"
    )


def test_same_packet_content_and_bounded_choices(runner):
    entry = json.loads((PREP / "packet.json").read_text())["cases"][0]
    packet, job, sizes = runner.jev_job(entry, "binding")
    assert job.task == "judge"
    assert job.inputs["llm_policy"].purpose == "judge:anchor"
    assert job.inputs["llm_policy"].queue_only
    assert not job.inputs["llm_policy"].allow_paid
    assert job.inputs["llm_policy"].allowed_models == ("typesafe/jev-1.13",)
    assert job.inputs["systemone"]["state"]["context"] == entry
    assert json.loads(runner.chat_messages(entry)[1]["content"]) == entry
    choice = next(iter(job.inputs["systemone"]["questions"].values()))
    assert set(choice["criteria"]) == set("ABCD")
    assert len(packet.items) == 3
    assert sizes["state"] + max(sizes["questions"]) <= 28000
    assert "candidate" not in entry["proposals"]["A"]


def test_oversized_context_rejected_before_submission(runner):
    entry = json.loads((PREP / "packet.json").read_text())["cases"][0]
    entry["oversized"] = "enormous " * 150000
    with pytest.raises(ValueError, match="fit"):
        runner.jev_job(entry, "binding")


def test_unknown_intent_is_held_without_duplication(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CALLS", tmp_path)
    (tmp_path / "intents").mkdir()
    (tmp_path / "batches").mkdir()
    (tmp_path / "intents" / "one.json").write_text(json.dumps({"attempt_id": "one"}))
    rows, unknown = runner.history()
    assert not rows and len(unknown) == 1


def test_neither_candidate_is_not_forced_to_match_a_judge(runner):
    entry = {
        "proposals": {
            "A": {"publication_action": "assign_existing", "feed_slugs": ["a"]},
            "B": {"publication_action": "assign_existing", "feed_slugs": ["b"]},
        }
    }
    assert (
        runner.choice_for({"publication_action": "assign_existing", "feed_slugs": ["c"]}, entry)
        == "C"
    )
    assert runner.choice_for({"publication_action": "unresolved", "feed_slugs": []}, entry) == "D"
