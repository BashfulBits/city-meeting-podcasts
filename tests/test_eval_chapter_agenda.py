"""The chapter-agenda eval harness scores against provider chapters as intended (GH#1852)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_chapter_agenda as ev  # noqa: E402

CHAPTERS = [{"title": "Approve the budget amendment"}, {"title": "Zoning case Z-24-17"}]


def _item(title: str) -> dict:
    return {"title": title, "evidence_text": "", "display_ref": None}


def test_a_perfect_agenda_matches_every_chapter_and_every_item():
    items = [_item("Approve the budget amendment"), _item("Zoning case Z-24-17")]
    assert ev.score_episode(items, CHAPTERS) == {
        "chapters": 2,
        "matched": 2,
        "items": 2,
        "mapped": 2,
        "conflicted": 0,
    }


def test_an_unchaptered_item_is_extra_not_an_error_and_a_missed_chapter_lowers_recall():
    items = [_item("Approve the budget amendment"), _item("Recognize the robotics team")]
    assert ev.score_episode(items, CHAPTERS) == {
        "chapters": 2,
        "matched": 1,
        "items": 2,
        "mapped": 1,
        "conflicted": 0,
    }
    gold = {"episodes": [{"uid": "a", "chapters": CHAPTERS}]}
    run = {"uid": "a", "answered": True, "valid": True, "items": items, "seconds": 1}
    summary = ev.summarize([run], gold)
    assert (summary["precision"], summary["recall"], summary["extra_items"]) == (1.0, 0.5, 1)


def test_two_equally_good_items_make_a_chapter_ambiguous_and_neither_counts():
    # The original matcher's rule: a chapter whose top two items score within 0.08 is ambiguous,
    # so duplicated items cost both recall and precision.
    items = [
        _item("Approve the budget amendment"),
        _item("Approve the budget amendment second reading"),
    ]
    score = ev.score_episode(items, [{"title": "Approve the budget amendment"}])
    assert (score["matched"], score["mapped"], score["conflicted"]) == (0, 0, 2)


def test_provider_errors_are_unanswered_not_invalid_and_rates_use_answered_episodes():
    gold = {"episodes": [{"uid": u, "chapters": CHAPTERS} for u in ("a", "b", "c")]}
    good = [_item(c["title"]) for c in CHAPTERS]
    runs = [
        {"uid": "a", "answered": True, "valid": True, "items": good, "seconds": 1},
        {"uid": "b", "answered": True, "valid": False, "seconds": 1},
        {"uid": "c", "answered": False, "valid": False, "seconds": 1},
    ]
    summary = ev.summarize(runs, gold)
    assert summary["answered"] == 2 and summary["valid_rate"] == 0.5
    assert (summary["recall"], summary["precision"], summary["f1"]) == (1.0, 1.0, 1.0)
    assert summary["inconclusive"] is True  # 1 of 3 unanswered > 10%


def test_admission_rule_requires_higher_f1_no_lower_precision_and_95_percent_valid():
    base = {"f1": 0.80, "precision": 0.85, "valid_rate": 0.97}
    assert ev.beats({"f1": 0.81, "precision": 0.85, "valid_rate": 0.95}, base)
    assert not ev.beats({"f1": 0.85, "precision": 0.84, "valid_rate": 1.0}, base)
    assert not ev.beats({"f1": 0.85, "precision": 0.90, "valid_rate": 0.94}, base)
    assert not ev.beats(
        {"f1": 0.85, "precision": 0.90, "valid_rate": 1.0, "inconclusive": True}, base
    )


def test_the_committed_eval_set_is_provider_chapter_ground_truth():
    manifest = json.loads((ROOT / "evals/chapter-agenda/manifest.json").read_text())
    gold = json.loads((ROOT / "evals/chapter-agenda/gold.json").read_text())
    assert manifest["version"] == gold["version"]
    assert {e["uid"] for e in manifest["episodes"]} == {e["uid"] for e in gold["episodes"]}
    assert all(e["agenda_text"].strip() for e in manifest["episodes"])
    assert all(e["chapters"] for e in gold["episodes"])
    # Inputs never carry the answer: provider chapters live only in gold.json.
    assert all("chapters" not in e for e in manifest["episodes"])


def test_a_rejected_response_records_each_item_outcome_and_the_raw_output():
    # Finalization reports only the first failure; the eval keeps every item's outcome so a
    # validator-repair decision can see whether one item or the whole response was bad.
    agenda = "1. Call to order\n2. Approve the budget amendment\n3. Adjourn"
    content = json.dumps(
        {
            "items": [
                {
                    "display_ref": "2.",
                    "title": "Approve the budget amendment",
                    "evidence_quote": "Approve the budget amendment",
                    "line_start": 2,
                    "line_end": 2,
                },
                {
                    "display_ref": "Item 9.Z",
                    "title": "Adjourn",
                    "evidence_quote": "A quote that is nowhere in the agenda",
                    "line_start": 3,
                    "line_end": 3,
                },
            ]
        }
    )

    class Result:
        output = content

    detail = ev._validation_detail(Result(), agenda)
    assert detail["raw_response"] == content
    assert detail["strict_items"] == 1
    assert [u["display_ref"] for u in detail["unrecovered"]] == ["Item 9.Z"]


def test_an_unparseable_reply_is_invalid_output_and_is_not_retried(monkeypatch):
    # An empty or non-JSON reply is the model's answer, not a busy provider: retrying it for
    # hours (as a response_format-incompatible route once did) only hides the incompatibility.
    from citypods.compute.llm import LLMStructuredOutputError

    calls = []

    class Backend:
        def run_inference(self, job):
            calls.append(job)
            raise LLMStructuredOutputError("structured LLM response failed Pydantic validation")

    monkeypatch.setattr(ev.time, "sleep", lambda _s: None)
    run = ev._run_one(Backend(), "m", {"uid": "a", "agenda_text": "1. Call to order"})
    assert (run["answered"], run["valid"], len(calls)) == (True, False, 1)
