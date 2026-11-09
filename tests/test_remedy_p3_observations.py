"""P3 observation storage stays append-only and separate from human decision events."""

from datetime import UTC, datetime

import pytest

from citypods.remedy_p3_observations import (
    P3_OBSERVATION_PREFIX,
    append_observation,
    load_observations,
    make_observation,
    observation_key,
    parse_observation,
)
from citypods.remedy_policy import canonical_hash
from tests.test_remedy_ledger import FakeEventStorage, FakeRemedyLease


def observation(**changes):
    values = dict(
        finding_id="unexpected-body:example:committee",
        source_key="source-a",
        stage="initial",
        role="sol",
        recipe_hash="a" * 64,
        request_hash="b" * 64,
        evidence_hash="c" * 64,
        config_hash="d" * 64,
        policy_hash="e" * 64,
        route_id="beatapi_gpt_6_1_sol_free",
        logical_model="openai/gpt-6.1-sol",
        upstream_model="gpt-6.1-sol",
        reasoning_level="high",
        response={"proposals": [{"finding_id": "f1", "action": "union"}]},
        observed_at=datetime(2026, 10, 15, tzinfo=UTC),
    )
    values.update(changes)
    return make_observation(**values)


def test_observation_is_content_addressed_and_path_safe():
    record = observation()
    payload = record.payload()
    assert record.response_hash == canonical_hash(record.response)
    assert record.observation_id != "0" * 64
    assert observation_key(record) == (
        f"{P3_OBSERVATION_PREFIX}source-a/{canonical_hash(record.finding_id)}/"
        f"{record.observation_id}.json"
    )
    assert parse_observation(payload) == record


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": True},
        {"schema_version": True},
        {"stage": "human_disposition"},
        {"stage": "initial", "role": "jev"},
        {"stage": "adjudication", "role": "sol"},
        {"route_id": "nvidia_deepseek_v4_1_flash_free"},
        {"source_key": "../unsafe"},
        {"request_hash": "A" * 64},
        {"response": {"different": True}},
        {"observed_at": "2026-10-15T01:00:00+01:00"},
    ],
)
def test_observation_rejects_invalid_or_tampered_records(changes):
    record = observation()
    payload = record.payload()
    payload.update(changes)
    with pytest.raises(ValueError):
        parse_observation(payload)


def test_append_load_is_idempotent_and_preserves_original_response():
    storage, lease = FakeEventStorage(), FakeRemedyLease()
    record = observation()
    key = append_observation(storage, record, lease=lease)
    assert append_observation(storage, record, lease=lease) == key
    loaded = load_observations(
        storage, source_key="source-a", finding_id=record.finding_id, lease=lease
    )
    assert loaded == (record,)
    assert storage.objects[key]


def test_same_recipe_and_response_reuses_first_observation_timestamp():
    storage, lease = FakeEventStorage(), FakeRemedyLease()
    first = observation()
    later = observation(observed_at=datetime(2026, 10, 16, tzinfo=UTC))
    assert append_observation(storage, first, lease=lease) == append_observation(
        storage, later, lease=lease
    )
    assert load_observations(
        storage, source_key="source-a", finding_id=first.finding_id, lease=lease
    ) == (first,)


def test_same_recipe_cannot_append_a_conflicting_response():
    storage, lease = FakeEventStorage(), FakeRemedyLease()
    first = observation()
    append_observation(storage, first, lease=lease)
    conflicting = observation(response={"proposals": []})
    with pytest.raises(ValueError, match="conflicting completed P3 result"):
        append_observation(storage, conflicting, lease=lease)
    assert load_observations(
        storage, source_key="source-a", finding_id=first.finding_id, lease=lease
    ) == (first,)


def test_observation_rejects_non_finite_response_values():
    payload = observation().payload()
    payload["response"] = {"score": float("nan")}
    with pytest.raises(ValueError, match="finite JSON values"):
        parse_observation(payload)


@pytest.mark.parametrize("problem", ["missing", "wrong_key", "no_cas", "lost"])
def test_append_requires_live_remedy_lease(problem):
    storage, lease = FakeEventStorage(), FakeRemedyLease()
    if problem == "missing":
        lease = None
    elif problem == "wrong_key":
        lease.key = "maintenance-leases/other.json"
    elif problem == "no_cas":
        lease.storage.cas_capable = False
    else:
        lease.lose_at = 2
    with pytest.raises((ValueError, RuntimeError)):
        append_observation(storage, observation(), lease=lease)
    assert not storage.objects


def test_load_requires_live_remedy_lease_before_listing():
    storage = FakeEventStorage()
    with pytest.raises(ValueError, match="requires the remedy maintenance lease"):
        load_observations(
            storage,
            source_key="source-a",
            finding_id="finding",
            lease=None,
        )
    assert storage.calls == []


def test_load_rejects_corrupt_or_misaddressed_history():
    storage = FakeEventStorage()
    record = observation()
    key = observation_key(record)
    storage.objects[key] = b"not json"
    with pytest.raises(ValueError, match="invalid stored P3 observation"):
        load_observations(
            storage,
            source_key="source-a",
            finding_id=record.finding_id,
            lease=FakeRemedyLease(),
        )
    assert storage.objects[key] == b"not json"


def test_content_addressed_observations_are_never_overwritten():
    storage = FakeEventStorage()
    record = observation()
    key = observation_key(record)
    storage.objects[key] = b'{"conflicting":"content"}'
    with pytest.raises(ValueError):
        append_observation(storage, record, lease=FakeRemedyLease())
    assert storage.objects[key] == b'{"conflicting":"content"}'


def test_v2_stage_binding_and_v1_payload_shape_are_distinct():
    from citypods.audit_remedy import P3_ROLE_PLANS
    from citypods.remedy_p3_observations import make_observation, parse_observation

    plan = P3_ROLE_PLANS["flash-deepseek-v2"]
    kwargs = dict(
        finding_id="finding-1",
        source_key="test-source",
        stage="initial",
        role="deepseek",
        recipe_hash="a" * 64,
        request_hash="b" * 64,
        evidence_hash="c" * 64,
        config_hash="d" * 64,
        policy_hash="e" * 64,
        route_id=plan.reference_routes["deepseek"],
        logical_model=plan.models["deepseek"],
        upstream_model="deepseek-v4.1-flash-free",
        reasoning_level=None,
        response={"ok": True},
    )
    record = make_observation(**kwargs, role_plan=plan.name)
    assert record.schema_version == 2
    assert parse_observation(record.payload()) == record
    assert record.role_plan_hash == plan.fingerprint
    with pytest.raises(ValueError, match="role is not valid"):
        make_observation(**kwargs)  # DeepSeek was not an initial v1 role.
    damaged = record.payload() | {"role_plan_hash": "f" * 64}
    with pytest.raises(ValueError, match="role plan binding"):
        parse_observation(damaged)
