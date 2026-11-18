"""Request building, scoring and the cloud worker's fake-model paths (review/60 Phase 3)."""

import importlib.util
from pathlib import Path

import pytest

from citypods.judging import moment_task, tag_task
from citypods.judging import open_weight_eval as ev

ROOT = Path(__file__).resolve().parents[1]


def _worker():
    spec = importlib.util.spec_from_file_location(
        "ow_worker", ROOT / "workers/decision-batch/open_weight_eval_worker.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rubrics_match_production():
    assert ev.TAG_RUBRIC == tag_task.TAG_RUBRIC
    assert ev.MOMENT_RUBRIC == moment_task.SUPPORTED.instruction


def _keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


def test_requests_carry_inputs_only():
    manifest = ev.load_manifest()
    allowed = {"state", "questions", "type", "instructions", "supported"}
    for split in ev.SPLITS:
        for key, request in ev.build_requests(manifest, split).items():
            names = set(_keys(request))
            # only the request skeleton and tag ids (chapter questions are keyed by tag id)
            assert names <= allowed or key.startswith("chapters/"), key
            assert not names & {"stratum", "label", "judge_verdicts", "suggested_tags", "gold"}


def test_request_counts_and_modes():
    manifest = ev.load_manifest()
    grouped = ev.build_requests(manifest, "dev")
    single = ev.build_requests(manifest, "dev", chapter_mode="single")
    tags = [k for k in grouped if k.startswith("tags/")]
    assert len(tags) == sum(1 for t in manifest["tags"] if t["split"] == "dev")
    chapters = [c for c in manifest["chapters"] if c["split"] == "dev"]
    assert sum(1 for k in grouped if k.startswith("chapters/")) == len(chapters)
    assert sum(1 for k in single if k.startswith("chapters/")) == sum(
        len(c["tags"]) for c in chapters
    )
    key = f"chapters/{chapters[0]['chapter_id']}"
    assert len(ev.answer_keys(key, grouped[key])) == len(chapters[0]["tags"])
    assert ev.answer_keys("tags/x", grouped[tags[0]]) == ["tags/x"]
    with pytest.raises(ValueError):
        ev.build_requests(manifest, "test")


def test_fold_for_openrouter_keeps_chapter_state():
    single = {"state": "EVIDENCE", "questions": {"q": {"type": "noul", "instructions": "ASK"}}}
    folded = ev.fold_for_openrouter(single)
    assert folded["state"] == {}
    assert folded["questions"]["q"]["instructions"] == "ASK\n\nEVIDENCE"
    chapter = {"state": "CH", "questions": {"a": {}, "b": {}}}
    assert ev.fold_for_openrouter(chapter) is chapter


def test_metrics_basics():
    assert ev.brier([(1.0, True), (0.0, False)]) == 0
    assert ev.brier([(0.0, True)]) == 1
    assert ev.ece([(0.9, True)] * 9 + [(0.9, False)]) == pytest.approx(0)
    assert ev.ece([(1.0, False)]) == 1


def test_bootstrap_is_deterministic_and_paired():
    values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0]
    mean = lambda ix: sum(values[i] for i in ix) / len(ix)  # noqa: E731
    first = ev.bootstrap_ci(8, mean, reps=200)
    assert first == ev.bootstrap_ci(8, mean, reps=200)
    assert 0 <= first[0] <= 0.75 <= first[1] <= 1
    clustered = ev.bootstrap_ci(8, mean, clusters=[0, 0, 1, 1, 2, 2, 3, 3], reps=200)
    assert clustered[0] <= clustered[1]


def _rows(n, stratum=None):
    return [
        {
            "key": f"tags/{i}",
            "truth": i % 2 == 0,
            "uncertain": i == 5,
            "stratum": stratum,
            "cluster": str(i),
            "gold_source": "human",
        }
        for i in range(n)
    ]


