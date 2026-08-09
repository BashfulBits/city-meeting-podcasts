"""Durable contracts for generated agenda chapters.

The agenda extractor and transcript locator are asynchronous producers.  This module keeps their
wire/storage contracts independent from the LLM client and from :class:`Episode`, so a completed
job can be finalized by a workflow, the deferred sweep, or a later rerun without reconstructing
the original request.  Provider chapters are deliberately not represented by these records.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

AGENDA_ARTIFACT_VERSION = "1"
LOCATOR_UNITS_VERSION = "1"
BOUNDARY_ARTIFACT_VERSION = "1"
GENERATED_CHAPTERS_VERSION = "1"


def recipe_hash(**parts: Any) -> str:
    """Return a stable recipe identity for an asynchronous chapter job."""

    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _non_empty(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} must be non-empty")
    return result


@dataclass(frozen=True)
class AgendaCandidate:
    """One accepted agenda item with immutable source evidence."""

    index: int
    title: str
    kind: str
    line_start: int
    line_end: int
    evidence_text: str
    locator_cues: tuple[str, ...] = ()
    display_ref: str | None = None
    status: str = "accepted"
    evidence_quote: str | None = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("agenda candidate index must be non-negative")
        _non_empty(self.title, "agenda candidate title")
        _non_empty(self.kind, "agenda candidate kind")
        if self.line_start < 1 or self.line_end < self.line_start:
            raise ValueError("agenda candidate source span is invalid")
        _non_empty(self.evidence_text, "agenda candidate evidence_text")
        if any(not str(cue).strip() for cue in self.locator_cues):
            raise ValueError("agenda candidate locator_cues must not contain empty values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "kind": self.kind,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "evidence_text": self.evidence_text,
            "locator_cues": list(self.locator_cues),
            "display_ref": self.display_ref,
            "status": self.status,
            "evidence_quote": self.evidence_quote,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgendaCandidate:
        return cls(
            index=int(value.get("index", -1)),
            title=str(value.get("title") or ""),
            kind=str(value.get("kind") or "substantive_action"),
            line_start=int(value.get("line_start", 0)),
            line_end=int(value.get("line_end", 0)),
            evidence_text=str(value.get("evidence_text") or ""),
            locator_cues=tuple(str(cue) for cue in (value.get("locator_cues") or ())),
            display_ref=value.get("display_ref") or None,
            status=str(value.get("status") or "accepted"),
            evidence_quote=value.get("evidence_quote") or None,
        )


@dataclass(frozen=True)
class AgendaCandidatesArtifact:
    episode_uid: str
    source_hash: str
    model: str
    prompt_version: str
    recipe: str
    items: tuple[AgendaCandidate, ...] = ()
    status: str = "completed"
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    version: str = AGENDA_ARTIFACT_VERSION

    def __post_init__(self) -> None:
        _non_empty(self.episode_uid, "episode_uid")
        _non_empty(self.source_hash, "agenda source_hash")
        _non_empty(self.model, "agenda model")
        _non_empty(self.prompt_version, "agenda prompt_version")
        _non_empty(self.recipe, "agenda recipe")
        indices = [item.index for item in self.items]
        if len(indices) != len(set(indices)):
            raise ValueError("agenda candidate indices must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "episode_uid": self.episode_uid,
            "source_hash": self.source_hash,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "recipe": self.recipe,
            "status": self.status,
            "items": [item.to_dict() for item in self.items],
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgendaCandidatesArtifact:
        return cls(
            episode_uid=str(value.get("episode_uid") or ""),
            source_hash=str(value.get("source_hash") or ""),
            model=str(value.get("model") or ""),
            prompt_version=str(value.get("prompt_version") or ""),
            recipe=str(value.get("recipe") or ""),
            items=tuple(AgendaCandidate.from_dict(item) for item in value.get("items") or ()),
            status=str(value.get("status") or "completed"),
            diagnostics=(
                value.get("diagnostics") if isinstance(value.get("diagnostics"), Mapping) else {}
            ),
            version=str(value.get("version") or AGENDA_ARTIFACT_VERSION),
        )


@dataclass(frozen=True)
class LocatorUnitArtifact:
    id: str
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if (
            not self.id
            or not math.isfinite(self.start)
            or not math.isfinite(self.end)
            or self.start < 0
            or self.end < self.start
        ):
            raise ValueError("invalid locator unit")
        _non_empty(self.text, "locator unit text")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "start": self.start, "end": self.end, "text": self.text}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> LocatorUnitArtifact:
        try:
            start = float(value.get("start", 0.0))
            end = float(value.get("end", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid locator unit timestamps: {exc}") from exc
        return cls(
            id=str(value.get("id") or ""),
            start=start,
            end=end,
            text=str(value.get("text") or ""),
        )


@dataclass(frozen=True)
class BoundaryResultArtifact:
    episode_uid: str
    agenda_recipe: str
    transcript_hash: str
    model: str
    prompt_version: str
    recipe: str
    anchors: tuple[Mapping[str, Any], ...] = ()
    status: str = "completed"
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    version: str = BOUNDARY_ARTIFACT_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("episode_uid", self.episode_uid),
            ("agenda_recipe", self.agenda_recipe),
            ("transcript_hash", self.transcript_hash),
            ("model", self.model),
            ("prompt_version", self.prompt_version),
            ("recipe", self.recipe),
        ):
            _non_empty(value, name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "episode_uid": self.episode_uid,
            "agenda_recipe": self.agenda_recipe,
            "transcript_hash": self.transcript_hash,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "recipe": self.recipe,
            "status": self.status,
            "anchors": [dict(anchor) for anchor in self.anchors],
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BoundaryResultArtifact:
        return cls(
            episode_uid=str(value.get("episode_uid") or ""),
            agenda_recipe=str(value.get("agenda_recipe") or ""),
            transcript_hash=str(value.get("transcript_hash") or ""),
            model=str(value.get("model") or ""),
            prompt_version=str(value.get("prompt_version") or ""),
            recipe=str(value.get("recipe") or ""),
            anchors=tuple(a for a in value.get("anchors") or () if isinstance(a, Mapping)),
            status=str(value.get("status") or "completed"),
            diagnostics=(
                value.get("diagnostics") if isinstance(value.get("diagnostics"), Mapping) else {}
            ),
            version=str(value.get("version") or BOUNDARY_ARTIFACT_VERSION),
        )


def artifact_key(kind: str, episode_uid: str, recipe: str) -> str:
    """Return the stable B2 key for one generated chapter artifact.

    The recipe suffix uses only the first 12 hex characters (48 bits), which is far more than
    enough collision resistance for a per-episode namespace.  Truncating keeps B2 paths readable
    and avoids excessively long object keys in logs and dashboards.
    """

    kind = _non_empty(kind, "artifact kind").replace("/", "-")
    uid = _non_empty(episode_uid, "episode_uid").replace("/", "-")
    recipe = _non_empty(recipe, "recipe")[:12]
    return f"state/generated_chapters/{kind}/{uid}-{recipe}.json"


__all__ = [
    "AGENDA_ARTIFACT_VERSION",
    "BOUNDARY_ARTIFACT_VERSION",
    "GENERATED_CHAPTERS_VERSION",
    "LOCATOR_UNITS_VERSION",
    "AgendaCandidate",
    "AgendaCandidatesArtifact",
    "BoundaryResultArtifact",
    "LocatorUnitArtifact",
    "artifact_key",
    "recipe_hash",
]
