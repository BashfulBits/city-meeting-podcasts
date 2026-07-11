from __future__ import annotations

import json
from pathlib import Path

import pytest

from citypods.security import SecurityError
from citypods.transcript_quality import (
    QualityConfig,
    TranscriptQualityRoute,
    _blind_mapping,
    _normalize_rollups,
    _read_ref_bytes,
    _render_review_page,
    accepted_recipe_allowed,
    evaluate_samples,
    ingest_review_decision,
    load_quality_routes,
    load_raw_log,
    load_rollups,
    load_rollups_ledger,
    package_reviews,
    parse_issue_decision,
    render_issue_body,
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
    with pytest.raises(ValueError, match="exactly one"):
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
        "- [ ] Tie / no meaningful difference",
        "- [x] Tie / no meaningful difference",
    )

    result = ingest_review_decision(
        state_dir,
        issue_number=12,
        issue_body=edited,
        actor="tester",
        issue_url="https://example.invalid/issues/12",
    )
    assert result["manual_decision"] == "tie"
    rollups = load_rollups(state_dir)
    evidence = rollups["rows"][0]["evidence"]["sample-1"]
    assert evidence["manual_decision"] == "tie"
    assert evidence["reviewed_by"] == "tester"


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
