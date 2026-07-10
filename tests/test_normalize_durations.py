from __future__ import annotations

from scripts import normalize_durations as nd


def test_normalize_records_dry_run_does_not_mutate():
    records = {
        "u1": {
            "uid": "u1",
            "audio": {"key": "audio/k1"},
            "timeline": {
                "version": "silence:2",
                "segments": [
                    {
                        "served_start": 0.0,
                        "served_end": 600.0,
                        "kind": "source",
                        "source_id": "s0",
                        "source_start": 0.0,
                        "source_end": 600.0,
                    }
                ],
            },
        }
    }

    rows, summary, changed = nd.normalize_records(
        records,
        source_key="src1",
        storage=None,
        ffmpeg_binary="ffmpeg",
        probe_existing=False,
        apply=False,
    )

    assert changed is False
    assert summary.probe_attempted == 0
    assert summary.probe_succeeded == 0
    assert summary.probe_failed == 0
    assert summary.failed == 1
    assert summary.canonical_null_before == 1
    assert summary.canonical_null_after == 1
    assert summary.legacy_null_before == 1
    assert summary.legacy_null_after == 1
    assert records["u1"].get("served_duration_seconds") is None
    assert records["u1"]["audio"].get("duration_served") is None
    assert rows[0]["outcome"] == "failed"
    assert rows[0]["reason"] == "probe-disabled"


def test_normalize_records_apply_writes_only_changed_rows():
    records = {
        "u1": {
            "uid": "u1",
            "audio": {"key": "audio/k1"},
            "timeline": {
                "version": "silence:2",
                "segments": [
                    {
                        "served_start": 0.0,
                        "served_end": 600.0,
                        "kind": "source",
                        "source_id": "s0",
                        "source_start": 0.0,
                        "source_end": 600.0,
                    }
                ],
            },
        },
        "u2": {
            "uid": "u2",
            "served_duration_seconds": 1200.0,
            "audio": {"key": "audio/k2", "duration_served": 1200.0},
        },
    }

    rows, summary, changed = nd.normalize_records(
        records,
        source_key="src1",
        storage=None,
        ffmpeg_binary="ffmpeg",
        probe_existing=False,
        apply=True,
    )

    assert changed is False
    assert summary.probe_attempted == 0
    assert summary.unchanged == 1
    assert records["u1"].get("served_duration_seconds") is None
    assert records["u1"]["audio"].get("duration_served") is None
    assert records["u2"]["served_duration_seconds"] == 1200.0
    assert rows[0]["outcome"] == "failed"
    assert rows[1]["outcome"] == "unchanged"


def test_normalize_records_apply_leaves_legacy_match_unwritten_without_probe():
    records = {
        "u1": {
            "uid": "u1",
            "audio": {"duration_served": 600.0},
            "timeline": {
                "version": "silence:2",
                "segments": [
                    {
                        "served_start": 0.0,
                        "served_end": 600.0,
                        "kind": "source",
                        "source_id": "s0",
                        "source_start": 0.0,
                        "source_end": 600.0,
                    }
                ],
            },
        }
    }

    rows, summary, changed = nd.normalize_records(
        records,
        source_key="src1",
        storage=None,
        ffmpeg_binary="ffmpeg",
        probe_existing=False,
        apply=True,
    )

    assert changed is False
    assert summary.changed == 0
    assert summary.probe_attempted == 0
    assert summary.skipped == 1
    assert summary.canonical_null_before == 1
    assert summary.canonical_null_after == 1
    assert summary.legacy_set_before == 1
    assert summary.legacy_set_after == 1
    assert records["u1"].get("served_duration_seconds") is None
    assert records["u1"]["audio"]["duration_served"] == 600.0
    assert rows[0]["outcome"] == "skipped"


def test_normalize_records_probe_tolerance_avoids_spurious_changes(monkeypatch):
    records = {
        "u1": {
            "uid": "u1",
            "served_duration_seconds": 0,
            "audio": {"key": "audio/k1", "duration_served": 600.0},
        }
    }

    monkeypatch.setattr(
        nd,
        "probe_hosted_audio_duration_seconds",
        lambda storage, key, *, ffmpeg_binary="ffmpeg": (600.2, "stream-sample"),
    )

    rows, summary, changed = nd.normalize_records(
        records,
        source_key="src1",
        storage=object(),
        ffmpeg_binary="ffmpeg",
        probe_existing=True,
        apply=True,
    )

    assert changed is True
    assert summary.probe_attempted == 1
    assert summary.probe_succeeded == 1
    assert summary.changed == 1
    assert summary.canonical_written_from_probe == 1
    assert summary.legacy_matched_probe_before == 1
    assert summary.canonical_matched_probe_after == 1
    assert records["u1"]["served_duration_seconds"] == 600.2
    assert rows[0]["outcome"] == "probed"


