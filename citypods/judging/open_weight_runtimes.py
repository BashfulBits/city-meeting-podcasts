"""Decision adapters for elephant-1 and Copernicus-31B on their authors' own scoring code.

review/60 Phase 1 follow-up. Both adapters expose the call of the Karpo adapter in
``workers/decision-batch/open_weight_eval_worker.py``::

    runner.run(requests) -> one reply per request, in order

A request is ``{"state": str, "questions": {qid: {"type": "noul", "instructions": str}}}`` and a
reply is ``{"answers": {qid: {"type": "noul", "noul": p}}, "usage": {"input_tokens": n}}`` or the
status string ``"too_large"`` / ``"invalid_input"``.

* elephant-1 runs OneJev's ``qev`` ``DecisionEngine`` (commit 81ce62f plus the model's
  ``serve.patch``) through the checkpoint's own ``engine.py``, with the checkpoint's own readout
  (``readout.json``: ``slot_lm``, bidirectional attention inside the suffix block).
* Copernicus-31B runs ``copernicus_server.Copernicus`` with its ``HF`` scoring backend and the
  server's default flags (temperature 1, input read twice for requests up to 3,000 tokens).

The authors' code is read-only source on ``PYTHONPATH`` (audited in review/60; each file is
checked against a pinned SHA-256 before it is imported). Weights stream through
``citypods.judging.karpo_stream.load_hf_backbone`` into the backbone the author's class expects;
``*_reference`` loads the same checkpoint through the author's own ``from_pretrained`` path, for
parity. Nothing here opens a network connection or reads a credential beyond the weight
manifest's bearer token.
"""

import contextlib
import hashlib
import importlib.util
import tempfile
import traceback
from pathlib import Path
from unittest import mock

from citypods.judging.glide_stream import fetch_small_objects
from citypods.judging.karpo_stream import load_hf_backbone, validate_manifest

ELEPHANT_REVISION = "b3f8a93462fb1051a9de031e94a7ff380e254072"
COPERNICUS_REVISION = "ae28efb0ae759c3494a1078c0a56eda10a9adb38"
ONEJEV_ROOT = "/opt/onejev"
ELEPHANT_ROOT = "/opt/elephant"
COPERNICUS_ROOT = "/opt/copernicus"
# SHA-256 of every author file that the adapters import, as installed in the image (the OneJev
# files after serve.patch). A different byte stream is refused rather than run.
SOURCE_SHA256 = {
    "elephant": {
        "engine.py": "72373353379c53b7cabfb5d05b6fef3c898d48c87cc386e03fc3de9c07e67ca7",
    },
    "onejev": {
        "qev/__init__.py": "6446cd0006940630b28180d275efe39972b871c2fb2ae502c848607c940e8376",
        "qev/answers.py": "09b06dba258c80a6a9afdaa5aacfd0f7523f9d2a087a7b6402ca03553ea6fba6",
        "qev/calibrate.py": "3fa6013a81c7e79a702afcdcc0ebd089bee7047590c18cc44708850f5aeda1ee",
        "qev/client.py": "560ca00f7ecffee72e6490afdd69a34cf4f55a6e1765a0493d4beebf8b6b4278",
        "qev/schema.py": "bce4b7d353ac115a9e50aef284975ddeed9a3bb452e3fd928a556668705bc2f3",
        "qev/engine.py": "dc3eb95e0118cb056357bd3e68c0ed9f18b4656492b02087e9ca53773af8d3f0",
        "qev/prompt.py": "f0763b5f747039c2919bc2a6d51e4ed88308fe3497e53498e6f298286b6169b2",
        "qev/readout.py": "e084edead750ddeb39384b9f659182df52720d96294d9a95c9259a8001e3c20d",
    },
    "decision_index": {
        "decision_index/__init__.py": "802fef8d0a617d28cd906facf8582b707ca7baa35475ce834ef8af41ab9aa54f",  # noqa: E501
        "decision_index/engines/__init__.py": "808a0971ad2ce9f9a1afc3c071691001375aa73b26f3ba891beb8364dcabba77",  # noqa: E501
        "decision_index/engines/base.py": "ae548636cfbd583fe774b67e4d3d2cdc6db10f580f47d9559d5de44373b79ca5",  # noqa: E501
    },
    "copernicus": {
        "copernicus_server.py": "1717f334094b44926a4a46e7860d3e67135cc91158125f8cccf4a637ef757a32",
        "copernicus_layout.py": "dc641d4dbac05cd088de59c20c06cfc44e1dcc18d4102470fc8feba943359639",
    },
}
DECISION_INDEX_ROOT = "/opt/decision-index"
ROOTS = {
    "elephant": ELEPHANT_ROOT,
    "onejev": ONEJEV_ROOT,
    "decision_index": DECISION_INDEX_ROOT,
    "copernicus": COPERNICUS_ROOT,
}


