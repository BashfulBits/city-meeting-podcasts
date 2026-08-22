from datetime import UTC, datetime, timedelta

from citypods.moment_evaluation import apply_admission, record_review, refresh_policies
from citypods.moment_judging import judge_policy
from citypods.moments import normalize_quote_candidate, transcript_region
from citypods.video_clips import caption_text, video_clip_key


def _candidate(**overrides):
    value = {
        "candidate_id": "r6-one",
        "meeting_family": "council",
        "provider_model": "gemini/gemini-3.6-flash",
        "prompt_version": "1",
        "duration_bucket": "20-44",
        "framing_profile": "social-vertical-v1",
        "quality_score": 0.9,
        "quote": "The project will improve safety for everyone in this district.",
        "start": 10,
        "end": 20,
    }
    value.update(overrides)
    return value


def test_grounding_uses_contiguous_timed_transcript_text():
    segments = [{"start": 10, "end": 20, "text": _candidate()["quote"]}]
    assert transcript_region(_candidate()["quote"], segments) == (10.0, 20.0)
    assert (
        normalize_quote_candidate(
            _candidate(),
            episode_uid="episode",
            provider_model="gemini/gemini-3.6-flash",
            recipe="recipe",
            meeting_family="council",
            transcript_segments=segments,
        )["admission"]
        == "shadow"
    )
    assert (
        normalize_quote_candidate(
            {**_candidate(), "quote": "The project will improve safety."},
            episode_uid="episode",
            provider_model="gemini/gemini-3.6-flash",
            recipe="recipe",
            meeting_family="council",
            transcript_segments=segments,
        )
        is None
    )


def test_manual_decisions_override_mode_and_technical_gate():
    assert (
        apply_admission(
            _candidate(manual_status="Good"), {}, technical_gate=True, global_mode="manual"
        )["admission"]
        == "admitted"
    )
    assert (
        apply_admission(
            _candidate(manual_status="Reject"), {}, technical_gate=True, global_mode="auto"
        )["admission"]
        == "rejected"
    )
    assert (
        apply_admission(
            _candidate(manual_status="Good"), {}, technical_gate=False, global_mode="manual"
        )["admission"]
        == "admitted_text_only"
    )


def test_calibration_requires_warmup_and_precision():
    state = {"version": 1, "reviews": [], "policies": {}}
    old = datetime.now(UTC) - timedelta(days=31)
    for index in range(30):
        candidate = _candidate(candidate_id=f"r6-{index}", quality_score=0.9 if index else 0.2)
        record_review(
            state,
            candidate,
            "Good" if index < 27 else "Reject",
            reviewer="maintainer",
            review_id=f"review-{index}",
            reviewed_at=old,
        )
    refresh_policies(state, now=datetime.now(UTC))
    assert state["policies"]
    assert any(policy["mode"] == "auto" for policy in state["policies"].values())


def test_judges_are_free_only_and_clips_are_recipe_addressed():
    policy = judge_policy(["zai/glm-4.7"])
    assert policy.allow_paid is False
    assert policy.allowed_models == ("zai/glm-4.7",)
    assert video_clip_key("episode", 10, 20, "timeline") != video_clip_key(
        "episode", 10, 20, "changed-timeline"
    )
    assert caption_text([{"start": 10, "end": 20, "text": "A & B"}], 10, 20) == "A & B"
