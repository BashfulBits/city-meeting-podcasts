import pytest

from scripts import score_jev_panels as scorer


def test_join_validates_probabilities_and_versions():
    bundling = {"set_version": 1, "answers": {"question-order1": {"e0": 0.8}}}
    context = {"set_version": 1, "answers": {"T1": {"i0": 0.2}}}
    assert scorer.probabilities(bundling, context, "question-order1") == {
        "eval-e0": 0.8,
        "eval-i0": 0.2,
    }
    for value in (True, float("nan"), 1.01, -0.1):
        context["answers"]["T1"]["i0"] = value
        with pytest.raises(ValueError, match="finite"):
            scorer.probabilities(bundling, context, "question-order1")
    context["set_version"] = 2
    with pytest.raises(ValueError, match="versions"):
        scorer.probabilities(bundling, context, "question-order1")


def test_contested_adjudication_requires_quote_and_never_overrides_panel():
    gold = {
        "bundling": {"a": False, "b": True, "c": True, "d": False},
        "context_ladder": {"controls": [], "adjudicated": {}},
    }
    observations = [
        {
            "id": "sibling",
            "model": "google/gemma-4-31b-it",
            "decisions": {"eval-a": True, "eval-b": True, "eval-c": None, "eval-d": False},
        },
        {
            "id": "nvidia_deepseek_v4_1_flash_free",
            "model": "deepseek",
            "decisions": {"eval-a": True, "eval-b": True},
            "evidence_valid": {"eval-a": True, "eval-b": False},
        },
    ]
    anchor = {"eval-a": 0.4, "eval-b": 0.4, "eval-c": 0.9, "eval-d": 0.1}
    panel = scorer.evaluate(observations, gold, anchor, 0.5)["panels"][0]
    assert panel["accepted"] == 0
    assert panel["human_review"] == 4
    assert panel["invalid_ids"] == ["eval-c"]
    adjudicator = panel["adjudicators"][0]
    assert adjudicator["usable"] == 1
    assert adjudicator["false_positive_ids"] == ["eval-a"]
    assert adjudicator["unusable_ids"] == ["eval-b"]
    # Admission sensitivity must not redefine the production binary disagreement boundary.
    higher = scorer.evaluate(observations, gold, anchor, 0.9)["panels"][0]
    assert higher["contested_ids"] == panel["contested_ids"]
    with pytest.raises(ValueError, match="Missing JEV"):
        scorer.evaluate(observations, gold, {}, 0.5)
