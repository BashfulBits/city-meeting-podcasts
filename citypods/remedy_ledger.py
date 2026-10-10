"""Source-local decision history and guarded persistence; no production caller activation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
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
        p["state"] in {"covered", "excluded"}
        or outcome in {"assigned", "excluded", "not_pursued", "watch"}
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


EVENT_PREFIX = "state/remedy/events/"
REMEDY_LEASE_KEY = "maintenance-leases/remedy.json"


def _source_segment(source_key: str) -> str:
    if not isinstance(source_key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source_key):
        raise ValueError("source_key: unsafe event path segment")
    return source_key


def event_key(event: RemedyEvent | dict) -> str:
    """Validate an event and derive its approved source-local, content-addressed key."""
    parsed = parse_event(event.payload() if isinstance(event, RemedyEvent) else event)
    source = _source_segment(parsed.source_key)
    return f"{EVENT_PREFIX}{source}/{parsed.decision_id}/{parsed.event_id}.json"


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"event JSON: duplicate object key {key!r}")
        result[key] = value
    return result


def _read_event(storage, key: str, local_path: Path) -> RemedyEvent | None:
    if not storage.get_file(key, local_path):
        return None
    try:
        payload = json.loads(
            local_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
        parsed = parse_event(payload)
        if event_key(parsed) != key:
            raise ValueError("path does not match event content")
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"invalid stored decision event {key!r}: {exc}") from exc
    return parsed


def _assert_remedy_lease(lease) -> None:
    if lease is None or getattr(lease, "key", None) != REMEDY_LEASE_KEY:
        raise ValueError("decision append requires the remedy maintenance lease")
    if not getattr(getattr(lease, "storage", None), "cas_capable", False):
        raise ValueError("decision append requires a CAS-capable lease backend")
    lease.assert_held()


def append_event(storage, event: RemedyEvent | dict, *, lease) -> str:
    """Append immutable history under a held lease, verifying retries and saved content.

    The lease serializes writes; it does not authenticate approval or actor claims. B2 does
    not enforce conditional writes. No production entrypoint calls this helper yet.
    """
    parsed = parse_event(event.payload() if isinstance(event, RemedyEvent) else event)
    key = event_key(parsed)
    _assert_remedy_lease(lease)
    with TemporaryDirectory(prefix="citypods-remedy-event-") as directory:
        local_path = Path(directory) / "event.json"
        existing = _read_event(storage, key, local_path)
        if existing is not None:
            if existing != parsed:
                raise ValueError(f"existing decision event differs: {key}")
            return key
        payload = {**_canonical(parsed.payload()), "event_id": parsed.event_id}
        local_path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        _assert_remedy_lease(lease)
        storage.put_file(key, local_path, "application/json")
        saved = _read_event(storage, key, local_path)
        if saved != parsed:
            raise ValueError(f"decision event read-back failed: {key}")
    return key


def load_decisions(storage, *, source_keys) -> dict[str, DecisionState]:
    """Reconstruct only explicit sources; reject damaged history without writing anything."""
    sources = sorted({_source_segment(source) for source in _sequence(source_keys, "source_keys")})
    grouped = {}
    with TemporaryDirectory(prefix="citypods-remedy-load-") as directory:
        local_path = Path(directory) / "event.json"
        for source in sources:
            prefix = f"{EVENT_PREFIX}{source}/"
            pattern = re.compile(re.escape(prefix) + r"[0-9a-f]{64}/[0-9a-f]{64}\.json")
            for key, _modified in storage.list_objects(prefix):
                if not isinstance(key, str) or not pattern.fullmatch(key):
                    raise ValueError(f"invalid listed decision event path: {key!r}")
                parsed = _read_event(storage, key, local_path)
                if parsed is None:
                    raise ValueError(f"listed decision event disappeared: {key}")
                grouped.setdefault(parsed.decision_id, []).append(parsed)
    return {key: fold_events(grouped[key]) for key in sorted(grouped)}
