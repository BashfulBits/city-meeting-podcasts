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
    "classify-civic-platforms",
    "agenda-item-extract",
    "agenda-chapter-locate",
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

    ``model`` (R13, additive/optional — every existing construction call site is unaffected) is
    the resolved model string for an LLM backend, or ``None`` for backends where "model" has no
    meaning (e.g. the ASR ``local`` adapter). It exists because a caller using policy-driven model
    auto-selection computes ``InferenceJob.recipe_hash`` *before* the backend has chosen a model, so
    the hash alone cannot identify which model produced a given result; a caller that needs to
    content-address its own stored artifact by model must fold this field in itself.
    """

    task: Task
    recipe_hash: str
    output: Any
    model: str | None = None


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
    # Optional, stable name of a Pydantic response contract.  This is deliberately a string, not
    # a Python class: queue/state implementations can persist it and later reconciliation can
    # validate the completed result after a process restart.
    structured_output: str | None = None
    # ``model`` / ``owner`` (R13, additive/optional): the resolved LLM model and its quota/cost
    # ledger reservation owner, present only for a policy-tracked async dispatch. A later
    # ``reconcile()`` uses ``owner`` to settle that reservation to actual usage once the Worker's
    # terminal response is available; both are ``None`` for every non-LLM dispatch backend.
    model: str | None = None
    # Physical LLM route identity, when a logical model was selected from a multi-provider pool.
    # It is deliberately optional so non-LLM dispatch backends and legacy handles remain valid.
    route_id: str | None = None
    owner: str | None = None
    # ``input_per_token`` / ``output_per_token`` (R13, additive/optional): the route's per-token
    # $ rates *at reservation time*, snapshotted onto the handle so a later ``reconcile()`` prices
    # actual usage against the rate the reservation was actually made under, not whatever
    # ``ROUTES`` happens to say at poll time (config can change between a dispatch and its
    # eventual reconciliation, however rare that drift is in practice).
    input_per_token: float | None = None
    output_per_token: float | None = None
    # ``attempted_requests`` (R13, additive/optional): how many real provider requests this
    # dispatch attempt already made before returning this handle (submitting a job is itself one
    # attempt, distinct from any polls that follow). Snapshotted so a later ``reconcile()`` can
    # settle the reservation to the real count instead of leaving it frozen at the worst-case
    # estimate reserved at dispatch time. ``None`` for every non-LLM dispatch backend.
    attempted_requests: int | None = None
    # ``deferred_request`` (R13, additive/optional): opaque to this generic contract by design --
    # it is never read or written here, only carried. A backend that produces a handle representing
    # "not eligible yet, but here's everything needed to retry, no remote submission happened" (the
    # LiteLLM backend's `LLMRequestPolicy.defer_as_handle`) stores its own request-shaped payload
    # here (a `citypods.compute.llm_policy.DeferredLLMRequest`) and reads it back in its own
    # ``reconcile()``. ``None`` for every real dispatch-Worker handle (Modal/Beam/Mistral).
    deferred_request: object | None = None


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