def verify_sources(which, roots=None):
    """Raise unless every pinned file of ``which`` (a key of ``SOURCE_SHA256``) is as pinned."""
    root = Path((roots or ROOTS)[which])
    for relative, expected in SOURCE_SHA256[which].items():
        path = root / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if digest != expected:
            raise ValueError(f"{which} source differs from the audited file: {relative}")


def check_origin(module, which, roots=None):
    """Raise unless ``module`` was imported from the verified root of ``which``."""
    root = Path((roots or ROOTS)[which]).resolve()
    origin = Path(getattr(module, "__file__", None) or "/nonexistent").resolve()
    if root not in origin.parents:
        raise ValueError(f"{module.__name__} was not imported from the verified {which} source")


def _load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release(torch):
    import gc

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


class _Runner:
    """Shared ``run``; subclasses implement ``_decide(state, questions) -> (answers, tokens)``."""

    def run_raw(self, request):
        """One request, letting any error propagate (for diagnostics); a reply dict."""
        answers, tokens = self._decide(request["state"], request["questions"])
        return {"answers": answers, "usage": {"input_tokens": tokens}}

    too_large = ()
    invalid = ()

    def run(self, requests):
        out = []
        for request in requests:
            try:
                answers, tokens = self._decide(request["state"], request["questions"])
            except self.too_large:
                out.append("too_large")
            except self.invalid:
                out.append("invalid_input")
            except Exception as exc:
                if type(exc).__name__ != "OutOfMemoryError":  # torch's, without importing torch
                    raise
                import torch

                _release(torch)
                out.append("too_large")
            else:
                reply = {"answers": answers}
                if isinstance(tokens, int) and not isinstance(tokens, bool):
                    reply["usage"] = {"input_tokens": tokens}
                out.append(reply)
        return out


# ------------------------------------------------------------------ elephant-1


class ElephantRunner(_Runner):
    def __init__(self, decision_model, unsupported, validation_error):
        self.model = decision_model
        self.too_large = (unsupported,)
        self.invalid = (validation_error,)
        self.tokens = None
        engine, original = decision_model.engine, decision_model.engine.decide

        def decide(request):
            response, extra = original(request)
            self.tokens = response.usage.input_tokens
            return response, extra

        engine.decide = decide

    def _decide(self, state, questions):
        self.tokens = None
        document, _ = self.model(state, questions)
        answers = {
            qid: {"type": "noul", "noul": float(entry["noul"])}
            for qid, entry in document["answers"].items()
        }
        return answers, self.tokens


def _elephant_modules():
    verify_sources("elephant")
    verify_sources("onejev")
    import pydantic

    verify_sources("decision_index")
    import decision_index.engines
    import qev.engine

    check_origin(qev.engine, "onejev")
    check_origin(decision_index.engines, "decision_index")
    engine = _load_file("elephant_engine", Path(ELEPHANT_ROOT) / "engine.py")
    from decision_index.engines import Unsupported

    return engine, Unsupported, pydantic.ValidationError


def _wrap_elephant(engine_module, decision_engine, label, device, unsupported, validation_error):
    from decision_index.engines import Engine

    model = engine_module.DecisionModel.__new__(engine_module.DecisionModel)
    Engine.__init__(model, model=label, device=device)
    model.engine = decision_engine
    model.provenance = {"runtime": "OneJev qev @81ce62f + serve.patch", "model": label}
    return ElephantRunner(model, unsupported, validation_error)


def load_elephant_reference(model_dir, *, device="cuda:0"):
    """The author's own load: ``engine.py`` ``DecisionModel(model_dir)`` (``from_pretrained``)."""
    engine_module, unsupported, validation_error = _elephant_modules()
    model = engine_module.DecisionModel(str(model_dir), device=device)
    return ElephantRunner(model, unsupported, validation_error)


