"""Trusted command binding and interrupted-write recovery, with fake API/storage only."""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from citypods.remedy_actions import (
    COMMAND,
    EVIDENCE_FIELDS,
    record_decision_action,
    validate_decision_action,
)
from citypods.remedy_ledger import fold_events
from tests.test_remedy_ledger import FakeEventStorage, FakeRemedyLease, event

NOW = datetime(2026, 10, 10, 1, tzinfo=UTC)


def inputs(**changes):
    original = event(**changes)
    keys = set(original) - {
        "event_id",
        "parent_event_ids",
        "occurred_at",
        "actor_kind",
        "actor_id",
        "artifact_ref",
        "approval_ref",
        "external_delivery_id",
        "provenance",
    }
    action = {key: original[key] for key in keys}
    action.update(action="decide", expected_parent_event_ids=original["parent_event_ids"])
    comment = dict(
        id=123,
        html_url="https://github.com/example/repo/issues/1#issuecomment-123",
        created_at="2026-10-10T00:00:00Z",
        user={"login": "human"},
        body=COMMAND + json.dumps(action),
    )
    context = dict(
        repository="example/repo",
        comment=comment,
        permission={"permission": "write", "user": {"login": "human"}},
        checked_at="2026-10-10T01:00:00Z",
        evidence={
            **{k: action[k] for k in EVIDENCE_FIELDS - {"observed_at"}},
            "observed_at": "2026-10-10T00:00:00Z",
        },
        decisions={},
        now=NOW,
    )
    return action, context


def submit(action, context):
    context["comment"]["body"] = COMMAND + json.dumps(action)
    return validate_decision_action(action, **context)


def test_identity_and_time_come_from_verified_comment():
    action, context = inputs()
    result = validate_decision_action(action, **context)
    assert result.actor_id == "human"
    assert result.approval_ref == context["comment"]["html_url"]
    assert result.external_delivery_id == "example/repo:issuecomment:123"
    assert result.occurred_at == "2026-10-10T00:00:00+00:00"
    assert result.recording_refs[0].uid == "original UID"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["permission"].update(permission="read"),
        lambda c: c["permission"]["user"].update(login="other"),
        lambda c: c.update(repository="other/repo"),
        lambda c: c.update(checked_at="2026-10-10T00:54:59Z"),
        lambda c: c.update(checked_at="2026-10-10T01:00:01Z"),
        lambda c: c["comment"].update(id=True),
        lambda c: c["comment"].update(created_at="2026-10-10T02:00:00Z"),
        lambda c: c["comment"].update(body="I agree"),
        lambda c: c["comment"].update(body=COMMAND + '{"action":"decide","action":"reopen"}'),
        lambda c: c["evidence"].update(observed_at="2026-10-08T00:00:00Z"),
        lambda c: c["evidence"].update(source_key="other"),
        lambda c: c["evidence"].update(city="other"),
        lambda c: c["evidence"].update(config_hash="d" * 64),
        lambda c: c["evidence"].update(extra="unexpected"),
    ],
)
def test_denied_or_mismatched_snapshots(mutation):
    action, context = inputs()
    mutation(context)
    with pytest.raises(ValueError):
        validate_decision_action(action, **context)


def test_approved_content_and_unknown_fields_cannot_be_substituted():
    action, context = inputs()
    action["rationale"] = "Different decision"
    with pytest.raises(ValueError, match="content mismatch"):
        validate_decision_action(action, **context)
    action["extra"] = True
    with pytest.raises(ValueError, match="unknown"):
        submit(action, context)


def test_current_parents_and_explicit_reopen():
    action, context = inputs(state="excluded", disposition="excluded")
    prior = validate_decision_action(action, **context)
    action, context = inputs(state="proposed", disposition=None)
    context["decisions"] = {prior.decision_id: fold_events([prior])}
    context["comment"].update(
        id=124, html_url="https://github.com/example/repo/issues/1#issuecomment-124"
    )
    with pytest.raises(ValueError, match="current tips"):
        submit(action, context)
    action["expected_parent_event_ids"] = [prior.event_id]
    with pytest.raises(ValueError, match="reopen required"):
        submit(action, context)
    action["action"] = "reopen"
    reopened = submit(action, context)
    assert fold_events([prior, reopened]).state == "proposed"
    assert reopened.disposition is None


def test_conflicting_history_cannot_be_reconciled_implicitly():
    action, context = inputs()
    prior = validate_decision_action(action, **context)
    other = deepcopy(action)
    other["rationale"] = "Independent root"
    alternative = submit(other, context)
    context["decisions"] = {prior.decision_id: fold_events([prior, alternative])}
    with pytest.raises(ValueError, match="conflicting"):
        submit(action, context)


def record(action, context, storage, lease):
    snapshot = {k: context[k] for k in ("repository", "comment", "permission", "checked_at")}
    return record_decision_action(
        storage,
        action,
        lease=lease,
        trusted_snapshot=snapshot,
        evidence=context["evidence"],
        now=context["now"],
    )


def test_persisted_retry_recovers_without_second_upload():
    action, context = inputs()
    storage = FakeEventStorage()
    lease = FakeRemedyLease()
    key = record(action, context, storage, lease)
    before = len([c for c in storage.calls if c[0] == "write"])
    assert record(action, context, storage, lease) == key
    assert len([c for c in storage.calls if c[0] == "write"]) == before
    action["rationale"] = "Edited command"
    context["comment"]["body"] = COMMAND + json.dumps(action)
    with pytest.raises(ValueError, match="conflicting retry"):
        record(action, context, storage, lease)


def test_missing_lease_prevents_source_reads():
    action, context = inputs()
    storage = FakeEventStorage()
    with pytest.raises(ValueError):
        record(action, context, storage, None)
    assert storage.calls == []


def test_retry_after_lost_acknowledgment_and_after_later_decision():
    class LostAckStorage(FakeEventStorage):
        lose_ack = True

        def put_file(self, key, path, content_type):
            result = super().put_file(key, path, content_type)
            if self.lose_ack:
                self.lose_ack = False
                raise OSError("acknowledgment lost")
            return result

    action, context = inputs()
    storage, lease = LostAckStorage(), FakeRemedyLease()
    with pytest.raises(OSError, match="acknowledgment"):
        record(action, context, storage, lease)
    key = record(action, context, storage, lease)
    assert sum(c[0] == "write" for c in storage.calls) == 1
    prior = validate_decision_action(action, **context)
    next_action, next_context = inputs(state="excluded", disposition="excluded")
    next_action["expected_parent_event_ids"] = [prior.event_id]
    next_context["comment"].update(
        id=124, html_url="https://github.com/example/repo/issues/1#issuecomment-124"
    )
    next_context["comment"]["body"] = COMMAND + json.dumps(next_action)
    record(next_action, next_context, storage, lease)
    assert record(action, context, storage, lease) == key
    assert sum(c[0] == "write" for c in storage.calls) == 2


def test_corrupt_history_and_lease_loss_never_upload():
    action, context = inputs()
    storage, lease = FakeEventStorage(), FakeRemedyLease()
    key = record(action, context, storage, lease)
    storage.objects[key] = b"damaged history"
    with pytest.raises(ValueError):
        record(action, context, storage, lease)
    assert sum(c[0] == "write" for c in storage.calls) == 1
    storage, lease = FakeEventStorage(), FakeRemedyLease()
    lease.lose_at = 2
    with pytest.raises(RuntimeError, match="lease lost"):
        record(action, context, storage, lease)
    assert not any(c[0] == "write" for c in storage.calls)
