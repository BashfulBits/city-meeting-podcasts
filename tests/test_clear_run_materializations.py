"""Tests for scripts/clear_run_materializations.py (the #39 run-undo tool)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import clear_run_materializations as crm  # noqa: E402

from citypods.records import load_records, save_records  # noqa: E402

# Real log format (source= before uid=), trimmed of the middle slug/provider/guid fields for width.
LOG = """
[enrich] audio encode done source=3b7588310856 uid=aaa111 bytes=258 seconds=5.4
[enrich] audio encode done source=3b7588310856 uid=bbb222 bytes=5724059 seconds=36.4
[enrich] audio encode error source=other999 uid=zzz999 seconds=0.0: boom
[enrich] audio encode done source=0134e94361a5 uid=ccc333 bytes=1219190 seconds=11.3
"""


def _rec(uid, key, url, bytes_, attempts=0):
    return {
        "uid": uid,
        "title": "Meeting",
        "audio": {
            "key": key,
            "url": url,
            "spec_hash": "sp",
            "bytes": bytes_,
            "encode_time": "t",
            "duration_served": 5.0,
            "attempts": attempts,
            "last_attempt": "t",
            "error": None,
        },
    }


def test_parse_materialized_extracts_done_lines_only():
    affected = crm.parse_materialized(LOG)
    assert affected == {"3b7588310856": {"aaa111", "bbb222"}, "0134e94361a5": {"ccc333"}}
    assert "other999" not in affected  # the `error` line did not materialize


def test_clear_resets_audio_block_and_backoff(tmp_path):
    sk = "3b7588310856"
    save_records(
        tmp_path,
        sk,
        {
            "aaa111": _rec("aaa111", "audio/k1", "https://cdn/k1", 258, attempts=2),
            "bbb222": _rec("bbb222", "audio/k2", "https://cdn/k2", 5724059),
            "keepme": _rec("keepme", "audio/k3", "https://cdn/k3", 999999),  # not in the run
        },
    )
    summary = crm.clear_materializations(tmp_path, {sk: {"aaa111", "bbb222"}}, apply=True)

    assert summary["cleared"] == 2
    assert summary["object_keys"] == ["audio/k1", "audio/k2"]  # sorted by uid
    recs = load_records(tmp_path, sk)
    for uid in ("aaa111", "bbb222"):
        a = recs[uid]["audio"]
        assert a["key"] is None and a["url"] is None and a["spec_hash"] is None
        assert a["bytes"] is None and a["duration_served"] is None
        assert a["attempts"] == 0 and a["error"] is None
    assert recs["keepme"]["audio"]["url"] == "https://cdn/k3"  # untouched


def test_clear_dry_run_persists_nothing(tmp_path):
    sk = "s1"
    save_records(tmp_path, sk, {"u1": _rec("u1", "audio/k", "https://cdn/k", 258)})
    summary = crm.clear_materializations(tmp_path, {sk: {"u1"}}, apply=False)
    assert summary["cleared"] == 1  # reports what it WOULD clear
    assert load_records(tmp_path, sk)["u1"]["audio"]["url"] == "https://cdn/k"  # but unchanged


def test_clear_deletes_objects_when_requested(tmp_path):
    sk = "s1"
    save_records(tmp_path, sk, {"u1": _rec("u1", "audio/k", "https://cdn/k", 258)})

    class FakeStorage:
        """Implements enough of the storage protocol for push_state() to treat this as a
        real, sync-capable backend -- clear_materializations() now requires a successful
        durable push before it will delete a source's objects (CR; see review/19)."""

        def __init__(self):
            self.deleted: list[str] = []
            self.put: list[str] = []
            self.events: list[tuple[str, str]] = []

        def delete(self, key):
            self.deleted.append(key)
            self.events.append(("delete", key))

        def get_file(self, key, local_path):
            return False

        def list_objects(self, prefix=""):
            return iter(())

        def put_file(self, key, local_path, content_type):
            self.put.append(key)
            self.events.append(("put", key))

    st = FakeStorage()
    summary = crm.clear_materializations(
        tmp_path, {sk: {"u1"}}, storage=st, delete_objects=True, apply=True
    )
    assert st.put, "durable push must happen before the object delete"
    assert st.deleted == ["audio/k"]
    put_positions = [i for i, (event, _) in enumerate(st.events) if event == "put"]
    delete_positions = [i for i, (event, _) in enumerate(st.events) if event == "delete"]
    assert put_positions and delete_positions
    assert max(put_positions) < min(delete_positions)  # push happens before delete
    assert summary["deleted"] == 1


def test_clear_skips_object_delete_when_push_fails(tmp_path):
    # If the durable push doesn't actually persist anything (push_state returns 0), the object
    # must not be deleted -- that would leave a durable record pointing at a missing object.
    sk = "s1"
    save_records(tmp_path, sk, {"u1": _rec("u1", "audio/k", "https://cdn/k", 258)})

    class UnsupportedStorage:
        """Lacks the storage protocol push_state() needs, so push_state() always returns 0."""

        def __init__(self):
            self.deleted: list[str] = []

        def delete(self, key):
            self.deleted.append(key)

    st = UnsupportedStorage()
    summary = crm.clear_materializations(
        tmp_path, {sk: {"u1"}}, storage=st, delete_objects=True, apply=True
    )
    assert st.deleted == []
    assert summary["deleted"] == 0
    # The record mutation (audio cleared) still happened locally even though the delete was
    # skipped -- only the destructive object delete is gated on the durable push.
    assert summary["cleared"] == 1


def test_clear_skips_records_with_no_hosted_audio(tmp_path):
    sk = "s1"
    save_records(tmp_path, sk, {"u1": {"uid": "u1", "audio": {"key": None, "url": None}}})
    summary = crm.clear_materializations(tmp_path, {sk: {"u1"}}, apply=True)
    assert summary["cleared"] == 0


def test_main_reports_actual_deleted_count_not_candidate_count(monkeypatch, tmp_path, capsys):
    # CR2-SC-14: the final summary line must report summary["deleted"] (what was actually
    # removed) once applying, not len(object_keys) (every candidate, including ones whose
    # per-source push failed and so were never actually deleted).
    sk = "3b7588310856"
    save_records(tmp_path, sk, {"aaa111": _rec("aaa111", "audio/k", "https://cdn/k", 258)})
    log_file = tmp_path / "run.log"
    log_file.write_text(LOG)

    class UnsupportedStorage:
        """Lacks the storage protocol push_state() needs, so push_state() always returns 0 —
        every candidate delete is skipped, but object_keys still lists it as a candidate."""

        def delete(self, key):
            raise AssertionError("delete must not be called when the durable push failed")

    monkeypatch.setattr(crm, "make_storage", lambda *a, **k: UnsupportedStorage())
    monkeypatch.setattr(crm, "load_site_config", lambda *a, **k: {})
    monkeypatch.setattr(crm, "resolve_state_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(crm, "pull_state", lambda *a, **k: 0)

    rc = crm.main(["--log-file", str(log_file), "--apply", "--delete-objects"])
    assert rc == 0
    out = capsys.readouterr().out
    # The push failed, so nothing was actually deleted — the accurate count is 0.
    assert "0 object(s) deleted" in out
    assert "1 object(s) deleted" not in out  # would be len(object_keys) under the old bug