HEAD_CHUNK = 16384


def _pinned(tensor, torch):
    """Page-locked host memory when a CUDA device exists (faster, asynchronous copies)."""
    return tensor.pin_memory() if torch.cuda.is_available() else tensor


def lean_head_class(torch):
    """A bf16-stored LM head whose logits are computed in float32, a vocabulary chunk at a time.

    ``DecisionEngine._upcast_head`` keeps a float32 copy of the head (3.8 GiB for elephant-1) to
    avoid bf16 logit rounding. The bf16 weights convert to float32 exactly, so the copy holds no
    extra information; this module multiplies the float32 activations by float32 views of the
    same weights, so the logits equal the engine's float32 head's up to summation order, while only
    the bf16 weights stay resident.
    """

    class LeanHead(torch.nn.Module):
        def __init__(self, weight, offload=False):
            super().__init__()
            self.offload = offload
            if offload:  # pinned host memory, not a parameter: model.device stays the GPU
                self._cpu_weight = _pinned(weight.detach().cpu(), torch)
            else:
                self.weight = torch.nn.Parameter(weight, requires_grad=False)

        def forward(self, hidden):
            h = hidden.float()
            weight = self._cpu_weight if self.offload else self.weight
            parts = [
                h @ weight[i : i + HEAD_CHUNK].to(h.device, non_blocking=True).float().t()
                for i in range(0, weight.shape[0], HEAD_CHUNK)
            ]
            return torch.cat(parts, dim=-1)

    return LeanHead


def chunk_mlps(model, chunk):
    """Run every decoder MLP over ``chunk`` tokens at a time.

    An MLP acts on each token independently, so this is the same computation with a smaller
    intermediate activation (12,288 wide per token for elephant-1), which dominates prefill
    memory at tens of thousands of tokens. Only the matrix shapes differ, so results can differ
    in the last bits of float accumulation.
    """
    count = 0
    for layer in model.model.layers:
        mlp = layer.mlp
        original = mlp.forward

        def forward(x, original=original):
            if x.shape[1] <= chunk:
                return original(x)
            return torch_cat([original(x[:, i : i + chunk]) for i in range(0, x.shape[1], chunk)])

        mlp.forward = forward
        count += 1
    return count


def torch_cat(parts):
    import torch

    return torch.cat(parts, dim=1)


def cpu_embedding_class(torch):
    """An embedding whose table stays in pinned host memory; only the looked-up rows reach the GPU.

    The lookup is an exact row copy, so the activations are bit-identical to the GPU table's.
    """

    class CpuEmbedding(torch.nn.Module):
        def __init__(self, embedding):
            super().__init__()
            self._table = _pinned(embedding.weight.detach().cpu(), torch)
            self.num_embeddings = embedding.num_embeddings
            self.embedding_dim = embedding.embedding_dim
            self.padding_idx = embedding.padding_idx
            self._device = embedding.weight.device

        @property
        def weight(self):
            return self._table

        def forward(self, ids):
            rows = torch.nn.functional.embedding(ids.to("cpu"), self._table, self.padding_idx)
            return rows.to(self._device, non_blocking=True)

    return CpuEmbedding


def causal_lm_from_backbone(
    backbone, extras, directory, torch, stats=None, lean_head=False, offload_tables=False
):
    """A ``Qwen3_5ForCausalLM`` over the streamed backbone's text model and ``lm_head.weight``.

    Equivalent to ``Qwen3_5ForCausalLM.from_pretrained`` on the checkpoint: the same text decoder
    weights and head; the vision tower, which the engine never uses, is dropped.
    """
    from transformers import AutoConfig, Qwen3_5ForCausalLM

    head = extras.get("lm_head.weight")
    text = backbone.language_model
    config = AutoConfig.from_pretrained(directory, local_files_only=True).get_text_config()
    with torch.device("meta"):
        causal = Qwen3_5ForCausalLM._from_config(
            config, dtype=torch.bfloat16, attn_implementation="sdpa"
        )
    expected = (config.vocab_size, config.hidden_size)
    if head is None or tuple(head.shape) != expected or head.dtype != torch.bfloat16:
        raise ValueError("invalid lm_head")
    if config.tie_word_embeddings:
        raise ValueError("elephant-1 has an untied head")
    causal.model = text
    if lean_head:
        causal.lm_head = lean_head_class(torch)(head, offload=offload_tables)
        if offload_tables:
            extras["lm_head.weight"] = None  # the only GPU reference besides the dropped Parameter
            del head
            text.embed_tokens = cpu_embedding_class(torch)(text.embed_tokens)
    else:
        causal.lm_head.weight = torch.nn.Parameter(head, requires_grad=False)
    backbone.visual = None
    _release(torch)
    if stats is not None:
        stats["gpu_gib_before_engine"] = round(torch.cuda.memory_allocated() / 2**30, 2)
    causal.eval().requires_grad_(False)
    return causal


