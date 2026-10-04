"""Tests for the raw-PDF-bytes agenda state reset tool."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import reset_raw_pdf_agenda_state as reset  # noqa: E402

from citypods.records import save_records  # noqa: E402

_HIT = {
    "key": "documents/src-1/uid-1/agenda-bad",
    "prefix": "%PDF-1.5\r%\r\n1 0 obj",
    "episodes": [{"slug": "example-city", "uid": "uid-1"}],
}


def test_load_hit_targets_accepts_bare_list_shape(tmp_path):
    hits_file = tmp_path / "hits.json"
    hits_file.write_text(json.dumps([_HIT]))

    targets = reset.load_hit_targets(hits_file, {"example-city": "src-1"})

    assert targets == {"src-1": {"uid-1": "documents/src-1/uid-1/agenda-bad"}}


def test_load_hit_targets_accepts_hits_and_failed_keys_shape(tmp_path):
    """Regression: the survey script's --json output changed shape (from a bare list) to also
    report failed_keys, so this must still parse the new {"hits": [...], "failed_keys": [...]}
    envelope, not just the older bare-list fixture."""
    hits_file = tmp_path / "hits.json"
    hits_file.write_text(json.dumps({"hits": [_HIT], "failed_keys": ["documents/src-2/x"]}))

    targets = reset.load_hit_targets(hits_file, {"example-city": "src-1"})

    assert targets == {"src-1": {"uid-1": "documents/src-1/uid-1/agenda-bad"}}


def test_load_hit_targets_skips_unknown_city_slugs(tmp_path, capsys):
    hits_file = tmp_path / "hits.json"
    hit = dict(_HIT, episodes=[{"slug": "unknown-city", "uid": "uid-9"}])
    hits_file.write_text(json.dumps([hit]))

    targets = reset.load_hit_targets(hits_file, {"example-city": "src-1"})

    assert targets == {}
    assert "unknown-city" in capsys.readouterr().err


def _record(uid: str, artifact_key: str | None) -> dict:
    rec = {"uid": uid, "title": "Meeting"}
    if artifact_key is not None:
        rec["links"] = {"agenda_text_artifact_key": artifact_key}
    return rec


def test_plan_resets_only_includes_records_still_matching_the_survey(tmp_path):
    save_records(
        tmp_path,
        "src-1",
        {
            "uid-1": _record("uid-1", "documents/src-1/uid-1/agenda-bad"),  # still corrupt
            "uid-2": _record("uid-2", "documents/src-1/uid-2/agenda-fixed"),  # reprocessed since
        },
    )
    targets = {
        "src-1": {
            "uid-1": "documents/src-1/uid-1/agenda-bad",
            "uid-2": "documents/src-1/uid-2/agenda-bad",  # stale expectation
            "uid-3": "documents/src-1/uid-3/agenda-bad",  # no longer in records at all
        }
    }

    planned = reset.plan_resets(tmp_path, targets)

    assert planned == {"src-1": ["uid-1"]}


def test_plan_resets_drops_a_source_with_no_remaining_matches(tmp_path):
    save_records(
        tmp_path, "src-1", {"uid-1": _record("uid-1", "documents/src-1/uid-1/agenda-fixed")}
    )
    targets = {"src-1": {"uid-1": "documents/src-1/uid-1/agenda-bad"}}

    assert reset.plan_resets(tmp_path, targets) == {}


class _FakeStorage:
    def __init__(self, present: set[str]):
        self._present = present

    def get_file(self, key: str, local_path: Path) -> bool:
        if key not in self._present:
            return False
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text('{"schema_version": 1, "episodes": {}}')
        return True


def test_sync_targeted_sources_reports_exact_synced_count(tmp_path):
    storage = _FakeStorage({"state/sources/src-1/episodes.json"})

    synced = reset.sync_targeted_sources(storage, tmp_path, ["src-1", "src-2"])

    assert synced == 1
