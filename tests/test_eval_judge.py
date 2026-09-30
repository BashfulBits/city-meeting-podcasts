"""The judge-pilot lane (scripts/eval_judge.py): sizing, clients, builders, metrics, freeze, runs.

No test touches the network: every client takes an injected ``post``, ``sleep`` and ``clock``.
"""

from __future__ import annotations

import importlib.util
import json
import random
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "eval_judge", REPO_ROOT / "scripts" / "eval_judge.py"
)
ej = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ej)
ORIGINAL_LADDER = ej.freeze_context_ladder


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def jev_ok(answers=None):
    return FakeResponse(
        200,
        {
            "answers": answers or {"q": {"type": "noul", "noul": 0.9}},
            "usage": {"input_tokens": 10, "output_tokens": 1},
        },
    )


def small_body(n=2):
    return {
        "model": ej.JEV_MODEL,
        "state": {"task": "t"},
        "questions": {f"q{i}": ej.q_noul("Is it so?") for i in range(n)},
    }


# ----------------------------------------------------------------------------- sizing


def test_est_tokens_is_conservative_and_monotonic():
    assert ej.est_tokens("a b c") >= 3
    assert ej.est_tokens("word " * 1000) > ej.est_tokens("word " * 500)
    assert ej.est_tokens("x" * 4000) >= 1000


def test_pack_by_tokens_respects_ceiling_and_preserves_order():
    items = list(range(10))
    batches = ej.pack_by_tokens(items, lambda i: 4, 10)
    assert [len(b) for b in batches] == [2, 2, 2, 2, 2]
    assert [i for b in batches for i in b] == items
    assert ej.pack_by_tokens([], lambda i: 1, 10) == []
    assert ej.pack_by_tokens([99], lambda i: 99, 10) == [[99]]  # oversized travels alone


def test_jev_size_guard_rejects_both_ceilings_before_any_call():
    big_state = {
        "model": ej.JEV_MODEL,
        "state": {"t": "word " * 30_000},
        "questions": {"q": ej.q_noul("x")},
    }
    with pytest.raises(ej.JudgeError) as exc:
        ej.check_jev_size(big_state)
    assert exc.value.kind == "oversize"
    many = {
        "model": ej.JEV_MODEL,
        "state": {"task": "t"},
        "questions": {f"q{i}": ej.q_noul("word " * 3_000) for i in range(20)},
    }
    with pytest.raises(ej.JudgeError):
        ej.check_jev_size(many)  # total above the ceiling although no single part is huge
    ej.check_jev_size(small_body())


def test_client_never_sends_an_oversized_request():
    calls = []
    client = ej.JevClient("k", post=lambda *a, **k: calls.append(1) or jev_ok())
    body = {
        "model": ej.JEV_MODEL,
        "state": {"t": "word " * 30_000},
        "questions": {"q": ej.q_noul("x")},
    }
    with pytest.raises(ej.JudgeError):
        client.judge(body)
    assert calls == []


# ----------------------------------------------------------------------------- JEV client


def test_key_is_stripped_and_required():
    seen = {}

    def post(url, headers, json, timeout):  # noqa: A002
        seen.update(headers)
        return jev_ok()

    ej.JevClient("secret\r\n", post=post).judge(small_body())
    assert seen["Authorization"] == "Bearer secret"
    with pytest.raises(ej.JudgeError):
        ej.JevClient("  \r\n")


def test_successful_calls_are_paced_to_one_a_minute():
    clock = Clock()
    client = ej.JevClient("k", post=lambda *a, **k: jev_ok(), sleep=clock.sleep, clock=clock)
    client.judge(small_body())
    assert clock.slept == []
    client.judge(small_body())
    assert clock.slept and clock.slept[0] == pytest.approx(ej.JEV_MIN_INTERVAL_SECONDS)


def test_429_is_retried_once_using_retry_after():
    clock = Clock()
    responses = [FakeResponse(429, {"error": {}}, {"Retry-After": "30"}), jev_ok()]
    client = ej.JevClient(
        "k", post=lambda *a, **k: responses.pop(0), sleep=clock.sleep, clock=clock
    )
    assert client.judge(small_body())["answers"]
    assert 30.0 in clock.slept


