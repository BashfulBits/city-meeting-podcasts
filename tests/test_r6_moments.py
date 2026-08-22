from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from citypods.video_clips import caption_text, render_video_clip, video_clip_key


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
    )
    assert replay == first
    assert len(state["reviews"]) == 1
    assert state["reviews"][0]["overrides"]["title"] == "Safety commitment"


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
    policy = judge_policy(["zai/glm-4.7"])
    assert policy.allow_paid is False
    assert policy.allowed_models == ("zai/glm-4.7",)
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
                "provider_model": "zai/glm-4.7",
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
                    "provider_model": "zai/glm-4.7",
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
    assert judged["admission_reason"] == "judge-calibrated:zai/glm-4.7"


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
