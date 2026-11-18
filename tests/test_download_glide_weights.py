"""Offline tests for the pinned, ranged model download."""

import hashlib
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import download_glide_weights as downloader

BUCKET = "example-model-bucket"


@pytest.fixture(autouse=True)
def _model_bucket(monkeypatch):
    monkeypatch.setenv("CITYPODS_MODEL_BUCKET", BUCKET)


def manifest(data):
    return {
        "bucket": BUCKET,
        "location": downloader.LOCATION,
        "revision": downloader.REVISION,
        "objects": [
            {
                "name": downloader.REVISION + "/weights/model.safetensors",
                "generation": "123",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
    }


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


def test_download_slices_and_checks_generation(tmp_path, monkeypatch):
    data = b"abc" * 500_000
    calls = []

    def get(url, token, headers=None):
        calls.append((url, headers))
        if "/o/" not in url:
            return Response(json.dumps({"location": "US-EAST4"}).encode())
        assert parse_qs(urlsplit(url).query)["generation"] == ["123"]
        if headers is None:
            return Response(b'{"generation":"123","size":"1500000"}')
        start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
        return Response(
            data[start : end + 1],
            206,
            {"Content-Range": f"bytes {start}-{end}/{len(data)}"},
        )

    monkeypatch.setattr(downloader, "api_get", get)
    dest = tmp_path / "model"
    stats = downloader.download(manifest(data), dest, "token", 2, 1024**2)
    assert (dest / "weights/model.safetensors").read_bytes() == data
    assert stats["bytes"] == len(data)
    assert sum(headers is not None for _, headers in calls) == 2


@pytest.mark.parametrize("suffix", ["../escape", "a/../b", "/absolute", "a\\b"])
def test_rejects_unsafe_paths(suffix):
    model = manifest(b"x")
    model["objects"][0]["name"] = downloader.REVISION + "/" + suffix
    with pytest.raises(ValueError):
        downloader.validate_manifest(model)


def test_bad_hash_cleans_partial_output(tmp_path, monkeypatch):
    model = manifest(b"x")
    model["objects"][0]["sha256"] = "0" * 64

    def get(url, token, headers=None):
        if "/o/" not in url:
            return Response(b'{"location":"US-EAST4"}')
        if headers is None:
            return Response(b'{"generation":"123","size":"1"}')
        return Response(b"x", 206, {"Content-Range": "bytes 0-0/1"})

    monkeypatch.setattr(downloader, "api_get", get)
    with pytest.raises(ValueError, match="SHA-256"):
        downloader.download(model, tmp_path / "model", "token")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("revision", sorted(set(downloader.MODEL_PREFIXES) - {downloader.REVISION}))
def test_karpo_revision_prefixes(revision):
    model = manifest(b"x")
    model["revision"] = revision
    model["objects"][0]["name"] = downloader.MODEL_PREFIXES[revision] + "/model.safetensors"
    assert downloader.validate_manifest(model) == model["objects"]
    model["objects"][0]["name"] = downloader.REVISION + "/model.safetensors"
    with pytest.raises(ValueError, match="outside pinned revision"):
        downloader.validate_manifest(model)


@pytest.mark.parametrize("failure", ["429", "503", "timeout", "connection", "url-timeout"])
def test_range_transient_failure_retries(tmp_path, monkeypatch, failure):
    import urllib.error

    calls = 0

    def get(url, token, headers=None):
        nonlocal calls
        if "/o/" not in url:
            return Response(b'{"location":"US-EAST4"}')
        if headers is None:
            return Response(b'{"generation":"123","size":"1"}')
        calls += 1
        if calls == 1:
            if failure.isdigit():
                raise urllib.error.HTTPError(url, int(failure), "transient", {}, None)
            if failure == "timeout":
                raise TimeoutError()
            if failure == "connection":
                raise ConnectionError()
            raise urllib.error.URLError(TimeoutError())
        return Response(b"x", 206, {"Content-Range": "bytes 0-0/1"})

    monkeypatch.setattr(downloader, "api_get", get)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    downloader.download(manifest(b"x"), tmp_path / "model", "token")
    assert calls == 2


def test_range_retry_is_bounded_and_integrity_errors_are_not_retried(tmp_path, monkeypatch):
    calls = 0
    integrity = False

    def get(url, token, headers=None):
        nonlocal calls
        if "/o/" not in url:
            return Response(b'{"location":"US-EAST4"}')
        if headers is None:
            return Response(b'{"generation":"123","size":"1"}')
        calls += 1
        if integrity:
            return Response(b"x", 200)
        raise TimeoutError()

    monkeypatch.setattr(downloader, "api_get", get)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="retries exhausted"):
        downloader.download(manifest(b"x"), tmp_path / "model", "token")
    assert calls == 3
    calls = 0
    integrity = True
    with pytest.raises(ValueError, match="invalid range"):
        downloader.download(manifest(b"x"), tmp_path / "model", "token")
    assert calls == 1
    assert not list(tmp_path.iterdir())


def test_model_bucket_is_required(monkeypatch):
    monkeypatch.delenv("CITYPODS_MODEL_BUCKET")
    with pytest.raises(SystemExit, match="CITYPODS_MODEL_BUCKET"):
        downloader.model_bucket()
