"""Safe immutable evidence and deterministic identities for review/48 config proposals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class LimitObservation:
    metric: str
    value: int
    observed_at: str
    provider: str
    account_id: str
    route_id: str | None = None
    scope: str = "unknown"
    source: str = "header"
    run_id: str = ""

    def __post_init__(self):
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value <= 0:
            raise ValueError("limit observations require positive integer values")
        if self.scope not in {"unknown", "route", "provider_account"}:
            raise ValueError("unknown limit scope")
        if self.metric not in {"rpm", "tpm", "rpd", "output_context_limit"}:
            raise ValueError("unsupported observed metric")


@dataclass(frozen=True)
class CatalogEvidence:
    provider: str
    observed_at: str
    complete: bool
    # ID, chat eligibility, free evidence, input context, output context, canonical identity.
    models: tuple[tuple[str, bool, bool | None, int | None, int | None, str | None], ...]


def catalog_digest(evidence: CatalogEvidence) -> str:
    """Time is provenance, not catalog content; listing order cannot change content identity."""
    return digest(
        {
            "provider": evidence.provider,
            "complete": evidence.complete,
            "models": sorted(evidence.models),
        }
    )


@dataclass(frozen=True)
class RouteEvidence:
    provider: str
    upstream_model: str
    account_id: str
    observed_at: str
    catalog_digest: str
    catalog_complete: bool
    free: bool
    input_context_limit: int | None
    output_context_limit: int | None
    model_key: str | None
    verdict: str
    structured_output_method: str | None
    structured_output_verified_on: str | None
    limits: tuple[LimitObservation, ...] = ()
    # Method, outcome, first-byte and completion latency; no untrusted response body.
    methods: tuple[tuple[str, str, float | None, float | None], ...] = ()
    contended: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RouteEvidence:
        value = dict(value)
        value["limits"] = tuple(LimitObservation(**x) for x in value.get("limits") or ())
        value["methods"] = tuple(tuple(x) for x in value.get("methods") or ())
        return cls(**value)


def candidate_digest(evidence: RouteEvidence, lanes: tuple[str, ...]) -> str:
    return digest({"evidence": evidence.to_dict(), "lanes": sorted(lanes)})


def positive_bound(record, keys) -> int | None:
    for key in keys:
        value = record
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def logical_identity(provider, upstream_model, rules, record, routes) -> str | None:
    """Exact configured IDs or a plugin's canonical identity; never AA/brand heuristics."""
    exact = {
        str(r.get("model_key") or r["model"])
        for r in routes
        if r.get("upstream_model") == upstream_model
    }
    canonical = rules.model_identity(upstream_model, record)
    if len(exact) > 1 or (exact and canonical and canonical not in exact):
        return None
    return next(iter(exact)) if exact else canonical
