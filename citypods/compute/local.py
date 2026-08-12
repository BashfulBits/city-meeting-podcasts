"""In-process GPU/ASR backend — the only adapter that must exist at 1.0.

A pure move of the faster-whisper / stable-ts calls that ``TranscriptStage`` used to make
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

from citypods.compute.base import InferenceJob, JobResult


class LocalBackend:
    """Synchronous backend running faster-whisper / stable-ts in this process."""

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
        * ``align`` — ``audio_path, text, model, language, cpu_threads``
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
            output = self._asr.align(
                inp["audio_path"],
                inp["text"],
                inp["model"],
                inp["language"],
                inp["cpu_threads"],
                inp.get("compute_type", "int8"),
            )
        else:
            raise ValueError(f"local backend does not implement task {job.task!r}")
        return JobResult(task=job.task, recipe_hash=job.recipe_hash, output=output)
