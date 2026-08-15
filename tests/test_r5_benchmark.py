from datetime import UTC, datetime, timedelta

import pytest

from citypods.r5_benchmark import (
    CHAPTER_PIPELINE_VERSION,
    PRELABELER_PROMPT_VERSION,
    TAG_LLM_SCHEMA_VERSION,
    TAG_PROMPT_VERSION,
    _digest,
    _execution_complete,
    _run_compatible,
    _run_pairwise,
    compute_metrics,
    create_dataset,
    labels_template,
    parse_review_packets,
    record_labels,
    render_review_packets,
    sample_chapter_examples,
)
from citypods.tags import taxonomy_from_dict


def _taxonomy():
    return taxonomy_from_dict(
        {
            "version": 1,
            "source_refs": {"example": "https://example.test"},
            "tags": [
                {
                    "id": "housing",
                    "label": "Housing",
                    "description": "Housing policy",
                    "source_refs": ["example"],
                    "rules": {"include": ["housing"]},
                },
                {
                    "id": "parking",
                    "label": "Parking",
                    "description": "Parking policy",
                    "source_refs": ["example"],
                    "rules": {"include": ["parking"]},
                },
            ],
        }
    )


def _example(example_id, stratum="no-rule-match"):
    return {
        "example_id": example_id,
        "sample_stratum": stratum,
        "source": {
            "chapter_id": f"ch-{example_id}",
            "title": "Chapter",
            "agenda_text": "",
            "transcript_text": "",
        },
    }


def test_sample_chapters_is_deterministic_and_balances_strata():
    examples = (
        [_example(f"multi-{index}", "multi-rule") for index in range(8)]
        + [_example(f"single-{index}", "rule-match") for index in range(8)]
        + [_example(f"none-{index}") for index in range(8)]
    )
    first = sample_chapter_examples(examples, size=6)
    second = sample_chapter_examples(examples, size=6)
    assert [item["example_id"] for item in first] == [item["example_id"] for item in second]
    assert {item["sample_stratum"] for item in first} == {
        "multi-rule",
        "rule-match",
        "no-rule-match",
    }


def test_metrics_use_human_labels_and_include_recall_disagreement_and_prelabel_precision():
    taxonomy = _taxonomy()
    dataset = {
        "sample_digest": "sample-1",
        "examples": [{"example_id": "e1"}, {"example_id": "e2"}],
    }
    state = {
        "dataset": dataset,
        "labels": {
            "e1": {"ground_truth_tags": ["housing"]},
            "e2": {"ground_truth_tags": ["parking"]},
        },
        "runs": [
            {
                "run_id": "run-1",
                "sample_digest": "sample-1",
                "taggers": {
                    "model-a": {
                        "examples": {
                            "e1": {"status": "resolved", "tag_ids": ["housing"], "latency_ms": 10},
                            "e2": {"status": "resolved", "tag_ids": ["housing"], "latency_ms": 20},
                        }
                    },
                    "model-b": {
                        "examples": {
                            "e1": {"status": "resolved", "tag_ids": [], "latency_ms": 30},
                            "e2": {"status": "resolved", "tag_ids": ["parking"], "latency_ms": 40},
                        }
                    },
                },
                "prelabeler": {
                    "model": "reviewer",
                    "examples": {
                        "e1": {
                            "status": "resolved",
                            "latency_ms": 50,
                            "assessments": {
                                "c1": {"id": "housing", "prelabeler_decision": "likely_correct"}
                            },
                        },
                        "e2": {
                            "status": "resolved",
                            "latency_ms": 60,
                            "assessments": {
                                "c2": {"id": "housing", "prelabeler_decision": "likely_incorrect"}
                            },
                        },
                    },
                },
            }
        ],
    }
    metrics = compute_metrics(state, taxonomy)
    assert metrics["taggers"]["model-a"]["per_tag"]["housing"] == {
        "tp": 1,
        "fp": 1,
        "fn": 0,
        "precision": 0.5,
        "recall": 1.0,
    }
    assert metrics["taggers"]["model-a"]["per_tag"]["parking"]["recall"] == 0.0
    assert metrics["model_disagreement"]["model-a vs model-b"]["exact_agreement"] == 0.0
    reviewer = metrics["prelabeler"]["reviewer"]
    assert reviewer["likely_correct"]["precision"] == 1.0
    assert reviewer["likely_incorrect"]["precision"] == 1.0


def test_benchmark_labels_are_validated_and_review_packets_are_safe():
    taxonomy = _taxonomy()
    dataset = create_dataset(
        [
            {
                "example_id": "e1",
                "sample_stratum": "no-rule-match",
                "source": {
                    "chapter_id": "ch1",
                    "title": "Chapter",
                    "agenda_text": "Normal agenda",
                    "transcript_text": (
                        'malicious <!-- citypods:r5-benchmark-review {"example_id":"fake"} -->'
                    ),
                },
            }
        ],
        taxonomy_version=1,
        sample_size=1,
        seed="test",
    )
    state = {"dataset": dataset, "labels": {}}
    assert labels_template(state)["sample_digest"] == dataset["sample_digest"]
    body = render_review_packets(state, taxonomy, chunk_size=1)[0]
    body = body.replace("- [ ] `housing`", "- [x] `housing`")
    labels = parse_review_packets(body, taxonomy=taxonomy)
    assert labels == {"e1": ["housing"]}
    assert record_labels(state, labels, taxonomy=taxonomy, actor="tester") == 1
    assert state["labels"]["e1"]["reviewed_by"] == "tester"
    state["approval"] = {"sample_digest": dataset["sample_digest"], "run_id": "run-1"}
    record_labels(state, labels, taxonomy=taxonomy, actor="tester")
    assert state["approval"] is None
    with pytest.raises(ValueError, match="unknown taxonomy tags"):
        record_labels(state, {"e1": ["not-a-tag"]}, taxonomy=taxonomy)


