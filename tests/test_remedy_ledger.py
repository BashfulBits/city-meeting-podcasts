"""Contract checks for pure decision memory, without credentials or external IO."""

from dataclasses import FrozenInstanceError

import pytest

from citypods.remedy_ledger import decision_id, event_id, fold_events, parse_event


def event(**changes):
    payload = dict(
        schema_version=1,
        city="example-tx",
        source_key="source-a",
        policy_id="committee",
        normalized_label="committee",
        decision_id=decision_id("example-tx", "source-a", "committee", "committee"),
        event_id="",
        parent_event_ids=[],
        occurred_at="2026-10-10T00:00:00Z",
        actor_kind="maintainer",
        actor_id="human",
        state="blocked",
        evidence_hash="a" * 64,
        config_hash="b" * 64,
        policy_hash="c" * 64,
        recording_refs=[
            dict(source_key="source-a", uid="original UID", provider_guid="View/9/123")
        ],
        provenance=[],
        rationale="Await another related recording.",
        artifact_ref=None,
        approval_ref="https://github.com/example/repo/issues/1",
        external_delivery_id=None,
        disposition="watch",
    )
    payload.update(changes)
    payload["event_id"] = event_id(payload)
    return payload


def test_watch_pending_and_immutable_without_raw_identity_rewrite():
    parsed = parse_event(event())
    assert parsed.recording_refs[0].uid == "original UID"
    assert parsed.recording_refs[0].provider_guid == "View/9/123"
    assert fold_events([parsed]).disposition == "watch"
    assert fold_events([parsed]).state == "blocked"
    with pytest.raises(FrozenInstanceError):
        parsed.state = "excluded"


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": True},
        {"schema_version": True},
        {"schema_version": "1"},
        {"city": ""},
        {"normalized_label": "Committee"},
        {"decision_id": "d" * 64},
        {"evidence_hash": "A" * 64},
        {"occurred_at": "2026-10-10T00:00:00"},
        {"occurred_at": "2026-10-10T01:00:00+01:00"},
        {"actor_kind": []},
        {"state": []},
        {"disposition": []},
        {"rationale": "x" * 4001},
        {"approval_ref": "http://github.com/example/repo/issues/1"},
        {"artifact_ref": "https://example.org/proof"},
        {"external_delivery_id": ""},
        {"parent_event_ids": ["d" * 64, "d" * 64]},
        {"parent_event_ids": "d" * 64},
        {"recording_refs": [dict(source_key="other", uid="1", provider_guid=None)]},
        {"recording_refs": [dict(source_key="source-a", uid=None, provider_guid=None)]},
        {"recording_refs": [dict(source_key="source-a", uid=1, provider_guid=None)]},
        {"provenance": [dict(role="judge")]},
        {"approval_ref": None},
        {"state": "covered"},
        {"disposition": "rejected"},
    ],
)
def test_strict_rejections(changes):
    with pytest.raises(ValueError):
        parse_event(event(**changes))


def test_content_id_is_checked():
    p = event()
    p["rationale"] = "tampered"
    with pytest.raises(ValueError, match="content mismatch"):
        parse_event(p)


def test_assignment_requires_recording_and_approval():
    with pytest.raises(ValueError, match="recording_refs"):
        parse_event(event(state="covered", disposition="assigned", recording_refs=[]))
    parsed = parse_event(event(state="covered", disposition="assigned"))
    assert fold_events([parsed]).state == "covered"


def test_optional_uid_and_provenance():
    p = event(
        recording_refs=[dict(source_key="source-a", uid=None, provider_guid="original")],
        provenance=[
            dict(
                role="judge",
                route_id="route",
                model_family="family",
                reasoning_level="high",
                report_hash="d" * 64,
            )
        ],
    )
    assert parse_event(p).recording_refs[0].uid is None
    assert parse_event(p).provenance[0].role == "judge"


def test_causal_order_over_timestamp_and_preserved_rejection():
    root = event(state="refinement_needed", disposition="rejected")
    child = event(parent_event_ids=[root["event_id"]], occurred_at="2026-10-09T00:00:00Z")
    a = fold_events([root, child, root])
    assert a == fold_events([child, root])
    assert len(a.events) == 2
    assert a.disposition == "watch"
    assert any(e.disposition == "rejected" for e in a.events)


