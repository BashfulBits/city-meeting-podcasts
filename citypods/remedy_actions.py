"""Validate trusted GitHub snapshots and record decisions; no production command runner."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from citypods.github_permissions import require_repository_write
from citypods.remedy_ledger import (
    RemedyEvent,
    _assert_remedy_lease,
    _unique_json_object,
    append_event,
    decision_id,
    event_id,
    fold_events,
    load_decisions,
    parse_event,
)
from citypods.remedy_policy import canonical_hash

ACTION_FIELDS = frozenset(
    "schema_version action decision_id expected_parent_event_ids city source_key policy_id "
    "normalized_label state disposition evidence_hash config_hash policy_hash "
    "recording_refs rationale".split()
)
FEED_ACTION_FIELDS = ACTION_FIELDS | {"feed_dispositions"}
EVIDENCE_FIELDS = frozenset(
    "city source_key policy_id normalized_label evidence_hash config_hash policy_hash "
    "recording_refs observed_at".split()
)
COMMAND = "/body-disposition\n"


def _utc(value, name):
    try:
        stamp = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name}: invalid timestamp") from exc
    if stamp.utcoffset() != timedelta(0):
        raise ValueError(f"{name}: expected aware UTC timestamp")
    return stamp


def _fresh(value, *, now, maximum, name):
    age = now - _utc(value, name)
    if not timedelta(0) <= age <= maximum:
        raise ValueError(f"{name}: stale or future snapshot")


def validate_decision_action(
    action,
    *,
    comment,
    permission,
    repository,
    evidence,
    decisions,
    now,
    checked_at,
    issue_number=None,
    issue_body=None,
) -> RemedyEvent:
    """Validate API response bindings; the caller must obtain responses through trusted IO."""
    now = _utc(now, "now")
    if not isinstance(comment, dict) or not isinstance(permission, dict):
        raise ValueError("snapshot: comment and permission must be objects")
    if not isinstance(comment.get("user"), dict) or not isinstance(permission.get("user"), dict):
        raise ValueError("snapshot: author identity required")
    if not isinstance(action, dict) or set(action) not in (ACTION_FIELDS, FEED_ACTION_FIELDS):
        raise ValueError("action: missing or unknown fields")
    if action["action"] not in ("decide", "reopen"):
        raise ValueError("action: unsupported operation")
    if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("repository: expected owner/name")
    _fresh(checked_at, now=now, maximum=timedelta(minutes=5), name="permission")
    require_repository_write(permission)
    author = comment.get("user", {}).get("login")
    if (
        not isinstance(author, str)
        or not author
        or permission.get("user", {}).get("login") != author
    ):
        raise ValueError("permission: author mismatch")
    comment_id = comment.get("id")
    if type(comment_id) is not int or comment_id <= 0:
        raise ValueError("comment: invalid delivery ID")
    url = comment.get("html_url")
    pattern = (
        rf"https://github\.com/{re.escape(repository)}/issues/[1-9][0-9]*#issuecomment-{comment_id}"
    )
    if not isinstance(url, str) or not re.fullmatch(pattern, url):
        raise ValueError("comment: repository or delivery URL mismatch")
    body = comment.get("body")
    if not isinstance(body, str) or not body.startswith(COMMAND):
        raise ValueError("comment: dedicated command required")
    try:
        submitted = json.loads(body[len(COMMAND) :], object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError) as exc:
        raise ValueError("comment: invalid command JSON") from exc
    if submitted != action:
        raise ValueError("comment: approved content mismatch")
    if "feed_dispositions" in action:
        _validate_issue_feed_choices(
            action,
            issue_number=issue_number,
            issue_body=issue_body,
            comment_url=url,
        )
    created = _utc(comment.get("created_at"), "comment.created_at")
    if created > now:
        raise ValueError("comment: future delivery")
    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_FIELDS:
        raise ValueError("evidence: missing or unknown fields")
    _fresh(evidence["observed_at"], now=now, maximum=timedelta(hours=24), name="evidence")
    for field in EVIDENCE_FIELDS - {"observed_at"}:
        if canonical_hash(evidence[field]) != canonical_hash(action[field]):
            raise ValueError(f"evidence: {field} mismatch")
    payload = {
        key: value
        for key, value in action.items()
        if key not in {"action", "expected_parent_event_ids"}
    }
    payload.update(
        parent_event_ids=action["expected_parent_event_ids"],
        occurred_at=created.astimezone(UTC).isoformat(),
        actor_kind="maintainer",
        actor_id=author,
        artifact_ref=url,
        approval_ref=url,
        external_delivery_id=f"{repository}:issuecomment:{comment_id}",
        provenance=[],
        event_id="",
    )
    payload["event_id"] = event_id(payload)
    candidate = parse_event(payload)
    if action["action"] == "reopen" and (
        candidate.state != "proposed" or candidate.disposition is not None
    ):
        raise ValueError("reopen: requires proposed state and null disposition")
    history = decisions.get(candidate.decision_id)
    if history is not None:
        history = fold_events(history.events)
        if history.decision_id != candidate.decision_id or history.diagnostics:
            raise ValueError("history: conflicting or damaged decision")
        parent_events = [e for e in history.events if e.event_id in candidate.parent_event_ids]
        if action["action"] == "decide" and any(
            e.state in {"covered", "excluded", "resolved"}
            and (candidate.state, candidate.disposition) != (e.state, e.disposition)
            for e in parent_events
        ):
            raise ValueError("history: explicit reopen required")
        for prior in history.events:
            if prior.external_delivery_id == candidate.external_delivery_id:
                if prior.payload() != candidate.payload():
                    raise ValueError("delivery: conflicting retry")
                return prior
        if candidate.parent_event_ids != history.tip_ids:
            raise ValueError("parents: current tips required")
        if action["action"] == "decide" and history.state in {"covered", "excluded", "resolved"}:
            tip = next(e for e in history.events if e.event_id == history.tip_ids[0])
            if (candidate.state, candidate.disposition) != (tip.state, tip.disposition):
                raise ValueError("history: explicit reopen required")
    elif candidate.parent_event_ids or action["action"] == "reopen":
        raise ValueError("parents: initial decision required")
    return candidate


def record_decision_action(storage, action, *, lease, trusted_snapshot, evidence, now) -> str:
    """Reconstruct under the lease and safely retry a previously acknowledged comment."""
    required_snapshot = {"repository", "comment", "permission", "checked_at"}
    optional_snapshot = {"issue_number", "issue_body"}
    if not isinstance(trusted_snapshot, dict) or set(trusted_snapshot) not in (
        required_snapshot,
        required_snapshot | optional_snapshot,
    ):
        raise ValueError("snapshot: missing or unknown fields")
    if not isinstance(action, dict) or set(action) not in (ACTION_FIELDS, FEED_ACTION_FIELDS):
        raise ValueError("action: missing or unknown fields")
    _assert_remedy_lease(lease)
    decisions = load_decisions(storage, source_keys=[action["source_key"]])
    event = validate_decision_action(
        action, evidence=evidence, decisions=decisions, now=now, **trusted_snapshot
    )
    return append_event(storage, event, lease=lease)


def _validate_issue_feed_choices(action, *, issue_number, issue_body, comment_url):
    """Require checkbox dispositions to match the issue whose action is being recorded."""
    if type(issue_number) is not int or issue_number <= 0 or not isinstance(issue_body, str):
        raise ValueError("issue: trusted number and body are required for feed dispositions")
    match = re.search(r"/issues/([1-9][0-9]*)#issuecomment-", comment_url)
    if match is None or int(match.group(1)) != issue_number:
        raise ValueError("issue: action comment is not on the bound issue")
    binding_matches = re.findall(r"<!-- citypods:remedy-p3:binding=([^\n]+) -->", issue_body)
    if len(binding_matches) != 1:
        raise ValueError("issue: expected one P3 decision binding")
    try:
        binding = json.loads(binding_matches[0], object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError) as exc:
        raise ValueError("issue: invalid P3 decision binding") from exc
    binding_fields = {
        "city",
        "source_key",
        "policy_id",
        "normalized_label",
        "decision_id",
        "evidence_hash",
        "config_hash",
        "policy_hash",
        "recording_refs",
        "expected_parent_event_ids",
        "observed_at",
        "feed_slugs",
    }
    if not isinstance(binding, dict) or set(binding) != binding_fields:
        raise ValueError("issue: invalid P3 decision binding fields")
    identity = decision_id(
        action["city"],
        action["source_key"],
        action["policy_id"],
        action["normalized_label"],
    )
    for field in (
        "city",
        "source_key",
        "policy_id",
        "normalized_label",
        "evidence_hash",
        "config_hash",
        "policy_hash",
        "recording_refs",
    ):
        if binding[field] != action[field]:
            raise ValueError(f"issue: P3 binding {field} mismatch")
    if binding["decision_id"] != identity:
        raise ValueError("issue: P3 decision identity mismatch")
    if sorted(binding["expected_parent_event_ids"]) != sorted(action["expected_parent_event_ids"]):
        raise ValueError("issue: P3 ledger parents mismatch")
    if not isinstance(action["feed_dispositions"], list) or any(
        not isinstance(item, dict) or set(item) != {"feed_slug", "disposition"}
        for item in action["feed_dispositions"]
    ):
        raise ValueError("issue: invalid feed disposition set")
    slugs = [item["feed_slug"] for item in action["feed_dispositions"]]
    candidate_slugs = binding["feed_slugs"]
    if (
        not isinstance(candidate_slugs, list)
        or any(not isinstance(slug, str) for slug in candidate_slugs)
        or candidate_slugs != sorted(set(candidate_slugs))
        or any(not isinstance(slug, str) for slug in slugs)
        or len(slugs) != len(set(slugs))
    ):
        raise ValueError("issue: invalid feed disposition set")
    selected = {}
    for slug in candidate_slugs:
        include = f"- [x] Include `{slug}`"
        exclude = f"- [x] Exclude `{slug}`"
        checked_count = issue_body.count(include) + issue_body.count(exclude)
        if checked_count > 1:
            raise ValueError(f"issue: conflicting include/exclude checkbox for {slug}")
        if checked_count:
            selected[slug] = "include" if include in issue_body else "exclude"
    no_assignment = "- [x] No feed assignment" in issue_body
    hold = "- [x] Hold for future evidence" in issue_body
    if no_assignment and hold:
        raise ValueError("issue: overall dispositions conflict")
    overall_checked = len(
        re.findall(r"- \[x\] (?:No feed assignment|Hold for future evidence)", issue_body)
    )
    if overall_checked > 1:
        raise ValueError("issue: conflicting overall disposition")
    if hold:
        if selected or slugs or action["state"] != "blocked" or action["disposition"] != "watch":
            raise ValueError("issue: future-evidence hold cannot include feed choices")
    elif no_assignment and (
        action["state"] != "excluded" or action["disposition"] != "not_pursued"
    ):
        raise ValueError("issue: no-assignment action must be not_pursued")
    elif not no_assignment and (
        action["state"] != "covered" or action["disposition"] != "assigned"
    ):
        raise ValueError("issue: feed assignment must be covered/assigned")
    if no_assignment and any(value != "exclude" for value in selected.values()):
        raise ValueError("issue: no-assignment disposition requires excluding every feed")
    if (
        not no_assignment
        and not hold
        and not any(value == "include" for value in selected.values())
    ):
        raise ValueError("issue: select an included feed or an overall disposition")
    if not hold and set(selected) != set(candidate_slugs):
        raise ValueError("issue: choose an include/exclude checkbox for every candidate feed")
    if selected != {item["feed_slug"]: item["disposition"] for item in action["feed_dispositions"]}:
        raise ValueError("issue: checked boxes differ from submitted feed dispositions")