def test_503_far_below_the_ceiling_is_retried_once():
    clock = Clock()
    bad = FakeResponse(503, {"error": {"code": "processing_failed", "retryable": True}})
    responses = [bad, jev_ok()]
    client = ej.JevClient(
        "k", post=lambda *a, **k: responses.pop(0), sleep=clock.sleep, clock=clock
    )
    assert client.judge(small_body())["answers"]


def test_503_near_a_ceiling_is_never_retried():
    clock = Clock()
    bad = FakeResponse(503, {"error": {"code": "processing_failed", "retryable": True}})
    calls = []
    client = ej.JevClient(
        "k", post=lambda *a, **k: calls.append(1) or bad, sleep=clock.sleep, clock=clock
    )
    near = {
        "model": ej.JEV_MODEL,
        "state": {"task": "t"},
        "questions": {f"q{i}": ej.q_noul("word " * 2_400) for i in range(17)},
    }
    size = ej.jev_size(near)
    assert size["total"] >= ej.JEV_OVERSIZE_SUSPECT_FRACTION * ej.JEV_TOTAL_CEILING_TOKENS
    assert size["total"] <= ej.JEV_TOTAL_CEILING_TOKENS
    with pytest.raises(ej.JudgeError) as exc:
        client.judge(near)
    assert exc.value.kind == "oversize_suspected"
    assert len(calls) == 1


@pytest.mark.parametrize("status,kind", [(401, "auth"), (400, "bad_request"), (500, "upstream")])
def test_error_classes(status, kind):
    client = ej.JevClient(
        "k", post=lambda *a, **k: FakeResponse(status, {"error": {"message": "m"}})
    )
    with pytest.raises(ej.JudgeError) as exc:
        client.judge(small_body())
    assert exc.value.kind == kind


def test_missing_answers_is_an_invalid_response():
    client = ej.JevClient("k", post=lambda *a, **k: FakeResponse(200, {"usage": {}}))
    with pytest.raises(ej.JudgeError) as exc:
        client.judge(small_body())
    assert exc.value.kind == "invalid_response"


# ----------------------------------------------------------------------------- Groq client


def groq_ok(content, completion_tokens=500):
    return FakeResponse(
        200,
        {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"completion_tokens": completion_tokens},
        },
    )


def test_groq_rejects_an_oversized_prompt_without_calling():
    calls = []
    client = ej.GroqClient("k", post=lambda *a, **k: calls.append(1) or groq_ok({}))
    with pytest.raises(ej.JudgeError) as exc:
        client.answer("word " * 6_000, ej.CHOOSE_SCHEMA)
    assert exc.value.kind == "oversize" and calls == []


def test_groq_output_budget_spaces_reasoning_calls():
    clock = Clock()
    client = ej.GroqClient(
        "k",
        post=lambda *a, **k: groq_ok({"best": "A", "reason": "r"}, 700),
        sleep=clock.sleep,
        clock=clock,
    )
    client.answer("pick", ej.CHOOSE_SCHEMA)
    assert clock.slept == []
    client.answer("pick", ej.CHOOSE_SCHEMA)  # 700 + 700 expected > 900 per minute
    assert clock.slept and clock.slept[0] > 0


def test_groq_429_retries_once_and_bad_json_is_invalid():
    clock = Clock()
    responses = [FakeResponse(429, {}), groq_ok({"best": "B", "reason": "r"})]
    client = ej.GroqClient(
        "k", post=lambda *a, **k: responses.pop(0), sleep=clock.sleep, clock=clock
    )
    assert client.answer("pick", ej.CHOOSE_SCHEMA)["content"]["best"] == "B"
    assert 65.0 in clock.slept
    bad = ej.GroqClient(
        "k", post=lambda *a, **k: FakeResponse(200, {"choices": [{"message": {"content": "{"}}]})
    )
    with pytest.raises(ej.JudgeError) as exc:
        bad.answer("pick", ej.CHOOSE_SCHEMA)
    assert exc.value.kind == "invalid_response"


# ----------------------------------------------------------------------------- builders