def test_score_lane_excludes_uncertain_and_counts_invalid():
    rows = _rows(8)
    answers = {r["key"]: {"p": 0.9 if r["truth"] else 0.1, "status": "ok"} for r in rows}
    answers["tags/0"] = {"p": None, "status": "too_large"}
    result = ev.score_lane("tags", rows, answers, reps=50)
    assert result["excluded_uncertain"] == 1 and result["scored"] == 7
    assert result["invalid_rate"] == pytest.approx(1 / 7, abs=1e-3)
    assert result["accuracy_valid"] == 1.0
    assert result["accuracy_strict"] == pytest.approx(6 / 7)


def test_score_lane_agreement_and_difference_against_reference():
    rows = _rows(8)
    good = {r["key"]: {"p": 0.9 if r["truth"] else 0.1} for r in rows}
    flip = {r["key"]: {"p": 0.1 if r["truth"] else 0.9} for r in rows}
    result = ev.score_lane("tags", rows, good, flip, reps=50)
    assert result["agreement_with_reference"] == 0
    assert result["accuracy_minus_reference"] == 1.0


def test_tag_strata_reweighted_to_production_mix():
    rows = []
    for stratum, count, truth in (("yes", 2, True), ("no", 4, False), ("split", 2, True)):
        for i in range(count):
            rows.append(
                {
                    "key": f"tags/{stratum}{i}",
                    "truth": truth,
                    "uncertain": False,
                    "stratum": stratum,
                    "cluster": f"{stratum}{i}",
                    "gold_source": "human",
                }
            )
    # always answers "supported": right on yes and split, wrong on no
    answers = {r["key"]: {"p": 0.9} for r in rows}
    result = ev.score_lane("tags", rows, answers, reps=20)
    mix = ev.PRODUCTION_STRATUM_MIX
    assert result["accuracy_strict"] == pytest.approx(mix["yes"] + mix["split"])


def test_chapter_lane_f1_and_tokens_per_tag():
    rows = [
        {
            "key": f"chapters/c/{t}",
            "truth": t == "a",
            "uncertain": False,
            "stratum": None,
            "cluster": "c",
            "gold_source": "claude",
        }
        for t in "abcd"
    ]
    answers = {
        f"chapters/c/{t}": {"p": 0.8 if t in "ab" else 0.2, "tokens": 400, "request": "chapters/c"}
        for t in "abcd"
    }
    result = ev.score_lane("chapters", rows, answers, reps=20)
    assert (result["precision"], result["recall"]) == (0.5, 1.0)
    assert ev.tokens_per_tag(answers, 4) == 100


def test_gold_rows_join_every_manifest_item():
    manifest, gold, reference = ev.load_manifest(), ev.load_gold(), ev.load_reference()
    for lane in ev.LANES:
        rows = ev.gold_rows(lane, manifest, gold, reference, "dev")
        assert rows and len({r["key"] for r in rows}) == len(rows)
    tags = ev.gold_rows("tags", manifest, gold, reference, "dev")
    assert {r["stratum"] for r in tags} == {"yes", "no", "split"}
    moments = {r["key"] for r in ev.gold_rows("moments", manifest, gold, reference, "dev")}
    assert moments == {f"moments/{m['id']}" for m in manifest["moments"] if m["split"] == "dev"}
    chapters = {r["key"] for r in ev.gold_rows("chapters", manifest, gold, reference, "all")}
    assert chapters == {
        f"chapters/{c['chapter_id']}/{t['id']}" for c in manifest["chapters"] for t in c["tags"]
    }


class _FakeDecider:
    def __init__(self):
        self.calls = []

    def plan(self, request):
        if request["state"] == "HUGE":
            raise LookupError("too long")
        return [request]

    def decide(self, requests, plans=None):
        self.calls.append(len(requests))
        return [
            {"answers": {q: {"noul": 0.75} for q in r["questions"]}, "usage": {"input_tokens": 7}}
            for r in requests
        ]


