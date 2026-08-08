from __future__ import annotations

import json
from pathlib import Path

import pytest

from citypods.ctc_align import CtcFitResult
from citypods.security import SecurityError
from citypods.transcript_quality import (
    QualityConfig,
    TranscriptQualityRoute,
    _auto_score_histogram,
    _blind_mapping,
    _candidate_metrics,
    _gold_fields,
    _normalize_rollups,
    _read_ref_bytes,
    _render_review_page,
    _route_from_row,
    _select_gold_coverage_samples,
    _slug,
    _summarize_evidence,
    accepted_recipe_allowed,
    build_calibration_report,
    check_gold_corrections,
    collect_flagged_corrections,
    collect_gold_points,
    evaluate_samples,
    ingest_review_decision,
    load_calibration_trend,
    load_quality_routes,
    load_raw_log,
    load_rollups,
    load_rollups_ledger,
    package_reviews,
    parse_issue_decision,
    render_issue_body,
    run_calibration,
    save_raw_log,
    save_rollups,
)
from tests._cas_fake import MemCAS


def _write_candidate(
    tmp_path: Path,
    stem: str,
    words: list[tuple[str, float, float]],
) -> tuple[str, str]:
    vtt = tmp_path / f"{stem}.vtt"
    words_path = tmp_path / f"{stem}.words.json"
    vtt_lines = ["WEBVTT", ""]
    segments = []
    for _idx, (token, start, end) in enumerate(words, start=1):
        vtt_lines.extend([f"00:00:{start:06.3f} --> 00:00:{end:06.3f}", token, ""])
        segments.append(
            {
                "start": start,
                "end": end,
                "text": token,
                "words": [{"w": token, "s": start, "e": end}],
            }
        )
    vtt.write_text("\n".join(vtt_lines))
    words_path.write_text(json.dumps({"schema": "1", "basis": "served", "segments": segments}))
    return vtt.as_posix(), words_path.as_posix()


def _manifest(tmp_path: Path) -> dict:
    a_vtt, a_words = _write_candidate(
        tmp_path, "a", [("hello", 0.0, 0.6), ("world", 0.6, 1.2), ("today", 1.2, 1.8)]
    )
    b_vtt, b_words = _write_candidate(tmp_path, "b", [("hello", 0.0, 1.2), ("world", 1.2, 2.4)])
    return {
        "version": 1,
        "sampled_at": "2026-07-10T00:00:00+00:00",
        "samples": [
            {
                "sample_id": "sample-1",
                "source_key": "src-1",
                "body_key": "city-council",
                "body_name": "City Council",
                "episode_uid": "ep-1",
                "episode_title": "Budget hearing",
                "clip_start": 0.0,
                "clip_end": 3.0,
                "audio_url": "https://example.invalid/audio.m4a",
                "candidates": [
                    {
                        "candidate_id": "provider-align",
                        "role": "provider-align",
                        "recipe_hash": "r1",
                        "transcript_ref": a_vtt,
                        "words_ref": a_words,
                        "acoustic_coverage": 1.0,
                        "word_logprob_mean": 0.9,
                    },
                    {
                        "candidate_id": "challenger",
                        "role": "asr-challenger",
                        "recipe_hash": "r2",
                        "transcript_ref": b_vtt,
                        "words_ref": b_words,
                        "acoustic_coverage": 1.0,
                        "word_logprob_mean": 0.3,
                    },
                ],
            }
        ],
    }


def test_read_ref_bytes_rejects_unsafe_network_url():
    """Every non-local ref goes through the same SSRF gate as other outbound fetches in this
    codebase (defense in depth — current callers should never actually reach it with an
    attacker-influenced URL, but a future refactor could change that silently)."""
    with pytest.raises(SecurityError):
        _read_ref_bytes("https://127.0.0.1/x.vtt")


def test_read_ref_bytes_local_file_path_is_unaffected(tmp_path):
    path = tmp_path / "local.vtt"
    path.write_text("WEBVTT")
    assert _read_ref_bytes(path.as_posix()) == b"WEBVTT"


def test_write_rollup_mirror_logs_but_does_not_raise_on_push_failure(tmp_path, capsys):
    """The CAS write (the authoritative copy) has already succeeded by the time the B2 mirror
    push runs, so a transient mirror failure must be visible (not silently swallowed) without
    turning a successfully-stored decision into a reported failure."""
    from citypods.transcript_quality import _write_rollup_mirror

    class _FailingStorage:
        def put_file(self, key, path, content_type):
            raise RuntimeError("network blip")

    state_dir = tmp_path / "state"
    result = _write_rollup_mirror(
        state_dir,
        [{"source_key": "src-1", "body_key": "city-council", "evidence": {}}],
        storage=_FailingStorage(),
    )
    assert result["rows"][0]["source_key"] == "src-1"
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "network blip" in out


def test_render_review_page_escapes_script_close_tag_in_candidate_text():
    """Candidate transcript text is untrusted (provider captions / ASR output) and gets embedded
    in a <script> block -- a literal "</script>" in that text must not be able to terminate the
    block early and inject markup into the reviewer's page."""
    sample = {
        "sample_id": "sample-xss",
        "audio_url": "https://example.invalid/audio.m4a",
        "clip_start": 0.0,
        "clip_end": 3.0,
        "episode_title": "Budget hearing",
    }
    blinded = [
        {
            "blind_label": "A",
            "units": [{"text": "</script><script>alert(1)</script>", "start": 0.0, "end": 1.0}],
        },
        {"blind_label": "B", "units": [{"text": "hello", "start": 0.0, "end": 1.0}]},
    ]
    metrics = {
        label: {
            "timing_badge": "good",
            "timing_coverage": 1.0,
            "timing_density": 1.0,
            "word_count": 1,
        }
        for label in ("A", "B")
    }
    html_text = _render_review_page(sample, blinded, metrics)
    # Only the page's own real closing tag may appear; the malicious text's copies must be escaped.
    assert html_text.count("</script>") == 1
    assert "<\\/script>" in html_text


def test_build_sample_manifest_skips_already_sampled_episodes(tmp_path, monkeypatch):
    """sample_id is deterministic over stable inputs, so resampling the same episode gives no
    new information -- the sampler must skip sample_ids already present in the rollup (reviewed
    or not) so weekly runs reach new episodes instead of re-grinding the same recent ones."""
    import citypods.transcript_quality as tq

    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {"sample-ep-old": {"sample_id": "sample-ep-old"}},
                }
            ],
        },
    )

    fake_city = type("FakeCity", (), {"slug": "t"})()
    monkeypatch.setattr(tq, "load_site_config", lambda path: {"defaults": {}})
    monkeypatch.setattr(tq, "load_city_configs", lambda config_dir, defaults: [fake_city])
    monkeypatch.setattr(tq, "resolve_state_dir", lambda site_config, output_dir: state_dir)
    monkeypatch.setattr(tq, "source_key", lambda city: "src-1")
    monkeypatch.setattr(
        tq,
        "load_records",
        lambda state_dir_arg, key: {"ep-old": {"uid": "ep-old"}, "ep-new": {"uid": "ep-new"}},
    )
    monkeypatch.setattr(tq, "record_to_episode", lambda rec: rec)
    monkeypatch.setattr(
        tq,
        "_episode_candidate_pair",
        lambda city, ep, *, config: {
            "sample_id": f"sample-{ep['uid']}",
            "source_key": "src-1",
            "episode_uid": ep["uid"],
            "published_at": "2026-07-01T00:00:00+00:00",
        },
    )

    manifest = tq.build_sample_manifest("site.yml", "config", "docs", limit=8)
    sample_ids = {s["sample_id"] for s in manifest["samples"]}
    assert sample_ids == {"sample-ep-new"}