def test_bundling_layouts_place_evidence_where_they_say():
    items = ej.bundling_items()
    question = ej.bundling_body(items[:3], "question")
    state = ej.bundling_body(items[:3], "state")
    assert "evidence" not in question["state"]
    assert all(it["text"] in question["questions"][it["id"]]["instructions"] for it in items[:3])
    assert set(state["state"]["evidence"]) == {it["id"] for it in items[:3]}
    assert all(it["text"] not in state["questions"][it["id"]]["instructions"] for it in items[:3])
    with pytest.raises(ValueError):
        ej.bundling_body(items, "other")


def test_bundling_items_are_deterministic_and_balanced():
    a, b = ej.bundling_items(), ej.bundling_items()
    assert a == b and len(a) == 30
    kinds = [i["kind"] for i in a]
    assert kinds.count("supported") == kinds.count("near_miss") == kinds.count("off_topic") == 10
    assert sum(i["truth"] for i in a) == 10
    assert len({i["id"] for i in a}) == 30


def test_question_type_builders_use_the_jev_shapes():
    meetings = [
        {
            "uid": "u1",
            "title": "t",
            "quotes": [
                {
                    "id": f"m0q{i}",
                    "quote": f"quote {i}",
                    "why": "w",
                    "dur": 10.0,
                    "producer_score": 0.5,
                }
                for i in range(4)
            ],
        }
    ]
    body, maps = ej.question_types_body(meetings, 1)
    qs = body["questions"]
    assert qs["pub_m0q0"]["type"] == "noul" and qs["pub_m0q0"]["criteria"] == {}
    assert qs["score_m0q0"]["type"] == "score" and qs["score_m0q0"]["criteria"] == list(
        ej.GRADE_LEVELS
    )
    assert qs["best_u1"]["type"] == "choice" and set(qs["best_u1"]["criteria"]) == set("ABCD")
    assert sorted(maps["best_u1"].values()) == [f"m0q{i}" for i in range(4)]
    body2, maps2 = ej.question_types_body(meetings, 2)
    assert maps["best_u1"] != maps2["best_u1"] or body["questions"] != body2["questions"]


def test_choose_prompt_maps_letters_back_to_ids_and_adjudicate_prompt_lists_ids():
    meeting = {"quotes": [{"id": f"q{i}", "quote": f"text {i}", "why": "w"} for i in range(4)]}
    text, mapping = ej.choose_prompt(meeting, random.Random(1))
    assert set(mapping) == set("ABCD") and sorted(mapping.values()) == ["q0", "q1", "q2", "q3"]
    assert all(f"text {i}" in text for i in range(4))
    items = ej.bundling_items()[:2]
    prompt = ej.adjudicate_prompt(items)
    assert all(f"ID {it['id']}" in prompt for it in items)


# ----------------------------------------------------------------------------- metrics


def test_metrics_on_hand_computed_cases():
    assert ej.auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert ej.auc([0.5], [0.5]) == 0.5
    assert ej.auc([], [0.1]) is None
    truth = {"a": True, "b": True, "c": False, "d": False}
    answers = {"a": 0.9, "b": 0.4, "c": 0.2, "d": 0.6}
    assert ej.accuracy_at(answers, truth) == 0.5
    assert ej.verdict_flips({"a": 0.9, "b": 0.4}, {"a": 0.2, "b": 0.6}) == 2
    assert ej.mean_abs_diff({"a": 0.9}, {"a": 0.7}) == pytest.approx(0.2)
    sep = ej.separation_metrics(answers, truth)
    assert sep["max_false"] == 0.6 and sep["min_true"] == 0.4 and sep["accuracy_at_0.5"] == 0.5


# ----------------------------------------------------------------------------- tiers and freeze


VTT = """WEBVTT

00:00:01.000 --> 00:00:05.000
Welcome to the meeting.

00:00:05.000 --> 00:00:10.000
We will discuss the short-term rental ordinance.

00:01:30.000 --> 00:01:40.000
Staff will present the fiscal impact of the ordinance now.

00:05:00.000 --> 00:05:10.000
Final item.
"""


def test_parse_vtt_and_cap_words():
    cues = ej.parse_vtt(VTT)
    assert cues[0] == (1.0, 5.0, "Welcome to the meeting.")
    assert len(cues) == 4
    assert ej.cap_words("a b c d e", 3) == "a b c"
    assert ej.cap_words("a b c d e", 3, center=True) == "b c d"
    assert ej.cap_words("a b", 3) == "a b"


