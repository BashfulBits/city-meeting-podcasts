"""Tests for the explicit failed LLM dispatch recovery action."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from botocore.exceptions import ClientError

from scripts.requeue_failed_llm_dispatch import ready_key, requeue_failed
from scripts.retire_legacy_prelabeler_dispatch import retire_legacy_prelabeler


def test_retirement_script_runs_directly_from_the_repository_root():
    result = subprocess.run(
        [sys.executable, "scripts/retire_legacy_prelabeler_dispatch.py", "--help"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Retire legacy pre-labeler dispatch records" in result.stdout


class _Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self) -> bytes:
        return self.value

    def close(self) -> None:
        pass


class _Paginator:
    def __init__(self, pages: list[dict]):
        self.pages = pages

    def paginate(self, **_kwargs):
        return iter(self.pages)


class _Client:
    def __init__(self, objects: dict[str, dict]):
        self.objects = objects
        self.puts: list[dict] = []
        self.deletes: list[dict] = []

    def get_paginator(self, _name: str):
        return _Paginator([{"Contents": [{"Key": key} for key in self.objects]}])

    def get_object(self, *, Bucket: str, Key: str):
        del Bucket
        item = self.objects[Key]
        return {"Body": _Body(item["body"]), "ETag": item.get("etag", '"etag-1"')}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        self.objects[kwargs["Key"]] = {
            "body": kwargs["Body"],
            "etag": '"etag-new"',
        }

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs)
        self.objects.pop(kwargs["Key"], None)


def _record(record_id: str, *, status: str = "failed", model: str = "google/gemma-4-31b-it"):
    return {
        "id": record_id,
        "status": status,
        "model": model,
        "created_at": "2026-08-13T00:00:00Z",
        "updated_at": "2026-08-13T01:00:00Z",
        "available_at": "2026-08-13T01:00:00Z",
        "attempts": 5,
        "error": {"code": "upstream_error", "status": 400},
        "completed_at": "2026-08-13T01:00:00Z",
        "request": {"model": model, "messages": [{"role": "user", "content": "x"}]},
        "policy": {},
    }


def _page(*keys: str) -> dict:
    return {"Contents": [{"Key": key} for key in keys]}


def test_dry_run_only_matches_failed_model_prefix_without_writing(capsys):
    records = {
        "requests/gemma.json": _record("gemma"),
        "requests/other.json": _record("other", model="deepseek/deepseek-v4-flash"),
        "requests/pending.json": _record("pending", status="pending"),
    }
    client = _Client({key: {"body": json.dumps(value).encode()} for key, value in records.items()})

    summary = requeue_failed(
        client,
        "dispatch",
        model_prefixes=("google/gemma-4-",),
        dry_run=True,
        workers=2,
        progress_every=1,
        progress_seconds=1,
    )

    assert summary["scanned"] == 3
    assert summary["matched"] == 1
    assert summary["requeued"] == 1
    assert not client.puts
    assert "mode=dry-run" in capsys.readouterr().out


def test_apply_resets_failure_and_writes_ready_marker():
    record = _record("gemma")
    key = "requests/gemma.json"
    client = _Client({key: {"body": json.dumps(record).encode()}})

    summary = requeue_failed(
        client,
        "dispatch",
        model_prefixes=("google/gemma-4-",),
        dry_run=False,
        workers=1,
        available_at="2026-08-13T02:00:00Z",
    )

    assert summary["requeued"] == 1
    expected_marker_record = {
        **record,
        "status": "pending",
        "available_at": "2026-08-13T02:00:00Z",
    }
    assert [put["Key"] for put in client.puts] == [key, ready_key(expected_marker_record)]
    updated = json.loads(client.puts[0]["Body"])
    assert updated["status"] == "pending"
    assert updated["attempts"] == 0
    assert updated["available_at"] == "2026-08-13T02:00:00Z"
    assert "error" not in updated
    assert "completed_at" not in updated
    assert client.puts[0]["IfMatch"] == '"etag-1"'
    marker_put = client.puts[1]
    assert marker_put["Metadata"]["ready_version"] == "1"
    assert marker_put["Metadata"]["status"] == "pending"


def test_apply_reports_cas_conflict_without_writing_marker():
    record = _record("gemma")
    key = "requests/gemma.json"

    class ConflictClient(_Client):
        def put_object(self, **kwargs):
            if kwargs["Key"] == key:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            super().put_object(**kwargs)

    client = ConflictClient({key: {"body": json.dumps(record).encode()}})
    summary = requeue_failed(
        client,
        "dispatch",
        model_prefixes=("google/gemma-4-",),
        dry_run=False,
        workers=1,
    )

    assert summary["conflicts"] == 1
    assert not client.puts


def test_retire_legacy_prelabeler_keeps_auditable_record_and_removes_ready_marker():
    record = _record("legacy", status="pending")
    record["request"]["response_format"] = {
        "type": "json_schema",
        "json_schema": {"schema": {"properties": {"assessments": {"type": "array"}}}},
    }
    key = "requests/legacy.json"
    client = _Client({key: {"body": json.dumps(record).encode()}})

    summary = retire_legacy_prelabeler(
        client,
        "dispatch",
        model_prefixes=("google/gemma-4-",),
        created_before="2026-08-15T00:00:00Z",
        dry_run=False,
        workers=1,
        now=datetime(2026, 8, 15, tzinfo=UTC),
    )

    assert summary == {
        "scanned": 1,
        "matched": 1,
        "retired": 1,
        "conflicts": 0,
        "ready_marker_failures": 0,
        "skipped": 0,
        "invalid": 0,
    }
    updated = json.loads(client.puts[0]["Body"])
    assert updated["status"] == "retired"
    assert updated["replacement_llm_schema_version"] == "2"
    assert updated["request"] == record["request"]
    assert client.puts[0]["IfMatch"] == '"etag-1"'
    assert client.deletes == [{"Bucket": "dispatch", "Key": ready_key(record)}]


def test_retire_legacy_prelabeler_never_selects_newer_or_non_assessment_requests():
    newer = _record("newer", status="pending")
    newer["created_at"] = "2026-08-15T00:00:00Z"
    newer["request"]["response_format"] = {
        "json_schema": {"schema": {"properties": {"assessments": {"type": "array"}}}}
    }
    tags = _record("tags", status="pending")
    tags["request"]["response_format"] = {
        "json_schema": {"schema": {"properties": {"tags": {"type": "array"}}}}
    }
    client = _Client(
        {
            "requests/newer.json": {"body": json.dumps(newer).encode()},
            "requests/tags.json": {"body": json.dumps(tags).encode()},
        }
    )

    summary = retire_legacy_prelabeler(
        client,
        "dispatch",
        model_prefixes=("google/gemma-4-",),
        created_before="2026-08-15T00:00:00Z",
        dry_run=True,
        workers=1,
    )

    assert summary["matched"] == 0
    assert summary["skipped"] == 2
    assert not client.puts
