import hashlib
import json
import struct
from unittest.mock import patch

import pytest

from citypods.weight_streaming import SliceManifest, gcs_range_fetcher, stream_safetensors


def fixture(header=None, payload=None, chunk=128):
    if payload is None:
        payload = bytes(range(128))
    if header is None:
        header = {"x": {"dtype": "BF16", "shape": [64], "data_offsets": [0, 128]}}
    encoded = json.dumps(header).encode()
    data = struct.pack("<Q", len(encoded)) + encoded + payload
    manifest = SliceManifest(
        len(data),
        chunk,
        tuple(hashlib.sha256(data[i : i + chunk]).hexdigest() for i in range(0, len(data), chunk)),
    )
    return data, manifest


def test_stream_reassembles_and_orders_segments():
    data, manifest = fixture()
    parts = []
    stream_safetensors(
        manifest,
        lambda a, b: data[a:b],
        lambda spec, offset, view: parts.append((spec, offset, bytes(view))),
    )
    assert all(
        spec.name == "x" and spec.dtype == "BF16" and spec.shape == (64,) for spec, _, _ in parts
    )
    assert [offset for _, offset, _ in parts] == sorted(offset for _, offset, _ in parts)
    assert b"".join(part for _, _, part in parts) == bytes(range(128))


def test_corrupt_slice_is_never_delivered():
    data, manifest = fixture()
    delivered = []

    def fetch(a, b):
        return data[a:b] if a == 0 else b"x" * (b - a)

    with pytest.raises(ValueError, match="SHA-256"):
        stream_safetensors(
            manifest, fetch, lambda spec, offset, view: delivered.append(bytes(view))
        )
    assert b"".join(delivered) == bytes(range(len(delivered[0])))


@pytest.mark.parametrize(
    "header",
    [
        {"x": {"dtype": "BF16", "shape": [64], "data_offsets": [1, 128]}},
        {"x": {"dtype": "unknown", "shape": [64], "data_offsets": [0, 128]}},
        {"x": {"dtype": "BF16", "shape": [True], "data_offsets": [0, 128]}},
        {"x": {"dtype": "BF16", "shape": [63], "data_offsets": [0, 126]}},
    ],
)
def test_bad_header_fails_before_delivery(header):
    data, manifest = fixture(header)
    delivered = []
    with pytest.raises(ValueError):
        stream_safetensors(manifest, lambda a, b: data[a:b], lambda *args: delivered.append(args))
    assert not delivered


def test_invalid_manifest_fails_before_fetch():
    data, manifest = fixture()
    calls = []
    with pytest.raises(ValueError):
        stream_safetensors(
            manifest, lambda *args: calls.append(args), lambda *args: None, workers=33
        )
    assert not calls


def test_zero_size_tensor_is_reported():
    data, manifest = fixture(
        {"empty": {"dtype": "F32", "shape": [0], "data_offsets": [0, 0]}}, payload=b""
    )
    observed = []
    stream_safetensors(
        manifest,
        lambda a, b: data[a:b],
        lambda spec, offset, view: observed.append((spec.size, offset, len(view))),
    )
    assert observed == [(0, 0, 0)]


def test_gcs_generation_range_and_response_validation():
    class Response:
        status = 206
        headers = {"Content-Range": "bytes 1-2/3"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b"bc"

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.endswith("/o/path%2Fname?alt=media&generation=123")
            assert request.get_header("Range") == "bytes=1-2"
            assert request.get_header("Authorization") == "Bearer secret"
            return Response()

    with patch("citypods.weight_streaming.transport.build_opener", return_value=Opener()):
        fetch = gcs_range_fetcher("bucket", "path/name", "123", 3, "secret")
        assert fetch(1, 3) == b"bc"
        with pytest.raises(ValueError):
            fetch(0, 4)
        Response.headers = {"Content-Range": "bytes 0-2/3"}
        with pytest.raises(ValueError, match="content range"):
            fetch(1, 3)


def test_concurrent_fetches_are_bounded_and_delivery_ordered():
    import threading

    payload = bytes(range(256)) * 8
    header = {"x": {"dtype": "U8", "shape": [len(payload)], "data_offsets": [0, len(payload)]}}
    data, manifest = fixture(header, payload, chunk=128)
    condition = threading.Condition()
    active = 0
    peak = 0
    released = False

    def fetch(start, end):
        nonlocal active, peak, released
        if start == 0:
            return data[start:end]
        with condition:
            active += 1
            peak = max(peak, active)
            if active == 16:
                released = True
                condition.notify_all()
            assert condition.wait_for(lambda: released, timeout=5)
            active -= 1
        return data[start:end]

    parts = []
    stream_safetensors(
        manifest, fetch, lambda spec, offset, view: parts.append((offset, bytes(view))), workers=16
    )
    assert peak == 16
    assert [offset for offset, _ in parts] == sorted(offset for offset, _ in parts)
    assert b"".join(part for _, part in parts) == payload


def test_final_slice_failure_never_delivers_corrupt_bytes():
    payload = bytes(range(256)) * 3
    header = {"x": {"dtype": "U8", "shape": [len(payload)], "data_offsets": [0, len(payload)]}}
    data, manifest = fixture(header, payload, chunk=128)
    delivered = []

    def fetch(start, end):
        return b"z" * (end - start) if end == len(data) else data[start:end]

    with pytest.raises(ValueError, match="SHA-256"):
        stream_safetensors(
            manifest, fetch, lambda spec, offset, view: delivered.append(bytes(view))
        )
    assert payload.startswith(b"".join(delivered))
    assert len(b"".join(delivered)) < len(payload)


def test_gcs_composition_binds_manifest_identity():
    from citypods.weight_streaming import GCSManifest, stream_gcs_safetensors

    data, slices = fixture()
    manifest = GCSManifest(
        "bucket", "weights", "123", slices.size, slices.chunk_size, slices.sha256
    )
    observed = []
    with patch("citypods.weight_streaming.gcs.gcs_range_fetcher") as factory:
        factory.return_value = lambda start, end: data[start:end]
        stream_gcs_safetensors(
            manifest, "token", lambda spec, offset, view: observed.append(bytes(view))
        )
    factory.assert_called_once_with("bucket", "weights", "123", len(data), "token")
    assert b"".join(observed) == bytes(range(128))


def test_non_utf8_header_is_rejected_even_when_hash_matches():
    header = '{"x":{"dtype":"U8","shape":[1],"data_offsets":[0,1]}}'.encode("utf-16")
    data = struct.pack("<Q", len(header)) + header + b"x"
    manifest = SliceManifest(len(data), len(data), (hashlib.sha256(data).hexdigest(),))
    calls = []
    with pytest.raises(ValueError, match="header"):
        stream_safetensors(manifest, lambda a, b: data[a:b], lambda *args: calls.append(args))
    assert not calls
