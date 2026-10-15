"""Append-only storage for completed P3 model observations.

P3 model output is intermediate evidence, not a maintainer disposition. Keep these objects apart
from ``remedy_ledger`` events while sharing its source-scoped storage and maintenance lease.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from citypods.remedy_ledger import REMEDY_LEASE_KEY, _source_segment
from citypods.remedy_policy import canonical_hash

P3_OBSERVATION_PREFIX = "state/remedy/p3-observations/"
P3_STAGES = frozenset({"initial", "adjudication"})
P3_ROLES = frozenset({"sol", "glm_flash", "deepseek", "jev"})
_ROLE_BINDINGS = {
    ("initial", "sol"): ("beatapi_gpt_6_1_sol_free", "openai/gpt-6.1-sol"),
    ("initial", "glm_flash"): (
        "orcarouter_zai_glm_5_3_flash_free",
        "zai/glm-5.3-flash",
    ),
    ("adjudication", "deepseek"): (
        "beatapi_deepseek_v4_1_flash_free",
        "deepseek/deepseek-v4.1-flash",
    ),
    ("adjudication", "jev"): ("beatapi_jev_1_13_free", "typesafe/jev-1.13"),
}
_HASH = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class P3Observation:
    """One completed route response, bound to its input and Worker recipe."""

    schema_version: int
    observation_id: str
    finding_id: str
    source_key: str
    stage: Literal["initial", "adjudication"]
    role: Literal["sol", "glm_flash", "deepseek", "jev"]
    recipe_hash: str
    request_hash: str
    evidence_hash: str
    config_hash: str
    policy_hash: str
    route_id: str
    logical_model: str
    upstream_model: str
    reasoning_level: str | None
    response_hash: str
    response: dict[str, Any]
    observed_at: str

    def payload(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


def _observation_content(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key != "observation_id"}


def observation_id(payload: dict[str, Any]) -> str:
    return canonical_hash(_observation_content(payload))


def parse_observation(payload: Any) -> P3Observation:
    """Validate a stored observation and all of its content-derived identities."""
    fields = set(P3Observation.__dataclass_fields__)
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("P3 observation: missing or unknown fields")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("P3 observation: unsupported schema version")
    for name in (
        "finding_id",
        "source_key",
        "role",
        "stage",
        "route_id",
        "logical_model",
        "upstream_model",
        "recipe_hash",
        "request_hash",
        "evidence_hash",
        "config_hash",
        "policy_hash",
        "response_hash",
        "observed_at",
    ):
        if not isinstance(payload[name], str) or not payload[name].strip():
            raise ValueError(f"P3 observation: {name} must be nonempty text")
    if payload["stage"] not in P3_STAGES or payload["role"] not in P3_ROLES:
        raise ValueError("P3 observation: invalid stage or role")
    expected_roles = {"sol", "glm_flash"} if payload["stage"] == "initial" else {"deepseek", "jev"}
    if payload["role"] not in expected_roles:
        raise ValueError("P3 observation: role is not valid for the stage")
    if (payload["route_id"], payload["logical_model"]) != _ROLE_BINDINGS[
        (payload["stage"], payload["role"])
    ]:
        raise ValueError("P3 observation: route or logical model does not match its role")
    _source_segment(payload["source_key"])
    for name in (
        "recipe_hash",
        "request_hash",
        "evidence_hash",
        "config_hash",
        "policy_hash",
        "response_hash",
        "observation_id",
    ):
        if not isinstance(payload[name], str) or not _HASH.fullmatch(payload[name]):
            raise ValueError(f"P3 observation: {name} must be lowercase SHA-256")
    if payload["reasoning_level"] is not None and not isinstance(payload["reasoning_level"], str):
        raise ValueError("P3 observation: reasoning_level must be text or null")
    if not isinstance(payload["response"], dict):
        raise ValueError("P3 observation: response must be a JSON object")
    try:
        json.dumps(payload["response"], allow_nan=False)
        response_digest = canonical_hash(payload["response"])
    except (TypeError, ValueError) as exc:
        raise ValueError("P3 observation: response must contain only finite JSON values") from exc
    if response_digest != payload["response_hash"]:
        raise ValueError("P3 observation: response hash mismatch")
    try:
        timestamp = datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("P3 observation: invalid observed_at timestamp") from exc
    if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
        raise ValueError("P3 observation: observed_at must be UTC")
    if observation_id(payload) != payload["observation_id"]:
        raise ValueError("P3 observation: content hash mismatch")
    return P3Observation(**payload)


def make_observation(
    *,
    finding_id: str,
    source_key: str,
    stage: Literal["initial", "adjudication"],
    role: Literal["sol", "glm_flash", "deepseek", "jev"],
    recipe_hash: str,
    request_hash: str,
    evidence_hash: str,
    config_hash: str,
    policy_hash: str,
    route_id: str,
    logical_model: str,
    upstream_model: str,
    reasoning_level: str | None,
    response: dict[str, Any],
    observed_at: datetime | None = None,
) -> P3Observation:
    """Build a validated immutable observation when a completed Worker result is collected."""
    timestamp = observed_at or datetime.now(UTC)
    if timestamp.utcoffset() is None or timestamp.utcoffset().total_seconds() != 0:
        raise ValueError("observed_at must be UTC")
    payload = {
        "schema_version": 1,
        "observation_id": "0" * 64,
        "finding_id": finding_id,
        "source_key": source_key,
        "stage": stage,
        "role": role,
        "recipe_hash": recipe_hash,
        "request_hash": request_hash,
        "evidence_hash": evidence_hash,
        "config_hash": config_hash,
        "policy_hash": policy_hash,
        "route_id": route_id,
        "logical_model": logical_model,
        "upstream_model": upstream_model,
        "reasoning_level": reasoning_level,
        "response_hash": canonical_hash(response),
        "response": response,
        "observed_at": timestamp.isoformat().replace("+00:00", "Z"),
    }
    payload["observation_id"] = observation_id(payload)
    return parse_observation(payload)


def observation_key(observation: P3Observation | dict[str, Any]) -> str:
    payload = observation.payload() if isinstance(observation, P3Observation) else observation
    parsed = parse_observation(payload)
    source = _source_segment(parsed.source_key)
    return (
        f"{P3_OBSERVATION_PREFIX}{source}/{canonical_hash(parsed.finding_id)}/"
        f"{parsed.observation_id}.json"
    )


def _assert_lease(lease) -> None:
    if lease is None or getattr(lease, "key", None) != REMEDY_LEASE_KEY:
        raise ValueError("P3 observation append requires the remedy maintenance lease")
    if not getattr(getattr(lease, "storage", None), "cas_capable", False):
        raise ValueError("P3 observation append requires CAS-capable storage")
    lease.assert_held()


def _read_observation(storage, key: str, local_path: Path) -> P3Observation | None:
    if not storage.get_file(key, local_path):
        return None
    try:
        payload = json.loads(
            local_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
        parsed = parse_observation(payload)
        if observation_key(parsed) != key:
            raise ValueError("path does not match observation content")
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid stored P3 observation {key!r}: {exc}") from exc
    return parsed


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"P3 observation JSON: duplicate key {key!r}")
        result[key] = value
    return result


def append_observation(storage, observation: P3Observation | dict[str, Any], *, lease) -> str:
    """Append a completed response idempotently under the shared remedy maintenance lease."""
    payload = observation.payload() if isinstance(observation, P3Observation) else observation
    parsed = parse_observation(payload)
    key = observation_key(parsed)
    _assert_lease(lease)
    with TemporaryDirectory(prefix="citypods-remedy-p3-") as directory:
        local_path = Path(directory) / "observation.json"
        prefix = f"{P3_OBSERVATION_PREFIX}{_source_segment(parsed.source_key)}/"
        finding_prefix = f"{prefix}{canonical_hash(parsed.finding_id)}/"
        for existing_key, _modified in storage.list_objects(finding_prefix):
            existing = _read_observation(storage, existing_key, local_path)
            if existing is None:
                raise ValueError(f"listed P3 observation disappeared: {existing_key}")
            if (
                existing.stage == parsed.stage
                and existing.role == parsed.role
                and existing.recipe_hash == parsed.recipe_hash
            ):
                same_response = {
                    name: value
                    for name, value in existing.payload().items()
                    if name not in {"observation_id", "observed_at"}
                } == {
                    name: value
                    for name, value in parsed.payload().items()
                    if name not in {"observation_id", "observed_at"}
                }
                if same_response:
                    return observation_key(existing)
                raise ValueError("conflicting completed P3 result for stage, role, and recipe hash")
        previous = _read_observation(storage, key, local_path)
        if previous is not None:
            if previous != parsed:
                raise ValueError(f"existing P3 observation differs: {key}")
            return key
        local_path.write_text(
            json.dumps(parsed.payload(), sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        _assert_lease(lease)
        storage.put_file(key, local_path, "application/json")
        _assert_lease(lease)
        saved = _read_observation(storage, key, local_path)
        if saved != parsed:
            raise ValueError(f"P3 observation read-back failed: {key}")
    return key


def load_observations(
    storage, *, source_key: str, finding_id: str, lease
) -> tuple[P3Observation, ...]:
    """Load and validate one finding's immutable P3 responses in deterministic order."""
    source = _source_segment(source_key)
    if not isinstance(finding_id, str) or not finding_id.strip():
        raise ValueError("finding_id: expected a nonempty identifier")
    _assert_lease(lease)
    prefix = f"{P3_OBSERVATION_PREFIX}{source}/{canonical_hash(finding_id)}/"
    pattern = re.compile(re.escape(prefix) + r"[0-9a-f]{64}\.json")
    observations = []
    with TemporaryDirectory(prefix="citypods-remedy-p3-load-") as directory:
        local_path = Path(directory) / "observation.json"
        for key, _modified in storage.list_objects(prefix):
            if not isinstance(key, str) or not pattern.fullmatch(key):
                raise ValueError(f"invalid P3 observation path: {key!r}")
            observation = _read_observation(storage, key, local_path)
            if observation is None:
                raise ValueError(f"listed P3 observation disappeared: {key}")
            if observation.source_key != source or observation.finding_id != finding_id:
                raise ValueError(f"P3 observation identity does not match path: {key}")
            observations.append(observation)
        _assert_lease(lease)
    return tuple(sorted(observations, key=lambda item: (item.stage, item.role, item.recipe_hash)))
