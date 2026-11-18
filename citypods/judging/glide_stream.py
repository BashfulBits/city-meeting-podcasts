"""GLiDE's text-only model adapter for authenticated streaming weights.

The supplied manifest must be authenticated independently. Model code and the
manifest are trusted; the remote objects are only accepted after hash validation.
"""

import hashlib
import json
import re
import tempfile
import traceback
from pathlib import Path

from citypods.weight_streaming import GCSManifest, gcs_range_fetcher, stream_gcs_safetensors

REVISION = "ab54df5acba0eebf7df45560ffdaacd64e32df3b"
MAX_CONFIG_BYTES = 32 * 1024**2
MAX_OBJECTS = 1000
MAX_TOTAL_BYTES = 20 * 1024**3


REQUIRED_FILES = ("decision_config.json", "config.json", "readout.safetensors")


def sharded_weight(path):
    return path.startswith("model-") and path.endswith(".safetensors")


def validate_manifest(
    manifest,
    *,
    revision=REVISION,
    prefix=None,
    max_total=MAX_TOTAL_BYTES,
    required=REQUIRED_FILES,
    is_weight=sharded_weight,
):
    """Validate a trusted object manifest; ``prefix`` is the object-name prefix of ``revision``."""
    prefix = revision if prefix is None else prefix
    if not isinstance(manifest, dict):
        raise ValueError("invalid model manifest")
    bucket = manifest.get("bucket")
    if (
        not isinstance(bucket, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or ".." in bucket
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", bucket)
    ):
        raise ValueError("invalid model bucket")
    if manifest.get("revision") != revision:
        raise ValueError("unexpected model revision")
    chunk = manifest.get("slice_bytes")
    if type(chunk) is not int or not 8 <= chunk <= 64 * 1024**2:
        raise ValueError("invalid slice size")
    objects = manifest.get("objects")
    if not isinstance(objects, list) or not 0 < len(objects) <= MAX_OBJECTS:
        raise ValueError("invalid object list")
    paths = {}
    total = 0
    for obj in objects:
        if not isinstance(obj, dict):
            raise ValueError("invalid object")
        name = obj.get("name")
        if not isinstance(name, str) or not name.startswith(prefix + "/"):
            raise ValueError("invalid object prefix")
        path = name[len(prefix) + 1 :]
        if (
            any(p in ("", ".", "..") for p in path.split("/"))
            or "\\" in path
            or "\x00" in path
            or path in paths
        ):
            raise ValueError("unsafe or duplicate object path")
        generation, size, digest, slices = (
            obj.get(k) for k in ("generation", "size", "sha256", "slices")
        )
        if (
            not isinstance(generation, str)
            or not re.fullmatch(r"[1-9][0-9]*", generation)
            or type(size) is not int
            or not 0 < size <= max_total
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(slices, list)
            or len(slices) != (size + chunk - 1) // chunk
            or any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in slices)
        ):
            raise ValueError("invalid object identity or hashes")
        if not path.endswith(".safetensors") and size > MAX_CONFIG_BYTES:
            raise ValueError("non-weight object exceeds limit")
        paths[path] = obj
        total += size
    if total > max_total or any(
        "/".join(path.split("/")[:i]) in paths
        for path in paths
        for i in range(1, path.count("/") + 1)
    ):
        raise ValueError("object size or path conflict")
    if any(name not in paths for name in required) or not any(is_weight(p) for p in paths):
        raise ValueError("missing required model files")
    return paths


