from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from citypods.compute.base import JobResult
from citypods.compute.llm_deferred import look_up_deferred, write_deferred


def _script_module():
    path = Path(__file__).parents[1] / "scripts" / "recover_v1_llm_dispatch_results.py"
    spec = importlib.util.spec_from_file_location("scripts.recover_v1_llm_dispatch_results", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recovery = _script_module()


class _Storage:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)

    def list_objects(self, prefix: str = ""):
        return [(key, None) for key in sorted(self.objects) if key.startswith(prefix)]

    def get_bytes(self, key: str):
        value = self.objects.get(key)
        return (value, '"etag"') if value is not None else None

    def get_file(self, key: str, path: Path, **_kwargs):
        value = self.objects.get(key)
        if value is None:
            return False
        path.write_bytes(value)
        return True

    def put_file(self, key: str, path: Path, _content_type: str):
        self.objects[key] = path.read_bytes()
        return f"https://b2.example/{key}"


class _Body:
    def __init__(self, value: bytes):
        self.value = value
        self.closed = False

    def read(self):
        return self.value

    def close(self):
        self.closed = True


class _Paginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, **kwargs):
        limit = kwargs.get("PaginationConfig", {}).get("MaxItems", 1000)
        prefix = kwargs["Prefix"]
        keys = [key for key in sorted(self.client.objects) if key.startswith(prefix)][:limit]
        yield {"Contents": [{"Key": key} for key in keys]}


class _R2:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = dict(objects)

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return _Paginator(self)

    def get_object(self, *, Bucket: str, Key: str):  # noqa: N803 - boto3 API spelling
        assert Bucket == "dispatch"
        return {"Body": _Body(self.objects[Key])}


def _state(*, entries: list[dict[str, Any]]) -> bytes:
    episodes = {str(index): entry for index, entry in enumerate(entries)}
    return json.dumps({"episodes": episodes}).encode()


def _agenda(ref: str, recipe: str = "agenda-recipe") -> dict[str, Any]:
    return {
        "generated_agenda_candidates": {
            "status": "pending",
            "recipe": recipe,
            "job_ref": ref,
        }
    }


def _completed(ref: str) -> bytes:
    return json.dumps(
        {
            "id": ref,
            "status": "completed",
            "model": "mistral/mistral-medium-2508",
            "response": {"choices": [{"message": {"content": "{}"}}]},
        }
    ).encode()


def _completed_with_content(ref: str, content: str) -> bytes:
    return json.dumps(
        {
            "id": ref,
            "status": "completed",
            "model": "mistral/mistral-medium-2508",
            "response": {"choices": [{"message": {"content": content}}]},
        }
    ).encode()


def test_dry_run_reports_only_validated_owned_completion():
    ref = "chatcmpl-a"
    storage = _Storage({"state/sources/source/episodes.json": _state(entries=[_agenda(ref)])})
    r2 = _R2({f"requests/{ref}.json": _completed(ref)})

    summary = recovery.recover_v1_results(
        storage,
        r2,
        "dispatch",
        dry_run=True,
        workers=1,
        validate=lambda candidate, response: (
            candidate.recipe_hash == "agenda-recipe" and bool(response)
        ),
    )

    assert summary["owned_completed"] == 1
    assert summary["importable_agenda"] == 1
    assert summary["importable_locator"] == 0
    assert summary["would_import"] == 1
    assert summary["imported"] == 0
    assert look_up_deferred(storage, "agenda-recipe") is None
    assert summary["r2_records_retained"] == 1


def test_apply_persists_one_owned_result_without_deleting_r2_record():
    ref = "chatcmpl-b"
    storage = _Storage({"state/sources/source/episodes.json": _state(entries=[_agenda(ref)])})
    request_key = f"requests/{ref}.json"
    raw = _completed(ref)
    r2 = _R2({request_key: raw})

    summary = recovery.recover_v1_results(
        storage,
        r2,
        "dispatch",
        dry_run=False,
        workers=1,
        validate=lambda _candidate, _response: True,
    )

    result = look_up_deferred(storage, "agenda-recipe")
    assert summary["imported"] == 1
    assert isinstance(result, JobResult)
    assert result.task == "agenda-item-extract"
    assert r2.objects[request_key] == raw


def test_ambiguous_owner_and_invalid_completion_are_never_imported():
    ref = "chatcmpl-c"
    storage = _Storage(
        {
            "state/sources/one/episodes.json": _state(entries=[_agenda(ref, "one")]),
            "state/sources/two/episodes.json": _state(entries=[_agenda(ref, "two")]),
        }
    )
    r2 = _R2({f"requests/{ref}.json": _completed(ref)})

    summary = recovery.recover_v1_results(
        storage,
        r2,
        "dispatch",
        dry_run=False,
        workers=1,
        validate=lambda _candidate, _response: False,
    )

    assert summary["ambiguous_owners"] == 1
    assert summary["unowned_completed"] == 1
    assert summary["imported"] == 0


