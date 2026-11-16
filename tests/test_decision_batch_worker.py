"""Offline checks of the local GLiDE worker handoff."""

import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

from citypods.judging.decision_batch import canonical_json

WORKER = Path(__file__).resolve().parents[1] / "workers/decision-batch/run_batch.py"
spec = importlib.util.spec_from_file_location("decision_batch_worker", WORKER)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class RequestError(Exception):
    pass


def make_row(question, **extra):
    row = {
        "schema_version": 1,
        "episode_uid": "episode",
        "subject_id": "subject",
        "question_id": "question",
        "tier": "T1",
        "prompt_version": "v1",
        "evidence_digest": "digest",
        "state": "evidence",
        "question": question,
        **extra,
    }
    row["item_id"] = hashlib.sha256(canonical_json(row).encode()).hexdigest()
    return row


def write_input(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        for row in rows:
            stream.write(canonical_json(row) + "\n")


def test_worker_runs_one_model_load_and_preserves_typed_answers(tmp_path):
    rows = [
        make_row({"type": "noul", "instructions": "yes?"}),
        make_row({"type": "score", "instructions": "grade", "criteria": ["low", "high"]}),
        make_row(
            {"type": "choice", "instructions": "choose", "criteria": {"A": "one", "B": "two"}},
            subject_ids={"A": "first", "B": "second"},
        ),
    ]
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    write_input(source, rows)
    loads = []

    class Model:
        def decide_batch(self, requests):
            assert [next(iter(r["questions"])) for r in requests] == [r["item_id"] for r in rows]
            answers = [
                {"type": "noul", "noul": 0.7},
                {"type": "score", "score": 1},
                {"type": "choice", "choice": "B"},
            ]
            return [
                {"answers": {row["item_id"]: answer}, "usage": {"input_tokens": 12}}
                for row, answer in zip(rows, answers, strict=True)
            ]

    def load():
        loads.append(True)
        return Model(), RequestError

    worker.run_batch(source, output, load)
    with gzip.open(output, "rt", encoding="utf-8") as stream:
        result = [json.loads(line) for line in stream]
    assert len(loads) == 1
    assert [r["item_id"] for r in result] == [r["item_id"] for r in rows]
    assert [r["answer"]["type"] for r in result] == ["noul", "score", "choice"]
    assert all(r["input_tokens"] == 12 for r in result)


def test_too_large_is_bounded_result(tmp_path):
    row = make_row({"type": "noul", "instructions": "yes?"})
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    write_input(source, [row])

    class Model:
        def decide_batch(self, requests):
            return [RequestError("prompt exceeds the maximum context length")]

    worker.run_batch(source, output, lambda: (Model(), RequestError))
    with gzip.open(output, "rt", encoding="utf-8") as stream:
        assert json.loads(stream.read()) == {
            "schema_version": 1,
            "item_id": row["item_id"],
            "status": "too_large",
        }


@pytest.mark.parametrize("mode", ["duplicate", "tamper", "too_many", "expanded", "compressed"])
def test_bad_input_never_loads_model(tmp_path, monkeypatch, mode):
    row = make_row({"type": "noul", "instructions": "yes?"})
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    if mode == "tamper":
        row["state"] = "changed"
    write_input(source, [row, row] if mode in {"duplicate", "too_many"} else [row])
    if mode == "too_many":
        monkeypatch.setattr(worker, "MAX_ROWS", 1)
    if mode == "expanded":
        monkeypatch.setattr(worker, "MAX_EXPANDED_BYTES", 1)
    if mode == "compressed":
        monkeypatch.setattr(worker, "MAX_COMPRESSED_BYTES", 1)
    with pytest.raises(ValueError):
        worker.run_batch(source, output, lambda: pytest.fail("model loaded for invalid input"))
    assert not output.exists()


@pytest.mark.parametrize("reply", [[], [RequestError("unknown")], [{"answers": {}}]])
def test_failed_result_does_not_publish_or_replace_existing_output(tmp_path, reply):
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    write_input(source, [make_row({"type": "noul", "instructions": "yes?"})])
    output.write_bytes(b"previous")

    class Model:
        def decide_batch(self, requests):
            return reply

    with pytest.raises(ValueError):
        worker.run_batch(source, output, lambda: (Model(), RequestError))
    assert output.read_bytes() == b"previous"


def test_exported_frozen_batch_with_fake_runtime(tmp_path):
    from scripts.eval_decision_batch import main

    source = Path(os.environ.get("CITYPODS_TEST_DECISION_BATCH", tmp_path / "input.gz"))
    if "CITYPODS_TEST_DECISION_BATCH" not in os.environ:
        assert main(["--output", str(source)]) == 0
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream]
    kinds = {row["question"]["type"] for row in rows}
    assert kinds == {"noul", "score", "choice"}
    assert any("subject_ids" in row for row in rows)
    assert any("subject_ids" not in row for row in rows)

    class Model:
        def decide_batch(self, requests):
            replies = []
            for request in requests:
                item_id, question = next(iter(request["questions"].items()))
                kind = question["type"]
                value = (
                    0.5
                    if kind == "noul"
                    else 0
                    if kind == "score"
                    else next(iter(question["criteria"]))
                )
                replies.append({"answers": {item_id: {"type": kind, kind: value}}})
            return replies

    output = tmp_path / "output.gz"
    worker.run_batch(source, output, lambda: (Model(), RequestError))
    with gzip.open(output, "rt", encoding="utf-8") as stream:
        results = [json.loads(line) for line in stream]
    assert [row["item_id"] for row in results] == [row["item_id"] for row in rows]
    assert all(result["status"] == "ok" for result in results)


