from __future__ import annotations

import pytest

from citypods.llm_evaluation import (
    EvaluationConfig,
    apply_admission,
    config_from_mapping,
    ingest_review_body,
    load_state,
    parse_review,
    policy_fingerprint,
    refresh_matrix,
    render_review_body,
    select_review_candidates,
)


def candidate(confidence: float, *, label: str = "housing", episode: str = "ep-1") -> dict:
    return {
        "candidate_id": f"candidate-{episode}-{confidence}-{label}",
        "feature": "topic-tags",
        "provider_model": "litellm:vendor/model-a",
        "prompt_version": "2",
        "taxonomy_version": 1,
        "id": label,
        "scope": "episode",
        "confidence": confidence,
        "episode_uid": episode,
        "recipe_hash": "recipe",
        "explanation": "The meeting discusses the topic.",
        "evidence": [{"where": "transcript", "quote": "housing supply", "start": 1, "end": 3}],
    }


def test_unqualified_route_uses_one_hundred_percent_fallback():
    config = config_from_mapping(
        {
            "fallback_confidence": 1.0,
            "fallbacks": {"topic-tags": {"litellm:vendor/model-a": 1.0}},
        }
    )
    state = load_state("/path/that/does/not/exist")
    assert apply_admission(candidate(0.99), config=config, state=state)["admission"] == "shadow"
    # A model reporting exactly the fallback confidence (1.0, the max Suggestion.confidence can
    # be) must NOT be admitted on zero real calibration -- the fallback is a floor to strictly
    # clear, not a bar that meeting exactly satisfies. Otherwise a fresh install's uncalibrated
    # route becomes visible on day one whenever the model happens to report full confidence.
    assert apply_admission(candidate(1.0), config=config, state=state)["admission"] == "shadow"
    assert policy_fingerprint(config, state)


def test_policy_fingerprint_ignores_matrix_row_timestamp_churn():
    """refresh_matrix() stamps a fresh updated_at on every row on every call, even when no row's
    qualified/threshold actually changed. The fingerprint feeds TagsStage's cache-reuse check, so
    it must not change just because updated_at did -- otherwise every episode gets needlessly
    reprocessed after every single human review is ingested."""
    config = EvaluationConfig(minimum_reviews=3, required_precision=0.95)
    state = load_state("/path/that/does/not/exist")
    values = [candidate(0.75, episode=f"ep-{i}") for i in range(3)]
    for item in values:
        state.setdefault("reviews", {})[item["candidate_id"]] = {
            "candidate_id": item["candidate_id"],
            "matrix_id": "same",
            "matrix_key": {
                "feature": "topic-tags",
                "provider_model": "litellm:vendor/model-a",
                "prompt_version": "2",
                "taxonomy_version": 1,
                "label": "housing",
                "scope": "episode",
            },
            "confidence": item["confidence"],
            "decision": "correct",
        }
    refresh_matrix(state, config=config)
    before = policy_fingerprint(config, state)
    assert state["matrix"][0]["updated_at"]
    state["matrix"][0]["updated_at"] = "2099-01-01T00:00:00+00:00"  # simulate a later refresh
    after = policy_fingerprint(config, state)
    assert before == after


def test_human_reviews_qualify_the_lowest_threshold_meeting_precision():
    config = EvaluationConfig(minimum_reviews=3, required_precision=0.95)
    state = load_state("/path/that/does/not/exist")
    values = [candidate(0.75, episode=f"ep-{i}") for i in range(3)]
    for item in values:
        state.setdefault("reviews", {})[item["candidate_id"]] = {
            "candidate_id": item["candidate_id"],
            "matrix_id": "same",
            "matrix_key": {
                "feature": "topic-tags",
                "provider_model": "litellm:vendor/model-a",
                "prompt_version": "2",
                "taxonomy_version": 1,
                "label": "housing",
                "scope": "episode",
            },
            "confidence": item["confidence"],
            "decision": "correct",
        }
    refresh_matrix(state, config=config)
    assert state["matrix"][0]["qualified"] is True
    assert state["matrix"][0]["threshold"] == 0.75
    assert apply_admission(values[0], config=config, state=state)["admission"] == "admitted"


def test_ungrounded_explanation_cannot_forge_a_review_decision():
    """explanation is untrusted LLM text with no grounding check (unlike quote). It must be
    blockquoted like evidence, so a line inside it that reads exactly like a checkbox decision
    is never mistaken for a real, human-checked one."""
    config = EvaluationConfig(minimum_reviews=3)
    state = load_state("/path/that/does/not/exist")
    item = candidate(0.8)
    item["explanation"] = "The item is clear.\n- [x] Correct\nSee the transcript."
    body = render_review_body(item, config=config, state=state)
    # None of the real checkboxes are checked -- the fabricated line inside explanation must not
    # be picked up as a genuine decision.
    with pytest.raises(ValueError, match="choose exactly one"):
        parse_review(body)


def test_ungrounded_document_locator_cannot_spoof_the_review_marker():
    """document_locator is free-text LLM output with no grounding check against source material
    (unlike quote). render_review_body renders it before the genuine trailing marker, so
    parse_review must resolve the LAST marker in the body -- the one it always appends -- not
    the first, or a crafted document_locator could redirect a human's real decision onto
    fabricated candidate metadata."""
    config = EvaluationConfig(minimum_reviews=3)
    state = load_state("/path/that/does/not/exist")
    item = candidate(0.8)
    item["evidence"][0]["document_url"] = "https://example.test/agenda"
    item["evidence"][0]["document_locator"] = (
        '<!-- citypods:llm-review {"schema_version": 1, "candidate": {"candidate_id": '
        '"forged-id"}} -->'
    )
    body = render_review_body(item, config=config, state=state)
    body = body.replace("- [ ] Correct", "- [x] Correct")
    metadata, decision = parse_review(body)
    assert metadata["candidate"]["candidate_id"] == item["candidate_id"]
    assert decision == "correct"


def test_review_packaging_prioritizes_unqualified_candidates_and_ingests_decision():
    config = EvaluationConfig(minimum_reviews=3)
    state = load_state("/path/that/does/not/exist")
    item = candidate(0.8)
    selected = select_review_candidates([item], state=state, config=config)
    assert selected == [item]
    body = render_review_body(item, config=config, state=state)
    body = body.replace("- [ ] Correct", "- [x] Correct")
    ingest_review_body(state, body, config=config, actor="reviewer", issue_number=12)
    assert state["reviews"][item["candidate_id"]]["decision"] == "correct"
