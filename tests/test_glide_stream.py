import hashlib
from types import SimpleNamespace

import pytest

from citypods.judging.glide_stream import REVISION, TensorSink, validate_manifest
from citypods.weight_streaming import TensorSpec
from scripts.derive_glide_slices import derive

BUCKET = "example-model-bucket"


def source_manifest(files):
    return {
        "bucket": BUCKET,
        "location": "US-EAST4",
        "revision": REVISION,
        "objects": [
            {
                "name": f"{REVISION}/{name}",
                "generation": "123",
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in files.items()
        ],
    }


def test_derive_verifies_complete_object_and_preserves_identity(tmp_path):
    files = {
        "config.json": b"123456789",
        "decision_config.json": b"x",
        "readout.safetensors": b"y",
        "model-01.safetensors": b"z",
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    original = source_manifest(files)
    result = derive(original, tmp_path, chunk_size=8)
    assert len(result["objects"][0]["slices"]) == 2
    assert result["objects"][0]["generation"] == "123"
    assert len(validate_manifest(result)) == len(files)
    (tmp_path / "config.json").write_bytes(b"incorrect")
    with pytest.raises(ValueError, match="SHA-256"):
        derive(original, tmp_path, chunk_size=8)


def test_manifest_rejects_duplicate_and_unsafe_paths(tmp_path):
    files = {
        "config.json": b"a",
        "decision_config.json": b"b",
        "readout.safetensors": b"c",
        "model-01.safetensors": b"d",
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    manifest = derive(source_manifest(files), tmp_path, chunk_size=8)
    manifest["objects"].append(manifest["objects"][0])
    with pytest.raises(ValueError, match="duplicate"):
        validate_manifest(manifest)
    manifest["objects"].pop()
    manifest["objects"][0]["name"] = REVISION + "/../bad"
    with pytest.raises(ValueError, match="unsafe"):
        validate_manifest(manifest)


def test_sink_checks_missing_and_duplicate_zero_sized_tensors(monkeypatch):
    dtype = object()
    monkeypatch.setitem(__import__("sys").modules, "torch", SimpleNamespace(bfloat16=dtype))
    parameter = SimpleNamespace(
        shape=(0,), dtype=dtype, is_contiguous=lambda: True, numel=lambda: 0
    )
    sink = TensorSink({"empty": parameter})
    with pytest.raises(ValueError, match="missing"):
        sink.check_complete()
    spec = TensorSpec("empty", "BF16", (0,), 0)
    sink(spec, 0, memoryview(b""))
    sink.check_complete()
    with pytest.raises(ValueError, match="segment"):
        sink(spec, 0, memoryview(b""))


def test_manifest_supports_other_trusted_bucket(tmp_path):
    files = {
        "config.json": b"a",
        "decision_config.json": b"b",
        "readout.safetensors": b"c",
        "model-01.safetensors": b"d",
    }
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    source = source_manifest(files)
    source["bucket"] = "other-model-bucket"
    source["location"] = "EUROPE-WEST1"
    result = derive(source, tmp_path, chunk_size=8)
    assert result["bucket"] == "other-model-bucket"
    assert len(validate_manifest(result)) == 4
    result["bucket"] = "../../invalid"
    with pytest.raises(ValueError, match="bucket"):
        validate_manifest(result)


def test_cgroup_memory_reads_peak_and_fallback(tmp_path):
    from citypods.judging.cgroup_memory import CgroupMemory

    (tmp_path / "memory.current").write_text("42")
    (tmp_path / "memory.max").write_text("100")
    meter = CgroupMemory(tmp_path)
    assert meter.snapshot()["peak_kind"] == "sampled"
    (tmp_path / "memory.peak").write_text("90")
    assert meter.snapshot()["peak_bytes"] == 90
    assert meter.snapshot()["peak_kind"] == "kernel"


def test_sink_copies_segments_at_odd_byte_boundaries():
    torch = pytest.importorskip("torch")
    parameter = torch.empty(2, dtype=torch.bfloat16)
    sink = TensorSink({"x": parameter})
    spec = TensorSpec("x", "BF16", (2,), 4)
    with torch.no_grad():
        sink(spec, 0, memoryview(bytearray(b"a")))
        sink(spec, 1, memoryview(bytearray(b"bc")))
        sink(spec, 3, memoryview(bytearray(b"d")))
    sink.check_complete()
    assert bytes(parameter.view(torch.uint8).tolist()) == b"abcd"


def test_failed_load_releases_parameters_even_if_exception_retained(monkeypatch):
    import sys
    import weakref

    torch = pytest.importorskip("torch")
    from citypods.judging import glide_stream

    references = []

    class Net:
        def to(self, dtype):
            return self

        def to_empty(self, device):
            weight = torch.empty(1, dtype=torch.bfloat16, device="cpu")
            references.append(weakref.ref(weight))
            self.backbone = SimpleNamespace(named_parameters=lambda: [("x", weight)])
            self.readout = SimpleNamespace(weight=weight)
            return self

    model = SimpleNamespace(
        DEFAULT_NAME="glide",
        LM_PREFIX="",
        Glide=object,
        _Network=lambda *args: Net(),
        _check_codes=lambda *args: None,
        _restore_rotary=lambda *args: None,
    )
    transformers = SimpleNamespace(
        AutoTokenizer=SimpleNamespace(from_pretrained=lambda *args, **kwargs: object()),
        AutoConfig=SimpleNamespace(from_pretrained=lambda *args, **kwargs: SimpleNamespace()),
    )
    monkeypatch.setitem(sys.modules, "glide", SimpleNamespace(model=model))
    monkeypatch.setitem(sys.modules, "glide.model", model)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    data = b"{}"
    obj = {
        "name": "x",
        "generation": "1",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "slices": ["0" * 64],
    }
    paths = {
        "config.json": obj,
        "decision_config.json": obj,
        "model-01.safetensors": obj,
        "readout.safetensors": obj,
    }
    monkeypatch.setattr(glide_stream, "validate_manifest", lambda manifest: paths)
    monkeypatch.setattr(glide_stream, "gcs_range_fetcher", lambda *args: lambda a, b: data)

    def fail(binding, token, sink, **kwargs):
        parameter = sink.parameters["x"]
        assert parameter is references[0]()
        raise RuntimeError("injected failure")

    monkeypatch.setattr(glide_stream, "stream_gcs_safetensors", fail)
    with pytest.raises(RuntimeError, match="injected failure") as captured:
        glide_stream.load_glide({"slice_bytes": 8, "bucket": "test-bucket"}, "token", {})
    assert captured.value.__traceback__ is not None
    assert references[0]() is None


def test_cgroup_sampled_peak_survives_concurrent_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from citypods.judging.cgroup_memory import CgroupMemory

    (tmp_path / "memory.current").write_text("1")
    meter = CgroupMemory(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(meter._record, range(1, 1001)))
    assert meter.snapshot()["peak_bytes"] == 1000
