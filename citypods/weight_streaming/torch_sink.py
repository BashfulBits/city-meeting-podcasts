"""Torch destinations for ``TensorSink``: direct copies and FP8 (e4m3) quantize-on-complete.

Import this module only where torch is installed; the rest of the package has no torch dependency.
The destination tensors are pre-allocated by the model owner (typically ``to_empty`` on the GPU
after building the model on the meta device). A sink validates every tensor against its target
and must be discarded after any exception, because earlier verified bytes may already be written.
"""

import warnings
from collections.abc import Mapping

import torch

E4M3 = torch.float8_e4m3fn
E4M3_MAX = torch.finfo(E4M3).max
DTYPES = {
    "BF16": torch.bfloat16,
    "F16": torch.float16,
    "F32": torch.float32,
    "F64": torch.float64,
    "I8": torch.int8,
    "U8": torch.uint8,
    "I16": torch.int16,
    "I32": torch.int32,
    "I64": torch.int64,
    "BOOL": torch.bool,
    "F8_E4M3": torch.float8_e4m3fn,
    "F8_E5M2": torch.float8_e5m2,
}


def quantize_rows_e4m3(x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Rows of ``x`` scaled into the e4m3 range with one float32 scale per row, shaped (rows, 1).

    The operations and their order are those of Karpo's ``serve.fp8._to_e4m3`` (the project's
    FP8 projection format), so the bytes match the author's quantization of the same weight.
    """
    scale = x.abs().amax(-1, keepdim=True).float().clamp(min=1e-12) / E4M3_MAX
    return (x.float() / scale).clamp(-E4M3_MAX, E4M3_MAX).to(E4M3), scale


def _bytes_of(tensor: torch.Tensor) -> torch.Tensor:
    return tensor.reshape(-1).view(torch.uint8)


def _source(data: memoryview) -> torch.Tensor:
    with warnings.catch_warnings():
        # The sink copies synchronously and never keeps the view; the buffer is only read.
        warnings.simplefilter("ignore")
        return torch.frombuffer(data, dtype=torch.uint8)


class TorchSink:
    """Copy verified segments into ``direct`` tensors, or quantize ``quantized`` ones when complete.

    ``direct`` maps a safetensors name to a contiguous destination of the same dtype and shape.
    ``quantized`` maps a BF16 (out, in) weight's name to its FP8 destination ``(weight, scale)``,
    with ``weight`` e4m3 (out, in) and ``scale`` float32 (1, out), the layout of ``Fp8Linear``.
    A quantized tensor is staged as BF16 on ``device`` one tensor at a time, so the BF16 model is
    never resident. ``ignore_prefixes`` lists tensors the model does not use; they are recorded.
    """

    def __init__(
        self,
        direct: Mapping[str, torch.Tensor],
        quantized: Mapping[str, tuple[torch.Tensor, torch.Tensor]] | None = None,
        *,
        device: torch.device | str | None = None,
        ignore_prefixes: tuple[str, ...] = (),
    ):
        quantized = dict(quantized or {})
        if set(direct) & set(quantized):
            raise ValueError("tensor is both direct and quantized")
        for name, tensor in direct.items():
            if not tensor.is_contiguous():
                raise ValueError("direct destination is not contiguous: " + name)
        for name, (weight, scale) in quantized.items():
            if (
                weight.dtype != E4M3
                or scale.dtype != torch.float32
                or weight.ndim != 2
                or tuple(scale.shape) != (1, weight.shape[0])
                or not weight.is_contiguous()
                or not scale.is_contiguous()
            ):
                raise ValueError("invalid FP8 destination: " + name)
        self.direct = dict(direct)
        self.quantized = quantized
        self.device = torch.device(device) if device is not None else None
        self.ignore_prefixes = tuple(ignore_prefixes)
        self.progress: dict[str, int] = {}
        self.ignored: set[str] = set()
        self._staging: dict[str, torch.Tensor] = {}
        self._done: set[str] = set()

    def _expected(self, name):
        if name in self.direct:
            tensor = self.direct[name]
            return tuple(tensor.shape), tensor.dtype, tensor.numel() * tensor.element_size()
        weight = self.quantized[name][0]
        return tuple(weight.shape), torch.bfloat16, weight.numel() * 2

    def __call__(self, spec, offset, data):
        name = spec.name
        if name not in self.direct and name not in self.quantized:
            if any(name.startswith(prefix) for prefix in self.ignore_prefixes):
                self.ignored.add(name)
                return
            raise ValueError("unexpected tensor: " + name)
        shape, dtype, size = self._expected(name)
        if (
            spec.shape != shape
            or DTYPES.get(spec.dtype) is not dtype
            or spec.size != size
            or (size == 0 and name in self.quantized)
        ):
            raise ValueError("tensor mismatch: " + name)
        if (
            offset != self.progress.get(name, 0)
            or offset + len(data) > size
            or (offset == 0 and name in self.progress)
        ):
            raise ValueError("invalid segment: " + name)
        with torch.no_grad():
            if name in self.direct:
                if len(data):
                    _bytes_of(self.direct[name])[offset : offset + len(data)].copy_(_source(data))
            else:
                stage = self._staging.get(name)
                if stage is None:
                    weight = self.quantized[name][0]
                    stage = torch.empty(
                        shape, dtype=torch.bfloat16, device=self.device or weight.device
                    )
                    self._staging[name] = stage
                if len(data):
                    _bytes_of(stage)[offset : offset + len(data)].copy_(_source(data))
        self.progress[name] = offset + len(data)
        if name in self.quantized and self.progress[name] == size:
            with torch.no_grad():
                values, scale = quantize_rows_e4m3(self._staging.pop(name))
                weight, target_scale = self.quantized[name]
                weight.copy_(values)
                target_scale.copy_(scale.t())
            self._done.add(name)

    def check_complete(self):
        """Every destination must be fully written exactly once; nothing may be left staged."""
        expected = {**self.direct, **self.quantized}
        if (
            set(self.progress) != set(expected)
            or self._staging
            or any(self.progress[n] != self._expected(n)[2] for n in expected)
        ):
            raise ValueError("missing or incomplete tensors")

    def discard(self):
        """Release every reference after a failure; the destinations must not be used."""
        self.direct = {}
        self.quantized = {}
        self._staging.clear()
        self.progress.clear()


class CollectSink:
    """Collect small tensors into new CPU tensors (readouts, other small safetensors files)."""

    def __init__(self, expected: Mapping[str, tuple[str, tuple[int, ...]]] | None = None):
        self.expected = dict(expected) if expected is not None else None
        self.tensors: dict[str, torch.Tensor] = {}
        self.progress: dict[str, int] = {}

    def __call__(self, spec, offset, data):
        name = spec.name
        if self.expected is not None and self.expected.get(name) != (spec.dtype, spec.shape):
            raise ValueError("unexpected tensor: " + name)
        dtype = DTYPES.get(spec.dtype)
        if dtype is None or spec.size > 1024**3:
            raise ValueError("unsupported small tensor: " + name)
        if offset != self.progress.get(name, 0) or offset + len(data) > spec.size:
            raise ValueError("invalid segment: " + name)
        if offset == 0:
            if name in self.tensors:
                raise ValueError("duplicate tensor: " + name)
            self.tensors[name] = torch.empty(spec.shape, dtype=dtype, device="cpu")
        if len(data):
            _bytes_of(self.tensors[name])[offset : offset + len(data)].copy_(_source(data))
        self.progress[name] = offset + len(data)

    def result(self) -> dict[str, torch.Tensor]:
        """The collected tensors; with ``expected`` every expected name must have arrived.

        A collector has no ``discard()``: after a failure drop the collector and its results.
        """
        if self.expected is not None and set(self.tensors) != set(self.expected):
            raise ValueError("missing or unexpected tensors")
        if any(self.progress[n] != t.numel() * t.element_size() for n, t in self.tensors.items()):
            raise ValueError("incomplete tensors")
        return self.tensors