def test_build_tiers_caps_a_giant_cue_and_locates_the_chapter():
    cues = ej.parse_vtt(VTT)
    giant = (200.0, 210.0, "word " * 5_000)
    tiers = ej.build_tiers(
        cues + [giant],
        [{"start": 0, "title": "Opening"}, {"start": 60, "title": "Ordinance"}],
        [95.0],
    )
    assert tiers["chapter"] == "Ordinance"
    assert "fiscal impact" in tiers["T1"]
    assert len(tiers["T0"].split()) <= ej.T0_WORD_CAP
    assert len(tiers["T1"].split()) <= ej.T1_WORD_CAP
    assert len(tiers["T2"].split()) <= ej.T2_WORD_CAP
    assert ej.build_tiers([], [{"start": 0}], [1.0]) is None
    assert ej.build_tiers(cues, [{"start": 0}], []) is None


def write_state(tmp_path: Path, taxonomy: dict):
    source = tmp_path / "state" / "sources" / "abc"
    source.mkdir(parents=True)
    episodes = {}
    tag_ids = list(taxonomy)
    for n in range(8):
        tag = tag_ids[n % len(tag_ids)]
        episodes[f"u{n}"] = {
            "uid": f"u{n}",
            "title": f"Meeting {n}",
            "transcript": {"url": f"https://example.test/{n}.vtt", "format": "vtt"},
            "chapters": [{"start": 0, "title": "Opening"}, {"start": 60, "title": "Ordinance"}],
            "llm_tag_candidates": [
                {
                    "source_kind": "rule",
                    "scope": "chapter",
                    "id": tag,
                    "rule_pattern": "rental",
                    "chapter_id": "c",
                    "evidence": [{"t": 95.0, "span": "rental"}],
                }
            ],
            "moments": {
                "pullquote_candidates": [
                    {
                        "quote": f"A sufficiently long pull quote number {q} for meeting {n}.",
                        "why": "because",
                        "start": 10.0 * q,
                        "end": 10.0 * q + 12,
                        "quality_score": 0.8,
                    }
                    for q in range(5)
                ]
            },
        }
    (source / "episodes.json").write_text(json.dumps({"episodes": episodes}))
    return tmp_path / "state"


def taxonomy():
    return {
        f"tag-{i}": {
            "id": f"tag-{i}",
            "label": f"Tag {i}",
            "description": "def " * 3,
            "group": f"g{i % 3}",
        }
        for i in range(6)
    }


def patch_eval(tmp_path, monkeypatch):
    """Point the lane at a temp dir with a synthetic taxonomy (no network, no repo files)."""
    tax = taxonomy()
    monkeypatch.setattr(ej, "EVAL_DIR", tmp_path / "evals")
    monkeypatch.setattr(ej, "MANIFEST_PATH", tmp_path / "evals" / "manifest.json")
    monkeypatch.setattr(ej, "GOLD_PATH", tmp_path / "evals" / "gold.json")
    monkeypatch.setattr(
        ej, "freeze_context_ladder", lambda s, f, **k: ORIGINAL_LADDER(s, f, taxonomy=tax)
    )
    return write_state(tmp_path, tax)


def test_freeze_is_deterministic_builds_controls_and_refuses_overwrite(tmp_path, monkeypatch):
    state = patch_eval(tmp_path, monkeypatch)
    summary = ej.freeze(state, force=False, fetch_text=lambda url: VTT)
    assert summary["bundling"] == 30 and summary["meetings"] == 6
    assert summary["controls"] > 0 and summary["context_items"] > summary["controls"]
    manifest_path = tmp_path / "evals" / "manifest.json"
    first = manifest_path.read_text()
    with pytest.raises(SystemExit):
        ej.freeze(state, force=False, fetch_text=lambda url: VTT)
    ej.freeze(state, force=True, fetch_text=lambda url: VTT)
    assert manifest_path.read_text() == first
    gold = json.loads((tmp_path / "evals" / "gold.json").read_text())
    manifest = json.loads(first)
    assert len(gold["bundling"]) == 30
    by_id = {i["id"]: i for i in manifest["context_ladder"]["items"]}
    for control_id in gold["context_ladder"]["controls"]:
        control = by_id[control_id]
        assert control["control"] is True
    assert all("truth" not in i for i in manifest["bundling"]["items"])  # inputs only