def test_worker_karpo_runner_maps_errors_and_batches():
    worker = _worker()
    fake = _FakeDecider()
    runner = worker.KarpoRunner(fake, LookupError, KeyError)
    request = {"state": "s", "questions": {"supported": {}}}
    replies = runner.run([request, {"state": "HUGE", "questions": {}}, request])
    assert replies[1] == "too_large" and fake.calls == [2]
    docs = worker.answer_docs(
        {"tags/a": request, "tags/b": {"state": "HUGE", "questions": {}}, "tags/c": request},
        replies,
        {},
    )
    assert docs["tags/a"]["p"] == 0.75 and docs["tags/a"]["tokens"] == 7
    assert docs["tags/b"] == {
        "request": "tags/b",
        "seconds": None,
        "p": None,
        "status": "too_large",
    }


def test_worker_answer_docs_fan_out_chapter_questions():
    worker = _worker()
    request = {"state": "s", "questions": {"x-tag": {}, "y-tag": {}}}
    reply = {
        "answers": {"x-tag": {"noul": 0.2}, "y-tag": {"noul": 0.9}},
        "usage": {"input_tokens": 9},
    }
    docs = worker.answer_docs({"chapters/c1": request}, [reply], {"chapters/c1": 0.5})
    assert docs["chapters/c1/x-tag"]["p"] == 0.2 and docs["chapters/c1/y-tag"]["p"] == 0.9
    assert docs["chapters/c1/y-tag"]["request"] == "chapters/c1"


def test_probability_rejects_overflow_and_out_of_range():
    assert ev._prob({"p": 10**400, "status": "ok"}) is None
    assert ev._prob({"p": 1.2}) is None
    assert ev._prob({"p": True}) is None
    assert ev._prob({"p": float("nan")}) is None
    assert ev._prob({"p": 0.25}) == 0.25


def test_chapter_recall_counts_a_missing_answer_as_a_miss():
    rows = [
        {
            "key": f"chapters/c/{t}",
            "truth": True,
            "uncertain": False,
            "stratum": None,
            "cluster": "c",
            "gold_source": "claude",
        }
        for t in "ab"
    ]
    answers = {"chapters/c/a": {"p": 0.9}, "chapters/c/b": {"p": None, "status": "missing"}}
    result = ev.score_lane("chapters", rows, answers, reps=10)
    assert result["recall"] == 0.5 and result["precision"] == 1.0


def test_stratified_mean_keeps_stratum_weights_when_an_episode_repeats():
    mix = ev.PRODUCTION_STRATUM_MIX
    values = [1.0, 0.0, 0.0, 1.0]
    strata = ["yes", "yes", "no", "no"]
    full = ev._group_mean(values, strata, range(4))
    assert full == pytest.approx((mix["yes"] * 0.5 + mix["no"] * 0.5) / (mix["yes"] + mix["no"]))
    # drawing the first yes row twice changes that stratum's mean, not the stratum's weight
    twice = ev._group_mean(values, strata, [0, 0, 2, 3])
    assert twice == pytest.approx((mix["yes"] * 1.0 + mix["no"] * 0.5) / (mix["yes"] + mix["no"]))


def test_run_api_helpers_and_split_check(tmp_path):
    from scripts import open_weight_eval as cli

    assert cli._probability({"noul": 10**400}) is None
    assert cli._probability({"noul": 1.2}) is None
    assert cli._probability({"noul": 0.4}) == 0.4
    cli._write_atomic(tmp_path / "a.json", {"split": "dev", "answers": {"tags/x": {"p": 0.5}}})
    assert not (tmp_path / "a.json.tmp").exists()
    with pytest.raises(SystemExit):
        cli.merge_answers([str(tmp_path / "a.json")], "holdout")
    assert cli.merge_answers([str(tmp_path / "a.json")], "all")["tags/x"]["p"] == 0.5


def test_no_redirect_handler_refuses_to_follow():
    from scripts import open_weight_eval as cli

    handler = cli._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere") is None