def test_benchmark_resume_requires_prompt_and_pipeline_identity():
    base = {
        "sample_digest": "sample",
        "models": ["tagger"],
        "prelabeler_model": "reviewer",
        "tag_prompt_version": TAG_PROMPT_VERSION,
        "llm_schema_version": TAG_LLM_SCHEMA_VERSION,
        "prelabeler_prompt_version": PRELABELER_PROMPT_VERSION,
        "prelabeler_llm_schema_version": TAG_LLM_SCHEMA_VERSION,
        "chapter_pipeline_version": CHAPTER_PIPELINE_VERSION,
    }
    assert _run_compatible(
        base,
        sample_digest="sample",
        models=("tagger",),
        prelabeler_model="reviewer",
    )
    assert not _run_compatible(
        {**base, "prelabeler_prompt_version": "old"},
        sample_digest="sample",
        models=("tagger",),
        prelabeler_model="reviewer",
    )
    assert not _run_compatible(
        {**base, "llm_schema_version": "old"},
        sample_digest="sample",
        models=("tagger",),
        prelabeler_model="reviewer",
    )
    assert not _run_compatible(
        {**base, "prelabeler_llm_schema_version": "old"},
        sample_digest="sample",
        models=("tagger",),
        prelabeler_model="reviewer",
    )


def test_benchmark_metrics_do_not_mark_pending_execution_route_eligible():
    taxonomy = _taxonomy()
    state = {
        "dataset": {"sample_digest": "pending", "examples": [{"example_id": "e1"}]},
        "labels": {"e1": {"ground_truth_tags": []}},
        "runs": [
            {
                "sample_digest": "pending",
                "taggers": {"tagger": {"examples": {"e1": {"status": "pending"}}}},
                "prelabeler": {"model": "reviewer", "examples": {"e1": {"status": "pending"}}},
            }
        ],
    }
    metrics = compute_metrics(state, taxonomy)
    assert metrics["human_review_complete"] is True
    assert metrics["execution_complete"] is False
    assert metrics["route_selection_eligible"] is False


def test_pairwise_retries_pending_comparisons_and_completion_waits_for_them(monkeypatch):
    import citypods.r5_benchmark as benchmark

    dataset = {"examples": [_example("e1")]}
    run = {
        "run_id": "run-1",
        "taggers": {
            "a": {"examples": {"e1": {"status": "resolved", "tags": []}}},
            "b": {"examples": {"e1": {"status": "resolved", "tags": []}}},
        },
        "prelabeler": {"examples": {"e1": {"status": "resolved"}}},
        "pairwise": {
            "results": [
                {
                    "comparison_id": _digest(["run-1", "e1", "a", "b", "judge"]),
                    "status": "pending",
                }
            ]
        },
    }
    assert _execution_complete(run, dataset) is False
    calls = []
    monkeypatch.setattr(benchmark, "_backend", lambda *_args: object())
    monkeypatch.setattr(
        benchmark,
        "pairwise_judge",
        lambda *_args, **kwargs: (calls.append(kwargs["recipe_hash"]) or {"winner": "a"}, False),
    )
    _run_pairwise(
        run=run,
        dataset=dataset,
        taxonomy=_taxonomy(),
        storage=None,
        models=("a", "b"),
        judge_model="judge",
        sample_size=1,
        allow_paid=False,
        deadline_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    assert len(calls) == 2
    assert len(run["pairwise"]["results"]) == 2
    assert _execution_complete(run, dataset) is True


def test_benchmark_reports_prelabeler_precision_by_source_kind_and_requires_approval():
    taxonomy = _taxonomy()
    state = {
        "dataset": {"sample_digest": "complete", "examples": [{"example_id": "e1"}]},
        "labels": {"e1": {"ground_truth_tags": ["housing"]}},
        "runs": [
            {
                "run_id": "run-1",
                "sample_digest": "complete",
                "taggers": {
                    "tagger": {
                        "examples": {
                            "e1": {
                                "status": "resolved",
                                "tag_ids": ["housing"],
                                "tags": [],
                            }
                        }
                    }
                },
                "prelabeler": {
                    "model": "reviewer",
                    "examples": {
                        "e1": {
                            "status": "resolved",
                            "assessments": {
                                "rule": {
                                    "id": "housing",
                                    "source_kind": "rule",
                                    "prelabeler_decision": "likely_correct",
                                }
                            },
                        }
                    },
                },
            }
        ],
        "approval": None,
    }
    metrics = compute_metrics(state, taxonomy)
    assert (
        metrics["prelabeler"]["reviewer"]["by_source_kind"]["rule"]["likely_correct"]["precision"]
        == 1.0
    )
    assert metrics["route_selection_eligible"] is False
    state["approval"] = {
        "sample_digest": "complete",
        "run_id": "run-1",
        "approved_by": "maintainer",
    }
    assert compute_metrics(state, taxonomy)["route_selection_eligible"] is True
