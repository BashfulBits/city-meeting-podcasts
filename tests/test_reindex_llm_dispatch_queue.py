from __future__ import annotations

import json

from botocore.exceptions import ClientError

from scripts.reindex_llm_dispatch_queue import _r2_with_retry, migrate, ready_key


class _Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self) -> bytes:
        return self.value


class _Paginator:
    def __init__(self, pages: list[dict]):
        self.pages = pages

    def paginate(self, **_kwargs):
        return iter(self.pages)


class _Client:
    def __init__(self, objects: dict[str, bytes], pages: list[dict]):
        self.objects = objects
        self.paginator = _Paginator(pages)
        self.puts: list[dict] = []

    def get_paginator(self, _name: str):
        return self.paginator

    def get_object(self, *, Bucket: str, Key: str):
        del Bucket
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


def _page(*keys: str) -> dict:
    return {"Contents": [{"Key": key} for key in keys]}


def _pending(record_id: str) -> dict:
    return {
        "id": record_id,
        "status": "pending",
        "model": "test-model",
        "created_at": "2026-08-13T00:00:00Z",
        "available_at": "2026-08-13T00:00:01Z",
    }


def test_migrate_reads_objects_concurrently_and_reports_progress(capsys):
    objects = {
        "requests/1.json": json.dumps(_pending("1")).encode(),
        "requests/2.json": json.dumps({"id": "2", "status": "done"}).encode(),
        "requests/3.json": b"not-json",
        "requests/4.json": json.dumps({"status": "pending"}).encode(),
    }
    client = _Client(objects, [_page(*objects)])

    scanned, pending, written = migrate(
        client,
        "dispatch",
        dry_run=True,
        workers=2,
        progress_every=1,
        progress_seconds=1,
    )

    assert (scanned, pending, written) == (4, 1, 1)
    assert not client.puts
    output = capsys.readouterr().out
    assert "starting reindex: bucket=dispatch mode=dry-run workers=2" in output
    assert "listed page: listed=4 queued=4" in output
    assert "progress:" in output
    assert "scanned=4 pending=1 written=1" in output


def test_migrate_writes_ready_markers_in_apply_mode():
    record = _pending("1")
    key = "requests/1.json"
    client = _Client({key: json.dumps(record).encode()}, [_page(key)])

    assert migrate(client, "dispatch", dry_run=False, workers=1) == (1, 1, 1)
    assert len(client.puts) == 1
    assert client.puts[0]["Key"] == ready_key(record)
    assert json.loads(client.puts[0]["Body"])["id"] == "1"


def test_transient_r2_errors_are_retried_with_backoff(monkeypatch, capsys):
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ClientError(
                {
                    "Error": {"Code": "ServiceUnavailable", "Message": "busy"},
                    "ResponseMetadata": {"HTTPStatusCode": 503},
                },
                "GetObject",
            )
        return "ok"

    monkeypatch.setattr("scripts.reindex_llm_dispatch_queue.time.sleep", lambda _delay: None)
    monkeypatch.setattr("scripts.reindex_llm_dispatch_queue.random.uniform", lambda *_args: 0.0)

    assert _r2_with_retry(operation, key="requests/1.json", retries=2) == "ok"
    assert attempts == 3
    assert "retrying object: key=requests/1.json attempt=1/2" in capsys.readouterr().out