def test_missing_parent_multiple_roots_tips_and_reconciliation():
    root = event()
    left = event(parent_event_ids=[root["event_id"]], rationale="left")
    right = event(parent_event_ids=[root["event_id"]], rationale="right")
    assert "missing parent" in fold_events([left]).diagnostics
    assert "multiple or absent tips" in fold_events([root, left, right]).diagnostics
    assert "multiple or absent roots" in fold_events([root, event(rationale="other")]).diagnostics
    join = event(parent_event_ids=[left["event_id"], right["event_id"]], rationale="reconciled")
    assert not fold_events([right, join, left, root]).diagnostics


def test_retry_timestamp_alias_and_conflicting_delivery():
    original = event(external_delivery_id="delivery")
    retry = event(external_delivery_id="delivery", occurred_at="2026-10-11T00:00:00Z")
    child = event(
        parent_event_ids=[retry["event_id"]],
        rationale="new related recording",
        recording_refs=[dict(source_key="source-a", uid="second", provider_guid=None)],
    )
    result = fold_events([child, retry, original])
    assert not result.diagnostics
    assert result.disposition == "watch"
    assert len(result.events) == 3
    conflict = event(external_delivery_id="delivery", rationale="contradictory")
    assert "conflicting delivery content" in fold_events([original, conflict]).diagnostics


def test_source_isolation_and_empty_history():
    assert fold_events([]).diagnostics == ("empty history",)
    other = event(
        source_key="source-b",
        decision_id=decision_id("example-tx", "source-b", "committee", "committee"),
        recording_refs=[],
    )
    with pytest.raises(ValueError, match="multiple decision identities"):
        fold_events([event(), other])


def test_unordered_inputs_have_stable_content_id():
    refs = [
        dict(source_key="source-a", uid="1", provider_guid=None),
        dict(source_key="source-a", uid="2", provider_guid=None),
    ]
    assert (
        event(recording_refs=refs)["event_id"]
        == event(recording_refs=list(reversed(refs)))["event_id"]
    )


def test_cycle_is_conservatively_blocked(monkeypatch):
    # Valid hashes make an actual cycle computationally infeasible; exercise the graph guard
    # independently so corrupted future storage cannot select a timestamp-based winner.
    from dataclasses import replace

    import citypods.remedy_ledger as ledger

    first = parse_event(event())
    second = parse_event(event(rationale="other"))
    nodes = {
        first.event_id: replace(first, parent_event_ids=(second.event_id,)),
        second.event_id: replace(second, parent_event_ids=(first.event_id,)),
    }
    monkeypatch.setattr(ledger, "parse_event", lambda payload: nodes[payload["event_id"]])
    result = fold_events([first, second])
    assert result.state == "blocked"
    assert "cycle" in result.diagnostics


@pytest.mark.parametrize("outcome", ["excluded", "not_pursued"])
def test_terminal_outcomes_require_approval(outcome):
    with pytest.raises(ValueError, match="approval_ref"):
        parse_event(event(state="excluded", disposition=outcome, approval_ref=None))
    assert fold_events([event(state="excluded", disposition=outcome)]).disposition == outcome


def test_new_evidence_does_not_infer_reopening():
    initial = event(state="covered", disposition="assigned")
    later = event(
        state="covered",
        disposition="assigned",
        parent_event_ids=[initial["event_id"]],
        occurred_at="2027-10-10T00:00:00Z",
        evidence_hash="d" * 64,
    )
    assert fold_events([later, initial]).state == "covered"
    assert fold_events([later, initial]).disposition == "assigned"


def test_excluded_state_requires_approval_even_without_disposition():
    with pytest.raises(ValueError, match="approval_ref"):
        parse_event(event(state="excluded", disposition=None, approval_ref=None))
    assert fold_events([event(state="excluded", disposition=None)]).state == "excluded"
    # Resolved can describe completion without an approved publication disposition.
    assert parse_event(event(state="resolved", disposition=None, approval_ref=None)).state == (
        "resolved"
    )


class FakeEventStorage:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.paths = []
        self.error = None
        self.drop_writes = False
        self.extra_listed = []

    def get_file(self, key, path):
        self.calls.append(("read", key))
        self.paths.append(path)
        if self.error == "read":
            raise OSError("read failed")
        if key not in self.objects:
            return False
        path.write_bytes(self.objects[key])
        return True

    def put_file(self, key, path, content_type):
        self.calls.append(("write", key))
        self.paths.append(path)
        assert content_type == "application/json"
        if self.error == "write":
            raise OSError("write failed")
        if not self.drop_writes:
            self.objects[key] = path.read_bytes()
        return "https://unused.example/" + key

    def list_objects(self, prefix):
        self.calls.append(("list", prefix))
        if self.error == "list":
            raise OSError("list failed")
        return [(key, None) for key in reversed(self.objects) if key.startswith(prefix)] + [
            (key, None) for key in self.extra_listed
        ]