def test_blind_mapping_stable_and_balanced():
    first = _blind_mapping("sample-1", [{"candidate_id": "c1"}, {"candidate_id": "c2"}])
    second = _blind_mapping("sample-1", [{"candidate_id": "c1"}, {"candidate_id": "c2"}])
    assert [item["blind_label"] for item in first] == ["A", "B"]
    assert [item["candidate_id"] for item in first] == [item["candidate_id"] for item in second]

    counts = {"c1_as_a": 0, "c2_as_a": 0}
    for idx in range(100):
        mapped = _blind_mapping(f"sample-{idx}", [{"candidate_id": "c1"}, {"candidate_id": "c2"}])
        counts[f"{mapped[0]['candidate_id']}_as_a"] += 1
    assert counts["c1_as_a"] > 20
    assert counts["c2_as_a"] > 20


def test_evaluate_generates_review_page_and_rollup(tmp_path):
    state_dir = tmp_path / "state"
    out_dir = tmp_path / "artifacts"
    result = evaluate_samples(
        _manifest(tmp_path),
        out_dir=out_dir,
        state_dir=state_dir,
        config=QualityConfig(auto_margin_threshold=0.05),
    )
    assert len(result["evaluated"]) == 1
    event = result["evaluated"][0]
    assert event["review_page"].endswith("/index.html")
    assert event["pair_metrics"]["text_agreement"] < 1.0
    html_text = Path(event["review_page"]).read_text()
    assert '"label":"A"' in html_text
    assert "timing " in html_text
    assert "Jump to clip start" in html_text

    rollups = load_rollups(state_dir)
    evidence = rollups["rows"][0]["evidence"]["sample-1"]
    assert evidence["review_page"].endswith("/index.html")
    assert "metrics" in evidence
    assert evidence["auto_outcome"] in {"a_better", "b_better", "tie"}
    assert evidence["l2_used"] is False  # no audio.m4a on disk in this fixture


def test_candidate_metrics_l2_fit_dominates_the_blended_score():
    """When present, L2's independent fit dominates auto_score (80%); L1 stays a 20%
    smoothing term rather than being dropped outright."""
    units = [{"text": "hello", "start": 0.0, "end": 0.5}]

    without_l2 = _candidate_metrics(units, acoustic_coverage=0.9, word_logprob_mean=0.9)
    with_low_l2 = _candidate_metrics(
        units,
        acoustic_coverage=0.9,
        word_logprob_mean=0.9,
        ctc_fit=CtcFitResult(mean_score=0.1, coverage=0.1, word_count=1, aligned_word_count=1),
    )

    # Same strong L1 signal, but a weak independent L2 fit pulls the blended score down hard --
    # proving L2 dominates rather than just nudging an L1-driven score.
    assert without_l2["auto_score"] > 0.85
    assert with_low_l2["auto_score"] < 0.35
    assert with_low_l2["l2_mean_score"] == 0.1
    assert with_low_l2["l2_coverage"] == 0.1


def test_candidate_metrics_omits_l2_fields_when_ctc_fit_absent():
    units = [{"text": "hello", "start": 0.0, "end": 0.5}]
    metrics = _candidate_metrics(units, acoustic_coverage=0.9, word_logprob_mean=0.9)
    assert metrics["l2_mean_score"] is None
    assert metrics["l2_coverage"] is None


def _write_sample_audio(out_dir: Path, sample: dict) -> Path:
    sample_dir = out_dir / sample["source_key"] / _slug(sample["episode_uid"]) / sample["sample_id"]
    sample_dir.mkdir(parents=True, exist_ok=True)
    audio_path = sample_dir / "audio.m4a"
    audio_path.write_bytes(b"fake audio bytes")
    return audio_path


def test_evaluate_uses_l2_when_audio_present_and_records_it(tmp_path, monkeypatch):
    """With the episode audio already materialized and L2 enabled, evaluate_samples calls the
    independent CTC judge and folds its score into auto_score, recording l2_used=True."""
    import citypods.transcript_quality as tq

    state_dir = tmp_path / "state"
    out_dir = tmp_path / "artifacts"
    manifest = _manifest(tmp_path)
    _write_sample_audio(out_dir, manifest["samples"][0])

    fit_calls: list[str] = []

    def fake_ctc_fit(audio_path, text, *, clip_start, clip_end, language):
        fit_calls.append(text)
        return CtcFitResult(mean_score=0.95, coverage=1.0, word_count=2, aligned_word_count=2)

    monkeypatch.setattr("citypods.ctc_align.ctc_fit", fake_ctc_fit)

    result = evaluate_samples(
        manifest,
        out_dir=out_dir,
        state_dir=state_dir,
        config=tq.QualityConfig(auto_margin_threshold=0.05, l2_sample_limit=2),
    )

    event = result["evaluated"][0]
    assert event["l2_used"] is True
    assert len(fit_calls) == 2  # both candidates scored
    for label in ("A", "B"):
        assert event["metrics"][label]["l2_mean_score"] == 0.95


def test_evaluate_falls_back_to_l1_when_l2_raises(tmp_path, monkeypatch):
    """A failing independent judge (extra not installed, download failure, non-English source)
    must not crash the sample -- it silently falls back to the L1-only auto_score."""
    import citypods.transcript_quality as tq

    state_dir = tmp_path / "state"
    out_dir = tmp_path / "artifacts"
    manifest = _manifest(tmp_path)
    _write_sample_audio(out_dir, manifest["samples"][0])

    def failing_ctc_fit(*args, **kwargs):
        raise ImportError("asr-align2 extra not installed")

    monkeypatch.setattr("citypods.ctc_align.ctc_fit", failing_ctc_fit)

    result = evaluate_samples(
        manifest,
        out_dir=out_dir,
        state_dir=state_dir,
        config=tq.QualityConfig(auto_margin_threshold=0.05),
    )

    assert result["errors"] == []
    event = result["evaluated"][0]
    assert event["l2_used"] is False
    assert event["metrics"]["A"]["l2_mean_score"] is None


def test_evaluate_l2_is_all_or_nothing_across_the_pair(tmp_path, monkeypatch):
    """If one candidate's ctc_fit succeeds and the other's raises, both candidates must fall
    back to the L1-only formula -- scoring one with the CTC-dominated blend and the other with
    the pure L1 formula would make the margin between them compare two different scoring
    formulas, not a fair independent judgment of the same pair."""
    import citypods.transcript_quality as tq

    state_dir = tmp_path / "state"
    out_dir = tmp_path / "artifacts"
    manifest = _manifest(tmp_path)
    _write_sample_audio(out_dir, manifest["samples"][0])

    calls: list[str] = []

    def partially_failing_ctc_fit(audio_path, text, *, clip_start, clip_end, language):
        calls.append(text)
        if len(calls) == 1:
            return CtcFitResult(mean_score=0.95, coverage=1.0, word_count=2, aligned_word_count=2)
        raise RuntimeError("alignment failed for this candidate")

    monkeypatch.setattr("citypods.ctc_align.ctc_fit", partially_failing_ctc_fit)

    result = evaluate_samples(
        manifest,
        out_dir=out_dir,
        state_dir=state_dir,
        config=tq.QualityConfig(auto_margin_threshold=0.05),
    )

    assert result["errors"] == []
    event = result["evaluated"][0]
    assert len(calls) == 2  # both candidates were attempted
    assert event["l2_used"] is False
    for label in ("A", "B"):
        assert event["metrics"][label]["l2_mean_score"] is None
        assert event["metrics"][label]["l2_coverage"] is None


def test_evaluate_respects_l2_sample_limit_across_samples(tmp_path, monkeypatch):
    """l2_sample_limit bounds L2 usage per evaluate() run, not per sample -- with a budget of 1
    across two eligible samples, only the first consumes it."""
    import citypods.transcript_quality as tq

    state_dir = tmp_path / "state"
    out_dir = tmp_path / "artifacts"
    manifest = _manifest(tmp_path)
    second = dict(manifest["samples"][0])
    second["sample_id"] = "sample-2"
    second["episode_uid"] = "ep-2"
    manifest["samples"].append(second)
    for sample in manifest["samples"]:
        _write_sample_audio(out_dir, sample)

    monkeypatch.setattr(
        "citypods.ctc_align.ctc_fit",
        lambda *a, **k: CtcFitResult(0.9, 1.0, 2, 2),
    )

    result = evaluate_samples(
        manifest,
        out_dir=out_dir,
        state_dir=state_dir,
        config=tq.QualityConfig(auto_margin_threshold=0.05, l2_sample_limit=1),
    )

    l2_used_by_sample = {e["sample_id"]: e["l2_used"] for e in result["evaluated"]}
    assert sorted(l2_used_by_sample.values()) == [False, True]


