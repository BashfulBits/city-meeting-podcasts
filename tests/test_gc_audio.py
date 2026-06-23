"""Unit + integration coverage for the orphan-GC report (scripts/gc_audio.py, GH#421 follow-up)."""

from __future__ import annotations

import json

from citypods.records import save_records
from citypods.storage.local import LocalStorage
from scripts import gc_audio


def test_attribute_maps_audio_and_transcript_keys_to_city():
    mapping = {"abc123": "denton-county-tx"}
    assert gc_audio.attribute("granicus/abc123/uid1-spec.m4a", mapping) == "denton-county-tx"
    assert gc_audio.attribute("transcripts/abc123/uid1-spec.vtt", mapping) == "denton-county-tx"
    # A source_key with no configured city, and non-source-scoped keys.
    assert gc_audio.attribute("granicus/zzz999/uid1-spec.m4a", mapping) == "(unconfigured)"
    assert gc_audio.attribute("clips/uid1.m4a", mapping) == "(other)"
    assert gc_audio.attribute("state/sources/x.json", mapping) == "(other)"


def test_summarize_groups_by_city_with_totals():
    orphans = [
        gc_audio.Orphan("granicus/a/u1-s.m4a", 100, "fort-worth-tx", None),
        gc_audio.Orphan("granicus/a/u2-s.m4a", 200, "fort-worth-tx", None),
        gc_audio.Orphan("transcripts/b/u3-s.vtt", 50, "denton-county-tx", None),
    ]
    summary = gc_audio.summarize(orphans)
    assert summary["total_files"] == 3
    assert summary["total_bytes"] == 350
    assert summary["by_city"]["fort-worth-tx"] == {"files": 2, "bytes": 300}
    assert summary["by_city"]["denton-county-tx"] == {"files": 1, "bytes": 50}


def test_human_bytes():
    assert gc_audio.human_bytes(512) == "512 B"
    assert gc_audio.human_bytes(1536) == "1.5 KB"
    assert gc_audio.human_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_render_issue_body_has_per_city_table_and_total():
    summary = gc_audio.summarize(
        [
            gc_audio.Orphan("granicus/a/u1-s.m4a", 1024 * 1024, "fort-worth-tx", None),
            gc_audio.Orphan("transcripts/b/u2-s.vtt", 0, "(unconfigured)", None),
        ]
    )
    body = gc_audio.render_issue_body(summary, applied=False)
    assert "| City | Files | Size |" in body
    assert "| fort-worth-tx | 1 | 1.0 MB |" in body
    assert "| **Total** | **2** |" in body
    assert "dry-run" in body
    assert "apply = true" in body


def test_local_iter_objects_returns_size(tmp_path):
    store = LocalStorage(root=tmp_path / "bucket", url_prefix="https://cdn")
    (tmp_path / "bucket" / "p").mkdir(parents=True)
    (tmp_path / "bucket" / "p" / "a.m4a").write_bytes(b"hello")
    rows = list(store.iter_objects())
    assert rows == [("p/a.m4a", rows[0][1], 5)]


def test_gc_out_report_written_on_dry_run(tmp_path):
    out = tmp_path / "docs"
    audio = out / "audio" / "p"
    audio.mkdir(parents=True)
    (audio / "kept.m4a").write_bytes(b"1")
    (audio / "orphan.m4a").write_bytes(b"22")

    state = tmp_path / "state"
    save_records(state, "src", {"u1": {"audio": {"key": "p/kept.m4a", "url": "x"}}})
    (tmp_path / "site.yml").write_text(
        f"state_dir: {state}\ndefaults:\n  audio_storage_backend: local\n"
    )
    report = tmp_path / "gc-out"
    argv = [
        "--site-config",
        str(tmp_path / "site.yml"),
        "--output-dir",
        str(out),
        "--min-age-days",
        "0",
        "--out",
        str(report),
    ]
    assert gc_audio.main(argv) == 0
    # Dry-run: orphan survives, but the report lists it.
    assert (audio / "orphan.m4a").exists()
    assert (report / "has_orphans").read_text() == "true"
    summary = json.loads((report / "summary.json").read_text())
    assert summary["total_files"] == 1
    assert summary["total_bytes"] == 2
    tsv = (report / "orphans.tsv").read_text()
    assert "p/orphan.m4a" in tsv
    assert "p/kept.m4a" not in tsv  # referenced, not an orphan
    assert "| **Total** | **1** |" in (report / "issue-body.md").read_text()
