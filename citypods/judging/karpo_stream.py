"""Karpo decision models (Gemma 4 12B/31B, Qwen3.5 4B/9B) over authenticated streaming weights.

The backbone is built on the meta device, its parameters are allocated on the GPU, and verified
safetensors slices are copied into them. With ``fp8`` every decoder projection that Karpo's own
``--fp8`` path would quantize is created as that path's ``Fp8Linear`` and filled by quantizing each
BF16 weight (one scale per output row, e4m3) the moment it is complete. The BF16 model is never
resident, so the 12B fits a 22 GiB L4 and the host holds only the in-flight slices.

The manifest must be authenticated independently (see ``citypods.weight_streaming``). Model code
(Karpo, Apache-2.0, audited at the pinned commit) and the manifest are trusted; remote bytes are
used only after SHA-256 verification. A checkpoint whose ``decision_config.json`` names a separate
``backbone`` path is refused, because that is a filesystem path read from remote content.
"""

import importlib
import resource
import tempfile
import time
import traceback
from pathlib import Path

from citypods.judging.glide_stream import fetch_small_objects
from citypods.judging.glide_stream import validate_manifest as _validate
from citypods.weight_streaming import GCSManifest, stream_gcs_safetensors

# Pinned checkpoints: model revision -> object prefix in the model bucket.
KARPO_PREFIXES = {
    "664640316919ce368750b3e16cc263e927ac6e2b": (
        "hf/MachinePulseAI__karpo-decision-12B/664640316919ce368750b3e16cc263e927ac6e2b"
    ),
    "0fb729874694e9a4928882eefb2cb1aa49b93735": (
        "hf/MachinePulseAI__karpo-decision-9B/0fb729874694e9a4928882eefb2cb1aa49b93735"
    ),
    "cd6f85bbf3669f66fb3f792cce6498c394b88bea": (
        "karpo/karpo-decision-9B/cd6f85bbf3669f66fb3f792cce6498c394b88bea"
    ),
    "4e52f27899229934797e8e13174d9935a786836d": (
        "karpo/karpo-decision-4B/4e52f27899229934797e8e13174d9935a786836d"
    ),
}
MAX_TOTAL_BYTES = 40 * 1024**3
# Backbone class per model_type (the classes Karpo's families load with from_pretrained).
BACKBONES = {
    "gemma4": ("transformers.models.gemma4.modeling_gemma4", "Gemma4Model"),
    "gemma4_unified": (
        "transformers.models.gemma4_unified.modeling_gemma4_unified",
        "Gemma4UnifiedModel",
    ),
    "qwen3_5": ("transformers.models.qwen3_5.modeling_qwen3_5", "Qwen3_5Model"),
}
FP8_MODULES = {
    "gemma4": "karpo_decision.serve.fp8",
    "gemma4_unified": "karpo_decision.serve.fp8",
    "qwen3_5": "karpo_decision.serve.qwen_fp8",
}


def validate_manifest(manifest):
    """Check a trusted manifest for one pinned Karpo revision; returns its object paths."""
    revision = manifest.get("revision") if isinstance(manifest, dict) else None
    if revision not in KARPO_PREFIXES:
        raise ValueError("unexpected model revision")
    paths = _validate(
        manifest, revision=revision, prefix=KARPO_PREFIXES[revision], max_total=MAX_TOTAL_BYTES
    )
    return paths


def _wants_fp8(model_type, name, child, torch, fp8_module):
    """Karpo's own selection: which children of a decoder layer ``quantize`` replaces."""
    if not isinstance(child, torch.nn.Linear):
        return False
    if model_type == "qwen3_5":
        return child.out_features >= fp8_module.MIN_FEATURES
    return name in fp8_module.PROJECTIONS


def _fp8_shells(backbone, model_type, torch):
    """Replace decoder projections with meta ``Fp8Linear`` shells; returns {weight name: shell}."""
    fp8_module = importlib.import_module(FP8_MODULES[model_type])
    shells = {}
    for index, layer in enumerate(backbone.language_model.layers):
        for path, parent in list(layer.named_modules()):
            for name, child in list(parent.named_children()):
                if _wants_fp8(model_type, name, child, torch, fp8_module):
                    shell = fp8_module.Fp8Linear(child)
                    setattr(parent, name, shell)
                    prefix = f"language_model.layers.{index}" + (f".{path}" if path else "")
                    shells[f"{prefix}.{name}.weight"] = shell
    return shells


