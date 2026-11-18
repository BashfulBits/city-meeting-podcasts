import hashlib
import json
import struct

import pytest

from citypods.weight_streaming import SliceManifest, TensorSpec, stream_safetensors
from tests._lazy import Lazy, require

torch = Lazy("torch")
torch_sink = Lazy("citypods.weight_streaming.torch_sink")


@pytest.fixture(autouse=True)
def _need_torch():
    require("torch")


def TorchSink(*args, **kwargs):  # noqa: N802
    return torch_sink.TorchSink(*args, **kwargs)


def CollectSink(*args, **kwargs):  # noqa: N802
    return torch_sink.CollectSink(*args, **kwargs)


def quantize_rows_e4m3(x):
    return torch_sink.quantize_rows_e4m3(x)


def reference_to_e4m3(x):
    """Verbatim from karpo_decision.serve.fp8._to_e4m3 (Apache-2.0), commit 6600011e."""
    e4m3_max = torch.finfo(torch.float8_e4m3fn).max
    scale = x.abs().amax(-1, keepdim=True).float().clamp(min=1e-12) / e4m3_max
    return (x.float() / scale).clamp(-e4m3_max, e4m3_max).to(torch.float8_e4m3fn), scale


def safetensors_bytes(tensors):
    header, payload = {}, b""
    for name, tensor in tensors.items():
        raw = tensor.contiguous().view(torch.uint8).view(-1).numpy().tobytes()
        dtype = {torch.bfloat16: "BF16", torch.float32: "F32"}[tensor.dtype]
        header[name] = {
            "dtype": dtype,
            "shape": list(tensor.shape),
            "data_offsets": [len(payload), len(payload) + len(raw)],
        }
        payload += raw
    encoded = json.dumps(header).encode()
    return struct.pack("<Q", len(encoded)) + encoded + payload


def stream(data, sink, chunk=128):
    manifest = SliceManifest(
        len(data),
        chunk,
        tuple(hashlib.sha256(data[i : i + chunk]).hexdigest() for i in range(0, len(data), chunk)),
    )
    stream_safetensors(manifest, lambda a, b: data[a:b], sink, workers=2)


def spec(name, tensor, dtype="BF16"):
    return TensorSpec(name, dtype, tuple(tensor.shape), tensor.numel() * tensor.element_size())


def test_quantizer_matches_author_formula_bytes():
    torch.manual_seed(0)
    x = torch.randn(33, 70, dtype=torch.bfloat16) * 3
    x[4] = 0  # an all-zero row exercises the scale clamp
    x[5, 0] = 1e4  # and a large outlier
    values, scale = quantize_rows_e4m3(x)
    ref_values, ref_scale = reference_to_e4m3(x)
    assert values.view(torch.uint8).equal(ref_values.view(torch.uint8))
    assert scale.equal(ref_scale)


def test_streamed_fp8_and_direct_tensors_are_byte_identical_to_layerwise_reference():
    torch.manual_seed(1)
    weight = torch.randn(48, 40, dtype=torch.bfloat16)
    norm = torch.randn(40, dtype=torch.bfloat16)
    gate = torch.randn(9, dtype=torch.float32)
    data = safetensors_bytes({"proj.weight": weight, "norm.weight": norm, "gate": gate})
    dest_w, dest_s = torch.zeros(48, 40, dtype=torch.float8_e4m3fn), torch.zeros(1, 48)
    direct = {"norm.weight": torch.empty(40, dtype=torch.bfloat16), "gate": torch.empty(9)}
    sink = TorchSink(direct, {"proj.weight": (dest_w, dest_s)})
    stream(data, sink, chunk=311)  # header fits the first slice; later cuts split tensors
    sink.check_complete()
    ref_values, ref_scale = reference_to_e4m3(weight)
    assert dest_w.view(torch.uint8).equal(ref_values.view(torch.uint8))
    assert dest_s.equal(ref_scale.t().contiguous())
    assert direct["norm.weight"].equal(norm) and direct["gate"].equal(gate)
    assert not sink._staging  # no BF16 weight stays resident


def test_sink_rejects_mismatches_and_incomplete_coverage():
    w = torch.randn(4, 8, dtype=torch.bfloat16)
    good = {"w": torch.empty(4, 8, dtype=torch.bfloat16)}
    sink = TorchSink(good)
    with pytest.raises(ValueError, match="unexpected"):
        sink(spec("other", w), 0, memoryview(b""))
    with pytest.raises(ValueError, match="mismatch"):
        sink(spec("w", w, "F32"), 0, memoryview(bytes(64)))
    with pytest.raises(ValueError, match="mismatch"):
        sink(TensorSpec("w", "BF16", (8, 4), 64), 0, memoryview(bytes(64)))
    with pytest.raises(ValueError, match="invalid segment"):
        sink(spec("w", w), 8, memoryview(bytes(8)))
    sink(spec("w", w), 0, memoryview(bytes(64)))
    with pytest.raises(ValueError, match="invalid segment"):
        sink(spec("w", w), 0, memoryview(bytes(64)))  # duplicate tensor
    sink.check_complete()
    partial = TorchSink({"w": torch.empty(4, 8, dtype=torch.bfloat16)})
    partial(spec("w", w), 0, memoryview(bytes(32)))
    with pytest.raises(ValueError, match="incomplete"):
        partial.check_complete()
    partial.discard()
    assert partial.direct == {}


def test_quantized_tensor_left_staged_is_not_complete_and_destination_is_validated():
    w = torch.randn(4, 8, dtype=torch.bfloat16)
    dest = (torch.empty(4, 8, dtype=torch.float8_e4m3fn), torch.empty(1, 4))
    sink = TorchSink({}, {"w": dest})
    sink(spec("w", w), 0, memoryview(bytes(32)))
    with pytest.raises(ValueError, match="incomplete"):
        sink.check_complete()
    with pytest.raises(ValueError, match="invalid FP8"):
        TorchSink({}, {"w": (torch.empty(4, 8, dtype=torch.bfloat16), dest[1])})
    with pytest.raises(ValueError, match="both"):
        TorchSink({"w": dest[0]}, {"w": dest})


def test_ignore_prefixes_and_collect_sink():
    w = torch.randn(2, 2, dtype=torch.bfloat16)
    sink = TorchSink({}, ignore_prefixes=("visual.",))
    sink(spec("visual.a", w), 0, memoryview(bytes(8)))
    assert sink.ignored == {"visual.a"}
    rows = torch.randn(3, 5)
    collect = CollectSink()
    stream(safetensors_bytes({"weight": rows}), collect, chunk=128)
    assert collect.result()["weight"].equal(rows)
    strict = CollectSink({"weight": ("F32", (3, 6))})
    with pytest.raises(ValueError, match="unexpected"):
        stream(safetensors_bytes({"weight": rows}), strict)


def test_collect_sink_requires_every_expected_tensor_and_stays_on_cpu():
    rows = torch.randn(3, 5)
    expected = {"weight": ("F32", (3, 5)), "bias": ("F32", (3, 5))}
    collect = CollectSink(expected)
    stream(safetensors_bytes({"weight": rows}), collect)
    with pytest.raises(ValueError, match="missing"):
        collect.result()
    assert collect.tensors["weight"].device.type == "cpu"
    complete = CollectSink({"weight": ("F32", (3, 5))})
    stream(safetensors_bytes({"weight": rows}), complete)
    assert complete.result()["weight"].equal(rows)
