"""Tests for storage backends and selection."""

from __future__ import annotations

from pathlib import Path

import pytest

from citypods.storage import StorageReadUnavailable, make_storage
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


def test_local_get_range_reads_a_byte_window(tmp_path):
    src = tmp_path / "src.m4a"
    src.write_bytes(b"0123456789")
    store = LocalStorage(root=tmp_path / "out", url_prefix="https://x/audio")
    store.put_file("k.m4a", src, "audio/mp4")

    assert store.get_range("k.m4a", 2, 5) == b"2345"  # inclusive end
    assert store.get_range("k.m4a", 8, 100) == b"89"  # end past EOF: just shorter, not an error
    assert store.get_range("missing.m4a", 0, 3) is None


def test_local_get_range_rejects_invalid_bounds(tmp_path):
    # CR2-CP-51: negative start seeks from EOF, and end < start yields a negative read() length
    # that reads to EOF -- both must return None (matching S3CompatibleStorage's HTTP-416
    # invalid-range contract) instead of returning the wrong bytes.
    src = tmp_path / "src.m4a"
    src.write_bytes(b"0123456789")
    store = LocalStorage(root=tmp_path / "out", url_prefix="https://x/audio")
    store.put_file("k.m4a", src, "audio/mp4")

    assert store.get_range("k.m4a", -1, 5) is None
    assert store.get_range("k.m4a", 5, 2) is None


def test_local_path_rejects_keys_that_escape_root(tmp_path):
    # CR2-CP-54: self.root / key can escape root via ".." or an absolute key (pathlib's `/`
    # operator lets an absolute rhs override the lhs entirely).
    store = LocalStorage(root=tmp_path / "out", url_prefix="https://x/audio")
    with pytest.raises(ValueError, match="escapes storage root"):
        store.exists("../../etc/passwd")
    with pytest.raises(ValueError, match="escapes storage root"):
        store.exists("/etc/passwd")


def test_local_path_allows_normal_nested_keys(tmp_path):
    store = LocalStorage(root=tmp_path / "out", url_prefix="https://x/audio")
    assert store.exists("denton-tx/abc.m4a") is False  # doesn't raise


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


def test_r2_public_base_required_for_public_backend(monkeypatch):
    from citypods.storage import s3 as s3_mod

    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.delenv("R2_PUBLIC_BASE_URL", raising=False)

    assert s3_mod.r2_from_env() is None