def load_elephant(
    manifest,
    token,
    *,
    device="cuda",
    workers=8,
    stats=None,
    lean_head=False,
    offload_tables=False,
    mlp_chunk=0,
    fork_token_budget=None,
):
    """Stream elephant-1 into a Qwen3.5 decoder and run the author's engine on it.

    ``lean_head``, ``mlp_chunk`` and ``fork_token_budget`` are the memory options for a 22 GiB
    GPU (see ``lean_head_class`` and ``chunk_mlps``); the defaults are the author's.
    """
    if manifest.get("revision") != ELEPHANT_REVISION:
        raise ValueError("not the pinned elephant-1 revision")
    import torch

    paths = validate_manifest(manifest)
    engine_module, unsupported, validation_error = _elephant_modules()
    backbone = extras = causal = engine = runner = None
    try:
        backbone, extras = load_hf_backbone(
            manifest, token, fp8=False, device=device, workers=workers, stats=stats
        )
        with tempfile.TemporaryDirectory() as temp:
            fetch_small_objects(paths, manifest["bucket"], token, temp)
            causal = causal_lm_from_backbone(
                backbone, extras, temp, torch, stats, lean_head, offload_tables
            )
            if mlp_chunk:
                chunk_mlps(causal, mlp_chunk)
            import transformers
            from qev.engine import DecisionEngine

            # DecisionEngine's own __init__ runs unchanged (slot check, float32 head, readout,
            # fork mode); only its from_pretrained returns the already streamed model.
            patches = [
                mock.patch.object(
                    transformers.Qwen3_5ForCausalLM,
                    "from_pretrained",
                    classmethod(lambda cls, *a, **k: causal),
                )
            ]
            if lean_head:  # the lean head already computes in float32; no second copy
                patches.append(
                    mock.patch.object(DecisionEngine, "_upcast_head", lambda self, dtype: None)
                )
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                extra = {"fork_token_budget": fork_token_budget} if fork_token_budget else {}
                engine = DecisionEngine(temp, device=str(torch.device(device)), **extra)
        runner = _wrap_elephant(
            engine_module, engine, "elephant-1", device, unsupported, validation_error
        )
        del backbone, extras, causal
        _release(torch)
        return runner
    except BaseException as exc:
        traceback.clear_frames(exc.__traceback__)
        backbone = extras = causal = engine = runner = None  # noqa: F841  (release GPU tensors)
        raise


# ------------------------------------------------------------------ Copernicus-31B


class CopernicusRunner(_Runner):
    def __init__(self, engine, unsupported):
        self.engine = engine
        self.too_large = (unsupported,)
        self.invalid = (ValueError,)

    def _decide(self, state, questions):
        answers, usage = self.engine.run(state, questions)
        return answers, usage.get("input_tokens")


def _copernicus_server():
    verify_sources("copernicus")
    import sys

    root = str(Path(COPERNICUS_ROOT))
    if root not in sys.path:
        sys.path.insert(0, root)
    import copernicus_layout
    import copernicus_server

    check_origin(copernicus_server, "copernicus")
    check_origin(copernicus_layout, "copernicus")
    return copernicus_server, copernicus_layout


def _copernicus_engine(server, layout_module, tokenizer, backend):
    """``Copernicus(...)`` with the server's default command-line flags (see its ``main``)."""
    layout = layout_module.Layout(
        header=False,
        header_min_questions=1,
        read_twice=True,
        twice_max_tokens=3000,
        referenced_fields=False,
    )
    return server.Copernicus(
        tokenizer, backend, 1.0, 16, {}, layout, server.Calibration(), False, "locate", None, None
    )


