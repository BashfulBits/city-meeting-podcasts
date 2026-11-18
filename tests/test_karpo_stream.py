import copy
import functools
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

from citypods.judging import karpo_stream
from citypods.weight_streaming import SliceManifest, stream_safetensors
from tests._lazy import Lazy, require

torch = Lazy("torch")
gemma = Lazy("transformers.models.gemma4_unified.modeling_gemma4_unified")
safetensors_torch = Lazy("safetensors.torch")
torch_sink = Lazy("citypods.weight_streaming.torch_sink")


@pytest.fixture
def need_torch():
    require("torch")


@pytest.fixture
def need_gemma(need_torch):
    require("transformers.models.gemma4_unified.modeling_gemma4_unified")


def TorchSink(*args, **kwargs):  # noqa: N802
    return torch_sink.TorchSink(*args, **kwargs)


def save_file(*args, **kwargs):
    return safetensors_torch.save_file(*args, **kwargs)


FIXTURE = Path(__file__).parent / "fixtures" / "karpo_gemma4_unified_tiny_config.json"


@functools.cache
def refs():
    """Verbatim copies of Karpo's Fp8Linear classes (Apache-2.0, commit 6600011e)."""
    e4m3 = torch.float8_e4m3fn
    e4m3_max = torch.finfo(e4m3).max

    def to_e4m3(x):
        scale = x.abs().amax(-1, keepdim=True).float().clamp(min=1e-12) / e4m3_max
        return (x.float() / scale).clamp(-e4m3_max, e4m3_max).to(e4m3), scale

    class Fp8(torch.nn.Module):  # serve.fp8.Fp8Linear: scale and weight are buffers
        def __init__(self, linear):
            super().__init__()
            weight, scale = to_e4m3(linear.weight.detach())
            self.register_buffer("weight", weight)
            self.register_buffer("scale", scale.t().contiguous())
            self.in_features, self.out_features = linear.in_features, linear.out_features

    class Qwen(torch.nn.Module):  # serve.qwen_fp8.Fp8Linear: Parameters
        def __init__(self, linear):
            super().__init__()
            weight, scale = to_e4m3(linear.weight.detach())
            self.weight = torch.nn.Parameter(weight, requires_grad=False)
            self.scale = torch.nn.Parameter(scale.t().contiguous(), requires_grad=False)
            self.in_features, self.out_features = linear.in_features, linear.out_features

    return types.SimpleNamespace(Fp8=Fp8, Qwen=Qwen)


PROJECTIONS = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")


@pytest.fixture
def ref_module(monkeypatch, need_torch):
    module = types.ModuleType("karpo_fp8_ref")
    module.PROJECTIONS, module.Fp8Linear = PROJECTIONS, refs().Fp8
    monkeypatch.setitem(sys.modules, "karpo_fp8_ref", module)
    monkeypatch.setitem(karpo_stream.FP8_MODULES, "gemma4_unified", "karpo_fp8_ref")
    return module


def layerwise_reference(directory):
    """Karpo's checkpoint.load + layer-by-layer quantize, on CPU."""
    model = gemma.Gemma4UnifiedModel.from_pretrained(
        str(directory), dtype=torch.bfloat16, attn_implementation="sdpa"
    )
    for layer in model.language_model.layers:
        for parent in list(layer.modules()):
            for name, child in list(parent.named_children()):
                if name in PROJECTIONS and isinstance(child, torch.nn.Linear):
                    setattr(parent, name, refs().Fp8(child))
    return model


def stream_file(path, sink, chunk=16384):
    data = Path(path).read_bytes()
    hashes = tuple(
        hashlib.sha256(data[i : i + chunk]).hexdigest() for i in range(0, len(data), chunk)
    )
    manifest = SliceManifest(len(data), chunk, hashes)
    stream_safetensors(manifest, lambda a, b: data[a:b], sink, workers=3)


@pytest.fixture
def tiny(tmp_path, need_gemma):
    from transformers import AutoConfig

    config = AutoConfig.for_model(**json.loads(FIXTURE.read_text()))
    torch.manual_seed(0)
    model = gemma.Gemma4UnifiedModel(config).to(torch.bfloat16)
    # Module-format names, as in the published Karpo 12B files (no key renaming on load).
    config.save_pretrained(str(tmp_path))
    save_file(
        {k: v.contiguous() for k, v in model.state_dict().items()},
        str(tmp_path / "model.safetensors"),
    )
    return tmp_path


