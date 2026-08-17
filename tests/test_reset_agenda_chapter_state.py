"""Tests for the one-time agenda/chapter state reset tool."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reset_agenda_chapter_state as reset  # noqa: E402


def _partial_record() -> dict:
    return {
        "uid": "ep-1",
        "title": "Meeting",
        "links": {
            "agenda": "https://provider.test/agenda.pdf",
            "agenda_text_artifact": "https://cdn/old.txt",
            "agenda_text_artifact_key": "documents/source/ep-1/agenda-old",
        },
        "agenda_text": {
            "url": "https://provider.test/agenda.pdf",
            "attempts": 0,
            "quality": {"status": "accepted"},
        },
        "agenda_backup": {"url": "https://cdn/backup.json"},
        "generated_agenda_candidates": {"status": "pending", "recipe": "recipe-1"},
        "generated_chapters": [{"start": 1.0, "title": "Item"}],
        "generated_chapters_spec_hash": "recipe-2",
        "stage_completion": {
            "agenda_text": {"state": "complete"},
            "chapter_agenda": {"state": "complete"},
            "transcript": {"state": "complete"},
        },
        "audio": {"key": "audio/ep-1"},
    }


def test_needs_reset_only_matches_partial_records_without_agenda_key():
    missing_key = _partial_record()
    missing_key["links"].pop("agenda_text_artifact_key")
    assert reset.needs_reset(missing_key)
    complete = _partial_record()
    complete["links"]["agenda_text_artifact_key"] = "documents/source/ep-1/agenda-new"
    assert not reset.needs_reset(complete)
    assert not reset.needs_reset({"uid": "empty", "links": {}})


def test_reset_record_preserves_official_links_and_unrelated_state():
    record = _partial_record()

    reset.reset_record(record)

    assert record["links"]["agenda"] == "https://provider.test/agenda.pdf"
    assert record["audio"] == {"key": "audio/ep-1"}
    assert record["links"]["agenda_text_artifact"] is None
    assert record["links"]["agenda_text_artifact_key"] is None
    assert "agenda_text" not in record
    assert "agenda_backup" not in record
    assert "generated_agenda_candidates" not in record
    assert "generated_chapters" not in record
    assert "generated_chapters_spec_hash" not in record
    assert record["stage_completion"]["agenda_text"] is None
    assert record["stage_completion"]["chapter_agenda"] is None
    assert "transcript" in record["stage_completion"]


def test_plan_resets_is_deterministic_and_capped(tmp_path):
    source_dir = tmp_path / "sources" / "source-a"
    source_dir.mkdir(parents=True)
    import json

    records = {}
    for n in range(3):
        record = _partial_record() | {"uid": f"ep-{n}"}
        record["links"].pop("agenda_text_artifact_key")
        records[f"ep-{n}"] = record
    (source_dir / "episodes.json").write_text(json.dumps({"episodes": records}))

    planned = reset.plan_resets(tmp_path, ["source-a"], max_records=2)

    assert planned == {"source-a": ["ep-0", "ep-1"]}


def test_apply_reapplies_tombstones_before_each_lane_push(tmp_path, monkeypatch):
    reset.save_records(tmp_path, "source-a", {"ep-1": _partial_record()})
    calls = []

    def fake_push(storage, state_dir, source_keys, **kwargs):
        record = reset.load_records(state_dir, "source-a")["ep-1"]
        calls.append((kwargs["lane"], record["links"].get("agenda_text_artifact_key")))
        if kwargs["lane"] == "chapter":
            restored = reset.load_records(state_dir, "source-a")
            restored["ep-1"]["links"]["agenda_text_artifact_key"] = "documents/stale"
            reset.save_records(state_dir, "source-a", restored)
        return 1

    monkeypatch.setattr(reset, "push_records_merged", fake_push)

    summary = reset.reset_agenda_chapter_state(
        tmp_path,
        {"source-a": ["ep-1"]},
        apply=True,
        storage=object(),
    )

    assert summary["pushed"] == {"chapter": 1, "audio": 1}
    assert calls == [("chapter", None), ("audio", None)]
