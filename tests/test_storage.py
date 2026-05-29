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


def test_make_storage_r2_without_env_returns_none(tmp_path, monkeypatch):
    for var in (
        "CLOUDFLARE_ACCOUNT_ID",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET",
        "R2_PUBLIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = {"defaults": {"audio_storage_backend": "r2"}}
    assert make_storage(cfg, "https://site", tmp_path) is None


def test_make_storage_unknown_backend(tmp_path):
    with pytest.raises(ValueError, match="unknown audio_storage_backend"):
        make_storage({"defaults": {"audio_storage_backend": "ftp"}}, "https://s", tmp_path)