def test_r2_public_base_optional_for_routing_coordination(monkeypatch):
    from citypods.storage import s3 as s3_mod

    captured = {}

    def fake_storage(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(s3_mod, "S3CompatibleStorage", fake_storage)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.delenv("R2_PUBLIC_BASE_URL", raising=False)

    assert s3_mod.r2_from_env(require_public_base_url=False) == captured
    assert captured["public_base_url"] == "https://acct.r2.cloudflarestorage.com"
    assert captured["cas_capable"] is True


@pytest.mark.parametrize("blank", ["", "   "])
def test_r2_blank_endpoint_uses_account_default(monkeypatch, blank):
    from citypods.storage import s3 as s3_mod

    captured = {}

    def fake_storage(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(s3_mod, "S3CompatibleStorage", fake_storage)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://pub.example")
    monkeypatch.setenv("R2_ENDPOINT", blank)

    s3_mod.r2_from_env()

    assert captured["endpoint_url"] == "https://acct.r2.cloudflarestorage.com"


def test_r2_endpoint_override_is_preserved(monkeypatch):
    from citypods.storage import s3 as s3_mod

    captured = {}

    def fake_storage(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(s3_mod, "S3CompatibleStorage", fake_storage)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "bucket")
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://pub.example")
    monkeypatch.setenv("R2_ENDPOINT", " https://acct.eu.r2.cloudflarestorage.com ")

    s3_mod.r2_from_env()

    assert captured["endpoint_url"] == "https://acct.eu.r2.cloudflarestorage.com"


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


# ── R2-is-ephemeral invariant (Component 0, GH#496) ──────────────────────────────────


def test_every_coordination_prefix_is_declared_ephemeral():
    """R2 has no backup and is aggressively expired by the reclaim lifecycle, so a canonical record
    routed there would be unrecoverable. Every COORDINATION_PREFIXES entry must be declared
    ephemeral/derivable — adding a new R2 prefix without that declaration must fail loudly."""
    from citypods.storage import routing

    # The production tuple must already satisfy the invariant (import guard also enforces this).
    routing.assert_coordination_prefixes_ephemeral()
    for prefix in routing.COORDINATION_PREFIXES:
        assert prefix in routing._EPHEMERAL_R2_PREFIXES


def test_undeclared_coordination_prefix_trips_the_guard():
    from citypods.storage import routing

    with pytest.raises(AssertionError, match="not declared ephemeral"):
        routing.assert_coordination_prefixes_ephemeral(
            ("audio/",), allow={"work-leases/": "declared"}
        )


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

    def get_range(self, key, start, end):
        self.calls.append(("get_range", key))
        return b"data"

    def list_objects(self, prefix=""):
        self.calls.append(("list_objects", prefix))
        return iter(())

    def delete(self, key):
        self.calls.append(("delete", key))


class _FakeCASBackend(_FakeBackend):
    """A CAS-capable backend (like R2)."""

    cas_capable = True

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


def test_routing_get_range_dispatches_by_prefix():
    router, primary, coord = _router()
    assert router.get_range("audio/x.m4a", 0, 10) == b"data"
    assert ("get_range", "audio/x.m4a") in primary.calls
    assert coord.calls == []


def test_routing_list_objects_is_namespace_scoped():
    # A broad/straddling prefix lists the B2 primary only (coordination is key-addressed, never
    # enumerated — review/18 §4.6); a fully-coordination prefix routes to R2. No merge, no R2 list
    # cost on broad B2 listings.
    router, primary, coord = _router()
    list(router.list_objects(""))  # broad → primary only
    list(router.list_objects("coord/"))  # fully-coordination → R2
    assert ("list_objects", "") in primary.calls
    assert ("list_objects", "") not in coord.calls
    assert ("list_objects", "coord/") in coord.calls
    assert router.telemetry() == {"r2_class_a": 1, "r2_class_b": 0}  # only the coord list bills R2


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


def test_routing_catalog_manifest_cas_uses_coordination_backend():
    # The compact manifest is a CAS-published coordination object even though its key is nested
    # under state/catalog/. It must not be sent to the non-CAS B2 primary.
    from citypods.storage.routing import COORDINATION_PREFIXES

    router, primary, coord = _router(prefixes=COORDINATION_PREFIXES)
    key = "state/catalog/manifest.json"
    assert router.get_bytes(key) == (b"{}", '"etag"')
    router.put_cas(key, b"{}", "application/json", if_match='"etag"')
    assert ("get_bytes", key) in coord.calls
    assert ("put_cas", key) in coord.calls
    assert all(called_key != key for _, called_key in primary.calls)


def test_routing_cas_on_primary_key_raises_when_unsupported():
    # A coordination-prefix key with no coordination backend falls to the primary, which
    # has no CAS — surface a clear error rather than silently using a non-atomic path.
    router, _, _ = _router(with_coord=False)
    with pytest.raises(NotImplementedError, match="put_cas"):
        router.put_cas("coord/l.json", b"{}", "application/json", if_none_match="*")


class _FakeNonCasCapableS3Backend(_FakeBackend):
    """Mirrors S3CompatibleStorage: defines put_cas/get_bytes unconditionally, but
    cas_capable=False for a non-R2 backend (e.g. B2) — the exact shape CR2-CP-53/H2 covers."""

    cas_capable = False

    def get_bytes(self, key):
        self.calls.append(("get_bytes", key))
        return (b"{}", '"etag"')

    def put_cas(self, key, data, content_type, *, if_none_match=None, if_match=None):
        self.calls.append(("put_cas", key))
        return (f"https://{self.name}/{key}", '"new-etag"')


def test_routing_put_cas_on_non_cas_capable_primary_raises_not_silently_degrades():
    # CR2-CP-53/H2: coordination absent (R2 creds missing) → a coordination-prefixed key falls
    # through to the B2 primary. Before the fix, hasattr(primary, "put_cas") was True (the
    # method exists unconditionally on S3CompatibleStorage) so the write silently proceeded
    # non-atomically instead of raising — a real atomicity violation in the GPU-budget/work-lease
    # coordination substrate.
    primary = _FakeNonCasCapableS3Backend("b2")
    router = RoutingStorage(primary=primary, coordination=None, coordination_prefixes=("coord/",))
    with pytest.raises(NotImplementedError, match="put_cas"):
        router.put_cas("coord/l.json", b"{}", "application/json", if_none_match="*")
    with pytest.raises(NotImplementedError, match="get_bytes"):
        router.get_bytes("coord/l.json")
    assert primary.calls == []  # neither call reached the non-atomic backend method


def test_routing_put_cas_on_non_cas_capable_coordination_raises():
    # A coordination backend attached but itself not CAS-capable (misconfiguration) must also
    # raise, not silently degrade — the router checks the routed backend's own flag, not just
    # "is a coordination backend attached".
    primary = _FakeBackend("b2")
    coord = _FakeNonCasCapableS3Backend("r2-misconfigured")
    router = RoutingStorage(primary=primary, coordination=coord, coordination_prefixes=("coord/",))
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
        self.closed = False

    def read(self):
        return self._data

    def close(self):
        self.closed = True


class _FakeS3Client:
    """Minimal boto3-shaped client: an in-memory keyspace with ETag preconditions."""

    def __init__(self):
        self.store: dict[str, tuple[bytes, str]] = {}
        self.bodies: dict[str, _FakeBody] = {}
        self._n = 0
        self.lifecycle: list | None = None
        self.lifecycle_puts = 0

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

    def get_object(self, *, Bucket, Key, Range=None):
        if Key not in self.store:
            raise _client_error("NoSuchKey", 404)
        data, etag = self.store[Key]
        if Range is not None:
            start, end = (int(x) for x in Range.removeprefix("bytes=").split("-"))
            if start >= len(data) or end < start:
                raise _client_error("InvalidRange", 416)
            data = data[start : end + 1]
        body = _FakeBody(data)
        self.bodies[Key] = body
        return {"Body": body, "ETag": etag}

    def get_bucket_lifecycle_configuration(self, *, Bucket):
        if self.lifecycle is None:
            raise _client_error("NoSuchLifecycleConfiguration", 404)
        return {"Rules": list(self.lifecycle)}

    def put_bucket_lifecycle_configuration(self, *, Bucket, LifecycleConfiguration):
        self.lifecycle = list(LifecycleConfiguration["Rules"])
        self.lifecycle_puts += 1

    def head_object(self, *, Bucket, Key):
        if Key not in self.store:
            raise _client_error("404", 404)
        return {"ETag": self.store[Key][1]}


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


def test_put_file_retries_transient_multipart_upload_failure(tmp_path, monkeypatch):
    """Retry the transfer-manager wrapper after boto exhausts its per-part attempts."""
    from boto3.exceptions import S3UploadFailedError

    store = _s3_with_fake_client()
    source = tmp_path / "episodes.json"
    source.write_bytes(b"{}")
    calls = 0

    def flaky_upload(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise S3UploadFailedError("Failed to upload: An error occurred (ServiceUnavailable)")

    store._client.upload_file = flaky_upload
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    assert store.put_file("state/episodes.json", source, "application/json") == (
        "https://pub/state/episodes.json"
    )
    assert calls == 3


def test_put_cas_retries_transient_internal_error(monkeypatch):
    store = _s3_with_fake_client()

    class _FlakyPutClient(_FakeS3Client):
        def __init__(self):
            super().__init__()
            self.failures = [_client_error("InternalError", 500)]

        def put_object(self, **kwargs):
            if self.failures:
                raise self.failures.pop(0)
            return super().put_object(**kwargs)

    store._client = _FlakyPutClient()
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    _, etag = store.put_cas("k.json", b"v", "application/json", if_none_match="*")

    assert etag
    assert store._client.store["k.json"] == (b"v", etag)


def test_exists_true_for_present_key_false_for_absent():
    store = _s3_with_fake_client()
    store.put_cas("k.json", b"v", "application/json", if_none_match="*")
    assert store.exists("k.json") is True
    assert store.exists("missing.json") is False


def test_exists_reraises_non_absent_client_errors():
    # CR2-CP-52: a throttling/permission/transient error must not be conflated with genuine
    # absence, which risks a double-upload or a false orphan-GC signal.
    store = _s3_with_fake_client()

    class _ThrottledClient(_FakeS3Client):
        def head_object(self, *, Bucket, Key):
            raise _client_error("SlowDown", 503)

    store._client = _ThrottledClient()
    with pytest.raises(StorageReadUnavailable, match="SlowDown") as caught:
        store.exists("k.json")
    assert caught.value.key == "k.json"
    assert caught.value.cause.response["Error"]["Code"] == "SlowDown"


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
    assert store._client.bodies["k.json"].closed is True


def test_get_bytes_retries_transient_s3_internal_error(monkeypatch):
    store = _s3_with_fake_client()

    class _FlakyGetClient(_FakeS3Client):
        def __init__(self):
            super().__init__()
            self.failures = [_client_error("InternalError", 500)]

        def get_object(self, *, Bucket, Key, Range=None):
            if self.failures:
                raise self.failures.pop(0)
            self.store[Key] = (b"hello", '"etag-1"')
            return super().get_object(Bucket=Bucket, Key=Key, Range=Range)

    client = _FlakyGetClient()
    store._client = client
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    data, etag = store.get_bytes("lease.json")
    assert data == b"hello" and etag == '"etag-1"'


@pytest.mark.parametrize("operation", ["bytes", "range"])
def test_get_reads_retry_transient_body_read_error(monkeypatch, operation):
    store = _s3_with_fake_client()

    class _ReadFlakyClient(_FakeS3Client):
        def __init__(self):
            super().__init__()
            self.failures = [_client_error("InternalError", 500)]
            self.store["k"] = (b"0123456789", '"etag-1"')

        def get_object(self, *, Bucket, Key, Range=None):
            response = super().get_object(Bucket=Bucket, Key=Key, Range=Range)
            body = response["Body"]
            read = body.read

            def flaky_read():
                if self.failures:
                    raise self.failures.pop(0)
                return read()

            body.read = flaky_read
            return response

    store._client = _ReadFlakyClient()
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    if operation == "bytes":
        assert store.get_bytes("k") == (b"0123456789", '"etag-1"')
    else:
        assert store.get_range("k", 2, 5) == b"2345"


def test_lifecycle_rules_empty_then_put_readback():
    store = _s3_with_fake_client()
    assert store.get_lifecycle_rules() == []  # NoSuchLifecycleConfiguration → []
    rules = [
        {
            "ID": "reclaim-r2-scratch-work-leases-validate",
            "Filter": {"Prefix": "work-leases/__validate__/"},
            "Status": "Enabled",
            "Expiration": {"Days": 1},
        }
    ]
    store.put_lifecycle_rules(rules)
    assert store.get_lifecycle_rules() == rules


def test_get_range_reads_a_byte_window_and_absent():
    store = _s3_with_fake_client()
    store.put_cas("k.m4a", b"0123456789", "audio/mp4", if_none_match="*")

    assert store.get_range("k.m4a", 2, 5) == b"2345"  # inclusive end, matches HTTP Range
    assert store.get_range("k.m4a", 99, 120) is None  # start past EOF: real S3 = 416
    assert store.get_range("missing.m4a", 0, 3) is None
    assert store._client.bodies["k.m4a"].closed is True


class _FakeDownloadClient:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def download_file(self, bucket, key, path):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        Path(path).write_text(f"{bucket}:{key}")


def test_get_file_retries_transfer_failures_then_succeeds(tmp_path, monkeypatch):
    from botocore.exceptions import ReadTimeoutError

    store = _s3_with_fake_client()
    client = _FakeDownloadClient([ReadTimeoutError(endpoint_url="https://x", error="timeout")])
    store._client = client
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    dest = tmp_path / "state.json"

    assert store.get_file("state/k.json", dest) is True
    assert dest.read_text() == "b:state/k.json"
    assert client.calls == 2


def test_get_file_retries_etag_race_then_succeeds(tmp_path, monkeypatch):
    from s3transfer.exceptions import S3DownloadFailedError

    store = _s3_with_fake_client()
    client = _FakeDownloadClient(
        [
            S3DownloadFailedError(
                'Contents of stored object "state/k.json" did not match expected ETag.'
            )
        ]
    )
    store._client = client
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    dest = tmp_path / "state.json"

    assert store.get_file("state/k.json", dest) is True
    assert dest.read_text() == "b:state/k.json"
    assert client.calls == 2


def test_transient_storage_error_accepts_sdk_independent_injection(monkeypatch):
    from citypods.storage import s3 as s3_module

    real_import = __import__

    def block_optional_sdk(name, *args, **kwargs):
        if name in {"boto3", "botocore", "s3transfer"} or name.startswith(
            ("boto3.", "botocore.", "s3transfer.")
        ):
            raise ImportError("optional S3 SDK unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", block_optional_sdk)

    class InjectedTransportError(Exception):
        pass

    assert s3_module.is_transient_storage_error(
        InjectedTransportError("connection reset"),
        transient_errors=(InjectedTransportError,),
    )


def test_get_file_raises_after_exhausting_transfer_retries(tmp_path, monkeypatch):
    from botocore.exceptions import ReadTimeoutError

    store = _s3_with_fake_client()
    client = _FakeDownloadClient(
        [ReadTimeoutError(endpoint_url="https://x", error="timeout") for _ in range(3)]
    )
    store._client = client
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    with pytest.raises(StorageReadUnavailable) as caught:
        store.get_file("state/k.json", tmp_path / "state.json")
    assert client.calls == 3
    assert caught.value.key == "state/k.json"
    assert isinstance(caught.value.cause, ReadTimeoutError)


def test_get_file_preserves_existing_destination_after_exhausted_retry(tmp_path, monkeypatch):
    from botocore.exceptions import ReadTimeoutError

    store = _s3_with_fake_client()
    destination = tmp_path / "state.json"
    destination.write_text("known-good")

    class _PartialDownloadClient(_FakeDownloadClient):
        def download_file(self, bucket, key, path):
            Path(path).write_text("partial")
            super().download_file(bucket, key, path)

    store._client = _PartialDownloadClient(
        [ReadTimeoutError(endpoint_url="https://x", error="timeout") for _ in range(3)]
    )
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    with pytest.raises(StorageReadUnavailable):
        store.get_file("state/k.json", destination)

    assert destination.read_text() == "known-good"
    assert not (tmp_path / ".state.json.download").exists()


def test_get_file_returns_false_only_for_absent_objects(tmp_path):
    store = _s3_with_fake_client()
    store._client = _FakeDownloadClient([_client_error("NoSuchKey", 404)])

    assert store.get_file("missing.json", tmp_path / "missing.json") is False


def test_get_file_raises_non_absent_client_errors(tmp_path, monkeypatch):
    store = _s3_with_fake_client()
    store._client = _FakeDownloadClient([_client_error("InternalError", 500) for _ in range(3)])
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    # Transient server errors are retried, but still surface after the bounded retry budget.
    with pytest.raises(StorageReadUnavailable, match="InternalError") as caught:
        store.get_file("state/k.json", tmp_path / "k.json")
    assert store._client.calls == 3
    assert caught.value.key == "state/k.json"


def test_get_file_retries_streaming_checksum_parser_failure_then_succeeds(tmp_path, monkeypatch):
    store = _s3_with_fake_client()
    parser_error = AttributeError("'StreamingChecksumBody' object has no attribute 'strip'")
    client = _FakeDownloadClient([parser_error])
    store._client = client
    monkeypatch.setattr("citypods.storage.s3.time.sleep", lambda _seconds: None)

    assert store.get_file("state/k.json", tmp_path / "state.json") is True
    assert client.calls == 2
