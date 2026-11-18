import json

from citypods.compute.llm_deferred import _read_json, _write_json, deferred_failure_key
from scripts.repair_tag_retry_markers import AUDITED, repair
from tests._cas_fake import MemStorage


def test_repair_is_audited_bounded_and_idempotent():
    storage = MemStorage()
    recipe, ref = next(iter(AUDITED.items()))
    key = deferred_failure_key(recipe)
    original = {
        "recipe_hash": recipe,
        "task": "tag",
        "status": "exhausted",
        "failure_count": 3,
        "schema_correction_attempted": False,
        "last_error": f"LLM dispatch v2 job {ref} failed permanently",
    }
    _write_json(storage, key, json.dumps(original).encode())
    assert repair(storage, recipe, ref) == "eligible"
    assert _read_json(storage, key) == original
    assert repair(storage, recipe, ref, apply=True) == "repaired"
    marker = _read_json(storage, key)
    assert marker["failure_count"] == 1
    assert marker["status"] == "retryable"
    assert marker["last_failed_ref"] == ref
    assert _read_json(storage, marker["repair_audit_key"]) == original
    assert repair(storage, recipe, ref, apply=True) == "marker-changed"
    assert repair(storage, recipe, "different", apply=True) == "not-audited"
