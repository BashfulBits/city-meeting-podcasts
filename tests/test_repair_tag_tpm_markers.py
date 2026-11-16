import json

from citypods.compute.llm_deferred import _read_json, _write_json, deferred_failure_key
from scripts.repair_tag_tpm_markers import MANIFEST, repair
from tests.test_compute_llm_deferred import MemStorage


def test_only_matching_tpm_marker_is_archived_and_fenced():
    storage = MemStorage()
    recipe, ref = next(iter(MANIFEST.items()))
    marker = {
        "task": "tag",
        "status": "exhausted",
        "failure_count": 3,
        "failure_class": "own_tpm",
        "last_failed_ref": ref,
        "schema_correction_attempted": False,
        "last_error": f"LLM dispatch v2 job {ref} failed permanently",
    }
    key = deferred_failure_key(recipe)
    _write_json(storage, key, json.dumps(marker).encode())
    assert repair(storage, recipe, ref) == "eligible"
    assert _read_json(storage, key) == marker
    assert repair(storage, recipe, ref, apply=True) == "repaired"
    updated = _read_json(storage, key)
    assert updated["status"] == "quota_blocked"
    assert updated["failure_count"] == 3
    assert _read_json(storage, updated["repair_audit_key"]) == marker
    assert repair(storage, recipe, ref, apply=True) == "marker-changed"
    assert repair(storage, recipe, "other", apply=True) == "not-audited"