def digest(tensor):
    return hashlib.sha256(
        tensor.detach().contiguous().reshape(-1).view(torch.uint8).numpy().tobytes()
    ).hexdigest()


def test_streamed_backbone_is_byte_identical_to_layerwise_quantized_load(tiny, ref_module):
    reference = layerwise_reference(tiny)
    backbone, shells = karpo_stream.build_backbone(
        tiny, "gemma4_unified", torch, fp8=True, device=torch.device("cpu")
    )
    assert len(shells) == sum(1 for _, m in reference.named_modules() if isinstance(m, refs().Fp8))
    direct, quantized = karpo_stream._targets(backbone, shells)
    sink = TorchSink(direct, quantized)
    stream_file(next(tiny.glob("*.safetensors")), sink)
    sink.check_complete()
    expected, actual = reference.state_dict(), backbone.state_dict()
    assert expected.keys() == actual.keys()
    for name in expected:
        assert expected[name].dtype == actual[name].dtype, name
        assert digest(expected[name]) == digest(actual[name]), name
    ref_buffers = dict(reference.named_buffers())
    new_buffers = dict(backbone.named_buffers())
    assert ref_buffers.keys() == new_buffers.keys()
    for name, value in ref_buffers.items():  # includes the non-persistent rotary tables
        assert digest(value) == digest(new_buffers[name]), name


def test_without_fp8_every_weight_is_copied_unchanged(tiny, ref_module):
    reference = gemma.Gemma4UnifiedModel.from_pretrained(
        str(tiny), dtype=torch.bfloat16, attn_implementation="sdpa"
    )
    backbone, shells = karpo_stream.build_backbone(
        tiny, "gemma4_unified", torch, fp8=False, device=torch.device("cpu")
    )
    assert shells == {}
    direct, quantized = karpo_stream._targets(backbone, shells)
    assert quantized == {}
    sink = TorchSink(direct)
    stream_file(next(tiny.glob("*.safetensors")), sink)
    sink.check_complete()
    for name, value in reference.state_dict().items():
        assert digest(value) == digest(backbone.state_dict()[name]), name


def test_missing_tensor_is_detected(tiny, ref_module):
    backbone, shells = karpo_stream.build_backbone(
        tiny, "gemma4_unified", torch, fp8=True, device=torch.device("cpu")
    )
    direct, quantized = karpo_stream._targets(backbone, shells)
    direct["extra.weight"] = torch.empty(1)
    sink = TorchSink(direct, quantized)
    stream_file(next(tiny.glob("*.safetensors")), sink)
    with pytest.raises(ValueError, match="incomplete"):
        sink.check_complete()


def test_manifest_requires_a_pinned_revision():
    with pytest.raises(ValueError, match="revision"):
        karpo_stream.validate_manifest({"revision": "x" * 40})
    assert copy.copy(karpo_stream.KARPO_PREFIXES)


# ----------------------------------------------------------------------------- Qwen3.5

QWEN_FIXTURE = Path(__file__).parent / "fixtures" / "karpo_qwen3_5_tiny_config.json"