class TensorSink:
    """Copy verified BF16 tensor segments into already allocated torch parameters."""

    def __init__(self, parameters):
        self.parameters = parameters
        self.progress = {}
        self.skipped = set()

    def __call__(self, spec, offset, data):
        import torch

        parameter = self.parameters.get(spec.name)
        if parameter is None:
            if spec.name.startswith("model.visual."):
                self.skipped.add(spec.name)
                return
            raise ValueError("unexpected tensor: " + spec.name)
        if (
            tuple(parameter.shape) != spec.shape
            or spec.dtype != "BF16"
            or parameter.dtype != torch.bfloat16
            or not parameter.is_contiguous()
            or spec.size != parameter.numel() * 2
        ):
            raise ValueError("parameter mismatch: " + spec.name)
        if (
            offset != self.progress.get(spec.name, 0)
            or offset + len(data) > spec.size
            or (offset == 0 and spec.name in self.progress)
        ):
            raise ValueError("invalid segment: " + spec.name)
        if len(data):
            source = torch.frombuffer(data, dtype=torch.uint8)
            parameter.view(torch.uint8).view(-1)[offset : offset + len(data)].copy_(source)
        self.progress[spec.name] = offset + len(data)

    def check_complete(self):
        if set(self.progress) != set(self.parameters) or any(
            self.progress[n] != p.numel() * 2 for n, p in self.parameters.items()
        ):
            raise ValueError("missing or incomplete tensors")


def fetch_small_objects(paths, bucket, token, directory):
    """Download and SHA-256-verify every non-safetensors object into ``directory``."""
    for path, obj in paths.items():
        if path.endswith(".safetensors"):
            continue
        fetch = gcs_range_fetcher(bucket, obj["name"], obj["generation"], obj["size"], token)
        data = fetch(0, obj["size"])
        if hashlib.sha256(data).hexdigest() != obj["sha256"]:
            raise ValueError("config hash mismatch")
        destination = Path(directory) / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def load_glide(manifest, token, settings, *, workers=8):
    """Load the pinned text-only GLiDE model; discard partial model on any failure."""
    paths = validate_manifest(manifest)
    import torch
    from glide.model import DEFAULT_NAME, LM_PREFIX, Glide, _check_codes, _Network, _restore_rotary
    from transformers import AutoConfig, AutoTokenizer

    chunk = manifest["slice_bytes"]
    bucket = manifest["bucket"]
    with tempfile.TemporaryDirectory() as temp:
        fetch_small_objects(paths, bucket, token, temp)
        config = json.loads((Path(temp) / "decision_config.json").read_text())
        tokenizer = AutoTokenizer.from_pretrained(temp, local_files_only=True)
        _check_codes(tokenizer, config)
        full = AutoConfig.from_pretrained(temp, local_files_only=True)
        text_config = getattr(full, "text_config", full)
        text_config._attn_implementation = "sdpa"
        text_config.use_cache = False
        if config.get("loop"):
            raise ValueError("unsupported loop")
        torch.backends.cuda.enable_cudnn_sdp(False)
        with torch.device("meta"):
            net = _Network(text_config, None, config.get("midloop")).to(torch.bfloat16)
        parameters = {}
        sink = None
        model = None
        try:
            net = net.to_empty(device="cuda")
            parameters = {LM_PREFIX + name: p for name, p in net.backbone.named_parameters()}
            parameters["weight"] = net.readout.weight
            sink = TensorSink(parameters)
            with torch.no_grad():
                weights = sorted(
                    p for p in paths if p.startswith("model-") and p.endswith(".safetensors")
                )
                for path in [*weights, "readout.safetensors"]:
                    obj = paths[path]
                    binding = GCSManifest(
                        bucket,
                        obj["name"],
                        obj["generation"],
                        obj["size"],
                        chunk,
                        tuple(obj["slices"]),
                    )
                    stream_gcs_safetensors(binding, token, sink, workers=workers)
                sink.check_complete()
                _restore_rotary(net, "cuda")
            net.eval().requires_grad_(False)
            model = Glide(
                net,
                tokenizer,
                config,
                device="cuda",
                max_len=settings["max_len"],
                token_budget=settings["token_budget"],
                name=config.get("model_name") or DEFAULT_NAME,
            )
            model.quantization = None
            model.quantized_modules = []
            return model
        except BaseException as exc:
            # Tracebacks in inner frames may retain tensors even when the caller keeps
            # the exception. The active loader frame is cleared explicitly below.
            traceback.clear_frames(exc.__traceback__)
            if sink is not None:
                sink.parameters = {}
            parameters.clear()
            model = None
            net = None
            raise