class FakeRemedyLease:
    key = "maintenance-leases/remedy.json"

    def __init__(self):
        from types import SimpleNamespace

        self.storage = SimpleNamespace(cas_capable=True)
        self.checked = 0
        self.lose_at = None

    def assert_held(self):
        self.checked += 1
        if self.lose_at is not None and self.checked >= self.lose_at:
            raise RuntimeError("lease lost")


def test_append_and_reconstruct_is_idempotent_and_cleans_temporary_files():
    from citypods.remedy_ledger import append_event, event_key, load_decisions

    storage, lease = FakeEventStorage(), FakeRemedyLease()
    payload = event()
    key = append_event(storage, payload, lease=lease)
    assert key == event_key(payload)
    assert key.startswith("state/remedy/events/source-a/")
    assert append_event(storage, payload, lease=lease) == key
    assert sum(call[0] == "write" for call in storage.calls) == 1
    decisions = load_decisions(storage, source_keys=["source-a"])
    assert decisions[payload["decision_id"]].disposition == "watch"
    assert not any(path.exists() for path in storage.paths)
    assert all(key.startswith("state/remedy/events/source-a/") for _, key in storage.calls)


@pytest.mark.parametrize("source", ["../source", "source/a", "", "/source", "source\\a"])
def test_unsafe_path_and_invalid_event_rejected_before_backend_calls(source):
    from citypods.remedy_ledger import append_event, load_decisions

    storage = FakeEventStorage()
    payload = event(
        source_key=source,
        decision_id=decision_id("example-tx", source or "valid", "committee", "committee"),
        recording_refs=[],
    )
    with pytest.raises(ValueError):
        append_event(storage, payload, lease=FakeRemedyLease())
    with pytest.raises(ValueError):
        load_decisions(storage, source_keys=[source])
    assert not storage.calls


@pytest.mark.parametrize("problem", ["missing", "wrong_key", "no_cas", "lost", "lost_before_write"])
def test_append_requires_owned_correct_cas_lease(problem):
    from citypods.remedy_ledger import append_event

    storage, lease = FakeEventStorage(), FakeRemedyLease()
    if problem == "missing":
        lease = None
    elif problem == "wrong_key":
        lease.key = "maintenance-leases/other.json"
    elif problem == "no_cas":
        lease.storage.cas_capable = False
    else:
        lease.lose_at = 1 if problem == "lost" else 2
    with pytest.raises((ValueError, RuntimeError)):
        append_event(storage, event(), lease=lease)
    assert not storage.objects
    assert not any(call[0] == "write" for call in storage.calls)
    assert not any(path.exists() for path in storage.paths)


@pytest.mark.parametrize("problem", ["corrupt", "different", "duplicate_key"])
def test_existing_bad_history_is_never_overwritten(problem):
    import json

    from citypods.remedy_ledger import append_event, event_key, load_decisions

    storage = FakeEventStorage()
    payload = event()
    key = event_key(payload)
    content = {
        "corrupt": b"not json",
        "different": json.dumps(event(rationale="different")).encode(),
        "duplicate_key": b'{"city":"one","city":"two"}',
    }[problem]
    storage.objects[key] = content
    with pytest.raises(ValueError):
        append_event(storage, payload, lease=FakeRemedyLease())
    with pytest.raises(ValueError):
        load_decisions(storage, source_keys=["source-a"])
    assert storage.objects[key] == content
    assert not any(call[0] == "write" for call in storage.calls)
    assert not any(path.exists() for path in storage.paths)


@pytest.mark.parametrize("problem", ["read", "write", "readback"])
def test_backend_failures_are_not_success_and_clean_files(problem):
    from citypods.remedy_ledger import append_event

    storage = FakeEventStorage()
    if problem == "readback":
        storage.drop_writes = True
    else:
        storage.error = problem
    with pytest.raises((OSError, ValueError)):
        append_event(storage, event(), lease=FakeRemedyLease())
    assert not any(path.exists() for path in storage.paths)


