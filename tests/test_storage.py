"""Tests for storage backends and selection."""

from __future__ import annotations

import pytest

from citypods.storage import make_storage
from citypods.storage.local import LocalStorage


def test_local_put_exists_url(tmp_path):
    src = tmp_path / "src.m4a"
    src.write_bytes(b"audio-bytes")
    store = LocalStorage(root=tmp_path / "out", url_prefix="https://x/audio")

    key = "denton-tx/abc.m4a"
    assert not store.exists(key)
    url = store.put_file(key, src, "audio/mp4")
    assert url == "https://x/audio/denton-tx/abc.m4a"
    assert store.exists(key)
    assert (tmp_path / "out" / key).read_bytes() == b"audio-bytes"


def test_make_storage_local(tmp_path):
    cfg = {"defaults": {"audio_storage_backend": "local"}}
    store = make_storage(cfg, "https://site", tmp_path)
    assert isinstance(store, LocalStorage)
    assert store.public_url("k") == "https://site/audio/k"


@pytest.mark.parametrize(
    "backend,env_vars",
    [
        (
            "r2",
            (
                "CLOUDFLARE_ACCOUNT_ID",
                "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY",
                "R2_BUCKET",
                "R2_PUBLIC_BASE_URL",
            ),
        ),
        ("b2", ("B2_ENDPOINT", "B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "B2_PUBLIC_BASE_URL")),
    ],
)
def test_make_storage_s3_without_env_returns_none(tmp_path, monkeypatch, backend, env_vars):
    monkeypatch.delenv("AUDIO_STORAGE_BACKEND", raising=False)
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)
    cfg = {"defaults": {"audio_storage_backend": backend}}
    assert make_storage(cfg, "https://site", tmp_path) is None


def test_env_override_beats_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_STORAGE_BACKEND", "local")
    cfg = {"defaults": {"audio_storage_backend": "r2"}}
    from citypods.storage.local import LocalStorage as _Local

    assert isinstance(make_storage(cfg, "https://site", tmp_path), _Local)


def test_b2_region_parsed_from_endpoint():
    from citypods.storage.s3 import _region_from_b2_endpoint

    assert _region_from_b2_endpoint("https://s3.us-west-004.backblazeb2.com") == "us-west-004"
    assert _region_from_b2_endpoint("https://example.com") == "auto"


def test_make_storage_unknown_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIO_STORAGE_BACKEND", raising=False)
    with pytest.raises(ValueError, match="unknown audio_storage_backend"):
        make_storage({"defaults": {"audio_storage_backend": "ftp"}}, "https://s", tmp_path)