def test_normalize_records_probe_verifies_and_corrects_canonical_value(monkeypatch):
    records = {
        "u1": {
            "uid": "u1",
            "served_duration_seconds": 590.0,
            "audio": {"key": "audio/k1", "duration_served": 590.0},
        }
    }

    monkeypatch.setattr(
        nd,
        "probe_hosted_audio_duration_seconds",
        lambda storage, key, *, ffmpeg_binary="ffmpeg": (600.0, "stream-sample"),
    )

    rows, summary, changed = nd.normalize_records(
        records,
        source_key="src1",
        storage=object(),
        ffmpeg_binary="ffmpeg",
        probe_existing=True,
        apply=True,
    )

    assert changed is True
    assert summary.probe_attempted == 1
    assert summary.probe_succeeded == 1
    assert summary.canonical_set_before == 1
    assert summary.canonical_mismatched_probe_before == 1
    assert summary.canonical_matched_probe_after == 1
    assert summary.canonical_written_from_probe == 1
    assert records["u1"]["served_duration_seconds"] == 600.0
    assert rows[0]["probe_served_duration_seconds"] == 600.0


def test_normalize_records_probe_counts_already_matching_canonical_as_unchanged(monkeypatch):
    records = {
        "u1": {
            "uid": "u1",
            "served_duration_seconds": 600.0,
            "audio": {"key": "audio/k1", "duration_served": 600.0},
        }
    }

    monkeypatch.setattr(
        nd,
        "probe_hosted_audio_duration_seconds",
        lambda storage, key, *, ffmpeg_binary="ffmpeg": (600.2, "stream-sample"),
    )

    rows, summary, changed = nd.normalize_records(
        records,
        source_key="src1",
        storage=object(),
        ffmpeg_binary="ffmpeg",
        probe_existing=True,
        apply=True,
    )

    assert changed is False
    assert summary.probe_succeeded == 1
    assert summary.unchanged == 1
    assert summary.canonical_unchanged_match_probe == 1
    assert summary.canonical_matched_probe_before == 1
    assert summary.canonical_matched_probe_after == 1
    assert rows[0]["after_served_duration_seconds"] == 600.0
    assert records["u1"]["served_duration_seconds"] == 600.0


def test_normalize_records_probe_match_does_not_mutate_record(monkeypatch):
    records = {
        "u1": {
            "uid": "u1",
            "served_duration_seconds": 600.0,
            "audio": {"key": "audio/k1", "duration_served": 600.0},
        },
        "u2": {
            "uid": "u2",
            "audio": {"key": "audio/k2"},
        },
    }

    def _probe(_storage, key, *, ffmpeg_binary="ffmpeg"):
        if key == "audio/k1":
            return 600.2, "stream-sample"
        return 601.0, "stream-sample"

    monkeypatch.setattr(nd, "probe_hosted_audio_duration_seconds", _probe)

    rows, summary, changed = nd.normalize_records(
        records,
        source_key="src1",
        storage=object(),
        ffmpeg_binary="ffmpeg",
        probe_existing=True,
        apply=True,
    )

    assert changed is True
    assert summary.changed == 1
    assert summary.unchanged == 1
    assert records["u1"]["served_duration_seconds"] == 600.0
    assert records["u2"]["served_duration_seconds"] == 601.0
    assert rows[0]["after_served_duration_seconds"] == 600.0


def test_normalize_records_probe_exception_reports_failure_and_continues(monkeypatch):
    records = {
        "u1": {
            "uid": "u1",
            "audio": {"key": "audio/k1"},
            "timeline": {
                "version": "silence:2",
                "segments": [
                    {
                        "served_start": 0.0,
                        "served_end": 600.0,
                        "kind": "source",
                        "source_id": "s0",
                        "source_start": 0.0,
                        "source_end": 600.0,
                    }
                ],
            },
        }
    }

    def _probe(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(nd, "probe_hosted_audio_duration_seconds", _probe)

    rows, summary, changed = nd.normalize_records(
        records,
        source_key="src1",
        storage=object(),
        ffmpeg_binary="ffmpeg",
        probe_existing=True,
        apply=True,
    )

    assert changed is False
    assert summary.probe_attempted == 1
    assert summary.probe_succeeded == 0
    assert summary.probe_failed == 1
    assert summary.failed == 1
    assert records["u1"].get("served_duration_seconds") is None
    assert rows[0]["outcome"] == "failed"


def test_render_summary_outputs_before_after_table():
    summary = nd.NormalizeSummary(
        examined=10,
        changed=2,
        probe_attempted=4,
        probe_succeeded=3,
        probe_failed=1,
        canonical_null_before=5,
        canonical_null_after=3,
        canonical_set_before=5,
        canonical_set_after=7,
        canonical_written_from_probe=2,
    )

    rendered = nd._render_summary(summary, apply=False)

    assert "| Metric | Before | After |" in rendered
    assert "| Probe attempted | 0 | 4 |" in rendered
    assert "| Canonical served_duration_seconds null | 5 | 3 |" in rendered
    assert "| Canonical written from probe | 0 | 2 |" in rendered