def test_evaluate_never_attempts_l2_when_disabled(tmp_path, monkeypatch):
    import citypods.transcript_quality as tq

    state_dir = tmp_path / "state"
    out_dir = tmp_path / "artifacts"
    manifest = _manifest(tmp_path)
    _write_sample_audio(out_dir, manifest["samples"][0])

    def unexpected_call(*args, **kwargs):
        raise AssertionError("ctc_fit should never be called when l2_enabled=False")

    monkeypatch.setattr("citypods.ctc_align.ctc_fit", unexpected_call)

    result = evaluate_samples(
        manifest,
        out_dir=out_dir,
        state_dir=state_dir,
        config=tq.QualityConfig(auto_margin_threshold=0.05, l2_enabled=False),
    )

    assert result["evaluated"][0]["l2_used"] is False


def test_evaluate_handles_candidate_with_no_timing_data(tmp_path):
    """A candidate with no words/cues at all (e.g. alignment produced nothing usable) must still
    render a review page and metrics, not crash the batch."""
    a_vtt, a_words = _write_candidate(tmp_path, "empty", [])
    b_vtt, b_words = _write_candidate(tmp_path, "b", [("hello", 0.0, 0.6), ("world", 0.6, 1.2)])
    manifest = {
        "version": 1,
        "sampled_at": "2026-07-10T00:00:00+00:00",
        "samples": [
            {
                "sample_id": "sample-empty",
                "source_key": "src-1",
                "body_key": "city-council",
                "body_name": "City Council",
                "episode_uid": "ep-1",
                "episode_title": "Budget hearing",
                "clip_start": 0.0,
                "clip_end": 3.0,
                "audio_url": "https://example.invalid/audio.m4a",
                "candidates": [
                    {
                        "candidate_id": "provider-align",
                        "role": "provider-align",
                        "recipe_hash": "r1",
                        "transcript_ref": a_vtt,
                        "words_ref": a_words,
                        "acoustic_coverage": 0.0,
                        "word_logprob_mean": None,
                    },
                    {
                        "candidate_id": "challenger",
                        "role": "asr-challenger",
                        "recipe_hash": "r2",
                        "transcript_ref": b_vtt,
                        "words_ref": b_words,
                        "acoustic_coverage": 1.0,
                        "word_logprob_mean": 0.8,
                    },
                ],
            }
        ],
    }
    result = evaluate_samples(
        manifest,
        out_dir=tmp_path / "artifacts",
        state_dir=tmp_path / "state",
        config=QualityConfig(),
    )
    assert len(result["evaluated"]) == 1
    assert result["errors"] == []
    event = result["evaluated"][0]
    metrics_by_role = {
        item["role"]: event["metrics"][label] for label, item in event["blind_mapping"].items()
    }
    empty_metrics = metrics_by_role["provider-align"]
    assert empty_metrics["word_count"] == 0
    assert empty_metrics["timing_badge"] == "missing words"
    assert empty_metrics["auto_score"] < metrics_by_role["asr-challenger"]["auto_score"]
    assert event["needs_review"] is True
    Path(event["review_page"]).read_text()  # renders without raising


def test_evaluate_one_bad_sample_does_not_lose_the_rest_of_the_batch(tmp_path):
    """A single failing sample (e.g. AlignmentQualityError on genuinely bad captions — exactly
    the case H15 exists to detect) must not abort the whole batch: every other sample's work in
    the same run has to survive, and the failure must be visible in the raw log."""
    state_dir = tmp_path / "state"
    out_dir = tmp_path / "artifacts"
    manifest = _manifest(tmp_path)
    manifest["samples"].append(
        {
            "sample_id": "sample-broken",
            "source_key": "src-1",
            "body_key": "city-council",
            "body_name": "City Council",
            "episode_uid": "ep-2",
            "episode_title": "Broken sample",
            "clip_start": 0.0,
            "clip_end": 3.0,
            "audio_url": "https://example.invalid/audio.m4a",
            "candidates": [
                # No transcript_ref and no city context -> _materialize_candidate raises.
                {"candidate_id": "provider-align", "role": "provider-align"},
                {"candidate_id": "challenger", "role": "asr-challenger"},
            ],
        }
    )
    result = evaluate_samples(
        manifest,
        out_dir=out_dir,
        state_dir=state_dir,
        config=QualityConfig(auto_margin_threshold=0.05),
        cities_by_slug={},
    )
    assert len(result["evaluated"]) == 1
    assert result["evaluated"][0]["sample_id"] == "sample-1"
    assert len(result["errors"]) == 1
    assert result["errors"][0]["sample_id"] == "sample-broken"

    raw_log = load_raw_log(state_dir)
    kinds = {(e["sample_id"], e["kind"]) for e in raw_log["events"]}
    assert ("sample-1", "evaluation") in kinds
    assert ("sample-broken", "evaluation_error") in kinds

    rollups = load_rollups(state_dir)
    assert "sample-1" in rollups["rows"][0]["evidence"]
    assert "sample-broken" not in rollups["rows"][0]["evidence"]


def test_raw_log_can_be_capped_without_pruning_rollups(tmp_path):
    state_dir = tmp_path / "state"
    save_raw_log(
        state_dir,
        {
            "version": 1,
            "events": [
                {"id": "e1", "created_at": "2026-07-10T00:00:00+00:00"},
                {"id": "e2", "created_at": "2026-07-10T00:01:00+00:00"},
                {"id": "e3", "created_at": "2026-07-10T00:02:00+00:00"},
            ],
        },
        max_events=2,
    )
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {
                        "sample-a": {"sample_id": "sample-a"},
                        "sample-b": {"sample_id": "sample-b"},
                        "sample-c": {"sample_id": "sample-c"},
                    },
                }
            ],
        },
    )
    assert [event["id"] for event in load_raw_log(state_dir)["events"]] == ["e2", "e3"]
    assert sorted(load_rollups(state_dir)["rows"][0]["evidence"]) == [
        "sample-a",
        "sample-b",
        "sample-c",
    ]


def test_periodic_reevaluation_cannot_clobber_a_recorded_human_decision(tmp_path):
    """sample_id is deterministic from stable inputs, so a later `evaluate` run can regenerate
    evidence for a sample_id a human already decided on (e.g. the weekly eval re-samples the same
    recent episode before it ages out of the window). A fresh, unreviewed re-evaluation for that
    same sample_id must never erase the recorded manual_decision, regardless of merge order."""
    reviewed_row = {
        "source_key": "src-1",
        "body_key": "city-council",
        "body_name": "City Council",
        "evidence": {
            "sample-1": {
                "sample_id": "sample-1",
                "manual_decision": "a_better",
                "reviewed_at": "2026-07-06T00:00:00",
                "blind_mapping": {
                    "A": {"role": "provider-align"},
                    "B": {"role": "asr-challenger"},
                },
            }
        },
    }
    reevaluated_row = {
        "source_key": "src-1",
        "body_key": "city-council",
        "body_name": "City Council",
        "evidence": {
            "sample-1": {
                "sample_id": "sample-1",
                "auto_outcome": "b_better",
                "blind_mapping": {
                    "A": {"role": "provider-align"},
                    "B": {"role": "asr-challenger"},
                },
            }
        },
    }
    for rows in ([reviewed_row, reevaluated_row], [reevaluated_row, reviewed_row]):
        merged = _normalize_rollups(rows)
        evidence = merged[0]["evidence"]["sample-1"]
        assert evidence["manual_decision"] == "a_better"
        assert evidence["reviewed_at"] == "2026-07-06T00:00:00"
        assert merged[0]["summary"]["reviewed"] == 1


