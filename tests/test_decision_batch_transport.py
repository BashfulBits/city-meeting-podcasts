import gzip
import hashlib
import json

import pytest

from citypods.judging.backends import Item
from citypods.judging.decision_batch import build_items, canonical_json
from citypods.judging.decision_batch_transport import (
    PreconditionFailed,
    collect,
    put_chunk,
    reserve_and_launch,
    seal_manifest,
    settle_failed,
)
from citypods.judging.tag_task import SUPPORTED
from citypods.judging.tasks import Subject, make_evidence


class Store:
    def __init__(self):
        self.objects = {}
        self.next_generation = 1

    def get(self, key, generation=None):
        obj = self.objects.get(key)
        return obj if obj and (generation is None or generation == obj[1]) else None

    def put(self, key, data, if_generation_match):
        old = self.objects.get(key)
        if (old[1] if old else 0) != if_generation_match:
            raise PreconditionFailed()
        generation = self.next_generation
        self.next_generation += 1
        self.objects[key] = (data, generation)
        return generation


class Launch:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def launch(self, manifest_id, attempt_id, mode):
        self.calls.append((manifest_id, attempt_id, mode))
        if self.fail:
            raise TimeoutError("lost acknowledgement")
        return "operation"


def setup():
    store = Store()
    subject = Subject("tag", "subject", "episode", None, None, {})
    row = build_items([Item(SUPPORTED, "T1", make_evidence("T1", "evidence"), (subject,))])[0]
    limits = {"max_rows": 10, "max_bytes": 100000, "max_estimated_tokens": 10000}
    chunk = put_chunk(store, [row], **limits)
    manifest = seal_manifest(
        store,
        [chunk],
        model_revision="rev",
        image_digest="digest",
        settings={"max_len": 4096, "launch_mode": "regular"},
        **limits,
    )
    return store, row, chunk, manifest, limits


def launch(store, manifest, launcher, attempt="attempt", amount=10):
    return reserve_and_launch(
        store,
        launcher,
        manifest,
        attempt,
        mode="regular",
        reservations={"2026-01": amount},
        allowances={"2026-01": 20},
    )


def completion(store, manifest, row, *, attempt="attempt", result=None):
    prefix = f"attempts/{manifest}/{attempt}"
    claim = {
        "schema_version": 1,
        "manifest_id": manifest,
        "attempt_id": attempt,
        "execution_id": "execution",
    }
    store.put(f"{prefix}/claim.json", canonical_json(claim).encode(), 0)
    if result is None:
        result = {
            "schema_version": 1,
            "item_id": row["item_id"],
            "status": "ok",
            "answer": {"type": "noul", "noul": 0.5},
        }
    data = gzip.compress((canonical_json(result) + "\n").encode(), mtime=0)
    key = f"{prefix}/result.jsonl.gz"
    generation = store.put(key, data, 0)
    complete = {
        **claim,
        "key": key,
        "generation": generation,
        "sha256": hashlib.sha256(data).hexdigest(),
        "count": 1,
        "telemetry": {},
    }
    store.put(f"{prefix}/completion.json", canonical_json(complete).encode(), 0)


def test_chunks_manifest_repeat_and_limits():
    store, row, chunk, manifest, limits = setup()
    assert put_chunk(store, [row], **limits) == chunk
    assert (
        seal_manifest(
            store,
            [chunk],
            model_revision="rev",
            image_digest="digest",
            settings={"max_len": 4096, "launch_mode": "regular"},
            **limits,
        )
        == manifest
    )
    for rows, options in (([], limits), ([row, row], limits), ([row], {**limits, "max_bytes": 2})):
        with pytest.raises(ValueError):
            put_chunk(store, rows, **options)
    with pytest.raises(ValueError):
        seal_manifest(
            store,
            [{**chunk, "generation": 999}],
            model_revision="rev",
            image_digest="digest",
            settings={},
            **limits,
        )


def test_launch_checks_manifest_image_before_reserving():
    store, _, _, manifest, _ = setup()
    checked = []

    def reject_image(digest):
        checked.append(digest)
        raise ValueError("job image mismatch")

    with pytest.raises(ValueError, match="job image mismatch"):
        reserve_and_launch(
            store,
            None,
            manifest,
            "attempt",
            mode="regular",
            reservations={"2026-01": 10},
            allowances={"2026-01": 20},
            verify_image=reject_image,
        )
    assert checked == ["digest"]
    assert store.get("dispatch/admission.json") is None


def test_ambiguous_launch_fenced_and_allowance():
    store, _, _, manifest, _ = setup()
    launcher = Launch(fail=True)
    with pytest.raises(TimeoutError):
        launch(store, manifest, launcher)
    with pytest.raises(ValueError, match="outstanding"):
        launch(store, manifest, launcher, attempt="another")
    assert len(launcher.calls) == 1
    state = json.loads(store.get("dispatch/admission.json")[0])
    assert state["spent"] == {"2026-01": 10}
    assert state["outstanding"]["mode"] == "regular"


def test_collect_replay_and_budget():
    store, row, _, manifest, _ = setup()
    launch(store, manifest, Launch())
    completion(store, manifest, row)
    receipt = collect(store, manifest, "attempt")
    assert collect(store, manifest, "attempt") == receipt
    with pytest.raises(ValueError, match="selected"):
        launch(store, manifest, Launch(), attempt="next", amount=11)


def test_partial_and_wrong_output_never_release_admission():
    store, row, _, manifest, _ = setup()
    launch(store, manifest, Launch())
    with pytest.raises(ValueError, match="completion missing"):
        collect(store, manifest, "attempt")
    completion(
        store,
        manifest,
        row,
        result={"schema_version": 1, "item_id": "wrong", "status": "too_large"},
    )
    with pytest.raises(ValueError):
        collect(store, manifest, "attempt")
    with pytest.raises(ValueError, match="outstanding"):
        launch(store, manifest, Launch(), attempt="next")


def test_failed_settlement_keeps_spend_and_blocks_reuse():
    store, _, _, manifest, _ = setup()
    launch(store, manifest, Launch())
    with pytest.raises(ValueError):
        settle_failed(store, manifest, "wrong")
    settle_failed(store, manifest, "attempt")
    with pytest.raises(ValueError, match="already used"):
        launch(store, manifest, Launch())
    with pytest.raises(ValueError, match="allowance"):
        launch(store, manifest, Launch(), attempt="next", amount=11)
    launch(store, manifest, Launch(), attempt="next", amount=10)


def test_concurrent_dispatch_cas_loser_does_not_launch():
    store, _, _, manifest, _ = setup()
    original_put = store.put

    def race(key, data, if_generation_match):
        if key == "dispatch/admission.json" and if_generation_match == 0:
            original_put(key, b'{"schema_version":1,"spent":{},"outstanding":{}}', 0)
        return original_put(key, data, if_generation_match)

    store.put = race
    launcher = Launch()
    with pytest.raises(PreconditionFailed):
        launch(store, manifest, launcher)
    assert launcher.calls == []


def test_wrong_output_generation_keeps_fence():
    store, row, _, manifest, _ = setup()
    launch(store, manifest, Launch())
    completion(store, manifest, row)
    key = f"attempts/{manifest}/attempt/completion.json"
    old, generation = store.get(key)
    changed = json.loads(old)
    changed["generation"] += 1
    store.put(key, canonical_json(changed).encode(), generation)
    with pytest.raises(ValueError, match="generation or digest"):
        collect(store, manifest, "attempt")
    with pytest.raises(ValueError, match="outstanding"):
        launch(store, manifest, Launch(), attempt="next")
