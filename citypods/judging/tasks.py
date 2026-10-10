"""Task-spec registry: every judged producer task plugs in by declaring questions and evidence.

Nothing in the judges, packing, transport or ledger is task-specific (review/49 section 3a). A task
declares its subjects, its ordered questions and how evidence is built at each context tier.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

QuestionKind = Literal["validate", "gate", "grade", "choose"]
ContextTier = Literal["T0", "T1", "T2", "T3"]
TIERS: tuple[ContextTier, ...] = ("T0", "T1", "T2", "T3")


@dataclass(frozen=True)
class QuestionSpec:
    id: str
    kind: QuestionKind
    instruction: str
    levels: tuple[str, ...] = ()  # grade only, lowest first
    prompt_version: str = "1"  # a bump starts a new calibration cell for this question


@dataclass(frozen=True)
class Subject:
    """One producer output to judge. ``payload`` is the stored candidate (mutated by the ledger)."""

    task: str
    subject_id: str
    episode_uid: str
    group: str | None
    producer_model: str | None  # None for deterministic rule candidates
    payload: dict[str, Any] = field(compare=False, hash=False, repr=False)  # read only
    # The episode's ``judging`` block, where this subject's judgments live (ledger.py).
    store: dict[str, Any] = field(default_factory=dict, compare=False, hash=False, repr=False)


@dataclass(frozen=True)
class Evidence:
    tier: ContextTier
    text: str
    digest: str


@dataclass(frozen=True)
class EpisodeTexts:
    """What evidence builders read: timed transcript segments, chapters and tag definitions."""

    segments: Sequence[Mapping[str, Any]] = ()
    # (chapter_id, title, start_seconds, end_seconds or None for the last chapter)
    chapters: Sequence[tuple[str, str, float, float | None]] = ()
    definitions: Mapping[str, str] = field(default_factory=dict)
    # The transcript the evidence came from; an "unbuildable" tier is retried when it changes.
    identity: str = ""


EVIDENCE_BUILDER_VERSION = "1"


def judging_store(ep: Any) -> dict[str, Any]:
    """The episode's ``judging`` block, created empty on first use."""
    store = getattr(ep, "judging", None)
    if not isinstance(store, dict):
        store = {}
        ep.judging = store
    return store


def transcript_identity(ep: Any) -> str:
    return (
        f"{getattr(ep, 'transcript_key', '') or ''}:{getattr(ep, 'transcript_spec_hash', '') or ''}"
    )


def make_evidence(tier: ContextTier, text: str) -> Evidence:
    digest = hashlib.sha256(f"{EVIDENCE_BUILDER_VERSION}\0{tier}\0{text}".encode()).hexdigest()
    return Evidence(tier=tier, text=text, digest=digest[:32])


@dataclass(frozen=True)
class TaskSpec:
    name: str
    questions: tuple[QuestionSpec, ...]
    subjects: Callable[[Any], list[Subject]]
    evidence: Callable[[Subject, ContextTier, EpisodeTexts], Evidence | None]
    first_tier: ContextTier
    # (tier, low, high): re-judge at ``tier`` when the anchor's probability is in [low, high].
    escalation: tuple[ContextTier, float, float] | None
    consensus_policy: str  # implemented in PR7/PR8; recorded only until then
    selection_policy: Literal["all_admitted", "top_k_per_group"]
    top_k: int | None = None
    # Tiers this task's evidence builder can produce; the all-tier sample asks only these.
    tiers: tuple[ContextTier, ...] = ("T0", "T1", "T2")

    def question(self, question_id: str) -> QuestionSpec:
        return next(q for q in self.questions if q.id == question_id)


_LOCK = threading.Lock()
_REGISTRY: dict[str, TaskSpec] = {}


def register(spec: TaskSpec) -> TaskSpec:
    ids = [q.id for q in spec.questions]
    if not spec.name or not spec.questions:
        raise ValueError("a task needs a name and at least one question")
    if len(set(ids)) != len(ids):
        raise ValueError(f"task {spec.name!r} repeats a question id: {ids}")
    for q in spec.questions:
        if q.kind == "grade" and not q.levels:
            raise ValueError(f"task {spec.name!r} grade question {q.id!r} needs levels")
        if q.kind == "choose" and spec.selection_policy != "top_k_per_group":
            raise ValueError(f"task {spec.name!r}: a choose question needs top_k_per_group")
    if spec.first_tier not in TIERS or (spec.escalation and spec.escalation[0] not in TIERS):
        raise ValueError(f"task {spec.name!r} names an unknown context tier")
    with _LOCK:
        existing = _REGISTRY.get(spec.name)
        if existing is not None and existing is not spec:
            raise ValueError(f"task {spec.name!r} is already registered")
        _REGISTRY[spec.name] = spec
    return spec


def task(name: str) -> TaskSpec:
    _ensure_builtin()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown judging task {name!r}") from None


def registered() -> tuple[str, ...]:
    _ensure_builtin()
    return tuple(sorted(_REGISTRY))


def _ensure_builtin() -> None:
    # Importing registers; kept lazy so the registry module has no heavy imports.
    from citypods.judging import moment_task, tag_task  # noqa: F401