def test_render_and_parse_issue_body_round_trip():
    body = render_issue_body(
        {
            "sample_id": "sample-1",
            "source_key": "src-1",
            "body_key": "city-council",
            "body_name": "City Council",
            "episode_uid": "ep-1",
            "episode_title": "Budget hearing",
            "review_page": "https://example.invalid/review",
            "clip_start": 0.0,
            "clip_end": 3.0,
            "blind_mapping": {"A": {"candidate_id": "one"}, "B": {"candidate_id": "two"}},
        }
    )
    edited = body.replace("- [ ] B is better", "- [x] B is better").replace(
        "- [ ] Timing makes A hard to use", "- [x] Timing makes A hard to use"
    )
    parsed = parse_issue_decision(edited)
    assert parsed["manual_decision"] == "b_better"
    assert parsed["timing_flags"] == ["timing_a_bad"]


def _issue_body() -> str:
    return render_issue_body(
        {
            "sample_id": "sample-1",
            "source_key": "src-1",
            "body_key": "city-council",
            "body_name": "City Council",
            "episode_uid": "ep-1",
            "episode_title": "Budget hearing",
            "review_page": "https://example.invalid/review",
            "clip_start": 0.0,
            "clip_end": 3.0,
            "blind_mapping": {
                "A": {"role": "provider-align", "recipe_hash": "provider-hash"},
                "B": {"role": "asr-challenger", "recipe_hash": "challenger-hash"},
            },
        }
    )


def test_parse_issue_decision_rejects_zero_checked_primary_outcomes():
    with pytest.raises(ValueError, match="no primary outcome checked"):
        parse_issue_decision(_issue_body())


def test_parse_issue_decision_rejects_two_checked_primary_outcomes():
    body = (
        _issue_body()
        .replace("- [ ] A is better", "- [x] A is better")
        .replace("- [ ] B is better", "- [x] B is better")
    )
    with pytest.raises(ValueError, match="exactly one"):
        parse_issue_decision(body)


def test_render_issue_body_never_leaks_role_or_recipe_outside_hidden_marker():
    """Reviews must stay blind: only the hidden `<!-- citypods:h15 ... -->` marker may carry
    role/recipe identity; the human-visible portion must show only A/B."""
    body = _issue_body()
    marker_start = body.index("<!-- citypods:h15")
    visible_text = body[:marker_start]
    assert "provider-align" not in visible_text
    assert "asr-challenger" not in visible_text
    assert "provider-hash" not in visible_text
    assert "challenger-hash" not in visible_text
    # The hidden marker is the only place that identity is allowed to appear.
    assert "provider-align" in body[marker_start:]


def test_package_and_ingest_review(tmp_path):
    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {
                        "sample-1": {
                            "sample_id": "sample-1",
                            "episode_uid": "ep-1",
                            "episode_title": "Budget hearing",
                            "review_page": "https://example.invalid/review",
                            "clip_start": 0.0,
                            "clip_end": 3.0,
                            "needs_review": True,
                            "blind_mapping": {
                                "A": {"candidate_id": "one"},
                                "B": {"candidate_id": "two"},
                            },
                        }
                    },
                }
            ],
        },
    )
    out = package_reviews(state_dir, out_dir=tmp_path / "issues", batch_size=10)
    assert out["children"][0]["sample_id"] == "sample-1"
    body_file = Path(out["children"][0]["body_file"])
    edited = body_file.read_text().replace(
        "- [ ] Both fully correct",
        "- [x] Both fully correct",
    )

    result = ingest_review_decision(
        state_dir,
        issue_number=12,
        issue_body=edited,
        actor="tester",
        issue_url="https://example.invalid/issues/12",
    )
    assert result["manual_decision"] == "both_correct"
    assert result["stored"] is True
    rollups = load_rollups(state_dir)
    evidence = rollups["rows"][0]["evidence"]["sample-1"]
    assert evidence["manual_decision"] == "both_correct"
    assert evidence["reviewed_by"] == "tester"


def test_ingest_review_cli_skips_unreviewed_issue_cleanly(tmp_path, capsys):
    state_dir = tmp_path / "state"
    save_rollups(state_dir, {"version": 1, "rows": []})
    body_file = tmp_path / "unreviewed.md"
    body_file.write_text(_issue_body())
    site_config = tmp_path / "site.yml"
    site_config.write_text("state:\n  local_path: " + str(state_dir) + "\n")
    from citypods.transcript_quality import main

    rc = main(
        [
            "ingest-review",
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


def test_ingest_review_cli_fails_on_multiple_checked_boxes(tmp_path):
    state_dir = tmp_path / "state"
    save_rollups(state_dir, {"version": 1, "rows": []})
    body = (
        _issue_body()
        .replace("- [ ] A is better", "- [x] A is better")
        .replace("- [ ] B is better", "- [x] B is better")
    )
    body_file = tmp_path / "multiple.md"
    body_file.write_text(body)
    site_config = tmp_path / "site.yml"
    site_config.write_text("state:\n  local_path: " + str(state_dir) + "\n")
    from citypods.transcript_quality import main

    with pytest.raises(ValueError, match="exactly one"):
        main(
            [
                "ingest-review",
                "--site-config",
                str(site_config),
                "--issue-number",
                "42",
                "--issue-body-file",
                str(body_file),
            ]
        )


def test_rollup_ledger_uses_cas_and_writes_b2_mirror(tmp_path):
    state_dir = tmp_path / "state"
    cas = MemCAS()
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {
                        "sample-1": {
                            "sample_id": "sample-1",
                            "episode_uid": "ep-1",
                            "review_page": "https://example.invalid/review",
                            "needs_review": True,
                            "blind_mapping": {
                                "A": {"candidate_id": "one"},
                                "B": {"candidate_id": "two"},
                            },
                        }
                    },
                }
            ],
        },
    )
    body = render_issue_body(
        {
            "sample_id": "sample-1",
            "source_key": "src-1",
            "body_key": "city-council",
            "body_name": "City Council",
            "episode_uid": "ep-1",
            "episode_title": "Budget hearing",
            "review_page": "https://example.invalid/review",
            "clip_start": 0.0,
            "clip_end": 3.0,
            "blind_mapping": {"A": {"candidate_id": "one"}, "B": {"candidate_id": "two"}},
        }
    ).replace("- [ ] A is better", "- [x] A is better")

    result = ingest_review_decision(
        state_dir,
        issue_number=18,
        issue_body=body,
        actor="cas-user",
        storage=cas,
    )
    assert result["manual_decision"] == "a_better"
    ledger = load_rollups_ledger(state_dir, cas)
    assert ledger["rows"][0]["evidence"]["sample-1"]["manual_decision"] == "a_better"
    assert cas.keys("state/transcript_quality_ledger.json") == [
        "state/transcript_quality_ledger.json"
    ]
    assert (
        load_rollups(state_dir)["rows"][0]["evidence"]["sample-1"]["manual_decision"] == "a_better"
    )


def test_mutate_rollups_ledger_without_cas_still_merge_pushes_remotely(tmp_path):
    """Without R2 CAS (e.g. a B2-only backend), mutate_rollups_ledger must not leave the write
    stranded on the runner's local filesystem — it should merge-push through
    push_transcript_quality_rollups_merged same as the CAS path mirrors to B2."""
    from citypods.statesync import STATE_PREFIX, pull_state
    from citypods.storage.local import LocalStorage
    from citypods.transcript_quality import mutate_rollups_ledger

    bucket = LocalStorage(root=tmp_path / "bucket", url_prefix="https://x")
    assert getattr(bucket, "cas_capable", False) is False

    remote_state_dir = tmp_path / "remote-seed"
    save_rollups(
        remote_state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {"sample-remote": {"sample_id": "sample-remote"}},
                }
            ],
        },
    )
    bucket.put_file(
        f"{STATE_PREFIX}/transcript_quality_rollups.json",
        remote_state_dir / "transcript_quality_rollups.json",
        "application/json",
    )

    state_dir = tmp_path / "state"
    mutate_rollups_ledger(
        state_dir,
        bucket,
        lambda rows: (
            rows
            + [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {"sample-local": {"sample_id": "sample-local"}},
                }
            ]
        ),
    )

    local_evidence = load_rollups(state_dir)["rows"][0]["evidence"]
    assert set(local_evidence) == {"sample-remote", "sample-local"}

    restored = tmp_path / "restored"
    pull_state(bucket, restored)
    remote_evidence = json.loads((restored / "transcript_quality_rollups.json").read_text())[
        "rows"
    ][0]["evidence"]
    assert set(remote_evidence) == {"sample-remote", "sample-local"}


