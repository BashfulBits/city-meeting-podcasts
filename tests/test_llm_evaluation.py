from __future__ import annotations

from citypods.llm_evaluation import (
    EvaluationConfig,
    apply_admission,
    config_from_mapping,
    ingest_review_body,
    load_state,
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
    assert policy_fingerprint(config, state)


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
