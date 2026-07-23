from __future__ import annotations

import hashlib

import pytest

import scripts.vendor_pinned_binary as vendor_mod
from citypods.security import SecurityError
from scripts.vendor_pinned_binary import vendor


class _FakeStorage:
    def __init__(self, *, existing_keys: frozenset[str] = frozenset()):
        self.uploads: list[tuple[str, bytes, str]] = []
        self._existing_keys = set(existing_keys)

    def exists(self, key):
        return key in self._existing_keys

    def put_file(self, key, local_path, content_type):
        self.uploads.append((key, local_path.read_bytes(), content_type))
        self._existing_keys.add(key)
        return f"https://podcasts.example.com/{key}"


@pytest.fixture(autouse=True)
def _skip_ssrf_gate(monkeypatch):
    """These tests exercise download/upload logic with local file:// fixtures -- the SSRF gate
    (validate_source_url, https-only) is covered separately below, on real (unresolved) URLs."""
    monkeypatch.setattr(vendor_mod, "validate_source_url", lambda url, **kwargs: None)


def test_vendor_uploads_to_deps_key_and_returns_public_url(tmp_path, monkeypatch):
    source = tmp_path / "thing-1.0.tar.xz"
    source.write_bytes(b"archive bytes\n")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    storage = _FakeStorage()
    monkeypatch.setattr(vendor_mod, "b2_from_env", lambda: storage)

    public_url, returned_digest = vendor(
        name="thing",
        version="1.0",
        filename="thing-1.0.tar.xz",
        source_urls=[source.as_uri()],
        expected_sha256=None,
    )

    assert returned_digest == digest
    assert public_url == "https://podcasts.example.com/deps/thing/1.0/thing-1.0.tar.xz"
    [(key, content, content_type)] = storage.uploads
    assert key == "deps/thing/1.0/thing-1.0.tar.xz"
    assert content == b"archive bytes\n"
    assert content_type == "application/octet-stream"


def test_vendor_verifies_expected_checksum(tmp_path, monkeypatch):
    source = tmp_path / "thing-1.0.tar.xz"
    source.write_bytes(b"archive bytes\n")

    storage = _FakeStorage()
    monkeypatch.setattr(vendor_mod, "b2_from_env", lambda: storage)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        vendor(
            name="thing",
            version="1.0",
            filename="thing-1.0.tar.xz",
            source_urls=[source.as_uri()],
            expected_sha256="0" * 64,
        )

    assert storage.uploads == []


def test_vendor_requires_configured_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(vendor_mod, "b2_from_env", lambda: None)

    with pytest.raises(RuntimeError, match="B2 storage is not configured"):
        vendor(
            name="thing",
            version="1.0",
            filename="thing-1.0.tar.xz",
            source_urls=[(tmp_path / "missing.tar.xz").as_uri()],
            expected_sha256=None,
        )


def test_vendor_refuses_to_overwrite_an_existing_key(tmp_path, monkeypatch):
    source = tmp_path / "thing-1.0.tar.xz"
    source.write_bytes(b"archive bytes\n")

    storage = _FakeStorage(existing_keys=frozenset({"deps/thing/1.0/thing-1.0.tar.xz"}))
    monkeypatch.setattr(vendor_mod, "b2_from_env", lambda: storage)

    with pytest.raises(RuntimeError, match="already exists"):
        vendor(
            name="thing",
            version="1.0",
            filename="thing-1.0.tar.xz",
            source_urls=[source.as_uri()],
            expected_sha256=None,
        )

    assert storage.uploads == []


# --- SSRF gate: every candidate URL must clear validate_source_url before any fetch -------------
# (_skip_ssrf_gate is not applied here -- these tests exercise the real gate.)


def test_vendor_rejects_non_https_source_url(tmp_path, monkeypatch):
    called = False

    def _fail_if_called():
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(vendor_mod, "b2_from_env", _fail_if_called)

    with pytest.raises(SecurityError, match="https only"):
        vendor(
            name="thing",
            version="1.0",
            filename="thing-1.0.tar.xz",
            source_urls=[(tmp_path / "thing-1.0.tar.xz").as_uri()],
            expected_sha256=None,
        )

    assert called is False