def test_failed_atomic_replace_preserves_previous_output(tmp_path, monkeypatch):
    row = make_row({"type": "noul", "instructions": "yes?"})
    source, output = tmp_path / "input.gz", tmp_path / "output.gz"
    write_input(source, [row])
    output.write_bytes(b"previous")

    class Model:
        def decide_batch(self, requests):
            return [{"answers": {row["item_id"]: {"type": "noul", "noul": 0.5}}}]

    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(worker.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        worker.run_batch(source, output, lambda: (Model(), RequestError))
    assert output.read_bytes() == b"previous"
    assert set(tmp_path.iterdir()) == {source, output}


def _cloud_module(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "run_batch", worker)
    path = WORKER.with_name("cloud_batch.py")
    cloud_spec = importlib.util.spec_from_file_location("cloud_batch", path)
    cloud = importlib.util.module_from_spec(cloud_spec)
    cloud_spec.loader.exec_module(cloud)
    return cloud


def _cloud_fixture(cloud):
    from citypods.judging.decision_batch_transport import (
        PreconditionFailed,
        put_chunk,
        seal_manifest,
    )

    class Store:
        def __init__(self):
            self.objects = {}
            self.next_generation = 1

        def get(self, key, generation=None):
            obj = self.objects.get(key)
            if obj is None or (generation is not None and obj[1] != generation):
                return None
            return obj

        def put(self, key, data, if_generation_match):
            old = self.objects.get(key)
            if (old[1] if old else 0) != if_generation_match:
                raise PreconditionFailed()
            generation = self.next_generation
            self.next_generation += 1
            self.objects[key] = (data, generation)
            return generation

    store = Store()
    row = make_row({"type": "noul", "instructions": "yes?"})
    chunk = put_chunk(store, [row], max_rows=1000, max_bytes=100000, max_estimated_tokens=100000)
    image = "sha256:" + "1" * 64
    manifest_id = seal_manifest(
        store,
        [chunk],
        model_revision=cloud.MODEL_REVISION,
        image_digest=image,
        settings={"token_budget": 16384, "max_len": 4096, "launch_mode": "regular"},
        max_rows=1000,
        max_bytes=100000,
        max_estimated_tokens=100000,
    )
    store.put(
        "dispatch/admission.json",
        canonical_json(
            {"outstanding": {"manifest_id": manifest_id, "attempt_id": "attempt"}}
        ).encode(),
        0,
    )

    class Model:
        def decide_batch(self, requests):
            return [
                {
                    "answers": {row["item_id"]: {"type": "noul", "noul": 0.5}},
                    "usage": {"input_tokens": 10},
                }
            ]

    return store, manifest_id, image, row, lambda settings: (Model(), RequestError)


def test_cloud_publishes_generation_pinned_result_and_replays_without_model(tmp_path, monkeypatch):
    cloud = _cloud_module(monkeypatch)
    store, manifest_id, image, row, load = _cloud_fixture(cloud)
    result = cloud.run_cloud(store, manifest_id, "attempt", "execution", image, load)
    assert result["count"] == 1
    assert result["telemetry"]["input_tokens"] == [10]
    data, generation = store.get(result["key"], result["generation"])
    assert generation == result["generation"]
    assert json.loads(gzip.decompress(data))["item_id"] == row["item_id"]
    assert (
        cloud.run_cloud(
            store,
            manifest_id,
            "attempt",
            "execution",
            image,
            lambda settings: pytest.fail("replay loaded model"),
        )
        == result
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        cloud.run_cloud(store, manifest_id, "attempt", "other-execution", image, load)


def test_cloud_rejects_bad_generation_and_duplicate_claim_before_model(monkeypatch):
    cloud = _cloud_module(monkeypatch)
    store, manifest_id, image, _, load = _cloud_fixture(cloud)
    key = next(key for key in store.objects if key.startswith("pending/"))
    data, generation = store.objects[key]
    store.objects[key] = (data, generation + 100)
    with pytest.raises(ValueError, match="generation or digest mismatch"):
        cloud.run_cloud(store, manifest_id, "attempt", "execution", image, load)
    store.objects[key] = (data, generation)
    store.put(f"attempts/{manifest_id}/attempt/claim.json", b"another worker", 0)
    with pytest.raises(ValueError, match="already claimed"):
        cloud.run_cloud(store, manifest_id, "attempt", "execution", image, load)
    assert not any(key.endswith("completion.json") for key in store.objects)


def test_cloud_failed_inference_and_stale_attempt_never_publish(monkeypatch):
    cloud = _cloud_module(monkeypatch)
    store, manifest_id, image, _, _ = _cloud_fixture(cloud)

    class Model:
        def decide_batch(self, requests):
            state = store.objects["dispatch/admission.json"]
            store.put("dispatch/admission.json", b'{"outstanding":null}', state[1])
            return []

    with pytest.raises(ValueError):
        cloud.run_cloud(
            store,
            manifest_id,
            "attempt",
            "execution",
            image,
            lambda settings: (Model(), RequestError),
        )
    assert not any(key.endswith("result.jsonl.gz") for key in store.objects)


def test_cloud_stale_after_inference_does_not_publish(monkeypatch):
    cloud = _cloud_module(monkeypatch)
    store, manifest_id, image, row, _ = _cloud_fixture(cloud)

    class Model:
        def decide_batch(self, requests):
            state = store.objects["dispatch/admission.json"]
            store.put("dispatch/admission.json", b'{"outstanding":null}', state[1])
            return [{"answers": {row["item_id"]: {"type": "noul", "noul": 0.5}}}]

    with pytest.raises(ValueError, match="outstanding reservation"):
        cloud.run_cloud(
            store,
            manifest_id,
            "attempt",
            "execution",
            image,
            lambda settings: (Model(), RequestError),
        )
    assert not any(key.endswith("result.jsonl.gz") for key in store.objects)


def test_cloud_worker_hands_off_to_collector(monkeypatch):
    from citypods.judging.decision_batch_transport import collect

    cloud = _cloud_module(monkeypatch)
    store, manifest_id, image, _, load = _cloud_fixture(cloud)
    cloud.run_cloud(store, manifest_id, "attempt", "execution", image, load)
    receipt = collect(store, manifest_id, "attempt")
    assert store.get(f"receipts/{receipt}.json") is not None
    assert collect(store, manifest_id, "attempt") == receipt


def test_glide_rejects_bad_model_manifest_before_cloud_claim(tmp_path, monkeypatch):
    cloud = _cloud_module(monkeypatch)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}")
    for key in (
        "DECISION_BATCH_MANIFEST_ID",
        "DECISION_BATCH_ATTEMPT_ID",
        "CLOUD_RUN_EXECUTION",
        "CITYPODS_IMAGE_DIGEST",
    ):
        monkeypatch.setenv(key, "unused")
    monkeypatch.setenv("CITYPODS_MODEL_DIR", str(tmp_path / "model"))
    monkeypatch.setenv("CITYPODS_MODEL_MANIFEST", str(manifest_path))
    monkeypatch.setattr(cloud, "run_cloud", lambda *args: pytest.fail("claimed batch"))
    with pytest.raises(ValueError, match="unexpected bucket"):
        cloud.main()
