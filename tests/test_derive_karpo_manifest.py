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
        "model-00001-of-00001.safetensors": hashlib.sha256(
            files["model-00001-of-00001.safetensors"]
        ).hexdigest()
    }
    result = derive_from(files, expected)
    manifest = result[MIB]
    names = [o["name"] for o in manifest["objects"]]
    assert all("README" not in n and "assets" not in n for n in names)
    assert sorted(manifest["unreferenced"]) == [
        "config.json",
        "decision_config.json",
        "readout.safetensors",
    ]
    assert len(validate_manifest(manifest, revision=REVISION, prefix=PREFIX)) == 4
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        derive_from(files, {"model-00001-of-00001.safetensors": "0" * 64})


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
        "model-00002-of-00002.safetensors": 13 * 1024**3,
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