def test_apply_does_not_replace_a_different_completed_b2_result():
    ref = "chatcmpl-d"
    storage = _Storage({"state/sources/source/episodes.json": _state(entries=[_agenda(ref)])})
    existing = JobResult(
        task="agenda-item-extract",
        recipe_hash="agenda-recipe",
        output={"choices": [{"message": {"content": "existing"}}]},
        model="existing-model",
    )
    write_deferred(storage, "agenda-recipe", existing)
    r2 = _R2({f"requests/{ref}.json": _completed(ref)})

    summary = recovery.recover_v1_results(
        storage,
        r2,
        "dispatch",
        dry_run=False,
        workers=1,
        validate=lambda _candidate, _response: True,
    )

    assert summary["completed_conflict"] == 1
    assert look_up_deferred(storage, "agenda-recipe") == existing


def test_apply_serializes_distinct_v1_refs_for_one_recipe():
    first_ref = "chatcmpl-a"
    second_ref = "chatcmpl-b"
    storage = _Storage(
        {
            "state/sources/source/episodes.json": _state(
                entries=[_agenda(first_ref), _agenda(second_ref)]
            )
        }
    )
    r2 = _R2(
        {
            f"requests/{first_ref}.json": _completed_with_content(first_ref, "first"),
            f"requests/{second_ref}.json": _completed_with_content(second_ref, "second"),
        }
    )

    summary = recovery.recover_v1_results(
        storage,
        r2,
        "dispatch",
        dry_run=False,
        workers=2,
        validate=lambda _candidate, _response: True,
    )

    result = look_up_deferred(storage, "agenda-recipe")
    assert summary["imported"] == 1
    assert summary["completed_conflict"] == 1
    assert isinstance(result, JobResult)
    assert result.output["choices"][0]["message"]["content"] == "first"


def test_dry_run_reconstructs_an_unowned_agenda_request_from_durable_input():
    from citypods.chapter_jobs import build_agenda_job

    ref = "chatcmpl-reconstructed"
    agenda_bytes = b"1. Consider a zoning request."
    job = build_agenda_job(
        episode_uid="episode-1",
        agenda_text=agenda_bytes.decode(),
        agenda_source_hash=hashlib.sha256(agenda_bytes).hexdigest(),
    )
    storage = _Storage(
        {
            "state/sources/source/episodes.json": json.dumps(
                {
                    "episodes": {
                        "episode-1": {
                            "uid": "episode-1",
                            "links": {"agenda_text_artifact_key": "agendas/episode-1.txt"},
                        }
                    }
                }
            ).encode(),
            "agendas/episode-1.txt": agenda_bytes,
        }
    )
    r2 = _R2(
        {
            f"requests/{ref}.json": json.dumps(
                {
                    "id": ref,
                    "status": "completed",
                    "model": "mistral/mistral-medium-2508",
                    "request": {
                        "messages": job.inputs["messages"],
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {"schema": {"properties": {"items": {}}}},
                        },
                    },
                    "response": {"choices": [{"message": {"content": "{}"}}]},
                }
            ).encode()
        }
    )

    summary = recovery.recover_v1_results(
        storage,
        r2,
        "dispatch",
        dry_run=True,
        workers=1,
        validate=lambda _candidate, _response: True,
    )

    assert summary["owned_requests"] == 0
    assert summary["reconstructed_candidates"] == 1
    assert summary["reconstructed_owned_requests"] == 1
    assert summary["reconstructed_matched_completed"] == 1
    assert summary["would_import"] == 1


def test_reconstruction_with_two_matching_owners_is_never_imported():
    from citypods.chapter_jobs import build_agenda_job

    ref = "chatcmpl-ambiguous-reconstruction"
    agenda_bytes = b"1. Consider a zoning request."
    job = build_agenda_job(
        episode_uid="episode-1",
        agenda_text=agenda_bytes.decode(),
        agenda_source_hash=hashlib.sha256(agenda_bytes).hexdigest(),
    )
    episode = {"links": {"agenda_text_artifact_key": "agendas/shared.txt"}}
    storage = _Storage(
        {
            "state/sources/one/episodes.json": _state(entries=[episode]),
            "state/sources/two/episodes.json": _state(entries=[episode]),
            "agendas/shared.txt": agenda_bytes,
        }
    )
    r2 = _R2(
        {
            f"requests/{ref}.json": json.dumps(
                {
                    "id": ref,
                    "status": "completed",
                    "request": {
                        "messages": job.inputs["messages"],
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {"schema": {"properties": {"items": {}}}},
                        },
                    },
                    "response": {"choices": [{"message": {"content": "{}"}}]},
                }
            ).encode()
        }
    )

    summary = recovery.recover_v1_results(
        storage,
        r2,
        "dispatch",
        dry_run=False,
        workers=1,
        validate=lambda _candidate, _response: True,
    )

    assert summary["reconstructed_candidates"] == 2
    assert summary["reconstructed_ambiguous_owners"] == 1
    assert summary["would_import"] == 0
    assert summary["imported"] == 0
