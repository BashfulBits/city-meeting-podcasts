"""Shared, fail-closed GitHub issue plumbing for weekly human review.

Feature modules retain ownership of candidate selection and durable state.  This module owns the
small contract shared by those features: versioned issue envelopes, bounded issue bodies,
exactly-one checkbox parsing, and portable batch manifests.  Keeping model/provider output out of
the control metadata is intentional: review issue bodies are untrusted and every adapter must look
a candidate up in its durable ledger before applying a decision.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

SCHEMA_VERSION = 1
MANAGED_LABEL = "agent:weekly-review"
ENVELOPE_RE = re.compile(r"<!-- citypods-review: ([A-Za-z0-9_=-]+) -->")
DECISION_BLOCK_START = "<!-- citypods-review-decisions:start -->"
DECISION_BLOCK_END = "<!-- citypods-review-decisions:end -->"
GITHUB_BODY_LIMIT_BYTES = 65_536
SAFE_BODY_LIMIT_BYTES = 60_000


class PublicationStatus(StrEnum):
    PUBLISHED = "published"
    NO_CANDIDATES = "no_candidates"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class ReviewChild:
    """One actionable review child, identified independently of its GitHub issue number."""

    candidate_id: str
    title: str
    body_file: str
    decisions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewBatch:
    """A native-parent/child review batch emitted by a feature adapter."""

    family: str
    label: str
    parent_title: str
    parent_body_file: str
    children: tuple[ReviewChild, ...] = ()
    status: PublicationStatus = PublicationStatus.PUBLISHED
    reasons: tuple[str, ...] = ()
    artifact_url: str = ""
    version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True)
class ReviewTicket:
    """A rolling, single-issue decision surface (used by tournament champion routing)."""

    family: str
    label: str
    title: str
    body_file: str
    status: PublicationStatus = PublicationStatus.PUBLISHED
    reasons: tuple[str, ...] = ()
    version: int = SCHEMA_VERSION


class ReviewAdapter(Protocol):
    """Feature-owned bridge between an issue decision and durable state."""

    family: str

    def package(self, out_dir: Path) -> ReviewBatch | ReviewTicket: ...

    def apply(self, *, issue_number: int, issue_url: str, actor: str, body: str) -> dict: ...


def encode_envelope(*, family: str, candidate_id: str = "", surface: str = "child") -> str:
    """Return a compact, versioned routing marker appended by the shared publisher."""
    payload = json.dumps(
        {
            "v": SCHEMA_VERSION,
            "family": family,
            "candidate_id": candidate_id,
            "surface": surface,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"<!-- citypods-review: {encoded} -->"


def decode_envelope(body: str) -> dict:
    """Read the last publisher marker, avoiding marker-shaped untrusted feature content."""
    matches = ENVELOPE_RE.findall(body)
    if not matches:
        raise ValueError("missing citypods review envelope")
    try:
        value = json.loads(base64.urlsafe_b64decode(matches[-1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid citypods review envelope") from exc
    if not isinstance(value, dict) or value.get("v") != SCHEMA_VERSION:
        raise ValueError("unsupported citypods review envelope")
    if not isinstance(value.get("family"), str) or not value["family"]:
        raise ValueError("citypods review envelope has no family")
    return value


def append_envelope(
    body: str, *, family: str, candidate_id: str = "", surface: str = "child"
) -> str:
    """Append the authoritative marker after all feature-rendered, potentially untrusted text."""
    return (
        body.rstrip()
        + "\n\n"
        + encode_envelope(family=family, candidate_id=candidate_id, surface=surface)
        + "\n"
    )


def append_bounded_envelope(
    body: str,
    *,
    family: str,
    candidate_id: str = "",
    surface: str = "child",
    limit: int = SAFE_BODY_LIMIT_BYTES,
) -> tuple[str, bool]:
    """Bound feature content while reserving space for an authoritative trailing envelope."""
    envelope_bytes = len(
        encode_envelope(family=family, candidate_id=candidate_id, surface=surface).encode("utf-8")
    )
    bounded, truncated = bounded_body(body, limit=limit - envelope_bytes - 3)
    rendered = append_envelope(bounded, family=family, candidate_id=candidate_id, surface=surface)
    if len(rendered.encode("utf-8")) > limit:
        raise ValueError("review envelope does not fit within the configured issue-body limit")
    return rendered, truncated


def bounded_body(body: str, *, limit: int = SAFE_BODY_LIMIT_BYTES) -> tuple[str, bool]:
    """Bound a body by UTF-8 bytes, preserving valid text and a visible artifact hint."""
    encoded = body.encode("utf-8")
    if len(encoded) <= limit:
        return body, False
    suffix = (
        "\n\n*(Truncated for GitHub's 64KB limit; full body is in this workflow run artifact.)*\n"
    )
    room = max(0, limit - len(suffix.encode("utf-8")))
    prefix = encoded[:room].decode("utf-8", "ignore")
    newline = prefix.rfind("\n")
    if newline > 0:
        prefix = prefix[:newline]
    return prefix.rstrip() + suffix, True


def render_decision_block(choices: tuple[str, ...]) -> str:
    """Render the fixed, publisher-owned task-list block for one review child."""
    return "\n".join(
        [DECISION_BLOCK_START, *(f"- [ ] {choice}" for choice in choices), DECISION_BLOCK_END]
    )


def _decision_block(body: str) -> str:
    """Return the final fixed decision block, excluding any provider-rendered preamble."""
    normalized = body.replace("\r\n", "\n")
    start = normalized.rfind(DECISION_BLOCK_START)
    if start == -1:
        return ""
    end = normalized.find(DECISION_BLOCK_END, start)
    if end == -1:
        return ""
    return normalized[start + len(DECISION_BLOCK_START) : end]


def checked_decisions(body: str, choices: tuple[str, ...]) -> tuple[str, ...]:
    """Return checked choices only from the fixed, publisher-owned task-list block."""
    block = _decision_block(body)
    return tuple(
        choice for choice in choices if re.search(rf"(?mi)^- \[x\] {re.escape(choice)}\s*$", block)
    )


def clear_decision_block(body: str) -> str:
    """Clear checked boxes in the final publisher-owned decision block only."""
    normalized = body.replace("\r\n", "\n")
    start = normalized.rfind(DECISION_BLOCK_START)
    if start == -1:
        return normalized
    end = normalized.find(DECISION_BLOCK_END, start)
    if end == -1:
        return normalized
    block_end = end + len(DECISION_BLOCK_END)
    block = re.sub(r"(?m)^- \[x\] ", "- [ ] ", normalized[start:block_end], flags=re.I)
    return normalized[:start] + block + normalized[block_end:]


def require_one_decision(body: str, choices: tuple[str, ...]) -> str:
    selected = checked_decisions(body, choices)
    if len(selected) != 1:
        raise ValueError("select exactly one review decision")
    return selected[0]


def write_batch(path: Path, batch: ReviewBatch) -> None:
    path.write_text(json.dumps(batch.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def publication_summary(
    *, status: PublicationStatus, selected: int, published: int, reasons=()
) -> str:
    """Stable machine-readable result for workflow summaries and tests."""
    return json.dumps(
        {
            "status": status.value,
            "selected": selected,
            "published": published,
            "reasons": list(reasons),
        },
        sort_keys=True,
    )
