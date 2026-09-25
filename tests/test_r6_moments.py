from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from citypods.compute.base import JobHandle, JobResult
from citypods.compute.llm_deferred import write_deferred
from citypods.models import Episode
from citypods.moment_evaluation import (
    append_judge_observation,
    apply_admission,
    load_state,
    record_review,
    refresh_policies,
    save_state,
)
from citypods.moment_judging import judge_policy
from citypods.moments import normalize_quote_candidate, parse_transcript_segments, transcript_region
from citypods.stages import StageContext, StageStats, _admit_r6_dispatch
from citypods.video_clips import caption_text, render_video_clip, video_clip_key
from tests._cas_fake import MemStorage


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


def test_r6_admission_budget_ignores_pending_and_cached_jobs():
    storage = MemStorage()
    ctx = StageContext(
        storage=storage,
        ffmpeg=None,
        max_kbps=96,
        dry_run=False,
        moment_max_dispatches=1,
    )
    stats = StageStats("moments")
    write_deferred(
        storage,
        "pending",
        JobHandle(task="moment-judge", recipe_hash="pending", backend="v2", ref="remote"),
    )
    write_deferred(
        storage,
        "cached",
        JobResult(task="moment-extraction", recipe_hash="cached", output={}),
    )

    assert _admit_r6_dispatch(ctx, stats, "pending", "episode") == "pending"
    assert _admit_r6_dispatch(ctx, stats, "cached", "episode") == "cached"
    assert _admit_r6_dispatch(ctx, stats, "fresh", "episode") == "admitted"
    assert _admit_r6_dispatch(ctx, stats, "another", "episode") == "cap"
    assert ctx.moment_dispatches == 1
    assert stats.defer_reasons == {"llm-pending": 1, "rollout-dispatch-cap": 1}


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
    text_only = apply_admission(
        _candidate(manual_status="Good"), {}, technical_gate=False, global_mode="manual"
    )
    assert text_only["admission"] == "admitted_text_only"
    assert text_only["display"] is True


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


def test_review_ids_are_idempotent_and_keep_auditable_controls():
    state = {"version": 1, "reviews": [], "policies": {}, "overrides": {}}
    first = record_review(
        state,
        _candidate(),
        "Good",
        reviewer="maintainer",
        review_id="review-1",
        overrides={"title": "Safety commitment"},
    )
    replay = record_review(
        state,
        _candidate(),
        "Good",
        reviewer="maintainer",
        review_id="review-1",
        overrides={"title": "Safety commitment"},
    )
    assert replay == first
    assert len(state["reviews"]) == 1
    assert state["reviews"][0]["overrides"]["title"] == "Safety commitment"


def test_review_id_rejects_a_conflicting_replay():
    state = {"version": 1, "reviews": [], "policies": {}, "overrides": {}}
    record_review(state, _candidate(), "Good", reviewer="maintainer", review_id="review-1")
    import pytest

    with pytest.raises(ValueError, match="conflicting replay"):
        record_review(state, _candidate(), "Reject", reviewer="maintainer", review_id="review-1")


def test_calibration_stays_manual_before_warmup_or_below_precision():
    for age, good_count in ((29, 27), (31, 20)):
        state = {"version": 1, "reviews": [], "policies": {}}
        when = datetime.now(UTC) - timedelta(days=age)
        for index in range(30):
            record_review(
                state,
                _candidate(candidate_id=f"r6-{age}-{index}", quality_score=0.9 if index else 0.2),
                "Good" if index < good_count else "Reject",
                reviewer="maintainer",
                review_id=f"review-{age}-{index}",
                reviewed_at=when,
            )
        refresh_policies(state)
        assert all(policy["mode"] == "manual" for policy in state["policies"].values())


def test_vtt_parser_accepts_cue_settings_and_quote_padding():
    segments = parse_transcript_segments(
        b"WEBVTT\n\n00:00:10.000 --> 00:00:20.000 align:start\n"
        b"The project will improve safety for everyone in this district.\n"
    )
    candidate = normalize_quote_candidate(
        _candidate(),
        episode_uid="episode",
        provider_model="gemini/gemini-3.6-flash",
        recipe="recipe",
        meeting_family="council",
        transcript_segments=segments,
    )
    assert candidate is not None
    assert candidate["start"] == 10.0
    assert candidate["end"] == 20.0