@pytest.mark.parametrize(
    ("recipe_hash", "accepted", "min_rank", "ranks", "expected"),
    [
        ("r1", ("r1",), None, None, True),
        ("r2", ("r1",), None, None, False),
        ("r2", (), 2, {"r2": 3}, True),
        ("r2", (), 2, {"r2": 1}, False),
    ],
)
def test_accepted_recipe_policy(recipe_hash, accepted, min_rank, ranks, expected):
    assert (
        accepted_recipe_allowed(
            recipe_hash,
            accepted_active_recipes=accepted,
            minimum_quality_rank=min_rank,
            recipe_ranks=ranks,
        )
        is expected
    )


def test_load_quality_routes_prefers_fresh_asr_after_repeated_challenger_wins(tmp_path):
    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "accepted_recipe_policy": {
                        "production_default": "recipe-new",
                        "accepted_active_recipes": ["recipe-new", "recipe-old"],
                    },
                    "evidence": {
                        "sample-1": {
                            "sample_id": "sample-1",
                            "manual_decision": "a_better",
                            "blind_mapping": {
                                "A": {
                                    "role": "asr-challenger",
                                    "recipe_hash": "recipe-new",
                                },
                                "B": {
                                    "role": "provider-align",
                                    "recipe_hash": "provider-align-hash",
                                },
                            },
                        },
                        "sample-2": {
                            "sample_id": "sample-2",
                            "manual_decision": "b_better",
                            "blind_mapping": {
                                "A": {
                                    "role": "provider-align",
                                    "recipe_hash": "provider-align-hash",
                                },
                                "B": {
                                    "role": "asr-challenger",
                                    "recipe_hash": "recipe-old",
                                },
                            },
                        },
                    },
                }
            ],
        },
    )
    routes = load_quality_routes({"defaults": {"transcript_quality": {}}}, state_dir)
    route = routes[("src-1", "city-council")]
    assert isinstance(route, TranscriptQualityRoute)
    assert route.prefers_fresh_asr is True
    assert route.accepted_active_recipes == ("recipe-new", "recipe-old")


def _blind(a_role: str, b_role: str) -> dict:
    return {
        "A": {"role": a_role, "recipe_hash": f"{a_role}-hash"},
        "B": {"role": b_role, "recipe_hash": f"{b_role}-hash"},
    }


def _evidence_item(
    *, score_a: float, score_b: float, manual_decision: str | None = None, auto_outcome: str
) -> dict:
    item = {
        "blind_mapping": _blind("provider-align", "asr-challenger"),
        "metrics": {"A": {"auto_score": score_a}, "B": {"auto_score": score_b}},
        "auto_outcome": auto_outcome,
    }
    if manual_decision is not None:
        item["manual_decision"] = manual_decision
    return item


def test_uncalibrated_route_ignores_auto_margin_and_uses_bootstrap_wins(tmp_path):
    """Before human review shows the automatic scorer tracking human judgment, route_mode must
    come from net human wins alone — the free auto signal is not yet trusted, even if it's
    already trending the other way on not-yet-reviewed samples."""
    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {
                        # Humans pick provider-align both times, but the automatic scorer
                        # disagrees both times (low agreement rate -> not calibrated).
                        "sample-1": _evidence_item(
                            score_a=0.9,
                            score_b=0.5,
                            manual_decision="a_better",
                            auto_outcome="b_better",
                        ),
                        "sample-2": _evidence_item(
                            score_a=0.9,
                            score_b=0.5,
                            manual_decision="a_better",
                            auto_outcome="b_better",
                        ),
                        # Unreviewed sample strongly favors the challenger on auto_score alone.
                        "sample-3": _evidence_item(
                            score_a=0.1, score_b=0.9, auto_outcome="b_better"
                        ),
                    },
                }
            ],
        },
    )
    route = load_quality_routes({"defaults": {"transcript_quality": {}}}, state_dir)[
        ("src-1", "city-council")
    ]
    assert route.calibrated is False
    assert route.human_agreement_rate == 0.0
    assert route.route_mode == "provider-align"  # bootstrap: 2 net human wins for provider-align


def test_split_human_panel_never_bootstraps_even_with_perfect_agreement(tmp_path):
    """A 2-2 human split has no net directional preference. Even if the automatic scorer agreed
    with every single reviewer (human_agreement_rate == 1.0), that must not be treated as
    'bootstrapped' -- otherwise a same-generator-biased auto margin could flip production routing
    on evidence humans never actually converged on."""
    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {
                        "sample-1": _evidence_item(
                            score_a=0.9,
                            score_b=0.5,
                            manual_decision="a_better",
                            auto_outcome="a_better",
                        ),
                        "sample-2": _evidence_item(
                            score_a=0.9,
                            score_b=0.5,
                            manual_decision="a_better",
                            auto_outcome="a_better",
                        ),
                        "sample-3": _evidence_item(
                            score_a=0.5,
                            score_b=0.9,
                            manual_decision="b_better",
                            auto_outcome="b_better",
                        ),
                        "sample-4": _evidence_item(
                            score_a=0.5,
                            score_b=0.9,
                            manual_decision="b_better",
                            auto_outcome="b_better",
                        ),
                    },
                }
            ],
        },
    )
    route = load_quality_routes({"defaults": {"transcript_quality": {}}}, state_dir)[
        ("src-1", "city-council")
    ]
    assert route.provider_wins == 2
    assert route.challenger_wins == 2
    assert route.human_agreement_rate == 1.0
    assert route.calibrated is False
    assert route.route_mode == "unknown"


def test_calibrated_route_follows_continuous_auto_margin_over_stale_human_wins(tmp_path):
    """Once reviewed samples show the automatic scorer agreeing with humans often enough, the
    continuously-updated auto-margin average (across ALL evaluated samples, not just reviewed
    ones) becomes the trigger — even when it now disagrees with the human win count, which can
    go stale between review batches."""
    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                {
                    "source_key": "src-1",
                    "body_key": "city-council",
                    "body_name": "City Council",
                    "evidence": {
                        # Two human-reviewed samples: humans and auto both favor provider-align.
                        "sample-1": _evidence_item(
                            score_a=0.9,
                            score_b=0.5,
                            manual_decision="a_better",
                            auto_outcome="a_better",
                        ),
                        "sample-2": _evidence_item(
                            score_a=0.9,
                            score_b=0.5,
                            manual_decision="a_better",
                            auto_outcome="a_better",
                        ),
                        # Three newer, unreviewed samples all favor the challenger strongly
                        # enough to flip the average signed margin negative.
                        "sample-3": _evidence_item(
                            score_a=0.3, score_b=0.9, auto_outcome="b_better"
                        ),
                        "sample-4": _evidence_item(
                            score_a=0.3, score_b=0.9, auto_outcome="b_better"
                        ),
                        "sample-5": _evidence_item(
                            score_a=0.3, score_b=0.9, auto_outcome="b_better"
                        ),
                    },
                }
            ],
        },
    )
    route = load_quality_routes({"defaults": {"transcript_quality": {}}}, state_dir)[
        ("src-1", "city-council")
    ]
    assert route.calibrated is True
    assert route.human_agreement_rate == 1.0
    assert route.provider_wins == 2  # the (now stale) human win tally
    assert route.auto_margin_avg < 0
    assert route.route_mode == "fresh-asr"  # continuous signal wins, not the stale human count