def build_backbone(directory, model_type, torch, *, fp8, device):
    """The backbone on ``device`` with uninitialised-but-restored buffers, plus its FP8 shells."""
    from transformers import AutoConfig

    module, class_name = BACKBONES[model_type]
    config = AutoConfig.from_pretrained(directory, local_files_only=True)
    with torch.device("meta"):
        backbone = getattr(importlib.import_module(module), class_name)._from_config(
            config, dtype=torch.bfloat16, attn_implementation="sdpa"
        )
    shells = _fp8_shells(backbone, model_type, torch) if fp8 else {}
    backbone.to_empty(device=device)
    # Non-persistent buffers (rotary tables, embedding scales) are not in the checkpoint;
    # transformers restores them through its own initialisation, as from_pretrained does.
    for module_ in backbone.modules():
        module_._is_hf_initialized = False
    with torch.no_grad():
        backbone.initialize_weights()
    backbone.requires_grad_(False)
    return backbone, shells


def _targets(backbone, shells):
    skip = set()
    quantized = {}
    for name, shell in shells.items():
        skip.update({name, name[: -len("weight")] + "scale"})
        quantized[name] = (shell.weight, shell.scale)
    direct = {n: t for n, t in backbone.state_dict().items() if n not in skip}
    return direct, quantized


def load_karpo(
    manifest,
    token,
    *,
    fp8=True,
    device="cuda",
    workers=8,
    max_length=None,
    token_budget=None,
    stats=None,
):
    """Build a ``karpo_decision.decider.Decider`` by streaming; discard everything on any failure.

    ``stats``, when a dict, receives the stream seconds, bytes and throughput.
    """
    paths = validate_manifest(manifest)
    import torch
    from karpo_decision import checkpoint as checkpoints
    from karpo_decision import families
    from karpo_decision.config import DecisionConfig
    from karpo_decision.decider import Decider
    from karpo_decision.model.readout import Readout

    from citypods.weight_streaming.torch_sink import CollectSink, TorchSink

    device = torch.device(device)
    bucket, chunk = manifest["bucket"], manifest["slice_bytes"]

    def binding(path):
        obj = paths[path]
        return GCSManifest(
            bucket, obj["name"], obj["generation"], obj["size"], chunk, tuple(obj["slices"])
        )

    sink = collect = backbone = decider = None
    try:
        with tempfile.TemporaryDirectory() as temp:
            fetch_small_objects(paths, bucket, token, temp)
            config = DecisionConfig.load(temp)
            if config.backbone:
                raise ValueError("checkpoint names an external backbone path")
            family = families.of(temp)
            model_type = families.model_type(temp)
            if model_type not in BACKBONES:
                raise ValueError("unsupported model type: " + model_type)
            processor = family.load_processor(temp, config.max_pixels)
            for code, token_id in zip(config.codes, config.token_ids, strict=True):
                if processor.tokenizer.encode(code, add_special_tokens=False) != [token_id]:
                    raise ValueError("answer code does not match the tokenizer")
            collect = CollectSink()
            stream_gcs_safetensors(binding("readout.safetensors"), token, collect, workers=2)
            rows = collect.result().get(checkpoints.READOUT_KEY)
            if rows is None or rows.ndim != 2:
                raise ValueError("invalid readout")
            backbone, shells = build_backbone(temp, model_type, torch, fp8=fp8, device=device)
            direct, quantized = _targets(backbone, shells)
            sink = TorchSink(direct, quantized, device=device)
            weights = sorted(
                p for p in paths if p.startswith("model-") and p.endswith(".safetensors")
            )
            started = time.monotonic()
            for path in weights:
                stream_gcs_safetensors(binding(path), token, sink, workers=workers)
            sink.check_complete()
            if device.type == "cuda":
                torch.cuda.synchronize()
            elapsed = time.monotonic() - started
            if stats is not None:
                size = sum(paths[p]["size"] for p in weights)
                stats.update(
                    stream_seconds=round(elapsed, 2),
                    stream_bytes=size,
                    stream_mb_per_s=round(size / 1e6 / elapsed, 1),
                    quantized_tensors=len(quantized),
                    direct_tensors=len(direct),
                    host_rss_gib=round(
                        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 2
                    ),
                )
            backbone.eval()
            readout = Readout.from_rows(
                rows, checkpoints._dtype(config.readout_dtype), family.logit_cap(backbone)
            ).to(device)
            ckpt = checkpoints.Checkpoint(config, processor, backbone, readout, Path(temp), family)
            kwargs = {
                k: v for k, v in (("max_length", max_length), ("token_budget", token_budget)) if v
            }
            decider = Decider(ckpt, **kwargs)
            if shells and model_type != "qwen3_5":
                fp8_module = importlib.import_module(FP8_MODULES[model_type])
                # The same hooks Karpo's quantize() installs: each activation quantised once.
                decider.model.norm_input, decider.model.mlp = fp8_module.norm, fp8_module.mlp
            result, decider = decider, None
            return result
    except BaseException as exc:
        # Inner frames can retain GPU tensors through the traceback; release them explicitly.
        traceback.clear_frames(exc.__traceback__)
        if sink is not None:
            sink.discard()
        decider = backbone = sink = None
        raise