def test_judges_are_free_only_and_clips_are_recipe_addressed():
    policy = judge_policy(["qwen/qwen3.8-27b"])
    assert policy.allow_paid is False
    assert policy.allowed_models == ("qwen/qwen3.8-27b",)
    source_a = video_clip_key("episode", 10, 20, "timeline", source_identity="source-a")
    assert source_a != video_clip_key(
        "episode", 10, 20, "changed-timeline", source_identity="source-a"
    )
    assert source_a != video_clip_key("episode", 10, 20, "timeline", source_identity="source-b")
    assert caption_text([{"start": 10, "end": 20, "text": "A & B"}], 10, 20) == "A & B"


def test_late_independent_judge_can_qualify_from_the_human_gate(tmp_path: Path):
    state = {"version": 1, "reviews": [], "policies": {}, "judge_policies": {}, "overrides": {}}
    old = datetime.now(UTC) - timedelta(days=31)
    for index in range(30):
        good = index < 26
        candidate = _candidate(
            candidate_id=f"r6-{index}",
            quality_score=0.9 if good else 0.1,
        )
        record_review(
            state,
            candidate,
            "Good" if good else "Reject",
            reviewer="maintainer",
            review_id=f"review-{index}",
            reviewed_at=old,
        )
    path = tmp_path / "r6.json"
    save_state(path, state)
    for index in range(30):
        append_judge_observation(
            path,
            _candidate(candidate_id=f"r6-{index}"),
            {
                "provider_model": "zai/glm-4.7-flash",
                "prompt_version": "1",
                "schema_version": "1",
                "admission_score": 0.95 if index < 26 else 0.05,
            },
        )
    state = load_state(path)
    refresh_policies(state)
    judged = apply_admission(
        _candidate(
            quality_score=0.2,
            judge_assessments=[
                {
                    "provider_model": "zai/glm-4.7-flash",
                    "prompt_version": "1",
                    "schema_version": "1",
                    "admission_score": 0.95,
                }
            ],
        ),
        state,
        technical_gate=True,
        global_mode="auto",
    )
    assert judged["admission"] == "admitted"
    assert judged["admission_reason"] == "judge-calibrated:zai/glm-4.7-flash"


def test_video_renderer_keeps_audio_and_uses_the_ffprobe_binary(monkeypatch):
    import citypods.video_clips as clips

    commands: list[list[str]] = []

    class Storage:
        def exists(self, key):
            return False

        def put_file(self, key, path, content_type):
            assert content_type == "video/mp4"
            assert path.exists()
            return "https://cdn.example/clips/r6.mp4"

    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"mp4")
        return None

    monkeypatch.setattr(clips, "_safe_media_url", lambda value: value)
    monkeypatch.setattr(clips, "_probe_size", lambda binary, value: (1280, 720))
    monkeypatch.setattr(clips, "_speaker_anchor", lambda *args: None)
    monkeypatch.setattr(clips.subprocess, "run", fake_run)
    episode = Episode(
        guid="episode",
        title="Council meeting",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url="https://video.example/meeting.mp4",
    )
    rendered = render_video_clip(
        episode,
        _candidate(),
        source_url=episode.video_url,
        source_identity="meeting.mp4",
        binary="ffmpeg-custom",
        probe_binary="ffprobe-custom",
        storage=Storage(),
        segments=[{"start": 10, "end": 20, "text": _candidate()["quote"]}],
        timeline_version="identity",
    )
    assert rendered["status"] == "ready"
    assert commands[0][0] == "ffmpeg-custom"
    assert "-an" not in commands[0]
    assert commands[0][commands[0].index("-c:a") + 1] == "aac"


# --- pull-quote criteria and word-accurate timing (2026-09-24) ---------------------------------

from citypods import moment_judging as _judging  # noqa: E402
from citypods import moments as _moments  # noqa: E402

_SEGMENTS = [
    {
        "start": 100.0,
        "end": 115.0,
        "text": "Thank you. Parking minimums blocked my bakery expansion.",
    },
    {"start": 115.0, "end": 130.0, "text": "We need a vote tonight."},
]
_WORDS = [
    {"text": w, "start": s, "end": s + 0.4}
    for w, s in [
        ("Thank", 100.0),
        ("you.", 100.5),
        ("Parking", 106.0),
        ("minimums", 106.5),
        ("blocked", 107.0),
        ("my", 107.5),
        ("bakery", 108.0),
        ("expansion.", 108.5),
        ("We", 115.0),
        ("need", 115.5),
        ("a", 116.0),
        ("vote", 116.5),
        ("tonight.", 117.0),
    ]
]


def test_a_quote_keeps_its_exact_spoken_span_from_word_timing():
    quote = "Parking minimums blocked my bakery expansion."
    assert _moments.quote_timing(quote, _SEGMENTS, _WORDS) == (106.0, 108.9, "words")
    # Without a words sidecar the cue-level span is the fallback, and says so.
    assert _moments.quote_timing(quote, _SEGMENTS, None) == (100.0, 115.0, "cues")


