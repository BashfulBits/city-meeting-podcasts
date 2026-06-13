"""Execution-backend contract for heavy inference (the **pre-1.0 lock**).

This is the GPU/process half of the widened §5.5 execution-backend interface — the
sibling of the pluggable ``storage/`` contract, but for *compute* instead of *bytes*.
A backend takes an :class:`InferenceJob` (a task verb + its inputs + the recipe hash that
content-addresses the output) and runs it, returning either:

* a :class:`JobResult` — a **synchronous** backend (today's in-process ``local`` GPU/ASR
  adapter) that produces the artifact in-band, or
* a :class:`JobHandle` — a **dispatch** backend (the Modal/Beam adapters land in H14) that
  submits the job to an external worker and returns a reference without awaiting it.

``task`` is typed for the **full §5.5 verb set** up front — the GPU/ASR verbs
``transcribe``/``align``/``diarize`` **and** the reserved LLM verbs
``summarize``/``tag``/``soundbite-select`` — so the R3/R4 LLM-API adapter slots in with no
interface change. This shape is the pre-1.0-locked contract; treat additions as breaking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# The full §5.5 task verb set. GPU/ASR verbs are live today (``transcribe``/``align`` via the
# ``local`` adapter; ``diarize`` rides the same interface — Phase R #7 adds an adapter call, not
# a new interface). The LLM verbs are reserved so R3/R4's API adapter needs no interface change.
Task = Literal[
    "transcribe",
    "align",
    "diarize",
    "summarize",
    "tag",
    "soundbite-select",
]


@dataclass(frozen=True)
class InferenceJob:
    """One unit of heavy inference to run on a :class:`Backend`.

    * ``task`` — which §5.5 verb to run (see :data:`Task`).
    * ``inputs`` — the task-specific payload. For the ``local`` ASR adapter this carries the
      audio path, the loaded/cached model, and the decode knobs; a dispatch backend reads the
      audio bucket key / enclosure URL instead. Kept an open ``Mapping`` so each verb (and each
      backend family) shapes its own inputs without widening this contract.
    * ``recipe_hash`` — the input-keyed hash that content-addresses the output object
      (``…-<recipe>.vtt``). Computed by the caller *before* running so a backend can name and
      reuse-check the artifact without first running inference.
    """

    task: Task
    inputs: Mapping[str, Any] = field(default_factory=dict)
    recipe_hash: str = ""


@dataclass(frozen=True)
class JobResult:
    """A completed job's artifact, returned by a **synchronous** backend.

    ``output`` is the task-specific artifact — e.g. an :class:`citypods.asr.TranscriptArtifacts`
    (segment VTT + word-JSON sidecar) for ``transcribe``/``align``.
    """

    task: Task
    recipe_hash: str
    output: Any


@dataclass(frozen=True)
class JobHandle:
    """A reference to an in-flight job submitted to a **dispatch** backend (H14).

    The remote worker writes the content-addressed artifact back to the shared object bucket and
    marks the manifest item ``done``; a later deploy reconciles it from durable state. ``ref`` is
    the backend's opaque job/artifact identifier.
    """

    task: Task
    recipe_hash: str
    backend: str
    ref: str


@runtime_checkable
class Backend(Protocol):
    """Runs an :class:`InferenceJob`. Mirrors ``storage.StorageBackend``'s Protocol shape.

    A synchronous backend returns the artifact (:class:`JobResult`); a dispatch backend submits
    the job and returns a :class:`JobHandle` without awaiting it.
    """

    name: str

    def run_inference(self, job: InferenceJob) -> JobResult | JobHandle: ...
