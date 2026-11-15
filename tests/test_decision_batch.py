import json
from dataclasses import replace

import pytest

from citypods.judging.backends import Item
from citypods.judging.decision_batch import (
    build_items,
    canonical_json,
    native_request,
    normalize_results,
    serialize_jsonl,
)
from citypods.judging.tag_task import SUPPORTED
from citypods.judging.tasks import Subject, make_evidence


def item(name="one"):
    subject = Subject("tag", name, "episode", None, None, {"truth": True})
    return Item(SUPPORTED, "T1", make_evidence("T1", "évidence"), (subject,))


def response(row, **answer):
    return {
        "model": "glide-9b",
        "answers": {row["item_id"]: {"type": "noul", "noul": 0.775, **answer}},
        "usage": {"input_tokens": 316, "output_tokens": 0},
    }


def test_rows_are_deterministic_content_addressed_and_gold_free():
    first = item()
    rows = build_items([first, item("two")])
    assert rows == build_items([item("two"), first])
    assert [r["item_id"] for r in rows] == sorted(r["item_id"] for r in rows)
    line = serialize_jsonl(rows)
    assert "évidence" in line and "truth" not in line
    assert [json.loads(x) for x in line.splitlines()] == rows
    assert line == serialize_jsonl(list(reversed(rows)))
    assert (
        build_items([first])[0]["item_id"]
        != build_items([replace(first, evidence=make_evidence("T1", "different"))])[0]["item_id"]
    )
    changed = replace(first.question, instruction="different rubric")
    assert (
        build_items([first])[0]["item_id"]
        != build_items([replace(first, question=changed)])[0]["item_id"]
    )
    with pytest.raises(ValueError):
        build_items([first, first])
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_native_request_and_round_trip():
    rows = build_items([item(), item("two")])
    row = rows[0]
    assert native_request(row) == {
        "state": "évidence",
        "questions": {row["item_id"]: {"type": "noul", "instructions": SUPPORTED.instruction}},
    }
    replies = [response(rows[1]), response(rows[0], confidence=0.549)]
    result = normalize_results(rows, replies)
    assert [r["item_id"] for r in result] == [r["item_id"] for r in rows]
    assert result[0] == {
        "schema_version": 1,
        "item_id": row["item_id"],
        "status": "ok",
        "answer": {"type": "noul", "noul": 0.775, "confidence": 0.549},
        "input_tokens": 316,
    }
    with pytest.raises(ValueError):
        native_request({**row, "question": {"type": "choice", "instructions": "x"}})


def test_native_request_rejects_tampering_and_nontext_state():
    row = build_items([item()])[0]
    with pytest.raises(ValueError, match="item ID does not match content"):
        native_request({**row, "state": "different evidence"})
    with pytest.raises(ValueError, match="text evidence"):
        native_request({**row, "state": ["image"]})
    with pytest.raises(ValueError, match="schema version"):
        native_request({**row, "schema_version": True})


def test_launch_modes_share_a_job_and_manifest():
    from scripts.eval_decision_batch import cloud_run_command

    fields = {
        "job": "projects/test/locations/us-central1/jobs/decision-batch",
        "region": "us-central1",
        "manifest_id": "m",
        "attempt_id": "a",
    }
    regular = cloud_run_command(mode="regular", **fields)
    delayed = cloud_run_command(mode="delayed", **fields)
    assert "--async" in regular and "--wait" not in regular
    assert "--no-delay-execution" in regular
    assert "--delay-execution" in delayed
    regular.remove("--no-delay-execution")
    delayed.remove("--delay-execution")
    assert regular == delayed


