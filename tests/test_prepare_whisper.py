"""Guard that Whisper model downloads are pinned to explicit revisions (GH#498).

An unpinned ``main`` lets model bytes drift silently while ``asr_spec_hash()`` still
treats the recipe as unchanged. These tests assert both download paths (direct CDN and
``snapshot_download``) carry the pinned revision, and that the B2 mirror is
revision-scoped.
"""

from __future__ import annotations

import re

import pytest

from scripts import prepare_whisper as pw

_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def test_revisions_are_pinned_full_commit_shas():
    assert _HEX40.match(pw.HF_PREFERRED_REVISION), "preferred revision must be a 40-hex commit SHA"
    assert _HEX40.match(pw.HF_FALLBACK_REVISION), "fallback revision must be a 40-hex commit SHA"


def test_resolve_url_pins_revision_not_main():
    url = pw._resolve_url(pw.HF_PREFERRED, pw.HF_PREFERRED_REVISION, "model.bin")
    assert f"/resolve/{pw.HF_PREFERRED_REVISION}/model.bin" in url
    assert "/resolve/main/" not in url


def test_b2_prefix_is_revision_scoped():
    # A revision bump must land under a fresh prefix, not overwrite the old bytes.
    assert pw.HF_PREFERRED_REVISION in pw.B2_PREFIX


def test_direct_download_requests_only_pinned_revision(monkeypatch, tmp_path):
    import requests

    captured: list[str] = []

    class _FakeResp:
        status_code = 200
        headers = {"content-length": "3"}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=0):
            yield b"abc"

    def _fake_get(url, headers=None, stream=None, timeout=None):
        captured.append(url)
        return _FakeResp()

    monkeypatch.setattr(requests, "get", _fake_get)
    rev = "deadbeef" * 5  # 40 hex chars
    ok = pw._hf_download_direct("some/repo", tmp_path, rev, token=None)

    assert ok
    assert captured, "no files were requested"
    assert all(f"/resolve/{rev}/" in u for u in captured)
    assert not any("/resolve/main/" in u for u in captured)


def test_snapshot_download_receives_revision(monkeypatch, tmp_path):
    pytest.importorskip("huggingface_hub")
    import huggingface_hub

    # Force the direct CDN path to fail so the snapshot_download fallback runs.
    monkeypatch.setattr(pw, "_hf_download_direct", lambda *a, **k: False)

    calls: dict[str, str] = {}

    def _fake_snapshot(repo, revision=None, local_dir=None):
        calls["repo"] = repo
        calls["revision"] = revision
        from pathlib import Path

        for name in pw._CT2_FILES_REQUIRED:
            (Path(local_dir) / name).write_text("x")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", _fake_snapshot)
    rev = "cafe" * 10  # 40 hex chars
    ok = pw._hf_download("some/repo", tmp_path, rev, retries=1)

    assert ok
    assert calls["revision"] == rev, "snapshot_download must be pinned to the revision"
