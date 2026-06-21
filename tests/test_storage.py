"""Tests for storage backends and selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from citypods.storage import make_storage
from citypods.storage.local import LocalStorage
from citypods.storage.routing import RoutingStorage
from citypods.storage.s3 import CASConflict, S3CompatibleStorage


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


def test_make_storage_routing_without_b2_env_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDIO_STORAGE_BACKEND", raising=False)
    for var in ("B2_ENDPOINT", "B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "B2_PUBLIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    cfg = {"defaults": {"audio_storage_backend": "routing"}}
    assert make_storage(cfg, "https://site", tmp_path) is None


# ── RoutingStorage ─────────────────────────────────────────────────────────────────


class _FakeBackend:
    """Records which backend a call landed on. No CAS surface (like B2)."""

    def __init__(self, name: str):
        self.name = name
        self.calls: list[tuple[str, str]] = []  # (method, key)

    def exists(self, key):
        self.calls.append(("exists", key))
        return False

    def put_file(self, key, local_path, content_type):
        self.calls.append(("put_file", key))
        return f"https://{self.name}/{key}"

    def public_url(self, key):
        return f"https://{self.name}/{key}"

    def get_file(self, key, local_path):
        self.calls.append(("get_file", key))
        return False

    def list_objects(self, prefix=""):
        self.calls.append(("list_objects", prefix))
        return iter(())

    def delete(self, key):
        self.calls.append(("delete", key))


class _FakeCASBackend(_FakeBackend):
    """A CAS-capable backend (like R2)."""

    def get_bytes(self, key):
        self.calls.append(("get_bytes", key))
        return (b"{}", '"etag"')

    def put_cas(self, key, data, content_type, *, if_none_match=None, if_match=None):
        self.calls.append(("put_cas", key))
        return (f"https://{self.name}/{key}", '"new-etag"')


def _router(prefixes=("coord/",), *, with_coord=True):
    primary = _FakeBackend("b2")
    coord = _FakeCASBackend("r2") if with_coord else None
    return (
        RoutingStorage(primary=primary, coordination=coord, coordination_prefixes=prefixes),
        primary,
        coord,
    )


def test_routing_dispatches_by_prefix():
    router, primary, coord = _router()
    router.put_file("audio/x.m4a", Path("/tmp/x"), "audio/mp4")
    router.put_file("coord/lease.json", Path("/tmp/l"), "application/json")
    assert ("put_file", "audio/x.m4a") in primary.calls
    assert ("put_file", "coord/lease.json") in coord.calls
    assert all(k != "coord/lease.json" for _, k in primary.calls)


def test_routing_degrades_to_primary_without_coordination():
    router, primary, _ = _router(with_coord=False)
    router.put_file("coord/lease.json", Path("/tmp/l"), "application/json")
    assert ("put_file", "coord/lease.json") in primary.calls


def test_routing_empty_prefixes_is_noop_all_primary():
    router, primary, coord = _router(prefixes=())
    router.put_file("coord/lease.json", Path("/tmp/l"), "application/json")
    assert ("put_file", "coord/lease.json") in primary.calls
    assert coord.calls == []
    assert router.telemetry() == {"r2_class_a": 0, "r2_class_b": 0}


def test_routing_class_a_b_telemetry():
    router, _, _ = _router()
    router.put_file("coord/a.json", Path("/tmp/a"), "application/json")  # A
    list(router.list_objects("coord/"))  # A (listing is Class A on R2)
    router.delete("coord/a.json")  # A
    router.exists("coord/a.json")  # B
    router.get_file("coord/a.json", Path("/tmp/a"))  # B
    # primary-routed ops are not counted
    router.put_file("audio/x", Path("/tmp/x"), "audio/mp4")
    assert router.telemetry() == {"r2_class_a": 3, "r2_class_b": 2}


def test_routing_cas_delegates_to_coordination():
    router, _, coord = _router()
    assert router.get_bytes("coord/l.json") == (b"{}", '"etag"')
    url, etag = router.put_cas("coord/l.json", b"{}", "application/json", if_none_match="*")
    assert etag == '"new-etag"'
    assert ("get_bytes", "coord/l.json") in coord.calls
    assert ("put_cas", "coord/l.json") in coord.calls
    assert router.telemetry() == {"r2_class_a": 1, "r2_class_b": 1}


def test_routing_cas_on_primary_key_raises_when_unsupported():
    # A coordination-prefix key with no coordination backend falls to the primary, which
    # has no CAS — surface a clear error rather than silently using a non-atomic path.
    router, _, _ = _router(with_coord=False)
    with pytest.raises(NotImplementedError, match="put_cas"):
        router.put_cas("coord/l.json", b"{}", "application/json", if_none_match="*")


# ── S3CompatibleStorage CAS (put_cas / get_bytes) via a fake boto3 client ───────────


def _client_error(code, status):
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "PutObject",
    )


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3Client:
    """Minimal boto3-shaped client: an in-memory keyspace with ETag preconditions."""

    def __init__(self):
        self.store: dict[str, tuple[bytes, str]] = {}
        self._n = 0

    def _next_etag(self):
        self._n += 1
        return f'"etag-{self._n}"'

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, IfMatch=None):
        exists = Key in self.store
        if IfNoneMatch == "*" and exists:
            raise _client_error("PreconditionFailed", 412)
        if IfMatch is not None and (not exists or self.store[Key][1] != IfMatch):
            raise _client_error("PreconditionFailed", 412)
        etag = self._next_etag()
        self.store[Key] = (Body, etag)
        return {"ETag": etag}

    def get_object(self, *, Bucket, Key):
        if Key not in self.store:
            raise _client_error("NoSuchKey", 404)
        data, etag = self.store[Key]
        return {"Body": _FakeBody(data), "ETag": etag}


def _s3_with_fake_client():
    store = S3CompatibleStorage(
        name="r2",
        endpoint_url="https://x",
        access_key_id="k",
        secret_access_key="s",
        bucket="b",
        public_base_url="https://pub",
    )
    store._client = _FakeS3Client()
    return store


def test_put_cas_create_if_absent_then_conflict():
    store = _s3_with_fake_client()
    url, etag = store.put_cas("k.json", b"v1", "application/json", if_none_match="*")
    assert url == "https://pub/k.json" and etag
    with pytest.raises(CASConflict):
        store.put_cas("k.json", b"v2", "application/json", if_none_match="*")


def test_put_cas_update_success_then_stale():
    store = _s3_with_fake_client()
    _, etag1 = store.put_cas("k.json", b"v1", "application/json", if_none_match="*")
    _, etag2 = store.put_cas("k.json", b"v2", "application/json", if_match=etag1)
    assert etag2 != etag1
    with pytest.raises(CASConflict):  # etag1 is now stale
        store.put_cas("k.json", b"v3", "application/json", if_match=etag1)


def test_get_bytes_roundtrip_and_absent():
    store = _s3_with_fake_client()
    assert store.get_bytes("missing.json") is None
    _, etag = store.put_cas("k.json", b"hello", "application/json", if_none_match="*")
    data, got_etag = store.get_bytes("k.json")
    assert data == b"hello" and got_etag == etag