def test_qwen_streamed_backbone_matches_layerwise_load(tmp_path, monkeypatch, need_torch):
    qwen = pytest.importorskip("transformers.models.qwen3_5.modeling_qwen3_5")
    from transformers import AutoConfig

    min_features = 16  # the tiny model has no 256-wide projection; the rule is the same
    module = types.ModuleType("karpo_qwen_fp8_ref")
    module.MIN_FEATURES, module.Fp8Linear = min_features, refs().Qwen
    monkeypatch.setitem(sys.modules, "karpo_qwen_fp8_ref", module)
    monkeypatch.setitem(karpo_stream.FP8_MODULES, "qwen3_5", "karpo_qwen_fp8_ref")
    config = AutoConfig.for_model(**json.loads(QWEN_FIXTURE.read_text()))
    torch.manual_seed(2)
    model = qwen.Qwen3_5Model(config).to(torch.bfloat16)
    config.save_pretrained(str(tmp_path))
    save_file(
        {k: v.contiguous() for k, v in model.state_dict().items()},
        str(tmp_path / "model.safetensors"),
    )
    reference = qwen.Qwen3_5Model.from_pretrained(
        str(tmp_path), dtype=torch.bfloat16, attn_implementation="sdpa"
    )
    for layer in reference.language_model.layers:
        for parent in list(layer.modules()):
            for name, child in list(parent.named_children()):
                if isinstance(child, torch.nn.Linear) and child.out_features >= min_features:
                    setattr(parent, name, refs().Qwen(child))
    backbone, shells = karpo_stream.build_backbone(
        tmp_path, "qwen3_5", torch, fp8=True, device=torch.device("cpu")
    )
    assert shells and any(n.endswith("linear_attn.in_proj_qkv.weight") for n in shells)
    direct, quantized = karpo_stream._targets(backbone, shells)
    sink = TorchSink(direct, quantized)
    stream_file(tmp_path / "model.safetensors", sink)
    sink.check_complete()
    expected = reference.state_dict()
    assert expected.keys() == backbone.state_dict().keys()
    for name, value in expected.items():
        assert digest(value) == digest(backbone.state_dict()[name]), name
    for name, value in reference.named_buffers():
        assert digest(value) == digest(dict(backbone.named_buffers())[name]), name


# ------------------------------------------------------------ load_karpo orchestration (mocked)


def install_fake_karpo(monkeypatch, directory_holder, *, backbone_path=None):
    """Stand-ins for karpo_decision (not importable with the local torch) and for the network."""
    modules = {}

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        modules[name] = mod
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    class Config:
        backbone = backbone_path
        max_pixels = 1
        codes = ("A", "B")
        token_ids = (7, 8)
        readout_dtype = "float32"

        @classmethod
        def load(cls, directory):
            return cls()

    class Tokenizer:
        def encode(self, code, add_special_tokens=False):
            return [{"A": 7, "B": 8}[code]]

    family = types.SimpleNamespace(
        load_processor=lambda d, p: types.SimpleNamespace(tokenizer=Tokenizer()),
        logit_cap=lambda backbone: None,
    )

    class Readout:
        @classmethod
        def from_rows(cls, rows, dtype, cap):
            obj = cls()
            obj.rows = rows
            return obj

        def to(self, device):
            return self

    class Decider:
        def __init__(self, ckpt, **kwargs):
            self.ckpt = ckpt
            self.model = types.SimpleNamespace()

    module("karpo_decision")
    module(
        "karpo_decision.checkpoint",
        READOUT_KEY="weight",
        _dtype=lambda n: torch.float32,
        Checkpoint=lambda *a: types.SimpleNamespace(backbone=a[2], readout=a[3]),
    )
    module("karpo_decision.families", of=lambda d: family, model_type=lambda d: "gemma4_unified")
    module("karpo_decision.config", DecisionConfig=Config)
    module("karpo_decision.decider", Decider=Decider)
    module("karpo_decision.model")
    module("karpo_decision.model.readout", Readout=Readout)
    return modules


@pytest.fixture
def mocked_load(tiny, monkeypatch, ref_module):
    import shutil

    install_fake_karpo(monkeypatch, None)
    rows = torch.randn(2, 64)
    objects = {"readout.safetensors": None}
    names = {"model-00001-of-00001.safetensors": next(tiny.glob("*.safetensors"))}
    manifest = {
        "revision": "664640316919ce368750b3e16cc263e927ac6e2b",
        "bucket": "b",
        "slice_bytes": 16384,
    }
    paths = {
        p: {"name": p, "generation": "1", "size": 1, "slices": ["0" * 64]}
        for p in (*objects, *names)
    }
    monkeypatch.setattr(karpo_stream, "validate_manifest", lambda m: paths)

    def fetch_small(paths_, bucket, token, directory):
        shutil.copy(tiny / "config.json", Path(directory) / "config.json")

    monkeypatch.setattr(karpo_stream, "fetch_small_objects", fetch_small)
    from safetensors.torch import save_file as save

    readout = tiny.parent / "readout.safetensors"
    save({"weight": rows}, str(readout))
    sources = {"readout.safetensors": readout, **names}
    calls = []

    def fake_stream(binding, token, sink, workers=8):
        calls.append(binding.name)
        stream_file(sources[binding.name], sink)

    monkeypatch.setattr(karpo_stream, "stream_gcs_safetensors", fake_stream)
    monkeypatch.setattr(
        karpo_stream, "GCSManifest", lambda b, n, g, s, c, h: types.SimpleNamespace(name=n)
    )
    return manifest, rows, calls


