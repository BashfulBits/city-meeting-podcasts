"""The probe set: human-verified items mixed invisibly into normal judge packets (review/53 PR6).

``evals/judge/<task>/gold.json`` is a plain list of items a human has settled, empty at the start.
A probe is asked of a judge like any other question (``sample: "probe"``), so a judge cannot tell it
from live work; its answer is compared with the verdict the human gave. Results are reported per
judge, **non-gating** until a task has about ``GATING_ITEMS`` probes: until then they show a judge's
reliability and never decide anything.

A probe's judgments live in the ledger of whichever episode's packet carried it, under the subject
id ``probe:<id>``; they are never a candidate's judgment and no stage reads them but the report.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from citypods.judging.tasks import TIERS, Subject, TaskSpec, make_evidence

PROBE_PREFIX = "probe:"
GATING_ITEMS = 20
MAX_EVIDENCE_CHARS = 12_000
DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "evals" / "judge"
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


@dataclass(frozen=True)
class Probe:
    id: str
    task: str
    question_id: str
    tier: str
    evidence: str
    verdict: bool
    source: str = ""
    added: str = ""

    @property
    def subject_id(self) -> str:
        return f"{PROBE_PREFIX}{self.id}"

    @property
    def digest(self) -> str:
        return make_evidence(self.tier, self.evidence).digest  # type: ignore[arg-type]


def is_probe_subject(subject_id: str) -> bool:
    return str(subject_id).startswith(PROBE_PREFIX)


def parse_probes(task: TaskSpec, rows: Any) -> list[Probe]:
    """Validate a gold list for ``task``; any malformed entry rejects the whole file."""
    if not isinstance(rows, list):
        raise ValueError("gold.json must be a list")
    probes: list[Probe] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("a gold entry must be an object")
        ident = str(row.get("id") or "")
        if not _ID.fullmatch(ident) or ident in seen:
            raise ValueError(f"gold id {ident!r} is missing, malformed or repeated")
        if not ident.startswith(f"{task.name}-"):
            # Ids are one namespace in the report and the ledger (`probe:<id>`): a tag probe and a
            # moment probe must never share one, so each task's ids carry its name.
            raise ValueError(f"gold id {ident!r} must start with {task.name}-")
        seen.add(ident)
        question = next((q for q in task.questions if q.id == row.get("question_id")), None)
        if question is None or question.kind not in ("validate", "gate"):
            raise ValueError(f"gold {ident}: question must be a binary question of {task.name}")
        if row.get("tier") not in TIERS:
            raise ValueError(f"gold {ident}: unknown tier")
        evidence = row.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"gold {ident}: evidence text is required")
        if len(evidence) > MAX_EVIDENCE_CHARS:
            raise ValueError(f"gold {ident}: evidence is over {MAX_EVIDENCE_CHARS} characters")
        if not isinstance(row.get("verdict"), bool):
            raise ValueError(f"gold {ident}: verdict must be true or false")
        probes.append(
            Probe(
                id=ident,
                task=task.name,
                question_id=question.id,
                tier=row["tier"],
                evidence=evidence,
                verdict=row["verdict"],
                source=str(row.get("source") or ""),
                added=str(row.get("added") or ""),
            )
        )
    return probes


def load_probes(task: TaskSpec, root: Path | None = None) -> list[Probe]:
    path = (root or DEFAULT_ROOT) / task.name / "gold.json"
    if not path.exists():
        return []
    return parse_probes(task, json.loads(path.read_text(encoding="utf-8")))


def probe_subject(probe: Probe, host: Subject) -> Subject:
    """The probe's subject, stored in the ledger of the episode whose packet carries it."""
    return Subject(
        task=probe.task,
        subject_id=probe.subject_id,
        episode_uid=host.episode_uid,
        group=None,
        producer_model=None,
        payload={},
        store=host.store,
    )


def order_key(probe: Probe, salt: str) -> str:
    """A stable order that differs by ``salt`` (the judge and the host), so the probes rotate."""
    return hashlib.sha256(f"{probe.id}|{salt}".encode()).hexdigest()


def gating(probes: list[Probe]) -> bool:
    return len(probes) >= GATING_ITEMS
