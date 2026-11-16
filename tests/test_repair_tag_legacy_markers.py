import json

from citypods.compute.llm_deferred import _read_json, _write_json, deferred_failure_key
from scripts import repair_tag_legacy_markers as module
from tests.test_compute_llm_deferred import MemStorage


def test_repair_checks_exact_snapshot_archives_and_preserves_failed_ref(monkeypatch):
    storage = MemStorage()
    recipe, ref = "recipe", "worker-id"
    marker = {
        "task": "tag",
        "status": "exhausted",
        "failure_count": 3,
        "schema_correction_attempted": False,
        "last_error": f"LLM dispatch v2 job {ref} failed permanently",
    }
    monkeypatch.setattr(
        module, "MANIFEST", {recipe: {"ref": ref, "marker_sha256": module.marker_digest(marker)}}
    )
    key = deferred_failure_key(recipe)
    _write_json(storage, key, json.dumps({**marker, "extra": "changed"}).encode())
    assert module.repair(storage, recipe, apply=True) == "marker-changed"
    _write_json(storage, key, json.dumps(marker).encode())
    assert module.repair(storage, recipe) == "eligible"
    assert module.repair(storage, recipe, apply=True) == "repaired"
    updated = _read_json(storage, key)
    assert (updated["status"], updated["failure_count"]) == ("retryable", 1)
    assert updated["last_failed_ref"] == ref and updated["failed_refs"] == [ref]
    assert _read_json(storage, updated["repair_audit_key"]) == marker
    assert module.repair(storage, recipe, apply=True) == "marker-changed"
    assert module.repair(storage, "other", apply=True) == "not-audited"