def test_freeze_question_types_needs_four_quotes_and_is_seeded(tmp_path):
    state = write_state(tmp_path, taxonomy())
    a = ej.freeze_question_types(state, meetings=3)
    b = ej.freeze_question_types(state, meetings=3)
    assert a == b and len(a) == 3
    assert all(len(m["quotes"]) == 4 for m in a)
    assert len({q["id"] for m in a for q in m["quotes"]}) == 12


# ----------------------------------------------------------------------------- runs


def inputs(tmp_path, monkeypatch):
    state = patch_eval(tmp_path, monkeypatch)
    ej.freeze(state, force=True, fetch_text=lambda url: VTT)
    return ej.load_inputs()


def truthful_jev(manifest, gold):
    truth = gold["bundling"]

    def post(url, headers, json, timeout):  # noqa: A002
        answers = {}
        for name in json["questions"]:
            answers[name] = {"type": "noul", "noul": 0.95 if truth.get(name) else 0.03}
        return FakeResponse(
            200, {"answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}
        )

    return post


def test_bundling_run_scores_both_layouts_and_writes_the_schema(tmp_path, monkeypatch):
    manifest, gold = inputs(tmp_path, monkeypatch)
    clock = Clock()
    jev = ej.JevClient("k", post=truthful_jev(manifest, gold), sleep=clock.sleep, clock=clock)
    result = ej.run_bundling(manifest, gold, jev)
    assert (
        result["calls_asked"] == 4 and result["calls_answered"] == 4 and not result["inconclusive"]
    )
    for key in ("question-order1", "state-order1", "question-order2", "state-order2"):
        assert (
            result["metrics"][key]["accuracy_at_0.5"] == 1.0
            and result["metrics"][key]["auc"] == 1.0
        )
    assert result["metrics"]["question-order1_vs_state-order1"]["verdict_flips"] == 0
    assert result["set_version"] == ej.SET_VERSION and result["prompt_version"] == ej.PROMPT_VERSION


def test_more_than_ten_percent_unanswered_is_inconclusive(tmp_path, monkeypatch):
    manifest, gold = inputs(tmp_path, monkeypatch)
    clock = Clock()
    jev = ej.JevClient(
        "k", post=lambda *a, **k: FakeResponse(500, {"error": {}}), sleep=clock.sleep, clock=clock
    )
    result = ej.run_bundling(manifest, gold, jev)
    assert result["inconclusive"] is True and result["calls_answered"] == 0


def test_context_ladder_run_reports_controls_cost_and_backfill_projection(tmp_path, monkeypatch):
    manifest, gold = inputs(tmp_path, monkeypatch)
    controls = set(gold["context_ladder"]["controls"])

    def post(url, headers, json, timeout):  # noqa: A002
        answers = {
            n: {"type": "noul", "noul": 0.02 if n in controls else 0.9} for n in json["questions"]
        }
        return FakeResponse(
            200, {"answers": answers, "usage": {"input_tokens": 5, "output_tokens": 1}}
        )

    clock = Clock()
    jev = ej.JevClient("k", post=post, sleep=clock.sleep, clock=clock)
    result = ej.run_context_ladder(manifest, gold, jev)
    assert not result["inconclusive"]
    for tier in ej.CONTEXT_TIERS:
        metrics = result["metrics"][tier]
        assert metrics["controls_accepted_at_0.5"] == 0
        assert metrics["auc_real_vs_controls"] == 1.0 and metrics["controls"] > 0
        assert result["plan"][tier]["items_per_call_at_ceiling"] >= 1
        assert result["plan"][tier]["projected_calls_for_backfill"] >= 1
    assert result["metrics"]["T0_vs_T2_real_items"]["verdict_flips"] == 0
    # bigger context costs more tokens per item and so fits fewer items in one call
    assert (
        result["plan"]["T2"]["est_tokens_per_item"] >= result["plan"]["T0"]["est_tokens_per_item"]
    )


def test_question_types_run_and_cross_judge_comparison(tmp_path, monkeypatch):
    manifest, gold = inputs(tmp_path, monkeypatch)
    meetings = manifest["question_types"]["meetings"]

    def post(url, headers, json, timeout):  # noqa: A002
        answers = {}
        for name, q in json["questions"].items():
            if q["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.6}
            elif q["type"] == "score":
                answers[name] = {"type": "score", "score": 3.0, "confidence": 0.1}
            else:
                first = sorted(q["criteria"])[0]
                answers[name] = {
                    "type": "choice",
                    "choice": first,
                    "confidence": 1,
                    "probabilities": {o: 0.25 for o in q["criteria"]},
                }
        return FakeResponse(
            200, {"answers": answers, "usage": {"input_tokens": 1, "output_tokens": 1}}
        )

    clock = Clock()
    jev = ej.JevClient("k", post=post, sleep=clock.sleep, clock=clock)
    result = ej.run_question_types(manifest, gold, "jev", jev, None)
    assert result["calls_answered"] == 2
    metrics = result["metrics"]
    assert metrics["meetings"] == len(meetings)
    assert 0 <= metrics["winner_stable_across_orders"] <= len(meetings)
    assert metrics["publish_probability_range"] == [0.6, 0.6]

    def groq_post(url, headers, json, timeout):  # noqa: A002
        return groq_ok({"best": "A", "reason": "r"}, 100)

    qwen = ej.GroqClient("k", post=groq_post, sleep=clock.sleep, clock=clock)
    qwen_result = ej.run_question_types(manifest, gold, "qwen", None, qwen)
    assert len(qwen_result["winners"]) == len(meetings)
    comparison = ej.compare_winners(result, qwen_result)
    assert len(comparison["qwen_equals_jev_winner_by_order"]) == 2


def test_adjudicator_run_scores_against_truth(tmp_path, monkeypatch):
    manifest, gold = inputs(tmp_path, monkeypatch)
    truth = gold["bundling"]

    def post(url, headers, json, timeout):  # noqa: A002
        prompt = json["messages"][0]["content"]
        ids = [line.split()[1] for line in prompt.split("\n") if line.startswith("ID ")]
        return groq_ok(
            {"verdicts": [{"id": i, "supported": truth[i], "quote": "q"} for i in ids]}, 100
        )

    clock = Clock()
    qwen = ej.GroqClient("k", post=post, sleep=clock.sleep, clock=clock)
    result = ej.run_adjudicator(manifest, gold, qwen)
    assert result["metrics"] == {"correct": 30, "items": 30, "answered_items": 30}
    assert result["calls_asked"] == 6 and not result["inconclusive"]


def test_dry_run_never_calls_a_judge(tmp_path, monkeypatch, capsys):
    inputs(tmp_path, monkeypatch)
    for experiment, judge in (
        ("bundling", "jev"),
        ("question-types", "jev"),
        ("question-types", "qwen"),
        ("context-ladder", "jev"),
        ("adjudicator", "qwen"),
    ):
        assert ej.main(["run", experiment, "--judge", judge, "--dry-run"]) == 0
    assert "plan" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        ej.main(["run", "bundling", "--judge", "qwen", "--dry-run"])


def test_report_summarizes_a_result_file(tmp_path, capsys):
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps(
            ej.finish(
                "bundling", "jev", {"metrics": {"x": {"auc": 1.0}}}, answered=4, asked=4, errors=[]
            )
        )
    )
    assert ej.main(["report", str(path)]) == 0
    out = capsys.readouterr().out
    assert "bundling / jev" in out and '"auc": 1.0' in out


# ----------------------------------------------------------------------------- adjudicated metrics


def test_tier_accuracy_and_escalation_simulation_on_hand_computed_cases():
    answers = {
        "T1": {"a": 0.9, "b": 0.5, "c": 0.1, "d": 0.6, "x": 0.02},
        "T2": {"a": 0.8, "b": 0.2, "c": 0.3, "d": 0.4, "x": 0.03},
    }
    labels = {"a": True, "b": False, "c": False, "d": True}
    accuracy = ej.tier_accuracy_on_labels(answers, labels)
    assert accuracy["T1"] == {"labeled_items": 4, "correct": 3, "accuracy": 0.75}
    assert accuracy["T2"]["correct"] == 3
    sim = ej.simulate_escalation(
        answers, labels, ["a", "b", "c", "d"], first="T1", then="T2", low=0.4, high=0.7
    )
    assert sim["escalated"] == 2 and sim["escalated_fraction"] == 0.5  # b (0.5) and d (0.6)
    # b -> T2 0.2 (correct), d -> T2 0.4 (wrong), a and c keep their T1 verdicts (correct)
    assert sim["correct_on_labeled"] == 3 and sim["labeled_items"] == 4
    empty = ej.simulate_escalation(answers, {}, [], first="T1", then="T2", low=0.3, high=0.7)
    assert empty["escalated_fraction"] is None and empty["accuracy_on_labeled"] is None


def test_derived_context_metrics_need_labels_and_exclude_controls():
    result = {
        "answers": {
            "T0": {"r1": 0.9, "r2": 0.4, "c1": 0.05},
            "T1": {"r1": 0.8, "r2": 0.45, "c1": 0.05},
            "T2": {"r1": 0.9, "r2": 0.2, "c1": 0.05},
        }
    }
    gold = {"context_ladder": {"controls": ["c1"], "adjudicated": {"r1": True, "r2": False}}}
    derived = ej.derive_context_metrics(result, gold)
    assert derived["accuracy_on_adjudicated"]["T2"]["accuracy"] == 1.0
    assert derived["escalation_T1_to_T2"][0]["real_items"] == 2  # the control is not a real item
    assert ej.derive_context_metrics(result, {"context_ladder": {"controls": []}}) == {}
    assert ej.derive_context_metrics({"answers": {}}, gold) == {}


def test_report_with_gold_and_compare_command(tmp_path, monkeypatch, capsys):
    result = {
        "experiment": "context-ladder",
        "judge": "jev",
        "generated_at": "2026-09-30T00:00:00+00:00",
        "set_version": 1,
        "git_sha": "x",
        "errors": [],
        "inconclusive": False,
        "answers": {"T1": {"r1": 0.9}, "T2": {"r1": 0.9}},
    }
    gold = {"context_ladder": {"controls": [], "adjudicated": {"r1": True}}}
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold))
    result_path = tmp_path / "r.json"
    result_path.write_text(json.dumps(result))
    monkeypatch.setattr(ej, "GOLD_PATH", gold_path)
    assert ej.main(["report", str(result_path)]) == 0
    assert "accuracy_on_adjudicated" in capsys.readouterr().out
    jev = {"runs": [{"answers": {"best_u1": {"choice": "A"}}, "maps": {"best_u1": {"A": "q0"}}}]}
    qwen = {"winners": {"u1": "q0"}}
    (tmp_path / "j.json").write_text(json.dumps(jev))
    (tmp_path / "q.json").write_text(json.dumps(qwen))
    assert ej.main(["compare", str(tmp_path / "j.json"), str(tmp_path / "q.json")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"qwen_equals_jev_winner_by_order": [1], "meetings": 1}


def test_uncertain_labels_are_excluded_and_the_rubric_is_in_the_tag_question():
    answers = {"T1": {"a": 0.9, "b": 0.9}, "T2": {"a": 0.9, "b": 0.1}}
    labels = {"a": True, "b": None}  # None = Uncertain
    accuracy = ej.tier_accuracy_on_labels(answers, labels)
    assert accuracy["T1"] == {"labeled_items": 1, "correct": 1, "accuracy": 1.0}
    sim = ej.simulate_escalation(
        answers, labels, ["a", "b"], first="T1", then="T2", low=0.3, high=0.7
    )
    assert sim["labeled_items"] == 1
    item = {"label": "L", "desc": "D", "chapter": "C", "T0": "t0", "T1": "t1", "T2": "t2"}
    text = ej.tag_question(item, "T1")
    assert "specific project, contract, program or policy" in text and "t1" in text
    assert "general-purpose services contract" in text


def test_result_path_records_a_non_default_prompt_version(monkeypatch):
    monkeypatch.setattr(ej, "PROMPT_VERSION", "1")
    assert ej.result_path("bundling", "jev", None).name.endswith("-bundling-jev.json")
    monkeypatch.setattr(ej, "PROMPT_VERSION", "2")
    assert ej.result_path("bundling", "jev", None).name.endswith("-bundling-jev-p2.json")
    assert ej.result_path("bundling", "jev", "x.json") == Path("x.json")