# --- H15 Layer 3: gold-anchored calibration -------------------------------------------------


def test_l2_sample_limit_defaults_to_full_run_budget():
    """Raised from 2 to 8 (== sample_limit): since sample_id exclusion means an episode is never
    re-sampled once evaluated, a lower L2 budget would permanently strand a fraction of every
    run's episodes on the Layer-1-only fallback, not just delay them."""
    config = QualityConfig()
    assert config.l2_sample_limit == config.sample_limit == 8


def test_both_fully_correct_round_trip():
    body = _issue_body()
    edited = body.replace("- [ ] Both fully correct", "- [x] Both fully correct")
    parsed = parse_issue_decision(edited)
    assert parsed["manual_decision"] == "both_correct"
    assert parsed["correction_text"] == ""


def test_checking_both_fully_correct_and_neither_still_rejects():
    body = (
        _issue_body()
        .replace("- [ ] Both fully correct", "- [x] Both fully correct")
        .replace("- [ ] Neither usable", "- [x] Neither usable")
    )
    with pytest.raises(ValueError, match="exactly one"):
        parse_issue_decision(body)


def test_correction_draft_edit_detection():
    """The correction box is pre-filled with a draft (the higher-auto_score candidate's text) --
    leaving it untouched must not be harvested as a reviewer correction, only an actual edit."""
    sample = {
        "sample_id": "sample-1",
        "source_key": "src-1",
        "body_key": "city-council",
        "episode_uid": "ep-1",
        "review_page": "https://example.invalid/review",
        "blind_mapping": {"A": {"candidate_id": "one"}, "B": {"candidate_id": "two"}},
        "metrics": {
            "A": {"auto_score": 0.4, "text": "the a candidate text"},
            "B": {"auto_score": 0.8, "text": "the b candidate text"},
        },
    }
    body = render_issue_body(sample)
    assert "the b candidate text" in body  # higher auto_score (B) is the pre-filled draft

    untouched = body.replace("- [ ] Neither usable", "- [x] Neither usable")
    assert parse_issue_decision(untouched)["correction_text"] == ""

    # Only replace the first occurrence (the visible fence) -- the hidden metadata's own copy of
    # the draft must stay untouched, exactly like a reviewer editing only the rendered markdown.
    edited = untouched.replace("the b candidate text", "the corrected text", 1)
    assert parse_issue_decision(edited)["correction_text"] == "the corrected text"


def test_summarize_evidence_counts_both_correct_as_reviewed_not_pending():
    summary = _summarize_evidence({"s1": {"manual_decision": "both_correct"}})
    assert summary["reviewed"] == 1
    assert summary["pending"] == 0
    assert summary["primary_counts"]["both_correct"] == 1


def test_route_from_row_both_correct_is_a_tie_not_a_directional_win():
    """both_correct must land in ties (not provider_wins/challenger_wins), must not perturb
    recipe_ranks (a None role falling through would silently rank both candidates' recipes),
    and must count as agreement when auto_outcome=='tie' -- the closest automatic equivalent to
    a human-confirmed 'both correct' verdict, since auto_outcome itself never emits
    'both_correct'."""
    row = {
        "source_key": "src-1",
        "body_key": "city-council",
        "evidence": {
            "sample-1": _evidence_item(
                score_a=0.8, score_b=0.82, manual_decision="both_correct", auto_outcome="tie"
            ),
        },
    }
    route = _route_from_row(row, fallback=QualityConfig())
    assert route.ties == 1
    assert route.provider_wins == 0
    assert route.challenger_wins == 0
    assert route.recipe_ranks == {}
    assert route.human_agreement_rate == 1.0


def _rollup_row(*, source_key_value: str = "src-1", evidence: dict) -> dict:
    return {
        "source_key": source_key_value,
        "body_key": "city-council",
        "body_name": "City Council",
        "evidence": evidence,
    }


def _coverage_candidate(
    sample_id: str, *, score_a: float, score_b: float, l2_used: bool = False
) -> dict:
    return {
        "sample_id": sample_id,
        "episode_uid": f"ep-{sample_id}",
        "review_page": "https://example.invalid/review",
        "clip_start": 0.0,
        "clip_end": 3.0,
        "blind_mapping": _blind("provider-align", "asr-challenger"),
        "metrics": {"A": {"auto_score": score_a}, "B": {"auto_score": score_b}},
        "l2_used": l2_used,
    }


def test_gold_coverage_selection_prefers_l2_scored_extremes_over_l1_only():
    """The whole point of gold data is calibrating L2 -- a deliberately-sampled extreme that
    turns out to be L1-only is a wasted pick, so L2-scored candidates are preferred even when an
    L1-only candidate is nominally more extreme."""
    rows = [
        _rollup_row(
            evidence={
                "l1-extreme": _coverage_candidate(
                    "l1-extreme", score_a=0.99, score_b=0.98, l2_used=False
                ),
                "l2-good": _coverage_candidate("l2-good", score_a=0.9, score_b=0.88, l2_used=True),
            }
        )
    ]
    picks = _select_gold_coverage_samples(
        rows, already_selected_ids=set(), good_limit=1, bad_limit=0
    )
    assert [p["sample_id"] for p in picks] == ["l2-good"]
    assert picks[0]["selection_reason"] == "gold_coverage_good"


def test_gold_coverage_selection_never_lets_source_balance_override_true_extremity():
    """A near-zero min_score candidate must never win the 'good' bucket just because its source
    has fewer existing gold points -- that would defeat the purpose of sampling genuine
    extremes. Source-balance may only decide between candidates that are already comparably
    extreme (see the 0.1-wide rounding bucket in _select_gold_coverage_samples)."""
    rows = [
        _rollup_row(
            source_key_value="city-a",
            evidence={
                "existing-gold": {
                    "sample_id": "existing-gold",
                    "gold_text": "x",
                    "manual_decision": "both_correct",
                    "metrics": {"A": {"auto_score": 0.5}, "B": {"auto_score": 0.5}},
                },
                "truly-good": _coverage_candidate(
                    "truly-good", score_a=0.95, score_b=0.92, l2_used=True
                ),
            },
        ),
        _rollup_row(
            source_key_value="city-b",
            evidence={
                "barely-good": _coverage_candidate(
                    "barely-good", score_a=0.05, score_b=0.2, l2_used=True
                ),
            },
        ),
    ]
    picks = _select_gold_coverage_samples(
        rows, already_selected_ids=set(), good_limit=1, bad_limit=0
    )
    assert [p["sample_id"] for p in picks] == ["truly-good"]


def test_gold_coverage_selection_respects_limits_and_excludes_already_selected():
    rows = [
        _rollup_row(
            evidence={
                "good1": _coverage_candidate("good1", score_a=0.95, score_b=0.93),
                "good2": _coverage_candidate("good2", score_a=0.85, score_b=0.83),
                "bad1": _coverage_candidate("bad1", score_a=0.1, score_b=0.05),
            }
        )
    ]
    picks = _select_gold_coverage_samples(
        rows, already_selected_ids={"good1"}, good_limit=1, bad_limit=1
    )
    ids = {p["sample_id"] for p in picks}
    assert "good1" not in ids  # already in the needs_review batch, must not be re-offered
    assert ids == {"good2", "bad1"}


def test_gold_fields_both_correct_requires_agreement_floor():
    config = QualityConfig()
    evidence = {
        "pair_metrics": {"text_agreement": 0.95},
        "blind_mapping": _blind("provider-align", "asr-challenger"),
        "metrics": {"A": {"text": "the correct text"}, "B": {"text": "the correct text"}},
    }
    parsed = {"manual_decision": "both_correct", "correction_text": ""}
    gold = _gold_fields(parsed, evidence, config=config)
    assert gold["gold_text"] == "the correct text"
    assert gold["gold_source"] == "both_correct"
    assert gold["gold_role"] == "provider-align"

    low_agreement = dict(evidence, pair_metrics={"text_agreement": 0.5})
    rejected = _gold_fields(parsed, low_agreement, config=config)
    assert rejected["gold_text"] is None
    assert rejected["gold_source"] is None