def test_load_karpo_streams_everything_and_installs_fp8_hooks(mocked_load, monkeypatch):
    manifest, rows, calls = mocked_load
    fp8 = types.ModuleType("karpo_fp8_ref")
    fp8.PROJECTIONS, fp8.Fp8Linear = PROJECTIONS, refs().Fp8
    fp8.norm, fp8.mlp = object(), object()
    monkeypatch.setitem(sys.modules, "karpo_fp8_ref", fp8)
    stats = {}
    decider = karpo_stream.load_karpo(manifest, "token", device="cpu", stats=stats)
    assert calls == ["readout.safetensors", "model-00001-of-00001.safetensors"]
    assert decider.model.norm_input is fp8.norm and decider.model.mlp is fp8.mlp
    assert decider.ckpt.readout.rows.equal(rows)
    assert stats["quantized_tensors"] > 0 and stats["stream_seconds"] >= 0
    assert any(isinstance(m, refs().Fp8) for m in decider.ckpt.backbone.modules())


def test_load_karpo_refuses_external_backbone_path(mocked_load, monkeypatch):
    manifest, _, calls = mocked_load
    install_fake_karpo(monkeypatch, None, backbone_path="/etc")
    with pytest.raises(ValueError, match="external backbone"):
        karpo_stream.load_karpo(manifest, "token", device="cpu")
    assert calls == []


def test_load_karpo_rejects_code_that_does_not_match_the_tokenizer(mocked_load, monkeypatch):
    manifest, _, calls = mocked_load
    modules = install_fake_karpo(monkeypatch, None)
    modules["karpo_decision.config"].DecisionConfig.token_ids = (7, 9)
    with pytest.raises(ValueError, match="tokenizer"):
        karpo_stream.load_karpo(manifest, "token", device="cpu")
    assert calls == []


def test_load_karpo_discards_a_partial_write_and_frees_the_model_while_the_error_is_held(
    mocked_load, monkeypatch
):
    import gc
    import weakref

    from citypods.weight_streaming import TensorSpec, torch_sink

    manifest, _, _ = mocked_load
    staged = []
    original = torch_sink.TorchSink.discard

    def tracking(self):
        staged.append(len(self._staging))  # a partly written FP8 weight is staged at this point
        original(self)
        staged.append(len(self._staging))

    monkeypatch.setattr(torch_sink.TorchSink, "discard", tracking)
    backbones, tensors = [], []
    build = karpo_stream.build_backbone

    def recording_build(*args, **kwargs):
        backbone, shells = build(*args, **kwargs)
        backbones.append(weakref.ref(backbone))
        return backbone, shells

    monkeypatch.setattr(karpo_stream, "build_backbone", recording_build)

    def fake_stream(binding, token, sink, workers=8):
        if binding.name == "readout.safetensors":
            spec = types.SimpleNamespace(name="weight", dtype="F32", shape=(2, 64), size=512)
            sink(spec, 0, memoryview(bytes(512)))
            return
        name, (weight, _) = next(iter(sink.quantized.items()))
        tensors.append(weakref.ref(weight))  # a GPU-resident FP8 weight
        tensors.append(weakref.ref(next(iter(sink.direct.values()))))
        size = weight.numel() * 2
        sink(TensorSpec(name, "BF16", tuple(weight.shape), size), 0, memoryview(bytes(16)))
        raise ValueError("SHA-256 mismatch")

    monkeypatch.setattr(karpo_stream, "stream_gcs_safetensors", fake_stream)
    with pytest.raises(ValueError, match="SHA-256") as held:  # the caller keeps the exception
        karpo_stream.load_karpo(manifest, "token", device="cpu")
    assert staged == [1, 0]
    gc.collect()
    assert backbones and backbones[0]() is None, "the failed model is still referenced"
    assert len(tensors) == 2 and all(t() is None for t in tensors), "weights are still referenced"
    assert held.value is not None
