import json
import subprocess
import sys

import pytest

from citypods import llm_tag_review
from citypods.llm_evaluation import (
    EvaluationConfig,
    apply_admission,
    candidate_matrix_key,
    config_from_mapping,
    ingest_review_body,
    load_state,
    parse_review,
    policy_fingerprint,
    prelabeler_review_candidate,
    record_review,
    refresh_matrix,
    render_review_body,
    select_review_candidates,
    visible_candidates,
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


def test_llm_evaluation_cli_is_importable_outside_checkout(tmp_path):
    """The console command must not depend on the un-packaged ``scripts/`` directory."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            ("from citypods.cli import main; main(['llm-evaluation', 'package', '--help'])"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--out-dir OUT_DIR" in result.stdout


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
    with pytest.raises(ValueError, match="no LLM review decision checked"):
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


def test_newline_injected_document_locator_cannot_forge_a_checkbox_decision():
    """document_locator is rendered as a single backtick-fenced line, but nothing strips embedded
    newlines from it -- an untrusted document_locator containing its own "\\n- [x] Correct\\n" can
    make the rendered body contain a syntactically valid checked-checkbox line before the real
    "Choose exactly one:" block. parse_review() must only ever look at the block from the LAST such
    header onward, so this earlier decoy is never mistaken for a genuine human decision."""
    config = EvaluationConfig(minimum_reviews=3)
    state = load_state("/path/that/does/not/exist")
    item = candidate(0.8)
    item["evidence"][0]["document_locator"] = "page 4\n- [x] Correct\n"
    body = render_review_body(item, config=config, state=state)
    assert "- [x] Correct" in body  # confirms the decoy line actually landed in the rendered body
    # None of the three REAL checkboxes are checked, so this must fail exactly like an
    # unreviewed issue would -- never silently accept the injected decoy as the decision.
    with pytest.raises(ValueError, match="no LLM review decision checked"):
        parse_review(body)


def test_parse_review_rejects_multiple_checked_boxes():
    item = candidate(0.8)
    config = EvaluationConfig()
    body = render_review_body(item, config=config, state={"reviews": {}, "matrix": []})
    body = body.replace("- [ ] Correct", "- [x] Correct")
    body = body.replace("- [ ] Ambiguous", "- [x] Ambiguous")
    with pytest.raises(ValueError, match="choose exactly one"):
        parse_review(body)


def test_load_state_fails_closed_on_a_corrupted_existing_file(tmp_path):
    """A missing file is a legitimate first-run case (empty snapshot); a file that exists but is
    unreadable/malformed must not be silently treated the same way -- ingest()/package() both
    save_state() the return value right back, so silently defaulting to empty would clobber real
    review history with an empty file the next time either script runs."""
    path = tmp_path / "llm_evaluation.json"
    path.write_text("not valid json {{{", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid LLM evaluation state"):
        load_state(path)

    missing = tmp_path / "does-not-exist.json"
    state = load_state(missing)
    assert state == {"version": 1, "reviews": {}, "matrix": [], "trend": []}


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


def test_prelabeler_overlay_qualifies_per_source_and_preserves_human_override():
    config = EvaluationConfig(
        minimum_reviews=2,
        prelabeler_minimum_reviews=2,
        prelabeler_minimum_decision_reviews=1,
        prelabeler_required_precision=0.95,
    )
    state = load_state("/path/that/does/not/exist")
    base = {
        "source_kind": "rule",
        "feature": "topic-tags",
        "provider_model": "rule:2",
        "prompt_version": "rule-2",
        "taxonomy_version": 1,
        "id": "housing",
        "scope": "chapter",
        "chapter_id": "ch-1",
        "recipe_hash": "rules",
        "confidence": 1.0,
        "prelabeler_model": "google/gemma-4-31b-it",
        "prelabeler_prompt_version": "1",
    }
    correct_candidate = {
        **base,
        "episode_uid": "ep-correct",
        "candidate_id": "rule-correct",
        "prelabeler_decision": "likely_correct",
        "prelabeler_confidence": 0.9,
    }
    incorrect_candidate = {
        **base,
        "episode_uid": "ep-incorrect",
        "candidate_id": "rule-incorrect",
        "prelabeler_decision": "likely_incorrect",
        "prelabeler_confidence": 0.9,
    }
    for candidate in (correct_candidate, incorrect_candidate):
        record_review(
            state,
            prelabeler_review_candidate(candidate),
            decision="correct",
        )
    refresh_matrix(state, config=config)
    assert apply_admission(incorrect_candidate, config=config, state=state)["display"] is False

    # A later human audit can reject the evaluator's suppression without mutating the raw result.
    record_review(
        state,
        prelabeler_review_candidate(incorrect_candidate),
        decision="incorrect",
    )
    refresh_matrix(state, config=config)
    projected = apply_admission(incorrect_candidate, config=config, state=state)
    assert projected["prelabeler_decision"] == "likely_incorrect"
    assert projected["display"] is True


def test_projection_recomputes_stale_persisted_display_after_tagger_qualification():
    config = EvaluationConfig(minimum_reviews=1, fallback_confidence=0.8)
    item = {
        **candidate(0.8),
        "display": False,
    }
    state = load_state("/path/that/does/not/exist")
    assert apply_admission(item, config=config, state=state)["display"] is False
    record_review(state, item, decision="correct")
    refresh_matrix(state, config=config)
    projected = apply_admission(item, config=config, state=state)
    assert projected["admission"] == "admitted"
    assert projected["display"] is True


def test_prelabeler_prompt_version_is_an_independent_matrix_dimension():
    base = {
        **candidate(0.9),
        "source_kind": "rule",
        "prelabeler_model": "reviewer",
        "prelabeler_decision": "likely_correct",
        "prelabeler_confidence": 0.9,
    }
    first = prelabeler_review_candidate({**base, "prelabeler_prompt_version": "1"})
    second = prelabeler_review_candidate({**base, "prelabeler_prompt_version": "2"})
    assert first["prompt_version"] != second["prompt_version"]
    assert first["candidate_id"] != second["candidate_id"]


def test_prelabeler_retries_keep_one_review_identity_per_subject():
    base = {
        **candidate(0.9),
        "source_kind": "rule",
        "prelabeler_model": "reviewer",
        "prelabeler_prompt_version": "1",
        "prelabeler_decision": "likely_correct",
        "prelabeler_confidence": 0.9,
        "prelabeler_reason": "first wording",
    }
    first = prelabeler_review_candidate(base)
    second = prelabeler_review_candidate({**base, "prelabeler_reason": "retry wording"})
    assert first["candidate_id"] == second["candidate_id"]


def test_historical_candidates_are_fail_closed_in_projection():
    item = {**candidate(1.0), "candidate_state": "historical", "display": True}
    config = EvaluationConfig(fallback_confidence=0.0)
    state = load_state("/path/that/does/not/exist")
    projected = apply_admission(item, config=config, state=state)
    assert projected["display"] is False
    assert visible_candidates([item], config=config, state=state) == []


def test_weekly_selector_caps_one_subject_stratum():
    config = EvaluationConfig(review_batch_size=10, max_reviews_per_subject_stratum=2)
    state = load_state("/path/that/does/not/exist")
    common = [candidate(0.8, episode=f"common-{index}") for index in range(20)]
    uncommon = [candidate(0.8, label="parking", episode=f"rare-{index}") for index in range(4)]
    selected = select_review_candidates(common + uncommon, state=state, config=config)
    common_count = sum(item["id"] == "housing" for item in selected)
    assert common_count <= 2


def test_legacy_llm_matrix_rows_still_qualify_after_ledger_normalization():
    item = candidate(0.8)
    legacy_key = {
        field: candidate_matrix_key(item)[field]
        for field in (
            "feature",
            "provider_model",
            "prompt_version",
            "taxonomy_version",
            "label",
            "scope",
        )
    }
    from citypods.llm_evaluation import _json_hash

    state = load_state("/path/that/does/not/exist")
    state["matrix"] = [
        {
            "matrix_id": _json_hash(legacy_key),
            "key": legacy_key,
            "qualified": True,
            "threshold": 0.75,
        }
    ]
    normalized = {**item, "source_kind": "llm", "assessment_kind": "tagger-admission"}
    assert (
        apply_admission(normalized, config=EvaluationConfig(), state=state)["admission"]
        == "admitted"
    )


def test_llm_tag_review_ingest_cli_skips_unreviewed_issue_cleanly(tmp_path, capsys):
    state_path = tmp_path / "llm_evaluation.json"
    state_path.write_text(json.dumps({"version": 1, "reviews": {}, "matrix": [], "trend": []}))
    item = candidate(0.8)
    config = EvaluationConfig()
    body = render_review_body(item, config=config, state={"reviews": {}, "matrix": []})
    body_file = tmp_path / "unreviewed.md"
    body_file.write_text(body)

    site_config = tmp_path / "site.yml"
    site_config.write_text(
        f"state:\n  local_path: {tmp_path}\n"
        "tagging:\n  evaluation:\n    state_path: llm_evaluation.json\n"
    )

    rc = llm_tag_review.main(
        [
            "ingest",
            "--site-config",
            str(site_config),
            "--issue-number",
            "42",
            "--issue-body-file",
            str(body_file),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stored"] is False
    assert out["reason"] == "no_decision_checked"


def test_llm_tag_review_ingest_cli_fails_on_multiple_checked_boxes(tmp_path):
    state_path = tmp_path / "llm_evaluation.json"
    state_path.write_text(json.dumps({"version": 1, "reviews": {}, "matrix": [], "trend": []}))
    item = candidate(0.8)
    config = EvaluationConfig()
    body = render_review_body(item, config=config, state={"reviews": {}, "matrix": []})
    body = body.replace("- [ ] Correct", "- [x] Correct")
    body = body.replace("- [ ] Ambiguous", "- [x] Ambiguous")
    body_file = tmp_path / "multiple.md"
    body_file.write_text(body)

    site_config = tmp_path / "site.yml"
    site_config.write_text(
        f"state:\n  local_path: {tmp_path}\n"
        "tagging:\n  evaluation:\n    state_path: llm_evaluation.json\n"
    )

    with pytest.raises(ValueError, match="choose exactly one"):
        llm_tag_review.main(
            [
                "ingest",
                "--site-config",
                str(site_config),
                "--issue-number",
                "42",
                "--issue-body-file",
                str(body_file),
            ]
        )