def test_gold_fields_neither_requires_an_actual_edited_correction():
    config = QualityConfig()
    evidence = {"pair_metrics": {}, "blind_mapping": {}, "metrics": {}}
    edited = _gold_fields(
        {"manual_decision": "neither", "correction_text": "what I actually heard"},
        evidence,
        config=config,
    )
    assert edited["gold_text"] == "what I actually heard"
    assert edited["gold_source"] == "reviewer_correction"

    untouched = _gold_fields(
        {"manual_decision": "neither", "correction_text": ""}, evidence, config=config
    )
    assert untouched["gold_text"] is None


def test_ingest_review_decision_harvests_gold_from_both_correct(tmp_path):
    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                _rollup_row(
                    evidence={
                        "sample-1": {
                            "sample_id": "sample-1",
                            "episode_uid": "ep-1",
                            "review_page": "https://example.invalid/review",
                            "needs_review": True,
                            "pair_metrics": {"text_agreement": 0.99},
                            "blind_mapping": _blind("provider-align", "asr-challenger"),
                            "metrics": {
                                "A": {"auto_score": 0.9, "text": "the correct transcript"},
                                "B": {"auto_score": 0.88, "text": "the correct transcript"},
                            },
                        }
                    }
                )
            ],
        },
    )
    body = render_issue_body(
        {
            "sample_id": "sample-1",
            "source_key": "src-1",
            "body_key": "city-council",
            "episode_uid": "ep-1",
            "review_page": "https://example.invalid/review",
            "blind_mapping": _blind("provider-align", "asr-challenger"),
            "pair_metrics": {"text_agreement": 0.99},
            "metrics": {
                "A": {"auto_score": 0.9, "text": "the correct transcript"},
                "B": {"auto_score": 0.88, "text": "the correct transcript"},
            },
        }
    ).replace("- [ ] Both fully correct", "- [x] Both fully correct")

    ingest_review_decision(state_dir, issue_number=1, issue_body=body)
    evidence = load_rollups(state_dir)["rows"][0]["evidence"]["sample-1"]
    assert evidence["gold_text"] == "the correct transcript"
    assert evidence["gold_source"] == "both_correct"
    assert evidence["gold_role"] == "provider-align"


def test_ingest_review_decision_re_review_clears_stale_gold(tmp_path):
    """asr-quality-ingest.yml re-triggers on issue edits even after close -- a reviewer flipping
    an earlier both_correct verdict must not leave the old gold reference behind."""
    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                _rollup_row(
                    evidence={
                        "sample-1": {
                            "sample_id": "sample-1",
                            "episode_uid": "ep-1",
                            "review_page": "https://example.invalid/review",
                            "pair_metrics": {"text_agreement": 0.99},
                            "blind_mapping": _blind("provider-align", "asr-challenger"),
                            "metrics": {
                                "A": {"auto_score": 0.9, "text": "the correct transcript"},
                                "B": {"auto_score": 0.88, "text": "the correct transcript"},
                            },
                        }
                    }
                )
            ],
        },
    )
    base_sample = {
        "sample_id": "sample-1",
        "source_key": "src-1",
        "body_key": "city-council",
        "episode_uid": "ep-1",
        "review_page": "https://example.invalid/review",
        "blind_mapping": _blind("provider-align", "asr-challenger"),
        "pair_metrics": {"text_agreement": 0.99},
        "metrics": {
            "A": {"auto_score": 0.9, "text": "the correct transcript"},
            "B": {"auto_score": 0.88, "text": "the correct transcript"},
        },
    }
    both_correct_body = render_issue_body(base_sample).replace(
        "- [ ] Both fully correct", "- [x] Both fully correct"
    )
    ingest_review_decision(state_dir, issue_number=1, issue_body=both_correct_body)
    assert load_rollups(state_dir)["rows"][0]["evidence"]["sample-1"]["gold_text"] is not None

    a_better_body = render_issue_body(base_sample).replace("- [ ] A is better", "- [x] A is better")
    ingest_review_decision(state_dir, issue_number=1, issue_body=a_better_body)
    evidence = load_rollups(state_dir)["rows"][0]["evidence"]["sample-1"]
    assert evidence["manual_decision"] == "a_better"
    assert evidence["gold_text"] is None
    assert evidence["gold_source"] is None


def test_collect_gold_points_and_report_end_to_end(tmp_path):
    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                _rollup_row(
                    evidence={
                        "gold-1": {
                            "sample_id": "gold-1",
                            "gold_text": "the correct meeting text",
                            "gold_source": "both_correct",
                            "gold_role": "provider-align",
                            "manual_decision": "both_correct",
                            "blind_mapping": _blind("provider-align", "asr-challenger"),
                            "metrics": {
                                "A": {
                                    "text": "the correct meeting text",
                                    "auto_score": 0.9,
                                    "l2_mean_score": 0.85,
                                },
                                "B": {
                                    "text": "the correct meeting taxed",
                                    "auto_score": 0.6,
                                    "l2_mean_score": 0.55,
                                },
                            },
                            "l2_used": True,
                        },
                        "no-gold": {
                            "sample_id": "no-gold",
                            "gold_text": None,
                            "manual_decision": "a_better",
                            "metrics": {"A": {"auto_score": 0.7}, "B": {"auto_score": 0.3}},
                            "l2_used": False,
                        },
                    }
                )
            ],
        },
    )
    points = collect_gold_points(state_dir)
    assert len(points) == 2  # one per candidate label of the single gold-bearing sample
    reference_point = next(p for p in points if p["is_reference"])
    assert reference_point["wer"] == 0.0  # same-role self-comparison is trivially zero WER
    other_point = next(p for p in points if not p["is_reference"])
    assert other_point["wer"] > 0.0

    from citypods.transcript_quality import _gold_harvest_stats

    stats = _gold_harvest_stats(state_dir)
    assert stats["l2_coverage"] == {
        "total_evaluated": 2,
        "l2_scored": 1,
        "l2_coverage_rate": 0.5,
    }
    assert stats["agreement_floor"] == {
        "both_correct_total": 1,
        "accepted": 1,
        "rejected": 0,
    }
    flagged = collect_flagged_corrections(state_dir)
    assert flagged == []  # no reviewer_correction gold in this fixture

    report = build_calibration_report(
        points, scan_stats=stats, flagged_corrections=flagged, config=QualityConfig()
    )
    assert report["point_count"] == 2
    assert report["l2_correlation_point_count"] == 2
    assert sum(bucket["count"] for bucket in report["auto_score_histogram"]) == 2


def test_run_calibration_appends_and_caps_trend_log(tmp_path):
    state_dir = tmp_path / "state"
    save_rollups(state_dir, {"version": 1, "rows": []})
    config = QualityConfig(calibration_trend_cap=2)
    for _ in range(3):
        run_calibration(state_dir, config=config)
    trend = load_calibration_trend(state_dir)
    assert len(trend["runs"]) == 2  # capped, oldest evicted


