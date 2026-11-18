"""Karpo decision models (Gemma 4 12B, Qwen3.5 4B/9B) over authenticated streaming weights.

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
import json
import resource
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from citypods.judging.glide_stream import fetch_small_objects
from citypods.judging.glide_stream import validate_manifest as _validate
from citypods.weight_streaming import GCSManifest, stream_gcs_safetensors


@dataclass(frozen=True)
class Pin:
    """A pinned checkpoint copy: object prefix, exact weight bytes and how its files are named."""

    prefix: str
    weight_bytes: int  # sum of the model*.safetensors sizes the publisher lists for the revision
    kind: str = "karpo"  # "karpo": decision_config.json and readout.safetensors; "hf": plain model
    strip: str = ""  # prefix of the tensor names in the files that the backbone does not use
    collect: tuple[str, ...] = ()  # tensors outside the backbone to keep, on the device


# Pinned checkpoints. The weight totals are those of the publishers' file listings at each
# revision. The size pin is an aggregate byte count: it stops a manifest from adding or dropping
# weight bytes and replaces a blanket size cap (the 31B is 58 GiB, which no 40 GiB cap would
# admit), but it cannot tell shards apart. Each object is bound by the independently
# authenticated manifest (generation, size and slice hashes), never by this table.
MODELS = {
    "664640316919ce368750b3e16cc263e927ac6e2b": Pin(
        "hf/MachinePulseAI__karpo-decision-12B/664640316919ce368750b3e16cc263e927ac6e2b",
        23_919_544_312,
    ),
    "0fb729874694e9a4928882eefb2cb1aa49b93735": Pin(
        "hf/MachinePulseAI__karpo-decision-9B/0fb729874694e9a4928882eefb2cb1aa49b93735",
        16_785_478_992,
    ),
    "cd6f85bbf3669f66fb3f792cce6498c394b88bea": Pin(
        "karpo/karpo-decision-9B/cd6f85bbf3669f66fb3f792cce6498c394b88bea", 16_785_478_992
    ),
    "4e52f27899229934797e8e13174d9935a786836d": Pin(
        "karpo/karpo-decision-4B/4e52f27899229934797e8e13174d9935a786836d", 9_078_616_112
    ),
    "b8e6fd7463cea8662c556d6257955d7ef9c10d19": Pin(
        "hf/MachinePulseAI__karpo-decision-31B/b8e6fd7463cea8662c556d6257955d7ef9c10d19",
        62_546_329_272,
    ),
    # Plain Hugging Face checkpoints in ForConditionalGeneration layout: weights only (the
    # authors' own runtimes are not run here). No readout and no Karpo decision config.
    "ae28efb0ae759c3494a1078c0a56eda10a9adb38": Pin(
        "hf/Poindexter-Labs__Copernicus-31B/ae28efb0ae759c3494a1078c0a56eda10a9adb38",
        62_546_336_352,
        kind="hf",
        strip="model.",
    ),
    "b3f8a93462fb1051a9de031e94a7ff380e254072": Pin(
        "hf/baselabs__elephant-1/b3f8a93462fb1051a9de031e94a7ff380e254072",
        18_819_722_392,
        kind="hf",
        strip="model.",
        collect=("lm_head.weight",),
    ),
}
KARPO_PREFIXES = {revision: pin.prefix for revision, pin in MODELS.items()}
SLACK = 256 * 1024**2  # room for the small text files
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


def _is_weight(path):
    return path.endswith(".safetensors") and path.rsplit("/", 1)[-1].startswith("model")


def validate_manifest(manifest):
    """Check a trusted manifest for one pinned revision; returns its object paths.

    The weight objects must total exactly the bytes the publisher lists for the revision.
    """
    revision = manifest.get("revision") if isinstance(manifest, dict) else None
    pin = MODELS.get(revision)
    if pin is None:
        raise ValueError("unexpected model revision")
    karpo = pin.kind == "karpo"
    paths = _validate(
        manifest,
        revision=revision,
        prefix=pin.prefix,
        max_total=pin.weight_bytes + SLACK,
        required=_validate_required(karpo),
        is_weight=_is_weight,
    )
    if sum(o["size"] for p, o in paths.items() if _is_weight(p) and p != "readout.safetensors") != (
        pin.weight_bytes
    ):
        raise ValueError("weight objects do not match the pinned size")
    return paths


def _validate_required(karpo):
    return (
        ("decision_config.json", "config.json", "readout.safetensors")
        if karpo
        else ("config.json",)
    )


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


def _binding(paths, manifest, path):
    obj = paths[path]
    return GCSManifest(
        manifest["bucket"],
        obj["name"],
        obj["generation"],
        obj["size"],
        manifest["slice_bytes"],
        tuple(obj["slices"]),
    )


def _stream_backbone(
    manifest, paths, token, directory, model_type, pin, *, fp8, device, workers, stats
):
    """Build the backbone on ``device`` and fill it from every weight shard.

    Returns ``(backbone, shells, extras)``; on failure the sink is discarded first.
    """
    import torch

    from citypods.weight_streaming.torch_sink import TorchSink

    backbone, shells = build_backbone(directory, model_type, torch, fp8=fp8, device=device)
    direct, quantized = _targets(backbone, shells)
    strip = pin.strip
    rename = (lambda n: n[len(strip) :] if strip and n.startswith(strip) else n) if strip else None
    sink = TorchSink(direct, quantized, device=device, collect=pin.collect, rename=rename)
    try:
        weights = sorted(p for p in paths if _is_weight(p) and p != "readout.safetensors")
        started = time.monotonic()
        for path in weights:
            stream_gcs_safetensors(_binding(paths, manifest, path), token, sink, workers=workers)
        sink.check_complete()
        if device.type == "cuda":
            torch.cuda.synchronize(device)
        elapsed = time.monotonic() - started
        if stats is not None:
            size = sum(paths[p]["size"] for p in weights)
            stats.update(
                stream_seconds=round(elapsed, 2),
                stream_bytes=size,
                stream_mb_per_s=round(size / 1e6 / elapsed, 1),
                quantized_tensors=len(quantized),
                direct_tensors=len(direct),
                host_rss_gib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20, 2),
            )
        return backbone, shells, sink.collected()
    except BaseException:
        sink.discard()
        raise


def load_hf_backbone(manifest, token, *, fp8=False, device="cuda", workers=8, stats=None):
    """Stream a pinned plain Hugging Face checkpoint into its backbone; returns (backbone, extras).

    For the checkpoints pinned with ``kind="hf"`` (weights only: their authors' runtimes are not
    run here). ``extras`` holds the tensors outside the backbone the pin asks to keep (such as
    ``lm_head.weight``). The model is discarded on any failure.
    """
    if fp8:
        raise ValueError("plain checkpoints load in BF16 only; FP8 projections are Karpo's")
    paths = validate_manifest(manifest)
    pin = MODELS[manifest["revision"]]
    if pin.kind != "hf":
        raise ValueError("a Karpo decision checkpoint needs load_karpo (readout and hooks)")
    import torch

    device = torch.device(device)
    backbone = extras = result = None
    try:
        with tempfile.TemporaryDirectory() as temp:
            fetch_small_objects(paths, manifest["bucket"], token, temp)
            model_type = json.loads((Path(temp) / "config.json").read_text()).get("model_type")
            if model_type not in BACKBONES:
                raise ValueError("unsupported model type: " + model_type)
            backbone, shells, extras = _stream_backbone(
                manifest,
                paths,
                token,
                temp,
                model_type,
                pin,
                fp8=fp8,
                device=device,
                workers=workers,
                stats=stats,
            )
            del shells  # empty: plain checkpoints are never quantized
            backbone.eval()
            result = backbone, extras
            backbone = extras = None
        return result
    except BaseException as exc:
        traceback.clear_frames(exc.__traceback__)
        backbone = extras = result = None
        raise


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

    from citypods.weight_streaming.torch_sink import CollectSink

    device = torch.device(device)
    pin = MODELS[manifest["revision"]]
    if pin.kind != "karpo":
        raise ValueError("not a Karpo decision checkpoint")
    bucket = manifest["bucket"]

    collect = backbone = decider = result = None
    shells = readout = ckpt = rows = None
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
            stream_gcs_safetensors(
                _binding(paths, manifest, "readout.safetensors"), token, collect, workers=2
            )
            rows = collect.result().get(checkpoints.READOUT_KEY)
            if rows is None or rows.ndim != 2:
                raise ValueError("invalid readout")
            backbone, shells, _ = _stream_backbone(
                manifest,
                paths,
                token,
                temp,
                model_type,
                pin,
                fp8=fp8,
                device=device,
                workers=workers,
                stats=stats,
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
        # The active frame's locals would keep the failed model on the GPU while a caller holds
        # the exception, so every tensor owner is released here.
        decider = backbone = collect = result = None
        shells = readout = ckpt = rows = None
        raise
