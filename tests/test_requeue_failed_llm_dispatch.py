"""Tests for the explicit failed LLM dispatch recovery action."""

from __future__ import annotations

import json

from botocore.exceptions import ClientError

from scripts.requeue_failed_llm_dispatch import ready_key, requeue_failed


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
