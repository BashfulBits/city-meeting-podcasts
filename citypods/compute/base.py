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
``summarize``/``tag``/``soundbite-select`` — so the R2 LLM-API adapter (ROADMAP, first built as
dedicated infra ahead of its R5/R6 feature consumers) slots in with no interface change. This
shape is the pre-1.0-locked contract; treat additions as breaking.
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


def lease_owner_for(handle: JobHandle) -> str:
    """The H5 ``work.json`` lease owner string for an in-flight dispatch job: ``"<backend>:<ref>"``
    (e.g. ``"modal:job-123"``). The first competitive use of the lease API (H14a) — the inverse,
    ``"<backend>:<ref>".split(":", 1)``, recovers the backend name at reconcile time."""
    return f"{handle.backend}:{handle.ref}"


@runtime_checkable
class Backend(Protocol):
    """Runs an :class:`InferenceJob`. Mirrors ``storage.StorageBackend``'s Protocol shape.

    A synchronous backend returns the artifact (:class:`JobResult`); a dispatch backend submits
    the job and returns a :class:`JobHandle` without awaiting it.
    """

    name: str

    def run_inference(self, job: InferenceJob) -> JobResult | JobHandle: ...


@runtime_checkable
class DispatchBackend(Protocol):
    """A **dispatch** backend (H14): submits a job to an external worker and returns a
    :class:`JobHandle` without awaiting it (the Modal/Beam adapters land in H14b/H14c; a
    fake stands in for tests). It is a :class:`Backend` whose ``run_inference`` always returns a
    :class:`JobHandle`, plus the two hooks the dispatcher's budget ledger needs.

    **The result-write contract.** The remote worker reads the audio from its public URL, runs the
    task, and writes the *content-addressed* artifact to the **same object bucket** the synchronous
    path uses — the existing ``transcripts/<src>/<uid>-asr-<recipe>.vtt`` (+ ``.words.json``) keys
    (``citypods.stages._asr_vtt_key`` / ``_asr_words_key``). It then marks its ``work.json``
    :class:`~citypods.ops.workqueue.WorkItem` ``done`` and records the actual GPU seconds in
    ``observed_seconds``. The **next** render reconciles the artifact onto the feed exactly as a
    not-yet-hosted episode is handled today; content-addressing on ``recipe_hash`` makes a
    re-dispatch idempotent (the artifact is already present ⇒ no-op).

    * ``estimate_gpu_seconds`` — the **decrement-on-dispatch** estimate the budget ledger reserves
      before submitting (reconciled to ``observed_seconds`` actuals when the item completes). It
      must not under-report, or the free-tier cap could be breached between dispatch and reconcile.
    """

    name: str

    def run_inference(self, job: InferenceJob) -> JobHandle: ...

    def estimate_gpu_seconds(self, job: InferenceJob) -> float: ...
