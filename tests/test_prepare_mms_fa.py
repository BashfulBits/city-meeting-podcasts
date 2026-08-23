"""Tests for scripts/prepare_mms_fa.py model preparation and caching."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts import prepare_mms_fa as pm


def test_mms_fa_constants():
    assert pm.MMS_FA_URL.startswith("https://dl.fbaipublicfiles.com/mms/torchaudio/")
    assert pm.SENTINEL == "model.pt"
    assert pm.B2_PREFIX == "models/mms-fa/v1"


def test_complete_checks_non_empty_sentinel(tmp_path):
    assert not pm._complete(tmp_path)

    empty_file = tmp_path / pm.SENTINEL
    empty_file.write_bytes(b"")
    assert not pm._complete(tmp_path)

    empty_file.write_bytes(b"non-empty checkpoint bytes")
    assert pm._complete(tmp_path)


def test_download_upstream_atomic_write(monkeypatch, tmp_path):
    import requests

    class _FakeResp:
        status_code = 200
        headers = {"content-length": "12"}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=0):
            yield b"fake_weights"

    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResp())

    ok = pm._download_upstream("https://example.com/model.pt", tmp_path, retries=1)
    assert ok
    target = tmp_path / pm.SENTINEL
    assert target.exists()
    assert target.read_bytes() == b"fake_weights"
    assert not (tmp_path / f"{pm.SENTINEL}.part").exists()


def test_download_upstream_retry_on_failure(monkeypatch, tmp_path):
    import requests

    attempts = 0

    class _FakeResp:
        status_code = 200
        headers = {"content-length": "12"}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=0):
            yield b"fake_weights"

    def _flaky_get(*a, **k):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise requests.ConnectionError("Connection reset")
        return _FakeResp()

    monkeypatch.setattr(requests, "get", _flaky_get)
    monkeypatch.setattr(pm.time, "sleep", lambda s: None)

    ok = pm._download_upstream("https://example.com/model.pt", tmp_path, retries=2)
    assert ok
    assert attempts == 2
    assert (tmp_path / pm.SENTINEL).read_bytes() == b"fake_weights"


def test_b2_download(tmp_path):
    client = MagicMock()

    def _fake_download_file(bucket, key, local_path):
        from pathlib import Path

        Path(local_path).write_bytes(b"b2_checkpoint_data")

    client.download_file = _fake_download_file

    ok = pm._b2_download(client, "test-bucket", tmp_path)
    assert ok
    assert (tmp_path / pm.SENTINEL).read_bytes() == b"b2_checkpoint_data"


def test_main_short_circuits_on_local_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "CHECKPOINT_DIR", tmp_path)
    (tmp_path / pm.SENTINEL).write_bytes(b"existing_cached_weights")

    b2_client_mock = MagicMock()
    monkeypatch.setattr(pm, "_b2_client", b2_client_mock)

    exit_code = pm.main()
    assert exit_code == 0
    b2_client_mock.assert_not_called()


def test_main_graceful_on_all_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "CHECKPOINT_DIR", tmp_path)
    monkeypatch.setattr(pm, "_b2_client", lambda: (None, None))
    monkeypatch.setattr(pm, "_download_upstream", lambda *a, **k: False)

    exit_code = pm.main()
    assert exit_code == 0