def load_copernicus_reference(model_dir, *, device=None):
    """The author's own load: its ``HF`` backend (``from_pretrained``) and its ``Copernicus``."""
    from transformers import AutoTokenizer

    server, layout_module = _copernicus_server()
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
    backend = server.HF(str(model_dir), device=device)
    return CopernicusRunner(
        _copernicus_engine(server, layout_module, tokenizer, backend), server.Unsupported
    )


def conditional_model_from_backbone(backbone, extras, directory, torch):
    """The class ``HF`` picks (``AutoModelForMultimodalLM``) over the streamed backbone."""
    import transformers
    from transformers import AutoConfig

    config = AutoConfig.from_pretrained(directory, local_files_only=True)
    multimodal = hasattr(config, "vision_config") or hasattr(config, "audio_config")
    cls = (
        getattr(transformers, "AutoModelForMultimodalLM", transformers.AutoModelForImageTextToText)
        if multimodal
        else transformers.AutoModelForCausalLM
    )
    with torch.device("meta"):
        model = cls.from_config(config, dtype=torch.bfloat16)
    model.model = backbone
    if config.get_text_config().tie_word_embeddings:
        model.lm_head.weight = backbone.get_input_embeddings().weight
    else:
        raise ValueError("an untied head needs a pinned lm_head tensor")
    model.eval().requires_grad_(False)
    return model


def load_copernicus(manifest, token, *, device="cuda", workers=8, stats=None):
    """Stream Copernicus-31B and score it with the author's backend class and engine."""
    if manifest.get("revision") != COPERNICUS_REVISION:
        raise ValueError("not the pinned Copernicus-31B revision")
    import torch

    paths = validate_manifest(manifest)
    server, layout_module = _copernicus_server()
    from transformers import AutoTokenizer

    backbone = extras = model = backend = runner = None
    try:
        backbone, extras = load_hf_backbone(
            manifest, token, fp8=False, device=device, workers=workers, stats=stats
        )
        with tempfile.TemporaryDirectory() as temp:
            fetch_small_objects(paths, manifest["bucket"], token, temp)
            model = conditional_model_from_backbone(backbone, extras, temp, torch)
            tokenizer = AutoTokenizer.from_pretrained(temp, local_files_only=True)
            backend = _streamed_hf(server, torch, model, tokenizer)
            runner = CopernicusRunner(
                _copernicus_engine(server, layout_module, tokenizer, backend), server.Unsupported
            )
        del backbone, extras, model
        _release(torch)
        return runner
    except BaseException as exc:
        traceback.clear_frames(exc.__traceback__)
        backbone = extras = model = backend = runner = None  # noqa: F841  (release GPU tensors)
        raise


def _streamed_hf(server, torch, model, tokenizer):
    """An ``HF`` backend object (the author's ``warm`` and ``score``) around an existing model."""
    import threading

    backend = server.HF.__new__(server.HF)
    backend.torch, backend.model, backend.tok = torch, model, tokenizer
    backend.lock = threading.Lock()
    return backend


# ------------------------------------------------------------------ comparison


def compare_answers(reference, streamed):
    """Per question: the largest and mean absolute difference of ``noul`` and any flip at 0.5.

    ``reference`` and ``streamed`` are lists of replies as returned by ``run``.
    """
    diffs, flips, statuses, unanswered = [], 0, 0, 0
    for a, b in zip(reference, streamed, strict=True):
        if not isinstance(a, dict) or not isinstance(b, dict):
            statuses += a != b
            unanswered += 1  # equal statuses are agreement about a failure, not about an answer
            continue
        if set(a["answers"]) != set(b["answers"]):
            raise ValueError("different question sets")
        for qid, entry in a["answers"].items():
            p, q = entry["noul"], b["answers"][qid]["noul"]
            diffs.append(abs(p - q))
            flips += (p >= 0.5) != (q >= 0.5)
    return {
        "questions": len(diffs),
        "max_abs_diff": max(diffs, default=0.0),
        "mean_abs_diff": sum(diffs) / len(diffs) if diffs else 0.0,
        "flips_at_0_5": flips,
        "status_mismatches": statuses,
        "unanswered_requests": unanswered,
    }