def test_a_short_quote_is_widened_to_the_minimum_clip_not_dropped():
    candidate = _moments.normalize_quote_candidate(
        {"quote": "Parking minimums blocked my bakery expansion.", "quality_score": 0.8},
        episode_uid="ep",
        provider_model="m",
        recipe="r",
        meeting_family="council",
        transcript_segments=_SEGMENTS,
        transcript_words=_WORDS,
    )
    assert candidate is not None
    assert (candidate["quote_start"], candidate["quote_end"], candidate["timing_source"]) == (
        106.0,
        108.9,
        "words",
    )
    assert candidate["end"] - candidate["start"] == _moments.MOMENTS_MIN_SECONDS
    assert (
        candidate["start"] <= candidate["quote_start"] < candidate["quote_end"] <= candidate["end"]
    )


def test_a_repeated_phrase_resolves_to_the_occurrence_inside_the_matched_cue():
    words = _WORDS + [{"text": "vote", "start": 300.0, "end": 300.4}]
    assert _moments.word_region("vote", words, near=(115.0, 130.0)) == (116.5, 116.9)
    assert _moments.word_region("vote", words) is None  # ambiguous without the cue


def test_decisions_carry_word_accurate_timing():
    decision = _moments.normalize_decision_candidate(
        {"quote": "We need a vote tonight.", "decision_type": "deferred"},
        provider_model="m",
        transcript_segments=_SEGMENTS,
        transcript_words=_WORDS,
    )
    assert (decision["start"], decision["end"], decision["timing_source"]) == (
        115.0,
        117.4,
        "words",
    )


def test_extraction_and_judge_share_the_pull_quote_criteria():
    for prompt in (_moments.MOMENTS_SYSTEM_PROMPT, _judging.JUDGE_SYSTEM_PROMPT):
        assert _moments.PULL_QUOTE_CRITERIA in prompt
    criteria = _moments.PULL_QUOTE_CRITERIA
    assert "Any civic topic qualifies" in criteria  # emphasis, not a restriction
    assert "never name a member of the public" in criteria
    assert (_moments.MOMENTS_PROMPT_VERSION, _judging.JUDGE_PROMPT_VERSION) == ("2", "2")


def test_a_word_match_outside_the_matched_cue_falls_back_to_the_cue():
    # The only word-level occurrence is far from the cue the quote matched: keep the cue span.
    words = [{"text": "vote", "start": 300.0, "end": 300.4}]
    assert _moments.word_region("vote", words, near=(115.0, 130.0)) is None
    timing = _moments.quote_timing("We need a vote tonight.", _SEGMENTS, words)
    assert timing == (115.0, 130.0, "cues")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"segments": null}',
        b'{"segments": [null, {"words": null},'
        b' {"words": [null, "w", {"w": "ok", "s": 1, "e": 2}]}]}',
        b"not json",
        b'["segments"]',
    ],
)
def test_a_malformed_words_sidecar_never_raises(payload):
    words = _moments.parse_words_sidecar(payload)
    assert all(word["text"] == "ok" for word in words)


def test_captions_show_only_words_spoken_inside_the_clip():
    from citypods.video_clips import caption_cues

    # The clip (105-111 s) is narrower than the cue (100-115 s): only the words it plays appear.
    cues = caption_cues(_SEGMENTS, 105.0, 111.0, _WORDS)
    assert [cue["text"] for cue in cues] == ["Parking minimums blocked my bakery expansion."]
    assert caption_text(_SEGMENTS, 105.0, 111.0, _WORDS) == cues[0]["text"]
    # Without word timing, whole overlapping cues are kept (previous behavior).
    assert caption_text(_SEGMENTS, 105.0, 111.0) == _SEGMENTS[0]["text"]


def test_the_moments_output_budget_leaves_room_for_reasoning_on_every_route():
    # At 4,096 a reasoning model spent the whole budget thinking and returned empty content.
    import json as _json
    from pathlib import Path as _Path

    from citypods.compute.llm_lanes import lane_for

    assert _moments.MOMENTS_OUTPUT_TOKEN_BUDGET >= 16_384
    catalog = _json.loads(
        (
            _Path(__file__).resolve().parents[1]
            / "workers/llm-dispatch-v2/src/dispatch_limits.json"
        ).read_text()
    )
    for model in lane_for("r6-moments").models:
        for route_id in catalog["model_routes_map"][model]:
            route = catalog["routes_by_id"][route_id]
            assert route["output_context_limit"] >= _moments.MOMENTS_OUTPUT_TOKEN_BUDGET, route_id
