"""Offline tests for the pinned, ranged model download."""

import hashlib
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import download_glide_weights as downloader


def manifest(data):
    return {
        "bucket": downloader.BUCKET,
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
