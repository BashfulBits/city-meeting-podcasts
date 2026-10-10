"""Pure source-local decision memory. Claims here are not production authorization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from citypods.bodies import body_key
from citypods.config import _policy_approval
from citypods.remedy_policy import canonical_hash

STATES = frozenset(
    {"covered", "excluded", "proposed", "in_review", "refinement_needed", "blocked", "resolved"}
)
DISPOSITIONS = frozenset({"assigned", "excluded", "watch", "not_pursued", "rejected"})
FIELDS = frozenset(
    "schema_version city source_key policy_id normalized_label decision_id event_id "
    "parent_event_ids occurred_at actor_kind actor_id state evidence_hash config_hash policy_hash "
    "recording_refs provenance rationale artifact_ref approval_ref "
    "external_delivery_id disposition".split()
)


def _text(value: Any, name: str, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: expected nonempty string")
    return value


def _hash(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError(f"{name}: expected lowercase SHA256")
    return value


def _sequence(value: Any, name: str) -> list | tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name}: expected sequence")
    return value


def decision_id(city: str, source_key: str, policy_id: str, label: str) -> str:
    for name, value in (
        ("city", city),
        ("source_key", source_key),
        ("policy_id", policy_id),
        ("label", label),
    ):
        _text(value, name)
    identity = dict(
        city=city, source_key=source_key, policy_id=policy_id, normalized_label=body_key(label)
    )
    for name, value in identity.items():
        _text(value, name)
    return canonical_hash(identity)


def _canonical(payload: dict) -> dict:
    result = {key: value for key, value in payload.items() if key != "event_id"}
    for name in ("parent_event_ids", "recording_refs", "provenance"):
        result[name] = sorted(result[name], key=canonical_hash)
    return result


def event_id(payload: dict) -> str:
    return canonical_hash(_canonical(payload))


@dataclass(frozen=True)
class RecordingRef:
    source_key: str
    uid: str | None
    provider_guid: str | None


@dataclass(frozen=True)
class Provenance:
    role: str
    route_id: str
    model_family: str
    reasoning_level: str
    report_hash: str


@dataclass(frozen=True)
class RemedyEvent:
    schema_version: int
    city: str
    source_key: str
    policy_id: str
    normalized_label: str
    decision_id: str
    event_id: str
    parent_event_ids: tuple[str, ...]
    occurred_at: str
    actor_kind: str
    actor_id: str
    state: str
    evidence_hash: str
    config_hash: str
    policy_hash: str
    recording_refs: tuple[RecordingRef, ...]
    provenance: tuple[Provenance, ...]
    rationale: str
    artifact_ref: str | None
    approval_ref: str | None
    external_delivery_id: str | None
    disposition: str | None

    def payload(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def parse_event(payload: dict) -> RemedyEvent:
    """Validate declarations only; never authenticate a caller or fetch a reference."""
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError("event: missing or unknown fields")
    p = dict(payload)
    if type(p["schema_version"]) is not int or p["schema_version"] != 1:
        raise ValueError("schema_version: expected integer 1")
    for name in ("city", "source_key", "policy_id", "normalized_label", "actor_id", "rationale"):
        _text(p[name], name)
    if body_key(p["normalized_label"]) != p["normalized_label"]:
        raise ValueError("normalized_label: expected body_key normalization")
    if len(p["rationale"]) > 4000:
        raise ValueError("rationale: exceeds 4000 characters")
    if p["actor_kind"] not in ("maintainer", "automation", "system"):
        raise ValueError("actor_kind: invalid actor")
    if not isinstance(p["state"], str) or p["state"] not in STATES:
        raise ValueError("state: invalid state")
    if p["disposition"] is not None and (
        not isinstance(p["disposition"], str) or p["disposition"] not in DISPOSITIONS
    ):
        raise ValueError("disposition: invalid outcome")
    for name in ("decision_id", "event_id", "evidence_hash", "config_hash", "policy_hash"):
        _hash(p[name], name)
    if p["decision_id"] != decision_id(
        p["city"], p["source_key"], p["policy_id"], p["normalized_label"]
    ):
        raise ValueError("decision_id: identity mismatch")
    _text(p["occurred_at"], "occurred_at")
    try:
        timestamp = datetime.fromisoformat(p["occurred_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("occurred_at: invalid ISO timestamp") from exc
    if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
        raise ValueError("occurred_at: expected aware UTC timestamp")
    parents = _sequence(p["parent_event_ids"], "parent_event_ids")
    for parent in parents:
        _hash(parent, "parent_event_ids")
    if len(set(parents)) != len(parents) or p["event_id"] in parents:
        raise ValueError("parent_event_ids: duplicate or self parent")
    p["parent_event_ids"] = tuple(sorted(parents))
    refs = []
    for ref in _sequence(p["recording_refs"], "recording_refs"):
        if not isinstance(ref, dict) or set(ref) != {"source_key", "uid", "provider_guid"}:
            raise ValueError("recording_refs: invalid fields")
        if ref["source_key"] != p["source_key"]:
            raise ValueError("recording_refs: source mismatch")
        uid = _text(ref["uid"], "uid", True)
        guid = _text(ref["provider_guid"], "provider_guid", True)
        if uid is None and guid is None:
            raise ValueError("recording_refs: missing identity")
        refs.append(RecordingRef(ref["source_key"], uid, guid))
    p["recording_refs"] = tuple(sorted(refs, key=lambda ref: canonical_hash(ref.__dict__)))
    provenance = []
    for entry in _sequence(p["provenance"], "provenance"):
        names = {"role", "route_id", "model_family", "reasoning_level", "report_hash"}
        if not isinstance(entry, dict) or set(entry) != names:
            raise ValueError("provenance: invalid fields")
        for name in names:
            _hash(entry[name], name) if name == "report_hash" else _text(entry[name], name)
        provenance.append(Provenance(**entry))
    p["provenance"] = tuple(sorted(provenance, key=lambda item: canonical_hash(item.__dict__)))
    for name in ("artifact_ref", "approval_ref", "external_delivery_id"):
        _text(p[name], name, True)
        if name != "external_delivery_id" and p[name] is not None:
            _policy_approval(p[name])
    outcome = p["disposition"]
    if (
        p["state"] == "covered" or outcome in {"assigned", "excluded", "not_pursued", "watch"}
    ) and (p["approval_ref"] is None):
        raise ValueError("approval_ref: required for approved outcome")
    if (p["state"] == "covered" or outcome == "assigned") and not refs:
        raise ValueError("recording_refs: required for assignment")
    if outcome == "watch" and p["state"] != "blocked":
        raise ValueError("watch: must remain pending in blocked state")
    if outcome == "rejected" and p["state"] != "refinement_needed":
        raise ValueError("rejected: requires refinement_needed")
    result = RemedyEvent(**p)
    if event_id(result.payload()) != result.event_id:
        raise ValueError("event_id: content mismatch")
    return result


@dataclass(frozen=True)
class DecisionState:
    decision_id: str | None
    source_key: str | None
    state: str
    disposition: str | None
    events: tuple[RemedyEvent, ...]
    tip_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]


def fold_events(events) -> DecisionState:
    """Reconstruct causal history conservatively, without production side effects."""
    validated = [parse_event(e.payload() if isinstance(e, RemedyEvent) else e) for e in events]
    retained = tuple(sorted({e.event_id: e for e in validated}.values(), key=lambda e: e.event_id))
    if not retained:
        return DecisionState(None, None, "blocked", None, (), (), ("empty history",))
    if (
        len(
            {
                (e.decision_id, e.city, e.source_key, e.policy_id, e.normalized_label)
                for e in retained
            }
        )
        != 1
    ):
        raise ValueError("history: multiple decision identities")
    diagnostics = set()
    aliases = {e.event_id: e.event_id for e in retained}
    deliveries = {}
    for e in retained:
        if e.external_delivery_id is not None:
            key = (e.source_key, e.actor_kind, e.actor_id, e.external_delivery_id)
            deliveries.setdefault(key, []).append(e)
    for group in deliveries.values():
        semantics = []
        for e in group:
            value = _canonical(e.payload())
            value.pop("occurred_at")
            semantics.append(canonical_hash(value))
        if len(set(semantics)) != 1:
            diagnostics.add("conflicting delivery content")
        else:
            representative = min(e.event_id for e in group)
            for e in group:
                aliases[e.event_id] = representative
    nodes = {aliases[e.event_id]: e for e in reversed(retained)}
    parents = {}
    for key, e in nodes.items():
        parents[key] = {aliases.get(parent, parent) for parent in e.parent_event_ids}
        if any(parent not in nodes for parent in parents[key]):
            diagnostics.add("missing parent")
    roots = {key for key, refs in parents.items() if not refs}
    if len(roots) != 1:
        diagnostics.add("multiple or absent roots")
    # Iterative topological elimination avoids recursion limits on long histories.
    remaining = set(nodes)
    done = set()
    while remaining:
        ready = {key for key in remaining if parents[key] <= done}
        if not ready:
            if "missing parent" not in diagnostics:
                diagnostics.add("cycle")
            break
        done.update(ready)
        remaining.difference_update(ready)
    consumed = set().union(*parents.values())
    tips = tuple(sorted(set(nodes) - consumed))
    if len(tips) != 1:
        diagnostics.add("multiple or absent tips")
    current = nodes[tips[0]] if len(tips) == 1 and not diagnostics else None
    first = retained[0]
    return DecisionState(
        first.decision_id,
        first.source_key,
        current.state if current else "blocked",
        current.disposition if current else None,
        retained,
        tips,
        tuple(sorted(diagnostics)),
    )
