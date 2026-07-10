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

    assert changed is True
    assert summary.timeline_fallback == 1
    assert records["u1"].get("served_duration_seconds") is None
    assert records["u1"]["audio"].get("duration_served") is None
    assert rows[0]["outcome"] == "timeline-fallback"


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

    assert changed is True
    assert summary.timeline_fallback == 1
    assert summary.unchanged == 1
    assert records["u1"]["served_duration_seconds"] == 600.0
    assert records["u1"]["audio"]["duration_served"] == 600.0
    assert records["u2"]["served_duration_seconds"] == 1200.0
    assert rows[1]["outcome"] == "unchanged"


def test_normalize_records_apply_marks_legacy_match_as_changed():
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

    assert changed is True
    assert summary.changed == 1
    assert summary.timeline_fallback == 1
    assert records["u1"]["served_duration_seconds"] == 600.0
    assert records["u1"]["audio"]["duration_served"] == 600.0
    assert rows[0]["outcome"] == "timeline-fallback"


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

    assert changed is False
    assert summary.probed == 1
    assert summary.changed == 0
    assert records["u1"]["served_duration_seconds"] == 600.2
    assert rows[0]["outcome"] == "probed"


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

    assert changed is True
    assert summary.timeline_fallback == 1
    assert summary.failed == 0
    assert records["u1"]["served_duration_seconds"] == 600.0
    assert rows[0]["outcome"] == "timeline-fallback"