def test_source_scoped_load_preserves_conflicts_without_mutating():
    from citypods.remedy_ledger import append_event, load_decisions

    storage, lease = FakeEventStorage(), FakeRemedyLease()
    root = event()
    left = event(parent_event_ids=[root["event_id"]], rationale="left")
    right = event(parent_event_ids=[root["event_id"]], rationale="right")
    for payload in (root, left, right):
        append_event(storage, payload, lease=lease)
    other = event(
        source_key="source-b",
        decision_id=decision_id("example-tx", "source-b", "committee", "committee"),
        recording_refs=[],
    )
    append_event(storage, other, lease=lease)
    storage.calls.clear()
    result = load_decisions(storage, source_keys=["source-a"])
    assert len(result) == 1
    assert result[root["decision_id"]].state == "blocked"
    assert "multiple or absent tips" in result[root["decision_id"]].diagnostics
    assert all(call[0] != "write" and "source-b" not in call[1] for call in storage.calls)
    assert load_decisions(storage, source_keys=["absent"]) == {}
    assert load_decisions(storage, source_keys=[]) == {}


@pytest.mark.parametrize("problem", ["outside_prefix", "traversal", "missing", "list_failure"])
def test_reconstruction_rejects_bad_listing_or_disappearing_read(problem):
    from citypods.remedy_ledger import event_key, load_decisions

    storage = FakeEventStorage()
    storage.extra_listed = [
        {
            "outside_prefix": event_key(event()).replace("source-a/", "source-b/"),
            "traversal": "state/remedy/events/source-a/../event.json",
            "missing": event_key(event()),
            "list_failure": event_key(event()),
        }[problem]
    ]
    if problem == "list_failure":
        storage.error = "list"
    with pytest.raises((ValueError, OSError)):
        load_decisions(storage, source_keys=["source-a"])
    assert not any(call[0] == "write" for call in storage.calls)
    assert not any(path.exists() for path in storage.paths)


def test_successful_upload_with_lost_acknowledgement_retries_without_overwrite():
    from citypods.remedy_ledger import append_event, event_key

    class LostAcknowledgement(FakeEventStorage):
        def put_file(self, key, path, content_type):
            super().put_file(key, path, content_type)
            raise OSError("acknowledgement lost")

    storage, lease = LostAcknowledgement(), FakeRemedyLease()
    payload = event()
    with pytest.raises(OSError, match="acknowledgement"):
        append_event(storage, payload, lease=lease)
    assert append_event(storage, payload, lease=lease) == event_key(payload)
    assert sum(call[0] == "write" for call in storage.calls) == 1
    assert not any(path.exists() for path in storage.paths)


def test_readback_corruption_fails_without_repairing_object():
    from citypods.remedy_ledger import append_event, event_key

    class CorruptUpload(FakeEventStorage):
        def put_file(self, key, path, content_type):
            super().put_file(key, path, content_type)
            self.objects[key] = b"corrupt"

    storage = CorruptUpload()
    with pytest.raises(ValueError, match="invalid stored"):
        append_event(storage, event(), lease=FakeRemedyLease())
    assert storage.objects[event_key(event())] == b"corrupt"
    assert sum(call[0] == "write" for call in storage.calls) == 1
    assert not any(path.exists() for path in storage.paths)


def test_reconstruction_folds_delivery_retries_and_orders_decisions():
    from citypods.remedy_ledger import append_event, load_decisions

    storage, lease = FakeEventStorage(), FakeRemedyLease()
    original = event(external_delivery_id="one")
    retry = event(external_delivery_id="one", occurred_at="2026-10-11T00:00:00Z")
    another = event(
        policy_id="other", decision_id=decision_id("example-tx", "source-a", "other", "committee")
    )
    for payload in (retry, original, another):
        append_event(storage, payload, lease=lease)
    result = load_decisions(storage, source_keys=["source-a"])
    assert list(result) == sorted(result)
    assert not result[original["decision_id"]].diagnostics
    assert len(result[original["decision_id"]].events) == 2
    assert result[original["decision_id"]].disposition == "watch"


def test_reconstruction_read_error_is_not_empty_history():
    import json

    from citypods.remedy_ledger import event_key, load_decisions

    storage = FakeEventStorage()
    payload = event()
    storage.objects[event_key(payload)] = json.dumps(payload).encode()
    storage.error = "read"
    with pytest.raises(OSError, match="read failed"):
        load_decisions(storage, source_keys=["source-a"])
    assert not any(path.exists() for path in storage.paths)
