import hashlib

import pytest

from citypods.judging.glide_stream import validate_manifest
from citypods.judging.karpo_stream import KARPO_PREFIXES
from scripts.derive_karpo_manifest import derive, hash_object, hf_expected

MIB = 1024**2
REVISION = "664640316919ce368750b3e16cc263e927ac6e2b"
PREFIX = KARPO_PREFIXES[REVISION]


def blobs():
    return {
        "config.json": b"c" * 100,
        "decision_config.json": b"d" * 50,
        "readout.safetensors": b"r" * 10,
        "model-00001-of-00001.safetensors": bytes(range(256)) * (MIB // 128),  # 2 MiB
        "README.md": b"skipped",
        "assets/logo.png": b"skipped",
    }


def derive_from(files, expected, sizes=(MIB, 2 * MIB), prefix=PREFIX):
    objects = [(f"{prefix}/{n}", "7", len(d)) for n, d in files.items()]
    by_name = {f"{prefix}/{n}": d for n, d in files.items()}
    return derive(
        "bucket-a",
        REVISION,
        objects,
        lambda name, generation, size: lambda a, b: by_name[name][a:b],
        sizes,
        expected,
        workers=2,
    )


def test_hash_object_reads_once_and_hashes_each_slice_size():
    data = bytes(range(251)) * (3 * MIB // 251 + 1)
    data = data[: 3 * MIB + 5]
    full, slices = hash_object(lambda a, b: data[a:b], len(data), (MIB, 2 * MIB), workers=3)
    assert full == hashlib.sha256(data).hexdigest()
    assert slices[MIB] == [
        hashlib.sha256(data[i : i + MIB]).hexdigest() for i in range(0, len(data), MIB)
    ]
    assert len(slices[2 * MIB]) == 2


def test_derived_manifest_validates_and_checks_expected_digests():
    files = blobs()
    expected = {
        name: hashlib.sha256(files[name]).hexdigest()
        for name in ("model-00001-of-00001.safetensors", "readout.safetensors")
    }
    result = derive_from(files, expected)
    manifest = result[MIB]
    names = [o["name"] for o in manifest["objects"]]
    assert all("README" not in n and "assets" not in n for n in names)
    assert sorted(manifest["unreferenced"]) == ["config.json", "decision_config.json"]
    assert len(validate_manifest(manifest, revision=REVISION, prefix=PREFIX)) == 4
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        derive_from(files, {**expected, "model-00001-of-00001.safetensors": "0" * 64})


def test_hf_expected_reads_lfs_oids_only():
    tree = [
        {"path": "a.safetensors", "lfs": {"oid": "a" * 64}},
        {"path": "config.json"},
    ]
    assert hf_expected(tree) == {"a.safetensors": "a" * 64}
    with pytest.raises(ValueError):
        hf_expected([{"path": "x", "lfs": {"oid": "short"}}])


def test_validate_accepts_a_manifest_larger_than_glide_limit():
    from citypods.judging.karpo_stream import validate_manifest as validate_karpo

    names = ["config.json", "decision_config.json", "readout.safetensors"]
    sizes = {n: 10 for n in names} | {
        "model-00001-of-00002.safetensors": 13 * 1024**3,
        "model-00002-of-00002.safetensors": 23_919_544_312 - 13 * 1024**3,
    }
    chunk = 64 * 1024**2
    objects = [
        {
            "name": f"{PREFIX}/{n}",
            "generation": "1",
            "size": size,
            "sha256": "0" * 64,
            "slices": ["0" * 64] * ((size + chunk - 1) // chunk),
        }
        for n, size in sizes.items()
    ]
    manifest = {
        "bucket": "bucket-a",
        "revision": REVISION,
        "slice_bytes": chunk,
        "objects": objects,
    }
    assert len(validate_karpo(manifest)) == 5
    with pytest.raises(ValueError):
        validate_manifest(manifest, revision=REVISION, prefix=PREFIX)  # GLiDE's 20 GiB cap


def test_slice_sizes_are_validated_before_any_work():
    from scripts.derive_karpo_manifest import validate_slice_sizes

    validate_slice_sizes([8 * MIB, 32 * MIB, 64 * MIB])
    for bad in ([], [0], [-8 * MIB], [128 * MIB], [3 * MIB], [MIB // 2], [True]):
        with pytest.raises(ValueError):
            validate_slice_sizes(bad)
    files = blobs()
    with pytest.raises(ValueError, match="slice sizes"):
        derive_from(files, {}, sizes=(0,))


def test_every_weight_needs_a_publisher_digest_before_hashing():
    files = blobs()
    calls = []
    objects = [(f"{PREFIX}/{n}", "7", len(d)) for n, d in files.items()]

    def fetcher_for(name, generation, size):
        calls.append(name)
        return lambda a, b: files[name[len(PREFIX) + 1 :]][a:b]

    only_weight = {
        "model-00001-of-00001.safetensors": hashlib.sha256(
            files["model-00001-of-00001.safetensors"]
        ).hexdigest()
    }
    with pytest.raises(ValueError, match="no publisher SHA-256.*readout"):
        derive("bucket-a", REVISION, objects, fetcher_for, (MIB,), only_weight)
    assert calls == []  # rejected before any object was read


def test_hash_object_fetches_each_range_once_and_digests_match_both_sizes():
    data = bytes(range(251)) * (3 * MIB // 251 + 1)
    data = data[: 3 * MIB + 5]
    ranges = []

    def fetch(a, b):
        ranges.append((a, b))
        return data[a:b]

    full, slices = hash_object(fetch, len(data), (MIB, 2 * MIB), workers=3)
    assert ranges == [(0, 3 * MIB + 5)]  # one read of the whole object, not one per slice size
    for size in (MIB, 2 * MIB):
        assert slices[size] == [
            hashlib.sha256(data[i : i + size]).hexdigest() for i in range(0, len(data), size)
        ]
    assert full == hashlib.sha256(data).hexdigest()