@pytest.mark.parametrize("value", [True, "0.5", None, -0.1, 1.1, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["noul", "confidence"])
def test_invalid_probability(field, value):
    rows = build_items([item()])
    with pytest.raises(ValueError):
        normalize_results(rows, [response(rows[0], **{field: value})])


@pytest.mark.parametrize("value", [True, -1, 1.5, "10", None])
def test_invalid_token_count(value):
    rows = build_items([item()])
    reply = response(rows[0])
    reply["usage"]["input_tokens"] = value
    with pytest.raises(ValueError):
        normalize_results(rows, [reply])


def test_rejects_wrong_ids_and_cardinality():
    rows = build_items([item(), item("two")])
    good = response(rows[0])
    unknown = response({"item_id": "unknown"})
    for replies in ([good], [good, good], [good, unknown], [good, good, good]):
        with pytest.raises(ValueError):
            normalize_results(rows, replies)
    multi = response(rows[0])
    multi["answers"].update(response(rows[1])["answers"])
    with pytest.raises(ValueError):
        normalize_results(rows, [multi, response(rows[1])])


def moment_item(question, *, reverse=False):
    from citypods.judging.tasks import Subject

    subject = Subject("moment", "candidate-1", "episode", "episode", None, {"truth": True})
    options = (
        (("A", "candidate-2"), ("B", "candidate-1"))
        if reverse
        else (("A", "candidate-1"), ("B", "candidate-2"))
    )
    return Item(
        question,
        "T1",
        make_evidence("T1", "A: quote one\nB: quote two"),
        (subject,),
        options=options if question.kind == "choose" else (),
        order="_r" if reverse else "",
    )


@pytest.mark.parametrize("count", [255, 256])
def test_native_option_limit_for_choice_and_score(count):
    from citypods.judging.moment_task import BEST, USEFULNESS

    options = tuple((str(i), f"candidate-{i}") for i in range(count))
    levels = tuple(f"level-{i}" for i in range(count))
    items = [
        replace(moment_item(BEST), options=options),
        moment_item(replace(USEFULNESS, levels=levels)),
    ]
    for row in build_items(items):
        if count == 255:
            assert len(native_request(row)["questions"][row["item_id"]]["criteria"]) == count
        else:
            with pytest.raises(ValueError, match="invalid question"):
                native_request(row)


def test_moment_native_shapes_and_reverse_mapping():
    from citypods.judging.moment_task import BEST, PUBLISHABLE, SUPPORTED, USEFULNESS

    for spec in (SUPPORTED, PUBLISHABLE):
        row = build_items([moment_item(spec)])[0]
        assert row["task"] == "moment"
        assert native_request(row)["questions"][row["item_id"]] == {
            "type": "noul",
            "instructions": spec.instruction,
        }
    score = build_items([moment_item(USEFULNESS)])[0]
    assert score["question"]["criteria"] == list(USEFULNESS.levels)
    choice = build_items([moment_item(BEST, reverse=True)])[0]
    assert choice["question_id"] == "best_r"
    assert choice["subject_ids"] == {"A": "candidate-2", "B": "candidate-1"}
    assert native_request(choice)["questions"][choice["item_id"]]["criteria"] == {
        "A": "Quote A",
        "B": "Quote B",
    }
    answers = [
        {
            "answers": {
                score["item_id"]: {
                    "type": "score",
                    "score": 2,
                    "expected_level": 1.86,
                    "confidence": 0.73,
                    "probabilities": {"0": 0.1, "1": 0.2, "2": 0.6, "3": 0.1},
                }
            }
        },
        {
            "answers": {
                choice["item_id"]: {
                    "type": "choice",
                    "choice": "A",
                    "confidence": 0.6,
                    "probabilities": {"A": 0.8, "B": 0.2},
                }
            }
        },
    ]
    results = normalize_results([score, choice], answers)
    assert results[0]["answer"] == answers[0]["answers"][score["item_id"]]
    assert results[1]["answer"] == answers[1]["answers"][choice["item_id"]]


@pytest.mark.parametrize(
    "field,value",
    [
        ("score", True),
        ("score", 1.5),
        ("score", 4),
        ("expected_level", float("nan")),
        ("expected_level", 4),
        ("expected_level", True),
        ("probabilities", {"0": 0.5}),
        ("probabilities", {"0": 0.1, "1": 0.2, "2": float("inf"), "3": 0.1}),
    ],
)
def test_invalid_score_answers(field, value):
    from citypods.judging.moment_task import USEFULNESS

    row = build_items([moment_item(USEFULNESS)])[0]
    answer = {"type": "score", "score": 2, field: value}
    with pytest.raises(ValueError):
        normalize_results([row], [{"answers": {row["item_id"]: answer}}])


def test_invalid_choice_and_error_results():
    from citypods.judging.moment_task import BEST

    row = build_items([moment_item(BEST)])[0]
    for answer in (
        {"type": "choice", "choice": "C"},
        {"type": "choice", "choice": "A", "probabilities": {"A": 1}},
        {"type": "choice", "choice": "A", "probabilities": {"A": True, "B": 0}},
    ):
        with pytest.raises(ValueError):
            normalize_results([row], [{"answers": {row["item_id"]: answer}}])
    error = {"item_id": row["item_id"], "status": "too_large"}
    assert normalize_results([row], [error]) == [{"schema_version": 1, **error}]
    with pytest.raises(ValueError):
        normalize_results([row], [{**error, "error": "raw exception"}])
