"""In-process GPU/ASR backend — the only adapter that must exist at 1.0.

A pure move of the faster-whisper / WhisperX calls that ``TranscriptStage`` used to make
directly on :mod:`citypods.asr`: this wraps them behind the :class:`~citypods.compute.base.Backend`
protocol with **byte-identical output**. The heavy lifting (model load/cache, VTT + word-JSON
emit, alignment-quality gate) stays in ``asr.py``; this adapter only routes the ``transcribe`` /
``align`` task verbs to the matching ``asr`` call.

The ``asr`` module is injected (defaulting to the real one) so a caller can hand in a
test/double or its own already-imported reference — ``TranscriptStage`` passes the module-level
``asr`` it already holds, which keeps the inference path swappable without this adapter reaching
back into the stage.
"""

from __future__ import annotations

from types import ModuleType

from citypods.compute.align import run_alignment
from citypods.compute.base import InferenceJob, JobResult


class LocalBackend:
    """Synchronous backend running faster-whisper / WhisperX in this process."""

    name = "local"

    def __init__(self, asr: ModuleType | None = None):
        if asr is None:
            from citypods import asr as asr_module  # lazy: keep ASR extras cost off import

            asr = asr_module
        self._asr = asr

    def run_inference(self, job: InferenceJob) -> JobResult:
        """Run ``job`` in-process and return its artifact.

        Inputs (per task verb), mirroring the old direct ``asr`` calls one-for-one:

        * ``transcribe`` — ``audio_path, model, language, compute_type, beam_size,
          initial_prompt, cpu_threads``
        * ``align`` — ``audio_path, sections, model, language, cpu_threads, interpolate_method``
        * ``diarize`` — ``audio_path, model, token, device``
        """
        inp = job.inputs
        if job.task == "transcribe":
            output = self._asr.transcribe(
                inp["audio_path"],
                inp["model"],
                inp["language"],
                inp["compute_type"],
                inp["beam_size"],
                inp["initial_prompt"],
                inp["cpu_threads"],
            )
        elif job.task == "align":
            output = run_alignment(self._asr, inp)
        elif job.task == "diarize":
            from citypods import diarize as diarize_module

            output = diarize_module.diarize(
                inp["audio_path"],
                inp.get("model", diarize_module.DEFAULT_DIARIZE_MODEL),
                embedding_model=inp.get("embedding_model", diarize_module.DEFAULT_EMBEDDING_MODEL),
                token=inp.get("token"),
                device=inp.get("device"),
            )
        else:
            raise ValueError(f"local backend does not implement task {job.task!r}")
        return JobResult(task=job.task, recipe_hash=job.recipe_hash, output=output)