def test_check_gold_corrections_scores_uncheck_corrections_only(tmp_path, monkeypatch, sample_city):
    import citypods.transcript_quality as tq

    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                _rollup_row(
                    source_key_value=tq.source_key(sample_city),
                    evidence={
                        "sample-1": {
                            "sample_id": "sample-1",
                            "episode_uid": "ep-1",
                            "clip_start": 0.0,
                            "clip_end": 3.0,
                            "gold_text": "hello world",
                            "gold_source": "reviewer_correction",
                        },
                        "sample-2": {
                            "sample_id": "sample-2",
                            "episode_uid": "ep-1",
                            "gold_text": "already checked",
                            "gold_source": "reviewer_correction",
                            "gold_ctc_fit_score": 0.5,
                            "gold_ctc_checked_at": "2026-01-01T00:00:00+00:00",
                        },
                        "sample-3": {
                            "sample_id": "sample-3",
                            "episode_uid": "ep-1",
                            "gold_text": "not a correction",
                            "gold_source": "both_correct",
                        },
                    },
                )
            ],
        },
    )

    class FakeEpisode:
        hosted_audio_url = "https://example.invalid/audio.m4a"

    monkeypatch.setattr(
        "citypods.transcript_quality.load_records",
        lambda state_dir, src_key: {"ep-1": {"uid": "ep-1"}},
    )
    monkeypatch.setattr("citypods.transcript_quality.record_to_episode", lambda rec: FakeEpisode())
    monkeypatch.setattr(
        "citypods.transcript_quality._download_audio_to_path",
        lambda url, dest: dest.write_bytes(b"fake"),
    )

    fit_calls: list[str] = []

    def fake_ctc_fit(audio_path, text, *, clip_start, clip_end, language):
        fit_calls.append(text)
        return CtcFitResult(mean_score=0.42, coverage=1.0, word_count=2, aligned_word_count=2)

    monkeypatch.setattr("citypods.ctc_align.ctc_fit", fake_ctc_fit)

    result = check_gold_corrections(state_dir, cities_by_slug={sample_city.slug: sample_city})
    assert result["checked"] == 1
    assert fit_calls == ["hello world"]

    evidence = load_rollups(state_dir)["rows"][0]["evidence"]
    assert evidence["sample-1"]["gold_ctc_fit_score"] == 0.42
    assert evidence["sample-1"]["gold_ctc_checked_at"] is not None
    assert evidence["sample-2"]["gold_ctc_fit_score"] == 0.5  # already checked, untouched
    assert "gold_ctc_fit_score" not in evidence["sample-3"]  # not a reviewer_correction


def test_check_gold_corrections_skips_write_if_correction_edited_mid_run(
    tmp_path, monkeypatch, sample_city
):
    """Audio download + CTC inference (the expensive read/score pass) happens before any CAS
    write. If a reviewer edits the correction via the separate ingest-review workflow in that
    window, the queued update must not clobber the new text with a score computed against the
    old one -- that would both show a wrong score and mark it "checked", so the new text would
    never get scored at all (check_gold_corrections skips anything already checked)."""
    import citypods.transcript_quality as tq

    state_dir = tmp_path / "state"
    save_rollups(
        state_dir,
        {
            "version": 1,
            "rows": [
                _rollup_row(
                    source_key_value=tq.source_key(sample_city),
                    evidence={
                        "sample-1": {
                            "sample_id": "sample-1",
                            "episode_uid": "ep-1",
                            "clip_start": 0.0,
                            "clip_end": 3.0,
                            "gold_text": "foo",
                            "gold_source": "reviewer_correction",
                        },
                    },
                )
            ],
        },
    )

    class FakeEpisode:
        hosted_audio_url = "https://example.invalid/audio.m4a"

    monkeypatch.setattr(
        "citypods.transcript_quality.load_records",
        lambda state_dir, src_key: {"ep-1": {"uid": "ep-1"}},
    )
    monkeypatch.setattr("citypods.transcript_quality.record_to_episode", lambda rec: FakeEpisode())
    monkeypatch.setattr(
        "citypods.transcript_quality._download_audio_to_path",
        lambda url, dest: dest.write_bytes(b"fake"),
    )

    def fake_ctc_fit(audio_path, text, *, clip_start, clip_end, language):
        # Simulate a reviewer's concurrent edit landing (via ingest_review_decision, a separate
        # workflow) in the window between this read/score pass and check_gold_corrections' own
        # CAS write below.
        def _edit(rows: list[dict]) -> list[dict]:
            rows[0]["evidence"]["sample-1"]["gold_text"] = "bar"
            return rows

        from citypods.transcript_quality import mutate_rollups_ledger

        mutate_rollups_ledger(state_dir, None, _edit)
        return CtcFitResult(mean_score=0.42, coverage=1.0, word_count=2, aligned_word_count=2)

    monkeypatch.setattr("citypods.ctc_align.ctc_fit", fake_ctc_fit)

    result = check_gold_corrections(state_dir, cities_by_slug={sample_city.slug: sample_city})
    assert result["checked"] == 1  # scored once, even though the write was skipped

    evidence = load_rollups(state_dir)["rows"][0]["evidence"]["sample-1"]
    assert evidence["gold_text"] == "bar"  # the concurrent edit, not clobbered
    assert "gold_ctc_fit_score" not in evidence  # stale score for "foo" never applied
    assert "gold_ctc_checked_at" not in evidence  # "bar" can still be picked up by a future run


def test_parse_issue_decision_tolerates_crlf_issue_bodies():
    """GitHub normalizes issue bodies to CRLF after any edit through the web UI (HTML textarea
    submission). _CORRECTION_RE's fence boundaries are literal "```\\n...\\n```" -- without
    normalizing the body first, a CRLF-line-ended body would never match, silently disabling the
    entire reviewer-correction gold-harvest path in production even though every test here
    builds bodies in-memory (LF only) and would never catch it."""
    sample = {
        "sample_id": "sample-1",
        "source_key": "src-1",
        "body_key": "city-council",
        "episode_uid": "ep-1",
        "review_page": "https://example.invalid/review",
        "blind_mapping": {"A": {"role": "provider-align"}, "B": {"role": "asr-challenger"}},
        "metrics": {
            "A": {"auto_score": 0.4, "text": "a candidate text"},
            "B": {"auto_score": 0.8, "text": "b candidate text"},
        },
    }
    body = render_issue_body(sample)
    body = body.replace("- [ ] Neither usable", "- [x] Neither usable")
    body = body.replace("b candidate text", "what was actually said", 1)
    crlf_body = body.replace("\n", "\r\n")
    result = parse_issue_decision(crlf_body)
    assert result["manual_decision"] == "neither"
    assert result["correction_text"] == "what was actually said"


def test_gold_fields_clears_stale_ctc_score_when_correction_text_changes():
    """check_gold_corrections skips any evidence with gold_ctc_checked_at already set. If a
    reviewer edits a correction from 'foo' to 'bar' without this clearing the old score, 'foo's
    stale CTC fit would stay attached to 'bar' forever -- never re-scored."""
    config = QualityConfig()
    evidence = {
        "pair_metrics": {},
        "blind_mapping": {},
        "metrics": {},
        "gold_text": "foo",
        "gold_source": "reviewer_correction",
        "gold_ctc_fit_score": 0.9,
        "gold_ctc_checked_at": "2026-01-01T00:00:00+00:00",
    }
    changed = _gold_fields(
        {"manual_decision": "neither", "correction_text": "bar"}, evidence, config=config
    )
    assert changed["gold_text"] == "bar"
    assert changed["gold_ctc_fit_score"] is None
    assert changed["gold_ctc_checked_at"] is None


def test_gold_fields_preserves_ctc_score_when_correction_text_is_unchanged():
    """A no-op resubmission of the same correction text must not discard an already-computed,
    still-valid CTC score."""
    config = QualityConfig()
    evidence = {
        "pair_metrics": {},
        "blind_mapping": {},
        "metrics": {},
        "gold_text": "foo",
        "gold_source": "reviewer_correction",
        "gold_ctc_fit_score": 0.9,
        "gold_ctc_checked_at": "2026-01-01T00:00:00+00:00",
    }
    unchanged = _gold_fields(
        {"manual_decision": "neither", "correction_text": "foo"}, evidence, config=config
    )
    assert "gold_ctc_fit_score" not in unchanged
    assert "gold_ctc_checked_at" not in unchanged


def test_auto_score_histogram_places_boundary_values_in_the_upper_bucket():
    """0.6 / 0.2 == 2.9999999999999996 in binary floating point -- a naive int() truncation
    would silently misfile an exact bucket-boundary score into the bucket below it."""
    points = [{"auto_score": 0.6}, {"auto_score": 0.4}, {"auto_score": 0.2}]
    histogram = {b["range"]: b["count"] for b in _auto_score_histogram(points)}
    assert histogram["0.6-0.8"] == 1
    assert histogram["0.4-0.6"] == 1
    assert histogram["0.2-0.4"] == 1
